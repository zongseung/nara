from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from multiprocessing import get_context
from multiprocessing.managers import ValueProxy
import json
import logging
import os
from pathlib import Path
import tempfile
from time import monotonic, sleep
from types import TracebackType
from typing import Protocol, TypedDict, override

from core.catalog_json import json_text
from core.catalog_sources import SearchQuery, SourceError, SourceProfile, get_profile
from core.updater.models import ItemObservation
from src.g2b_client import fetch_search_page
from src.transport import RequestLimiter
from src.updater.exports import write_observation_xlsx
from src.updater.source import observations_from_page


logger = logging.getLogger(__name__)


class ProcessGate(Protocol):
    def __enter__(self) -> bool: ...
    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None,
                 traceback: TracebackType | None, /) -> None: ...


class ProcessRequestLimiter(RequestLimiter):
    def __init__(self, gate: ProcessGate, next_start: ValueProxy[float]) -> None:
        self.gate: ProcessGate = gate
        self.next_start: ValueProxy[float] = next_start
        super().__init__(sleep=self.pause_all)

    @override
    def before_request(self) -> None:
        with self.gate:
            delay=max(0, self.next_start.value-monotonic())
            if delay:
                sleep(delay)
            self.next_start.value = monotonic()+1.0

    def pause_all(self, seconds: float) -> None:
        with self.gate:
            self.next_start.value = max(self.next_start.value, monotonic()+seconds)
        sleep(seconds)


@dataclass(frozen=True, slots=True)
class LivePage:
    number: int
    response_total: int
    matched_total: int
    observed_at: datetime
    items: tuple[ItemObservation, ...]
    process_id: int


@dataclass(frozen=True, slots=True)
class PageFailure:
    code: str
    field: str
    attempts: int
    status: int | None


@dataclass(frozen=True, slots=True)
class LiveCollection:
    items: tuple[ItemObservation, ...]
    response_total: int
    matched_total: int
    pages_fetched: int
    process_ids: tuple[int, ...]
    observed_from: datetime
    observed_to: datetime


def _fetch_page(query: SearchQuery, profile: SourceProfile, gate: ProcessGate,
                next_start: ValueProxy[float]) -> LivePage | PageFailure:
    try:
        page = fetch_search_page(query, profile, limiter=ProcessRequestLimiter(gate,next_start))
        return LivePage(query.page, page.total_size,
                        page.row_total if page.row_total is not None else page.total_size,
                        page.observed_at, observations_from_page(page), os.getpid())
    except SourceError as error:
        return PageFailure(error.code,error.field,error.attempts,error.status)


def _checked(result: LivePage | PageFailure) -> LivePage:
    if isinstance(result, PageFailure):
        raise SourceError(result.code,result.field,result.attempts,result.status)
    return result


