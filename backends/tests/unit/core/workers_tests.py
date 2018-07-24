# Copyright 2018 Google Inc
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import types
import unittest

from apiclient.errors import HttpError
import cloudstorage
from google.appengine.ext import testbed
from google.cloud.bigquery.dataset import Dataset
from google.cloud.bigquery.table import Table
from google.cloud.exceptions import ClientError
import mock

from core import workers


class TestAbstractWorker(unittest.TestCase):

  def setUp(self):
    super(TestAbstractWorker, self).setUp()
    self.testbed = testbed.Testbed()
    self.testbed.activate()
    # Activate which service we want to stub
    self.testbed.init_taskqueue_stub()
    self.testbed.init_memcache_stub()
    self.testbed.init_app_identity_stub()

  def tearDown(self):
    super(TestAbstractWorker, self).tearDown()
    self.testbed.deactivate()

  def test_default_params_values(self):
    class DummyWorker(workers.Worker):
      PARAMS = [
        ('int_with_default', 'number', True, 20, 'Description'),
      ]
    worker = DummyWorker({}, 1, 1)
    self.assertIsInstance(worker._params['int_with_default'], int)
    self.assertEqual(worker._params['int_with_default'], 20)

  @mock.patch('core.logging.logger')
  def test_log_info_succeeds(self, patched_logger):
    patched_logger.log_struct.__name__ = 'foo'
    worker = workers.Worker({}, 1, 1)
    self.assertEqual(patched_logger.log_struct.call_count, 0)
    worker.log_info('Hi there!')
    self.assertEqual(patched_logger.log_struct.call_count, 1)
    call_first_arg = patched_logger.log_struct.call_args[0][0]
    self.assertEqual(call_first_arg.get('log_level'), 'INFO')

  @mock.patch('core.logging.logger')
  def test_log_warn_succeeds(self, patched_logger):
    patched_logger.log_struct.__name__ = 'foo'
    worker = workers.Worker({}, 1, 1)
    self.assertEqual(patched_logger.log_struct.call_count, 0)
    worker.log_warn('Hi there!')
    self.assertEqual(patched_logger.log_struct.call_count, 1)
    call_first_arg = patched_logger.log_struct.call_args[0][0]
    self.assertEqual(call_first_arg.get('log_level'), 'WARNING')

  @mock.patch('core.logging.logger')
  def test_log_error_succeeds(self, patched_logger):
    patched_logger.log_struct.__name__ = 'foo'
    worker = workers.Worker({}, 1, 1)
    self.assertEqual(patched_logger.log_struct.call_count, 0)
    worker.log_error('Hi there!')
    self.assertEqual(patched_logger.log_struct.call_count, 1)
    call_first_arg = patched_logger.log_struct.call_args[0][0]
    self.assertEqual(call_first_arg.get('log_level'), 'ERROR')

  @mock.patch('core.logging.logger')
  def test_execute_client_error_raises_worker_exception(self, patched_logger):
    patched_logger.log_struct.__name__ = 'foo'
    class WorkerRaisingClientError(workers.Worker):
      def _execute(self):
        raise ClientError('There has been an issue here.')
        yield None
    worker = WorkerRaisingClientError({}, 1, 1)
    with self.assertRaises(workers.WorkerException):
      result = worker.execute()
      self.assertIsInstance(result, types.GeneratorType)
      # We need to access at least one item otherwise the generator
      # is garbage collected.
      enqueued_worker = next(result)

  def test_enqueue_formats_as_tuple(self):
    worker = workers.Worker({}, 1, 1)
    result = worker._enqueue('DummyClass', 'params')
    self.assertIsInstance(result, tuple)
    self.assertEqual(result[0], 'DummyClass')
    self.assertEqual(result[1], 'params')

  @mock.patch('core.logging.logger')
  def test_execute_returns_generator_of_enqueued_workers(self, patched_logger):
    patched_logger.log_struct.__name__ = 'foo'
    class WorkerEnqueuingJob(workers.Worker):
      def _execute(self):
        yield self._enqueue('DummyClass', 'params')
    worker = WorkerEnqueuingJob({}, 1, 1)
    result = worker.execute()
    self.assertIsInstance(result, types.GeneratorType)
    enqueued_worker = next(result)
    self.assertIsInstance(enqueued_worker, tuple)
    self.assertEqual(enqueued_worker[0], 'DummyClass')
    self.assertEqual(enqueued_worker[1], 'params')

  @mock.patch('time.sleep')
  @mock.patch('core.logging.logger')
  def test_retry_until_a_finite_number_of_times(self, patched_logger,
      patched_time_sleep):
    patched_logger.log_struct.__name__ = 'foo'
    # NB: bypass the time.sleep wait, otherwise the test will take ages
    patched_time_sleep.side_effect = lambda delay: delay
    worker = workers.Worker({}, 1, 1)
    def _raise_value_error_exception(*args, **kwargs):
      raise ValueError('Wrong value.')
    fake_request = mock.Mock()
    fake_request.__name__ = 'foo'
    fake_request.side_effect = _raise_value_error_exception
    with self.assertRaises(ValueError):
      worker.retry(fake_request)()
    self.assertGreaterEqual(fake_request.call_count, 2)

  def test_retry_raises_error_if_bad_request_error(self):
    worker = workers.Worker({}, 1, 1)
    def _raise_value_error_exception(*args, **kwargs):
      raise HttpError(mock.Mock(status=400), '')
    fake_request = mock.Mock()
    fake_request.__name__ = 'foo'
    fake_request.side_effect = _raise_value_error_exception
    with self.assertRaises(HttpError):
      worker.retry(fake_request)()
    self.assertEqual(fake_request.call_count, 1)


