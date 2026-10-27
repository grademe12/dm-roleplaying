"""In-process execution ownership, not an OS security boundary."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from common import require

OWNER = ContextVar('decision_driver_owner', default=None)


def key(cfg):
    return (str(Path(cfg['state_dir']).resolve()), str(Path(cfg['db']).resolve()), cfg['session'])


@contextmanager
def owned(cfg):
    token = OWNER.set(key(cfg))
    try:
        yield
    finally:
        OWNER.reset(token)


def check(cfg):
    opts = cfg.get('decision_driver') or {}
    if isinstance(opts, dict) and opts.get('enabled') is True:
        require(OWNER.get() == key(cfg), 'decision_driver_owned')
