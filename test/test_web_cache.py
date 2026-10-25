import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError

from src.web.api import create_app
from src.web.cache import MAX_ENTRY_BYTES, TTL_SECONDS, ItemCache
from src.web.models import JobRequest, ReviewItem
from src.web.store import JobStore
from test.test_web_jobs import product


class WebCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = JobStore(self.root)
        self.job = self.store.create(JobRequest(company='기준회사'))
        self.store.save_item(self.job.id, ReviewItem(index=0, company='기준회사',
            reference=product('10000000', '기준회사', 100),
            candidates=[product('20000000', '경쟁사', 110)]))
        self.store.update(self.job.id, lambda job: job.model_copy(update={'status': 'review'}))
        env = patch.dict('os.environ', {'NARA_WEB_REDIS_URL': 'redis://test-cache:6379/0'})
        env.start()
        self.addCleanup(env.stop)
        redis = patch('src.web.cache.Redis.from_url')
        self.redis = redis.start().return_value
        self.addCleanup(redis.stop)
        self.redis.get.return_value = None
        self.client = TestClient(create_app(self.root, submit=lambda j, o: None))

    def test_repeated_item_read_uses_cached_body_without_reading_items(self):
        first = self.client.get(f'/jobs/{self.job.id}/items')
        self.assertEqual(first.headers['x-cache'], 'MISS')
        self.redis.get.return_value = first.content
        with patch.object(JobStore, 'items', side_effect=AssertionError('cache hit reread files')):
            second = self.client.get(f'/jobs/{self.job.id}/items')
        self.assertEqual(second.content, first.content)
        self.assertEqual(second.headers['x-cache'], 'HIT')
        self.assertEqual(second.headers['cache-control'], 'no-store')
        self.redis.set.assert_called_once_with(self.redis.get.call_args.args[0], first.content, ex=TTL_SECONDS)
        self.assertNotIn('_raw', first.json()[0]['reference'])

    def test_selection_change_uses_new_key_and_new_review_state(self):
        self.client.get(f'/jobs/{self.job.id}/items')
        old_key = self.redis.get.call_args.args[0]
        self.client.post(f'/jobs/{self.job.id}/items/0/selection', json={'revision': 0, 'excluded': [0]})
        current = self.client.get(f'/jobs/{self.job.id}/items')
        self.assertNotEqual(self.redis.get.call_args.args[0], old_key)
        self.assertEqual(current.json()[0]['excluded'], [0])

    def test_worker_progress_changes_key_without_review_revision_change(self):
        self.client.get(f'/jobs/{self.job.id}/items')
        old_key = self.redis.get.call_args.args[0]
        self.store.update(self.job.id, lambda job: job.model_copy(update={'completed': 1}))
        self.client.get(f'/jobs/{self.job.id}/items')
        self.assertNotEqual(self.redis.get.call_args.args[0], old_key)

    def test_cache_outage_reads_files_and_backs_off(self):
        self.redis.get.side_effect = ConnectionError('unavailable')
        with self.assertLogs('src.web.cache', level='WARNING'):
            first = self.client.get(f'/jobs/{self.job.id}/items')
        second = self.client.get(f'/jobs/{self.job.id}/items')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.json()[0]['reference']['가격'], '100')
        self.redis.get.assert_called_once()
        self.redis.set.assert_not_called()

    def test_large_entries_are_not_cached(self):
        cache = ItemCache()
        cache.put('test-large', b'x' * (MAX_ENTRY_BYTES+1))
        self.redis.set.assert_not_called()

    def test_write_failure_does_not_break_the_response(self):
        self.redis.set.side_effect = ConnectionError('unavailable')
        with self.assertLogs('src.web.cache', level='WARNING'):
            response = self.client.get(f'/jobs/{self.job.id}/items')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]['reference']['가격'], '100')

    def test_job_status_is_never_cached(self):
        self.assertEqual(self.client.get(f'/jobs/{self.job.id}').json()['status'], 'review')
        self.redis.get.assert_not_called()
