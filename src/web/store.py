import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel

from src.updater.locks import file_lock
from src.web.models import Event, Job, JobRequest, ReviewItem, now


class JobConflict(Exception):
    pass


class JobStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, job_id: str) -> Path:
        return self.root / str(UUID(job_id))

    def write(self, path: Path, value: BaseModel) -> None:
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix='.partial')
        try:
            with os.fdopen(descriptor, 'w') as handle:
                handle.write(value.model_dump_json())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def create(self, request: JobRequest, upload: bytes | None = None) -> Job:
        job = Job(request=request, uploaded=upload is not None)
        directory = self.directory(job.id)
        directory.mkdir(mode=0o700)
        (directory/'items').mkdir()
        if upload is not None:
            (directory/'input.xls').write_bytes(upload)
        self.write(directory/'job.json', job)
        return job

    def read(self, job_id: str) -> Job:
        return Job.model_validate_json((self.directory(job_id)/'job.json').read_bytes())

    def update(self, job_id: str, change: Callable[[Job], Job]) -> Job:
        with file_lock(self.directory(job_id)/'job.lock'):
            job = change(self.read(job_id)).model_copy(update={'updated_at': now()})
            self.write(self.directory(job_id)/'job.json', job)
            return job

    def recent(self) -> list[Job]:
        paths = sorted(self.root.glob('*/job.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        return [Job.model_validate_json(p.read_bytes()) for p in paths[:50]]

    def item(self, job_id: str, index: int) -> ReviewItem:
        if index < 0:
            raise FileNotFoundError(index)
        return ReviewItem.model_validate_json((self.directory(job_id)/'items'/f'{index}.json').read_bytes())

    def items(self, job_id: str) -> list[ReviewItem]:
        paths = sorted((self.directory(job_id)/'items').glob('*.json'), key=lambda p: int(p.stem))
        return [ReviewItem.model_validate_json(p.read_bytes()) for p in paths]

    def save_item(self, job_id: str, item: ReviewItem) -> None:
        self.write(self.directory(job_id)/'items'/f'{item.index}.json', item)

    def restart_items(self, job_id: str, operation_id: str) -> None:
        directory = self.directory(job_id)
        with file_lock(directory/'job.lock'):
            items = directory/'items'
            if items.exists() and any(items.iterdir()):
                items.rename(directory/f'items-before-{operation_id}')
            items.mkdir(exist_ok=True)

    def event(self, job_id: str, event: Event) -> None:
        with file_lock(self.directory(job_id)/'events.lock'), (self.directory(job_id)/'events.jsonl').open('a') as handle:
            handle.write(event.model_dump_json()+'\n')

    def events(self, job_id: str) -> list[Event]:
        path = self.directory(job_id)/'events.jsonl'
        if not path.exists():
            return []
        with file_lock(self.directory(job_id)/'events.lock', shared=True):
            from collections import deque
            with path.open() as handle:
                return [Event.model_validate_json(line) for line in deque(handle, maxlen=100)]


def require_review(job: Job, revision: int) -> None:
    if job.status not in ('review', 'exported') or revision != job.revision:
        raise JobConflict('작업이 진행 중이거나 다른 창에서 변경되었습니다. 새로고침하세요.')
