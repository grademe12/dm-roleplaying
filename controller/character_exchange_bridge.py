#!/usr/bin/env python3
"""Dropbox exchange bridge for the character editor: same one-pass bridge as memory, with its own profile.

Local folder <character_documents.path's folder>/character-editor <-> Dropbox /yujin-project/character-exchange/in,/out.
Only character-model-input-<run_id>.json goes up and character-model-response-<run_id>.json comes down.
Memory exchange files, remote root, state and lock are never touched. Prints one JSON line without contents."""
import json
from pathlib import Path
import sys

import memory_exchange_bridge as bridge

DEFAULT_REMOTE_ROOT = '/yujin-project/character-exchange'


def _load(config_path):
    path = Path(config_path).expanduser().resolve()
    return path, json.loads(path.read_text(encoding='utf-8-sig'))


def enabled(config_path):
    _, cfg = _load(config_path)
    opts = cfg.get('character_documents')
    return isinstance(opts, dict) and opts.get('mode', 'off') in ('shadow', 'enforce')


def resolve_exchange_dir(config_path):
    """Mirrors character_documents.settings(): explicit path, else <state_dir>/character-documents.sqlite3."""
    path, cfg = _load(config_path)

    def absolute(value):
        item = Path(str(value)).expanduser()
        return (item if item.is_absolute() else path.parent / item).resolve()
    opts = cfg.get('character_documents')
    if isinstance(opts, dict) and opts.get('path'):
        store = absolute(opts['path'])
    else:
        state = absolute(cfg['state_dir']) if cfg.get('state_dir') else absolute(cfg['db']).parent / 'automation-state'
        store = state / 'character-documents.sqlite3'
    return store.parent / 'character-editor'


PROFILE = bridge.Profile('character', DEFAULT_REMOTE_ROOT, 'character_executor.py', resolve_exchange_dir,
                         'character-bridge-state.json', 'character-bridge.lock', enabled=enabled)


def main(argv=None, client=None):
    return bridge.main(argv, client=client, profile=PROFILE)


if __name__ == '__main__':
    sys.exit(main())
