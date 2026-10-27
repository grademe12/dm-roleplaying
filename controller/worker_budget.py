"""Conservative worker budget from a local hook request, not a liveness probe.

The private runtime DB is unavailable here. A ticket issued BEFORE wake gives
an upper bound that includes worker startup, model work and local waits.
"""
import json
from pathlib import Path
import re
import uuid

from common import ControlError, integer, require


def policy(cfg):
    settings = cfg.get('worker_execution', {})
    require(isinstance(settings, dict), 'invalid_worker_execution')
    enabled = settings.get('enabled', False)
    require(type(enabled) is bool, 'invalid_worker_execution')
    strict = settings.get('require_invocation', False)
    require(type(strict) is bool, 'invalid_worker_execution')
    seconds = integer(settings.get('timeout_seconds', 600), 'worker_timeout_seconds', 1)
    reserve = integer(settings.get('finish_reserve_seconds', 20), 'worker_finish_reserve_seconds', 1)
    require(reserve < seconds <= 1800, 'invalid_worker_timing')
    return {'enabled': enabled, 'require_invocation': strict,
            'timeout_ms': seconds * 1000, 'reserve_ms': reserve * 1000}


def ticket_path(cfg, invocation_id):
    require(isinstance(invocation_id, str) and re.fullmatch(r'[a-f0-9]{32}', invocation_id),
            'invalid_worker_invocation')
    return Path(cfg['state_dir']) / 'worker-invocations' / (invocation_id + '.json')


def issue(cfg, now):
    import gate
    settings = policy(cfg)
    require(settings['enabled'], 'worker_budget_disabled')
    invocation_id = uuid.uuid4().hex
    record = {'schema_version': 1, 'invocation_id': invocation_id,
              'requested_ms': integer(now), 'deadline_ms': now + settings['timeout_ms'],
              'basis': 'local_hook_request'}
    folder = ticket_path(cfg, invocation_id).parent
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    gate.write_json(ticket_path(cfg, invocation_id), record)
    return record


def bind(cfg, invocation_id, wake_id, now):
    """Caller holds gate_lock. A ticket is consumed by exactly one claim."""
    import gate
    settings = policy(cfg)
    if not settings['enabled']:
        require(invocation_id is None, 'worker_budget_disabled')
        return {}
    if invocation_id is None:
        require(not settings['require_invocation'], 'worker_invocation_required')
        # Cron has no pre-wake hook timestamp. Bound from entry without pretending
        # this is the private runtime's created_at. Hook deployments must verify
        # local_hook_request to get the tighter, startup-inclusive deadline.
        return {'requested_ms': now, 'deadline_ms': now + settings['timeout_ms'],
                'finish_reserve_ms': settings['reserve_ms'], 'basis': 'worker_entry_fallback'}
    try:
        record = json.loads(ticket_path(cfg, invocation_id).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ControlError('worker_invocation_unavailable')
    require(isinstance(record, dict) and type(record.get('schema_version')) is int
            and record['schema_version'] == 1 and record.get('invocation_id') == invocation_id
            and record.get('basis') == 'local_hook_request', 'invalid_worker_invocation')
    requested = integer(record.get('requested_ms'), 'worker_requested_ms')
    deadline = integer(record.get('deadline_ms'), 'worker_deadline_ms')
    require(requested <= now and 0 < deadline - requested <= 1800_000, 'invalid_worker_invocation')
    # Config changes and delayed entry may shorten, never restart the original clock.
    deadline = min(deadline, requested + settings['timeout_ms'])
    require(now + settings['reserve_ms'] < deadline, 'worker_invocation_expired')
    require(not record.get('claimed_wake_id'), 'worker_invocation_consumed')
    record['claimed_wake_id'] = wake_id
    gate.write_json(ticket_path(cfg, invocation_id), record)
    return {'invocation_id': invocation_id, 'requested_ms': requested,
            'deadline_ms': deadline, 'finish_reserve_ms': settings['reserve_ms'],
            'basis': 'local_hook_request'}


def remaining(wake, session_started_ms, now, session_limit_ms):
    deadline = session_started_ms + session_limit_ms
    if type(wake.get('claim_deadline_ms')) is int:
        deadline = min(deadline, wake['claim_deadline_ms'])
    execution = wake.get('worker_execution') or {}
    reserve = 0
    if execution:
        deadline = min(deadline, execution['deadline_ms'])
        reserve = execution['finish_reserve_ms']
    return {'deadline_ms': deadline, 'finish_reserve_ms': reserve,
            'remaining_ms': max(0, deadline - now - reserve),
            'basis': execution.get('basis', 'controller_session')}
