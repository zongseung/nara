import argparse
import gzip
import json
from math import ceil
from pathlib import Path
import time

from core.catalog_json import json_text
from core.catalog_sources import SearchQuery, get_profile
from src.g2b_client import fetch_search_page
from src.updater.collector import CollectionPolicy, refresh
from src.updater.exports import export_xlsx
from src.updater.reader import open_published_snapshot
from src.updater.store import Store, database
from test.updater_fixtures import FixtureSource


def main() -> None:
    parser = argparse.ArgumentParser(description='Measure a live sample in an isolated fixture store; never publish operational data.')
    parser.add_argument('--out-dir', type=Path, required=True)
    args = parser.parse_args()
    root: Path = args.out_dir
    root.mkdir(parents=True, exist_ok=False)
    profile = get_profile('shopping-all')
    pages = []
    durations = []
    started = time.monotonic()
    first = fetch_search_page(SearchQuery(), profile)
    durations.append(time.monotonic() - started)
    last = ceil(first.total_size / 100)
    rows = []
    raw_bytes = compressed_bytes = 0
    numbers = sorted({1, max(1,last//4), max(1,last//2), max(1,3*last//4), last})
    for number in numbers:
        started = time.monotonic()
        page = first if number == 1 else fetch_search_page(SearchQuery(page=number), profile)
        if number != 1:
            durations.append(time.monotonic() - started)
        if page.total_size != first.total_size:
            raise RuntimeError('source_count_changed_during_measurement')
        wire = (page.raw_text or json_text(page.raw)).encode()
        compressed = gzip.compress(wire, compresslevel=6, mtime=0)
        (root / f'page-{number}.json.gz').write_bytes(compressed)
        raw_bytes += len(wire)
        compressed_bytes += len(compressed)
        rows.extend(dict(item.raw) for item in page.rows)
        pages.append(dict(page=number, rows=len(page.rows), response_bytes=len(wire), gzip_bytes=len(compressed)))
        print(json.dumps(pages[-1]), flush=True)
    keys = {row['ctrtItemMngNo'] for row in rows}
    if len(keys) != len(rows):
        raise RuntimeError('repeated_sample_item')
    store = Store(root / 'fixture-format-measurement')
    manifest = refresh(store, FixtureSource(rows), environment='fixture',
                       policy=CollectionPolicy(request_interval=0))
    with database(store.path(f'stage/{manifest.publication_id}.sqlite')) as conn:
        stage_without_pages = sum(record[0] for record in conn.execute(
            "SELECT sum(pgsize) FROM dbstat WHERE name NOT IN ('pages','sqlite_autoindex_pages_1','sqlite_autoindex_pages_2')"))
    snapshot_bytes = store.path(manifest.snapshot_path).stat().st_size
    output = root / 'sample.xlsx'
    with open_published_snapshot(manifest, root=store.root) as snapshot:
        export_xlsx(snapshot, output)
    scale = first.total_size / len(rows)
    stage_estimate = ceil(stage_without_pages * scale + compressed_bytes / len(pages) * last)
    snapshot_estimate = ceil(snapshot_bytes * scale)
    report = dict(observed_at=first.observed_at.isoformat(), total_items=first.total_size,
                  sample_items=len(rows), sample_pages=pages, image_files_included=False,
                  snapshot_sample_bytes=snapshot_bytes, xlsx_sample_bytes=output.stat().st_size,
                  estimated_initial_snapshot_bytes=snapshot_estimate,
                  estimated_staging_bytes=stage_estimate,
                  estimated_response_bytes=ceil(raw_bytes / len(pages) * last),
                  estimated_initial_xlsx_bytes=ceil(output.stat().st_size * scale),
                  recommended_free_bytes=ceil((stage_estimate+snapshot_estimate*3)*1.2),
                  observed_request_seconds=durations,
                  estimated_four_worker_network_hours=last*sum(durations)/len(durations)/4/3600,
                  estimation_only=True, production_full_collection_verified=False)
    (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
