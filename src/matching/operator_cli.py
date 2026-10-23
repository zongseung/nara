"""Authenticated review, training and evaluation command handlers."""

import argparse
import json
import pwd
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from core.matching.contracts import Candidate, ContractError, InputItem, loads_contract
from src.matching.datasets import SplitManifest, import_labels
from src.matching.installation import Installation, installation, operator
from src.matching.releases import OperatorContext
from src.matching.reviews import Review
from src.matching.store import MatchingStore


class PairRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    request: dict[str, JsonValue]
    candidate: dict[str, JsonValue]


class CaseEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)
    request_id: str = Field(min_length=1)
    holdout: Literal['representative', 'supplier', 'subsequent_time']
    absence_verified: bool | None
    evidence_hash: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def require_evidence(self):
        if (self.holdout != 'representative' or self.absence_verified is not None) and self.evidence_hash is None:
            raise ValueError('tagged holdout/known absence requires genuine evidence hash')
        return self


class CaseManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    split_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    cases: tuple[CaseEvidence, ...]


def export_reviews(ledger: Path, output: Path) -> None:
    with MatchingStore(ledger) as store, output.open('x', encoding='utf-8') as stream:
        for job, candidate, evidence, payload in store.connection.execute('SELECT job_id,candidate_id,evidence_hash,payload FROM candidate_checks ORDER BY job_id,candidate_id'):
            stream.write(json.dumps({'job_id': job, 'candidate_id': candidate, 'evidence_hash': evidence, 'evidence': json.loads(payload), 'instruction': 'Submit Review JSONL with review_id,target,reason,reviewed_at,expires_at; evidence is read-only; approval remains pending.'}, ensure_ascii=False) + '\n')


def execute(args: argparse.Namespace) -> None:
    config = installation()
    identity = operator(config)
    identity.require('reviewer' if args.command in ('labels-import', 'review-import') else 'evaluator')
    from src.updater.locks import file_lock
    with file_lock(config.ledger.with_suffix('.operations.lock')):
        _execute(args, config, identity)


def _execute(args: argparse.Namespace, config: Installation, identity: OperatorContext) -> None:
    match args.command:
        case 'labels-import':
            labels = import_labels(args.input, trusted_reviewers=frozenset({identity.principal}))
            with MatchingStore(config.ledger) as store:
                store.import_labels(labels, identity)
            print(json.dumps({'labels_imported': len(labels)}))
        case 'review-import':
            reviews = tuple(Review.model_validate_json(line) for line in args.input.read_text().splitlines() if line.strip())
            with MatchingStore(config.ledger) as store:
                for review in reviews:
                    store.import_review(review, identity, now=datetime.now(UTC))
            print(json.dumps({'reviews_imported': len(reviews), 'registry_active': False}))
        case 'train' | 'evaluate':
            _model_command(args, config, identity)
        case 'backup' | 'restore' | 'resume':
            from src.matching.backup import backup, restore, resume
            match args.command:
                case 'backup': backup(config, args.path)
                case 'restore': restore(config, args.path)
                case 'resume': resume(config)
        case 'rollback' | 'audit':
            from src.matching.monitoring import operate
            operate(args, config, identity)
        case _:
            raise ContractError('command', 'unsupported operator operation')


