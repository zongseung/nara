"""Safe JSON/native CPU models with hash-bound, trusted-local artifacts; no pickle."""

from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import version
from math import exp, isfinite
from pathlib import Path
from typing import Literal, Protocol, assert_never

from catboost import CatBoostClassifier
from pydantic import BaseModel, ConfigDict, Field

from core.matching.contracts import ContractError
from core.matching.features import FEATURE_NAMES, FEATURE_VERSION, Pair, Vocabulary, features


class Artifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    algorithm: Literal["rules", "logreg", "catboost"]
    vocabulary: Vocabulary
    coefficients: tuple[float, ...] = ()
    intercept: float = 0
    calibration_slope: float = 1
    calibration_intercept: float = 0
    threshold: float = Field(ge=0, le=1)
    dataset_hash: str
    split_hash: str
    runtime: tuple[tuple[str, str], ...]
    train_ids: tuple[str, ...]
    calibration_ids: tuple[str, ...]
    validation_ids: tuple[str, ...]
    binary_hash: str | None = None
    feature_version: str = FEATURE_VERSION
    feature_names: tuple[str, ...] = FEATURE_NAMES
    environment: Literal["fixture", "production"]

    @property
    def content_hash(self) -> str:
        return sha256(self.model_dump_json().encode()).hexdigest()


def sigmoid(value: float) -> float:
    return 1 / (1 + exp(-max(-700, min(700, value))))


class Scorer(Protocol):
    @property
    def artifact_hash(self) -> str: ...
    @property
    def environment(self) -> str: ...
    def probability(self, pair: Pair) -> float: ...


@dataclass(frozen=True, slots=True)
class CPUModel:
    artifact: Artifact
    booster: CatBoostClassifier | None = None

    @property
    def artifact_hash(self) -> str:
        return self.artifact.content_hash

    @property
    def environment(self) -> str:
        return self.artifact.environment

    def raw_score(self, pair: Pair) -> float:
        values = features(pair, self.artifact.vocabulary)
        match self.artifact.algorithm:
            case "rules":
                return 8 * (values[0] - 0.5) - sum(values[i] for i in (3, 6, 7, 8, 9))
            case "logreg":
                return (
                    sum(
                        a * b
                        for a, b in zip(values, self.artifact.coefficients, strict=True)
                    )
                    + self.artifact.intercept
                )
            case "catboost":
                if self.booster is None:
                    raise ContractError("model", "missing CatBoost binary")
                return float(
                    self.booster.predict([values], prediction_type="RawFormulaVal")[0]
                )
            case unreachable:
                assert_never(unreachable)

    def probability(self, pair: Pair) -> float:
        result = sigmoid(
            self.artifact.calibration_slope * self.raw_score(pair)
            + self.artifact.calibration_intercept
        )
        if not isfinite(result):
            raise ContractError("probability", "nonfinite prediction")
        return result


def runtime_versions() -> tuple[tuple[str, str], ...]:
    return tuple(
        (name, version(name))
        for name in ("pydantic", "numpy", "scikit-learn", "catboost", "scipy")
    )


def save_model(model: CPUModel, root: Path) -> Path:
    """Exclusive content-addressed directory; interrupted saves never load without manifest."""
    directory = root / model.artifact_hash
    directory.mkdir(parents=True, exist_ok=False)
    if model.booster is not None:
        binary = directory / "model.cbm"
        model.booster.save_model(str(binary))
        if sha256(binary.read_bytes()).hexdigest() != model.artifact.binary_hash:
            raise ContractError("binary_hash", "model changed after freezing")
    with (directory / "artifact.json").open("x") as stream:
        stream.write(model.artifact.model_dump_json(indent=2))
    return directory


def load_model(root: Path, digest: str) -> CPUModel:
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ContractError("model_path", "content hash required")
    directory = root / digest
    if directory.is_symlink() or directory.resolve().parent != root.resolve():
        raise ContractError("model_path", "outside trusted root")
    manifest = directory / "artifact.json"
    if manifest.is_symlink():
        raise ContractError("model_path", "symlink manifest")
    artifact = Artifact.model_validate_json(manifest.read_text())
    if (
        artifact.content_hash != digest
        or artifact.runtime != runtime_versions()
        or artifact.feature_names != FEATURE_NAMES
        or artifact.feature_version != FEATURE_VERSION
    ):
        raise ContractError("model_hash", "artifact/runtime/features mismatch")
    booster = None
    if artifact.binary_hash is not None:
        binary = directory / "model.cbm"
        if (
            binary.is_symlink()
            or sha256(binary.read_bytes()).hexdigest() != artifact.binary_hash
        ):
            raise ContractError("binary_hash", "tampered binary")
        booster = CatBoostClassifier(task_type="CPU", thread_count=2)
        booster.load_model(str(binary))
    return CPUModel(artifact, booster)
