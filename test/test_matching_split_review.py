"""Regressions for split-policy provenance and study-wide heldout consumption."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from core.matching.contracts import ContractError
from src.matching.datasets import freeze_split
from src.matching.engine import evaluate_candidate
from src.matching.evaluation import EvaluationCase, evaluate_final
from src.matching.store import MatchingStore
from src.matching.training import train_models
from test.test_matching_datasets import label
from test.test_matching_engine import NOW, OPERATOR, pipeline
from test.test_matching_models import examples


class SplitReviewTests(unittest.TestCase):
    def test_explicit_supplier_holdout_trains(self):
        rows = examples()
        split = freeze_split(
            tuple(row.label for row in rows), seller_holdout=frozenset({"s39"})
        )
        comparison = train_models(rows, split)
        self.assertEqual(len(comparison.models), 3)
        for model in comparison.models:
            self.assertNotIn(
                "39",
                model.artifact.train_ids
                + model.artifact.calibration_ids
                + model.artifact.validation_ids,
            )

    def test_training_metadata_cannot_reset_consumed_final_test(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                MatchingStore(root / "matching.sqlite") as store,
                pipeline(root) as (item, policy, release, index),
            ):
                rows = tuple(
                    label(i, purpose=item.purpose, category_id=item.category_id)
                    for i in range(40)
                )
                cases = tuple(
                    EvaluationCase(
                        replace(item, input_row_id=reviewed.request_id), (reviewed,)
                    )
                    for reviewed in freeze_split(rows).test_labels
                )

                def runner(value):
                    return evaluate_candidate(
                        value,
                        policy,
                        index,
                        release=release,
                        now=NOW,
                        operator=OPERATOR,
                    )

                evaluate_final(
                    cases,
                    release,
                    runner,
                    split=freeze_split(rows),
                    store=store,
                    operator=OPERATOR,
                )
                revised = (replace(rows[0], reason="training-only revision"),) + rows[
                    1:
                ]
                with self.assertRaises(ContractError):
                    evaluate_final(
                        cases,
                        release,
                        runner,
                        split=freeze_split(revised),
                        store=store,
                        operator=OPERATOR,
                    )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT count(*) FROM evaluations"
                    ).fetchone()[0],
                    1,
                )

    def test_label_changes_related_members_and_total_renaming_cannot_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                MatchingStore(root / "matching.sqlite") as store,
                pipeline(root) as (item, policy, release, index),
            ):
                first = label(
                    1,
                    request_id=item.input_row_id,
                    purpose=item.purpose,
                    category_id=item.category_id,
                )

                def attempt(reviewed, request, candidate_release=release):
                    return evaluate_final(
                        (EvaluationCase(request, (reviewed,)),),
                        candidate_release,
                        lambda value: evaluate_candidate(
                            value,
                            policy,
                            index,
                            release=candidate_release,
                            now=NOW,
                            operator=OPERATOR,
                        ),
                        split=freeze_split((reviewed,)),
                        store=store,
                        operator=OPERATOR,
                    )

                attempt(first, item)
                unrelated = label(
                    99,
                    request_id="renamed-request",
                    purpose=item.purpose,
                    category_id=item.category_id,
                )
                variants = (
                    replace(first, reason="new reviewer text", target=False),
                    replace(unrelated, family_id=first.family_id),
                    replace(unrelated, revision_group=first.revision_group),
                    replace(unrelated, evidence_hash=first.evidence_hash),
                    replace(unrelated, candidate_id=first.candidate_id),
                    unrelated,
                )
                for reviewed in variants:
                    with (
                        self.subTest(
                            reviewed=reviewed.label_id,
                            family=reviewed.family_id,
                            reason=reviewed.reason,
                        ),
                        self.assertRaises(ContractError),
                    ):
                        attempt(
                            reviewed,
                            replace(item, input_row_id=reviewed.request_id),
                            release.model_copy(update={"threshold": 0.95}),
                        )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT count(*) FROM evaluations"
                    ).fetchone()[0],
                    1,
                )

    def test_new_independent_requests_are_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                MatchingStore(root / "matching.sqlite") as store,
                pipeline(root) as (item, policy, release, index),
            ):
                for number in (1, 2):
                    request = replace(
                        item,
                        input_row_id=f"r{number}",
                        name_raw=f"independent item {number}",
                    )
                    reviewed = label(
                        number,
                        request_id=request.input_row_id,
                        purpose=item.purpose,
                        category_id=item.category_id,
                    )
                    evaluate_final(
                        (EvaluationCase(request, (reviewed,)),),
                        release,
                        lambda value: evaluate_candidate(
                            value,
                            policy,
                            index,
                            release=release,
                            now=NOW,
                            operator=OPERATOR,
                        ),
                        split=freeze_split((reviewed,)),
                        store=store,
                        operator=OPERATOR,
                    )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT count(*) FROM evaluations"
                    ).fetchone()[0],
                    2,
                )

    def test_frozen_label_content_mismatch_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                MatchingStore(root / "matching.sqlite") as store,
                pipeline(root) as (item, policy, release, index),
            ):
                reviewed = label(
                    1,
                    request_id=item.input_row_id,
                    purpose=item.purpose,
                    category_id=item.category_id,
                )
                with self.assertRaises(ContractError):
                    evaluate_final(
                        (EvaluationCase(item, (replace(reviewed, target=False),)),),
                        release,
                        lambda value: evaluate_candidate(
                            value,
                            policy,
                            index,
                            release=release,
                            now=NOW,
                            operator=OPERATOR,
                        ),
                        split=freeze_split((reviewed,)),
                        store=store,
                        operator=OPERATOR,
                    )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT count(*) FROM evaluations"
                    ).fetchone()[0],
                    0,
                )

    def test_interruption_consumes_holdout_before_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                MatchingStore(root / "matching.sqlite") as store,
                pipeline(root) as (item, policy, release, index),
            ):
                reviewed = label(
                    1,
                    request_id=item.input_row_id,
                    purpose=item.purpose,
                    category_id=item.category_id,
                )

                def interrupted(value):
                    raise RuntimeError("fixture interruption")

                cases = (EvaluationCase(item, (reviewed,)),)
                with self.assertRaises(RuntimeError):
                    evaluate_final(
                        cases,
                        release,
                        interrupted,
                        split=freeze_split((reviewed,)),
                        store=store,
                        operator=OPERATOR,
                    )
                with self.assertRaises(ContractError):
                    evaluate_final(
                        cases,
                        release,
                        lambda value: evaluate_candidate(
                            value,
                            policy,
                            index,
                            release=release,
                            now=NOW,
                            operator=OPERATOR,
                        ),
                        split=freeze_split((reviewed,)),
                        store=store,
                        operator=OPERATOR,
                    )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT count(*) FROM evaluations"
                    ).fetchone()[0],
                    1,
                )

    def test_tampered_split_assignments_cannot_train(self):
        rows = examples()
        split = freeze_split(
            tuple(row.label for row in rows), seller_holdout=frozenset({"s39"})
        )
        tampered = replace(
            split,
            assignments=tuple(
                (identity, "train" if identity == "39" else partition)
                for identity, partition in split.assignments
            ),
        )
        with self.assertRaises(ContractError):
            train_models(rows, tampered)

    def test_legacy_consumed_study_fails_closed(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            MatchingStore(Path(temporary) / "matching.sqlite") as store,
        ):
            with store.connection:
                store.connection.execute(
                    "INSERT INTO evaluations VALUES ('legacy','release','consumed')"
                )
            with self.assertRaises(ContractError):
                store.claim_test("new", "new-release", identities=("independent",))
            self.assertEqual(
                store.connection.execute("SELECT count(*) FROM evaluations").fetchone()[
                    0
                ],
                1,
            )

    def test_overlapping_identity_claim_rolls_back_all_new_keys(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            MatchingStore(Path(temporary) / "matching.sqlite") as store,
        ):
            store.claim_test("first", "release", identities=("shared", "original"))
            with self.assertRaises(ContractError):
                store.claim_test(
                    "second", "new-release", identities=("fresh", "shared")
                )
            store.claim_test("third", "new-release", identities=("fresh",))
            self.assertEqual(
                store.connection.execute("SELECT count(*) FROM evaluations").fetchone()[
                    0
                ],
                2,
            )

    def test_split_serialization_preserves_holdout_policy_and_label_contents(self):
        from pydantic import TypeAdapter

        from src.matching.datasets import SplitManifest

        rows = examples()
        split = freeze_split(
            tuple(row.label for row in rows), seller_holdout=frozenset({"s39"})
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "split.json"
            split.write(path)
            restored = TypeAdapter(SplitManifest).validate_json(path.read_text())
        self.assertEqual(restored, split)
        self.assertEqual(restored.seller_holdout, ("s39",))
        self.assertIn("39", {row.label_id for row in restored.test_labels})

    def test_easy_subset_cannot_hide_frozen_holdout_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                MatchingStore(root / "matching.sqlite") as store,
                pipeline(root) as (item, policy, release, index),
            ):
                rows = tuple(
                    label(i, purpose=item.purpose, category_id=item.category_id)
                    for i in range(40)
                )
                request = replace(item, input_row_id=rows[-1].request_id)
                with self.assertRaises(ContractError):
                    evaluate_final(
                        (EvaluationCase(request, (rows[-1],)),),
                        release,
                        lambda value: evaluate_candidate(
                            value,
                            policy,
                            index,
                            release=release,
                            now=NOW,
                            operator=OPERATOR,
                        ),
                        split=freeze_split(rows),
                        store=store,
                        operator=OPERATOR,
                    )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT count(*) FROM evaluations"
                    ).fetchone()[0],
                    0,
                )
