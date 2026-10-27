#!/usr/bin/env python3
"""Model-free Slack receipt resolver. Never requests delivery history scans."""
import argparse
import json
import os
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path


def now_ms():
    return time.time_ns()//1_000_000


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False)
    os.replace(temporary, path)


def listener_healthy(cfg, now):
    try:
        health = json.loads((Path(cfg['state_dir'])/'slack_health.json').read_text())
    except (OSError, ValueError):
        return False
    age = now-health.get('last_heartbeat_ms', 0)
    return (health.get('connected') is True and 0 <= age <= cfg['slack'].get('max_heartbeat_age_seconds', 120)*1000
            and not health.get('alerts', {}).get('ingest_failed'))


def ctl(cfg, *args):
    try:
        result = subprocess.run([cfg['python'], cfg['ctl'], '--db', cfg['db'], *args],
                                capture_output=True, text=True, encoding='utf-8', timeout=60)
        return result.returncode, json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 1, {'ok': False, 'reason': 'delivery_controller_unavailable'}


@contextmanager
def single_instance(state_dir):
    import fcntl
    path = Path(state_dir)/'slack-delivery-worker.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


class RecoveryWorker:
    def __init__(self, cfg, run_ctl=None, clock=now_ms):
        self.cfg = cfg
        self.run_ctl = run_ctl or (lambda *args: ctl(cfg, *args))
        self.clock = clock
        self.recheck_dispatch = False
        self.initialized = False

    def cycle(self):
        now = self.clock()
        session = self.cfg['session']
        code, pending = self.run_ctl('slack-delivery-pending', '--session', session)
        result = {'at_ms': now, 'state': 'controller_error', 'confirmed': [], 'unresolved': [], 'errors': []}
        if code or not pending.get('ok'):
            return self._save(result)
        settings = pending['settings']
        result['manual_unresolved_count'] = pending.get('manual_unresolved_count', 0)
        if not settings.get('auto_confirm_enabled'):
            result['state'] = 'disabled'
            return self._save(result)
        self.run_ctl('recover-stale', '--session', session)
        code, refreshed = self.run_ctl('slack-delivery-pending', '--session', session)
        if code or not refreshed.get('ok'):
            return self._save(result)
        pending = refreshed
        settings = pending['settings']
        result['manual_unresolved_count'] = pending.get('manual_unresolved_count', 0)
        if not settings.get('auto_confirm_enabled'):
            result['state'] = 'disabled'
            return self._save(result)
        if not self.initialized:
            self.recheck_dispatch = self.recheck_dispatch or pending.get('dispatch_pending', False)
            self.initialized = True
        result['state'] = 'manual_review' if result['manual_unresolved_count'] else 'idle'
        result['mode'] = settings['mode']
        for job in pending['jobs']:
            code, out = self.run_ctl('slack-delivery-reconcile', '--session', session, '--job-id', job['id'])
            if code == 0 and out.get('ok'):
                if out.get('dispatch_recheck'):
                    result['confirmed'].append(job['id'])
                    self.recheck_dispatch = True
            else:
                result['unresolved'].append(job['id'])
                if out.get('reason') != 'delivery_evidence_missing':
                    result['errors'].append({'job_id': job['id'], 'reason': out.get('reason')})
        if result['unresolved']:
            conflicts = ('delivery_receipt_conflict', 'delivery_receipt_already_used', 'delivery_evidence_mismatch',
                         'delivery_attempt_mismatch', 'delivery_body_changed')
            result['state'] = ('conflict' if any(e['reason'] in conflicts for e in result['errors'])
                               else 'controller_busy' if result['errors'] else 'waiting_for_receipts')
        if self.recheck_dispatch and not listener_healthy(self.cfg, self.clock()):
            result['dispatch'] = {'reason': 'listener_unhealthy'}
        elif self.recheck_dispatch:
            # Retry the existing dispatch entry point, never resend unresolved work directly.
            code, out = self.run_ctl('send-due', '--session', session, '--channel', 'slack',
                                     '--transport-config', '@'+self.cfg['slack']['transport_config'])
            result['dispatch'] = {'reason': out.get('reason'), 'code': code}
            if code == 0 and out.get('ok'):
                self.recheck_dispatch = out.get('reason') in ('send_interval', 'claimed', 'limit_reached')
                if result['confirmed'] and not result['unresolved'] and not result['manual_unresolved_count']:
                    result['state'] = 'confirmed'
        return self._save(result)

    def _save(self, result):
        write_json(Path(self.cfg['state_dir'])/'slack-delivery-health.json', result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    if not cfg.get('slack', {}).get('transport_config'):
        raise SystemExit('slack.transport_config is required')
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    with single_instance(cfg['state_dir']) as owned:
        if not owned:
            return 3
        worker = RecoveryWorker(cfg)
        while not stop.is_set():
            worker.cycle()
            if args.once:
                break
            stop.wait(2)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
