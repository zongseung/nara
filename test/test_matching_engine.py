import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from src.matching.catalog import open_catalog
from core.matching.contracts import (
    NORMALIZER_VERSION,
    ContractError,
    DecisionStatus,
    InputItem,
    Purpose,
    ReleaseManifest,
)
from src.matching.engine import evaluate_candidate, run_match
from src.matching.index import build_index, open_index
from core.matching.normalization import normalize_dimensions
from core.matching.policy import load_policy
from src.matching.releases import DecisionConfig, OperatorContext, Release
from test.test_matching_policy import policy_data
from test.updater_fixtures import FixtureSource, row
from src.updater.collector import CollectionPolicy, refresh
from src.updater.store import Store

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
OPERATOR = OperatorContext(
    principal="fixture-operator", roles=frozenset({"reviewer", "evaluator"})
)


class EnrichedFixture:
    """Explicit synthetic facts for software QA; not evidence supplied by the real producer."""

    def __init__(self, catalog):
        self.catalog = catalog

    @property
    def manifest(self):
        return self.catalog.manifest

    def require_open(self):
        self.catalog.require_open()

    def candidates(self):
        for candidate in self.catalog.candidates():
            yield replace(
                candidate,
                item_kind="main",
                latest_contract=True,
                seller_id=candidate.listing_key,
            )


@contextmanager
def pipeline(root, rows=None, *, enriched=True, environment="fixture"):
    store = Store(root / "source")
    manifest = refresh(
        store,
        FixtureSource(rows if rows is not None else [row("A", spec="20x30mm")]),
        policy=CollectionPolicy(request_interval=0),
        environment=environment,
    )
    item = InputItem(
        "request",
        Purpose.FUNCTIONAL_SUBSTITUTE,
        "123",
        "품목",
        "20x30mm",
        attributes=(("dimensions", normalize_dimensions("20x30mm")),),
        item_kind="main",
    )
    policy = load_policy(policy_data(category_ids=["123"], state="active"))
    release = Release(
        manifest=ReleaseManifest(
            "fixture-release",
            item.purpose,
            item.category_id,
            policy.content_hash,
            "dataset",
            manifest.source_normalizer_version,
            NORMALIZER_VERSION,
            environment,
        ),
        state="candidate",
        threshold=0.9,
    )
    with open_catalog(manifest, root=store.root) as catalog:
        projected = EnrichedFixture(catalog) if enriched else catalog
        descriptor = build_index(projected, root / "index")
        with open_index(descriptor, catalog=projected) as index:
            yield item, policy, release, index


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_candidate_evaluation_and_shadow_are_separate(self):
        with pipeline(self.root) as (item, policy, release, index):
            result = evaluate_candidate(
                item, policy, index, release=release, now=NOW, operator=OPERATOR
            )
            ordinary = run_match(item, policy, index, release=release, now=NOW)
            self.assertTrue(result.would_auto_confirm)
            self.assertEqual((result.requested_count, result.shortage), (3, 2))
            self.assertEqual(result.decision.status, DecisionStatus.REVIEW_REQUIRED)
            self.assertFalse(ordinary.would_auto_confirm)
            with self.assertRaises(ContractError):
                run_match(
                    item, policy, index, release=release, now=NOW, mode="evaluation"
                )

    def test_hard_failure_cannot_be_overridden_by_high_score(self):
        with pipeline(self.root, [row("A", spec="99x99mm")]) as (
            item,
            policy,
            release,
            index,
        ):
            result = evaluate_candidate(
                item, policy, index, release=release, now=NOW, operator=OPERATOR
            )
            self.assertFalse(result.would_auto_confirm)
            self.assertEqual(result.decision.candidates, ())

    def test_real_source_unknown_facts_never_invented(self):
        with pipeline(self.root, enriched=False) as (item, policy, release, index):
            result = evaluate_candidate(
                item, policy, index, release=release, now=NOW, operator=OPERATOR
            )
            self.assertFalse(result.would_auto_confirm)
            self.assertGreater(result.retrieval.unknown_count, 0)

    def test_incomplete_retrieval_blocks_auto(self):
        with pipeline(
            self.root, [row("A", spec="20x30mm"), row("B", spec="20x30mm")]
        ) as (item, policy, release, index):
            release = release.model_copy(
                update={"config": DecisionConfig(max_detail=1)}
            )
            result = evaluate_candidate(
                item, policy, index, release=release, now=NOW, operator=OPERATOR
            )
            self.assertFalse(result.would_auto_confirm)
            self.assertFalse(result.retrieval.decision_evaluation_complete)

    def test_competitor_equal_scores_select_multiple_suppliers(self):
        with pipeline(
            self.root, [row("A", spec="20x30mm"), row("B", spec="20x30mm")]
        ) as (item, policy, release, index):
            result = evaluate_candidate(
                item, policy, index, release=release, now=NOW, operator=OPERATOR
            )
            self.assertTrue(result.would_auto_confirm)
            self.assertEqual(len(result.decision.candidates), 2)

    def test_stale_and_retired_release_block(self):
        with pipeline(self.root) as (item, policy, release, index):
            retired = release.model_copy(update={"state": "retired"})
            result = evaluate_candidate(
                item, policy, index, release=retired, now=NOW, operator=OPERATOR
            )
            self.assertFalse(result.would_auto_confirm)

    def test_forged_certificate_without_authority_rejected(self):
        with (
            pipeline(self.root) as (item, policy, release, index),
            self.assertRaises(ContractError),
        ):
            run_match(item, policy, index, release=release, now=NOW, mode="production")

    def test_no_match_default_off_and_unknown_blocks(self):
        with pipeline(self.root, []) as (item, policy, release, index):
            result = evaluate_candidate(
                item, policy, index, release=release, now=NOW, operator=OPERATOR
            )
            self.assertFalse(result.would_auto_no_match)
            result = evaluate_candidate(
                item,
                policy,
                index,
                release=release.model_copy(update={"no_match_enabled": True}),
                now=NOW,
                operator=OPERATOR,
            )
            self.assertTrue(result.would_auto_no_match)

    def test_reconciled_false_provenance_is_not_a_gate(self):
        with pipeline(self.root, environment="production") as (
            item,
            policy,
            release,
            index,
        ):
            self.assertFalse(index.catalog.manifest.source_snapshot_verified)
            result = evaluate_candidate(
                item, policy, index, release=release, now=NOW, operator=OPERATOR
            )
            self.assertTrue(result.would_auto_confirm)

    def test_maintenance_blocks_new_production_evaluation(self):
        with pipeline(self.root, environment="production") as (
            item,
            policy,
            release,
            index,
        ):
            (self.root / "source/control/maintenance.flag").write_text("maintenance")
            with self.assertRaises(ContractError):
                evaluate_candidate(
                    item, policy, index, release=release, now=NOW, operator=OPERATOR
                )
