from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest
from core.catalog_partitions import PriceDomain
from core.catalog_sources import get_profile, SourceError
from test.updater_fixtures import FixtureSource, row
from src.updater.collector import collect, refresh, CollectionPolicy, prepare_collection, collect_partition
from src.updater.reader import catalog_manifest
from src.updater.store import Store, StoreError


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.policy = CollectionPolicy(PriceDomain(Decimal('0'), Decimal('200'), Decimal('0.5'), 2), page_size=2, safe_pages=1, request_interval=0)

    def test_partition_price_move_and_fresh_run(self):
        rows = [row(str(i), str(i * 30)) for i in range(6)]
        first = refresh(self.store, FixtureSource(rows), policy=self.policy, environment='fixture')
        rows[0]['ctrtUprc'] = '180.5'
        second = refresh(self.store, FixtureSource(rows, 1), policy=self.policy, environment='fixture')
        self.assertNotEqual(first.publication_id, second.publication_id)
        with self.store.control() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM runs WHERE status='published'").fetchone()[0], 2)

    def test_single_price_overflow_and_missing_domain_block(self):
        for policy in (self.policy, replace(self.policy, domain=None)):
            with self.assertRaises((StoreError, SourceError)):
                refresh(self.store, FixtureSource([row(str(i)) for i in range(4)]), policy=policy, environment='fixture')
        with self.assertRaises(StoreError):
            catalog_manifest(self.store)

    def test_verified_pagination_collects_same_price_overflow(self):
        source = FixtureSource([row(str(i)) for i in range(13)])
        policy = CollectionPolicy(page_size=2, safe_pages=1, request_interval=0,
                                  allow_deep_pages=True)
        manifest = refresh(self.store, source, policy=policy, environment='fixture')
        from src.updater.reader import open_published_snapshot
        with open_published_snapshot(manifest, root=self.store.root) as reader:
            self.assertEqual(len(list(reader.current())), 13)
        self.assertTrue(any(query.page == 7 for query in source.calls))

    def test_verified_pagination_rejects_repeated_deep_page(self):
        class RepeatingSource(FixtureSource):
            def fetch(self, query):
                return super().fetch(replace(query, page=1))
        policy = CollectionPolicy(page_size=2, safe_pages=1, request_interval=0,
                                  allow_deep_pages=True)
        with self.assertRaises(StoreError):
            refresh(self.store, RepeatingSource([row(str(i)) for i in range(13)]),
                    policy=policy, environment='fixture')
        with self.assertRaises(StoreError):
            catalog_manifest(self.store)

    def test_chunk_resume_preserves_committed_pages(self):
        source = FixtureSource([row(str(i)) for i in range(13)])
        policy = CollectionPolicy(page_size=2, safe_pages=1, request_interval=0,
                                  allow_deep_pages=True)
        with self.store.writer(get_profile('shopping-all'), environment='fixture') as lease:
            jobs = prepare_collection(self.store, lease, source, policy=policy)
            collect_partition(self.store, lease, source, job=jobs[0])
            source.calls.clear()
            collect(self.store, lease, source, policy=policy)
            self.assertEqual(sum(query.page == 1 for query in source.calls), 2)
            with self.store.stage(lease) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM observations').fetchone()[0], 13)

    def test_large_page_evidence_is_compressed_losslessly(self):
        import gzip
        import json
        wire = json.dumps({'rsltList': [row()], 'metadata': 'unchanged ' * 10000})
        class LargeSource(FixtureSource):
            def fetch(self, query):
                return replace(super().fetch(query), raw_text=wire)
        with self.store.writer(get_profile('shopping-all'), environment='fixture') as lease:
            collect(self.store, lease, LargeSource([row()]),
                    policy=CollectionPolicy(request_interval=0))
            with self.store.stage(lease) as conn:
                stored = conn.execute('SELECT raw_json FROM pages').fetchone()[0]
            self.assertIsInstance(stored, bytes)
            self.assertEqual(gzip.decompress(stored).decode(), wire)
            self.assertLess(len(stored), len(wire.encode()))

    def test_outside_domain_and_null_price_block(self):
        for rows in ([row(price='200.5')], [row(price=None)]):
            with self.assertRaises((StoreError, SourceError)):
                refresh(self.store, FixtureSource(rows), policy=self.policy, environment='fixture')

    def test_page_checkpoint_resume_same_run(self):
        policy = CollectionPolicy(page_size=1, safe_pages=3, request_interval=0)
        source = FixtureSource([row('A'),row('B')])
        with self.store.writer(get_profile('shopping-all'), environment='fixture') as lease:
            collect(self.store, lease, source, policy=policy)
            first_calls = len(source.calls)
            collect(self.store, lease, source, policy=policy)
            self.assertEqual(len(source.calls) - first_calls, 2)
            with self.store.stage(lease) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM observations').fetchone()[0], 2)

    def test_duplicate_page_fails(self):
        policy = CollectionPolicy(page_size=1, safe_pages=3, request_interval=0)
        with self.assertRaises(sqlite3.IntegrityError):
            refresh(self.store, FixtureSource([row(),row()]), policy=policy, environment='fixture')

    def test_sparse_domain_empty_partitions_and_zero_rows(self):
        policy = CollectionPolicy(PriceDomain(Decimal('0'),Decimal('100'),Decimal('1'),1), page_size=1, safe_pages=1, request_interval=0)
        manifest = refresh(self.store, FixtureSource([row('A','0'),row('B','1'),row('C','100')]), policy=policy, environment='fixture')
        self.assertTrue(manifest.coverage_complete)
        empty = refresh(self.store, FixtureSource([]), policy=policy, environment='fixture')
        from src.updater.reader import open_published_snapshot
        with open_published_snapshot(empty, root=self.store.root) as reader:
            self.assertTrue(all(r['presence_status']=='not_observed' for r in reader.current()))