class TestBQWorker(unittest.TestCase):

  @mock.patch('time.sleep')
  @mock.patch('google.cloud.bigquery.job.QueryJob')
  def test_begin_and_wait_start_jobs(self, patched_bigquery_QueryJob,
      patched_time_sleep):
    # NB: bypass the time.sleep wait, otherwise the test will take ages
    patched_time_sleep.side_effect = lambda delay: delay
    worker = workers.BQWorker({}, 1, 1)
    job0 = patched_bigquery_QueryJob()
    job0.begin.side_effect = lambda: True
    def _mark_as_done():
      job0.state = 'DONE'
    job0.reload.side_effect = _mark_as_done
    job0.error_result = None
    # Consume the generator to avoid garbage collection.
    generator = worker._begin_and_wait(job0)
    workers_to_enqueue = list(generator)
    job0.begin.assert_called_once()

  @mock.patch('time.sleep')
  @mock.patch('google.cloud.bigquery.job.QueryJob')
  @mock.patch('core.workers.BQWorker._enqueue')
  def test_begin_and_wait_enqueue_bqwaiter_after_some_time(self,
      patched_BQWorker_enqueue, patched_bigquery_QueryJob, patched_time_sleep):
    # NB: bypass the time.sleep wait, otherwise the test will take ages
    patched_time_sleep.side_effect = lambda delay: delay
    def _fake_enqueue(*args, **kwargs):
      # Do Nothing
      return True
    patched_BQWorker_enqueue.side_effect = _fake_enqueue
    worker = workers.BQWorker({'bq_project_id': 'BQID'}, 1, 1)
    job0 = patched_bigquery_QueryJob()
    job0.error_result = None
    # Consume the generator to avoid garbage collection.
    generator = worker._begin_and_wait(job0)
    workers_to_enqueue = list(generator)
    patched_BQWorker_enqueue.assert_called_once()
    self.assertEqual(patched_BQWorker_enqueue.call_args[0][0], 'BQWaiter')
    self.assertIsInstance(patched_BQWorker_enqueue.call_args[0][1], dict)


class TestBQWaiter(unittest.TestCase):

  def test_execute_enqueue_job_if_done(self):
    patcher_get_client = mock.patch.object(workers.BQWaiter, '_get_client',
        return_value=None)
    self.addCleanup(patcher_get_client.stop)
    patcher_get_client.start()
    mockAsyncJob = mock.Mock()
    mockAsyncJob.error_result = None
    patcher_async_job = mock.patch('google.cloud.bigquery.job._AsyncJob',
        return_value=mockAsyncJob)
    self.addCleanup(patcher_async_job.stop)
    patcher_async_job.start()
    patcher_worker_enqueue = mock.patch('core.workers.BQWaiter._enqueue')
    self.addCleanup(patcher_worker_enqueue.stop)
    patched_enqueue = patcher_worker_enqueue.start()
    worker = workers.BQWaiter(
        {
            'bq_project_id': 'BQID',
            'job_names': ['Job1', 'Job2'],
        },
        1,
        1)
    worker._client = mock.Mock()

    # Consume the generator to avoid garbage collection.
    generator = worker._execute()
    workers_to_enqueue = list(generator)

    patched_enqueue.assert_called_once()
    self.assertEqual(patched_enqueue.call_args[0][0], 'BQWaiter')


