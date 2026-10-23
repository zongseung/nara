"""Actual streaming synthetic catalog benchmark; never evidence of market accuracy."""
import argparse
import json
import multiprocessing
import os
import platform
import resource
import shutil
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from src.matching.catalog import open_catalog
from core.matching.contracts import InputItem, Purpose
from src.matching.datasets import freeze_split
from src.matching.engine import run_match
from src.matching.index import build_index, open_index
from src.matching.models import load_model, save_model
from core.matching.normalization import normalize_dimensions
from core.matching.policy import load_policy
from src.matching.releases import Release
from src.matching.training import train_models
from test.test_matching_models import examples
from test.test_matching_policy import policy_data
from test.test_matching_reports import prepare
from src.updater.reader import Manifest


class SyntheticCatalog:
    def __init__(self, manifest, template, rows):
        self.manifest, self.template, self.rows = manifest, template, rows
    def require_open(self):
        pass
    def candidates(self):
        for i in range(self.rows):
            yield replace(self.template, listing_key=f'bench-{i}', goods_id=f'{i:012}', goods_id_verified=True, category_id=f'cat-{i % 10000}', seller_id=f'seller-{i}', latest_contract=True, item_kind='main', environment='fixture')


def worker(job):
    root, queries, worker_id = job
    root = Path(root)
    manifest = Manifest.model_validate_json((root / 'manifest.json').read_text())
    catalog = SyntheticCatalog(manifest, None, 0)
    release = Release.model_validate_json((root / 'release.json').read_text())
    scorer = load_model(root / 'models', release.model_hash)
    elapsed = []
    counters = {'scanned': 0, 'candidate_checks': 0, 'scored': 0, 'hard_pass': 0, 'unknown': 0}
    statuses = {}
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    with open_index(root / 'index' / (manifest.snapshot_id + '.json'), catalog=catalog) as index:
        for number in range(queries):
            category = f'cat-{(number * 2 + worker_id) % 10000}'
            policy = load_policy(policy_data(category_ids=[category], state='shadow'))
            current = release.model_copy(update={'manifest': replace(release.manifest, category_id=category, policy_hash=policy.content_hash)})
            item = InputItem(f'q-{worker_id}-{number}', Purpose.FUNCTIONAL_SUBSTITUTE, category, '품목', '20x30mm', attributes=(('dimensions', normalize_dimensions('20x30mm')),), item_kind='main')
            started = time.perf_counter()
            result = run_match(item, policy, index, release=current, now=now, scorer=scorer)
            elapsed.append(time.perf_counter()-started)
            counters['scanned'] += result.retrieval.scanned_count
            counters['candidate_checks'] += len(result.candidate_checks)
            counters['scored'] += sum(check.route == 'model' and check.suitability_probability is not None for check in result.candidate_checks)
            counters['hard_pass'] += sum(all(c.status.value == 'PASS' for c in check.constraints) for check in result.candidate_checks)
            counters['unknown'] += result.retrieval.unknown_count
            status = result.decision.status.value
            statuses[status] = statuses.get(status, 0) + 1
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {'seconds': elapsed, 'rss_bytes': rss if sys.platform == 'darwin' else rss * 1024, 'statuses': statuses, 'counters': counters}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rows', type=int, required=True)
    parser.add_argument('--queries', type=int, required=True)
    parser.add_argument('--workers', type=int, required=True)
    parser.add_argument('--reuse-index', action='store_true')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if min(args.rows, args.queries, args.workers) < 1:
        parser.error('positive sizes required')
    root = args.out.parent / (args.out.stem + '-artifacts')
    root.mkdir(parents=True, exist_ok=args.reuse_index)
    free = shutil.disk_usage(root).free
    if free < args.rows * 10000 + 1_000_000_000:
        raise RuntimeError('insufficient disk preflight')
    started = time.perf_counter()
    if not args.reuse_index:
        prepare(root)
    manifest = Manifest.model_validate_json((root / 'manifest.json').read_text())
    with open_catalog(manifest, root=root / 'source') as catalog:
        template = next(catalog.candidates())
        if args.reuse_index:
            with open_index(root / 'index' / (manifest.snapshot_id + '.json'), catalog=SyntheticCatalog(manifest, template, args.rows)) as existing:
                descriptor = existing.manifest
            build_seconds = json.loads(args.out.read_text())['build_seconds']
        else:
            descriptor = build_index(SyntheticCatalog(manifest, template, args.rows), root / 'index', enable_fts=False)
            build_seconds = time.perf_counter()-started
    training = examples()
    comparison = train_models(training, freeze_split(tuple(e.label for e in training)))
    model = next(m for m in comparison.models if m.artifact.algorithm == 'logreg')
    if not (root / 'models' / model.artifact_hash).exists():
        save_model(model, root / 'models')
    release = Release.model_validate_json((root / 'release.json').read_text()).model_copy(update={'model_hash': model.artifact_hash})
    (root / 'release.json').write_text(release.model_dump_json())
    started = time.perf_counter()
    tasks = [(str(root), args.queries // args.workers + (i < args.queries % args.workers), i) for i in range(args.workers)]
    with multiprocessing.get_context('spawn').Pool(args.workers) as pool:
        results = pool.map(worker, tasks)
    query_seconds = time.perf_counter()-started
    times = sorted(value for result in results for value in result['seconds'])
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    p95 = times[min(len(times)-1, int(len(times)*.95))]
    output = {'rows_actual': descriptor.row_count, 'queries_actual': len(times), 'workers': args.workers, 'cpu_count': os.cpu_count(), 'machine': platform.platform(), 'disk_free_before': free, 'index_bytes': descriptor.path.stat().st_size, 'build_seconds': build_seconds, 'query_wall_seconds': query_seconds, 'p95_seconds': p95, 'parent_rss_bytes': rss if sys.platform == 'darwin' else rss*1024, 'worker_rss_bytes': [r['rss_bytes'] for r in results], 'statuses': [r['statuses'] for r in results], 'counters': [r['counters'] for r in results], 'scope': f'synthetic augmented fixture across 10000 categories; full engine with fixture-trained logreg CPU scoring; {descriptor.row_count} index rows; {len(times)} queries', 'targets': {'p95_5_seconds': p95 <= 5, 'hardware_budget_verified': False, 'hardware_limitation': f'{args.workers} processes on {os.cpu_count()}-CPU host; 2-vCPU/8-GiB hard limit not enforced'}, 'aggregate_rss_bytes': (rss if sys.platform == 'darwin' else rss*1024) + sum(r['rss_bytes'] for r in results), 'business_accuracy': 'not measured', 'source_snapshot_verified': False, 'artifacts': str(root), 'cleanup': 'retained task-owned SQLite/index for independent verification'}
    args.out.write_text(json.dumps(output, indent=2))
    print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
