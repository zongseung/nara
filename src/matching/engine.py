"""One pinned source, mandatory checks, calibrated suitability, and guarded decisions."""

from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import isfinite
from pathlib import Path
from typing import Literal

from src.matching.catalog import open_catalog
from core.matching.constraints import aggregate_status, evaluate_constraints
from core.matching.contracts import (
    NORMALIZER_VERSION,
    Candidate,
    ConstraintResult,
    ConstraintStatus,
    ContractError,
    DecisionStatus,
    InputItem,
    MatchDecision,
    Purpose,
    ReasonCode,
    RetrievalResult,
)
from core.matching.features import Pair
from src.matching.index import SearchIndex
from src.matching.models import Scorer
from core.matching.policy import Policy
from src.matching.releases import OperatorContext, Release, ReleaseAuthority
from src.matching.retrieval import retrieve
from src.matching.reviews import Registry, pair_key


@dataclass(frozen=True, slots=True)
class CandidateCheck:
    candidate: Candidate
    constraints: tuple[ConstraintResult, ...]
    suitability_probability: float | None
    rank_score: float | None
    route: Literal["model", "rules", "reuse"]


@dataclass(frozen=True, slots=True)
class EngineResult:
    decision: MatchDecision
    retrieval: RetrievalResult
    candidate_checks: tuple[CandidateCheck, ...]
    requested_count: int
    shortage: int
    release_hash: str
    evaluation_only: bool = False
    would_auto_confirm: bool = False
    would_auto_no_match: bool = False


def run_match(
    item: InputItem,
    policy: Policy,
    index: SearchIndex,
    *,
    release: Release,
    now: datetime,
    scorer: Scorer | None = None,
    registry: Registry | None = None,
    mode: Literal["shadow", "production"] = "shadow",
    authority: ReleaseAuthority | None = None,
) -> EngineResult:
    if mode not in ("shadow", "production"):
        raise ContractError("mode", "operator evaluation is a separate entrypoint")
    if mode == "production":
        if authority is None:
            raise ContractError(
                "release_authority", "trusted active release root required"
            )
        authority.require_active(release)
    return _run(
        item,
        policy,
        index,
        release=release,
        now=now,
        scorer=scorer,
        registry=registry,
        production=mode == "production",
        evaluation=False,
    )


def evaluate_candidate(
    item: InputItem,
    policy: Policy,
    index: SearchIndex,
    *,
    release: Release,
    now: datetime,
    operator: OperatorContext,
    scorer: Scorer | None = None,
    registry: Registry | None = None,
) -> EngineResult:
    operator.require("evaluator")
    return _run(
        item,
        policy,
        index,
        release=release,
        now=now,
        scorer=scorer,
        registry=registry,
        production=release.manifest.environment == "production",
        evaluation=True,
    )


def _run(
    item: InputItem,
    policy: Policy,
    index: SearchIndex,
    *,
    release: Release,
    now: datetime,
    scorer: Scorer | None,
    registry: Registry | None,
    production: bool,
    evaluation: bool,
) -> EngineResult:
    with ExitStack() as stack:
        if production:
            source = index.catalog.manifest
            stack.enter_context(
                open_catalog(
                    source, root=Path(source.reader_locator.data_root), production=True
                )
            )
        return _decide(
            item,
            policy,
            index,
            release=release,
            now=now,
            scorer=scorer,
            registry=registry,
            production=production,
            evaluation=evaluation,
        )


