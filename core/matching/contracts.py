"""Immutable matching-domain contracts, independent of source transport DTOs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Annotated, ClassVar, Final, TypeAlias, TypedDict

from pydantic import BeforeValidator, ConfigDict, PlainSerializer, TypeAdapter

SCHEMA_VERSION: Final = "matching/1"
NORMALIZER_VERSION: Final = "matching-attributes/1"


class DecimalPayload(TypedDict):
    decimal: str


AttributeValue: TypeAlias = str | Decimal | tuple[Decimal, ...] | tuple[str, ...] | None
AttributeJSON: TypeAlias = str | DecimalPayload | list[DecimalPayload | str] | None


def _read_attribute(value: AttributeJSON | AttributeValue) -> AttributeValue:
    if isinstance(value, dict):
        if set(value) != {"decimal"}:
            raise ValueError("tagged decimal required")
        try:
            number = Decimal(value["decimal"])
        except InvalidOperation as exc:
            raise ValueError("invalid decimal") from exc
        if not number.is_finite():
            raise ValueError("finite decimal required")
        return number
    if isinstance(value, list):
        if all(isinstance(v, str) for v in value):
            return tuple(str(v) for v in value)
        numbers = []
        for part in value:
            number = _read_attribute(part)
            if not isinstance(number, Decimal):
                raise ContractError("attribute", "homogeneous decimal tuple required")
            numbers.append(number)
        return tuple(numbers)
    return value


def _write_attribute(value: AttributeValue) -> AttributeJSON:
    if isinstance(value, Decimal):
        return DecimalPayload(decimal=str(value))
    if isinstance(value, tuple):
        return [
            DecimalPayload(decimal=str(v)) if isinstance(v, Decimal) else v
            for v in value
        ]
    return value


WireAttribute: TypeAlias = Annotated[
    AttributeValue,
    BeforeValidator(_read_attribute, json_schema_input_type=AttributeJSON),
    PlainSerializer(_write_attribute),
]


class ContractError(ValueError):
    def __init__(self, field: str, reason: str = "invalid or missing value") -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


class Purpose(str, Enum):
    EXACT_IDENTITY = "exact_identity"
    SAME_PRICE_COMPETITOR = "same_price_competitor"
    FUNCTIONAL_SUBSTITUTE = "functional_substitute"


class EvidenceState(str, Enum):
    KNOWN = "known"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"


class ConstraintStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class DecisionStatus(str, Enum):
    AUTO_CONFIRMED = "auto_confirmed"
    AUTO_NO_MATCH = "auto_no_match"
    REVIEW_REQUIRED = "review_required"
    INVALID_INPUT = "invalid_input"
    PROCESSING_FAILED = "processing_failed"


class ReasonCode(str, Enum):
    MISSING_REQUIRED_ATTRIBUTE = "MISSING_REQUIRED_ATTRIBUTE"
    CONFLICTING_ATTRIBUTE = "CONFLICTING_ATTRIBUTE"
    UNSUPPORTED_CATEGORY = "UNSUPPORTED_CATEGORY"
    INCOMPLETE_RETRIEVAL = "INCOMPLETE_RETRIEVAL"
    STALE_EVIDENCE = "STALE_EVIDENCE"
    RELEASE_INELIGIBLE = "RELEASE_INELIGIBLE"
    MISMATCH = "MISMATCH"
    MATCH = "MATCH"
    UNKNOWN_PRICE_BASIS = "UNKNOWN_PRICE_BASIS"
    EXCLUDED_SELLER = "EXCLUDED_SELLER"
    EXPIRED_CONTRACT = "EXPIRED_CONTRACT"


@dataclass(frozen=True, slots=True)
class AttributeEvidence:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    value: WireAttribute
    unit: str | None
    source_field: str
    raw: str
    status: EvidenceState
    normalizer_version: str = NORMALIZER_VERSION
    source_span: tuple[int, int] | None = None
    checked_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, EvidenceState):
            raise ContractError("status")
        if (self.status is EvidenceState.KNOWN) != (self.value is not None):
            raise ContractError(
                "value", "only known evidence may carry a normalized value"
            )
        if self.value is not None and not isinstance(self.value, (str, Decimal, tuple)):
            raise ContractError("value", "unsupported attribute shape")
        if isinstance(self.value, tuple) and (
            not self.value
            or not (
                all(isinstance(v, Decimal) for v in self.value)
                or all(isinstance(v, str) for v in self.value)
            )
        ):
            raise ContractError("value", "homogeneous nonempty scalar tuple required")
        values = self.value if isinstance(self.value, tuple) else (self.value,)
        if any(isinstance(v, Decimal) and not v.is_finite() for v in values):
            raise ContractError("value", "finite decimals required")
        if self.checked_at is not None and self.checked_at.tzinfo is None:
            raise ContractError("checked_at", "timezone required")

    def to_json(self) -> str:
        match self.value:
            case Decimal() as number:
                value, kind = str(number), "decimal"
            case tuple() as values:
                value = [str(v) for v in values]
                kind = (
                    "decimals"
                    if all(isinstance(v, Decimal) for v in values)
                    else "strings"
                )
            case str() | None:
                value, kind = self.value, "string"
        return json.dumps(
            {
                "schema": SCHEMA_VERSION,
                "value": value,
                "kind": kind,
                "unit": self.unit,
                "source_field": self.source_field,
                "raw": self.raw,
                "status": self.status.value,
                "normalizer_version": self.normalizer_version,
                "source_span": self.source_span,
                "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> AttributeEvidence:
        try:
            data = json.loads(raw)
            if data["schema"] != SCHEMA_VERSION:
                raise ContractError("schema")
            value = data["value"]
            match data["kind"]:
                case "decimal":
                    value = Decimal(value)
                case "decimals":
                    value = tuple(Decimal(v) for v in value)
                case "strings":
                    value = tuple(value)
                case "string":
                    if value is not None and not isinstance(value, str):
                        raise ContractError("value")
                case _:
                    raise ContractError("kind")
            return cls(
                value,
                data["unit"],
                data["source_field"],
                data["raw"],
                EvidenceState(data["status"]),
                data["normalizer_version"],
                tuple(data["source_span"]) if data["source_span"] else None,
                datetime.fromisoformat(data["checked_at"])
                if data["checked_at"]
                else None,
            )
        except (KeyError, TypeError, InvalidOperation, json.JSONDecodeError) as exc:
            raise ContractError("attribute", str(exc)) from exc


@dataclass(frozen=True, slots=True)
class PriceBasis:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    amount: Decimal | None = None
    kind: str | None = None
    currency: str | None = None
    tax_basis: str | None = None
    unit: str | None = None
    pack_quantity: Decimal | None = None

    def __post_init__(self) -> None:
        for name, number in (
            ("amount", self.amount),
            ("pack_quantity", self.pack_quantity),
        ):
            if number is not None and (
                not isinstance(number, Decimal) or not number.is_finite() or number <= 0
            ):
                raise ContractError(name, "positive finite Decimal required")


@dataclass(frozen=True, slots=True)
class InputItem:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    input_row_id: str
    purpose: Purpose
    category_id: str
    name_raw: str
    spec_raw: str
    attributes: tuple[tuple[str, AttributeEvidence], ...] = ()
    price: PriceBasis = field(default_factory=PriceBasis)
    goods_id: str | None = None
    goods_id_verified: bool = False
    seller_id: str | None = None
    seller_name: str | None = None
    manufacturer_id: str | None = None
    manufacturer_name: str | None = None
    model: str | None = None
    options: str | None = None
    item_kind: str | None = None
    file_path: str | None = None
    file_sha256: str | None = None
    sheet: str | None = None
    row: int | None = None
    raw_fields: tuple[tuple[str, str], ...] = ()
    excluded_seller_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not self.input_row_id
            or not isinstance(self.purpose, Purpose)
            or not self.name_raw
        ):
            raise ContractError(
                "input", "row ID, explicit purpose and raw name required"
            )
        if len(dict(self.attributes)) != len(self.attributes):
            raise ContractError("attributes", "duplicate fields")


@dataclass(frozen=True, slots=True)
class Candidate:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    listing_key: str
    category_id: str | None
    name_raw: str
    spec_raw: str = ""
    attributes: tuple[tuple[str, AttributeEvidence], ...] = ()
    price: PriceBasis = field(default_factory=PriceBasis)
    goods_id: str | None = None
    goods_id_verified: bool = False
    seller_id: str | None = None
    seller_name: str | None = None
    manufacturer_id: str | None = None
    manufacturer_name: str | None = None
    model: str | None = None
    options: str | None = None
    item_kind: str | None = None
    contract_until: date | None = None
    latest_contract: bool | None = None
    group_checked_at: tuple[tuple[str, datetime | None], ...] = ()
    certificates: tuple[tuple[str, date | None], ...] = ()
    snapshot_id: str | None = None
    item_version_id: str | None = None
    source_normalizer_version: str | None = None
    environment: str = "unverified"
    raw_fields: tuple[tuple[str, str | None], ...] = ()


@dataclass(frozen=True, slots=True)
class ConstraintResult:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    field: str
    status: ConstraintStatus
    reason: ReasonCode
    expected: WireAttribute = None
    actual: WireAttribute = None


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    candidates: tuple[Candidate, ...]
    scope_definition: str
    total_count: int | None
    scanned_count: int
    unknown_count: int
    scope_scan_complete: bool
    decision_evaluation_complete: bool
    truncated: bool
    termination_reason: str
    snapshot_id: str
    index_generation: str


@dataclass(frozen=True, slots=True)
class MatchDecision:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    input_row_id: str
    status: DecisionStatus
    reasons: tuple[ReasonCode, ...]
    candidates: tuple[str, ...] = ()
    constraints: tuple[ConstraintResult, ...] = ()
    snapshot_id: str | None = None
    release_id: str | None = None
    environment: str = "shadow"


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    release_id: str
    purpose: Purpose
    category_id: str
    policy_hash: str
    dataset_hash: str
    source_normalizer_version: str
    matching_normalizer_version: str
    environment: str = "shadow"
    schema_version: str = SCHEMA_VERSION


Contract: TypeAlias = (
    InputItem
    | Candidate
    | ConstraintResult
    | RetrievalResult
    | MatchDecision
    | ReleaseManifest
    | AttributeEvidence
    | PriceBasis
)


def dumps_contract(value: Contract) -> str:
    """Serialize Decimal/null, enums and timezone-aware evidence without coercion."""
    adapter: TypeAdapter[Contract] = TypeAdapter(type(value))
    return json.dumps(
        {
            "schema": SCHEMA_VERSION,
            "type": type(value).__name__,
            "payload": json.loads(adapter.dump_json(value)),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def loads_contract(raw: str) -> Contract:
    """Strict schema-dispatched boundary; unsupported fields and enums are rejected."""
    classes = {
        cls.__name__: cls
        for cls in (
            InputItem,
            Candidate,
            ConstraintResult,
            RetrievalResult,
            MatchDecision,
            ReleaseManifest,
            AttributeEvidence,
            PriceBasis,
        )
    }
    try:
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or set(envelope) != {
            "schema",
            "type",
            "payload",
        }:
            raise ContractError("schema")
        if envelope["schema"] != SCHEMA_VERSION or envelope["type"] not in classes:
            raise ContractError("schema")
        adapter: TypeAdapter[Contract] = TypeAdapter(classes[envelope["type"]])
        return adapter.validate_json(json.dumps(envelope["payload"]), strict=True)
    except (ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("contract", str(exc)) from exc
