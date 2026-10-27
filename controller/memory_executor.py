#!/usr/bin/env python3
"""Code-first memory execution, without a DM claim or model wake for empty work."""
import argparse
from pathlib import Path
import sqlite3

from common import ControlError, encode, now_ms
import memory_organizer as organizer


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
        cfg=organizer.load_config(args.config)
        if args.action=='run':
            result=organizer.run(cfg,clock=now_ms)
        else:
            path=args.response[1:] if args.response.startswith('@') else args.response
            result=organizer.complete_model_file(cfg,args.run_id,Path(path),clock=now_ms)
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error,ControlError) as exc:
        result={'ok':False,'reason':exc.reason if isinstance(exc,ControlError) else 'memory_executor_io_error'}
    print(encode(result))
    return 0 if result.get('ok') else 1


if __name__=='__main__':raise SystemExit(main())
