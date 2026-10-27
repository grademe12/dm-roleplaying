"""Shared validation and single-host OS locks (Python 3.11+)."""
import hashlib
import json
import math
import os
import time
from contextlib import contextmanager
from pathlib import Path


class ControlError(Exception):
    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


def now_ms():
    return time.time_ns() // 1_000_000


def require(condition, reason):
    if not condition:
        raise ControlError(reason)


def integer(value, name='integer', minimum=0):
    require(type(value) is int and value >= minimum, 'invalid_' + name)
    return value


def text(value, name='text'):
    require(isinstance(value, str) and bool(value.strip()), 'invalid_' + name)
    return value


def finite_tree(value):
    if isinstance(value, float):
        require(math.isfinite(value), 'nonfinite_number')
    elif isinstance(value, dict):
        for child in value.values():
            finite_tree(child)
    elif isinstance(value, list):
        for child in value:
            finite_tree(child)


def encode(value):
    finite_tree(value)
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def decode(value):
    result = json.loads(value)
    finite_tree(result)
    return result


@contextmanager
def file_lock(db_path, purpose, private=False):
    digest = hashlib.sha256(purpose.encode()).hexdigest()[:24]
    path = Path(str(Path(db_path).resolve()) + '.' + digest + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    if private:
        handle = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600), 'a+b')
        os.chmod(path, 0o600)
    else:
        handle = open(path, 'a+b')
    owned = False
    try:
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ControlError('busy') from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ControlError('busy') from exc
        owned = True
        yield
    finally:
        if owned:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def required_text(path):
    """A missing required instruction is a controlled error, never silently skipped."""
    try:
        return Path(path).read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc:
        raise ControlError('required_document_unavailable') from exc
