"""Fixed price partitions, atomic page checkpoints and bounded collection."""
from core.catalog_json import json_text
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
import gzip
import shutil
from threading import Lock
import time
from typing import TYPE_CHECKING, Literal
from core.catalog_partitions import Partition, PriceDomain, plan_partitions
from core.catalog_sources import SearchQuery, SourceError, SourceProfile, entity_key
from src.g2b_client import SearchPage
from src.updater.source import ItemSource, observations_from_page
from src.updater.store import Lease, Store, StoreError
if TYPE_CHECKING:
    from src.updater.reader import Manifest


@dataclass(frozen=True, slots=True)
class CollectionPolicy:
    domain: PriceDomain | None = None
    page_size: int = 100
    safe_pages: int = 10
    request_interval: float = 1.0
    max_observation_seconds: float = 43200
    transient_replays: int = 1
    allow_deep_pages: bool = False
    network_workers: int = 2


@dataclass(frozen=True, slots=True)
class PartitionJob:
    partition_id: str
    partition: Partition
    policy: CollectionPolicy
    first_page: int = 1
    last_page: int | None = None


class RateLimitedSource:
    """Shared mutable request clock bounds starts across collection workers."""
    def __init__(self, source: ItemSource, interval: float) -> None:
        self.source, self.interval = source, interval
        self.lock, self.next_request = Lock(), 0.0

    def fetch(self, query: SearchQuery) -> SearchPage:
        with self.lock:
            time.sleep(max(0,self.next_request-time.monotonic()))
            self.next_request=time.monotonic()+self.interval
        return self.source.fetch(query)


def transient(error: SourceError) -> bool:
    return error.code in ('timeout','connection_error','transport_error') or (error.code=='http_status' and (error.status==429 or (error.status is not None and error.status>=500)))


def _fetch(source: ItemSource, query: SearchQuery, lease: Lease) -> SearchPage:
    page=source.fetch(query)
    if page.profile.profile_hash!=lease.profile.profile_hash:
        raise StoreError('profile_mismatch')
    observations_from_page(page)
    if page.observed_at.tzinfo is None or page.observed_at.utcoffset() is None:
        raise StoreError('naive_observed_at')
    return page


