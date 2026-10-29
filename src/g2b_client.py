"""
나라장터 종합쇼핑몰(shop.g2b.go.kr) 상품검색 비공식 클라이언트.

공식 UI가 호출하는 내부 JSON 엔드포인트(newShopUntySrchApi.do)를 그대로 사용한다.
로그인 불필요, 응답은 순수 JSON. 페이지 크기는 서버가 100으로 상한을 고정한다
(500/1000을 요청해도 100건만 반환됨 — 실측 확인).

ponytail: 내부 비공식 API. 차세대 나라장터 개편 시 엔드포인트/필드가 바뀔 수 있다.
          깨지면 공식 오픈API(data.go.kr 15129471 종합쇼핑몰 품목정보)로 교체.
"""
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urljoin

import requests

from core.catalog_sources import (
    JsonValue,
    RawRow,
    SearchQuery,
    SourceError,
    SourceProfile,
    SourceRow,
    get_profile,
    parse_source_row,
)
from src.transport import RequestLimiter, post_json

_DEFAULT_LIMITER = RequestLimiter()

# UI가 보내는 정적 헤더 (CSRF 토큰·암호화 없음). 그대로 재현.
_HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json",
    "submissionid": "mf_wfm_container_tabShopSubHeader_contents_tabShopLstFormCon_body_sbmSrch",
    "Menu-Info": '{"menuNo":"12167","menuCangVal":"GMSD003_01","bsneClsfCd":"%EC%97%85130034","scrnNo":"07677"}',
    "User-Agent": "Mozilla/5.0",
}


@dataclass(frozen=True, slots=True)
class SearchPage:
    rows: tuple[SourceRow, ...]
    raw: RawRow
    total_size: int
    row_total: int | None
    profile: SourceProfile
    observed_at: datetime
    errors: tuple[SourceError, ...] = ()
    attempts: int = 1
    raw_text: str | None = None


def _count(value: JsonValue, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise SourceError('invalid_count', field)
    if isinstance(value, str) and (not value.isascii() or not value.isdigit()):
        raise SourceError('invalid_count', field)
    try:
        count = int(value)
    except ValueError as exc:
        raise SourceError('invalid_count', field) from exc
    if count < 0:
        raise SourceError('invalid_count', field)
    return count


def parse_search_page(body: JsonValue, profile: SourceProfile) -> SearchPage:
    profile.require_collection_ready()
    if not isinstance(body, dict):
        raise SourceError('invalid_response', 'root')
    if 'ErrorCode' in body and (type(body['ErrorCode']) not in (int, str) or body['ErrorCode'] not in (0, '0')):
        raise SourceError('business_error', 'ErrorCode')
    rows = body.get('rsltList')
    if not isinstance(rows, list):
        raise SourceError('invalid_response', 'rsltList')
    total = _count(body.get('totalSize'), 'totalSize')
    if (not rows and total != 0) or (rows and total < len(rows)) or len(rows) > profile.page_cap:
        raise SourceError('invalid_response', 'rsltList/totalSize')
    parsed, counts = [], []
    for row in rows:
        if not isinstance(row, dict):
            raise SourceError('invalid_response', 'rsltList[]')
        counts.append(_count(row.get('totCnt'), 'totCnt'))
        parsed.append(parse_source_row(row))
    if counts and (len(set(counts)) != 1 or counts[0] < len(rows)):
        raise SourceError('invalid_count', 'totCnt')
    return SearchPage(tuple(parsed), body, total, counts[0] if counts else None,
                      profile, datetime.now(UTC))


def fetch_search_page(query: SearchQuery, profile: SourceProfile | None = None, *,
                      session: requests.Session | None = None, timeout: float | tuple[float, float] = (10, 35),
                      limiter: RequestLimiter | None = None) -> SearchPage:
    selected = profile or get_profile()
    response = post_json(selected.endpoint, selected.payload(query), session=session,
                         limiter=limiter or _DEFAULT_LIMITER, headers=_HEADERS, timeout=timeout)
    try:
        page = parse_search_page(response.body, selected)
    except SourceError as exc:
        raise replace(exc, attempts=response.attempts, status=200) from exc
    return replace(page, attempts=response.attempts, raw_text=response.text)


def enrich_product(raw: RawRow, *, session: requests.Session | None = None,
                   required: tuple[str, ...] = ()) -> RawRow:
    from core.legacy_matching import extract, missing_product_fields
    item = extract(raw)
    if raw.get('_detail_checked_at') or not missing_product_fields(item, required):
        return raw
    goods_id, category = str(raw.get('itemIdnfNo') or ''), str(raw.get('itemClsfNo') or '')
    if not re.fullmatch(r'\d{8}', goods_id) or not re.fullmatch(r'\d{8}', category):
        return raw
    payload: dict[str, dict[str, str | int]] = {'dlGdsDtlInfoSrchM': {
        'srchItemIdnfNo': goods_id, 'srchItemClsfNo': category,
        'srchCtrtNo': '', 'srchCtrtChgOrd': '', 'srchCtrtItemSqno': ''}}
    endpoint = urljoin(get_profile().endpoint, '/gm/gms/gmsf/GdsDtlInfo/selectPdctAtrbInfo.do')
    response = post_json(endpoint, payload, session=session, limiter=_DEFAULT_LIMITER)
    body = response.body
    if not isinstance(body, dict) or type(body.get('ErrorCode')) not in (int, str) or body.get('ErrorCode') not in (0, '0'):
        raise SourceError('invalid_detail_response', goods_id, response.attempts, 200)
    attributes = body.get('dlPdctAtrbInfoL')
    if not isinstance(attributes, list) or any(not isinstance(a, dict) or
            not isinstance(a.get('atrbnm'), str) or type(a.get('atrbvalue')) not in (str, int, float, Decimal, type(None)) or
            not isinstance(a.get('atrboptnvalue'), (str, type(None)))
            for a in attributes):
        raise SourceError('invalid_detail_response', goods_id, response.attempts, 200)
    # 상세 응답에 없는 기존 속성(예: 입력 파일의 모델명 열)은 유지하고, 같은 이름은 상세 응답을 따른다.
    labels = {attribute['atrbnm'] for attribute in attributes}
    previous = raw.get('_detail_attributes')
    kept = [a for a in previous if isinstance(a, dict) and a.get('atrbnm') not in labels] if isinstance(previous, list) else []
    return dict(raw, _detail_attributes=[*attributes, *kept], _detail_checked_at=datetime.now(UTC).isoformat())
