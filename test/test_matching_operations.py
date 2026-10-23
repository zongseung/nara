import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src.matching.backup import backup, restore
from core.matching.contracts import ContractError
from src.matching.datasets import freeze_split
from src.matching.installation import Installation, installation, operator
from src.matching.models import load_model, save_model
from src.matching.training import train_models
from test.test_matching_models import examples
from test.test_matching_reports import prepare
from src.updater.locks import maintenance_active


def configure(root):
    config = Installation(source_root=root/'source', authority_root=root/'authority', artifact_root=root/'artifacts', ledger=root/'matching.sqlite', policy_dir=root/'policies', dataset_root=root/'datasets', index_dir=root/'index', roles_by_uid={str(os.geteuid()): frozenset({'reviewer', 'evaluator'})})
    for path in (config.authority_root, config.artifact_root, config.dataset_root):
        path.mkdir(exist_ok=True, mode=0o700)
    path = root/'operator.json'
    path.write_text(config.model_dump_json())
    path.chmod(0o600)
    return config, path


class OperationsTests(unittest.TestCase):
    def test_default_configuration_loads_existing_user_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, supplied = configure(root)
            home = root / 'home'
            target = home / '.config' / 'nara' / 'matching.json'
            target.parent.mkdir(parents=True)
            target.write_bytes(supplied.read_bytes())
            target.chmod(0o600)
            with patch.dict(os.environ, {'HOME': str(home)}):
                os.environ.pop('NARA_MATCHING_CONFIG', None)
                self.assertEqual(installation(), config)

    def test_protected_identity_cannot_be_taken_from_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepare(root)
            _config, path = configure(root)
            with patch.dict(os.environ, {'NARA_MATCHING_CONFIG': str(path)}):
                self.assertEqual(operator(installation()).principal, pwd.getpwuid(os.geteuid()).pw_name)
                path.chmod(0o666)
                with self.assertRaises(ContractError):
                    installation()

    def test_real_binary_backup_restore_and_tamper_gate(self):
        from src.matching.store import MatchingStore
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepare(root)
            config, _ = configure(root)
            values = examples()
            comparison = train_models(values, freeze_split(tuple(e.label for e in values)))
            model = next(m for m in comparison.models if m.artifact.algorithm == 'catboost')
            directory = save_model(model, config.artifact_root)
            expected = model.probability(values[-1].pair)
            with MatchingStore(config.ledger):
                pass
            backup(config, root/'backup')
            self.assertTrue((root/'backup'/'artifacts'/model.artifact_hash/'model.cbm').is_file())
            shutil.rmtree(directory)
            restore(config, root/'backup')
            self.assertAlmostEqual(load_model(config.artifact_root, model.artifact_hash).probability(values[-1].pair), expected)
            self.assertTrue(maintenance_active(config.source_root))
            (root/'backup'/'artifacts'/model.artifact_hash/'model.cbm').write_bytes(b'corrupt')
            with self.assertRaises(ContractError):
                restore(config, root/'backup')
            self.assertTrue(maintenance_active(config.source_root))

    def test_actual_cli_train_saves_vocabulary_calibrator_binary(self):
        from core.matching.contracts import dumps_contract
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepare(root)
            config, config_path = configure(root)
            principal = pwd.getpwuid(os.geteuid()).pw_name
            values = tuple(replace(e, label=replace(e.label, reviewer=principal)) for e in examples())
            labels = root/'labels.jsonl'
            labels.write_text(''.join(json.dumps(e.label.to_payload())+'\n' for e in values))
            pairs = root/'pairs.jsonl'
            pairs.write_text(''.join(json.dumps({'request': json.loads(dumps_contract(e.pair.request)), 'candidate': json.loads(dumps_contract(e.pair.candidate))})+'\n' for e in values))
            freeze_split(tuple(e.label for e in values)).write(root/'split.json')
            result = subprocess.run([sys.executable, '-m', 'src.matching.cli', 'train', '--labels', str(labels), '--pairs', str(pairs), '--split', str(root/'split.json'), '--out', str(root/'training.json')], env={**os.environ, 'NARA_MATCHING_CONFIG': str(config_path)}, check=False, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(list(config.artifact_root.glob('*/model.cbm'))), 1)
            self.assertEqual(len(json.loads((root/'training.json').read_text())['artifacts']), 3)

    def test_real_cli_evaluate_covers_all_frozen_holdout_once(self):
        from core.matching.contracts import dumps_contract
        from src.matching.releases import read_release, write_release
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepare(root)
            _config, config_path = configure(root)
            principal = pwd.getpwuid(os.geteuid()).pw_name
            values = tuple(replace(e, pair=replace(e.pair, request=replace(e.pair.request, category_id='123'), candidate=replace(e.pair.candidate, category_id='123')), label=replace(e.label, reviewer=principal, category_id='123')) for e in examples())
            (root/'labels.jsonl').write_text(''.join(json.dumps(e.label.to_payload())+'\n' for e in values))
            (root/'pairs.jsonl').write_text(''.join(json.dumps({'request': json.loads(dumps_contract(e.pair.request)), 'candidate': json.loads(dumps_contract(e.pair.candidate))})+'\n' for e in values))
            split = freeze_split(tuple(e.label for e in values))
            split.write(root/'split.json')
            release = read_release(root/'release.json').model_copy(update={'state': 'candidate'})
            write_release(release, root/'candidate.json')
            env = {**os.environ, 'NARA_MATCHING_CONFIG': str(config_path)}
            command = [sys.executable, '-m', 'src.matching.cli', 'evaluate', '--labels', str(root/'labels.jsonl'), '--pairs', str(root/'pairs.jsonl'), '--split', str(root/'split.json'), '--release', str(root/'candidate.json'), '--catalog-manifest', str(root/'manifest.json'), '--out', str(root/'evaluation.json')]
            result = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((root/'evaluation.json').read_text())
            self.assertEqual(report['total'], 8)
            self.assertNotEqual(report['eligibility'], 'eligible')
            result = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('consumed', result.stderr)

    def test_validated_negative_auto_audit_shadows_only_its_cell(self):
        import argparse
        from datetime import timedelta

        from core.matching.contracts import DecisionStatus
        from src.matching.engine import run_match
        from src.matching.monitoring import operate, require_cell_active
        from src.matching.reviews import Review
        from src.matching.store import MatchingStore
        from test.test_matching_engine import NOW, OPERATOR, pipeline
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with pipeline(root) as (item, policy, release, index):
                config, _ = configure(root)
                original = run_match(item, policy, index, release=release, now=NOW)
                result = replace(original, decision=replace(original.decision, status=DecisionStatus.AUTO_CONFIRMED))
                with MatchingStore(config.ledger) as ledger:
                    job = ledger.record_job(item, result, release)
                    candidate, evidence = ledger.connection.execute('SELECT candidate_id,evidence_hash FROM candidate_checks').fetchone()
                    ledger.import_review(Review(review_id='audit-fixture', job_id=job, candidate_id=candidate, evidence_hash=evidence, target=False, reason='fixture mandatory mismatch', reviewed_at=NOW, expires_at=NOW+timedelta(days=1)), OPERATOR, now=NOW)
                operate(argparse.Namespace(command='audit', review_id='audit-fixture'), config, OPERATOR)
                with self.assertRaises(ContractError):
                    require_cell_active(config, release)
                other = release.model_copy(update={'manifest': replace(release.manifest, category_id='other')})
                require_cell_active(config, other)
                import sqlite3
                with sqlite3.connect(config.authority_root/'operations.sqlite') as connection, self.assertRaises(sqlite3.IntegrityError):
                    connection.execute('DELETE FROM events')

    def test_malformed_backup_manifest_is_typed_and_gate_stays_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepare(root)
            config, _ = configure(root)
            malformed = root/'malformed'
            malformed.mkdir()
            (malformed/'matching-backup.json').write_text('[]')
            with self.assertRaises(ContractError):
                restore(config, malformed)
            self.assertTrue(maintenance_active(config.source_root))

    def test_missing_training_pair_is_concise_boundary_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepare(root)
            _config, config_path = configure(root)
            principal = pwd.getpwuid(os.geteuid()).pw_name
            labels = tuple(replace(e.label, reviewer=principal) for e in examples())
            (root/'labels.jsonl').write_text(''.join(json.dumps(label.to_payload())+'\n' for label in labels))
            (root/'pairs.jsonl').write_text('')
            freeze_split(labels).write(root/'split.json')
            result = subprocess.run([sys.executable, '-m', 'src.matching.cli', 'train', '--labels', str(root/'labels.jsonl'), '--pairs', str(root/'pairs.jsonl'), '--split', str(root/'split.json'), '--out', str(root/'training.json')], env={**os.environ, 'NARA_MATCHING_CONFIG': str(config_path)}, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('missing pair', result.stderr)
            self.assertNotIn('Traceback', result.stderr)

    def test_auto_rejects_caller_index_root_before_release_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            options = prepare(root)
            config, config_path = configure(root)
            evil = root/'caller-index'
            evil.mkdir()
            options[options.index('--index-dir')+1] = str(evil)
            result = subprocess.run([sys.executable, 'compare_file.py', str(root/'input.xlsx'), '--mode', 'auto', *options, '--out', str(root/'auto.xlsx')], env={**os.environ, 'NARA_MATCHING_CONFIG':str(config_path)}, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('configured protected index', result.stderr)
            self.assertFalse((root/'auto.xlsx').exists())
            config.index_dir.mkdir()
            config.index_dir.chmod(0o777)
            with patch.dict(os.environ, {'NARA_MATCHING_CONFIG':str(config_path)}), self.assertRaises(ContractError):
                installation()

    def test_cli_case_metadata_freezes_all_holdouts_and_binds_report(self):
        import sqlite3
        from hashlib import sha256

        from core.matching.contracts import dumps_contract
        from src.matching.releases import EvaluationReport, read_release, write_release
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepare(root)
            config, config_path = configure(root)
            principal = pwd.getpwuid(os.geteuid()).pw_name
            values = tuple(replace(e, pair=replace(e.pair, request=replace(e.pair.request, category_id='123'), candidate=replace(e.pair.candidate, category_id='123')), label=replace(e.label, reviewer=principal, category_id='123')) for e in examples())
            (root/'labels.jsonl').write_text(''.join(json.dumps(e.label.to_payload())+'\n' for e in values))
            (root/'pairs.jsonl').write_text(''.join(json.dumps({'request':json.loads(dumps_contract(e.pair.request)),'candidate':json.loads(dumps_contract(e.pair.candidate))})+'\n' for e in values))
            split = freeze_split(tuple(e.label for e in values))
            split.write(root/'split.json')
            base = read_release(root/'release.json')
            write_release(base.model_copy(update={'state':'candidate', 'manifest':replace(base.manifest, dataset_hash=split.content_hash)}),root/'candidate.json')
            entries = [{'request_id':label.request_id,'holdout':'supplier' if i==0 else 'subsequent_time' if i==1 else 'representative','absence_verified':False,'evidence_hash':label.evidence_hash} for i,label in enumerate(split.test_labels)]
            metadata = {'split_hash':split.content_hash,'cases':entries}
            (root/'cases.json').write_text(json.dumps({**metadata,'cases':entries[:-1]}))
            command = [sys.executable,'-m','src.matching.cli','evaluate','--labels',str(root/'labels.jsonl'),'--pairs',str(root/'pairs.jsonl'),'--split',str(root/'split.json'),'--release',str(root/'candidate.json'),'--catalog-manifest',str(root/'manifest.json'),'--cases',str(root/'cases.json'),'--out',str(root/'evaluation.json')]
            env = {**os.environ,'NARA_MATCHING_CONFIG':str(config_path)}
            invalid = subprocess.run(command,env=env,capture_output=True,text=True,check=False)
            self.assertNotEqual(invalid.returncode,0)
            with sqlite3.connect(config.ledger) as ledger:
                self.assertEqual(ledger.execute('SELECT count(*) FROM evaluations').fetchone()[0],0)
            (root/'cases.json').write_text(json.dumps(metadata))
            result = subprocess.run(command,env=env,capture_output=True,text=True,check=False)
            self.assertEqual(result.returncode,0,result.stderr)
            report = EvaluationReport.model_validate_json((root/'evaluation.json').read_text())
            expected = sha256(json.dumps({'split_hash':split.content_hash,'cases':sorted(entries,key=lambda entry:entry['request_id'])},sort_keys=True,separators=(',',':')).encode()).hexdigest()
            self.assertEqual(report.case_metadata_hash,expected)
            self.assertEqual(dict(report.holdouts)['supplier'],'1')
            self.assertEqual(dict(report.holdouts)['time'],'1')
            self.assertEqual(report.total,8)
            self.assertTrue((config.dataset_root/split.content_hash/'evaluation-cases'/f'{expected}.json').is_file())
            self.assertNotEqual(report.content_hash,report.model_copy(update={'case_metadata_hash':'0'*64}).content_hash)
