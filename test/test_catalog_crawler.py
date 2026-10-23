import tempfile
import unittest
from collections import namedtuple
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src.catalog_client import FixtureSource
from src.catalog_planner import make_plan
from core.catalog_sources import SearchQuery, SourceError, get_profile
from src.catalog_store import CatalogStore
from src.catalog_verify import reconcile
from src.crawl_all import collect
from test.test_catalog_planner import FIXTURE


class CrawlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "catalog.db"
        self.source = FixtureSource(FIXTURE, get_profile("shopping-all"))

    def test_resume_preserves_checkpoint_and_matches_uninterrupted(self):
        # Given a committed first page and an interrupted run.
        with CatalogStore(self.db) as store:
            run = store.create_run(self.source.profile, make_plan(self.source))
            store.save_page(run, 0, 1, self.source.fetch(SearchQuery()))
            store.set_state(run, "interrupted")
            # When resumed through the real scheduler.
            collect(store, self.source, run)
            result = reconcile(store, run)
            # Then exactly three pages and 205 unique contracts reconcile.
            self.assertEqual(result.validation, "reconciled_observation")
            self.assertEqual(
                (result.received, result.unique_listings, result.unique_goods),
                (205, 205, 103),
            )
            self.assertEqual(
                store.db.execute("SELECT count(*) FROM pages").fetchone()[0], 3
            )

    def test_global_limit_stays_limited_on_resume(self):
        # Given a page budget smaller than the fixed plan.
        with CatalogStore(self.db) as store:
            run = store.create_run(
                self.source.profile, make_plan(self.source), max_pages=2
            )
            # When collecting and resuming.
            collect(store, self.source, run)
            collect(store, self.source, run)
            # Then the global cap never grows and cannot publish.
            result = reconcile(store, run)
            self.assertEqual(
                (result.state, result.validation, result.received),
                ("limited", "incomplete", 200),
            )
            self.assertEqual(
                store.db.execute("SELECT count(*) FROM published_runs").fetchone()[0], 0
            )

    def test_failed_page_is_retryable_without_success_checkpoint(self):
        # Given a transient failed second page and one worker.
        with CatalogStore(self.db) as store:
            run = store.create_run(self.source.profile, make_plan(self.source))
            original = self.source.fetch

            def fail(query):
                if query.page == 2:
                    raise SourceError("timeout", attempts=3)
                return original(query)

            self.source.fetch = fail
            # When the transport exhausts its own attempts.
            with self.assertRaises(SourceError):
                collect(store, self.source, run, workers=1, pending=1)
            self.assertEqual(store.run(run)["state"], "failed")
            self.assertEqual(
                store.db.execute("SELECT count(*) FROM pages").fetchone()[0], 1
            )
            self.source.fetch = original
            collect(store, self.source, run, workers=1, pending=1)
            # Then resume fetches the missing page and reconciles.
            self.assertEqual(reconcile(store, run).validation, "reconciled_observation")

    def test_profile_and_plan_mutation_are_refused(self):
        # Given stored immutable identities; when changed; then resume is rejected before fetching.
        with CatalogStore(self.db) as store:
            run = store.create_run(self.source.profile, make_plan(self.source))
            self.source.profile = replace(
                self.source.profile, purchase_method="본010001"
            )
            with self.assertRaises(SourceError):
                collect(store, self.source, run)
            store.db.execute("UPDATE runs SET plan_json='{}' WHERE run_id=?", (run,))
            with self.assertRaises(SourceError):
                store.plan(run)

    def test_insufficient_disk_records_failure(self):
        # Given too little disk; when preflight runs; then no page is committed.
        with CatalogStore(self.db) as store:
            run = store.create_run(self.source.profile, make_plan(self.source))
            usage = namedtuple("usage", "total used free")(100, 99, 1)
            with (
                patch("shutil.disk_usage", return_value=usage),
                self.assertRaises(SourceError),
            ):
                collect(store, self.source, run)
            self.assertEqual(store.run(run)["state"], "failed")
            self.assertEqual(
                store.db.execute("SELECT count(*) FROM pages").fetchone()[0], 0
            )

    def test_keyboard_interrupt_records_checkpoint_and_resumes(self):
        # Given a source interrupted before its second page.
        with CatalogStore(self.db) as store:
            run = store.create_run(self.source.profile, make_plan(self.source))
            original = self.source.fetch

            def interrupted(query):
                if query.page == 2:
                    raise KeyboardInterrupt
                return original(query)

            self.source.fetch = interrupted
            # When collection is interrupted; then the successful page remains resumable.
            with self.assertRaises(KeyboardInterrupt):
                collect(store, self.source, run, workers=1, pending=1)
            self.assertEqual(store.run(run)["state"], "interrupted")
            self.source.fetch = original
            collect(store, self.source, run)
            self.assertEqual(reconcile(store, run).validation, "reconciled_observation")

    def test_pending_limit_rejects_unbounded_submission(self):
        # Given a fixed run; when requesting more than four pending jobs; then fail before work.
        with CatalogStore(self.db) as store:
            run = store.create_run(self.source.profile, make_plan(self.source))
            with self.assertRaises(SourceError):
                collect(store, self.source, run, pending=5)
            self.assertEqual(
                store.db.execute("SELECT count(*) FROM pages").fetchone()[0], 0
            )
