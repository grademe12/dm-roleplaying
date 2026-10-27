#!/usr/bin/env python3
"""Decision gate for Muse. Evaluates local state and fences controller apply.

Exit 0  -> decision claimed (with --claim) or wake requested (legacy Hook mode).
Exit 10 -> stay silent.

Fast retries are bounded; unresolved work remains eligible for slower recovery retries.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
import re

import decision_routing
import driver_ownership
import worker_budget
from common import ControlError, integer, require
from datetime import datetime, timezone
from pathlib import Path

WAKE, SILENT = 0, 10
# needs_decision reasons that require Muse. 'unprocessed_events' is excluded on purpose:
# a deferred DM event may stay unprocessed and would wake Muse forever.
WAKE_NEEDS = ('morning_contact_due', 'proactive_check_due', 'decision_due', 'reply_due', 'activity_due', 'appointment_due',
              'appointment_prepare_due','appointment_review_due',
              'weekly_plan_missing', 'no_current_activity', 'draft_needs_review', 'draft_stale')
RETRY_DELAYS_MS = (0, 10 * 60_000, 30 * 60_000)
DM_RETRY_DELAYS_MS = (0, 0, 0, 0)
# Temporary allowance for multi-minute model reasoning. Tune from observed gaps;
# this is a recovery delay, not proof that the model is making progress.
WORKER_STALE_MS = 15 * 60_000
# Handover window after a live wait: the session has this long to pick the claim
# up with its next command. Past it the claim counts as abandoned even while the
# ack is fresh. Same span as store.LEASE_GRACE_MS (the composing lease).
PICKUP_GRACE_MS = 180_000
MAX_CLAIM_RUN_MS = 30 * 60_000
RECOVERY_RETRY_MS = 30 * 60_000
DM_RECOVERY_RETRY_MS = 10 * 60_000
POLLER_STALE_MS = 10 * 60_000
SLACK_HEALTH = {}   # loaded once per run in main()


def now_ms():
    return time.time_ns() // 1_000_000


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def append_decision_trace(state_dir, wake_id, phase, **fields):
    """Append timing metadata only; the decision document and DM text stay out of logs."""
    at_ms = now_ms()
    day = datetime.fromtimestamp(at_ms / 1000, timezone.utc).strftime('%Y-%m-%d')
    path = state_dir / f'decision-trace-{day}.jsonl'
    entry = {'at_ms': at_ms, 'wake_id': wake_id, 'phase': phase, **fields}
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(',', ':')) + '\n')
    except OSError as exc:
        print(f'yujin gate: decision trace unavailable ({type(exc).__name__})', file=sys.stderr)


@contextmanager
def gate_lock(state_dir):
    """Serialize claim, acknowledgement and claimed apply across Cron runs."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / 'gate.lock', 'a+b') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def claim_is_current(state_dir, wake_id, now=None):
    wake = load_json(state_dir / 'wake.json', {})
    gate_state = load_json(state_dir / 'gate_state.json', {})
    ack = load_json(state_dir / 'wake_ack.json', {})
    now = now_ms() if now is None else now
    return (bool(wake_id) and wake.get('wake_id') == wake_id
            and gate_state.get('wake_id') == wake_id
            and ack.get('wake_id') == wake_id and ack.get('phase') == 'started'
            and now < wake.get('claim_deadline_ms', now + 1))