def _model_command(args: argparse.Namespace, config: Installation, identity: OperatorContext) -> None:
    from core.matching.features import Pair
    from src.matching.models import load_model, save_model
    from src.matching.training import Example, train_models
    reviewers = frozenset(pwd.getpwuid(int(uid)).pw_name for uid, roles in config.roles_by_uid.items() if 'reviewer' in roles)
    labels = import_labels(args.labels, trusted_reviewers=reviewers)
    split = TypeAdapter(SplitManifest).validate_json(args.split.read_text())
    pairs = {}
    for line in args.pairs.read_text().splitlines():
        try:
            raw = PairRecord.model_validate_json(line)
        except ValidationError as exc:
            raise ContractError('pairs', 'malformed typed pair record') from exc
        request, candidate = loads_contract(json.dumps(raw.request)), loads_contract(json.dumps(raw.candidate))
        if not isinstance(request, InputItem) or not isinstance(candidate, Candidate):
            raise ContractError('pairs', 'typed request/candidate required')
        key = request.input_row_id, candidate.listing_key
        if key in pairs:
            raise ContractError('pairs', 'duplicate pair')
        pairs[key] = Pair(request, candidate)
    if any((label.request_id, label.candidate_id) not in pairs for label in labels):
        raise ContractError('pairs', 'missing pair for reviewed label')
    examples = tuple(Example(pairs[(label.request_id, label.candidate_id)], label) for label in labels)
    with MatchingStore(config.ledger) as store:
        store.record_split(split)
        if args.command == 'train':
            comparison = train_models(examples, split)
            _freeze_dataset(args, config, split)
            artifacts = [str(save_model(model, config.artifact_root)) for model in comparison.models]
            payload = {'artifacts': artifacts, 'champion_hash': comparison.champion.artifact_hash, 'business_quality': comparison.business_quality}
            with args.out.open('x') as stream:
                json.dump(payload, stream, indent=2)
            print(json.dumps(payload))
            return
        from src.matching.catalog import open_catalog
        from src.matching.engine import evaluate_candidate
        from src.matching.evaluation import (
            EvaluationCase,
            case_metadata_hash,
            evaluate_final,
        )
        from src.matching.index import build_index, open_index
        from core.matching.policy import PolicyRegistry
        from src.matching.policy import read_policy
        from src.matching.releases import read_release
        from src.matching.reviews import read_registry
        release = read_release(args.release)
        policy = PolicyRegistry(tuple(read_policy(p) for p in config.policy_dir.glob('*.json'))).get(release.manifest.purpose, release.manifest.category_id)
        if policy is None:
            raise ContractError('policy', 'unsupported cell')
        scorer = load_model(config.artifact_root, release.model_hash) if release.model_hash else None
        registry = read_registry(config.artifact_root / (release.registry_hash + '.registry.json'), expected_hash=release.registry_hash) if release.registry_hash else None
        tests = {label_id for label_id, partition in split.assignments if partition == 'test'}
        requests = {example.pair.request.input_row_id: example.pair.request for example in examples if example.label.label_id in tests}
        cases = tuple(EvaluationCase(request, tuple(label for label in labels if label.request_id == key and label.label_id in tests)) for key, request in requests.items())
        if args.cases:
            metadata = CaseManifest.model_validate_json(args.cases.read_text())
            by_request = {case.request_id: case for case in metadata.cases}
            if release.manifest.dataset_hash != split.content_hash or metadata.split_hash != split.content_hash or set(by_request) != set(requests) or len(by_request) != len(metadata.cases):
                raise ContractError('cases', 'split hash and exact frozen request coverage required')
            cases = tuple(EvaluationCase(case.request, case.labels, by_request[case.request.input_row_id].absence_verified, by_request[case.request.input_row_id].holdout, by_request[case.request.input_row_id].evidence_hash) for case in cases)
            digest = case_metadata_hash(split, cases)
            frozen = config.dataset_root / split.content_hash / 'evaluation-cases'
            frozen.mkdir(parents=True, exist_ok=True)
            target = frozen / (digest + '.json')
            encoded = metadata.model_dump_json()
            if target.exists() and target.read_text() != encoded:
                raise ContractError('cases', 'immutable case evidence collision')
            if not target.exists():
                with target.open('x') as stream:
                    stream.write(encoded)

        _freeze_dataset(args, config, split)
        now = datetime.now(UTC)
        with open_catalog(args.catalog_manifest, root=config.source_root) as catalog:
            descriptor = build_index(catalog, config.index_dir)
            with open_index(descriptor, catalog=catalog) as index:
                def runner(item):
                    return evaluate_candidate(item, policy, index, release=release, now=now, operator=identity, scorer=scorer, registry=registry)
                report = evaluate_final(cases, release, runner, split=split, store=store, operator=identity, registry=registry)
        report.write(args.out)
        print(json.dumps({'report': str(args.out), 'eligibility': report.eligibility}))


def _freeze_dataset(args: argparse.Namespace, config: Installation, split: SplitManifest) -> None:
    frozen = config.dataset_root / split.content_hash
    frozen.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name, source in (('labels.jsonl', args.labels), ('pairs.jsonl', args.pairs), ('split.json', args.split)):
        content = source.read_bytes()
        target = frozen / name
        if target.exists():
            if target.read_bytes() != content:
                raise ContractError('dataset', 'immutable dataset collision')
        else:
            with target.open('xb') as stream:
                stream.write(content)
        hashes[name] = sha256(content).hexdigest()
    manifest = frozen / 'files.json'
    encoded = json.dumps(hashes, sort_keys=True)
    if manifest.exists() and manifest.read_text() != encoded:
        raise ContractError('dataset', 'immutable manifest collision')
    if not manifest.exists():
        with manifest.open('x') as stream:
            stream.write(encoded)
