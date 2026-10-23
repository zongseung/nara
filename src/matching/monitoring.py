"""Append-only operational observations and cell-specific safety stops."""

import argparse
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from core.matching.contracts import ContractError
from src.matching.installation import Installation, validate_assets
from src.matching.releases import OperatorContext, Release, ReleaseAuthority, read_release
from src.matching.store import MatchingStore


def status(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise ContractError('ledger', 'not found')
    with MatchingStore(path) as store:
        counts = Counter()
        unknown = 0
        constraint_count = 0
        patterns = Counter()
        cells = set()
        for request, payload in store.connection.execute('SELECT j.request,d.payload FROM jobs j JOIN decisions d ON d.job_id=j.id'):
            item = json.loads(request)['payload']
            cells.add((item['purpose'], item['category_id']))
            result = json.loads(payload)
            counts[result['decision']['status']] += 1
            for check in result['candidate_checks']:
                for constraint in check['constraints']:
                    constraint_count += 1
                    if constraint['status'] == 'UNKNOWN':
                        unknown += 1
                        patterns[constraint['field']] += 1
        total = sum(counts.values())
        reviews = store.connection.execute('SELECT count(*) FROM review_events').fetchone()[0]
        active_cells = set()
        config_state = 'not configured'
        from src.matching.installation import installation
        from src.updater.locks import maintenance_active
        try:
            config = installation()
            pointer = config.authority_root / 'active-release.json'
            config_state = 'maintenance' if maintenance_active(config.source_root) else 'configured'
            if pointer.exists() and config_state != 'maintenance':
                active = read_release(pointer)
                ReleaseAuthority(config.authority_root).require_active(active)
                require_cell_active(config, active)
                active_cells.add((active.manifest.purpose.value, active.manifest.category_id))
        except FileNotFoundError:
            pass
        except ContractError as exc:
            config_state = str(exc)
        return {'jobs': total, 'installation_state': config_state, 'active_cells': sorted(active_cells), 'nonactivated_cells': sorted(cells - active_cells), 'unknown_constraint_rate': unknown / constraint_count if constraint_count else None, 'statuses': dict(counts), 'match_coverage': counts['auto_confirmed'] / total if total else None, 'auto_close_coverage': (counts['auto_confirmed'] + counts['auto_no_match']) / total if total else None, 'review_required': counts['review_required'], 'review_events': reviews, 'errors': counts['processing_failed'] + counts['invalid_input'], 'unknown_constraints': unknown, 'unknown_fields': dict(patterns), 'observed_cells': sorted(cells), 'new_pattern_rate': None, 'new_pattern_limitation': 'requires a frozen representative baseline; not inferred from all-review output', 'business_accuracy': None}


def _events(config: Installation) -> sqlite3.Connection:
    config.authority_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(config.authority_root / 'operations.sqlite')
    connection.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, purpose TEXT NOT NULL, category TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, principal TEXT NOT NULL, observed_at TEXT NOT NULL)')
    for action in ('UPDATE', 'DELETE'):
        connection.execute(f"CREATE TRIGGER IF NOT EXISTS events_{action.lower()} BEFORE {action} ON events BEGIN SELECT RAISE(ABORT,'append-only'); END")
    connection.commit()
    return connection


def require_cell_active(config: Installation, release: Release) -> None:
    connection = _events(config)
    try:
        row = connection.execute('SELECT state FROM events WHERE purpose=? AND category=? ORDER BY id DESC LIMIT 1', (release.manifest.purpose.value, release.manifest.category_id)).fetchone()
        if row and row[0] == 'shadow':
            raise ContractError('cell', 'operational audit/rollback requires shadow')
    finally:
        connection.close()


def operate(args: argparse.Namespace, config: Installation, identity: OperatorContext) -> None:
    identity.require('evaluator')
    if args.command == 'audit':
        with MatchingStore(config.ledger) as ledger:
            row = ledger.connection.execute('SELECT r.payload,j.config,d.payload FROM review_events r JOIN jobs j ON j.id=r.job_id JOIN decisions d ON d.job_id=r.job_id WHERE r.id=?', (args.review_id,)).fetchone()
        if row is None or json.loads(row[0])['target'] is not False or json.loads(row[2])['decision']['status'] != 'auto_confirmed':
            raise ContractError('audit', 'requires a trusted negative review of an auto-confirmed decision')
        release = Release.model_validate_json(row[1])
        purpose, category, state, reason = release.manifest.purpose.value, release.manifest.category_id, 'shadow', 'confirmed false positive: ' + args.review_id
    else:
        purpose, category, state, reason = args.purpose, args.category, 'shadow', 'operator rollback to shadow'
        if args.release:
            release = read_release(args.release)
            if (release.manifest.purpose.value, release.manifest.category_id) != (purpose, category):
                raise ContractError('rollback', 'cell mismatch')
            validate_assets(config, release)
            if release.certificate is None:
                raise ContractError('rollback', 'certificate required')
            evidence = config.authority_root / (release.certificate.evaluation_hash + '.evaluation.json')
            ReleaseAuthority(config.authority_root).activate(release, evidence, operator=identity)
            state, reason = 'active', release.content_hash
    connection = _events(config)
    try:
        with connection:
            connection.execute('INSERT INTO events(purpose,category,state,reason,principal,observed_at) VALUES (?,?,?,?,?,?)', (purpose, category, state, reason, identity.principal, datetime.now(UTC).isoformat()))
    finally:
        connection.close()
    print(json.dumps({'purpose': purpose, 'category': category, 'state': state, 'reason': reason}))
