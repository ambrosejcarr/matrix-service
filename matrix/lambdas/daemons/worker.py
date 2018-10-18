import concurrent.futures
import math
import os
import typing

import pandas
import zarr

from matrix.common.dss_zarr_store import DSSZarrStore
from matrix.common.dynamo_handler import DynamoHandler, DynamoTable, StateTableField
from matrix.common.lambda_handler import LambdaHandler, LambdaName
from matrix.common.logging import Logging
from matrix.common.pandas_utils import convert_dss_zarr_root_to_subset_pandas_dfs
from matrix.common.s3_zarr_store import S3ZarrStore

logger = Logging.get_logger(__name__)


class Worker:
    """
    The worker (third) task in a distributed filter merge job.
    """
    def __init__(self, request_id: str):
        Logging.set_correlation_id(logger, value=request_id)

        self.lambda_handler = LambdaHandler()
        self.dynamo_handler = DynamoHandler()
        self._request_id = request_id
        self._deployment_stage = os.environ['DEPLOYMENT_STAGE']
        self._bundle_uuids = []
        self._bundle_versions = []
        self._input_start_rows = []
        self._input_end_rows = []
        self._num_rows = []
        self.zarr_group = None

    def run(self, worker_chunk_spec: typing.List[dict]):
        """Process and write one chunk of dss bundle matrix to s3 and
        invoke reducer lambda when last worker job is completed.

        Inputs:
        worker_chunk_spec: (dict) Information about input bundle and matrix row indices to process
        """
        logger.debug(f"Worker running with parameters: worker_chunk_spec={worker_chunk_spec}, format={format}")
        # TO DO pass in the parameters in worker chunk spec flat
        self._parse_worker_chunk_spec(worker_chunk_spec)
        num_bundles = len(self._bundle_uuids)
        exp_dfs = []
        qc_dfs = []

        # Parallelize high latency bundle reads from DSS
        with concurrent.futures.ThreadPoolExecutor(max_workers=25) as executor:
            future_to_chunk_map = {executor.submit(self._parse_chunk_to_dataframe, chunk_idx): chunk_idx
                                   for chunk_idx in range(num_bundles)}
            for future in concurrent.futures.as_completed(future_to_chunk_map):
                chunk_idx = future_to_chunk_map[future]
                try:
                    exp_df, qc_df = future.result()
                except Exception as e:
                    logger.debug(f"Parsing bundle uuid {self._bundle_uuids[chunk_idx]} from DSS "
                                 f"to pandas.DataFrame caused exception {e}")
                    raise
                exp_dfs.append(exp_df)
                qc_dfs.append(qc_df)

                # log every tertile of bundles read
                if any(chunk_idx + 1 == math.ceil(num_bundles * ((i + 1) / 3)) for i in range(3)):
                    logger.debug(f"{chunk_idx + 1} of {num_bundles} bundles successfully read from the DSS")

        # In some test cases, empty dataframes are actually returned. Don't try to
        # pass those to pandas.concat
        if any(not df.empty for df in exp_dfs):
            exp_df = pandas.concat(exp_dfs, axis=0, copy=False)
            qc_df = pandas.concat(qc_dfs, axis=0, copy=False)
        else:
            exp_df, qc_df = None, None

        s3_zarr_store = S3ZarrStore(request_id=self._request_id, exp_df=exp_df, qc_df=qc_df)
        s3_zarr_store.write_from_pandas_dfs(sum(self._num_rows))

        self.dynamo_handler.increment_table_field(DynamoTable.STATE_TABLE,
                                                  self._request_id,
                                                  StateTableField.COMPLETED_WORKER_EXECUTIONS,
                                                  1)

        workers_and_mappers_are_complete = self._check_if_all_workers_and_mappers_for_request_are_complete(
            self._request_id)
        if workers_and_mappers_are_complete:
            logger.debug("Mappers and workers are complete. Invoking reducer.")

            s3_zarr_store.write_column_data(self.zarr_group)
            reducer_payload = {
                'request_id': self._request_id
            }
            self.lambda_handler.invoke(LambdaName.REDUCER, reducer_payload)

    def _parse_chunk_to_dataframe(self, i: int):
        dss_zarr_store = DSSZarrStore(bundle_uuid=self._bundle_uuids[i],
                                      bundle_version=self._bundle_versions[i],
                                      dss_instance=self._deployment_stage)
        group = zarr.group(store=dss_zarr_store)
        if not self.zarr_group:
            self.zarr_group = group
        return convert_dss_zarr_root_to_subset_pandas_dfs(group, self._input_start_rows[i], self._input_end_rows[i])

    def _parse_worker_chunk_spec(self, worker_chunk_spec: typing.List[dict]):
        """Parse worker chunk spec into Worker instance variables.

        Input:
            worker_chunk_spec: (dict) keys expected 'bundle_uuid', 'bundle_version',
                               'start_row', 'num_rows'
        """
        for entry in worker_chunk_spec:
            self._bundle_uuids.append(entry['bundle_uuid'])
            self._bundle_versions.append(entry['bundle_version'])
            self._input_start_rows.append(entry['start_row'])
            self._input_end_rows.append(entry['start_row'] + entry['num_rows'])
            self._num_rows.append(entry['num_rows'])

    def _check_if_all_workers_and_mappers_for_request_are_complete(self, request_id):
        """Check if all workers and mappers for request are completed.

        Input:
            request_id: (str) request id for filter merge job
        Output:
            Bool
        """
        complete = False
        entry = self.dynamo_handler.get_table_item(DynamoTable.STATE_TABLE, request_id)
        done_mapping = (entry[StateTableField.EXPECTED_MAPPER_EXECUTIONS.value] ==
                        entry[StateTableField.COMPLETED_MAPPER_EXECUTIONS.value])
        done_working = (entry[StateTableField.EXPECTED_WORKER_EXECUTIONS.value] ==
                        entry[StateTableField.COMPLETED_WORKER_EXECUTIONS.value])
        if done_mapping and done_working:
            complete = True
        return complete
