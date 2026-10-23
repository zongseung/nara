"""Persistent-inode local locks; callers acquire maintenance before version pins."""
from contextlib import contextmanager
import fcntl
from pathlib import Path
from typing import Iterator


@contextmanager
def file_lock(path: Path, *, shared: bool = False, blocking: bool = True) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(handle, mode | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def maintenance_active(root: Path) -> bool:
    """Persistent boot gate shared by submitters, workers and production consumers."""
    return (root / 'control/maintenance.flag').exists()
