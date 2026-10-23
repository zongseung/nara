"""Versioned source scopes and identities shared by collection and updating."""
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Literal, Mapping

type JsonValue = None | bool | int | float | Decimal | str | list[JsonValue] | dict[str, JsonValue]
type RawRow = Mapping[str, JsonValue]


@dataclass(slots=True)  # noqa: MUTABLE_OK — exception propagation assigns __traceback__.
class SourceError(Exception):
    code: str
    field: str = ''
    attempts: int = 0
    status: int | None = None

    def __str__(self) -> str:
        return f'{self.code}: {self.field} (attempts={self.attempts}, status={self.status})'


@dataclass(frozen=True, slots=True)
class SearchQuery:
    page: int = 1
    page_size: int = 100
    keyword: str = ''
    start_price: str = ''
    end_price: str = ''
    company: str = ''


@dataclass(frozen=True, slots=True)
class SourceProfile:
    source_id: str
    scope: str
    entity_kind: Literal['listing', 'goods']
    endpoint: str
    target: str
    purchase_method: str
    digital: str = 'N'
    validation_status: Literal['validated', 'unvalidated', 'unsupported'] = 'validated'
    page_cap: int = 100
    source_schema_version: str = 'shopping-search/1'
    source_normalizer_version: str = 'shopping-normalizer/1'
    allow_dqid: bool = False
    name_aliases: tuple[str, ...] = ('itemIdnfNm', 'itemIndfNmView')

    @property
    def profile_hash(self) -> str:
        return sha256(json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def require_collection_ready(self) -> None:
        if self.validation_status != 'validated':
            raise SourceError(f'{self.validation_status}_scope', self.scope)
        if self.source_schema_version != 'shopping-search/1' or self.source_normalizer_version != 'shopping-normalizer/1':
            raise SourceError('unvalidated_version', self.source_schema_version)
        if self.name_aliases != ('itemIdnfNm', 'itemIndfNmView'):
            raise SourceError('unvalidated_alias', self.scope)

    def payload(self, query: SearchQuery) -> dict[str, dict[str, str | int]]:
        self.require_collection_ready()
        if type(query.page) is not int or type(query.page_size) is not int or query.page < 1 or query.page_size < 1:
            raise SourceError('invalid_query', 'pagination')
        return {'searchVO': {
            'tabDiv': '', 'target': self.target, 'searchKeyword': query.keyword,
            'etpsNm': query.company, 'prchsMthdSeCd': self.purchase_method,
            'sortCndt': 'SRCH_UPRC_ASC', 'recordCountPerPage': str(min(query.page_size, self.page_cap)),
            'currentPage': query.page, 'srchSeCd': '검030006', 'rdoIndex': 1,
            'dgtlSrvcMallYn': self.digital, 'startPrce': query.start_price, 'endPrce': query.end_price,
        }}


def get_profile(scope: str = 'shopping-main') -> SourceProfile:
    endpoint = 'https://shop.g2b.go.kr/gm/gms/gmsd/newShopUntySrchApi.do'
    match scope:  # noqa: MATCH_OK — open source names deliberately map to an unsupported profile.
        case 'shopping-all' | 'shopping-main':
            return SourceProfile('shopping', scope, 'listing', endpoint,
                                 '계300001,계300002,계309999', '' if scope == 'shopping-all' else '본010001')
        case 'digital':
            return SourceProfile('digital', scope, 'listing',
                                 'https://shop.g2b.go.kr/gm/gms/gmsd/newDigitalUntySrchApi.do',
                                 '계300022,계300026,계300027,계300028,계300032', '',
                                 digital='Y', validation_status='unvalidated')
        case 'catalog':
            return SourceProfile('catalog', scope, 'goods',
                                 'https://goods.g2b.go.kr:8053/search/unifiedGoodsSearch.do', '', '',
                                 validation_status='unvalidated', page_cap=20, source_schema_version='catalog/unvalidated')
        case _:
            return SourceProfile(scope, scope, 'listing', '', '', '', validation_status='unsupported')


def entity_key(profile: SourceProfile, row: RawRow) -> str:
    profile.require_collection_ready()
    field = 'ctrtItemMngNo'
    value = row.get(field)
    if value is None and profile.allow_dqid:
        field, value = 'dqId', row.get('dqId')
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise SourceError('invalid_identity', field)
    return f'{profile.source_id}:{profile.entity_kind}:{field}:{value}'


@dataclass(frozen=True, slots=True)
class SourceRow:
    raw: RawRow
    contract_price_decimal: Decimal | None


def parse_source_row(row: RawRow) -> SourceRow:
    value = row.get('ctrtUprc')
    if value is None:
        return SourceRow(row, None)
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise SourceError('invalid_price', 'ctrtUprc')
    try:
        price = Decimal(str(value))
    except InvalidOperation as exc:
        raise SourceError('invalid_price', 'ctrtUprc') from exc
    if not price.is_finite() or price < 0:
        raise SourceError('invalid_price', 'ctrtUprc')
    return SourceRow(row, price)
