import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timezone
from src.updater.collector import refresh, CollectionPolicy
from src.updater.store import Store
from test.updater_fixtures import FixtureSource, row
from src.matching.catalog import open_catalog
from src.matching.index import build_index, open_index
from src.matching.retrieval import retrieve, RetrievalBudget
from core.matching.contracts import InputItem, Purpose
from core.matching.normalization import normalize_dimensions
from core.matching.policy import load_policy
from test.test_matching_policy import policy_data

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


class KnownFixtureCatalog:
    """Synthetic suitability facts only; never claims producer supplied them."""

    def __init__(self, catalog):
        self.catalog = catalog

    @property
    def manifest(self):
        return self.catalog.manifest

    def require_open(self):
        self.catalog.require_open()

    def candidates(self):
        for candidate in self.catalog.candidates():
            yield replace(candidate, item_kind="main", latest_contract=True)


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "source")
        self.item = InputItem(
            "request",
            Purpose.FUNCTIONAL_SUBSTITUTE,
            "123",
            "name",
            "20x30mm",
            attributes=(("dimensions", normalize_dimensions("20x30mm")),),
            item_kind="main",
        )
        self.policy = load_policy(policy_data(category_ids=["123"]))

    def run_query(self, rows, *, known=True, **kwargs):
        manifest = refresh(
            self.store,
            FixtureSource(rows),
            policy=CollectionPolicy(request_interval=0),
            environment="fixture",
        )
        with open_catalog(manifest, root=self.store.root) as catalog:
            descriptor = build_index(
                KnownFixtureCatalog(catalog) if known else catalog, self.root / "index"
            )
            with open_index(descriptor, catalog=catalog) as index:
                return retrieve(self.item, self.policy, index, now=NOW, **kwargs)

    def test_501st_answer_after_500_hard_failures(self):
        rows = [row(f"{i:04d}", spec="10x20mm") for i in range(500)] + [
            row("0500", spec="20x30mm")
        ]
        result = self.run_query(rows)
        self.assertEqual(result.scanned_count, 501)
        self.assertEqual(
            [c.listing_key for c in result.candidates],
            ["shopping:listing:ctrtItemMngNo:0500"],
        )
        self.assertTrue(
            result.scope_scan_complete and result.decision_evaluation_complete
        )

    def test_unknown_not_dropped(self):
        result = self.run_query([row(spec="20x30mm")], known=False)
        self.assertEqual(result.unknown_count, 1)
        self.assertEqual(len(result.candidates), 1)

    def test_eligible_overflow_keeps_scope_complete_but_detail_incomplete(self):
        result = self.run_query([row(f"{i:04d}", spec="20x30mm") for i in range(501)])
        self.assertTrue(result.scope_scan_complete)
        self.assertFalse(result.decision_evaluation_complete)
        self.assertEqual(len(result.candidates), 500)

    def test_scan_budget_is_incomplete(self):
        result = self.run_query(
            [row("A", spec="20x30mm"), row("B", spec="20x30mm")],
            budget=RetrievalBudget(max_scan=1),
        )
        self.assertFalse(result.scope_scan_complete)
        self.assertEqual(result.termination_reason, "scan_budget")

    def test_time_budget_is_incomplete(self):
        ticks = iter([0, 0, 21])
        result = self.run_query(
            [row("A", spec="20x30mm"), row("B", spec="20x30mm")],
            clock=lambda: next(ticks),
        )
        self.assertFalse(result.scope_scan_complete)
        self.assertEqual(result.termination_reason, "time_budget")

    def test_empty_declared_scope_complete(self):
        result = self.run_query([])
        self.assertEqual((result.total_count, result.scanned_count), (0, 0))
        self.assertTrue(
            result.scope_scan_complete and result.decision_evaluation_complete
        )

    def test_unknown_overflow_cannot_be_auto_no_match(self):
        result = self.run_query(
            [row(str(i), spec="20x30") for i in range(3)],
            known=False,
            budget=RetrievalBudget(max_detail=2),
        )
        self.assertEqual(result.unknown_count, 3)
        self.assertFalse(result.decision_evaluation_complete)

    def test_null_category_survives_blocking_as_unknown(self):
        raw = row(spec="20x30mm")
        raw["itemClsfNo"] = None
        result = self.run_query([raw])
        self.assertEqual(
            (result.total_count, result.unknown_count, len(result.candidates)),
            (1, 1, 1),
        )

    def test_exact_price_scope_does_not_expand(self):
        from decimal import Decimal
        from core.matching.contracts import PriceBasis

        self.item = replace(
            self.item,
            purpose=Purpose.SAME_PRICE_COMPETITOR,
            price=PriceBasis(Decimal("100")),
        )
        self.policy = load_policy(
            policy_data(category_ids=["123"], purpose="same_price_competitor")
        )
        result = self.run_query(
            [row("near", "101", spec="20x30mm"), row("exact", "100", spec="20x30mm")]
        )
        self.assertEqual(result.total_count, 1)
        self.assertEqual(
            result.candidates[0].listing_key, "shopping:listing:ctrtItemMngNo:exact"
        )
        self.assertEqual(result.unknown_count, 1)

    def test_sql_count_is_inside_time_budget(self):
        import sqlite3
        from src.matching.retrieval import _scope

        manifest = refresh(
            self.store,
            FixtureSource([row(f"{i:04d}", spec="20x30mm") for i in range(501)]),
            policy=CollectionPolicy(request_interval=0),
            environment="fixture",
        )
        with open_catalog(manifest, root=self.store.root) as catalog:
            descriptor = build_index(KnownFixtureCatalog(catalog), self.root / "index")
            with open_index(descriptor, catalog=catalog) as index:
                ticks = iter([0] + [21] * 10000)
                result = retrieve(
                    self.item, self.policy, index, now=NOW, clock=lambda: next(ticks)
                )
                self.assertEqual(result.termination_reason, "time_budget")
                self.assertEqual(result.scanned_count, 0)
                self.assertIsNone(result.total_count)
                self.assertFalse(result.scope_scan_complete)
