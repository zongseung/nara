"""Append-only human resolutions and immutable release-specific suitability registry."""

from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from core.matching.contracts import Candidate, ContractError, InputItem, dumps_contract
from src.matching.releases import Release


class Review(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    review_id: str
    job_id: str
    candidate_id: str
    evidence_hash: str
    target: bool
    reason: str
    reviewed_at: datetime
    expires_at: datetime


class Approval(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    pair_key: str
    evidence_hash: str
    approved_at: datetime
    expires_at: datetime
    reviewer: str


def pair_key(item: InputItem, candidate: Candidate, release: Release) -> str:
    """Price amount and observation time do not change price-independent pair suitability."""
    requested = replace(
        item,
        input_row_id="pair",
        file_path=None,
        file_sha256=None,
        sheet=None,
        row=None,
        raw_fields=(),
        price=replace(item.price, amount=None),
        attributes=tuple((k, replace(v, checked_at=None)) for k, v in item.attributes),
    )
    product = replace(
        candidate,
        snapshot_id=None,
        item_version_id=None,
        group_checked_at=(),
        raw_fields=(),
        price=replace(candidate.price, amount=None),
        attributes=tuple(
            (k, replace(v, checked_at=None)) for k, v in candidate.attributes
        ),
    )
    dependencies = "|".join(
        (
            release.manifest.policy_hash,
            release.model_hash or "rules",
            release.feature_version,
            str(release.threshold),
            release.config.model_dump_json(),
            release.manifest.source_normalizer_version,
            release.manifest.matching_normalizer_version,
        )
    )
    return sha256(
        (dumps_contract(requested) + dumps_contract(product) + dependencies).encode()
    ).hexdigest()


class Registry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    approvals: tuple[Approval, ...] = ()
    schema_version: Literal["matching-registry/1"] = "matching-registry/1"

    @property
    def content_hash(self) -> str:
        return sha256(self.model_dump_json().encode()).hexdigest()

    def permits(self, key: str, now: datetime) -> bool:
        return any(
            a.pair_key == key and a.approved_at <= now < a.expires_at
            for a in self.approvals
        )

    def write(self, path: Path) -> None:
        with path.open("x") as stream:
            stream.write(self.model_dump_json(indent=2))


def read_registry(path: Path, expected_hash: str) -> Registry:
    if path.is_symlink():
        raise ContractError("registry", "symlink forbidden")
    registry = Registry.model_validate_json(path.read_text())
    if registry.content_hash != expected_hash:
        raise ContractError("registry", "hash mismatch")
    return registry
