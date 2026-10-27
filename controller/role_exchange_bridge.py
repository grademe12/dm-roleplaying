#!/usr/bin/env python3
"""Separate optional file exchange for role-executor tasks; metadata-only output."""
import json
from pathlib import Path
import sys

import memory_exchange_bridge as bridge


def enabled(config_path):
    cfg=json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
    opts=cfg.get('role_execution')
    return isinstance(opts,dict) and opts.get('enabled') is True


def resolve_exchange_dir(config_path):
    path=Path(config_path).expanduser().resolve();cfg=json.loads(path.read_text(encoding='utf-8-sig'))
    def absolute(value):
        item=Path(value).expanduser();return (item if item.is_absolute() else path.parent/item).resolve()
    opts=cfg.get('role_execution') or {}
    return absolute(opts['path']) if opts.get('path') else absolute(cfg['state_dir'])/'role-execution'


PROFILE=bridge.Profile('role','/yujin-project/role-exchange','role_executor.py',resolve_exchange_dir,
                       'role-bridge-state.json','role-bridge.lock',enabled=enabled,complete_timeout=1860)


def main(argv=None,client=None):return bridge.main(argv,client=client,profile=PROFILE)


if __name__=='__main__':sys.exit(main())
