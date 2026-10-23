import json
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from openpyxl import load_workbook

from test.test_matching_operations import configure
from test.test_matching_reports import prepare
from test.updater_fixtures import FixtureSource, row
from src.updater.collector import CollectionPolicy, refresh
from src.updater.store import Store


def write_xls(path):
    def record(code, data):
        return struct.pack('<HH', code, len(data)) + data
    payload = record(9, struct.pack('<HH', 2, 16)) + record(0x42, struct.pack('<H', 949))
    for i, values in enumerate([['물품식별번호', '한글품명', '희망가(부가세포함)'], ['00001', '품목 20x30mm', '100']]):
        for j, value in enumerate(values):
            raw = value.encode('cp949')
            payload += record(4, struct.pack('<HH', i, j) + b'\0\0\0' + bytes([len(raw)]) + raw)
    path.write_bytes(payload + record(10, b''))


def scenario(root):
    options = prepare(root)
    config, config_path = configure(root)
    env = {**os.environ, 'NARA_MATCHING_CONFIG': str(config_path)}
    # sitecustomize blocks every network connection in actual subprocesses.
    guard = root/'network-guard'
    guard.mkdir()
    (guard/'sitecustomize.py').write_text('import socket\ndef blocked(*a, **k):\n    raise AssertionError("NETWORK_FORBIDDEN")\nsocket.socket.connect=blocked\nsocket.create_connection=blocked\n')
    env['PYTHONPATH'] = str(guard) + os.pathsep + str(Path.cwd())
    commands = []
    def run(arguments, expected=0):
        result = subprocess.run([sys.executable, *arguments], env=env, check=False, capture_output=True, text=True)
        commands.append({'command': [sys.executable, *arguments], 'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
        if result.returncode != expected:
            raise AssertionError(commands[-1])
        return result
    write_xls(root/'input.xls')
    for extension in ('xls', 'xlsx'):
        run(['compare_file.py', str(root/f'input.{extension}'), '--mode', 'shadow', *options, '--out', str(root/f'{extension}-report.xlsx')])
    run(['-m', 'src.matching.cli', 'review-export', '--ledger', str(config.ledger), '--out', str(root/'review-export.jsonl')])
    with sqlite3.connect(config.ledger) as ledger:
        xls_job = next(job for job, raw in ledger.execute('SELECT id,request FROM jobs') if json.loads(raw)['payload']['file_path'].endswith('.xls'))
    exported = next(json.loads(line) for line in (root/'review-export.jsonl').read_text().splitlines() if json.loads(line)['job_id'] == xls_job)
    now = datetime.now(UTC)
    review = {key: exported[key] for key in ('job_id', 'candidate_id', 'evidence_hash')}
    review.update(review_id='manual-fixture-review', target=True, reason='fixture software QA only', reviewed_at=now.isoformat(), expires_at=(now+timedelta(days=1)).isoformat())
    (root/'review.jsonl').write_text(json.dumps(review)+'\n')
    for _ in range(2):
        run(['-m', 'src.matching.cli', 'review-import', str(root/'review.jsonl')])
    run(['compare_file.py', str(root/'input.xls'), '--mode', 'shadow', *options, '--out', str(root/'reviewed-report.xlsx')])
    with sqlite3.connect(config.ledger) as ledger:
        assert ledger.execute('SELECT count(*) FROM review_events').fetchone()[0] == 1
        assert ledger.execute('SELECT count(*) FROM decisions').fetchone()[0] == 2
    workbook = load_workbook(root/'reviewed-report.xlsx')
    resolution = [tuple(row) for row in workbook['검토필요'].values]
    workbook.close()
    assert any('human_confirmed' in row for row in resolution)
    changed = refresh(Store(config.source_root), FixtureSource([row('A', price=175, spec='20x30mm')], tick=1), policy=CollectionPolicy(request_interval=0), environment='fixture')
    (root/'manifest.json').write_text(changed.model_dump_json())
    run(['compare_file.py', str(root/'input.xls'), '--mode', 'shadow', *options, '--out', str(root/'updated-report.xlsx')])
    workbook = load_workbook(root/'updated-report.xlsx')
    company = next(sheet for sheet in workbook if sheet.title.startswith('업체_'))
    assert company['J2'].value == 175
    workbook.close()
    auto = subprocess.run([sys.executable, 'compare_file.py', str(root/'input.xls'), '--mode', 'auto', *options, '--out', str(root/'auto.xlsx')], env=env, check=False, capture_output=True, text=True)
    assert auto.returncode != 0 and not (root/'auto.xlsx').exists()
    commands.append({'command': auto.args, 'exit_code': auto.returncode, 'stderr': auto.stderr})
    run(['-m', 'src.matching.cli', 'status', '--ledger', str(config.ledger)])
    run(['-m', 'src.matching.cli', 'rollback', '--purpose', 'functional_substitute', '--category', '123'])
    (root/'commands.json').write_text(json.dumps(commands, ensure_ascii=False, indent=2))
    return {'xls_xlsx': True, 'review_idempotent': True, 'review_pending_not_activated': True, 'updated_price': 175, 'auto_fixture_rejected': True, 'network_connections': 0, 'commands': str(root/'commands.json')}


class EndToEnd(unittest.TestCase):
    def test_published_source_to_review_and_new_price(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(scenario(Path(temporary))['updated_price'], 175)