class TestStorageToBQImporter(unittest.TestCase):

  def setUp(self):
    super(TestStorageToBQImporter, self).setUp()
    self.testbed = testbed.Testbed()
    self.testbed.activate()
    # Activate which service we want to stub
    self.testbed.init_urlfetch_stub()
    self.testbed.init_memcache_stub()
    self.testbed.init_app_identity_stub()
    self.testbed.init_blobstore_stub()
    self.testbed.init_datastore_v3_stub()

    patcher_listbucket = mock.patch('cloudstorage.listbucket')
    patched_listbucket = patcher_listbucket.start()
    self.addCleanup(patcher_listbucket.stop)
    def _fake_listbucket(bucket_prefix):
      filenames = [
        'input.csv',
        'subdir/input.csv',
        'data.csv',
        'subdir/data.csv',
      ]
      for suffix in filenames:
        filename = os.path.join(bucket_prefix, suffix)
        stat = cloudstorage.GCSFileStat(
            filename,
            0,
            '686897696a7c876b7e',
            0)
        yield stat
    patched_listbucket.side_effect = _fake_listbucket

  def tearDown(self):
    super(TestStorageToBQImporter, self).tearDown()
    self.testbed.deactivate()

  def test_get_source_uris_succeeds(self):
    worker = workers.StorageToBQImporter(
      {
        'source_uris': [
          'gs://bucket/data.csv',
          'gs://bucket/subdir/data.csv',
        ]
      },
      1,
      1)
    source_uris = worker._get_source_uris()
    self.assertEqual(len(source_uris), 2)
    self.assertEqual(source_uris[0], 'gs://bucket/data.csv')
    self.assertEqual(source_uris[1], 'gs://bucket/subdir/data.csv')

  def test_get_source_uris_with_pattern(self):
    worker = workers.StorageToBQImporter(
      {
        'source_uris': [
          'gs://bucket/subdir/*.csv',
        ]
      },
      1,
      1)
    source_uris = worker._get_source_uris()
    self.assertEqual(len(source_uris), 2)
    self.assertEqual(source_uris[0], 'gs://bucket/subdir/input.csv')
    self.assertEqual(source_uris[1], 'gs://bucket/subdir/data.csv')


class TestBQToMeasurementProtocolMixin(object):

  def _use_query_results(self, response_json):
    # NB: be sure to remove the jobReference from the api response used to
    #     create the Table instance.
    response_json_copy = response_json.copy()
    del response_json_copy['jobReference']
    mock_dataset = mock.Mock()
    mock_dataset._client = self._client
    mock_table = Table('mock_table', mock_dataset)
    self._client._connection.api_request.return_value = response_json
    self._client.dataset.return_value = mock_dataset
    mock_dataset.table.return_value = mock_table


