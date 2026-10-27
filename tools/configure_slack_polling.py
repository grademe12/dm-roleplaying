#!/usr/bin/env python3
"""Preview/apply internal-app polling settings locally on the operator's VM.

No Slack HTTP calls or service restarts. Runtime files and backups never belong in Git.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')


def controller(cfg, command, document=None):
    argv = [cfg['python'], cfg['ctl'], '--db', cfg['db'], command,
            '--session', cfg.get('session', 'main')]
    if document is not None:
        argv += ['--settings', '@-']
    result = subprocess.run(argv, input=json.dumps(document) if document is not None else None,
                            capture_output=True, text=True, timeout=30)
    value = json.loads(result.stdout)
    if result.returncode or not value.get('ok'):
        raise RuntimeError('controller_configuration_failed')
    return value


def configure(path, seconds=15, apply=False, internal_app_confirmed=False):
    if not isinstance(seconds, int) or not 1 <= seconds <= 600:
        raise ValueError('invalid_polling_interval')
    if seconds < 60 and not internal_app_confirmed:
        raise ValueError('internal_app_confirmation_required')
    path = Path(path).expanduser().resolve()
    cfg = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(cfg.get('slack'), dict) or not Path(cfg['db']).expanduser().is_file():
        raise ValueError('existing_runtime_configuration_required')
    old = controller(cfg, 'slack-delivery-pending')['settings']
    current = json.loads(json.dumps(cfg))
    current['slack']['backfill_seconds'] = seconds
    settings = {**old, 'history_min_interval_ms': seconds * 1000}
    summary = {'ok': True, 'applied': False, 'backfill_seconds': seconds,
               'history_min_interval_ms': seconds * 1000, 'restart_listener_required': apply,
               'existing_retry_after_preserved': True}
    if not apply:
        return summary
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    backup = path.parent / 'backups' / ('slack-polling-' + stamp)
    backup.mkdir(parents=True, mode=0o700)
    private_json(backup / 'automation.json', cfg)
    private_json(backup / 'delivery-settings.json', old)
    # Prepare the file before updating DB. Never reset the history budget/Retry-After.
    fd, temporary = tempfile.mkstemp(prefix='.slack-polling-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(current, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        controller(cfg, 'slack-delivery-configure', settings)
        try:
            os.replace(temporary, path)
        except OSError:
            # DB/file cannot share a transaction. Restore the previous settings if file commit fails.
            controller(cfg, 'slack-delivery-configure', old)
            raise
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {**summary, 'applied': True, 'backup_dir': str(backup)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--interval-seconds', type=int, default=15)
    parser.add_argument('--internal-app-confirmed', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = configure(args.config, args.interval_seconds, args.apply, args.internal_app_confirmed)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        # Runtime exceptions may include paths, settings, or tool output. Keep stdout minimal.
        result = {'ok': False, 'reason': 'polling_configuration_failed'}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
