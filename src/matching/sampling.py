"""Reproducible review selection, never automatic gold-label creation."""

import random
from dataclasses import dataclass

from core.matching.contracts import ContractError
from src.matching.datasets import Label, SplitManifest, freeze_split


@dataclass(frozen=True, slots=True)
class ReviewSample:
    label_ids: tuple[str, ...]
    selections: tuple[tuple[str, str], ...]
    seed: int
    requested_count: int
    missing_count: int
    audit_ids: tuple[str, ...] = ()
    origin: str = "active_learning_train_only"


def sample_review_pool(
    labels: tuple[Label, ...],
    *,
    budget: int,
    seed: int,
    uncertain_ids: frozenset[str] = frozenset(),
    novel_ids: frozenset[str] = frozenset(),
    auto_confirmed_ids: tuple[str, ...] = (),
    split_manifest: SplitManifest | None = None,
) -> ReviewSample:
    if budget < 0:
        raise ContractError("budget")
    rng = random.Random(seed)
    assignments = dict((split_manifest or freeze_split(labels)).assignments)
    available = sorted(
        {row.label_id for row in labels if assignments.get(row.label_id) == "train"}
    )
    selected = []
    reasons = []
    quotas = (
        ("uncertain", budget // 2, uncertain_ids),
        ("novel", budget // 4, novel_ids),
        ("random", budget - budget // 2 - budget // 4, frozenset(available)),
    )
    for reason, quota, pool in quotas:
        choices = [
            value for value in available if value in pool and value not in selected
        ]
        picked = rng.sample(choices, min(quota, len(choices)))
        selected.extend(picked)
        reasons.extend((value, reason) for value in picked)
    remaining = [value for value in available if value not in selected]
    refill = rng.sample(remaining, min(budget - len(selected), len(remaining)))
    selected.extend(refill)
    reasons.extend((value, "random_refill") for value in refill)
    audit_pool = sorted(set(auto_confirmed_ids))
    # A nonempty population gets a reproducible audit; ceiling preserves coverage
    # for populations below one hundred without calling zero samples an audit.
    audit_count = min(50, (len(audit_pool) + 99) // 100)
    audits = tuple(rng.sample(audit_pool, audit_count))
    return ReviewSample(
        tuple(selected), tuple(reasons), seed, budget, budget - len(selected), audits
    )