class TestBQToMeasurementProtocolProcessor(TestBQToMeasurementProtocolMixin, unittest.TestCase):

  def setUp(self):
    super(TestBQToMeasurementProtocolProcessor, self).setUp()

    self.testbed = testbed.Testbed()
    self.testbed.activate()
    # Activate which service we want to stub
    self.testbed.init_memcache_stub()
    self.testbed.init_app_identity_stub()

    self._client = mock.Mock()
    patcher_get_client = mock.patch.object(
        workers.BQToMeasurementProtocolProcessor,
        '_get_client',
        return_value=self._client)
    self.addCleanup(patcher_get_client.stop)
    patcher_get_client.start()

    patcher_requests_post = mock.patch('requests.post')
    self.addCleanup(patcher_requests_post.stop)
    self._patched_post = patcher_requests_post.start()

  def tearDown(self):
    super(TestBQToMeasurementProtocolProcessor, self).tearDown()
    self.testbed.deactivate()

  @mock.patch('time.sleep')
  @mock.patch('core.logging.logger')
  def test_success_with_one_post_request(self, patched_logger,
      patched_time_sleep):
    # Bypass the time.sleep wait
    patched_time_sleep.return_value = 1
    # NB: patching the StackDriver logger is needed because there is no
    #     testbed service available for now
    patched_logger.log_struct.__name__ = 'foo'
    patched_logger.log_struct.return_value = "patched_log_struct"
    self._worker = workers.BQToMeasurementProtocolProcessor(
        {
            'bq_project_id': 'BQID',
            'bq_dataset_id': 'DTID',
            'bq_table_id': 'table_id',
            'bq_page_token': None,
            'bq_batch_size': 10,
            'mp_batch_size': 20,
        },
        1,
        1)
    self._use_query_results({
        'tableReference': {
            'tableId': 'mock_table',
        },
        'jobReference': {
            'jobId': 'two-rows-query',
        },
        'rows': [
            {
                'f': [
                    {'v': 'UA-12345-1'},
                    {'v': '35009a79-1a05-49d7-b876-2b884d0f825b'},
                    {'v': 'event'},
                    {'v': 1},
                    {'v': 'category'},
                    {'v': 'action'},
                    {'v': 'label'},
                    {'v': 0.9},
                    {'v': 'User Agent / 1.0'},
                ]
            },
            {
                'f': [
                    {'v': 'UA-12345-1'},
                    {'v': '35009a79-1a05-49d7-b876-2b884d0f825b'},
                    {'v': 'event'},
                    {'v': 1},
                    {'v': 'category'},
                    {'v': 'action'},
                    {'v': 'label'},
                    {'v': 0.8},
                    {'v': 'User Agent / 1.0'},
                ]
            }
        ],
        'schema': {
            'fields': [
                {'name': 'tid', 'type': 'STRING'},
                {'name': 'cid', 'type': 'STRING'},
                {'name': 't', 'type': 'STRING'},
                {'name': 'ni', 'type': 'FLOAT'},
                {'name': 'ec', 'type': 'STRING'},
                {'name': 'ea', 'type': 'STRING'},
                {'name': 'el', 'type': 'STRING'},
                {'name': 'ev', 'type': 'FLOAT'},
                {'name': 'ua', 'type': 'STRING'},
            ]
        }
    })

    mock_response = mock.Mock()
    mock_response.status_code = 200
    self._patched_post.return_value = mock_response

    # Consume the generator to avoid garbage collection.
    generator = self._worker.execute()
    workers_to_enqueue = list(generator)
    self._patched_post.assert_called_once()
    self.assertEqual(
        self._patched_post.call_args[0][0],
        'https://www.google-analytics.com/batch')
    self.assertEqual(
        self._patched_post.call_args[1],
        {
            'headers': {'user-agent': 'CRMint / 0.1'},
            'data':
"""ni=1.0&el=label&cid=35009a79-1a05-49d7-b876-2b884d0f825b&ea=action&ec=category&t=event&v=1&tid=UA-12345-1&ev=0.9&ua=User+Agent+%2F+1.0
ni=1.0&el=label&cid=35009a79-1a05-49d7-b876-2b884d0f825b&ea=action&ec=category&t=event&v=1&tid=UA-12345-1&ev=0.8&ua=User+Agent+%2F+1.0""",
        })

  @mock.patch('core.logging.logger')
  @mock.patch('time.sleep')
  def test_log_exception_if_http_fails(self, patched_time_sleep, patched_logger):
    # Bypass the time.sleep wait
    patched_time_sleep.return_value = 1
    # NB: patching the StackDriver logger is needed because there is no
    #     testbed service available for now
    patched_logger.log_struct.__name__ = 'foo'
    patched_logger.log_struct.return_value = "patched_log_struct"
    self._worker = workers.BQToMeasurementProtocolProcessor(
        {
            'bq_project_id': 'BQID',
            'bq_dataset_id': 'DTID',
            'bq_table_id': 'table_id',
            'bq_page_token': None,
            'bq_batch_size': 10,
            'mp_batch_size': 20,
        },
        1,
        1)
    self._use_query_results({
        'tableReference': {
            'tableId': 'mock_table',
        },
        'jobReference': {
            'jobId': 'one-row-query',
        },
        'rows': [
            {
                'f': [
                    {'v': 'UA-12345-1'},
                    {'v': '35009a79-1a05-49d7-b876-2b884d0f825b'},
                    {'v': 'event'},
                    {'v': 1},
                    {'v': 'category'},
                    {'v': 'action'},
                    {'v': 'label'},
                    {'v': 'value'},
                    {'v': 'User Agent / 1.0'},
                ]
            }
        ],
        'schema': {
            'fields': [
                {'name': 'tid', 'type': 'STRING'},
                {'name': 'cid', 'type': 'STRING'},
                {'name': 't', 'type': 'STRING'},
                {'name': 'ni', 'type': 'FLOAT'},
                {'name': 'ec', 'type': 'STRING'},
                {'name': 'ea', 'type': 'STRING'},
                {'name': 'el', 'type': 'STRING'},
                {'name': 'ev', 'type': 'STRING'},
                {'name': 'ua', 'type': 'STRING'},
            ]
        }
    })

    mock_response = mock.Mock()
    mock_response.status_code = 500
    self._patched_post.return_value = mock_response

    # Consume the generator to avoid garbage collection.
    generator = self._worker.execute()
    workers_to_enqueue = list(generator)
    # Called 2 times because of 1 retry.
    self.assertEqual(self._patched_post.call_count, 2)
    # When retry stops it should log the message as an error.
    patched_logger.log_error.called_once()


