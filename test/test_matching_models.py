import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from core.matching.contracts import ContractError
from src.matching.datasets import freeze_split
from core.matching.features import Pair, Vocabulary, features
from src.matching.models import load_model, save_model
from src.matching.training import Example, train_models
from test.test_matching_constraints import pair
from test.test_matching_datasets import label


def examples():
    result = []
    for i in range(40):
        item, candidate = pair()
        item = replace(item, input_row_id=f"r{i}", name_raw="stainless board")
        candidate = replace(
            candidate,
            listing_key=f"c{i}",
            name_raw="stainless board" if i % 2 else "wooden chair",
        )
        result.append(
            Example(
                Pair(item, candidate),
                label(i, purpose=item.purpose, target=bool(i % 2)),
            )
        )
    return tuple(result)


class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.examples = examples()
        cls.split = freeze_split(tuple(e.label for e in cls.examples))
        cls.comparison = train_models(cls.examples, cls.split)

    def test_all_cpu_models_calibrated_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as temporary:
            for model in self.comparison.models:
                with self.subTest(algorithm=model.artifact.algorithm):
                    path = save_model(model, Path(temporary))
                    restored = load_model(Path(temporary), path.name)
                    self.assertAlmostEqual(
                        model.probability(self.examples[-1].pair),
                        restored.probability(self.examples[-1].pair),
                    )
                    self.assertTrue(model.artifact.calibration_ids)
                    self.assertFalse(
                        set(model.artifact.train_ids)
                        & set(model.artifact.calibration_ids)
                    )
                    self.assertFalse(
                        set(model.artifact.validation_ids)
                        & {i for i, p in self.split.assignments if p == "test"}
                    )
        self.assertEqual(
            {m.artifact.algorithm for m in self.comparison.models},
            {"rules", "logreg", "catboost"},
        )

    def test_test_text_cannot_fit_vocabulary(self):
        changed = tuple(
            replace(
                e,
                pair=replace(
                    e.pair,
                    candidate=replace(e.pair.candidate, name_raw="UNSEENTESTTOKEN"),
                ),
            )
            if dict(self.split.assignments)[e.label.label_id] == "test"
            else e
            for e in self.examples
        )
        result = train_models(changed, self.split)
        self.assertEqual(
            self.comparison.champion.artifact.vocabulary,
            result.champion.artifact.vocabulary,
        )
        self.assertEqual(
            self.comparison.champion.artifact_hash, result.champion.artifact_hash
        )

    def test_one_class_rejected(self):
        changed = tuple(
            replace(e, label=replace(e.label, target=True)) for e in self.examples
        )
        with self.assertRaises(ContractError):
            train_models(changed, freeze_split(tuple(e.label for e in changed)))

    def test_tamper_and_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = self.comparison.models[0]
            path = save_model(model, root)
            (path / "artifact.json").write_text(
                (path / "artifact.json")
                .read_text()
                .replace('"threshold":', '"threshold":0.1,"extra":')
            )
            with self.assertRaises(ValueError):
                load_model(root, model.artifact_hash)
            with self.assertRaises(ContractError):
                load_model(root, "../escape")

    def test_raw_ids_do_not_change_features(self):
        p = self.examples[0].pair
        vocabulary = Vocabulary.fit_train((p,))
        changed = Pair(
            replace(p.request, seller_id="unique123"),
            replace(p.candidate, seller_id="unique456"),
        )
        self.assertEqual(features(p, vocabulary), features(changed, vocabulary))
