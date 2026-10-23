import tempfile
import unittest
from pathlib import Path
from core.catalog_sources import get_profile
from src.updater.store import Store, StoreError


class StoreTests(unittest.TestCase):
    def test_writer_fence_and_exclusion(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root))
            with store.writer(get_profile('shopping-all')) as lease:
                store.assert_owner(lease)
                with self.assertRaises(StoreError):
                    with store.writer(lease.profile):
                        self.fail('second writer acquired')
            with self.assertRaises(StoreError):
                store.assert_owner(lease)
