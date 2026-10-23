"""One frozen release config binds every decision route and certification dependency."""

from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.matching.contracts import NORMALIZER_VERSION, ContractError, ReleaseManifest
from src.matching.retrieval import RetrievalBudget


class OperatorContext(BaseModel):
    """Created by the trusted operator entrypoint; never parsed from workbook contents."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    principal: str
    roles: frozenset[Literal["reviewer", "evaluator"]]

    def require(self, role: Literal["reviewer", "evaluator"]) -> None:
        if not self.principal or role not in self.roles:
            raise ContractError("operator", "unauthorized role")


class DecisionConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    top_n: int = Field(default=3, ge=1, le=10, strict=True)
    shortage: Literal["allow_fewer", "review"] = "allow_fewer"
    ranking: Literal["probability", "lowest_price"] = "probability"
    supplier_dedupe: Literal["verified_seller"] = "verified_seller"
    reuse: Literal["disabled", "certified_registry"] = "disabled"
    max_scan: int = Field(default=50_000, ge=1)
    max_seconds: float = Field(default=20, gt=0)
    max_detail: int = Field(default=500, ge=1)

    @property
    def budget(self) -> RetrievalBudget:
        return RetrievalBudget(self.max_scan, self.max_seconds, self.max_detail)


class Certificate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    release_hash: str
    evaluation_hash: str
    label_hash: str
    split_hash: str
    certified_routes: tuple[str, ...]
    positive_count: int = Field(ge=1)
    positive_lower: float = Field(ge=0.995, le=1)
    no_match_count: int = Field(default=0, ge=0)
    no_match_lower: float | None = Field(default=None, ge=0.995, le=1)
    environment: Literal["production"] = "production"


class Release(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    manifest: ReleaseManifest
    config: DecisionConfig = DecisionConfig()
    threshold: float = Field(default=1, ge=0, le=1)
    model_hash: str | None = None
    registry_hash: str | None = None
    feature_version: str = "pair-features/1"
    state: Literal["draft", "candidate", "certified", "retired"] = "draft"
    certificate: Certificate | None = None
    no_match_enabled: bool = False

    @property
    def content_hash(self) -> str:
        return sha256(
            self.model_dump_json(exclude={"certificate", "state"}).encode()
        ).hexdigest()

    def require_certificate(self) -> None:
        certificate = self.certificate
        if (
            self.state != "certified"
            or certificate is None
            or certificate.release_hash != self.content_hash
        ):
            raise ContractError("release", "missing/stale/retired certificate")
        if (
            self.manifest.environment != "production"
            or self.manifest.matching_normalizer_version != NORMALIZER_VERSION
        ):
            raise ContractError("release", "nonproduction or stale normalizer")
        if self.no_match_enabled and (
            certificate.no_match_lower is None or certificate.no_match_count < 1
        ):
            raise ContractError("release", "absence certification missing")


def write_release(release: Release, path: Path) -> None:
    with path.open("x") as stream:
        stream.write(release.model_dump_json(indent=2))


def read_release(path: Path, *, expected_hash: str | None = None) -> Release:
    if path.is_symlink():
        raise ContractError("release_path", "symlink forbidden")
    release = Release.model_validate_json(path.read_text())
    if expected_hash is not None and release.content_hash != expected_hash:
        raise ContractError("release_hash", "content changed")
    return release


class Cell(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    category: str
    purpose: str
    route: str
    top_n: int
    positive_count: int
    positive_correct: int
    positive_unresolved: int
    positive_lower: float | None
    no_match_count: int
    no_match_correct: int
    no_match_unresolved: int
    no_match_lower: float | None


class EvaluationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    release_hash: str
    test_hash: str
    split_hash: str
    label_hash: str
    registry_hash: str | None
    total: int
    positive_count: int
    no_match_count: int
    match_coverage: float | None
    auto_close_rate: float | None
    status_counts: tuple[tuple[str, int], ...]
    incomplete_retrieval_count: int
    unknown_candidate_count: int
    recall_at_500: float | None
    pair_precision: float | None
    pair_recall: float | None
    bundle_precision: float | None
    latency_p50_seconds: float
    latency_p95_seconds: float
    peak_rss_bytes: int
    throughput_per_minute: float | None
    cells: tuple[Cell, ...]
    eligibility: Literal["eligible", "INSUFFICIENT_EVALUATION_DATA"]
    limitations: tuple[str, ...]
    holdouts: tuple[tuple[str, str], ...]
    case_metadata_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None)
    schema_version: Literal["matching-evaluation/1"] = "matching-evaluation/1"

    @property
    def content_hash(self) -> str:
        return sha256(self.model_dump_json().encode()).hexdigest()

    def write(self, path: Path) -> None:
        with path.open("x") as stream:
            stream.write(self.model_dump_json(indent=2))


class ReleaseAuthority:
    """Protected operator-owned local root; employees only consume its active pointer."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def require_active(self, release: Release) -> None:
        release.require_certificate()
        pointer = self.root / "active-release.json"
        if pointer.is_symlink():
            raise ContractError("release_authority", "symlink pointer")
        active = read_release(pointer)
        if active.model_dump_json() != release.model_dump_json():
            raise ContractError(
                "release_authority", "release is not operator activated"
            )
        certificate = release.certificate
        if certificate is None:
            raise ContractError("release_authority", "missing certificate")
        evidence = self.root / (certificate.evaluation_hash + ".evaluation.json")
        if evidence.is_symlink():
            raise ContractError("release_authority", "symlink evidence")
        report = EvaluationReport.model_validate_json(evidence.read_text())
        if (
            report.content_hash != certificate.evaluation_hash
            or report.release_hash != release.content_hash
            or report.eligibility != "eligible"
        ):
            raise ContractError("release_authority", "evaluation evidence mismatch")

    def activate(
        self, release: Release, report_path: Path, *, operator: "OperatorContext"
    ) -> None:
        operator.require("evaluator")
        report = EvaluationReport.model_validate_json(report_path.read_text())
        expected = certify(
            release.model_copy(update={"certificate": None, "state": "candidate"}),
            report,
        )
        if expected != release:
            raise ContractError(
                "activation", "release differs from certified evaluation"
            )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        evidence = self.root / (report.content_hash + ".evaluation.json")
        if not evidence.exists():
            report.write(evidence)
            evidence.chmod(0o600)
        pending = self.root / "active-release.pending"
        write_release(release, pending)
        pending.chmod(0o600)
        pending.replace(self.root / "active-release.json")


