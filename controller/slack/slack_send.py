#!/usr/bin/env python3
"""dm-send-v2 transport wrapper for Slack (standard library only).

argv: slack_send.py /abs/slack-send.json
stdin: one controller request (text bubble, or kind=image with attachment_path)
stdout: one JSON result {"status": confirmed|failed|unknown, ...}

The token is read from token_path at call time and never printed, logged or stored.
Result rules (no retries here; the controller never resends unknown results):
- Slack answered ok           -> confirmed (provider_message_id = message ts / file id)
- HTTP 429 or "ratelimited"   -> failed, not_sent, code 429, retry_after_ms
- Slack answered ok=false with an error that means nothing was posted -> failed, not_sent
- no answer after the request may have been delivered (timeout, reset) -> unknown
"""
import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from dataclasses import dataclass

NOT_SENT_ERRORS = {
    'channel_not_found', 'not_in_channel', 'is_archived', 'msg_too_long', 'no_text', 'invalid_auth',
    'not_authed', 'account_inactive', 'token_revoked', 'token_expired', 'missing_scope', 'restricted_action',
    'invalid_arguments', 'invalid_blocks', 'too_many_attachments', 'cannot_reply_to_message',
    'invalid_channel', 'file_not_found', 'upload_failed', 'file_upload_size_restricted', 'invalid_args'}


@dataclass
class SendState:
    publish_possible: bool = False
    confirmed_result: dict | None = None


class SlackError(Exception):
    def __init__(self, result):
        super().__init__(result.get('evidence'))
        self.result = result


def load_config(path):
    cfg = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    for key in ('channel', 'token_path'):
        if not isinstance(cfg.get(key), str) or not cfg[key]:
            raise SystemExit(f'config missing {key}')
    return cfg


def read_token(cfg):
    token = Path(cfg['token_path']).expanduser().read_text(encoding='utf-8').strip()
    if not token.startswith(('xoxb-', 'xoxp-')):
        raise SlackError({'status': 'failed', 'not_sent': True, 'evidence': 'token_file_invalid'})
    return token


def escape(text):
    # Slack mrkdwn control characters; everything else is sent as written.
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def call(cfg, token, method, payload=None, form=None, sent_is_possible=True):
    """POST to a Slack Web API method. Raises SlackError with a controller result on failure."""
    url = cfg.get('api_base', 'https://slack.com/api').rstrip('/') + '/' + method
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        ctype = 'application/x-www-form-urlencoded'
    else:
        body = json.dumps(payload or {}, ensure_ascii=False).encode('utf-8')
        ctype = 'application/json; charset=utf-8'
    req = urllib.request.Request(url, data=body, method='POST',
                                 headers={'Authorization': 'Bearer ' + token, 'Content-Type': ctype})
    state = cfg.get('_send_state')
    if state is not None and sent_is_possible:
        state.publish_possible = True
    return _send(req, cfg, method, sent_is_possible)


def _send(req, cfg, label, sent_is_possible):
    timeout = cfg.get('timeout_seconds', 20)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            retry = exc.headers.get('Retry-After')
            result = {'status': 'failed', 'not_sent': True, 'code': 429, 'evidence': f'{label} http 429'}
            if retry and retry.isdigit():
                result['retry_after_ms'] = int(retry) * 1000
            raise SlackError(result)
        raise SlackError({'status': 'unknown' if sent_is_possible else 'failed', 'not_sent': not sent_is_possible,
                          'evidence': f'{label} http {exc.code}'})
    except (socket.timeout, TimeoutError):
        raise SlackError({'status': 'unknown' if sent_is_possible else 'failed', 'not_sent': not sent_is_possible,
                          'evidence': f'{label} timeout'})
    except urllib.error.URLError as exc:
        # DNS failure or refused connection: the request never reached Slack.
        reason = exc.reason
        never_sent = isinstance(reason, (socket.gaierror, ConnectionRefusedError))
        if never_sent or not sent_is_possible:
            raise SlackError({'status': 'failed', 'not_sent': True, 'evidence': f'{label} connection failed'})
        raise SlackError({'status': 'unknown', 'evidence': f'{label} connection error'})
    if label.startswith('upload'):
        return {'ok': True}
    try:
        data = json.loads(raw.decode('utf-8'))
    except ValueError:
        raise SlackError({'status': 'unknown' if sent_is_possible else 'failed', 'not_sent': not sent_is_possible,
                          'evidence': f'{label} unparseable response'})
    if data.get('ok') is True:
        return data
    error = str(data.get('error', 'unknown_error'))
    if error == 'ratelimited':
        raise SlackError({'status': 'failed', 'not_sent': True, 'code': 429, 'evidence': f'{label} ratelimited'})
    if error in NOT_SENT_ERRORS or not sent_is_possible:
        raise SlackError({'status': 'failed', 'not_sent': True, 'evidence': f'{label} {error}'})
    raise SlackError({'status': 'unknown', 'evidence': f'{label} {error}'})


