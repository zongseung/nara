import os
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from src.web.models import Job

Submit = Callable[[str, str], object]


def next_operation(job: Job, operation: str, item_index: int | None = None) -> Job:
    return job.model_copy(update={'operation': operation, 'operation_id': str(uuid4()),
        'research': job.research if operation == 'research' else None,
        'completed': max(0, job.total-1) if operation == 'research' else job.completed,
        'item_index': item_index, 'status': 'queued', 'stage': 'queued', 'error': None,
        'cancel_requested': False, 'workbook': None, 'message': '작업 대기 중'})


def root_path() -> Path:
    return Path(os.environ.get('NARA_WEB_DATA_DIR', 'storage/web')).resolve()
