#!/usr/bin/env python3
"""Read-only capability probe for an already-posted correlated Slack text message. Never sends."""
import argparse
import hashlib
import json
from pathlib import Path

from delivery_worker import ctl
from slack_send import SlackError, call, read_token


def probe(cfg, job_id, message_ts=None):
    code, info = ctl(cfg, 'slack-delivery-inspect', '--session', cfg['session'], '--job-id', job_id)
    if code or not info.get('ok'):
        return {'ok': False, 'reason': info.get('reason', 'probe_controller_error')}
    binding = info['settings']
    ts = message_ts or info.get('message_ts')
    if not ts:
        return {'ok': False, 'reason': 'probe_message_ts_required'}
    api = {'token_path': cfg['slack']['bot_token_path']}
    if cfg['slack'].get('api_base'):
        api['api_base'] = cfg['slack']['api_base']
    token = read_token(api)
    auth = call(api, token, 'auth.test', sent_is_possible=False)
    if auth.get('team_id') != binding['workspace_id'] or auth.get('user_id') != binding['self_user_id']:
        return {'ok': False, 'reason': 'probe_identity_mismatch'}
    code, budget = ctl(cfg, 'slack-history-claim', '--session', cfg['session'])
    if code or not budget.get('ok') or not budget.get('allowed'):
        return {'ok': False, 'reason': 'probe_history_budget_wait', 'retry_after_ms': budget.get('retry_after_ms')}
    try:
        history = call(api, token, 'conversations.history',
                       {'channel': binding['channel_id'], 'oldest': ts, 'latest': ts,
                        'inclusive': True, 'limit': 1, 'include_all_metadata': True}, sent_is_possible=False)
    except SlackError as exc:
        if exc.result.get('retry_after_ms'):
            ctl(cfg, 'slack-history-backoff', '--session', cfg['session'], '--retry-after-ms', str(exc.result['retry_after_ms']))
        raise
    messages = [m for m in history.get('messages', []) if m.get('ts') == ts]
    valid = False
    if len(messages) == 1:
        msg = messages[0]
        meta = msg.get('metadata') or {}
        valid = (msg.get('user') == binding['self_user_id'] and msg.get('app_id') == binding['app_id']
                 and meta.get('event_type') == binding['event_type']
                 and (meta.get('event_payload') or {}).get('delivery_id') == info['delivery_id']
                 and isinstance(msg.get('text'), str)
                 and hashlib.sha256(msg['text'].encode()).hexdigest() == info['wire_digest'])
    return {'ok': valid, 'reason': 'probe_metadata_history_verified' if valid else 'probe_evidence_mismatch',
            'metadata_history_verified': valid, 'events_verified': False, 'sent_any_message': False,
            'changed_delivery_status': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--message-ts')
    args = parser.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    try:
        result = probe(cfg, args.job_id, args.message_ts)
    except Exception as exc:
        result = {'ok': False, 'reason': 'probe_read_failed', 'error_type': type(exc).__name__, 'sent_any_message': False}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
