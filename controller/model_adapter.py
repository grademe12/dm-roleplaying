"""Task-neutral JSON model boundary. No controller, gate or memory-store access."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from common import ControlError, encode, integer, require, text

REQUEST_SCHEMA = 'persona-model-request-v1'
RESPONSE_SCHEMA = 'persona-model-response-v1'
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def resolve_paths(cfg, config_path):
    """Resolve file settings against the config, without validating unused adapters."""
    result = copy.deepcopy(cfg)
    registry = result.get('model_adapters')
    if not isinstance(registry, dict):
        return result
    base = Path(config_path).expanduser().resolve().parent
    for options in registry.values():
        if not isinstance(options, dict):
            continue
        for field in ('key_file', 'cwd'):
            value = options.get(field)
            if isinstance(value, str) and value:
                path = Path(value).expanduser()
                options[field] = str(path if path.is_absolute() else base / path)
        argv = options.get('argv')
        if isinstance(argv, list) and argv and isinstance(argv[0], str) and '/' in argv[0]:
            path = Path(argv[0]).expanduser()
            argv[0] = str(path if path.is_absolute() else base / path)
    return result


def endpoint(value):
    """An OpenAI-compatible chat completions URL; there is no built-in provider."""
    url = text(value, 'model_endpoint')
    try:
        parsed = urllib.parse.urlsplit(url)
        parsed.port  # Validate configured ports before any child/model work.
    except ValueError as exc:
        raise ControlError('invalid_model_endpoint') from exc
    require(parsed.scheme in ('http', 'https') and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None and not parsed.fragment,
            'invalid_model_endpoint')
    return url


def options(cfg, name=None, legacy=None):
    if name is None:
        require(isinstance(legacy, dict) and bool(legacy.get('model')) and bool(legacy.get('key_file')),
                'model_not_configured')
        value = {'backend': 'http', 'model': legacy['model'], 'key_file': legacy['key_file'],
                 'endpoint': legacy.get('endpoint')}
    else:
        require(isinstance(name, str) and bool(name.strip()), 'invalid_model_adapter')
        registry = cfg.get('model_adapters')
        require(isinstance(registry, dict) and isinstance(registry.get(name), dict), 'invalid_model_adapter')
        value = dict(registry[name])
    value = {'timeout_seconds': 120, 'max_tokens': 4096, **value}
    backend = value.get('backend')
    require(backend in ('http', 'command', 'file'), 'invalid_model_backend')
    integer(value['timeout_seconds'], 'model_timeout', 1)
    require(value['timeout_seconds'] <= 1800, 'invalid_model_timeout')
    integer(value['max_tokens'], 'model_max_tokens', 1)
    if backend == 'http':
        text(value.get('model'), 'model')
        if value.get('key_file') is not None:
            text(value['key_file'], 'model_key_file')
        value['endpoint'] = endpoint(value.get('endpoint'))
    elif backend == 'command':
        argv = value.get('argv')
        if value.get('cwd') is not None:
            text(value['cwd'], 'model_cwd')
        require(isinstance(argv, list) and bool(argv) and all(isinstance(x, str) and x for x in argv),
                'invalid_model_command')
    if value.get('model') is not None:
        text(value['model'], 'model')
    return value


def request(task, request_id, input_sha256, instructions, payload, opts, deadline_ms):
    return {'schema': REQUEST_SCHEMA, 'task': text(task), 'request_id': text(request_id),
            'input_sha256': text(input_sha256), 'instructions': text(instructions), 'input': payload,
            'instructions_sha256': hashlib.sha256(instructions.encode('utf-8')).hexdigest(),
            'model': opts.get('model'), 'max_tokens': opts['max_tokens'], 'deadline_ms': deadline_ms,
            'response_contract': {'schema': RESPONSE_SCHEMA, 'request_id': request_id,
                                  'input_sha256': input_sha256, 'output': '<task result JSON>'}}


def usage(value):
    value = value if isinstance(value, dict) else {}
    result = {key: value[key] for key in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'cached_tokens')
              if type(value.get(key)) is int and value[key] >= 0}
    cost = value.get('cost_usd')
    if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
        result['cost_usd'] = cost
    return result


def result(req, output, reported_usage=None):
    return {'schema': RESPONSE_SCHEMA, 'request_id': req['request_id'],
            'input_sha256': req['input_sha256'], 'output': output, 'usage': usage(reported_usage)}


def validate_result(value, req):
    require(isinstance(value, dict) and value.get('schema') == RESPONSE_SCHEMA and 'output' in value,
            'model_output_invalid')
    require(value.get('request_id') == req['request_id'] and value.get('input_sha256') == req['input_sha256'],
            'model_input_mismatch')
    # Do not forward arbitrary backend diagnostics, prompts or credential fields.
    return result(req, value['output'], value.get('usage'))


def chat_body(opts, req):
    body = {'model': opts['model'], 'messages': [{'role': 'system', 'content': req['instructions']},
            {'role': 'user', 'content': encode(req['input'])}], 'max_tokens': req['max_tokens']}
    return body


def parse_chat(value, req):
    require(isinstance(value, dict), 'model_output_invalid')
    choices = value.get('choices')
    require(isinstance(choices, list) and bool(choices) and isinstance(choices[0], dict), 'model_output_invalid')
    require(choices[0].get('finish_reason') == 'stop', 'model_incomplete')
    try:
        output = json.loads(choices[0]['message']['content'])
    except (ValueError, TypeError, KeyError) as exc:
        raise ControlError('model_output_invalid') from exc
    reported = usage(value.get('usage'))
    details = (value.get('usage') or {}).get('prompt_tokens_details') if isinstance(value.get('usage'), dict) else None
    if isinstance(details, dict) and type(details.get('cached_tokens')) is int and details['cached_tokens'] >= 0:
        reported['cached_tokens'] = details['cached_tokens']
    cost = value.get('cost')
    if isinstance(cost, dict):
        reported.update(usage({'cost_usd': cost.get('usd')}))
    return result(req, output, reported)


def terminate(proc):
    if os.name != 'nt':
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        proc.kill()
    try:
        proc.communicate(timeout=1)
    except (subprocess.TimeoutExpired, ValueError):
        proc.stdout.close(); proc.stderr.close()
        proc.wait(timeout=1)


def process(argv, payload, timeout, cwd=None):
    serialized = encode(payload)
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding='utf-8', cwd=cwd, start_new_session=os.name != 'nt')
    except OSError as exc:
        raise ControlError('model_process_unavailable') from exc
    try:
        output, _ = proc.communicate(serialized, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        terminate(proc)
        raise ControlError('model_timeout') from exc
    except (ValueError, UnicodeError) as exc:
        terminate(proc)
        raise ControlError('model_output_invalid') from exc
    require(proc.returncode == 0, 'model_process_failed')
    require(len(output.encode('utf-8')) <= MAX_RESPONSE_BYTES, 'model_output_invalid')
    try:
        return json.loads(output)
    except ValueError as exc:
        raise ControlError('model_output_invalid') from exc


def key(opts):
    if not opts.get('key_file'):
        return None
    path = Path(opts['key_file']).expanduser()
    require(path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600, 'model_key_permissions')
    value = path.read_text(encoding='utf-8').strip()
    require(bool(value), 'model_key_empty')
    return value


def http_worker(payload):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    headers = {'Content-Type': 'application/json'}
    if payload.get('key'):
        headers['Authorization'] = 'Bearer ' + payload['key']
    req = urllib.request.Request(payload['endpoint'], data=encode(payload['body']).encode('utf-8'), headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=payload['timeout']) as stream:
            raw = stream.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return {'ok': False, 'reason': 'model_output_invalid'}
        return {'ok': True, 'response': json.loads(raw)}
    except urllib.error.HTTPError as exc:
        reason = 'http_' + str(exc.code)
        exc.close()
        return {'ok': False, 'reason': reason}
    except (OSError, TimeoutError):
        return {'ok': False, 'reason': 'network_error'}
    except (ValueError, ControlError):
        return {'ok': False, 'reason': 'model_output_invalid'}


def invoke(opts, req, timeout, transport=None):
    timeout = min(timeout, opts['timeout_seconds'])
    require(timeout > 0, 'model_timeout')
    if opts['backend'] == 'command':
        return validate_result(process(opts['argv'], req, timeout, opts.get('cwd')), req)
    require(opts['backend'] == 'http', 'model_file_handoff_required')
    body = chat_body(opts, req)
    if transport is not None:
        raw = transport(body, opts, timeout)
    else:
        envelope = process([sys.executable, str(Path(__file__).resolve()), '--http-worker'],
                           {'endpoint': opts['endpoint'], 'key': key(opts), 'body': body, 'timeout': timeout}, timeout)
        require(isinstance(envelope, dict) and envelope.get('ok') is True,
                envelope.get('reason', 'network_error') if isinstance(envelope, dict) else 'network_error')
        raw = envelope['response']
    return parse_chat(raw, req)


if __name__ == '__main__':
    try:
        require(sys.argv[1:] == ['--http-worker'], 'invalid_model_worker_command')
        print(encode(http_worker(json.loads(sys.stdin.read()))))
    except (OSError, ValueError, KeyError, TypeError, ControlError):
        print(encode({'ok': False, 'reason': 'model_worker_unavailable'}))
