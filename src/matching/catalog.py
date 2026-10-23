"""Matching projection of a live, pinned updater publication."""

import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from core.catalog_sources import JsonValue, get_profile
from core.matching.contracts import (
    AttributeEvidence,
    Candidate,
    ContractError,
    EvidenceState,
    PriceBasis,
)
from core.matching.normalization import normalize_dimensions, normalize_material
from src.updater.reader import Manifest, PublishedSnapshot, open_published_snapshot
from src.updater.store import StoreError


class CatalogError(ContractError):
    """A source or derived-index boundary was not safe to consume."""


class CatalogProvider(Protocol):
    @property
    def manifest(self) -> Manifest: ...
    def candidates(self) -> Iterator[Candidate]: ...
    def require_open(self) -> None: ...


def _text(row: Mapping[str, JsonValue], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CatalogError(key, "expected nullable source text")
    return value or None


def _decimal(row: Mapping[str, JsonValue], key: str) -> Decimal | None:
    value = row.get(key)
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise CatalogError(key, "invalid decimal") from exc
    if not number.is_finite():
        raise CatalogError(key, "nonfinite decimal")
    return number if number > 0 else None


def _expiry(raw: str | None) -> date | None:
    if raw is None:
        return None
    try:
        return (
            datetime.strptime(raw, "%Y%m%d").date()
            if len(raw) == 8
            else date.fromisoformat(raw)
        )
    except ValueError:
        return None


def _project(row: Mapping[str, JsonValue], manifest: Manifest) -> Candidate:
    groups = row.get("information_groups")
    checked = []
    if not isinstance(groups, dict):
        raise CatalogError("information_groups", "missing source group evidence")
    for name, group in groups.items():
        if not isinstance(group, dict):
            raise CatalogError("information_groups", "invalid group evidence")
        stamp = None
        raw = group.get("checked_at")
        if (
            row.get("presence_status") == "observed"
            and group.get("status") == "observed"
            and group.get("supported") is True
            and isinstance(raw, str)
        ):
            try:
                stamp = datetime.fromisoformat(raw)
            except ValueError as exc:
                raise CatalogError(name, "invalid timestamp") from exc
            if stamp.tzinfo is None:
                stamp = None
        checked.append((name, stamp))
    names = row.get("raw_names")
    if not isinstance(names, dict):
        raise CatalogError("raw_names", "source alias evidence missing")
    raw_fields = []
    for name, value in names.items():
        if value is not None and not isinstance(value, str):
            raise CatalogError(name, "invalid alias value")
        raw_fields.append((name, value))
    for name in (
        "presence_status",
        "contract_end",
        "contract_price_decimal",
        "name_status",
    ):
        raw_fields.append((name, _text(row, name)))
    spec = _text(row, "spec_raw") or ""
    identity_known = (
        row.get("name_status") == "resolved"
        and dict(checked).get("identity") is not None
    )
    name_evidence = AttributeEvidence(
        _text(row, "display_name") if identity_known else None,
        "text",
        "raw_names",
        str(names),
        EvidenceState.KNOWN
        if identity_known
        else EvidenceState.CONFLICTING
        if row.get("name_status") == "unresolved"
        else EvidenceState.MISSING,
    )
    return Candidate(
        listing_key=_text(row, "listing_key") or "",
        category_id=_text(row, "category_id"),
        name_raw=_text(row, "display_name") or "",
        spec_raw=spec,
        attributes=(
            ("dimensions", normalize_dimensions(spec, "spec_raw")),
            ("material", normalize_material("", "material")),
            ("identity_name", name_evidence),
        ),
        price=PriceBasis(
            _decimal(row, "contract_price_decimal"),
            _text(row, "price_kind"),
            _text(row, "currency"),
            _text(row, "tax_basis"),
            _text(row, "unit"),
            _decimal(row, "pack_quantity"),
        ),
        goods_id=_text(row, "goods_id"),
        goods_id_verified=identity_known,
        seller_id=_text(row, "seller_id"),
        seller_name=_text(row, "seller_name"),
        manufacturer_id=_text(row, "manufacturer_id"),
        manufacturer_name=_text(row, "manufacturer_name"),
        contract_until=_expiry(_text(row, "contract_end")),
        group_checked_at=tuple(checked),
        snapshot_id=manifest.snapshot_id,
        item_version_id=_text(row, "item_version_id"),
        source_normalizer_version=manifest.source_normalizer_version,
        environment=manifest.environment,
        raw_fields=tuple(raw_fields),
    )


@dataclass(frozen=True, slots=True)
class PublishedCatalog:
    reader: PublishedSnapshot

    @property
    def manifest(self) -> Manifest:
        return self.reader.manifest

    def require_open(self) -> None:
        try:
            self.reader.connection.execute("SELECT 1")
        except sqlite3.Error as exc:
            raise CatalogError("source_pin", "catalog context has closed") from exc

    def candidates(self) -> Iterator[Candidate]:
        self.require_open()
        for row in self.reader.current():
            yield _project(row, self.manifest)


@contextmanager
def open_catalog(
    manifest: Manifest | Path | str, *, root: Path, production: bool = False
) -> Iterator[PublishedCatalog]:
    """Own the public reader context for the full index build or matching job."""
    try:
        parsed = (
            manifest
            if isinstance(manifest, Manifest)
            else Manifest.model_validate_json(Path(manifest).read_text())
        )
        expected = get_profile("shopping-all")
        if (
            parsed.source_id,
            parsed.scope,
            parsed.profile_hash,
            parsed.source_schema_version,
            parsed.source_normalizer_version,
        ) != (
            expected.source_id,
            expected.scope,
            expected.profile_hash,
            expected.source_schema_version,
            expected.source_normalizer_version,
        ):
            raise CatalogError("source", "unsupported source/profile/normalizer")
        if not parsed.coverage_complete or parsed.unresolved_count != 0:
            raise CatalogError("coverage", "unreconciled publication")
        if production and parsed.environment != "production":
            raise CatalogError("environment", "fixture cannot enter production")
        with open_published_snapshot(
            parsed, root=root, require_active=production
        ) as reader:
            yield PublishedCatalog(reader)
    except (StoreError, ValidationError, OSError, sqlite3.Error) as exc:
        raise CatalogError("catalog", str(exc)) from exc
