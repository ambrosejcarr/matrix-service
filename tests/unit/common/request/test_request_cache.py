import copy
import hashlib
import random
import uuid

from unittest import mock

from matrix.common.aws.cloudwatch_handler import MetricName
from matrix.common.request.request_cache import RequestCache, RequestIdNotFound
from tests.unit import MatrixTestCaseUsingMockAWS


class TestRequestCache(MatrixTestCaseUsingMockAWS):

    def setUp(self):
        super(TestRequestCache, self).setUp()

        self.request_id = str(uuid.uuid4())
        self.request_hash = hashlib.sha256().hexdigest()
        self.request_cache = RequestCache(self.request_id)

        self.create_test_cache_table()

    def test_uninitialized_request(self):

        with self.assertRaises(RequestIdNotFound):
            self.request_cache.retrieve_hash()

    def test_set_and_retrieve_hash(self):

        bundle_fqids = ["bundle1.version0", "bundle2.version0"]
        format_ = "test_format"

        hash_1 = self.request_cache.set_hash(bundle_fqids, format_)

        copy_bundle_fqids = copy.deepcopy(bundle_fqids)
        random.shuffle(copy_bundle_fqids)
        hash_2 = RequestCache(str(uuid.uuid4())).set_hash(copy_bundle_fqids, format_)
        hash_3 = RequestCache(str(uuid.uuid4())).set_hash(bundle_fqids, format_ + '_')

        self.assertEqual(hash_1, hash_2)
        self.assertNotEqual(hash_2, hash_3)

        retrieved_hash = self.request_cache.retrieve_hash()
        self.assertEqual(retrieved_hash, hash_1)

    @mock.patch("matrix.common.aws.cloudwatch_handler.CloudwatchHandler.put_metric_data")
    def test_initialize(self, mock_cw_put):
        self.request_cache.initialize()
        mock_cw_put.assert_called_once_with(metric_name=MetricName.REQUEST, metric_value=1)
        self.assertIsNone(self.request_cache.retrieve_hash())
