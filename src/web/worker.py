from collections.abc import Callable
from pathlib import Path

from core.catalog_sources import SearchQuery, SourceError
from core.comparison import CandidateProvider, compare
from core.legacy_matching import ComparisonItem, extract, norm_company
from src.build_report import render_multi
from src.catalog_provider import LiveProvider
from src.format_convert import read_items
from src.live_collection import collect_live
from src.updater.locks import file_lock
from src.web.models import Event, Job, JobRequest, ReviewItem
from src.web.store import JobStore

Companies = dict[str, list[ComparisonItem]]


class Cancelled(Exception):
    pass


def collect_company(request: JobRequest) -> Companies:
    result = collect_live(SearchQuery(company=request.company, keyword=request.keyword),
                          limit=request.limit or 10000, workers=1)
    if not request.limit and len(result.items) < result.matched_total:
        raise SourceError('company_limit_exceeded')
    items = [extract(dict(item.raw)) for item in result.items]
    items = [item for item in items if norm_company(item['업체']) == norm_company(request.company)]
    if not items:
        raise SourceError('company_not_found')
    return {items[0]['업체']: items}


def _check(store: JobStore, job: Job) -> None:
    current = store.read(job.id)
    if current.cancel_requested or current.operation_id != job.operation_id:
        raise Cancelled()


def _progress(store: JobStore, job: Job, stage: str, message: str, index: int | None = None) -> None:
    _check(store, job)
    store.event(job.id, Event(stage=stage, message=message, item_index=index))
    store.update(job.id, lambda current: current.model_copy(update={'stage': stage, 'message': message}))


def _compare_item(store: JobStore, job: Job, item: ReviewItem, provider: CandidateProvider,
                  excluded: list[str]) -> None:
    _progress(store, job, 'matching', f'{item.reference.get("모델") or item.reference["품명"]} 후보 비교 중', item.index)
    original = item.reference.copy()
    if item.support_review:
        original['지지대수'] = extract(original['_raw'])['지지대수']
    reference, candidates = compare(original, provider, top_n=job.request.top,
        exact_price=job.request.exact_price, fallback_band=job.request.fallback_band, excluded=excluded,
        confirmed_supports=item.support_review.count if item.support_review else None,
        same_direction=job.request.same_direction,
        excluded_ids=item.rejected_ids,
        on_stage=lambda stage: _progress(store, job, stage, {
            'searching': '경쟁사 후보 조회 중', 'checking': '필수 조건 검사 중',
            'enriching': '부족한 상세 속성 확인 중', 'selecting': '경쟁사 후보 선정 중'}[stage], item.index))
    _check(store, job)
    store.save_item(job.id, item.model_copy(update={'reference': reference, 'candidates': candidates, 'excluded': []}))
    _progress(store, job, 'item_completed', reference.get('비교보류사유') or f'후보 {len(candidates)}개 선정', item.index)
    store.update(job.id, lambda current: current.model_copy(update={'completed': current.completed+1}))


def _research_item(store: JobStore, job: Job, provider: CandidateProvider) -> str:
    if job.item_index is None or job.research is None:
        raise SourceError('missing_research_request')
    item = store.item(job.id, job.item_index)
    if item.research_operation_id == job.operation_id:
        return '재검색 결과 저장 완료'
    target = job.research.replace_item_id
    if sum(c['식별번호'] == target for c in item.candidates) != 1:
        raise SourceError('research_target_changed')
    rejected = set(item.rejected_ids) | {target}
    kept = [c for c in item.candidates if c['식별번호'] != target]
    _progress(store, job, 'searching', '선택한 후보의 대체 제품 재검색 중', item.index)
    _, found = compare(item.reference, provider, top_n=1,
        exact_price=job.request.exact_price, fallback_band=job.request.fallback_band,
        same_direction=job.request.same_direction,
        confirmed_supports=item.support_review.count if item.support_review else None,
        excluded=[i.company for i in store.items(job.id)] + [c['업체'] for c in kept],
        excluded_ids=rejected | {identifier for c in kept if (identifier := c['식별번호'])},
        required_material=job.research.conditions.material,
        on_stage=lambda stage: _progress(store, job, stage, {
            'searching': '대체 후보 조회 중', 'checking': '기존 조건과 요청 조건 검사 중',
            'enriching': '대체 후보 상세 확인 중', 'selecting': '대체 후보 선정 중'}[stage], item.index))
    candidates, excluded = item.candidates, item.excluded
    if found:
        candidates = sorted([*kept, found[0]], key=lambda c: (c['가격'] or 0, c['식별번호'] or ''))
        excluded_ids = {item.candidates[i]['식별번호'] for i in item.excluded}
        excluded = [i for i, c in enumerate(candidates) if c['식별번호'] in excluded_ids]
    message = ('대체 후보 반영 완료 · 가격 오름차순 정렬' if found else
               '조회 범위에서 요청 조건에 맞는 대체 후보가 없어 기존 후보를 유지했습니다.')
    def commit(current: Job) -> Job:
        _check(store, job)
        store.save_item(job.id, item.model_copy(update={'candidates': candidates, 'excluded': excluded,
            'rejected_ids': sorted(rejected), 'research_operation_id': job.operation_id}))
        return current.model_copy(update={'message': message, 'completed': current.total})
    store.update(job.id, commit)
    _progress(store, job, 'research_completed', message, item.index)
    return message


