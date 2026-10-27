#!/usr/bin/env python3
"""Best-effort re-wake after a wake ended with unanswered DMs.

Reuses the Slack listener's webhook nudge config (automation.json "nudge": url_path,
key_path, key_header) and request shape. URL and key are secrets: never logged.
Debounced across processes (state file under gate_lock) and at most once per wake,
so a wake that keeps failing cannot turn this into a loop. Failures are traced, never raised.
"""
import json
from pathlib import Path
import time
import urllib.request

import gate

STATE_FILE = 'wake_nudge.json'
MIN_INTERVAL_MS = 60_000
TIMEOUT_SECONDS = 5


def _config(cfg):
    nudge = cfg.get('nudge')
    if isinstance(nudge, dict) and nudge.get('enabled') is True and nudge.get('url_path'):
        return nudge
    return None


def _post(nudge, body):
    url = Path(nudge['url_path']).expanduser().read_text(encoding='utf-8').strip()
    headers = {'Content-Type': 'application/json'}
    if nudge.get('key_path'):
        key = Path(nudge['key_path']).expanduser().read_text(encoding='utf-8').strip()
        name = nudge.get('key_header', 'Authorization')
        headers[name] = f'Bearer {key}' if name.lower() == 'authorization' else key
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        resp.read(1024)


def interval_ms(nudge):
    try:
        configured = int(float(nudge.get('min_interval_seconds', 0)) * 1000)
    except (TypeError, ValueError):
        configured = 0
    return max(MIN_INTERVAL_MS, configured)


def send(cfg, wake_id, reason, *, now=None, post=_post):
    """Returns 'sent', 'disabled', 'duplicate_wake', 'debounced' or 'failed'."""
    state_dir = Path(cfg['state_dir'])
    nudge = _config(cfg)
    if nudge is None:
        gate.append_decision_trace(state_dir, wake_id, 'wake_nudge_skipped', reason=reason, result='disabled')
        return 'disabled'
    now = gate.now_ms() if now is None else now
    with gate.gate_lock(state_dir):
        last = gate.load_json(state_dir / STATE_FILE, {})
        result = ('duplicate_wake' if last.get('wake_id') == wake_id
                  else 'debounced' if type(last.get('at_ms')) is int
                  and 0 <= now - last['at_ms'] < interval_ms(nudge) else None)
        if result is None:
            gate.write_json(state_dir / STATE_FILE, {'wake_id': wake_id, 'at_ms': now, 'reason': reason})
    if result:
        gate.append_decision_trace(state_dir, wake_id, 'wake_nudge_skipped', reason=reason, result=result)
        return result
    body = json.dumps({'session': cfg['session'], 'reason': reason,
                       'ts': f'{now / 1000:.6f}'}).encode('utf-8')
    try:
        post(nudge, body)
    except Exception as exc:   # secrets stay out of logs: type only
        gate.append_decision_trace(state_dir, wake_id, 'wake_nudge_failed', reason=reason,
                                   error_type=type(exc).__name__)
        return 'failed'
    gate.append_decision_trace(state_dir, wake_id, 'wake_nudge_sent', reason=reason)
    return 'sent'
