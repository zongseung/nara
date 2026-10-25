import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import requests

from src.web.models import Job
from src.web.store import JobStore

Submit = Callable[[str, str], str | None]


@contextmanager
def prefect_client() -> Iterator[requests.Session]:
    auth = os.environ.get('PREFECT_API_AUTH_STRING')
    with requests.Session() as client:
        if auth:
            username, password = auth.split(':', 1)
            client.auth = (username, password)
        yield client


def prefect_base() -> str:
    return os.environ.get('PREFECT_API_URL', 'http://127.0.0.1:4200/api').rstrip('/')


def submit(job_id: str, operation_id: str) -> str:
    base = prefect_base()
    name = os.environ.get('NARA_WEB_DEPLOYMENT', 'compare-web/nara-web')
    with prefect_client() as client:
        deployment = client.get(f'{base}/deployments/name/{name}', timeout=(3, 10))
        deployment.raise_for_status()
        response = client.post(f'{base}/deployments/{deployment.json()["id"]}/create_flow_run',
            json={'parameters': {'job_id': job_id, 'operation_id': operation_id},
                  'idempotency_key': operation_id}, timeout=(3, 10))
        response.raise_for_status()
        return str(response.json()['id'])


def enqueue(store: JobStore, job: Job, sender: Submit) -> Job:
    try:
        flow_run_id = sender(job.id, job.operation_id)
    except requests.RequestException:
        store.update(job.id, lambda current: current.model_copy(update={
            'message': 'Prefect 연결을 확인한 후 작업 제출을 다시 시도하세요.'}))
        raise
    store.update(job.id, lambda current: current.model_copy(update={'flow_run_id': flow_run_id})
                 if current.operation_id == job.operation_id else current)
    return store.read(job.id)


def next_operation(job: Job, operation: str, item_index: int | None = None) -> Job:
    return job.model_copy(update={'operation': operation, 'operation_id': str(uuid4()),
        'research': job.research if operation == 'research' else None,
        'completed': max(0, job.total-1) if operation == 'research' else job.completed,
        'item_index': item_index, 'status': 'queued', 'stage': 'queued', 'error': None,
        'cancel_requested': False, 'workbook': None, 'flow_run_id': None, 'message': '작업 대기 중'})


def reconcile(store: JobStore, job: Job) -> Job:
    if job.status not in ('queued', 'running') or not job.flow_run_id:
        return job
    try:
        with prefect_client() as client:
            response = client.get(f'{prefect_base()}/flow_runs/{job.flow_run_id}', timeout=(2, 3))
            response.raise_for_status()
            state = response.json().get('state_type')
    except requests.RequestException:
        return job
    if state in ('FAILED', 'CRASHED', 'CANCELLED', 'COMPLETED'):
        return store.update(job.id, lambda current: current.model_copy(update={
            'status': 'cancelled' if state == 'CANCELLED' else 'failed', 'stage': 'failed',
            'message': f'워커가 종료되었습니다 ({state}). 작업을 재시도하세요.'})
            if current.operation_id == job.operation_id and current.status in ('queued', 'running') else current)
    return job


def root_path() -> Path:
    return Path(os.environ.get('NARA_WEB_DATA_DIR', 'storage/web')).resolve()
