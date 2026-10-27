#!/usr/bin/env python3
"""Validate locally, then optionally stage a response. Never drives an executor."""
import argparse
import json
from pathlib import Path
import sys

from common import ControlError, encode, file_lock, now_ms, require
import muse_executor
import role_executor
import role_preflight
import gate


def pickup(request_path, now=None):
    """Stamp when a session picks an input up, so the step's trace can split
    into session wait (prepared -> pickup) and model work (pickup -> staged).
    The marker sits next to the input file, which lives in the spool folder.
    First stamp wins: re-reads of the same input must not move it."""
    request = json.loads(Path(request_path).read_text(encoding='utf-8-sig'))
    ident = request['request_id']
    require(isinstance(ident, str) and len(ident) == 32 and all(c in '0123456789abcdef' for c in ident),
            'invalid_role_request_id')
    folder = Path(request_path).resolve().parent
    target = folder / ('role-model-pickup-' + ident + '.json')
    stamp = now_ms() if now is None else now
    with file_lock(folder / 'pickup', ident, private=True):
        existing = gate.load_json(target, {})
        if type(existing.get('picked_up_ms')) is int:
            return {'ok': True, 'reason': 'pickup_already_recorded', 'request_id': ident,
                    'picked_up_ms': existing['picked_up_ms']}
        muse_executor.private_write(target, encode({'request_id': ident, 'picked_up_ms': stamp}))
    return {'ok': True, 'reason': 'pickup_recorded', 'request_id': ident, 'picked_up_ms': stamp}


def validate(request_path, response_path, now=None):
    # Input delivery budgets are advisory; do not impose the model RESPONSE cap on a request.
    request = json.loads(Path(request_path).read_text(encoding='utf-8-sig'))
    response = role_executor.read_response(Path(response_path))
    return request, response, role_preflight.check(request, response, now_ms() if now is None else now)


def submit(config, request_path, response_path, now=None):
    cfg = role_executor.load_config(config)
    request, response, result = validate(request_path, response_path, now)
    opts=role_executor.settings(cfg)
    require(opts['enabled'], 'role_execution_off')
    folder = opts['path']
    ident = request['request_id']
    require(isinstance(ident, str) and len(ident) == 32 and all(c in '0123456789abcdef' for c in ident),
            'invalid_role_request_id')
    matches = []
    for path in folder.glob('run-*.json'):
        state = json.loads(path.read_text())
        pending = state.get('pending') or {}
        if (pending.get('id') == ident and pending.get('request') == request) or ident in state.get('accepted',{}):
            matches.append(state)
    require(len(matches) == 1, 'role_request_superseded')
    source=matches[0]['binding']
    require(source['db']==str(Path(cfg['db']).resolve()) and source['session']==cfg['session']
            and source['state_dir']==str(Path(cfg['state_dir']).resolve())
            and source['persona']==cfg.get('persona_id','yujin'), 'role_binding_mismatch')
    if ident in matches[0].get('accepted',{}):
        require(role_executor.contract.digest(response)==matches[0]['accepted'][ident], 'role_response_changed')
        return {'ok':True,'reason':'already_accepted','request_id':ident,'next':'driver','committed':False,'replayed':True}
    if not result['ok']:
        return result
    target = folder / ('role-model-response-' + ident + '.json')
    with gate.gate_lock(Path(cfg['state_dir'])):
        require(gate.claim_is_current(Path(cfg['state_dir']),source['wake'],now_ms() if now is None else now),
                'stale_claim')
        with file_lock(folder / 'response-submit', ident, private=True):
            if target.is_file():
                require(role_executor.read_response(target) == response, 'role_response_changed')
            else:
                muse_executor.private_write(target, encode(response))
    return {**result, 'reason': 'response_staged', 'request_id': ident, 'next': 'driver', 'committed': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('validate', 'submit', 'pickup'))
    parser.add_argument('--request', required=True)
    parser.add_argument('--response')
    parser.add_argument('--config')
    args = parser.parse_args(argv)
    try:
        if args.action == 'pickup':
            result = pickup(args.request.removeprefix('@'))
        elif args.action == 'submit':
            require(bool(args.config), 'config_required')
            require(bool(args.response), 'response_required')
            result = submit(args.config, args.request.removeprefix('@'), args.response.removeprefix('@'))
        else:
            require(bool(args.response), 'response_required')
            _, _, result = validate(args.request.removeprefix('@'), args.response.removeprefix('@'))
    except (ControlError, OSError, ValueError, KeyError, TypeError) as exc:
        result = {'ok': False, 'reason': exc.reason if isinstance(exc, ControlError) else 'response_file_invalid'}
    print(encode(result))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
