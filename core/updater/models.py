"""Business observations; transport and profile types live in the shared source modules."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from core.catalog_sources import RawRow, SourceProfile


type FieldState = Literal['present', 'missing', 'null']
type GroupStatus = Literal['observed', 'not_checked', 'unsupported', 'unresolved']


@dataclass(frozen=True, slots=True)
class GroupObservation:
    name: str
    supported: bool
    status: GroupStatus
    checked_at: datetime | None


@dataclass(frozen=True, slots=True)
class ItemObservation:
    listing_key: str
    goods_id: str | None
    seller_name: str | None
    manufacturer_name: str | None
    raw_names: tuple[tuple[str, str | None], ...]
    display_name: str | None
    name_status: Literal['resolved', 'unknown', 'unresolved']
    spec_raw: str | None
    contract_price_decimal: Decimal
    unit: str | None
    category_id: str | None
    parent_listing_key: str | None
    contract_number: str | None
    contract_start: str | None
    contract_end: str | None
    profile: SourceProfile
    observed_at: datetime
    raw: RawRow
    field_states: tuple[tuple[str, FieldState], ...]
    groups: tuple[GroupObservation, ...]
    seller_id: str | None = None
    manufacturer_id: str | None = None
    currency: str | None = None
    tax_basis: str | None = None
    pack_quantity: Decimal | None = None
    source_modified_at: datetime | None = None
    price_kind: Literal['contract_unit_price'] = 'contract_unit_price'
    presence_status: Literal['observed'] = 'observed'