def collect_live(query: SearchQuery, *, limit: int = 1000, workers: int = 4,
                 profile: SourceProfile | None = None) -> LiveCollection:
    if not 1 <= limit <= 10000 or not 1 <= workers <= 4:
        raise SourceError('invalid_live_settings','limit=1..10000, workers=1..4')
    if query.page != 1 or query.page_size != 100:
        raise SourceError('invalid_live_pagination')
    if bool(query.start_price) != bool(query.end_price):
        raise SourceError('invalid_price_range','both bounds are required')
    if query.start_price:
        try:
            lower,upper = Decimal(query.start_price),Decimal(query.end_price)
        except InvalidOperation as error:
            raise SourceError('invalid_price_range') from error
        if not lower.is_finite() or not upper.is_finite() or lower < 0 or upper < lower:
            raise SourceError('invalid_price_range')
    selected = profile or get_profile('shopping-all')
    selected.require_collection_ready()
    logger.info('수집중 [0/?] 첫 페이지 조회 중 | 프로세스 %d개', workers)
    context = get_context('spawn')
    with context.Manager() as manager:
        gate = manager.Lock()
        next_start = manager.Value('d',0.0)
        with ProcessPoolExecutor(max_workers=workers,mp_context=context) as pool:
            first = _checked(pool.submit(_fetch_page,query,selected,gate,next_start).result())
            page_count = max(1,(min(limit,first.matched_total)+99)//100)
            items: list[ItemObservation] = []
            seen: set[str] = set()
            process_ids: list[int] = []
            observed = [first.observed_at]
            def accept(page: LivePage) -> None:
                expected = min(100,max(0,first.matched_total-(page.number-1)*100))
                if page.matched_total != first.matched_total or len(page.items) != expected:
                    raise SourceError('live_count_drift_or_short_page',str(page.number))
                for item in page.items:
                    if item.listing_key in seen:
                        raise SourceError('live_duplicate_item',item.listing_key)
                    seen.add(item.listing_key)
                items.extend(page.items[:limit-len(items)])
                process_ids.append(page.process_id)
                observed.append(page.observed_at)
                logger.info('수집중 [%d/%d] 페이지 완료 | %d/%d건',
                            len(process_ids), page_count, len(items), min(limit, first.matched_total))
            accept(first)
            for batch in range(2,page_count+1,workers):
                pending = [pool.submit(_fetch_page,replace(query,page=number),selected,gate,next_start)
                           for number in range(batch,min(batch+workers,page_count+1))]
                for future in pending:
                    accept(_checked(future.result()))
    return LiveCollection(tuple(items),first.response_total,first.matched_total,page_count,
                          tuple(process_ids),min(observed),max(observed))


class LiveReport(TypedDict):
    mode: str
    keyword: str
    company: str
    min_price: str
    max_price: str
    requested_limit: int
    collected_items: int
    matched_total: int
    response_total: int
    pages_fetched: int
    workers: int
    truncated: bool
    source_snapshot_verified: bool
    observed_from: str
    observed_to: str
    out: str
    file_bytes: int


def export_live(query: SearchQuery, destination: Path, *, limit: int = 1000, workers: int = 4,
                profile: SourceProfile | None = None) -> LiveReport:
    if destination.suffix.lower() not in ('.xlsx','.jsonl'):
        raise SourceError('unsupported_live_format')
    if destination.exists() or destination.is_symlink():
        raise SourceError('output_exists',str(destination))
    if not destination.parent.is_dir():
        raise SourceError('output_directory_missing',str(destination.parent))
    result = collect_live(query,limit=limit,workers=workers,profile=profile)
    report = LiveReport(mode='live_query',keyword=query.keyword,company=query.company,
        min_price=query.start_price,max_price=query.end_price,requested_limit=limit,
        collected_items=len(result.items),matched_total=result.matched_total,response_total=result.response_total,
        pages_fetched=result.pages_fetched,workers=workers,truncated=len(result.items)<result.matched_total,
        source_snapshot_verified=False,observed_from=result.observed_from.isoformat(),
        observed_to=result.observed_to.isoformat(),out=str(destination),file_bytes=0)
    logger.info('저장중 | %s', destination)
    if destination.suffix.lower() == '.xlsx':
        write_observation_xlsx(result.items,((key,str(value)) for key,value in report.items() if key!='file_bytes'),
                               destination,metadata_sheet='수집정보',items_sheet='실시간품목')
    else:
        descriptor,temporary = tempfile.mkstemp(prefix=destination.name+'.',suffix='.partial',dir=destination.parent)
        try:
            with os.fdopen(descriptor,'w',encoding='utf-8') as handle:
                metadata={key:value for key,value in report.items() if key!='file_bytes'}
                _ = handle.write(json.dumps(dict(kind='metadata',**metadata),ensure_ascii=False)+'\n')
                for item in result.items:
                    _ = handle.write(json_text(dict(kind='item',listing_key=item.listing_key,
                        observed_at=item.observed_at.isoformat(),raw=dict(item.raw)))+'\n')
            os.link(temporary,destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
    report['file_bytes'] = destination.stat().st_size
    logger.info('수집 완료 | %d건 | %s (%d바이트)', len(result.items), destination, report['file_bytes'])
    return report
