"""Shopping business policy on the shared, independently useful raw transport."""
from dataclasses import dataclass
from typing import Protocol

import requests

from core.catalog_sources import RawRow, SearchQuery, SourceError, SourceProfile, entity_key, get_profile
from src.transport import RequestLimiter
from src.g2b_client import SearchPage, fetch_search_page
from core.updater.models import FieldState, GroupObservation, GroupStatus, ItemObservation

# No captured field has a verified deletion marker or complete certification semantics.
# Missing and null remain distinct evidence; neither authorizes clearing stored data.
FIELD_GROUPS = (
    ('identity', ('ctrtItemMngNo', 'itemIdnfNo', 'rprsCtrtItemMngNo', 'itemIdnfNm', 'itemIndfNmView', 'itemClsfNo')),
    ('price', ('ctrtUprc', 'ctrtUntVal')),
    ('parties', ('ctentUntyGrpNm', 'mnftrEtpsNm')),
    ('specification', ('pdctAtrbCdDtlNm',)),
    ('contract', ('ctrtNo', 'ctrtBgngYmd', 'ctrtEndYmd')),
)
EXPLICIT_DELETION_FIELDS: tuple[str, ...] = ()


class ItemSource(Protocol):
    def fetch(self, query: SearchQuery) -> SearchPage: ...


@dataclass(frozen=True, slots=True)
class ShoppingSource:
    session: requests.Session | None = None
    timeout: float | tuple[float, float] = (10, 35)
    limiter: RequestLimiter | None = None

    def fetch(self, query: SearchQuery) -> SearchPage:
        return fetch_search_page(query, get_profile('shopping-all'), session=self.session, timeout=self.timeout, limiter=self.limiter)


def _text(row: RawRow, field: str) -> str | None:
    value = row.get(field)
    if value is not None and not isinstance(value, str):
        raise SourceError('invalid_field_type', field)
    return value


def observations_from_page(page: SearchPage) -> tuple[ItemObservation, ...]:
    page.profile.require_collection_ready()
    if page.profile.scope != 'shopping-all' or page.profile.source_id != 'shopping':
        raise SourceError('unsupported_scope', page.profile.scope)
    if page.errors:
        raise page.errors[0]
    observations = []
    for source_row in page.rows:
        row = source_row.raw
        key = entity_key(page.profile, row)
        if source_row.contract_price_decimal is None:
            raise SourceError('required_price', 'ctrtUprc')
        fields: list[tuple[str, FieldState]] = []
        for _, names in FIELD_GROUPS:
            for field in names:
                if field != 'ctrtUprc':
                    _text(row, field)
                state: FieldState = 'missing' if field not in row else 'null' if row[field] is None else 'present'
                fields.append((field, state))
        raw_names = tuple((field, _text(row, field)) for field in page.profile.name_aliases if field in row)
        names = {value for _, value in raw_names if value}
        name_status = 'unknown' if not names else 'resolved' if len(names) == 1 else 'unresolved'
        groups = []
        for group, group_fields in FIELD_GROUPS:
            status: GroupStatus = 'observed' if all(row.get(field) is not None for field in group_fields) else 'not_checked'
            if group == 'identity' and name_status == 'unresolved':
                status = 'unresolved'
            groups.append(GroupObservation(group, True, status, page.observed_at if status == 'observed' else None))
        groups.append(GroupObservation('certifications', False, 'unsupported', None))
        parent = _text(row, 'rprsCtrtItemMngNo')
        observations.append(ItemObservation(
            listing_key=key, goods_id=_text(row, 'itemIdnfNo'),
            seller_name=_text(row, 'ctentUntyGrpNm'), manufacturer_name=_text(row, 'mnftrEtpsNm'),
            raw_names=raw_names, display_name=next((value for _, value in raw_names if value), None),
            name_status=name_status, spec_raw=_text(row, 'pdctAtrbCdDtlNm'),
            contract_price_decimal=source_row.contract_price_decimal, unit=_text(row, 'ctrtUntVal'),
            category_id=_text(row, 'itemClsfNo'),
            parent_listing_key=entity_key(page.profile, {'ctrtItemMngNo': parent}) if parent else None,
            contract_number=_text(row, 'ctrtNo'), contract_start=_text(row, 'ctrtBgngYmd'),
            contract_end=_text(row, 'ctrtEndYmd'), profile=page.profile, observed_at=page.observed_at,
            raw=row, field_states=tuple(fields), groups=tuple(groups),
        ))
    return tuple(observations)
