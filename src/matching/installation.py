"""Protected installation identity and release asset validation."""

import json
import os
import pwd
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from core.matching.contracts import ContractError
from src.matching.releases import EvaluationReport, OperatorContext, Release


class Installation(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    source_root: Path
    authority_root: Path
    artifact_root: Path
    ledger: Path
    policy_dir: Path
    dataset_root: Path
    index_dir: Path
    roles_by_uid: dict[str, frozenset[Literal["reviewer", "evaluator"]]]


def installation() -> Installation:
    path = Path(os.environ.get('NARA_MATCHING_CONFIG', '~/.config/nara/matching.json')).expanduser()
    if path.is_symlink():
        raise ContractError('installation', 'symlink configuration forbidden')
    stat = path.stat()
    if stat.st_uid not in (0, os.geteuid()) or stat.st_mode & 0o022:
        raise ContractError('installation', 'configuration must be operator-owned and not group/world writable')
    config = Installation.model_validate_json(path.read_text())
    for root in (config.authority_root, config.artifact_root, config.policy_dir, config.dataset_root, config.index_dir):
        if not root.is_absolute() or root.is_symlink():
            raise ContractError('installation', 'protected roots must be absolute real paths')
        existing = root
        while not existing.exists():
            existing = existing.parent
        if existing.exists():
            info = existing.stat()
            if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
                raise ContractError('installation', 'unsafe artifact root permissions')
    return config


def operator(config: Installation) -> OperatorContext:
    roles = config.roles_by_uid.get(str(os.geteuid()), frozenset())
    return OperatorContext(principal=pwd.getpwuid(os.geteuid()).pw_name, roles=roles)


def validate_assets(config: Installation, release: Release) -> None:
    from src.matching.models import load_model
    from core.matching.policy import PolicyRegistry
    from src.matching.policy import read_policy
    from src.matching.reviews import read_registry
    release.require_certificate()
    policy = PolicyRegistry(tuple(read_policy(p) for p in config.policy_dir.glob('*.json'))).get(release.manifest.purpose, release.manifest.category_id)
    if policy is None or policy.content_hash != release.manifest.policy_hash:
        raise ContractError('policy', 'missing or mismatched active policy')
    model = load_model(config.artifact_root, release.model_hash) if release.model_hash else None
    if release.registry_hash:
        read_registry(config.artifact_root / (release.registry_hash + '.registry.json'), expected_hash=release.registry_hash)
    dataset = config.dataset_root / release.manifest.dataset_hash
    if not dataset.is_dir() or not (dataset / 'files.json').is_file():
        raise ContractError('dataset', 'frozen release dataset missing')
    hashes = json.loads((dataset / 'files.json').read_text())
    if set(hashes) != {'labels.jsonl', 'pairs.jsonl', 'split.json'}:
        raise ContractError('dataset', 'incomplete frozen dataset')
    for name, digest in hashes.items():
        if (dataset / name).is_symlink() or sha256((dataset / name).read_bytes()).hexdigest() != digest:
            raise ContractError('dataset', 'frozen dataset hash mismatch')



    certificate = release.certificate
    if certificate is None:
        raise ContractError('certificate', 'required for active assets')
    report = EvaluationReport.model_validate_json((config.authority_root / (certificate.evaluation_hash + '.evaluation.json')).read_text())
    if report.case_metadata_hash:
        path = dataset / 'evaluation-cases' / (report.case_metadata_hash + '.json')
        if path.is_symlink():
            raise ContractError('cases', 'symlink case evidence')
        try:
            metadata = json.loads(path.read_text())
            canonical = {'split_hash': metadata['split_hash'], 'cases': sorted(metadata['cases'], key=lambda row: row['request_id'])}
            digest = sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            if digest != report.case_metadata_hash or metadata['split_hash'] != release.manifest.dataset_hash or (model is not None and model.artifact.dataset_hash != metadata['split_hash']):
                raise ContractError('cases', 'frozen evaluation/training evidence mismatch')
        except (ValueError, KeyError, TypeError) as exc:
            raise ContractError('cases', 'invalid frozen evaluation evidence') from exc
