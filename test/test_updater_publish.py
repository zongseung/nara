import json
from pathlib import Path
import tempfile
import unittest
from src.updater.collector import refresh, collect, CollectionPolicy
from src.updater.publisher import publish
from src.updater.reader import open_published_snapshot
from src.updater.store import Store, StoreError
from core.catalog_sources import get_profile
from test.updater_fixtures import FixtureSource, row

POLICY = CollectionPolicy(request_interval=0)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))

    def refresh(self, rows, tick=0):
        return refresh(self.store, FixtureSource(rows, tick), policy=POLICY, environment='fixture')

    def test_price_reversion_versions_and_field_changes(self):
        for tick, price in enumerate(('100', '100.0', '120', '100')):
            manifest = self.refresh([row(price=price)], tick)
        with open_published_snapshot(manifest, root=self.store.root) as reader:
            current = list(reader.current())[0]
            self.assertEqual(current['contract_price_decimal'], '100')
            self.assertEqual(len(list(reader.history(current['listing_key']))), 3)
            prices = list(reader.history(current['listing_key'], price_only=True))
            self.assertEqual([p['event_kind'] for p in prices], ['initial','price_changed','price_changed'])
            self.assertIsNotNone(current['price_changed_at'])
            self.assertEqual(reader.connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        manifest = self.refresh([row(spec='12mm')], 4)
        with open_published_snapshot(manifest, root=self.store.root) as reader:
            self.assertEqual(len(list(reader.history(current['listing_key'], price_only=True))), 3)
            self.assertEqual(list(reader.current())[0]['price_changed_at'], current['price_changed_at'])

    def test_missing_preserves_values_presence_and_contract_identity(self):
        self.refresh([row(), row('B')])
        missing = row()
        del missing['mnftrEtpsNm']
        manifest = self.refresh([missing], 1)
        with open_published_snapshot(manifest, root=self.store.root) as reader:
            items = list(reader.current())
            self.assertEqual(items[0]['manufacturer_name'], 'Maker')
            self.assertEqual(items[0]['information_groups']['parties']['status'], 'not_checked')
            self.assertEqual(items[1]['presence_status'], 'not_observed')
        manifest = self.refresh([row(), row('B')], 2)
        with open_published_snapshot(manifest, root=self.store.root) as reader:
            self.assertEqual(reader.connection.execute("SELECT count(*) FROM record_events WHERE event_kind='reappeared'").fetchone()[0], 1)
            self.assertEqual(reader.connection.execute('SELECT count(*) FROM item_versions').fetchone()[0], 2)

    def test_cancel_and_stale_owner_leave_pointer(self):
        previous = self.refresh([row()])
        with self.store.writer(get_profile('shopping-all'), environment='fixture') as lease:
            collect(self.store, lease, FixtureSource([row(price='200')]), policy=POLICY)
            with self.store.control() as conn, conn:
                conn.execute('UPDATE runs SET cancelled=1 WHERE run_id=?', (lease.run_id,))
            with self.assertRaises(StoreError):
                publish(self.store, lease)
        from src.updater.reader import catalog_manifest
        self.assertEqual(catalog_manifest(self.store), previous)

    def test_ack_retry_after_later_publication_uses_event(self):
        with self.store.writer(get_profile('shopping-all'), environment='fixture') as lease:
            collect(self.store, lease, FixtureSource([row()]), policy=POLICY)
            first = publish(self.store, lease)
        second = self.refresh([row(price='200')], 1)
        self.store.path(first.snapshot_path).unlink()
        self.assertEqual(publish(self.store, lease), first)
        with open_published_snapshot(second, root=self.store.root) as reader:
            self.assertEqual(reader.connection.execute('SELECT count(*) FROM price_history').fetchone()[0], 2)
