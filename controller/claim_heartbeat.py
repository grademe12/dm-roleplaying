#!/usr/bin/env python3
"""Bounded wake keeper. State ownership is observable; model liveness is not.

The daemon can outlive a dead model until the original claim deadline. It never
extends that deadline or claims new work. No runtime API or model calls.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from common import ControlError, require
import gate
import wake_nudge

# Stop reasons meaning the wake is over; a lease it still holds is orphaned.
WAKE_ENDED = ('claim_deadline', 'ack_failed', 'ack_finished', 'ownership_lost')
# Ends where unanswered DMs need an immediate re-wake even if no lease was left behind.
NUDGE_ENDS = ('claim_deadline', 'ack_failed')


def wake_ended(cfg, wake_id, reason, *, nudge=None):
    """Release this wake's own lease; re-wake if DMs are still unanswered. Best-effort."""
    if reason not in WAKE_ENDED:
        return None
    state_dir = Path(cfg['state_dir'])
    result = gate.release_wake_lease(cfg, state_dir, wake_id, reason)
    if not result or not result.get('received_count'):
        return None
    if result.get('released') or reason in NUDGE_ENDS:
        try:
            return (nudge or wake_nudge.send)(cfg, wake_id, 'pending_dm_after_' + reason)
        except Exception as exc:   # never let the re-wake hook break the keeper
            gate.append_decision_trace(state_dir, wake_id, 'wake_nudge_failed', reason=reason,
                                       error_type=type(exc).__name__)
    return None


def marker_path(state_dir, wake_id):
    require(bool(re.fullmatch(r'[A-Za-z0-9_:-]{1,80}', wake_id)), 'invalid_wake_id')
    return Path(state_dir) / ('heartbeat-' + wake_id + '.json')


def pulse(cfg, wake_id, *, now=None, force_deadline=False, renew=True):
    """One short locked check; returns a stop reason or None. Never reacquire."""
    state_dir = Path(cfg['state_dir'])
    now = gate.now_ms() if now is None else now
    with gate.gate_lock(state_dir):
        wake = gate.load_json(state_dir / 'wake.json', {})
        state = gate.load_json(state_dir / 'gate_state.json', {})
        ack = gate.load_json(state_dir / 'wake_ack.json', {})
        if not all(record.get('wake_id') == wake_id for record in (wake, state, ack)):
            return 'ownership_lost'
        if ack.get('phase') != 'started':
            return 'ack_' + str(ack.get('phase')) if ack.get('phase') in ('finished', 'failed') else 'ack_not_started'
        deadline = wake.get('claim_deadline_ms')
        require(type(deadline) is int and type(wake.get('woken_ms')) is int
                and 0 < deadline - wake['woken_ms'] <= gate.MAX_CLAIM_RUN_MS, 'unbounded_claim')
        if force_deadline or now >= deadline:
            gate.write_json(state_dir / 'wake_ack.json', {'wake_id': wake_id, 'phase': 'failed',
                            'at_ms': now, 'source': 'daemon', 'reason': 'claim_deadline'})
            gate.append_decision_trace(state_dir, wake_id, 'ack_failed', reason='claim_deadline', ack_at_ms=now)
            return 'claim_deadline'
        if renew and not gate.refresh_claim(state_dir, wake_id, 'daemon', now):
            return 'ownership_lost'
    return None


def run(cfg, wake_id, stopped, *, clock=gate.now_ms, monotonic=time.monotonic, sleep=time.sleep):
    state_dir = Path(cfg['state_dir'])
    marker = marker_path(state_dir, wake_id)
    policy = gate.claim_policy(cfg)
    lock_fd = os.open(marker.with_suffix('.lock'), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, 'a+b') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 'already_running'
        wake = gate.load_json(state_dir / 'wake.json', {})
        require(wake.get('wake_id') == wake_id, 'stale_claim')
        deadline = wake.get('claim_deadline_ms')
        require(type(deadline) is int and type(wake.get('woken_ms')) is int
                and 0 < deadline - wake['woken_ms'] <= gate.MAX_CLAIM_RUN_MS, 'unbounded_claim')
        # Readiness is written before acquiring gate_lock: the launcher holds
        # that lock until it sees this marker. No heartbeat can race the claim.
        gate.write_json(marker, {'wake_id': wake_id, 'pid': os.getpid(), 'ready': True,
                                 'claim_deadline_ms': deadline})
        gate.append_decision_trace(state_dir, wake_id, 'heartbeat_started', pid=os.getpid(),
                                   claim_deadline_ms=deadline,
                                   heartbeat_interval_ms=policy['heartbeat_interval_ms'],
                                   liveness_source='claim_state_only')
        end = monotonic() + min(gate.MAX_CLAIM_RUN_MS, max(0, deadline - clock())) / 1000
        next_renewal = monotonic()
        reason = 'daemon_stopped'
        try:
            while not stopped():
                current = monotonic()
                reason = pulse(cfg, wake_id, now=clock(), force_deadline=current >= end,
                               renew=current >= next_renewal)
                if reason:
                    break
                if current >= next_renewal:
                    next_renewal = current + policy['heartbeat_interval_ms'] / 1000
                sleep(min(1, max(0, end - monotonic())))
            wake_ended(cfg, wake_id, reason)
            return reason or 'daemon_stopped'
        finally:
            gate.append_decision_trace(state_dir, wake_id, 'heartbeat_stopped',
                                       reason=reason or 'daemon_stopped', **gate.claim_metadata(state_dir))
            marker.unlink(missing_ok=True)


def start(cfg, wake_id):
    """Called once by gate --claim, under gate_lock; no model discretion needed."""
    state_dir = Path(cfg['state_dir'])
    marker = marker_path(state_dir, wake_id)
    command = [sys.executable, str(Path(__file__).resolve()), '--config', cfg['_config_path'],
               '--wake-id', wake_id, '--detach']
    subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=True, timeout=5)
    until = time.monotonic() + 2
    while time.monotonic() < until:
        ready = gate.load_json(marker, {})
        if ready.get('wake_id') == wake_id and ready.get('ready') is True:
            return
        time.sleep(0.02)
    raise ControlError('heartbeat_start_failed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--wake-id', required=True)
    parser.add_argument('--detach', action='store_true')
    args = parser.parse_args(argv)
    cfg = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    gate.claim_policy(cfg)
    marker_path(cfg['state_dir'], args.wake_id)
    if args.detach:
        if os.fork():
            return 0
        os.setsid()
    cancelled = [False]

    def cancel(_signum, _frame):
        cancelled[0] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, cancel)
    code = 0
    try:
        run(cfg, args.wake_id, lambda: cancelled[0])
    except (ControlError, OSError, ValueError, KeyError, TypeError) as exc:
        reason = exc.reason if isinstance(exc, ControlError) else 'heartbeat_io_error'
        gate.append_decision_trace(Path(cfg['state_dir']), args.wake_id, 'heartbeat_error',
                                   reason=reason, error_type=type(exc).__name__)
        code = 1
    if args.detach:
        os._exit(code)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
