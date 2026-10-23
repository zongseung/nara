"""Reviewed labels and immutable group/time split manifests.

Synthetic labels exercise software only and are never business certification.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import TypedDict

from core.matching.contracts import ContractError, Purpose


class Trust(str, Enum):
    GOLD = "gold"
    WEAK = "weak"


class LabelPayload(TypedDict):
    label_id: str
    request_id: str
    candidate_id: str
    revision_group: str
    family_id: str
    seller_id: str
    purpose: str
    category_id: str
    target: bool | None
    reason: str
    evidence_hash: str
    reviewer: str
    reviewed_at: str
    trust: str
    provenance: str
    sampling_origin: str
    environment: str


@dataclass(frozen=True, slots=True)
class Label:
    label_id: str
    request_id: str
    candidate_id: str
    revision_group: str
    family_id: str
    seller_id: str
    purpose: Purpose
    category_id: str
    target: bool | None
    reason: str
    evidence_hash: str
    reviewer: str
    reviewed_at: datetime
    trust: Trust
    provenance: str
    sampling_origin: str
    environment: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.label_id,
                self.request_id,
                self.candidate_id,
                self.revision_group,
                self.family_id,
                self.seller_id,
                self.category_id,
                self.reason,
                self.reviewer,
            )
        ):
            raise ContractError(
                "label", "all grouping and review evidence fields required"
            )
        if not isinstance(self.purpose, Purpose) or not isinstance(self.trust, Trust):
            raise ContractError("label_enum")
        if self.target is not None and type(self.target) is not bool:
            raise ContractError("target")
        if not re.fullmatch("[0-9a-f]{64}", self.evidence_hash):
            raise ContractError("evidence_hash")
        if self.reviewed_at.tzinfo is None:
            raise ContractError("reviewed_at", "timezone required")
        if self.trust is Trust.GOLD and self.provenance != "human_review":
            raise ContractError("trust", "gold requires human review provenance")
        if self.sampling_origin not in (
            "representative",
            "active_learning",
        ) or self.environment not in ("fixture", "production"):
            raise ContractError("label_origin")

    def to_payload(self) -> LabelPayload:
        return LabelPayload(
            label_id=self.label_id,
            request_id=self.request_id,
            candidate_id=self.candidate_id,
            revision_group=self.revision_group,
            family_id=self.family_id,
            seller_id=self.seller_id,
            purpose=self.purpose.value,
            category_id=self.category_id,
            target=self.target,
            reason=self.reason,
            evidence_hash=self.evidence_hash,
            reviewer=self.reviewer,
            reviewed_at=self.reviewed_at.isoformat(),
            trust=self.trust.value,
            provenance=self.provenance,
            sampling_origin=self.sampling_origin,
            environment=self.environment,
        )


def import_labels(
    path: str | Path, *, trusted_reviewers: frozenset[str]
) -> tuple[Label, ...]:
    """Read a protected caller-selected manifest; reviewer allowlist is external to it."""
    labels = []
    with Path(path).open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                data = json.loads(line)
                if data["reviewer"] not in trusted_reviewers:
                    raise ContractError("reviewer", "not trusted")
                data["purpose"] = Purpose(data["purpose"])
                data["trust"] = Trust(data["trust"])
                data["reviewed_at"] = datetime.fromisoformat(data["reviewed_at"])
                labels.append(Label(**data))
            except (ValueError, TypeError, KeyError) as exc:
                raise ContractError(f"label line {line_number}", str(exc)) from exc
    _validate(tuple(labels))
    return tuple(labels)


def _validate(labels: tuple[Label, ...]) -> None:
    seen_ids = set()
    targets = {}
    for row in labels:
        if row.label_id in seen_ids:
            raise ContractError("label_id", "duplicate")
        seen_ids.add(row.label_id)
        for key in (
            ("pair", row.request_id, row.candidate_id, row.purpose.value),
            ("evidence", row.evidence_hash, row.purpose.value),
        ):
            if (
                key in targets
                and targets[key] is not None
                and row.target is not None
                and targets[key] != row.target
            ):
                raise ContractError(
                    "label", "conflicting labels for same pair/evidence"
                )
            if row.target is not None or key not in targets:
                targets[key] = row.target


def binary_examples(labels: tuple[Label, ...]) -> tuple[Label, ...]:
    return tuple(row for row in labels if row.target is not None)


@dataclass(frozen=True, slots=True)
class SplitManifest:
    assignments: tuple[tuple[str, str], ...]
    content_hash: str
    label_availability: str
    total_count: int
    unresolved_count: int
    distribution: tuple[tuple[str, str, int, int], ...]
    requested_ratios: tuple[int, int, int, int] = (50, 15, 15, 20)
    limitations: tuple[str, ...] = ()
    seller_holdout: tuple[str, ...] = ()
    test_labels: tuple[Label, ...] = ()

    def write(self, path: str | Path) -> None:
        """Exclusive creation prevents silently replacing a frozen split."""
        with Path(path).open("x", encoding="utf-8") as stream:
            json.dump(
                {
                    "assignments": self.assignments,
                    "content_hash": self.content_hash,
                    "label_availability": self.label_availability,
                    "total_count": self.total_count,
                    "unresolved_count": self.unresolved_count,
                    "distribution": self.distribution,
                    "requested_ratios": self.requested_ratios,
                    "limitations": self.limitations,
                    "seller_holdout": self.seller_holdout,
                    "test_labels": [label.to_payload() for label in self.test_labels],
                },
                stream,
                ensure_ascii=False,
                indent=2,
            )


def freeze_split(
    labels: tuple[Label, ...], *, seller_holdout: frozenset[str] = frozenset()
) -> SplitManifest:
    """Connect request/revision/family/content duplicates, then hold out later groups.

    Groups straddling time boundaries are moved wholly later; their early rows
    are marked excluded_temporal. Weak/AL groups stay train-only, and their later
    rows are excluded_temporal. This avoids reporting a false temporal holdout.
    """
    _validate(labels)
    rows = sorted(labels, key=lambda r: (r.reviewed_at, r.label_id))
    parents = list(range(len(rows)))

    def root(i: int) -> int:
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    keys = {}
    for i, row in enumerate(rows):
        for key in (
            ("request", row.request_id),
            ("revision", row.revision_group),
            ("family", row.family_id),
            ("evidence", row.evidence_hash),
            ("candidate", row.candidate_id),
        ):
            if key in keys:
                parents[root(i)] = root(keys[key])
            else:
                keys[key] = i
    groups = {}
    for i, row in enumerate(rows):
        groups.setdefault(root(i), []).append(row)
    ordered = sorted(
        groups.values(),
        key=lambda g: (max(r.reviewed_at for r in g), min(r.label_id for r in g)),
    )
    eligible = [
        group
        for group in ordered
        if not any(
            r.trust is Trust.WEAK or r.sampling_origin == "active_learning"
            for r in group
        )
    ]
    count = len(eligible)
    split_names = ("train", "calibration", "validation", "test")
    boundaries = (int(count * 0.50), int(count * 0.65), int(count * 0.80))
    assignments = {}
    group_splits = {}
    for index, group in enumerate(eligible):
        split = split_names[sum(index >= b for b in boundaries)]
        group_splits[id(group)] = split
    cutoffs = {}
    for split in split_names[1:]:
        later = [
            max(r.reviewed_at for r in group)
            for group in eligible
            if split_names.index(group_splits[id(group)]) >= split_names.index(split)
        ]
        if later:
            cutoffs[split] = min(later)
    for group in ordered:
        train_only = any(
            r.trust is Trust.WEAK or r.sampling_origin == "active_learning"
            for r in group
        )
        held = any(r.seller_id in seller_holdout for r in group)
        if train_only and held:
            raise ContractError(
                "seller_holdout", "weak/AL component cannot enter holdout"
            )
        split = "train" if train_only else "test" if held else group_splits[id(group)]
        for row in group:
            assignments[row.label_id] = split
    # Exclude entire components that cross a temporal boundary rather than leak
    # related requests or relabel individual revisions into different partitions.
    for group in ordered:
        split = assignments[group[0].label_id]
        lower = cutoffs.get(split)
        next_index = split_names.index(split) + 1
        upper = (
            cutoffs.get(split_names[next_index])
            if next_index < len(split_names)
            else None
        )
        crosses = any(
            (lower is not None and r.reviewed_at < lower)
            or (upper is not None and r.reviewed_at >= upper)
            for r in group
        )
        if crosses:
            for row in group:
                assignments[row.label_id] = "excluded_temporal"
    distribution = []
    for purpose, category in sorted({(r.purpose.value, r.category_id) for r in rows}):
        subset = [
            r for r in rows if r.purpose.value == purpose and r.category_id == category
        ]
        distribution.append(
            (
                purpose,
                category,
                sum(r.target is not None for r in subset),
                sum(r.target is None for r in subset),
            )
        )
    frozen = tuple(sorted(assignments.items()))
    digest = sha256(
        json.dumps(
            {
                "labels": [
                    r.to_payload() for r in sorted(rows, key=lambda r: r.label_id)
                ],
                "assignments": frozen,
                "seller_holdout": sorted(seller_holdout),
                "ratios": [50, 15, 15, 20],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    limitations = [
        "group/time constraints may prevent target ratios; inspect actual assignments",
        "active-learning selection is biased; evaluation requires representative requests",
    ]
    if not rows:
        limitations.append("no reviewed business labels available")
    if any(r.environment == "fixture" for r in rows):
        limitations.append("synthetic fixture labels cannot certify production")
    return SplitManifest(
        frozen,
        digest,
        "present" if rows else "absent",
        len(rows),
        sum(r.target is None for r in rows),
        tuple(distribution),
        limitations=tuple(limitations),
        seller_holdout=tuple(sorted(seller_holdout)),
        test_labels=tuple(
            r
            for r in sorted(rows, key=lambda r: r.label_id)
            if assignments[r.label_id] == "test"
        ),
    )