def send_text(cfg, token, text, receipt=None):
    payload = {'channel': cfg['channel'], 'text': escape(text), 'unfurl_links': False, 'unfurl_media': False}
    if receipt is not None:
        if (not isinstance(receipt, dict) or receipt.get('channel_id') != cfg['channel']
                or not isinstance(receipt.get('delivery_id'), str) or not receipt['delivery_id']
                or not isinstance(receipt.get('event_type'), str) or not receipt['event_type']):
            raise SlackError({'status': 'failed', 'not_sent': True, 'evidence': 'invalid_delivery_binding'})
        payload['metadata'] = {'event_type': receipt['event_type'],
                               'event_payload': {'delivery_id': receipt['delivery_id']}}
    data = call(cfg, token, 'chat.postMessage', payload)
    if not isinstance(data.get('ts'), str) or not data['ts']:
        raise SlackError({'status': 'unknown', 'evidence': 'chat.postMessage missing message ts'})
    result = {'status': 'confirmed', 'provider_message_id': data['ts'], 'evidence': 'chat.postMessage ok'}
    if cfg.get('_send_state') is not None:
        cfg['_send_state'].confirmed_result = result
    return result


def send_image(cfg, token, path):
    path = Path(path)
    if not path.is_file():
        return {'status': 'failed', 'not_sent': True, 'evidence': 'attachment_missing'}
    data = path.read_bytes()
    # Steps 1-2 cannot post anything to the channel, so their failures are always "not sent".
    meta = call(cfg, token, 'files.getUploadURLExternal', form={'filename': path.name, 'length': str(len(data))},
                sent_is_possible=False)
    req = urllib.request.Request(meta['upload_url'], data=data, method='POST',
                                 headers={'Content-Type': 'application/octet-stream'})
    _send(req, cfg, 'upload', sent_is_possible=False)
    # Step 3 shares the file into the DM; an unanswered call may already have shared it.
    call(cfg, token, 'files.completeUploadExternal',
         {'files': [{'id': meta['file_id'], 'title': path.name}], 'channel_id': cfg['channel']})
    result = {'status': 'confirmed', 'provider_message_id': meta['file_id'], 'evidence': 'files.completeUploadExternal ok'}
    if cfg.get('_send_state') is not None:
        cfg['_send_state'].confirmed_result = result
    return result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        raise SystemExit('usage: slack_send.py /abs/slack-send.json')
    state = SendState()
    try:
        cfg = load_config(argv[0])
        cfg['_send_state'] = state
        request = json.loads(sys.stdin.buffer.read().decode('utf-8'))
        if request.get('contract') not in ('dm-send-v1', 'dm-send-v2'):
            raise SlackError({'status': 'failed', 'not_sent': True, 'evidence': 'unknown_contract'})
        if request.get('channel', 'slack') != 'slack':
            raise SlackError({'status': 'failed', 'not_sent': True, 'evidence': 'wrong_channel_for_transport'})
        token = read_token(cfg)
        if request.get('kind') == 'image':
            result = send_image(cfg, token, request.get('attachment_path', ''))
        else:
            result = send_text(cfg, token, request['text'], request.get('delivery_receipt'))
    except SlackError as exc:
        result = exc.result
    except Exception as exc:
        # Classify by the publication stage, including previously unknown exception types.
        # A failed or timed-out response after a publishing request is never proof of non-send.
        if state.confirmed_result is not None:
            result = state.confirmed_result
        elif state.publish_possible:
            result = {'status': 'unknown', 'evidence': 'publish_result_unresolved: ' + type(exc).__name__}
        else:
            result = {'status': 'failed', 'not_sent': True, 'evidence': 'prepublish_error: ' + type(exc).__name__}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
