import tempfile
import unittest
from pathlib import Path

from core.matching.contracts import ContractError
from src.matching.datasets import freeze_split
from src.matching.engine import evaluate_candidate
from src.matching.evaluation import EvaluationCase, clopper_pearson_lower, evaluate_final
from src.matching.releases import certify
from src.matching.store import MatchingStore
from test.test_matching_datasets import label
from test.test_matching_engine import NOW, OPERATOR, pipeline


class EvaluationTests(unittest.TestCase):
    def test_exact_600_correct_math_only(self):
        self.assertAlmostEqual(clopper_pearson_lower(600, 600), 0.9950195566, places=8)
        self.assertLess(clopper_pearson_lower(599, 600), 0.995)
        self.assertIsNone(clopper_pearson_lower(0, 0))
        self.assertEqual(clopper_pearson_lower(0, 10), 0)

    def evaluate(
        self, root, *, target=True, environment="fixture", empty=False, absence=None
    ):
        with (
            MatchingStore(root / "matching.sqlite") as store,
            pipeline(root, [] if empty else None) as (item, policy, release, index),
        ):
            release = release.model_copy(update={"no_match_enabled": empty})
            candidate_id = "shopping:listing:ctrtItemMngNo:A"
            reviewed = label(
                1,
                request_id=item.input_row_id,
                candidate_id=candidate_id,
                purpose=item.purpose,
                category_id=item.category_id,
                target=target,
                environment=environment,
            )
            cases = (EvaluationCase(item, (reviewed,), absence),)
            split = freeze_split((reviewed,))

            def runner(request):
                return evaluate_candidate(
                    request, policy, index, release=release, now=NOW, operator=OPERATOR
                )

            report = evaluate_final(
                cases, release, runner, split=split, store=store, operator=OPERATOR
            )
            with self.assertRaises(ContractError):
                evaluate_final(
                    cases, release, runner, split=split, store=store, operator=OPERATOR
                )
            self.assertEqual(
                store.connection.execute("SELECT count(*) FROM decisions").fetchone()[
                    0
                ],
                0,
            )
            return report, release

    def test_fixture_full_pipeline_cannot_certify(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, release = self.evaluate(Path(temporary))
            self.assertEqual(report.positive_count, 1)
            self.assertEqual(report.match_coverage, 1)
            self.assertEqual(report.eligibility, "INSUFFICIENT_EVALUATION_DATA")
            with self.assertRaises(ContractError):
                certify(release, report)

    def test_selected_unknown_bundle_is_unresolved(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, _ = self.evaluate(Path(temporary), target=None)
            self.assertEqual(report.cells[0].positive_unresolved, 1)
            self.assertEqual(report.cells[0].positive_correct, 0)

    def test_one_bad_pair_makes_bundle_wrong(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, _ = self.evaluate(Path(temporary), target=False)
            self.assertEqual(report.cells[0].positive_correct, 0)
            self.assertEqual(report.cells[0].positive_lower, 0)

    def test_no_match_declared_with_real_answer_is_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, _ = self.evaluate(Path(temporary), empty=True, absence=True)
            self.assertEqual(report.no_match_count, 1)
            self.assertEqual(report.cells[0].no_match_correct, 0)
            self.assertEqual(report.cells[0].no_match_unresolved, 0)

    def test_absence_unverified_is_unresolved(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, _ = self.evaluate(Path(temporary), empty=True, target=False)
            self.assertEqual(report.cells[0].no_match_unresolved, 1)

    def test_zero_cases_are_unmeasured(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                MatchingStore(root / "matching.sqlite") as store,
                pipeline(root) as (_item, policy, release, index),
            ):
                report = evaluate_final(
                    (),
                    release,
                    lambda request: evaluate_candidate(
                        request,
                        policy,
                        index,
                        release=release,
                        now=NOW,
                        operator=OPERATOR,
                    ),
                    split=freeze_split(()),
                    store=store,
                    operator=OPERATOR,
                )
                self.assertIsNone(report.match_coverage)
                self.assertEqual(report.cells, ())

    def test_train_labels_cannot_enter_final_test(self):
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
                    sampling_origin="active_learning",
                )
                with self.assertRaises(ContractError):
                    evaluate_final(
                        (EvaluationCase(item, (reviewed,)),),
                        release,
                        lambda request: evaluate_candidate(
                            request,
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
