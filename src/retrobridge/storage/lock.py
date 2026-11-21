import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

class LockError(Exception):
    """Exception raised when another RetroBridge instance is running."""
    pass

@contextmanager
def file_lock(lock_dir: Path, lock_name: str = ".retrobridge.lock") -> Generator[None, None, None]:
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / lock_name
    fd = open(lock_file, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as e:
        fd.close()
        raise LockError("Another RetroBridge command is currently running.") from e

    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        fd.close()
