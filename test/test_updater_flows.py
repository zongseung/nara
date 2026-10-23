import unittest
from prefect.context import TaskRunContext
from src.updater.flows import refresh_shop_prices,collect_pages,publish_snapshot


class FlowPolicyTests(unittest.TestCase):
    def test_side_effects_are_not_cached_or_implicitly_retried(self):
        self.assertEqual(refresh_shop_prices.retries,0)
        self.assertFalse(refresh_shop_prices.persist_result)
        self.assertEqual(collect_pages.name,'collect-partition')
        self.assertEqual(collect_pages.retries,1)
        self.assertIsNotNone(collect_pages.cache_policy)
        for price in ('100','120'):
            self.assertIsNone(collect_pages.cache_policy.compute_key(TaskRunContext.model_construct(),{'price':price},{}))
        self.assertFalse(collect_pages.persist_result)
        self.assertEqual(publish_snapshot.retries,0)
        self.assertIsNotNone(publish_snapshot.cache_policy)
        self.assertIsNone(publish_snapshot.cache_policy.compute_key(TaskRunContext.model_construct(),{'run_id':'another-run'},{}))
