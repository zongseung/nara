import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from src.catalog_client import FixtureSource
from src.catalog_planner import make_plan
from core.catalog_sources import SearchQuery, get_profile
from src.catalog_store import CatalogStore
from src.catalog_verify import reconcile
from src.crawl_all import collect
from test.test_catalog_planner import FIXTURE


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "db.sqlite"
        self.source = FixtureSource(FIXTURE, get_profile("shopping-all"))

    def test_clean_publication_is_observation_not_snapshot(self):
        # Given a complete fixed source; when reconciled; then the pointer and metrics agree.
        with CatalogStore(self.path) as store:
            run = store.create_run(self.source.profile, make_plan(self.source))
            collect(store, self.source, run)
            result = reconcile(store, run)
            self.assertEqual(
                store.db.execute("SELECT run_id FROM published_runs").fetchone()[0], run
            )
            self.assertFalse(result.source_snapshot_verified)
            self.assertEqual(
                (result.expected, result.received, result.inserted, result.quarantined),
                (205, 205, 205, 0),
            )

    def test_drift_cannot_replace_published_run(self):
        # Given a valid prior publication and a new observation with count drift.
        with CatalogStore(self.path) as store:
            old = store.create_run(self.source.profile, make_plan(self.source))
            collect(store, self.source, old)
            reconcile(store, old)
            run = store.create_run(self.source.profile, make_plan(self.source))
            collect(store, self.source, run)
            store.db.execute(
                "UPDATE runs SET end_count=end_count+1 WHERE run_id=?", (run,)
            )
            # When the new run is reconciled.
            result = reconcile(store, run)
            # Then drift blocks the pointer replacement.
            self.assertEqual(result.validation, "drift_detected")
            self.assertEqual(
                store.db.execute("SELECT run_id FROM published_runs").fetchone()[0], old
            )

    def test_duplicate_replacement_with_same_count_is_detected(self):
        # Given two identical pages with unchanged totals.
        with CatalogStore(self.path) as store:
            run = store.create_run(self.source.profile, make_plan(self.source))
            first = self.source.fetch(SearchQuery())
            store.save_page(run, 0, 1, first)
            store.save_page(run, 0, 2, first)
            store.save_page(run, 0, 3, self.source.fetch(SearchQuery(page=3)))
            store.set_state(run, "collected")
            store.db.execute(
                "UPDATE runs SET end_count=205,end_total_size=205 WHERE run_id=?",
                (run,),
            )
            # When reconciled; then raw duplicates remain evidence and publication fails.
            result = reconcile(store, run)
            self.assertEqual(result.received, 205)
            self.assertEqual(result.validation, "incomplete")
            self.assertIn("repeated_page_fingerprint", result.unresolved)
            self.assertEqual(result.quarantined, 100)

    def test_missing_short_and_empty_pages_are_not_complete(self):
        # Given a deliberately short middle page; when reconciled; then it cannot publish.
        with CatalogStore(self.path) as store:
            run = store.create_run(self.source.profile, make_plan(self.source))
            first = self.source.fetch(SearchQuery())
            store.save_page(run, 0, 1, replace(first, rows=first.rows[:1]))
            store.set_state(run, "collected")
            store.db.execute(
                "UPDATE runs SET end_count=205,end_total_size=205 WHERE run_id=?",
                (run,),
            )
            result = reconcile(store, run)
            self.assertIn("partition_0:missing_pages", result.unresolved)
            self.assertIn("partition_0:short_or_extra_page", result.unresolved)
            self.assertEqual(result.validation, "incomplete")