def _decide(
    item: InputItem,
    policy: Policy,
    index: SearchIndex,
    *,
    release: Release,
    now: datetime,
    scorer: Scorer | None,
    registry: Registry | None,
    production: bool,
    evaluation: bool,
) -> EngineResult:
    source = index.catalog.manifest
    index.catalog.require_open()
    retrieval = retrieve(item, policy, index, now=now, budget=release.config.budget)
    reasons: list[ReasonCode] = []
    checks: list[CandidateCheck] = []
    selected: list[CandidateCheck] = []
    valid_release = (
        release.manifest.policy_hash == policy.content_hash
        and release.manifest.purpose is item.purpose
        and release.manifest.category_id == item.category_id
        and release.manifest.source_normalizer_version
        == source.source_normalizer_version
        and release.manifest.matching_normalizer_version == NORMALIZER_VERSION
        and release.manifest.environment == source.environment
        and release.state != "retired"
        and (
            release.model_hash is None
            or (
                scorer is not None
                and scorer.artifact_hash == release.model_hash
                and scorer.environment == release.manifest.environment
            )
        )
    )
    if release.config.reuse == "certified_registry":
        valid_release = (
            valid_release
            and registry is not None
            and registry.content_hash == release.registry_hash
        )
    if production:
        valid_release = (
            valid_release
            and policy.auto_eligible
            and source.environment == "production"
            and source.coverage_complete
            and source.unresolved_count == 0
        )
        if not evaluation:
            try:
                release.require_certificate()
            except ContractError:
                valid_release = False
    if not valid_release:
        reasons.append(ReasonCode.RELEASE_INELIGIBLE)
    if (
        not retrieval.scope_scan_complete
        or not retrieval.decision_evaluation_complete
        or retrieval.truncated
    ):
        reasons.append(ReasonCode.INCOMPLETE_RETRIEVAL)
    for candidate in retrieval.candidates:
        constraints = evaluate_constraints(item, candidate, policy, now=now)
        status = aggregate_status(constraints)
        probability = None
        route: Literal["model", "rules", "reuse"] = "rules"
        if status is ConstraintStatus.PASS:
            probability = 1.0
            if scorer is not None and release.model_hash is not None:
                route = "model"
                probability = scorer.probability(Pair(item, candidate))
            if (
                release.config.reuse == "certified_registry"
                and registry is not None
                and registry.content_hash == release.registry_hash
                and registry.permits(pair_key(item, candidate, release), now)
            ):
                route, probability = "reuse", 1.0
            if not isfinite(probability) or not 0 <= probability <= 1:
                raise ContractError(
                    "probability", "expected finite calibrated probability"
                )
        checks.append(
            CandidateCheck(candidate, constraints, probability, probability, route)
        )
    eligible = [
        c
        for c in checks
        if c.suitability_probability is not None
        and c.suitability_probability >= release.threshold
    ]
    if any(c.candidate.seller_id is None for c in eligible):
        reasons.append(ReasonCode.MISSING_REQUIRED_ATTRIBUTE)
    if (
        item.purpose is Purpose.EXACT_IDENTITY
        and len(
            {
                c.candidate.goods_id
                or (c.candidate.manufacturer_id, c.candidate.model, c.candidate.options)
                for c in eligible
            }
        )
        > 1
    ):
        reasons.append(ReasonCode.MISMATCH)
    if release.config.ranking == "lowest_price":
        bases = {
            (
                c.candidate.price.kind,
                c.candidate.price.currency,
                c.candidate.price.tax_basis,
                c.candidate.price.unit,
                c.candidate.price.pack_quantity,
            )
            for c in eligible
        }
        if (
            len(bases) > 1
            or any(None in b for b in bases)
            or any(c.candidate.price.amount is None for c in eligible)
        ):
            reasons.append(ReasonCode.UNKNOWN_PRICE_BASIS)
        eligible.sort(
            key=lambda c: (
                c.candidate.price.amount or Decimal("Infinity"),
                c.candidate.listing_key,
            )
        )
        if (
            len(eligible) > 1
            and eligible[0].candidate.price.amount == eligible[1].candidate.price.amount
        ):
            reasons.append(ReasonCode.MISMATCH)
    else:
        eligible.sort(
            key=lambda c: (-(c.suitability_probability or 0), c.candidate.listing_key)
        )
    sellers: set[str | None] = set()
    for check in eligible:
        if check.candidate.seller_id not in sellers:
            sellers.add(check.candidate.seller_id)
            selected.append(check)
        if len(selected) == release.config.top_n:
            break
    if production and not evaluation and release.certificate is not None:
        bundle_route = "+".join(sorted({check.route for check in selected}))
        if selected and bundle_route not in release.certificate.certified_routes:
            reasons.append(ReasonCode.RELEASE_INELIGIBLE)
    shortage = release.config.top_n - len(selected)
    if shortage and release.config.shortage == "review":
        reasons.append(ReasonCode.MISSING_REQUIRED_ATTRIBUTE)
    positive = bool(selected) and not reasons
    negative = (
        not retrieval.candidates
        and retrieval.unknown_count == 0
        and not reasons
        and release.no_match_enabled
    )
    status = DecisionStatus.REVIEW_REQUIRED
    if (production and not evaluation) and (positive or negative):
        status = (
            DecisionStatus.AUTO_CONFIRMED if positive else DecisionStatus.AUTO_NO_MATCH
        )
    if not reasons and not positive and not negative:
        reasons.append(ReasonCode.MISSING_REQUIRED_ATTRIBUTE)
    decision = MatchDecision(
        item.input_row_id,
        status,
        tuple(dict.fromkeys(reasons)),
        tuple(c.candidate.listing_key for c in selected),
        tuple(r for c in checks for r in c.constraints),
        source.snapshot_id,
        release.manifest.release_id,
        "evaluation" if evaluation else "production" if production else "shadow",
    )
    return EngineResult(
        decision,
        retrieval,
        tuple(checks),
        release.config.top_n,
        shortage,
        release.content_hash,
        evaluation,
        positive if evaluation else False,
        negative if evaluation else False,
    )