def claim_policy(cfg):
    policy = cfg.get('decision_claim', {})
    require(isinstance(policy, dict), 'invalid_decision_claim')
    stale = integer(policy.get('stale_after_seconds', WORKER_STALE_MS // 1000),
                    'claim_stale_after_seconds', 30)
    interval = integer(policy.get('heartbeat_interval_seconds', 30),
                       'claim_heartbeat_interval_seconds', 1)
    maximum = integer(policy.get('max_run_seconds', 1800), 'claim_max_run_seconds', 1)
    enabled = policy.get('auto_heartbeat', False)
    require(type(enabled) is bool, 'invalid_auto_heartbeat')
    require(interval * 3 <= stale and interval < maximum
            and maximum * 1000 <= MAX_CLAIM_RUN_MS, 'invalid_claim_timing')
    return {'stale_after_ms': stale * 1000, 'heartbeat_interval_ms': interval * 1000,
            'max_run_ms': maximum * 1000, 'auto_heartbeat': enabled}


def active_claim(state, ack, now, stale_after_ms):
    return (ack.get('wake_id') == state.get('wake_id') and ack.get('phase') == 'started'
            and now - ack.get('at_ms', 0) < stale_after_ms
            and now < state.get('claim_deadline_ms', now + 1)
            and not (type(ack.get('pickup_deadline_ms')) is int
                     and now >= ack['pickup_deadline_ms']))


def claim_metadata(state_dir, now=None):
    now = now_ms() if now is None else now
    wake = load_json(state_dir / 'wake.json', {})
    state = load_json(state_dir / 'gate_state.json', {})
    ack = load_json(state_dir / 'wake_ack.json', {})
    return {'current_wake_id': wake.get('wake_id'), 'gate_wake_id': state.get('wake_id'),
            'claim_deadline_ms': wake.get('claim_deadline_ms'),
            'ack_wake_id': ack.get('wake_id'), 'ack_phase': ack.get('phase'),
            'ack_at_ms': ack.get('at_ms'),
            'heartbeat_age_ms': now - ack['at_ms'] if type(ack.get('at_ms')) is int else None}


def rejected(state_dir, wake_id, operation, reason='stale_claim', **fields):
    append_decision_trace(state_dir, wake_id, 'claim_rejected', operation=operation,
                          reason=reason, **claim_metadata(state_dir), **fields)


def refresh_claim(state_dir, wake_id, source, now=None, pickup_deadline_ms=None):
    """Caller holds gate_lock. A late heartbeat never reacquires a replaced claim.

    pickup_deadline_ms marks the handover window (PICKUP_GRACE_MS): past it the
    claim stops counting as active even while the ack stays fresh. A 'daemon'
    refresh preserves the previous deadline — the auto heartbeat must not extend
    a dead session's handover window. Any other source sets the deadline to the
    given value, or clears it when none is given.
    """
    if not claim_is_current(state_dir, wake_id, now):
        rejected(state_dir, wake_id, 'heartbeat')
        return False
    now = now_ms() if now is None else now
    previous = load_json(state_dir / 'wake_ack.json', {})
    if source == 'daemon':
        pickup_deadline_ms = previous.get('pickup_deadline_ms')
    ack = {'wake_id': wake_id, 'phase': 'started', 'at_ms': now, 'source': source}
    if type(pickup_deadline_ms) is int:
        ack['pickup_deadline_ms'] = pickup_deadline_ms
    write_json(state_dir / 'wake_ack.json', ack)
    append_decision_trace(state_dir, wake_id, 'heartbeat', source=source,
                          ack_at_ms=now, previous_ack_at_ms=previous.get('at_ms'))
    return True


def read_tick(cfg):
    argv = [cfg['python'], cfg['ctl'], '--db', cfg['db'], 'tick', '--session', cfg['session'], '--limit', '1', '--view', 'gate']
    proc = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', timeout=20)
    data = json.loads(proc.stdout)
    if proc.returncode != 0 or not data.get('ok'):
        raise RuntimeError(data.get('reason', f'exit={proc.returncode}'))
    return data


def release_wake_lease(cfg, state_dir, wake_id, reason):
    """Best-effort: drop the conversation lease only if `wake_id` (a wake that has ended) owns it.

    The controller compares the owner atomically, so a lease set by another, still-live
    wake (or an owner-less legacy wait) is never touched. Returns the controller result or None.
    """
    if not isinstance(wake_id, str) or not re.fullmatch(r'[A-Za-z0-9_:-]{1,80}', wake_id):
        return None
    argv = [cfg['python'], cfg['ctl'], '--db', cfg['db'], 'release-lease', '--session', cfg['session'],
            '--owner', wake_id]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', timeout=20)
        data = json.loads(proc.stdout)
        require(proc.returncode == 0 and isinstance(data, dict) and data.get('ok') is True,
                'lease_release_failed')
    except (OSError, ValueError, subprocess.SubprocessError, ControlError) as exc:
        append_decision_trace(state_dir, wake_id, 'lease_release_failed', reason=reason,
                              error_type=type(exc).__name__)
        return None
    if data.get('released'):
        append_decision_trace(state_dir, wake_id, 'lease_released', reason=reason,
                              was_live=data.get('was_live'), received_count=data.get('received_count'))
    return data


def orphaned_lease_owner(tick, gate_state, ack, now, stale_after_ms):
    """Owner of a live lease whose wake is no longer an active claim, else None."""
    owner = tick.get('conversation_lease_owner')
    if not tick.get('conversation_lease_until_ms') or not owner:
        return None
    if owner == gate_state.get('wake_id') and active_claim(gate_state, ack, now, stale_after_ms):
        return None
    return owner


def settle_incoming(cfg, clock=None, sleep=time.sleep):
    """Before claiming, wait for a burst of unanswered messages to pause. Returns waited ms.

    Off unless automation.json sets incoming_settle_seconds; capped by incoming_settle_cap_seconds.
    """
    clock = clock or now_ms
    settle_ms = int(float(cfg.get('incoming_settle_seconds', 0)) * 1000)
    cap_ms = int(float(cfg.get('incoming_settle_cap_seconds', 20)) * 1000)
    started = clock()
    if settle_ms <= 0:
        return 0
    while True:
        try:
            tick = read_tick(cfg)
        except Exception:
            return clock() - started   # the claim itself reports controller errors
        now = clock()
        if tick.get('conversation_lease_until_ms'):
            return now - started
        times = [max(m.get('observed_ms') or 0, (m.get('observation') or {}).get('ingested_ms') or 0)
                 for m in tick.get('messages', []) if m.get('status') == 'received']
        remaining = max(0, max(times) + settle_ms - now) if times else 0
        if not remaining or now - started >= cap_ms:
            return now - started
        sleep(min(2.0, remaining / 1000))


def sleep_interrupt(tick, now):
    """One stable draw per Slack notification cluster; polling/retries never reroll it."""
    activity = tick.get('activity') or {}
    label = activity.get('label') or ''
    is_sleep = activity.get('category') in ('sleep', '수면') or any(word in label for word in ('수면', '취침', '잠'))
    if activity.get('attention') != 'unavailable' or not is_sleep or (activity.get('planned_end_ms') or 0) <= now:
        return None
    messages = sorted((m for m in tick['messages']
                       if (m.get('observation') or {}).get('channel') == 'slack'
                       and m['status'] in ('received', 'deferred')
                       and m['observed_ms'] >= activity['start_ms']),
                      key=lambda m: (m['observed_ms'], m['id']))
    if not messages:
        return None
    received_times = [m['observed_ms'] for m in messages if m['status'] == 'received']
    if not received_times:
        return None
    messages = [m for m in messages if m['observed_ms'] <= max(received_times)]
    cluster = [messages[0]]
    for message in messages[1:]:
        if message['observed_ms'] - cluster[-1]['observed_ms'] > 3 * 60_000:
            cluster = []
        cluster.append(message)
    chance = 10 if len(cluster) == 1 else 30 if len(cluster) == 2 else 40
    seed = f"{activity['id']}:{cluster[0]['id']}".encode()
    draw = int.from_bytes(hashlib.sha256(seed).digest()[:8], 'big') / 2**64
    return {'activity_id': activity['id'], 'message_ids': [m['id'] for m in cluster],
            'chance_percent': chance, 'wake_allowed': draw < chance / 100}


def retry_delays(reasons):
    return DM_RETRY_DELAYS_MS if any(r in reasons for r in (
        'morning_contact_due', 'proactive_check_due', 'new_dm', 'reply_due', 'draft_needs_review', 'draft_stale')) else RETRY_DELAYS_MS


def evaluate(tick, health, now):
    """Returns (reasons, detail). detail carries IDs only, never message text."""
    reasons, detail = [], {}
    new_ids = [m['id'] for m in tick['messages'] if m['status'] == 'received']
    if new_ids:
        reasons.append('new_dm')
        detail['new_message_ids'] = new_ids
        slack_new = any(m['status'] == 'received' and
                        (m.get('observation') or {}).get('channel') == 'slack' for m in tick['messages'])
        interruption = sleep_interrupt(tick, now) if slack_new else None
        if interruption:
            detail['sleep_interrupt'] = interruption
    review = []
    for m in tick['messages']:
        # tick returns *_json columns already decoded (observation_json -> observation).
        if (m.get('observation') or {}).get('review_required') and m['status'] != 'handled':
            review.append(m['id'])
    if review:
        reasons.append('message_review')
        detail['review_message_ids'] = review
    reasons += [r for r in WAKE_NEEDS if r in tick['needs_decision']]
    if 'draft_needs_review' in reasons:
        detail['draft_review_group_ids'] = tick.get('draft_review_group_ids', [])
    if 'draft_stale' in reasons:
        detail['draft_stale_group_ids'] = tick.get('draft_stale_group_ids', [])
    unresolved = [j['id'] for j in tick['jobs'] if j['status'] in ('submitted', 'unknown')]
    if unresolved:
        # Receipt resolution is asynchronous metadata, not a mandatory model wake.
        detail['unresolved_job_ids'] = unresolved
    if not health or now - health.get('last_run_ms', 0) > POLLER_STALE_MS:
        reasons.append('poller_stale')
    alerts = sorted((health or {}).get('alerts', {}))
    # Slack listener problems (ingest failure, unexpected sender, backfill errors) are reported the same way.
    alerts += sorted('slack:' + a for a in SLACK_HEALTH.get('alerts', {}))
    if alerts:
        reasons.append('poller_alert')
        detail['poller_alerts'] = alerts
    activity = tick.get('activity') or {}
    # Fingerprint excludes revision: send-due and ingest bump revision without changing
    # what Muse has to decide.
    detail['fingerprint_basis'] = {
        'reasons': reasons, 'ids': [new_ids, review, alerts,
                                   tick.get('draft_review_group_ids', []),
                                   tick.get('draft_stale_group_ids', [])],
        'next_decision_ms': tick['state'].get('next_decision_ms'),
        'continuations': [(r['id'],r['generation']) for r in tick.get('continuations', [])
                          if r['id'] in tick.get('continuation_due_ids', [])],
        'activity': [activity.get('id'), activity.get('planned_end_ms'), activity.get('attention')],
        'reply_due': sorted(m['id'] for m in tick['messages']
                            if m['status'] == 'deferred' and (m.get('reply_due_ms') or 0) <= now)}
    if 'morning_contact_due' in reasons:
        morning=tick['morning_contact']
        detail['morning_contact']={k:morning[k] for k in ('local_date','wake_activity_id','expires_ms')}
        detail['morning_contact']['review_groups']=morning.get('review_groups',[])
        detail['fingerprint_basis']['morning_contact']=detail['morning_contact']
    if 'proactive_check_due' in reasons:
        check=tick.get('proactive_check') or {}
        detail['proactive_check']={k:check.get(k) for k in ('after_ms','interval_ms','status')}
        detail['fingerprint_basis']['proactive_check']=detail['proactive_check']
    if any(r in reasons for r in ('appointment_due','appointment_prepare_due','appointment_review_due')):
        work = tick.get('appointment_lifecycle',{})
        detail['fingerprint_basis']['appointment_work'] = {
            'reviews':work.get('review_ids',[]),'preparations':work.get('preparations',[]),
            'active_ids':[a['id'] for a in tick.get('appointments',[]) if a['status'] == 'confirmed']}
    detail['work_manifest'] = decision_routing.work_manifest(tick,now,reasons)
    return reasons, detail


def decide(reasons, detail, gate_state, now, ack=None, stale_after_ms=WORKER_STALE_MS):
    """Applies the retry policy. Returns (wake, attempt)."""
    if not reasons:
        return False, 0
    delays = retry_delays(reasons)
    fp = hashlib.sha256(json.dumps(detail['fingerprint_basis'], sort_keys=True).encode()).hexdigest()[:16]
    detail['fingerprint'] = fp
    attempts = gate_state.get('attempts', 0)
    if active_claim(gate_state, ack or {}, now, stale_after_ms):
        return False, attempts
    if gate_state.get('fingerprint') != fp:
        return True, 1
    if attempts >= len(delays):
        recovery_delay = DM_RECOVERY_RETRY_MS if delays == DM_RETRY_DELAYS_MS else RECOVERY_RETRY_MS
        if now - gate_state.get('last_wake_ms', 0) < recovery_delay:
            return False, attempts
        return True, attempts
    expired = (type(gate_state.get('claim_deadline_ms')) is int
               and now >= gate_state['claim_deadline_ms'])
    if ((ack or {}).get('wake_id') == gate_state.get('wake_id')
            and ((ack or {}).get('phase') == 'failed' or expired)):
        return True, attempts + 1
    if now - gate_state.get('last_wake_ms', 0) < delays[attempts]:
        return False, attempts
    return True, attempts + 1


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--ack-phase', choices=('started', 'finished', 'failed'))
    parser.add_argument('--claim', action='store_true', help='Cron: evaluate and claim work atomically')
    parser.add_argument('--invocation-id', help='controller_invocation_id from the local hook wake payload')
    parser.add_argument('--check', action='store_true', help='inspect work without claiming or changing gate state')
    parser.add_argument('--heartbeat', action='store_true', help='refresh a live claim')
    parser.add_argument('--apply-claimed', metavar='DECISION', help='apply @decision.json only for the current claim')
    parser.add_argument('--wake-id')
    parser.add_argument('--turn-id', help='optional per-turn telemetry correlation ID')
    args = parser.parse_args(argv)
    if sum((bool(args.ack_phase), args.claim, args.check, args.heartbeat, bool(args.apply_claimed))) > 1:
        parser.error('choose only one gate action')
    if (args.heartbeat or args.apply_claimed) and not args.wake_id:
        parser.error('--wake-id is required')
    if args.invocation_id and not args.claim:
        parser.error('--invocation-id requires --claim')
    if args.turn_id and not re.fullmatch(r'[A-Za-z0-9_:-]{1,80}',args.turn_id):
        parser.error('invalid turn ID')
    if args.wake_id and not re.fullmatch(r'[A-Za-z0-9_:-]{1,80}', args.wake_id):
        parser.error('invalid wake ID')
    cfg = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    cfg['_config_path'] = str(Path(args.config).expanduser().resolve())
    try:
        if not args.check and not args.heartbeat and args.ack_phase != 'failed':
            driver_ownership.check(cfg)
        claim_policy(cfg)
        worker_budget.policy(cfg)
    except ControlError as exc:
        print(json.dumps({'ok': False, 'reason': exc.reason}))
        return 2
    state_dir = Path(cfg['state_dir'])
    if args.check:
        # Inspection must not create a gate lock file or delay a Decision worker waiting to claim.
        return _locked_main(args, cfg, state_dir)
    if args.claim:
        # Outside the gate lock so other gate calls are not held while the user is typing.
        settle_incoming(cfg)
    with gate_lock(state_dir):
        return _locked_main(args, cfg, state_dir)


def claim_work(cfg, invocation_id=None):
    """Supervisor entry: return the claimed wake under the same gate lock."""
    driver_ownership.check(cfg)
    state_dir = Path(cfg['state_dir'])
    args = argparse.Namespace(ack_phase=None, heartbeat=False, apply_claimed=None,
                              check=False, claim=True, wake_id=None, turn_id=None, invocation_id=invocation_id)
    settle_incoming(cfg)
    with gate_lock(state_dir):
        result = _locked_main(args, cfg, state_dir)
        return result, load_json(state_dir / 'wake.json', {}) if result == WAKE else None


def _locked_main(args, cfg, state_dir):
    policy = claim_policy(cfg)
    stale_after_ms = policy['stale_after_ms']
    if args.ack_phase:
        current = load_json(state_dir / 'wake.json', {})
        if not args.wake_id or current.get('wake_id') != args.wake_id:
            rejected(state_dir, args.wake_id, 'ack_' + args.ack_phase)
            return SILENT
        previous = load_json(state_dir / 'wake_ack.json', {})
        if args.ack_phase == 'started':
            if previous.get('wake_id') == args.wake_id:
                rejected(state_dir, args.wake_id, 'ack_started', reason='already_acknowledged')
                return SILENT
        elif previous.get('wake_id') != args.wake_id or previous.get('phase') != 'started':
            rejected(state_dir, args.wake_id, 'ack_' + args.ack_phase, reason='claim_not_started')
            return SILENT
        write_json(state_dir / 'wake_ack.json', {'wake_id': args.wake_id, 'phase': args.ack_phase,
                                                 'at_ms': now_ms()})
        append_decision_trace(state_dir, args.wake_id, 'ack_' + args.ack_phase, ack_at_ms=now_ms())
        if args.ack_phase in ('finished', 'failed'):
            # The wake is over; a lease it still holds would silence the gate until expiry.
            release_wake_lease(cfg, state_dir, args.wake_id, 'ack_' + args.ack_phase)
        if args.ack_phase in ('started', 'finished'):
            write_json(state_dir / 'gate_health.json', {})
        return WAKE
    if args.heartbeat:
        return WAKE if refresh_claim(state_dir, args.wake_id, 'manual') else SILENT
    if args.apply_claimed:
        if not claim_is_current(state_dir, args.wake_id):
            rejected(state_dir, args.wake_id, 'apply', turn_id=args.turn_id)
            return SILENT
        argv = [cfg['python'], cfg['ctl'], '--db', cfg['db'], 'apply', '--session', cfg['session'],
                '--decision', args.apply_claimed]
        if args.turn_id:
            argv += ['--turn-id',args.turn_id,'--wake-id',args.wake_id]
        apply_started_ms = now_ms()
        append_decision_trace(state_dir, args.wake_id, 'apply_started', turn_id=args.turn_id)
        try:
            result = subprocess.run(argv, check=False, timeout=60, stdout=subprocess.PIPE,
                                    text=True, encoding='utf-8')
        except subprocess.TimeoutExpired:
            append_decision_trace(state_dir, args.wake_id, 'apply_finished',
                                  turn_id=args.turn_id, exit_code=124, elapsed_ms=now_ms() - apply_started_ms)
            print('yujin gate: claimed apply timed out', file=sys.stderr)
            return 124
        except OSError as exc:
            append_decision_trace(state_dir, args.wake_id, 'apply_finished',
                                  turn_id=args.turn_id, exit_code=127, elapsed_ms=now_ms() - apply_started_ms)
            print(f'yujin gate: claimed apply could not start: {exc}', file=sys.stderr)
            return 127
        if result.stdout:
            print(result.stdout, end='')
        try:
            reason = json.loads(result.stdout).get('reason')
            if not isinstance(reason, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', reason):
                reason = 'controller_result'
        except (ValueError, AttributeError):
            reason = 'controller_output_unparseable'
        append_decision_trace(state_dir, args.wake_id, 'apply_finished', reason=reason,
                              turn_id=args.turn_id, exit_code=result.returncode, elapsed_ms=now_ms() - apply_started_ms)
        if result.returncode == 0:
            write_json(state_dir / 'wake_ack.json', {'wake_id': args.wake_id, 'phase': 'started',
                                                     'at_ms': now_ms()})
        return result.returncode
    gate_path = state_dir / 'gate_state.json'
    gate_state = load_json(gate_path, {})
    ack = load_json(state_dir / 'wake_ack.json', {})
    health = load_json(state_dir / 'poller_health.json', None)
    SLACK_HEALTH.clear()
    SLACK_HEALTH.update(load_json(state_dir / 'slack_health.json', {}))
    now = now_ms()
    try:
        tick = read_tick(cfg)
        orphan = orphaned_lease_owner(tick, gate_state, ack, now, stale_after_ms)
        if orphan and args.check:
            tick = {**tick, 'conversation_lease_until_ms': None}   # --claim releases it; stay read-only
        elif orphan:
            # The wake that set this lease ended (deadline/failed/finished) without ending the
            # conversation. Release its lease so pending DMs are claimed now, not at expiry.
            if (release_wake_lease(cfg, state_dir, orphan, 'orphaned_lease') or {}).get('released'):
                tick = read_tick(cfg)
        if tick.get('conversation_lease_until_ms'):
            if args.check:
                print(json.dumps({'ok': True, 'has_work': False, 'reason': 'conversation_lease',
                                  'wake_eligible': False,
                                  'received_count': sum(m['status'] == 'received' for m in tick['messages'])}))
                return WAKE
            # A Muse conversation session owns the DMs right now (wait-dm). Never start a second run.
            write_json(state_dir / 'gate_check.json', {
                'at_ms': now, 'result': 'silent', 'reason': 'conversation_lease',
                'lease_until_ms': tick['conversation_lease_until_ms'],
                'received_count': sum(m['status'] == 'received' for m in tick['messages'])})
            if any(m['status'] == 'received' for m in tick['messages']):
                append_decision_trace(state_dir, gate_state.get('wake_id'), 'gate_silent',
                                      reason='conversation_lease')
            return SILENT
        reasons, detail = evaluate(tick, health, now)
        revision = tick['revision']
    except Exception as exc:   # DB or controller broken: let Muse report it.
        if args.check:
            print(json.dumps({'ok': False, 'reason': 'controller_error', 'detail': type(exc).__name__}))
            return 2
        reasons, detail, revision = ['controller_error'], {'error': type(exc).__name__ + ': ' + str(exc)[:200],
                                                           'fingerprint_basis': {'reasons': ['controller_error']}}, None
    if args.check:
        eligible, _ = decide(reasons, detail, gate_state, now, ack, stale_after_ms)
        print(json.dumps({'ok': True, 'has_work': bool(reasons), 'reasons': reasons,
                          'wake_eligible': eligible,
                          'received_count': sum(m['status'] == 'received' for m in tick['messages']),
                          'revision': revision}))
        return WAKE
    wake, attempt = decide(reasons, detail, gate_state, now, ack, stale_after_ms)
    delays = retry_delays(reasons)
    if gate_state.get('fingerprint') and reasons:
        matching_ack = ack.get('wake_id') == gate_state.get('wake_id')
        failure = ('worker_not_started' if not matching_ack and now - gate_state.get('last_wake_ms', 0) >= delays[1]
                   else 'worker_stalled' if matching_ack and ack.get('phase') == 'started'
                   and now - ack.get('at_ms', 0) >= stale_after_ms else None)
        if failure:
            previous_alert = load_json(state_dir / 'gate_health.json', {})
            if (previous_alert.get('alert'), previous_alert.get('wake_id')) != (failure, gate_state.get('wake_id')):
                write_json(state_dir / 'gate_health.json', {'alert': failure, 'at_ms': now,
                                                            'wake_id': gate_state.get('wake_id'),
                                                            'reasons': gate_state.get('reasons', [])})
                print(f'yujin gate alert: {failure} (wake {gate_state.get("wake_id")})')
    if not wake:
        active_worker = active_claim(gate_state, ack, now, stale_after_ms)
        write_json(state_dir / 'gate_check.json', {
            'at_ms': now, 'result': 'silent',
            'reason': 'no_work' if not reasons else 'worker_active' if active_worker else 'retry_wait',
            'reasons': reasons, 'attempts': gate_state.get('attempts', 0)})
        if reasons:
            append_decision_trace(state_dir, gate_state.get('wake_id'), 'gate_silent',
                                  reason='worker_active' if active_worker else 'retry_wait')
        if not reasons and gate_state.get('fingerprint') and not active_worker:
            append_decision_trace(state_dir, gate_state.get('wake_id'), 'claim_cleared',
                                  reason='no_work', stale_after_ms=stale_after_ms,
                                  **claim_metadata(state_dir, now))
            write_json(gate_path, {})          # situation resolved; reset the retry counter
            write_json(state_dir / 'gate_health.json', {})
        elif reasons and gate_state.get('attempts', 0) >= len(delays) and not active_worker:
            previous_alert = load_json(state_dir / 'gate_health.json', {})
            if (previous_alert.get('alert'), previous_alert.get('wake_id')) != ('retry_exhausted', gate_state.get('wake_id')):
                write_json(state_dir / 'gate_health.json', {'alert': 'retry_exhausted', 'at_ms': now,
                                                            'wake_id': gate_state.get('wake_id'),
                                                            'reasons': reasons})
                print('yujin gate alert: retry_exhausted')
        return SILENT
    # Tell Muse when the previous situation gave up without being resolved, so it can report it.
    if gate_state.get('fingerprint') != detail['fingerprint']:
        previous_reasons = gate_state.get('reasons') or []
        stuck = previous_reasons if gate_state.get('attempts', 0) >= len(retry_delays(previous_reasons)) else None
    else:
        stuck = gate_state.get('previous_stuck')
    wake_id = uuid.uuid4().hex
    bound = {'claim_deadline_ms': now + policy['max_run_ms']} if args.claim and policy['auto_heartbeat'] else {}
    if args.claim:
        try:
            execution = worker_budget.bind(cfg, getattr(args, 'invocation_id', None), wake_id, now)
        except ControlError as exc:
            print(json.dumps({'ok': False, 'reason': exc.reason}))
            return 2
        if execution:
            bound.update(worker_execution=execution,
                         claim_deadline_ms=min(now + policy['max_run_ms'], execution['deadline_ms']))
    if gate_state.get('wake_id'):
        matching = ack.get('wake_id') == gate_state['wake_id']
        expired = now >= gate_state.get('claim_deadline_ms', now + 1)
        replacement_reason = ('claim_deadline' if expired and matching and ack.get('phase') == 'started'
                              else 'pickup_expired' if matching and ack.get('phase') == 'started'
                              and type(ack.get('pickup_deadline_ms')) is int
                              and now >= ack['pickup_deadline_ms']
                              else 'worker_stalled' if matching and ack.get('phase') == 'started'
                              else 'previous_finished' if matching and ack.get('phase') == 'finished'
                              else 'previous_failed' if matching and ack.get('phase') == 'failed'
                              else 'worker_not_started')
        append_decision_trace(state_dir, wake_id, 'claim_replaced',
                              previous_wake_id=gate_state['wake_id'], reason=replacement_reason,
                              fingerprint_changed=gate_state.get('fingerprint') != detail['fingerprint'],
                              stale_after_ms=stale_after_ms, **claim_metadata(state_dir, now))
    write_json(gate_path, {'fingerprint': detail['fingerprint'], 'attempts': attempt, 'last_wake_ms': now,
                           'wake_id': wake_id, 'reasons': reasons, 'previous_stuck': stuck, **bound})
    detail.pop('fingerprint_basis', None)
    write_json(state_dir / 'wake.json', {'wake_id': wake_id, 'woken_ms': now, 'revision': revision, 'reasons': reasons,
                                         'attempt': attempt, 'max_attempts': len(delays),
                                         'last_attempt': attempt >= len(delays) and gate_state.get('attempts', 0) < len(delays),
                                         'recovery_retry': gate_state.get('fingerprint') == detail['fingerprint']
                                                           and gate_state.get('attempts', 0) >= len(delays),
                                         'previous_stuck_reasons': stuck, **detail, **bound})
    if args.claim:
        write_json(state_dir / 'wake_ack.json', {'wake_id': wake_id, 'phase': 'started', 'at_ms': now})
        append_decision_trace(state_dir, wake_id, 'claimed', reasons=reasons, attempt=attempt,
                              ack_at_ms=now, stale_after_ms=stale_after_ms,
                              worker_budget_basis=bound.get('worker_execution', {}).get('basis'),
                              claim_deadline_ms=bound.get('claim_deadline_ms'))
        if policy['auto_heartbeat']:
            try:
                from claim_heartbeat import start
                start(cfg, wake_id)
            except (ControlError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                write_json(state_dir / 'wake_ack.json', {'wake_id': wake_id, 'phase': 'failed',
                           'at_ms': now_ms(), 'reason': 'heartbeat_start_failed'})
                append_decision_trace(state_dir, wake_id, 'ack_failed', reason='heartbeat_start_failed',
                                      error_type=type(exc).__name__)
                print('yujin gate: heartbeat_start_failed', file=sys.stderr)
                return 1
    else:
        append_decision_trace(state_dir, wake_id, 'wake_requested', reasons=reasons, attempt=attempt)
    write_json(state_dir / 'gate_check.json', {
        'at_ms': now, 'result': 'claimed' if args.claim else 'wake_requested',
        'reasons': reasons, 'attempt': attempt, 'wake_id': wake_id})
    print(f'yujin wake: {",".join(reasons)} (attempt {attempt}/{len(delays)}); read automation-state/wake.json')
    return WAKE


if __name__ == '__main__':
    sys.exit(main())
