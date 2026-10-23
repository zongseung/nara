"""One-use independent full-pipeline evaluation with exact bundle confidence bounds."""

import json
import resource
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from hashlib import sha256
from statistics import median
from time import perf_counter
from typing import Literal

from scipy.stats import beta

from core.matching.contracts import ContractError, InputItem, dumps_contract
from src.matching.datasets import Label, SplitManifest, Trust
from src.matching.engine import EngineResult
from src.matching.releases import Cell, EvaluationReport, OperatorContext, Release
from src.matching.reviews import Registry
from src.matching.store import MatchingStore


def clopper_pearson_lower(correct: int, total: int) -> float | None:
    if total < 0 or correct < 0 or correct > total:
        raise ContractError("counts", "expected 0 <= correct <= total")
    if total == 0:
        return None
    if correct == 0:
        return 0.0
    return float(beta.ppf(0.05, correct, total - correct + 1))


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    request: InputItem
    labels: tuple[Label, ...]
    absence_verified: bool | None = None
    holdout: Literal["representative", "supplier", "subsequent_time"] = "representative"
    evidence_hash: str | None = None


def case_metadata_hash(split: SplitManifest, cases: tuple[EvaluationCase, ...]) -> str:
    payload = {'split_hash': split.content_hash, 'cases': [
        {'request_id': case.request.input_row_id, 'holdout': case.holdout,
         'absence_verified': case.absence_verified, 'evidence_hash': case.evidence_hash}
        for case in sorted(cases, key=lambda case: case.request.input_row_id)
    ]}
    return sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def evaluate_final(
    cases: tuple[EvaluationCase, ...],
    release: Release,
    runner: Callable[[InputItem], EngineResult],
    *,
    split: SplitManifest,
    store: MatchingStore,
    operator: OperatorContext,
    registry: Registry | None = None,
) -> EvaluationReport:
    """Trusted operator supplies the exact evaluate_candidate pipeline, never workbook flags."""
    operator.require("evaluator")
    if release.state != "candidate":
        raise ContractError("release", "freeze a candidate before opening final-test")
    if (registry.content_hash if registry else None) != release.registry_hash:
        raise ContractError("registry", "frozen registry differs from release")
    labels = tuple(label for case in cases for label in case.labels)
    assignments = dict(split.assignments)
    if any(assignments.get(label.label_id) != "test" for label in labels):
        raise ContractError(
            "final_test", "all evaluation labels must belong to untouched test"
        )
    frozen_labels = {label.label_id: label for label in split.test_labels}
    if set(frozen_labels) != {
        identity for identity, partition in split.assignments if partition == "test"
    }:
        raise ContractError(
            "final_test", "missing or inconsistent frozen test label provenance"
        )
    if any(frozen_labels.get(label.label_id) != label for label in labels):
        raise ContractError(
            "final_test", "label contents differ from frozen test manifest"
        )
    if (
        len(labels) != len(frozen_labels)
        or {label.label_id for label in labels} != set(frozen_labels)
        or {case.request.input_row_id for case in cases}
        != {label.request_id for label in split.test_labels}
    ):
        raise ContractError(
            "final_test",
            "cases must cover every frozen holdout request and label exactly once",
        )
    if len({case.request.input_row_id for case in cases}) != len(cases):
        raise ContractError("final_test", "duplicate request")
    if any(
        label.request_id != case.request.input_row_id
        or label.purpose is not case.request.purpose
        or label.category_id != case.request.category_id
        for case in cases
        for label in case.labels
    ):
        raise ContractError("label", "label belongs to different request/cell")
    if registry and {a.evidence_hash for a in registry.approvals} & {
        label.evidence_hash for label in split.test_labels
    }:
        raise ContractError("registry", "final-test labels already in approval cache")
    label_hash = sha256(
        "".join(
            str(label.to_payload())
            for label in sorted(labels, key=lambda label: label.label_id)
        ).encode()
    ).hexdigest()
    test_hash = sha256(
        json.dumps(
            [label.to_payload() for label in split.test_labels],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    identities = set()
    for label in split.test_labels:
        for kind, value in (
            ("request", label.request_id),
            ("revision", label.revision_group),
            ("family", label.family_id),
            ("candidate", label.candidate_id),
            ("evidence", label.evidence_hash),
        ):
            identities.add(sha256((kind + ":" + value).encode()).hexdigest())
    for case in cases:
        canonical = replace(
            case.request,
            input_row_id="heldout",
            file_path=None,
            file_sha256=None,
            sheet=None,
            row=None,
            raw_fields=(),
        )
        identities.add(
            sha256(
                ("request-content:" + dumps_contract(canonical)).encode()
            ).hexdigest()
        )
        identities.add(
            sha256(("request:" + case.request.input_row_id).encode()).hexdigest()
        )
    frozen_case_hash = case_metadata_hash(split, cases)
    store.record_split(split)
    store.claim_test(
        test_hash, release.content_hash, identities=tuple(sorted(identities))
    )
    limitations: list[str] = []
    if any((case.holdout != 'representative' or case.absence_verified is not None) and not case.evidence_hash for case in cases):
        limitations.append('holdout/absence evidence hashes not supplied')
    if not cases or not labels:
        limitations.append("no independent reviewed business labels")
    if (
        any(label.environment != "production" for label in labels)
        or release.manifest.environment != "production"
    ):
        limitations.append("fixture cannot certify business quality")
    if any(
        label.trust is not Trust.GOLD or label.sampling_origin != "representative"
        for label in labels
    ):
        limitations.append(
            "weak/active-learning labels are not representative evaluation"
        )
    statuses: Counter[str] = Counter()
    groups: dict[tuple[str, str, str, int], list[tuple[bool, bool | None]]] = {}
    incomplete = unknown = positives = negatives = retrieved_gold = total_gold = 0
    durations: list[float] = []
    pair_tp = pair_fp = pair_fn = 0
    started = perf_counter()
    for case in cases:
        tick = perf_counter()
        try:
            result = runner(case.request)
        except (ContractError, OSError, sqlite3.Error):
            statuses["processing_failed"] += 1
            durations.append(perf_counter() - tick)
            continue
        durations.append(perf_counter() - tick)
        if (
            not result.evaluation_only
            or result.release_hash != release.content_hash
            or result.decision.environment != "evaluation"
            or result.decision.input_row_id != case.request.input_row_id
        ):
            raise ContractError(
                "evaluation", "runner must use pinned evaluation-only core"
            )
        if result.decision.status.value.startswith("auto_"):
            raise ContractError("evaluation", "business auto decision forbidden")
        statuses[result.decision.status.value] += 1
        incomplete += int(
            not result.retrieval.decision_evaluation_complete
            or not result.retrieval.scope_scan_complete
        )
        unknown += result.retrieval.unknown_count
        gold = {label.candidate_id for label in case.labels if label.target is True}
        total_gold += len(gold)
        retrieved_gold += len(
            gold & {c.listing_key for c in result.retrieval.candidates[:500]}
        )
        targets = {label.candidate_id: label.target for label in case.labels}
        for check in result.candidate_checks:
            target = targets.get(check.candidate.listing_key)
            predicted = (
                check.suitability_probability is not None
                and check.suitability_probability >= release.threshold
            )
            pair_tp += int(predicted and target is True)
            pair_fp += int(predicted and target is False)
            pair_fn += int(not predicted and target is True)
        selected = result.decision.candidates
        route = (
            "+".join(
                sorted(
                    {
                        check.route
                        for check in result.candidate_checks
                        if check.candidate.listing_key in selected
                    }
                )
            )
            or "none"
        )
        key = (
            case.request.category_id,
            case.request.purpose.value,
            route,
            result.requested_count,
        )
        if result.would_auto_confirm:
            positives += 1
            values = [targets.get(candidate) for candidate in selected]
            correct = (
                False
                if False in values
                else None
                if None in values or not values
                else True
            )
            groups.setdefault(key, []).append((True, correct))
        if result.would_auto_no_match:
            negatives += 1
            correct = False if gold else case.absence_verified
            groups.setdefault(key, []).append((False, correct))
    cells: list[Cell] = []
    for (category, purpose, route, n), outcomes in sorted(groups.items()):
        positive = [truth for pos, truth in outcomes if pos]
        negative = [truth for pos, truth in outcomes if not pos]
        cells.append(
            Cell(
                category=category,
                purpose=purpose,
                route=route,
                top_n=n,
                positive_count=len(positive),
                positive_correct=positive.count(True),
                positive_unresolved=positive.count(None),
                positive_lower=clopper_pearson_lower(
                    positive.count(True), len(positive)
                ),
                no_match_count=len(negative),
                no_match_correct=negative.count(True),
                no_match_unresolved=negative.count(None),
                no_match_lower=clopper_pearson_lower(
                    negative.count(True), len(negative)
                ),
            )
        )
    if (
        not cells
        or not positives
        or any(
            c.positive_unresolved
            or c.positive_lower is None
            or c.positive_lower < 0.995
            for c in cells
            if c.positive_count
        )
    ):
        limitations.append(
            "positive cells lack resolved independent evidence with lower bound >= .995"
        )
    if release.no_match_enabled and (
        not negatives
        or any(
            c.no_match_unresolved
            or c.no_match_lower is None
            or c.no_match_lower < 0.995
            for c in cells
            if c.no_match_count
        )
    ):
        limitations.append("absence declarations lack independent validation")
    # These holdouts require separately identified business datasets; never infer them from fixture rows.
    holdout_counts = Counter(case.holdout for case in cases)
    if not holdout_counts["supplier"] or not holdout_counts["subsequent_time"]:
        limitations.append("supplier and subsequent-time holdouts not supplied")
    total = len(cases)
    return EvaluationReport(
        release_hash=release.content_hash,
        test_hash=test_hash,
        split_hash=split.content_hash,
        label_hash=label_hash,
        registry_hash=release.registry_hash,
        total=total,
        positive_count=positives,
        no_match_count=negatives,
        match_coverage=positives / total if total else None,
        auto_close_rate=(positives + negatives) / total if total else None,
        status_counts=tuple(sorted(statuses.items())),
        incomplete_retrieval_count=incomplete,
        unknown_candidate_count=unknown,
        recall_at_500=retrieved_gold / total_gold if total_gold else None,
        pair_precision=pair_tp / (pair_tp + pair_fp) if pair_tp + pair_fp else None,
        pair_recall=pair_tp / (pair_tp + pair_fn) if pair_tp + pair_fn else None,
        bundle_precision=sum(c.positive_correct for c in cells) / positives
        if positives
        else None,
        latency_p50_seconds=median(durations) if durations else 0,
        latency_p95_seconds=sorted(durations)[
            min(len(durations) - 1, int(len(durations) * 0.95))
        ]
        if durations
        else 0,
        peak_rss_bytes=int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        * (1 if sys.platform == "darwin" else 1024),
        throughput_per_minute=total * 60 / (perf_counter() - started)
        if total
        else None,
        cells=tuple(cells),
        eligibility="INSUFFICIENT_EVALUATION_DATA" if limitations else "eligible",
        limitations=tuple(limitations),
        case_metadata_hash=frozen_case_hash,
        holdouts=(
            ("supplier", str(holdout_counts["supplier"])),
            ("product_family", "split manifest"),
            ("time", str(holdout_counts["subsequent_time"])),
        ),
    )
