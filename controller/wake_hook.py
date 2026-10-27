"""Credential-free local hook decision. No claims, ACKs, ingest or model APIs.

Use gate --check (including retry eligibility), then emit one wake/silent result.
The runtime's registered agent keeps the original gate --claim entry contract.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from common import ControlError, file_lock, integer, require
from wake_receiver import durable_json, state_json
import worker_budget


def probe(cfg):
    command = [cfg['python'], str(Path(__file__).with_name('gate.py')), '--config', cfg['_config_path'], '--check']
    result = subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=25,check=False)
    require(result.returncode == 0, 'wake_gate_check_failed')
    report = json.loads(result.stdout)
    require(report.get('ok') is True and type(report.get('wake_eligible')) is bool,
            'wake_gate_contract_mismatch')
    return report


def check(cfg,*,gate_check=None,now=None,dry_run=False):
    if cfg.get('wake',{}).get('enabled') is not True:
        return {'action':'silent','reason':'wake_disabled','payload':{}}
    now = time.time_ns()//1_000_000 if now is None else integer(now)
    path = Path(cfg['state_dir'])/'wake-hook.json'
    with file_lock(cfg['db'],'wake_hook'):
        report = (gate_check or (lambda: probe(cfg)))()
        if not report.get('wake_eligible'):
            return {'action':'silent','reason':'gate_not_eligible','payload':{}}
        state = state_json(path)
        # This is a bounded debounce, never a permanent "already woken" watermark.
        last = state.get('last_wake_ms')
        if last is not None and 0 <= now-last < 30000:
            return {'action':'silent','reason':'wake_debounce','payload':{}}
        if not dry_run:
            durable_json(path,{'last_wake_ms':now})
        payload = {'reasons':report.get('reasons',[]),'revision':report.get('revision'),
                   'at_ms':now,'entry':'gate_claim'}
        if worker_budget.policy(cfg)['enabled'] and not dry_run:
            ticket = worker_budget.issue(cfg, now)
            payload.update(controller_invocation_id=ticket['invocation_id'],
                           worker_requested_ms=ticket['requested_ms'],
                           worker_deadline_ms=ticket['deadline_ms'])
        return {'action':'wake','reason':'gate_work_due','payload':payload}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--dry-run',action='store_true')
    args = parser.parse_args(argv)
    try:
        cfg = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
        cfg['_config_path'] = str(Path(args.config).resolve())
        result = check(cfg,dry_run=args.dry_run)
    except (ControlError,OSError,ValueError,KeyError,TypeError,sqlite3.Error,subprocess.TimeoutExpired) as exc:
        result = {'action':'silent','reason':exc.reason if isinstance(exc,ControlError) else 'wake_check_failed',
                  'payload':{'check_failed':True}}
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
