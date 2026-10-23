import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from core.matching.contracts import ContractError
from src.matching.engine import run_match
from src.matching.releases import DecisionConfig, OperatorContext
from src.matching.reviews import Registry, Review, pair_key, read_registry
from src.matching.store import MatchingStore
from test.test_matching_engine import NOW, OPERATOR, pipeline


class ReviewTests(unittest.TestCase):
    def test_import_pending_idempotent_and_does_not_change_auto_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                MatchingStore(root / "matching.sqlite") as ledger,
                pipeline(root) as (item, policy, release, index),
            ):
                release = release.model_copy(
                    update={
                        "config": DecisionConfig(reuse="certified_registry"),
                        "registry_hash": Registry().content_hash,
                    }
                )
                result = run_match(
                    item, policy, index, release=release, now=NOW, registry=Registry()
                )
                job = ledger.record_job(item, result, release)
                candidate_id, evidence = ledger.connection.execute(
                    "SELECT candidate_id,evidence_hash FROM candidate_checks"
                ).fetchone()
                review = Review(
                    review_id="review",
                    job_id=job,
                    candidate_id=candidate_id,
                    evidence_hash=evidence,
                    target=True,
                    reason="verified fixture",
                    reviewed_at=NOW,
                    expires_at=NOW + timedelta(days=1),
                )
                before = ledger.connection.execute(
                    "SELECT payload FROM decisions"
                ).fetchone()[0]
                approval = ledger.import_review(review, OPERATOR, now=NOW)
                self.assertEqual(
                    approval, ledger.import_review(review, OPERATOR, now=NOW)
                )
                self.assertEqual(
                    ledger.connection.execute(
                        "SELECT count(*) FROM review_events"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    before,
                    ledger.connection.execute(
                        "SELECT payload FROM decisions"
                    ).fetchone()[0],
                )
                self.assertEqual(release.registry_hash, Registry().content_hash)
                self.assertIsNotNone(approval)
                candidate = result.candidate_checks[0].candidate
                changed_price = replace(
                    candidate, price=replace(candidate.price, amount=Decimal(101))
                )
                self.assertEqual(
                    pair_key(item, candidate, release),
                    pair_key(item, changed_price, release),
                )
                changed_spec = replace(candidate, model="changed")
                self.assertNotEqual(
                    pair_key(item, candidate, release),
                    pair_key(item, changed_spec, release),
                )
                registry = Registry(approvals=(approval,))
                registry.write(root / "registry.json")
                self.assertEqual(
                    registry,
                    read_registry(root / "registry.json", registry.content_hash),
                )
                with self.assertRaises(ContractError):
                    ledger.import_review(
                        review.model_copy(
                            update={"review_id": "bad", "evidence_hash": "wrong"}
                        ),
                        OPERATOR,
                        now=NOW,
                    )
                with self.assertRaises(ContractError):
                    ledger.import_review(
                        review,
                        OperatorContext(principal="employee", roles=frozenset()),
                        now=NOW,
                    )
                with self.assertRaises(ContractError):
                    ledger.import_review(review, OPERATOR, now=NOW + timedelta(days=2))
                self.assertFalse(
                    registry.permits(approval.pair_key, NOW + timedelta(days=2))
                )