def execute(root: Path, job_id: str, operation_id: str, *, provider: CandidateProvider | None = None,
            collect: Callable[[JobRequest], Companies] = collect_company) -> None:
    store = JobStore(root)
    with file_lock(root/'worker.lock'):
        job = store.read(job_id)
        if job.operation_id != operation_id or job.status not in ('queued', 'running'):
            return
        completed = 0 if job.operation == 'collect' else max(0, job.total-1) if job.operation in ('rematch', 'research') else job.completed
        job = store.update(job_id, lambda current: current.model_copy(update={'status': 'running', 'completed': completed}))
        source = provider or LiveProvider()
        try:
            message = '검토 준비 완료'
            match job.operation:
                case 'collect':
                    _progress(store, job, 'collecting', '기준 품목 조회 중')
                    companies = read_items(store.directory(job_id)/'input.xls') if job.uploaded else collect(job.request)
                    if job.uploaded and job.request.limit:
                        companies = {name: items[:job.request.limit] for name, items in companies.items()}
                    total = sum(len(items) for items in companies.values())
                    if not total or total > 10000:
                        raise SourceError('invalid_job_size')
                    store.restart_items(job_id, operation_id)
                    store.update(job_id, lambda current: current.model_copy(update={
                        'total': total, 'partial': bool(job.request.limit)}))
                    index = 0
                    for company, items in companies.items():
                        for reference in items:
                            item = ReviewItem(index=index, company=company, reference=reference)
                            store.save_item(job_id, item)
                            _compare_item(store, job, item, source, list(companies))
                            index += 1
                case 'rematch':
                    if job.item_index is None:
                        raise SourceError('missing_item_index')
                    item = store.item(job_id, job.item_index)
                    _compare_item(store, job, item, source, list({i.company for i in store.items(job_id)}))
                case 'research':
                    message = _research_item(store, job, source)
                case 'export':
                    _progress(store, job, 'exporting', '검토 결과로 Excel 생성 중')
                    pairs: dict[str, list[tuple[ComparisonItem, list[ComparisonItem]]]] = {}
                    for item in store.items(job_id):
                        selected = [c for i, c in enumerate(item.candidates) if i not in item.excluded]
                        pairs.setdefault(item.company, []).append((item.reference, selected))
                    filename = f'comparison-{job.revision}-{job.operation_id}.xlsx'
                    render_multi(pairs, store.directory(job_id)/filename, top_n=job.request.top)
                    _check(store, job)
                    store.update(job_id, lambda current: current.model_copy(update={
                        'status': 'exported', 'stage': 'exported', 'completed': current.total,
                        'message': '비교표 준비 완료', 'workbook': filename}))
                    return
            _check(store, job)
            store.update(job_id, lambda current: current.model_copy(update={
                'status': 'review', 'stage': 'review', 'completed': current.total, 'message': message}))
        except Cancelled:
            store.update(job_id, lambda current: current.model_copy(update={
                'status': 'cancelled', 'stage': 'cancelled', 'message': '작업을 중지했습니다.'}))
        except Exception as error:
            error_name, message = type(error).__name__, str(error)
            store.update(job_id, lambda current: current.model_copy(update={
                'status': 'failed', 'stage': 'failed', 'error': error_name,
                'message': f'작업 실패: {message}'}))
            raise