def prepare_collection(store: Store, lease: Lease, source: ItemSource, *, policy: CollectionPolicy) -> tuple[PartitionJob,...]:
    store.assert_owner(lease)
    if policy.page_size<1 or policy.page_size>lease.profile.page_cap or policy.safe_pages<1 or policy.transient_replays not in (0,1) or not 1 <= policy.network_workers <= 4:
        raise StoreError('invalid_collection_policy')
    encoded=json.dumps(asdict(policy),default=str,sort_keys=True)
    with store.stage(lease) as conn,conn:
        prior=conn.execute("SELECT value FROM collection_metadata WHERE key='policy'").fetchone()
        if prior is not None and prior[0]!=encoded:
            raise StoreError('incompatible_resume_policy')
        conn.execute("INSERT OR IGNORE INTO collection_metadata VALUES ('policy',?)",(encoded,))
    initial=_fetch(source,SearchQuery(page_size=policy.page_size),lease)
    if lease.environment == 'production' and initial.rows:
        pages = (initial.total_size + policy.page_size - 1) // policy.page_size
        row_bytes = sum(len(json_text(item.raw).encode()) for item in initial.rows) / len(initial.rows)
        raw = initial.raw_text if initial.raw_text is not None else json_text(initial.raw)
        page_bytes = len(gzip.compress(raw.encode(), compresslevel=6, mtime=0))
        required = max(2*1024**3, int((pages*page_bytes + initial.total_size*row_bytes*8)*1.2))
        if shutil.disk_usage(store.path('')).free < required:
            raise StoreError('insufficient_disk_space')
    with store.stage(lease) as conn:
        fixed=conn.execute('SELECT * FROM partitions ORDER BY CAST(partition_id AS INTEGER)').fetchall()
    if fixed:
        partitions=tuple(Partition(Decimal(json.loads(r['query_json'])['start_price']) if json.loads(r['query_json'])['start_price'] else None,
                                  Decimal(json.loads(r['query_json'])['end_price']) if json.loads(r['query_json'])['end_price'] else None,r['total']) for r in fixed)
    elif policy.domain is not None:
        def count(lower: Decimal,upper: Decimal) -> int:
            return _fetch(source,SearchQuery(page_size=policy.page_size,start_price=str(lower),end_price=str(upper)),lease).total_size
        partitions=plan_partitions(policy.domain,count)
    elif initial.total_size<=policy.page_size*policy.safe_pages or policy.allow_deep_pages:
        if initial.total_size > policy.page_size*policy.safe_pages:
            last = (initial.total_size + policy.page_size - 1) // policy.page_size
            seen = {entity_key(lease.profile, item.raw) for item in initial.rows}
            for number in sorted({policy.safe_pages + 1, (last + 1) // 2, last} - {1}):
                page = _fetch(source, SearchQuery(page=number, page_size=policy.page_size), lease)
                keys = {entity_key(lease.profile, item.raw) for item in page.rows}
                expected = min(policy.page_size, initial.total_size - (number-1)*policy.page_size)
                if (page.total_size != initial.total_size or page.row_total not in (None, initial.total_size)
                        or len(page.rows) != expected or len(keys) != expected or seen & keys):
                    raise StoreError('pagination_probe_failed')
                seen.update(keys)
        partitions=(Partition(None,None,initial.total_size),)
    else:
        raise StoreError('coverage_unresolved')
    if sum(p.count for p in partitions)!=initial.total_size or (not policy.allow_deep_pages and any(p.count>policy.page_size*policy.safe_pages for p in partitions)):
        raise StoreError('coverage_unresolved')
    with store.stage(lease) as conn,conn:
        conn.execute("INSERT OR IGNORE INTO collection_metadata VALUES ('observed_from',?)",(initial.observed_at.isoformat(),))
        for index,part in enumerate(partitions):
            query=SearchQuery(page_size=policy.page_size,start_price='' if part.lower is None else str(part.lower),end_price='' if part.upper is None else str(part.upper))
            conn.execute('INSERT OR IGNORE INTO partitions VALUES (?,?,?)',(str(index),json.dumps(asdict(query)),part.count))
    return tuple(PartitionJob(str(i),part,policy,first,min(first+policy.safe_pages-1,pages))
                 for i,part in enumerate(partitions)
                 for pages in ((part.count+policy.page_size-1)//policy.page_size,)
                 for first in range(1,max(1,pages)+1,policy.safe_pages))


def collect_partition(store: Store, lease: Lease, source: ItemSource, *, job: PartitionJob) -> None:
    """One partition attempt, resuming only committed pages; caller owns retry budget."""
    part,policy=job.partition,job.policy
    with store.stage(lease) as conn:
        done={r[0] for r in conn.execute('SELECT page FROM pages WHERE partition_id=? AND page>=? AND page<=?',
                                      (job.partition_id,job.first_page,job.last_page if job.last_page is not None else (part.count+policy.page_size-1)//policy.page_size))}
    last = job.last_page if job.last_page is not None else (part.count+policy.page_size-1)//policy.page_size
    for number in range(job.first_page,last+1):
        if number in done:
            continue
        store.assert_owner(lease)
        query=SearchQuery(number,policy.page_size,start_price='' if part.lower is None else str(part.lower),end_price='' if part.upper is None else str(part.upper))
        page=_fetch(source,query,lease)
        observations=observations_from_page(page)
        expected=min(policy.page_size,max(0,part.count-(number-1)*policy.page_size))
        if page.total_size!=part.count or (page.row_total is not None and page.row_total!=part.count) or len(observations)!=expected:
            raise StoreError('count_drift_or_short_page')
        if any(not part.contains(o.contract_price_decimal,None if policy.domain is None else policy.domain.quantum) for o in observations):
            raise StoreError('price_domain_violation')
        fingerprint=sha256(json.dumps(sorted(o.listing_key for o in observations)).encode()).hexdigest()
        raw = page.raw_text if page.raw_text is not None else json_text(page.raw)
        evidence = gzip.compress(raw.encode(), compresslevel=6, mtime=0) if len(raw) >= 65536 else raw
        store.assert_owner(lease)
        with store.stage(lease) as conn,conn:
            conn.execute('INSERT INTO pages VALUES (?,?,?,?,?)',(job.partition_id,number,fingerprint,page.observed_at.isoformat(),evidence))
            conn.executemany('INSERT INTO observations VALUES (?,?,?,?,?)',((o.listing_key,number,job.partition_id,json_text(o.raw),o.observed_at.isoformat()) for o in observations))


def reconcile_collection(store: Store, lease: Lease, source: ItemSource, *, policy: CollectionPolicy) -> None:
    final=_fetch(source,SearchQuery(page_size=policy.page_size),lease)
    with store.stage(lease) as conn:
        actual,earliest,latest=conn.execute('SELECT count(*),min(observed_at),max(observed_at) FROM observations').fetchone()
        total=conn.execute('SELECT sum(total) FROM partitions').fetchone()[0]
        quarantine=conn.execute('SELECT count(*) FROM quarantine').fetchone()[0]
        started=conn.execute("SELECT value FROM collection_metadata WHERE key='observed_from'").fetchone()[0]
    earliest=earliest or started
    latest=latest or final.observed_at.isoformat()
    if actual!=total or final.total_size!=total or quarantine:
        raise StoreError('coverage_unresolved')
    if (max(datetime.fromisoformat(latest),final.observed_at)-min(datetime.fromisoformat(earliest),datetime.fromisoformat(started))).total_seconds()>policy.max_observation_seconds:
        raise StoreError('observation_window_exceeded')
    validation=dict(count=actual,observed_from=earliest,observed_to=latest,coverage_complete=True,unresolved_count=0,network_workers=policy.network_workers,pending_limit=4,http_attempts=3,partition_replay=1)
    with store.control() as conn,conn:
        store.check_owner(conn,lease)
        conn.execute("UPDATE runs SET status='validated',validation_json=? WHERE run_id=?",(json.dumps(validation),lease.run_id))


def collect(store: Store, lease: Lease, source: ItemSource, *, policy: CollectionPolicy = CollectionPolicy()) -> None:
    limited=RateLimitedSource(source,max(1 if lease.environment=='production' else 0,policy.request_interval))
    jobs=iter(prepare_collection(store,lease,limited,policy=policy))
    def execute(job: PartitionJob) -> None:
        for attempt in range(policy.transient_replays+1):
            try:
                collect_partition(store,lease,limited,job=job)
                return
            except SourceError as error:
                if attempt==policy.transient_replays or not transient(error):
                    raise
    with ThreadPoolExecutor(max_workers=policy.network_workers) as pool:
        pending=[]
        while True:
            while len(pending)<4:
                job=next(jobs,None)
                if job is None:
                    break
                pending.append(pool.submit(execute,job))
            if not pending:
                break
            pending.pop(0).result()
    reconcile_collection(store,lease,limited,policy=policy)


def refresh(store: Store, source: ItemSource, *, profile: SourceProfile | None = None, policy: CollectionPolicy = CollectionPolicy(), environment: Literal['fixture','production'] = 'production') -> 'Manifest':
    from core.catalog_sources import get_profile
    from src.updater.publisher import publish
    with store.writer(profile or get_profile('shopping-all'),environment=environment) as lease:
        collect(store,lease,source,policy=policy)
        return publish(store,lease)
