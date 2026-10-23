"""Versioned, explicit purpose/category policy registry."""

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
from typing import TypedDict

from core.matching.contracts import AttributeValue, ContractError, Purpose


class RulePayload(TypedDict):
    field: str
    operator: str
    unit: str
    required: bool
    tolerance: str
    value: str | list[str] | None


class PolicyPayload(TypedDict):
    policy_id: str
    version: str
    purpose: str
    category_ids: list[str]
    item_kind: str
    state: str
    freshness_hours: str
    required_groups: list[str]
    rules: list[RulePayload]
    required_certificates: list[str]
    allow_rotation: bool


class Operator(str, Enum):
    EQ = "eq"
    MIN = "min"
    MAX = "max"
    RANGE = "range"
    SET = "set"


@dataclass(frozen=True, slots=True)
class AttributeRule:
    field: str
    operator: Operator
    unit: str
    value: AttributeValue
    tolerance: Decimal
    required: bool


@dataclass(frozen=True, slots=True)
class Policy:
    policy_id: str
    version: str
    purpose: Purpose
    category_ids: tuple[str, ...]
    item_kind: str
    state: str
    freshness_hours: Decimal
    required_groups: tuple[str, ...]
    rules: tuple[AttributeRule, ...]
    required_certificates: tuple[str, ...]
    allow_rotation: bool
    content_hash: str

    @property
    def auto_eligible(self) -> bool:
        """Activation additionally requires a category release certificate in the engine."""
        return self.state == "active"


def load_policy(data: PolicyPayload, *, expected_hash: str | None = None) -> Policy:
    try:
        if set(data) != set(PolicyPayload.__annotations__):
            raise ContractError("policy", "missing or unsupported fields")
        if not data["policy_id"] or not data["version"]:
            raise ContractError("policy_id/version")
        digest = sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if expected_hash is not None and digest != expected_hash:
            raise ContractError("policy_hash", "content mismatch")
        if data["state"] not in ("draft", "shadow", "active"):
            raise ContractError("state")
        if not data["category_ids"] or not all(
            isinstance(c, str) and c for c in data["category_ids"]
        ):
            raise ContractError("category_ids")
        if (
            data["item_kind"] not in ("main", "accessory")
            or type(data["allow_rotation"]) is not bool
        ):
            raise ContractError("item_kind/allow_rotation")
        freshness = Decimal(data["freshness_hours"])
        if not freshness.is_finite() or freshness <= 0 or not data["required_groups"]:
            raise ContractError("freshness_hours/required_groups")
        rules = []
        for raw in data["rules"]:
            if set(raw) != set(RulePayload.__annotations__):
                raise ContractError("rule", "missing or unsupported fields")
            if raw["unit"] not in (
                "text",
                "mm",
                "L",
                "kg",
                "g",
                "W",
                "V",
                "A",
                "m2",
                "m3",
                "count",
            ):
                raise ContractError("unit", "unsupported canonical unit")
            operator = Operator(raw["operator"])
            if raw["required"] is not True or not raw["field"] or not raw["unit"]:
                raise ContractError("rule", "explicit required field and unit needed")
            tolerance = Decimal(raw["tolerance"])
            if not tolerance.is_finite() or tolerance < 0:
                raise ContractError("tolerance")
            value = raw["value"]
            if raw["unit"] != "text":
                value = (
                    tuple(Decimal(v) for v in value)
                    if isinstance(value, list)
                    else Decimal(value)
                    if value is not None
                    else None
                )
                numbers = (
                    value
                    if isinstance(value, tuple)
                    else (value,)
                    if value is not None
                    else ()
                )
                if any(not v.is_finite() for v in numbers):
                    raise ContractError("value")
            elif isinstance(value, list):
                value = tuple(value)
            if operator is Operator.RANGE and (
                not isinstance(value, tuple)
                or len(value) != 2
                or not isinstance(value[0], Decimal)
                or not isinstance(value[1], Decimal)
                or value[0] > value[1]
            ):
                raise ContractError("range", "ordered two-bound range required")
            if operator is Operator.SET and (not isinstance(value, tuple) or not value):
                raise ContractError("set", "nonempty values required")
            if operator in (Operator.MIN, Operator.MAX) and not isinstance(
                value, Decimal
            ):
                raise ContractError("value", "numeric threshold required")
            if raw["unit"] == "text" and (
                tolerance != 0
                or operator in (Operator.MIN, Operator.MAX, Operator.RANGE)
            ):
                raise ContractError(
                    "operator", "text supports exact or set comparisons"
                )
            rules.append(
                AttributeRule(
                    raw["field"], operator, raw["unit"], value, tolerance, True
                )
            )
        if len({r.field for r in rules}) != len(rules):
            raise ContractError("rules", "duplicate field")
        return Policy(
            data["policy_id"],
            data["version"],
            Purpose(data["purpose"]),
            tuple(data["category_ids"]),
            data["item_kind"],
            data["state"],
            freshness,
            tuple(data["required_groups"]),
            tuple(rules),
            tuple(data["required_certificates"]),
            data["allow_rotation"],
            digest,
        )
    except (KeyError, TypeError, InvalidOperation, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("policy", str(exc)) from exc


@dataclass(frozen=True, slots=True)
class PolicyRegistry:
    policies: tuple[Policy, ...]

    def __post_init__(self) -> None:
        seen = set()
        for policy in self.policies:
            for category in policy.category_ids:
                key = (policy.purpose, category)
                if key in seen:
                    raise ContractError(
                        "policy_registry",
                        "duplicate purpose/category; select version explicitly",
                    )
                seen.add(key)

    def get(self, purpose: Purpose, category_id: str) -> Policy | None:
        return next(
            (
                p
                for p in self.policies
                if p.purpose is purpose and category_id in p.category_ids
            ),
            None,
        )
