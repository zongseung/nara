from pathlib import Path

from prefect import flow
from prefect.client.orchestration import get_client
from prefect.client.schemas.actions import WorkPoolCreate
from prefect.client.schemas.objects import ConcurrencyLimitStrategy, ConcurrencyOptions
from prefect.exceptions import ObjectNotFound

from src.web.dispatch import root_path
from src.web.worker import execute


@flow(name='compare-web', retries=0, persist_result=False)
def compare_web(job_id: str, operation_id: str) -> None:
    execute(root_path(), job_id, operation_id)


def register() -> str:
    import os
    pool = os.environ.get('NARA_WEB_POOL', 'nara-web')
    with get_client(sync_client=True) as client:
        try:
            client.read_work_pool(pool)
        except ObjectNotFound:
            client.create_work_pool(WorkPoolCreate(name=pool, type='process'))
        flow_id = client.create_flow_from_name('compare-web')
        deployment = client.create_deployment(flow_id, name='nara-web', work_pool_name=pool,
            path=str(Path(__file__).resolve().parents[2]), entrypoint='src/web/flows.py:compare_web',
            concurrency_limit=1, concurrency_options=ConcurrencyOptions(
                collision_strategy=ConcurrencyLimitStrategy.ENQUEUE), paused=False,
            job_variables={'env': {'NARA_WEB_DATA_DIR': str(root_path()),
                                   'PREFECT_SERVER_ALLOW_EPHEMERAL_MODE': 'false'}})
        return str(deployment)


if __name__ == '__main__':
    print(register())