class TestBQToMeasurementProtocol(TestBQToMeasurementProtocolMixin, unittest.TestCase):

  def setUp(self):
    super(TestBQToMeasurementProtocol, self).setUp()

    self.testbed = testbed.Testbed()
    self.testbed.activate()
    # Activate which service we want to stub
    self.testbed.init_memcache_stub()
    self.testbed.init_app_identity_stub()

    self._client = mock.Mock()
    patcher_get_client = mock.patch.object(
        workers.BQToMeasurementProtocol,
        '_get_client',
        return_value=self._client)
    self.addCleanup(patcher_get_client.stop)
    patcher_get_client.start()

  def tearDown(self):
    super(TestBQToMeasurementProtocol, self).tearDown()
    self.testbed.deactivate()

  @mock.patch('time.sleep')
  @mock.patch('core.logging.logger')
  def test_success_with_spawning_new_worker(self, patched_logger,
      patched_time_sleep):
    # Bypass the time.sleep wait
    patched_time_sleep.return_value = 1
    # NB: patching the StackDriver logger is needed because there is no
    #     testbed service available for now
    patched_logger.log_struct.__name__ = 'foo'
    patched_logger.log_struct.return_value = "patched_log_struct"
    self._worker = workers.BQToMeasurementProtocol(
        {
            'bq_project_id': 'BQID',
            'bq_dataset_id': 'DTID',
            'bq_table_id': 'table_id',
            'bq_page_token': None,
            'mp_batch_size': 20,
        },
        1,
        1)
    self._worker.MAX_ENQUEUED_JOBS = 1
    api_response = {
        'tableReference': {
            'tableId': 'mock_table',
        },
        'jobReference': {
            'jobId': 'one-row-query',
        },
        'pageToken': 'abc',
        'rows': [
            {
                'f': [
                    {'v': 'UA-12345-1'},
                    {'v': '35009a79-1a05-49d7-b876-2b884d0f825b'},
                    {'v': 'event'},
                    {'v': 1},
                    {'v': 'category'},
                    {'v': 'action'},
                    {'v': 'label'},
                    {'v': 0.9},
                    {'v': 'User Agent / 1.0'},
                ]
            },
            {
                'f': [
                    {'v': 'UA-12345-1'},
                    {'v': '35009a79-1a05-49d7-b876-2b884d0f825b'},
                    {'v': 'event'},
                    {'v': 1},
                    {'v': 'category'},
                    {'v': 'action'},
                    {'v': 'label'},
                    {'v': 0.8},
                    {'v': 'User Agent / 1.0'},
                ]
            },
        ],
        'schema': {
            'fields': [
                {'name': 'tid', 'type': 'STRING'},
                {'name': 'cid', 'type': 'STRING'},
                {'name': 't', 'type': 'STRING'},
                {'name': 'ni', 'type': 'FLOAT'},
                {'name': 'ec', 'type': 'STRING'},
                {'name': 'ea', 'type': 'STRING'},
                {'name': 'el', 'type': 'STRING'},
                {'name': 'ev', 'type': 'FLOAT'},
                {'name': 'ua', 'type': 'STRING'},
            ]
        }
    }
    self._use_query_results(api_response)

    patcher_worker_enqueue = mock.patch.object(workers.BQToMeasurementProtocol, '_enqueue')
    self.addCleanup(patcher_worker_enqueue.stop)
    patched_enqueue = patcher_worker_enqueue.start()

    def _remove_next_page_token(worker_name, *args, **kwargs):
      if worker_name == 'BQToMeasurementProtocol':
        del api_response['pageToken']
        self._use_query_results(api_response)
    patched_enqueue.side_effect = _remove_next_page_token

    # Consume the generator to avoid garbage collection.
    generator = self._worker.execute()
    workers_to_enqueue = list(generator)
    self.assertEqual(patched_enqueue.call_count, 2)
    self.assertEqual(patched_enqueue.call_args_list[0][0][0], 'BQToMeasurementProtocolProcessor')
    self.assertEqual(patched_enqueue.call_args_list[0][0][1]['bq_page_token'], None)
    self.assertEqual(patched_enqueue.call_args_list[1][0][0], 'BQToMeasurementProtocol')
    self.assertEqual(patched_enqueue.call_args_list[1][0][1]['bq_page_token'], 'abc')
