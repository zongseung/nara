import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from src.web.dispatch import Submit, next_operation, root_path
from src.web.models import (
    Event,
    Job,
    JobRequest,
    ResearchInput,
    Revision,
    SelectionInput,
    SupportInput,
    SupportReview,
)
from src.web.store import JobConflict, JobStore, require_review
from src.web.worker import execute


def create_app(root: Path | None = None, *, submit: Submit | None = None) -> FastAPI:
    store = JobStore(root or root_path())
    executor = ThreadPoolExecutor(max_workers=1)  # 원천 요청 제한(초당 1건)이 프로세스 단위라 작업은 하나씩 실행한다.
    run = submit or (lambda job_id, operation_id: executor.submit(execute, store.root, job_id, operation_id))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for path in store.root.glob('*/job.json'):  # 이전 프로세스의 대기·진행 작업은 다시 실행되지 않는다.
            if Job.model_validate_json(path.read_bytes()).status in ('queued', 'running'):
                store.update(path.parent.name, lambda job: job.model_copy(update={'status': 'failed', 'stage': 'failed',
                    'message': '서버가 다시 시작되어 작업이 중단되었습니다. 작업을 재시도하세요.'}))
        try:
            yield
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title='nara 비교 API', version='0.1.0', lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=os.environ.get(
        'NARA_WEB_HOSTS', 'localhost,127.0.0.1,testserver').split(','))

    @app.middleware('http')
    async def same_origin(request: Request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            if (origin and origin != str(request.base_url).rstrip('/')) or request.headers.get('sec-fetch-site') == 'cross-site':
                return JSONResponse({'detail': '다른 사이트에서 보낸 요청은 허용하지 않습니다.'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https://*.g2b.go.kr data:; "
            "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith('/jobs') else 'no-cache'
        return response

    @app.exception_handler(JobConflict)
    async def conflict(request: Request, error: JobConflict):
        return JSONResponse({'detail': str(error)}, status_code=409)

    @app.exception_handler(FileNotFoundError)
    async def missing(request: Request, error: FileNotFoundError):
        return JSONResponse({'detail': '작업 또는 품목을 찾을 수 없습니다.'}, status_code=404)

    def send(job: Job) -> Job:
        run(job.id, job.operation_id)
        return job

    @app.get('/health')
    def health():
        return {'status': 'ok', 'storage': 'files'}

    @app.get('/jobs')
    def jobs():
        return store.recent()

    @app.post('/jobs', status_code=202)
    def create_job(body: JobRequest):
        if not body.company:
            raise HTTPException(422, '기준 업체를 입력하세요.')
        return send(store.create(body))

    @app.post('/jobs/upload', status_code=202)
    async def upload_job(request: Request, limit: int = 0, top: int = 3, exact_price: bool = False,
                         fallback_band: float = 0, same_direction: bool = False):
        from pydantic import ValidationError
        try:
            settings = JobRequest(limit=limit, top=top, exact_price=exact_price,
                                  fallback_band=fallback_band, same_direction=same_direction)
        except ValidationError as error:
            raise HTTPException(422, '입력 조건을 확인하세요.') from error
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 8*1024*1024:
                raise HTTPException(413, '입력 파일은 최대 8MB입니다.')
        if not content.startswith(bytes.fromhex('D0CF11E0A1B11AE1')):
            raise HTTPException(422, '품목등록결과 .xls 파일을 선택하세요.')
        return send(store.create(settings, bytes(content)))

    @app.get('/jobs/{job_id}')
    def get_job(job_id: UUID):
        return store.read(str(job_id))

    @app.get('/jobs/{job_id}/items')
    def get_items(job_id: UUID):
        return [item.model_dump(mode='json', exclude={'reference': {'_raw'}, 'candidates': {'__all__': {'_raw'}}})
                for item in store.items(store.read(str(job_id)).id)]

    @app.get('/jobs/{job_id}/events')
    def get_events(job_id: UUID):
        store.read(str(job_id))
        return store.events(str(job_id))

    @app.post('/jobs/{job_id}/items/{index}/supports', status_code=202)
    def supports(job_id: UUID, index: int, body: SupportInput):
        key = str(job_id)
        def change(job: Job) -> Job:
            require_review(job, body.revision)
            item = store.item(key, index)
            from core.legacy_matching import extract
            known = extract(item.reference['_raw'])['지지대수']
            if known is not None and known != body.count:
                raise HTTPException(409, '원천에 명시된 지지대 개수와 충돌합니다.')
            store.save_item(key, item.model_copy(update={'support_review': SupportReview(count=body.count, note=body.note)}))
            store.event(key, Event(stage='support_confirmed', item_index=index,
                                  message=f'사용자 확인: 지지대 {body.count}개 / {body.note}'))
            return next_operation(job.model_copy(update={'revision': job.revision+1}), 'rematch', index)
        return send(store.update(key, change))

    @app.post('/jobs/{job_id}/items/{index}/selection')
    def selection(job_id: UUID, index: int, body: SelectionInput):
        key = str(job_id)
        def change(job: Job) -> Job:
            require_review(job, body.revision)
            item = store.item(key, index)
            if len(set(body.excluded)) != len(body.excluded) or any(i < 0 or i >= len(item.candidates) for i in body.excluded):
                raise HTTPException(422, '유효하지 않은 후보입니다.')
            store.save_item(key, item.model_copy(update={'excluded': body.excluded}))
            store.event(key, Event(stage='selection_changed', item_index=index,
                                  message=f'사용자 제외 후보: {body.excluded}'))
            return job.model_copy(update={'revision': job.revision+1, 'status': 'review', 'workbook': None})
        return store.update(key, change)

    @app.post('/jobs/{job_id}/items/{index}/research', status_code=202)
    def research(job_id: UUID, index: int, body: ResearchInput):
        key = str(job_id)
        def change(job: Job) -> Job:
            require_review(job, body.revision)
            item = store.item(key, index)
            if sum(c['식별번호'] == body.replace_item_id for c in item.candidates) != 1:
                raise HTTPException(422, '교체할 후보를 다시 선택하세요.')
            store.event(key, Event(stage='research_requested', item_index=index,
                message=f'{body.replace_item_id} 대체 후보 요청 · 재질: {body.conditions.material or "기존 조건"}'))
            return next_operation(job.model_copy(update={
                'revision': job.revision+1, 'research': body}), 'research', index)
        return send(store.update(key, change))

    @app.post('/jobs/{job_id}/workbook', status_code=202)
    def workbook(job_id: UUID, body: Revision):
        def change(job: Job) -> Job:
            require_review(job, body.revision)
            return next_operation(job, 'export')
        return send(store.update(str(job_id), change))

    @app.get('/jobs/{job_id}/workbook')
    def download(job_id: UUID):
        job = store.read(str(job_id))
        if job.status != 'exported' or not job.workbook:
            raise HTTPException(409, '현재 검토 결과의 비교표가 아직 준비되지 않았습니다.')
        return FileResponse(store.directory(job.id)/job.workbook, filename='가격비교표.xlsx',
                            media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    @app.post('/jobs/{job_id}/retry', status_code=202)
    def retry(job_id: UUID):
        def change(job: Job) -> Job:
            if job.status not in ('failed', 'cancelled'):
                raise JobConflict('실패·중지 상태의 작업만 재시도할 수 있습니다.')
            return next_operation(job, job.operation, job.item_index)
        return send(store.update(str(job_id), change))

    @app.post('/jobs/{job_id}/cancel')
    def cancel(job_id: UUID):
        def change(job: Job) -> Job:
            if job.status not in ('queued', 'running'):
                raise JobConflict('진행 중인 작업만 중지할 수 있습니다.')
            return job.model_copy(update={'cancel_requested': True,
                'status': 'cancelled' if job.status == 'queued' else job.status,
                'message': '진행 중인 조회가 끝나면 중지합니다.'})
        return store.update(str(job_id), change)

    app.mount('/', StaticFiles(directory=Path(__file__).parent/'static', html=True), name='web')
    return app


app = create_app()
