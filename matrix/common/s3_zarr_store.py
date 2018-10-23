import os
import math
import json
import time

import s3fs
import zarr
import numpy

from matrix.common.constants import ZarrayName
from matrix.common.dynamo_handler import DynamoHandler
from matrix.common.dynamo_handler import DynamoTable
from matrix.common.dynamo_handler import OutputTableField
from matrix.common.dynamo_utils import Lock
from matrix.common.exceptions import MatrixException


ZARR_OUTPUT_CONFIG = {
    "cells_per_chunk": 3000,
    "compressor": zarr.storage.default_compressor,
    "dtypes": {
        "expression": "<f4",
        "cell_id": "<U64",
        "cell_metadata_numeric": "<f4",
        "cell_metadata_string": "<U64"
    },
    "order": "C"
}


class S3ZarrStore:

    def __init__(self, request_id: str, exp_df=None, qc_df=None):
        self._request_id = request_id
        self._results_bucket = os.environ['S3_RESULTS_BUCKET']
        self.s3_results_prefix = f"s3://{self._results_bucket}/{self._request_id}.zarr"
        self._cells_per_chunk = ZARR_OUTPUT_CONFIG['cells_per_chunk']
        self.dynamo_handler = DynamoHandler()
        self.s3_file_system = s3fs.S3FileSystem(anon=False)
        self.exp_df = exp_df
        self.qc_df = qc_df

    def write_from_pandas_dfs(self, num_rows: int):
        """Write specified number of rows from matrix dataframes to s3 results bucket.

        Input:
            num_rows: (int) number of rows to write from input dataframes
        """
        # Figure out which rows of the output table this filtered chunk will be assigned.
        output_start_row_idx, output_end_row_idx = self._get_output_row_boundaries(num_rows)
        # Based on that, determine which zarr chunks we need to write to
        output_start_chunk_idx, output_end_chunk_idx = self._get_output_chunk_boundaries(
            output_start_row_idx,
            output_end_row_idx
        )

        # Now iterate through each chunk we're supposed to write to, and write the
        # appropriate rows to each one.
        written_rows = 0
        for chunk_idx in range(output_start_chunk_idx, output_end_chunk_idx):
            output_chunk_start = chunk_idx * self._cells_per_chunk

            # Get the start and end rows in the filtered matrix that correspond to
            # this chunk as well as the start and end rows in the chunk.
            input_row_start = int(written_rows)
            output_row_start = int(max(0, output_start_row_idx - output_chunk_start))
            input_row_end = int(min(output_end_row_idx - output_start_row_idx,
                                    input_row_start + self._cells_per_chunk - output_row_start))
            output_row_end = int(output_row_start + input_row_end - input_row_start)
            print(f"Writing {input_row_start}:{input_row_end} --> {output_row_start}:{output_row_end}")

            input_bounds = (input_row_start, input_row_end)
            output_bounds = (output_row_start, output_row_end)
            self._write_row_data_to_results_chunk(chunk_idx, input_bounds, output_bounds)
            row_count = input_row_end - input_row_start
            written_rows += row_count

    def _write_row_data_to_results_chunk(self, chunk_idx: int, input_bounds: tuple, output_bounds: tuple):
        """Write bounded row data from input to specified results chunk.

        Input:
            chunk_idx: (str) index corresponding with index of chunk in s3 zarr store
            input_bounds: (tuple) Beginning and end of boundaries for input rows
            output_bounds: (tuple) Beginning and end of boundaries in output rows
        """
        # TODO TEST THIS FUNCTION
        for dset in ["expression", "cell_metadata_numeric", "cell_metadata_string", "cell_id"]:
            if dset == "expression":
                values = self.exp_df.values
            elif dset == "cell_metadata_numeric":
                values = self.qc_df.select_dtypes("float32").values
            elif dset == "cell_metadata_string":
                values = self.qc_df.select_dtypes("object").values
            elif dset == "cell_id":
                values = self.exp_df.index.values
            full_dest_key = f"s3://{self._results_bucket}/{self._request_id}.zarr/{dset}/{chunk_idx}"
            print(f"Writing {dset} to {full_dest_key}")
            if values.ndim == 2:
                chunk_shape = (self._cells_per_chunk, values.shape[1])
                full_dest_key += ".0"
            else:
                chunk_shape = (self._cells_per_chunk,)
            dtype = ZARR_OUTPUT_CONFIG['dtypes'][dset]

            # Reading and writing zarr chunks is pretty straightforward, you
            # just pass it through the compression and cast it to a numpy array
            with Lock(full_dest_key):
                # This is a graceless workaround for the issue identified here:
                # https://github.com/pangeo-data/pangeo/issues/196
                # Sometimes blosc.decode raises a RuntimeError that goes away on retry
                # TODO: factor out the retry logic
                num_tries = 0
                delay = 1
                while True:
                    try:
                        arr = numpy.frombuffer(
                            ZARR_OUTPUT_CONFIG['compressor'].decode(
                                self.s3_file_system.open(full_dest_key, 'rb').read()),
                            dtype=dtype).reshape(chunk_shape, order=ZARR_OUTPUT_CONFIG['order'])
                        break
                    except FileNotFoundError:
                        arr = numpy.zeros(shape=chunk_shape,
                                          dtype=dtype)
                        print("Created new array")
                        break
                    except RuntimeError:
                        if num_tries > 10:
                            raise
                        time.sleep(delay)
                        num_tries += 1
                        delay *= 1.6

                arr.setflags(write=1)
                arr[output_bounds[0]:output_bounds[1]] = values[input_bounds[0]:input_bounds[1]]
                self.s3_file_system.open(full_dest_key, 'wb').write(ZARR_OUTPUT_CONFIG['compressor'].encode(arr))

    def write_group_metadata(self):
        """
        Writes the group and zarray metadata of a complete expression matrix zarr in S3.
        """
        self._write_zgroup_metadata()

        num_output_rows = int(self.dynamo_handler.get_table_item(DynamoTable.OUTPUT_TABLE,
                                                                 self._request_id)[OutputTableField.ROW_COUNT.value])
        for zarray in [ZarrayName.EXPRESSION,
                       ZarrayName.CELL_METADATA_NUMERIC,
                       ZarrayName.CELL_METADATA_STRING,
                       ZarrayName.CELL_ID]:
            self._write_zarray_metadata(zarray, num_output_rows)

    def _write_zgroup_metadata(self):
        """
        Writes the top level .zgroup file to this zarr store in S3.
        """
        data = json.dumps({'zarr_format': 2}).encode()
        self.s3_file_system.open(f"{self.s3_results_prefix}/.zgroup", 'wb').write(data)

    def _write_zarray_metadata(self, zarray: ZarrayName, row_count: int):
        """
        Writes the metadata for the specified zarray to this zarr store in S3.
        :param zarray: Expression matrix zarray for which the metadata will be written for
        :param row_count: Total number of output rows.
        """
        num_cols = self._get_zarray_column_count(zarray)
        chunks = [ZARR_OUTPUT_CONFIG["cells_per_chunk"]]
        shape = [row_count]

        if num_cols:
            chunks.append(num_cols)
            shape.append(num_cols)

        zarray_metadata = {
            "chunks": chunks,
            "compressor": ZARR_OUTPUT_CONFIG['compressor'].get_config(),
            "dtype": ZARR_OUTPUT_CONFIG['dtypes'][zarray.value],
            "fill_value": self._fill_value(numpy.dtype(ZARR_OUTPUT_CONFIG['dtypes'][zarray.value])),
            "filters": None,
            "order": ZARR_OUTPUT_CONFIG['order'],
            "shape": shape,
            "zarr_format": 2
        }
        zarray_key = f"{self.s3_results_prefix}/{zarray.value}/.zarray"
        self.s3_file_system.open(zarray_key, "wb").write(json.dumps(zarray_metadata).encode())

    def _read_zarray(self, zarray: ZarrayName):
        s3_location = f"s3://{self._results_bucket}/{self._request_id}.zarr/{zarray.value}/.zarray"
        data = self.s3_file_system.open(s3_location, 'rb').read()
        return json.loads(data)

    def _get_zarray_column_count(self, zarray: ZarrayName):
        if zarray == ZarrayName.EXPRESSION:
            return int(self._read_zarray(ZarrayName.GENE_ID)['chunks'][0])
        elif zarray == ZarrayName.CELL_METADATA_NUMERIC:
            return int(self._read_zarray(ZarrayName.CELL_METADATA_NUMERIC_NAME)['chunks'][0])
        elif zarray == ZarrayName.CELL_METADATA_STRING:
            return int(self._read_zarray(ZarrayName.CELL_METADATA_STRING_NAME)['chunks'][0])
        elif zarray == ZarrayName.CELL_ID:
            return 0
        else:
            raise MatrixException(400, f"Unsupported ZarrayName value supplied {zarray.value}.")

    def write_column_data(self, group: zarr.Group):
        """Write all column data from input to results s3 bucket.

        Input:
            group: (str) zarr.Group representation of dss zarr store
        """
        # TO DO TEST THIS FUNCTION
        for dset in ["gene_id", "cell_metadata_numeric_name", "cell_metadata_string_name"]:
            full_dest_key = f"s3://{self._results_bucket}/{self._request_id}.zarr/{dset}/0"
            if not self.s3_file_system.exists(full_dest_key):
                with Lock(full_dest_key):
                    arr = numpy.array(getattr(group, dset))
                    self.s3_file_system.open(full_dest_key, 'wb').write(ZARR_OUTPUT_CONFIG['compressor'].encode(arr))

                zarray_key = f"s3://{self._results_bucket}/{self._request_id}.zarr/{dset}/.zarray"
                zarray = {
                    "chunks": [arr.shape[0]],
                    "compressor": ZARR_OUTPUT_CONFIG['compressor'].get_config(),
                    "dtype": str(arr.dtype),
                    "fill_value": self._fill_value(arr.dtype),
                    "filters": None,
                    "order": ZARR_OUTPUT_CONFIG['order'],
                    "shape": [arr.shape[0]],
                    "zarr_format": 2
                }
                with Lock(zarray_key):
                    self.s3_file_system.open(zarray_key, 'wb').write(json.dumps(zarray).encode())

    def _get_output_row_boundaries(self, nrows: int):
        """Get the start and end rows in the output table to write to.

        Input:
            nrows: number of rows for current chunk
        Output:
            Tuple:
                output_start_row_idx: (int) start row in output table to begin writing
                output_start_end_idx: (int) end row in output table at which to end writing
        """
        field_enum = OutputTableField.ROW_COUNT
        output_start_row_idx, output_end_row_idx = self.dynamo_handler.increment_table_field(
            DynamoTable.OUTPUT_TABLE, self._request_id, field_enum, nrows)
        return output_start_row_idx, output_end_row_idx

    def _get_output_chunk_boundaries(self, output_start_row_idx: int, output_end_row_idx: int):
        """Get the start and end chunks in the output table to write to.

        Input:
            output_start_row_idx: (int) start row in output table to begin writing
            output_end_row_idx:   (int) end row in output table to end writing

        Output:
            Tuple:
                start_chunk_idx: (int) start chunk in output table to begin writing
                end_chunk_idx: (int) end chunk in output table at which to end writing
        """
        output_start_chunk_idx = math.floor(output_start_row_idx / self._cells_per_chunk)
        output_end_chunk_idx = math.ceil(output_end_row_idx / self._cells_per_chunk)
        return output_start_chunk_idx, output_end_chunk_idx

    def _fill_value(self, dtype: str):
        if dtype.kind == 'f':
            return float(0)
        elif dtype.kind == 'i':
            return 0
        elif dtype.kind == 'U':
            return ""
