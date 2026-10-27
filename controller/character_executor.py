#!/usr/bin/env python3
"""Code-first character editor execution, without a DM claim or model wake for empty work."""
import argparse
from pathlib import Path
import re
import sqlite3

from common import ControlError, encode, now_ms
import character_documents as documents
import character_editor as editor

# Normal waits, not failures: nothing to do, lease still open, retry backoff, human attention needed.
WAITING = ('off', 'no_work', 'busy', 'retry_later', 'needs_attention')
FILE_RE = re.compile(r'character-model-(?:input|response)-([0-9a-f]{32})\.json')


def sweep(cfg, keep=None):
    """Drop leftover hand-off files of runs that already ended (failed/expired/completed); returns a count.
    Pending runs, unknown runs and any I/O or ledger error keep their files."""
    removed = 0
    try:
        folder = documents.settings(cfg)['path'].parent / 'character-editor'
        entries = sorted(folder.iterdir()) if folder.is_dir() else []
    except (OSError, ControlError):
        return 0
    for path in entries:
        match = FILE_RE.fullmatch(path.name)
        if not match or match.group(1) == keep:
            continue
        try:
            if documents.response_binding(cfg, match.group(1))['status'] in ('failed', 'completed'):
                path.unlink(missing_ok=True)
                removed += 1
        except (OSError, sqlite3.Error, ControlError):
            continue
    return removed


def normalize(result):
    if not result.get('ok') and result.get('reason') in WAITING:
        return {**result, 'ok': True}
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    commands=parser.add_subparsers(dest='action',required=True)
    commands.add_parser('run')
    complete=commands.add_parser('complete')
    complete.add_argument('--run-id',required=True)
    complete.add_argument('--response',required=True)
    args=parser.parse_args(argv)
    try:
        cfg=editor.load_config(args.config)
        if args.action=='run':
            result=normalize(editor.run(cfg,clock=now_ms))
            if result.get('ok') and documents.settings(cfg)['mode']!='off':
                swept=sweep(cfg,keep=result.get('run_id') if result.get('next') in ('model','await_response') else None)
                if swept:result={**result,'swept':swept}
        else:
            path=args.response[1:] if args.response.startswith('@') else args.response
            result=normalize(editor.finish(cfg,args.run_id,Path(path),clock=now_ms))
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error,ControlError) as exc:
        result=normalize({'ok':False,'reason':exc.reason if isinstance(exc,ControlError) else 'character_executor_io_error'})
    print(encode(result))
    return 0 if result.get('ok') else 1


if __name__=='__main__':raise SystemExit(main())
