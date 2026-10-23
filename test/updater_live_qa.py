import json
from pathlib import Path

from core.catalog_sources import get_profile
from src.updater.collector import CollectionPolicy, prepare_collection, collect_partition
from src.updater.source import ShoppingSource
from src.updater.store import Store


def main() -> None:
    root = Path('.omo/collection-readiness/live-smoke')
    root.mkdir(parents=True, exist_ok=False)
    store = Store(root)
    source = ShoppingSource()
    policy = CollectionPolicy(allow_deep_pages=True, safe_pages=2, network_workers=4)
    with store.writer(get_profile('shopping-all')) as lease:
        jobs = prepare_collection(store, lease, source, policy=policy)
        collect_partition(store, lease, source, job=jobs[0])
        with store.stage(lease) as conn:
            observed = conn.execute('SELECT count(*) FROM observations').fetchone()[0]
            total = conn.execute('SELECT sum(total) FROM partitions').fetchone()[0]
            pages = conn.execute('SELECT count(*) FROM pages').fetchone()[0]
        with store.control() as conn:
            publications = conn.execute('SELECT count(*) FROM publications').fetchone()[0]
        if observed != 200 or pages != 2 or publications != 0:
            raise RuntimeError('live_partial_collection_failed')
        report = dict(run_id=lease.run_id, expected_items=total, collected_items=observed,
                      completed_pages=pages, planned_chunks=len(jobs),
                      partial_only=True, operational_publications=publications)
        (root / 'report.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
