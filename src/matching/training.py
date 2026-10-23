"""CPU comparison with train/calibration/validation separation; final test is untouched."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from catboost import CatBoostClassifier
from sklearn.linear_model import LogisticRegression

from core.matching.contracts import ContractError
from src.matching.datasets import Label, SplitManifest, freeze_split
from core.matching.features import Pair, Vocabulary, features
from src.matching.models import Artifact, CPUModel, runtime_versions


@dataclass(frozen=True, slots=True)
class Example:
    pair: Pair
    label: Label


@dataclass(frozen=True, slots=True)
class Comparison:
    models: tuple[CPUModel, ...]
    champion: CPUModel
    validation_coverage: tuple[tuple[str, float], ...]
    business_quality: str


def train_models(examples: tuple[Example, ...], split: SplitManifest) -> Comparison:
    """Freeze vocabulary on train, sigmoid on calibration, champion/threshold on validation."""
    labels = tuple(e.label for e in examples)
    if freeze_split(labels, seller_holdout=frozenset(split.seller_holdout)) != split:
        raise ContractError(
            "split", "manifest does not match independent group/time split"
        )
    for example in examples:
        a, b, label = example.pair.request, example.pair.candidate, example.label
        if (a.input_row_id, b.listing_key, a.purpose, a.category_id) != (
            label.request_id,
            label.candidate_id,
            label.purpose,
            label.category_id,
        ):
            raise ContractError("label", "pair identity mismatch")
    assignments = dict(split.assignments)
    partitions = {
        name: tuple(
            e
            for e in examples
            if assignments[e.label.label_id] == name and e.label.target is not None
        )
        for name in ("train", "calibration", "validation")
    }
    if any(
        {e.label.target for e in part} != {False, True} for part in partitions.values()
    ):
        raise ContractError(
            "model_ineligible", "both classes required in train/calibration/validation"
        )
    train, calibration, validation = (
        partitions[name] for name in ("train", "calibration", "validation")
    )
    vocabulary = Vocabulary.fit_train(tuple(e.pair for e in train))
    x = np.asarray([features(e.pair, vocabulary) for e in train], dtype=float)
    y = np.asarray([int(e.label.target is True) for e in train])
    logistic = LogisticRegression(max_iter=1000, random_state=42).fit(x, y)
    booster = CatBoostClassifier(
        task_type="CPU",
        depth=6,
        iterations=300,
        thread_count=2,
        random_seed=42,
        verbose=False,
        allow_writing_files=False,
    )
    booster.fit(x, y)
    with TemporaryDirectory() as temporary:
        binary = Path(temporary) / "model.cbm"
        booster.save_model(str(binary))
        binary_hash = sha256(binary.read_bytes()).hexdigest()
    models: list[CPUModel] = []
    coverages: list[tuple[str, float]] = []
    for algorithm in ("rules", "logreg", "catboost"):
        artifact = Artifact(
            algorithm=algorithm,
            vocabulary=vocabulary,
            threshold=1,
            dataset_hash=split.content_hash,
            split_hash=split.content_hash,
            runtime=runtime_versions(),
            train_ids=tuple(e.label.label_id for e in train),
            calibration_ids=tuple(e.label.label_id for e in calibration),
            validation_ids=tuple(e.label.label_id for e in validation),
            environment="production"
            if all(l.environment == "production" for l in labels)
            else "fixture",
            coefficients=tuple(float(v) for v in logistic.coef_[0])
            if algorithm == "logreg"
            else (),
            intercept=float(logistic.intercept_[0]) if algorithm == "logreg" else 0,
            binary_hash=binary_hash if algorithm == "catboost" else None,
        )
        model = CPUModel(artifact, booster if algorithm == "catboost" else None)
        calibrator = LogisticRegression(max_iter=1000, random_state=42).fit(
            np.asarray([[model.raw_score(e.pair)] for e in calibration]),
            np.asarray([int(e.label.target is True) for e in calibration]),
        )
        artifact = artifact.model_copy(
            update={
                "calibration_slope": float(calibrator.coef_[0, 0]),
                "calibration_intercept": float(calibrator.intercept_[0]),
            }
        )
        model = CPUModel(artifact, model.booster)
        scores = [(model.probability(e.pair), e.label.target) for e in validation]
        thresholds = sorted({p for p, _ in scores})
        acceptable = [
            t
            for t in thresholds
            if all(target is True for p, target in scores if p >= t)
        ]
        threshold = min(acceptable) if acceptable else 1.0
        coverage = sum(p >= threshold for p, _ in scores) / len(scores)
        model = CPUModel(
            artifact.model_copy(update={"threshold": threshold}), model.booster
        )
        models.append(model)
        coverages.append((algorithm, coverage))
    best = max(range(len(models)), key=lambda i: coverages[i][1])
    return Comparison(
        tuple(models),
        models[best],
        tuple(coverages),
        "UNMEASURED: independent final-test certification required; fixtures never certify",
    )
