"""Whole-partition Prefect tasks with persistent business checkpoints."""
from dataclasses import dataclass, replace, field
from pathlib import Path
from typing import Literal
import os
import json
from decimal import Decimal
from core.catalog_partitions import PriceDomain
from prefect import flow, task
from prefect.cache_policies import NONE
from prefect.artifacts import create_markdown_artifact
from prefect.runtime import flow_run
from prefect.futures import PrefectFuture
from core.catalog_sources import get_profile, SearchQuery, SourceProfile, SourceError
from src.transport import RequestLimiter
from src.g2b_client import SearchPage, fetch_search_page
from src.updater.collector import CollectionPolicy, PartitionJob, RateLimitedSource, prepare_collection, collect_partition, reconcile_collection, transient
from src.updater.dispatcher import require_available, ensure_slot, claim_slot
from src.updater.locks import file_lock
from src.updater.publisher import publish
from src.updater.store import Store, Lease, StoreError


@dataclass(frozen=True, slots=True)
class EndpointSource:
    profile: SourceProfile
    endpoint: str | None = None
    limiter: RequestLimiter = field(default_factory=RequestLimiter)

    def fetch(self, query: SearchQuery) -> SearchPage:
        transport=self.profile if self.endpoint is None else replace(self.profile,endpoint=self.endpoint)
        return replace(fetch_search_page(query,transport,timeout=(10,35),limiter=self.limiter),profile=self.profile)


def retry_transient(task, task_run, state) -> bool:
    try:
        state.result()
    except SourceError as error:
        return transient(error)
    except (StoreError, OSError):
        return False
    return False


@task(name='collect-partition',cache_policy=NONE,persist_result=False,retries=1,retry_condition_fn=retry_transient)
def collect_pages(data_root: str, lease: Lease, source: RateLimitedSource, job: PartitionJob) -> None:
    collect_partition(Store(Path(data_root)),lease,source,job=job)


@task(name='publish-validated-snapshot',cache_policy=NONE,persist_result=False,retries=0)
def publish_snapshot(data_root: str, lease: Lease) -> str:
    return publish(Store(Path(data_root)),lease).model_dump_json()


@flow(name='refresh-shop-prices',retries=0,persist_result=False,timeout_seconds=43200)
def refresh_shop_prices(slot_key: str | None = None, environment: Literal['fixture','production'] = 'production', source_endpoint: str | None = None, data_root: str | None = None) -> dict[str,str|int|None]:
    from src.updater.reader import Manifest
    result: dict[str,str|int|None]
    store=Store(None if data_root is None else Path(data_root))
    with file_lock(store.path('locks/maintenance.lock'),shared=True):
        require_available(store)
        if environment=='production' and os.environ.get('NARA_SOURCE_ENABLED')!='true':
            raise StoreError('production_source_not_enabled')
        if environment!='fixture' and source_endpoint:
            raise StoreError('production_endpoint_override')
        if slot_key is None:
            slot_key=ensure_slot(store,flow_run.scheduled_start_time)
        with store.control() as conn:
            slot=conn.execute('SELECT * FROM scheduled_slots WHERE slot_key=?',(slot_key,)).fetchone()
        if slot is None:
            raise StoreError('unknown_slot')
        business_run=slot['business_run_id']
        committed=None if business_run is None else store.publication(business_run)
        if committed:
            return dict(status='published_ack_recovered',run_id=business_run)
        try:
            with store.writer(get_profile('shopping-all'),environment=environment,resume_run_id=business_run) as lease:
                if not claim_slot(store,slot_key,lease.run_id):
                    result=dict(status='skipped_duplicate_or_obsolete',slot_key=slot_key)
                else:
                    lease=replace(lease,slot_key=slot_key)
                    source=RateLimitedSource(EndpointSource(lease.profile,source_endpoint),1.0)
                    domain=None
                    if os.environ.get('NARA_PRICE_DOMAIN_JSON'):
                        settings=json.loads(os.environ['NARA_PRICE_DOMAIN_JSON'])
                        domain=PriceDomain(Decimal(settings['lower']),Decimal(settings['upper']),Decimal(settings['quantum']),int(settings['max_partition_rows']))
                    policy=CollectionPolicy(domain=domain,transient_replays=0,allow_deep_pages=domain is None,network_workers=4)
                    jobs=prepare_collection(store,lease,source,policy=policy)
                    pending: list[PrefectFuture[None]]=[]
                    for job in jobs:
                        pending.append(collect_pages.submit(str(store.root),lease,source,job))
                        if len(pending) >= policy.network_workers:
                            pending.pop(0).result()
                    for future in pending:
                        future.result()
                    reconcile_collection(store,lease,source,policy=policy)
                    manifest=Manifest.model_validate_json(publish_snapshot(str(store.root),lease))
                    result=dict(status='published',run_id=lease.run_id,catalog_generation=manifest.catalog_generation,
                                observed_from=manifest.observed_from,observed_to=manifest.observed_to,published_at=manifest.published_at)
        finally:
            with store.control() as conn,conn:
                conn.execute("UPDATE scheduled_slots SET status='failed',reason='collection_failed' WHERE slot_key=? AND status='running' AND business_run_id IN (SELECT run_id FROM runs WHERE status='failed')",(slot_key,))
        create_markdown_artifact(key='g2b-refresh-summary',markdown=str(result))
        return result
