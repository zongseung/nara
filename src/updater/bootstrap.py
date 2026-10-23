"""Idempotent inactive deployment registration for a fixed process-worker image."""
from prefect.client.orchestration import get_client
from prefect.client.schemas.actions import WorkPoolCreate, DeploymentScheduleCreate, DeploymentUpdate
from prefect.client.schemas.objects import ConcurrencyOptions, ConcurrencyLimitStrategy
from prefect.client.schemas.schedules import CronSchedule
from prefect.exceptions import ObjectNotFound
from src.updater.dispatcher import DEPLOYMENT_NAME, require_available, initialize
from src.updater.store import Store
from src.updater.locks import file_lock


def bootstrap() -> str:
    store=Store()
    with file_lock(store.path('locks/maintenance.lock'),shared=True):
        require_available(store)
        initialize(store)
        with get_client(sync_client=True) as client:
            try:
                client.read_work_pool('g2b-price-updates')
            except ObjectNotFound:
                client.create_work_pool(WorkPoolCreate(name='g2b-price-updates',type='process'))
            try:
                existing = client.read_deployment_by_name(DEPLOYMENT_NAME)
            except ObjectNotFound:
                existing = None
            if existing is not None:
                client.update_deployment(existing.id, DeploymentUpdate(
                    path='/app', entrypoint='src/updater/flows.py:refresh_shop_prices'))
                return str(existing.id)
            flow_id=client.create_flow_from_name('refresh-shop-prices')
            deployment=client.create_deployment(flow_id,name='g2b-price-refresh',work_pool_name='g2b-price-updates',
                path='/app',entrypoint='src/updater/flows.py:refresh_shop_prices',paused=True,
                schedules=[DeploymentScheduleCreate(schedule=CronSchedule(cron='0 2 * * *',timezone='Asia/Seoul'),active=False,slug='daily-kst')],
                concurrency_limit=1,concurrency_options=ConcurrencyOptions(collision_strategy=ConcurrencyLimitStrategy.CANCEL_NEW))
            return str(deployment)


if __name__=='__main__':
    print(bootstrap())
