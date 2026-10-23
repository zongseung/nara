import unittest
import tempfile
import json
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from core.matching.contracts import Purpose, ContractError
from src.matching.datasets import Label, Trust, import_labels, freeze_split, binary_examples
from src.matching.sampling import sample_review_pool

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


def label(i, **changes):
    data = dict(
        label_id=str(i),
        request_id=f"r{i}",
        candidate_id=f"c{i}",
        revision_group=f"rev{i}",
        family_id=f"f{i}",
        seller_id=f"s{i}",
        purpose=Purpose.EXACT_IDENTITY,
        category_id="fixture",
        target=True,
        reason="manual evidence",
        evidence_hash=f"{i:064x}",
        reviewer="reviewer",
        reviewed_at=NOW + timedelta(days=i),
        trust=Trust.GOLD,
        provenance="human_review",
        sampling_origin="representative",
        environment="fixture",
    )
    data.update(changes)
    return Label(**data)


class DatasetTests(unittest.TestCase):
    def test_unknown_excluded_not_negative(self):
        self.assertEqual(binary_examples((label(1, target=None),)), ())

    def test_related_revisions_remain_together(self):
        rows = [label(i) for i in range(20)]
        rows[19] = replace(rows[19], family_id=rows[0].family_id)
        result = freeze_split(tuple(rows))
        self.assertEqual(dict(result.assignments)["0"], dict(result.assignments)["19"])

    def test_weak_and_al_train_only(self):
        result = freeze_split(
            (
                label(1, trust=Trust.WEAK),
                label(2, sampling_origin="active_learning"),
                label(3),
            )
        )
        self.assertEqual(dict(result.assignments)["1"], "train")
        self.assertEqual(dict(result.assignments)["2"], "train")

    def test_conflicting_content_rejected(self):
        with self.assertRaises(ContractError):
            freeze_split((label(1), label(2, evidence_hash=f"{1:064x}", target=False)))

    def test_split_hash_stable_and_temporal(self):
        rows = tuple(label(i) for i in range(20))
        result = freeze_split(rows)
        self.assertEqual(
            result.content_hash, freeze_split(tuple(reversed(rows))).content_hash
        )
        assignments = dict(result.assignments)
        self.assertTrue(
            max(r.reviewed_at for r in rows if assignments[r.label_id] == "train")
            < min(r.reviewed_at for r in rows if assignments[r.label_id] == "test")
        )

    def test_empty_reports_absent(self):
        self.assertEqual(freeze_split(()).label_availability, "absent")

    def test_model_cannot_be_gold(self):
        with self.assertRaises(ContractError):
            label(1, provenance="model_prediction")

    def test_untrusted_reviewer_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "labels.jsonl"
            p.write_text(json.dumps(label(1).to_payload()) + "\n")
            with self.assertRaises(ContractError):
                import_labels(p, trusted_reviewers=frozenset({"someone_else"}))

    def test_reproducible_sampling(self):
        rows = tuple(label(i) for i in range(30))
        a = sample_review_pool(
            rows,
            budget=8,
            seed=1,
            uncertain_ids=frozenset({"1", "2", "3", "4"}),
            novel_ids=frozenset({"5", "6"}),
        )
        self.assertEqual(
            a,
            sample_review_pool(
                rows,
                budget=8,
                seed=1,
                uncertain_ids=frozenset({"1", "2", "3", "4"}),
                novel_ids=frozenset({"5", "6"}),
            ),
        )
        self.assertEqual(len(a.label_ids), 8)

    def test_sampling_never_selects_holdout(self):
        rows = tuple(label(i) for i in range(20))
        split = freeze_split(rows)
        sample = sample_review_pool(rows, budget=20, seed=2)
        self.assertTrue(
            all(dict(split.assignments)[i] == "train" for i in sample.label_ids)
        )
