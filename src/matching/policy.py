import json
from pathlib import Path

from core.matching.policy import Policy, load_policy


def read_policy(path: str | Path, *, expected_hash: str | None = None) -> Policy:
    with Path(path).open(encoding="utf-8") as stream:
        return load_policy(json.load(stream), expected_hash=expected_hash)