def certify(release: Release, report: EvaluationReport) -> Release:
    if (
        report.release_hash != release.content_hash
        or report.eligibility != "eligible"
        or release.manifest.environment != "production"
    ):
        raise ContractError(
            "certificate", "independent production evidence is insufficient"
        )
    if (
        any(
            c.positive_unresolved
            or c.positive_lower is None
            or c.positive_lower < 0.995
            for c in report.cells
            if c.positive_count
        )
        or not report.positive_count
    ):
        raise ContractError("certificate", "cell did not pass")
    if release.no_match_enabled and (
        not report.no_match_count
        or any(
            c.no_match_unresolved
            or c.no_match_lower is None
            or c.no_match_lower < 0.995
            for c in report.cells
            if c.no_match_count
        )
    ):
        raise ContractError("certificate", "absence cell did not pass")
    certificate = Certificate(
        release_hash=release.content_hash,
        evaluation_hash=report.content_hash,
        label_hash=report.label_hash,
        split_hash=report.split_hash,
        positive_count=report.positive_count,
        positive_lower=min(
            c.positive_lower or 0 for c in report.cells if c.positive_count
        ),
        certified_routes=tuple(
            sorted(c.route for c in report.cells if c.positive_count)
        ),
        no_match_count=report.no_match_count,
        no_match_lower=min(
            c.no_match_lower or 0 for c in report.cells if c.no_match_count
        )
        if release.no_match_enabled
        else None,
    )
    return release.model_copy(update={"certificate": certificate, "state": "certified"})
