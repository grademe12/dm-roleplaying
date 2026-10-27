#!/usr/bin/env python3
"""Dropbox exchange bridge for file-backend external processors (one pass per run).

Local folder <-> Dropbox <remote-root>/in and /out. A profile fixes the file prefix, remote root,
local folder and complete command. Default profile = memory organizer (episodic_memory.path's folder);
the character editor profile lives in character_exchange_bridge.py.
Uploads only <prefix>-model-input-<run_id>.json; downloads only exactly
<prefix>-model-response-<run_id>.json for runs whose input is still local.
Never prints or logs file contents: the single stdout line carries run_ids, counts,
byte sizes and reasons only. Exit 0 = clean pass, 1 = any error (soft failure).
Stdlib only; the Dropbox client is injectable (see DropboxClient)."""
import argparse
import base64
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_REMOTE_ROOT = '/yujin-project/memory-exchange'
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_INPUT_BYTES = 16 * 1024 * 1024
STALE_SECONDS = 24 * 3600
HERE = Path(__file__).resolve().parent
CRED_FILES = ('dropbox-app-key', 'dropbox-app-secret', 'dropbox-refresh-token')


class Profile:
    """What differs between exchanges: file prefix, remote root, local folder, executor script."""
    def __init__(self, prefix, remote_root, executor, resolve_dir, state_name, lock_name, enabled=None, complete_timeout=180):
        self.prefix = prefix
        self.remote_root = remote_root
        self.executor = executor
        self.resolve_dir = resolve_dir
        self.state_name = state_name
        self.lock_name = lock_name
        self.complete_timeout = complete_timeout
        self.enabled = enabled  # optional callable(config_path) -> bool; None = always on
        body = re.escape(prefix) + r'-model-%s-([0-9a-f]{32})\.json'
        self.input_re = re.compile(body % 'input')
        self.response_re = re.compile(body % 'response')
        self.stale_re = re.compile(re.escape(prefix) + r'-model-(?:input|response)-[0-9a-f]{32}\.json(?:\..{1,64})?')
        self.id_in_name = re.compile(re.escape(prefix) + r'-model-(?:input|response)-([0-9a-f]{32})\.json')

    def input_name(self, run_id):
        return '%s-model-input-%s.json' % (self.prefix, run_id)


def resolve_exchange_dir(config_path):
    cfg = json.loads(Path(config_path).read_text())
    raw = Path(str(cfg['episodic_memory']['path'])).expanduser()
    raw = raw if raw.is_absolute() else Path(config_path).resolve().parent / raw
    return raw.parent


MEMORY_PROFILE = Profile('memory', DEFAULT_REMOTE_ROOT, 'memory_executor.py', resolve_exchange_dir,
                         'memory-bridge-state.json', 'memory-bridge.lock')
# Memory-profile aliases kept for existing callers.
INPUT_RE, RESPONSE_RE = MEMORY_PROFILE.input_re, MEMORY_PROFILE.response_re
STALE_RE, ID_IN_NAME = MEMORY_PROFILE.stale_re, MEMORY_PROFILE.id_in_name


class DropboxError(Exception):
    """Reason only (class/HTTP code/Dropbox error tag); never file contents or secrets."""
    def __init__(self, reason, fatal=False):
        super().__init__(reason)
        self.reason = reason
        self.fatal = fatal


class DropboxClient:
    """Interface. list_folder -> [{'name','size','modified'(epoch s)}]; missing folder -> [].
    upload(overwrite=False) -> True if written, False if the path already exists."""
    def list_folder(self, path): raise NotImplementedError
    def upload(self, path, data, overwrite=False): raise NotImplementedError
    def download(self, path): raise NotImplementedError
    def delete(self, path): raise NotImplementedError  # missing path is not an error


def credential_dir(env=None, home=None):
    env = os.environ if env is None else env
    home = Path(home or os.path.expanduser('~'))
    candidates = [env.get('MEMORY_BRIDGE_DROPBOX_CONFIG_DIR'), env.get('YUJIN_DROPBOX_CONFIG_DIR'),
                  home / '.config' / 'yujin-image', env.get('SHARED_DROPBOX_CONFIG_DIR'),
                  home / '.config' / 'shared-dropbox']
    for item in candidates:
        if item and all((Path(item) / name).is_file() for name in CRED_FILES):
            return Path(item)
    raise DropboxError('dropbox_credentials_missing', fatal=True)


class RealDropbox(DropboxClient):
    """Same refresh-token flow as dropbox_upload.py; full-Dropbox paths (no app folder root)."""
    def __init__(self, config_dir=None, timeout=60):
        self.config_dir = Path(config_dir) if config_dir else credential_dir()
        self.timeout = timeout
        self._token = None

    def _read(self, name):
        return (self.config_dir / name).read_text().strip()

    def _access(self):
        if self._token:
            return self._token
        try:
            basic = base64.b64encode(
                f"{self._read('dropbox-app-key')}:{self._read('dropbox-app-secret')}".encode()).decode()
            body = urllib.parse.urlencode({'grant_type': 'refresh_token',
                                           'refresh_token': self._read('dropbox-refresh-token')}).encode()
            request = urllib.request.Request('https://api.dropbox.com/oauth2/token', data=body,
                                             headers={'Authorization': 'Basic ' + basic})
            with urllib.request.urlopen(request, timeout=30) as response:
                self._token = json.load(response)['access_token']
        except OSError as exc:
            raise DropboxError('dropbox_auth_' + self._why(exc), fatal=True)
        except (ValueError, KeyError):
            raise DropboxError('dropbox_auth_bad_response', fatal=True)
        return self._token

    @staticmethod
    def _why(exc):
        if isinstance(exc, urllib.error.HTTPError):
            try:
                tag = json.loads(exc.read(4096).decode('utf-8', 'replace')).get('error_summary', '')
            except (ValueError, OSError, AttributeError):
                tag = ''
            tag = re.sub(r'[^A-Za-z0-9_/.]', '', tag)[:60]
            return 'http_%s%s' % (exc.code, ('_' + tag) if tag else '')
        return type(exc).__name__

    def _request(self, url, data, headers):
        headers = {'Authorization': 'Bearer ' + self._access(), **headers}
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method='POST'),
                                        timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise DropboxError('dropbox_' + self._why(exc), fatal=exc.code in (401, 429) or exc.code >= 500)
        except OSError as exc:
            raise DropboxError('dropbox_network_' + type(exc).__name__, fatal=True)

    def _rpc(self, endpoint, body):
        raw = self._request('https://api.dropboxapi.com/2/' + endpoint, json.dumps(body).encode(),
                            {'Content-Type': 'application/json'})
        return json.loads(raw.decode('utf-8')) if raw else {}

    def list_folder(self, path):
        entries = []
        try:
            result = self._rpc('files/list_folder', {'path': path, 'limit': 500})
            while True:
                for item in result.get('entries', []):
                    if item.get('.tag') == 'file':
                        stamp = item.get('server_modified', '')
                        try:
                            modified = datetime.datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ').replace(
                                tzinfo=datetime.timezone.utc).timestamp()
                        except ValueError:
                            modified = time.time()
                        entries.append({'name': item['name'], 'size': int(item.get('size', 0)), 'modified': modified})
                if not result.get('has_more'):
                    return entries
                result = self._rpc('files/list_folder/continue', {'cursor': result['cursor']})
        except DropboxError as exc:
            if 'path/not_found' in exc.reason:
                return []
            raise

    def upload(self, path, data, overwrite=False):
        arg = json.dumps({'path': path, 'mode': 'overwrite' if overwrite else 'add', 'autorename': False,
                          'mute': True})
        try:
            self._request('https://content.dropboxapi.com/2/files/upload', data,
                          {'Dropbox-API-Arg': arg, 'Content-Type': 'application/octet-stream'})
            return True
        except DropboxError as exc:
            if 'conflict' in exc.reason:
                return False
            raise

    def download(self, path):
        return self._request('https://content.dropboxapi.com/2/files/download', None,
                             {'Dropbox-API-Arg': json.dumps({'path': path})})

    def delete(self, path):
        try:
            self._rpc('files/delete_v2', {'path': path})
        except DropboxError as exc:
            if 'not_found' not in exc.reason:
                raise


# ---- local helpers -------------------------------------------------------------------------

def load_state(path):
    try:
        data = json.loads(Path(path).read_text())
        if isinstance(data, dict):
            return {'uploaded': dict(data.get('uploaded') or {}), 'rejected': dict(data.get('rejected') or {})}
    except (OSError, ValueError, TypeError):
        pass
    return {'uploaded': {}, 'rejected': {}}


def atomic_private_write(path, data):
    """tmp + fsync + os.replace, mode 0600; the tmp file never survives a failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.memory-bridge-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def save_state(path, state):
    atomic_private_write(path, (json.dumps(state, sort_keys=True) + '\n').encode())


def local_ids(folder, pattern):
    found = {}
    try:
        for entry in os.scandir(folder):
            match = pattern.fullmatch(entry.name)
            if match and entry.is_file(follow_symlinks=False):
                found[match.group(1)] = Path(entry.path)
    except FileNotFoundError:
        pass
    return found


def run_complete(run_id, response_path, config, timeout=180, profile=None):
    """Optional: ask the profile's executor to finish a run. Only ok/reason survive into the summary."""
    profile = profile or MEMORY_PROFILE
    try:
        proc = subprocess.run([sys.executable, profile.executor, '--config', str(config), 'complete',
                               '--run-id', run_id, '--response', '@' + str(response_path)],
                              cwd=str(HERE), capture_output=True, text=True, timeout=timeout)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        reason = str(out.get('reason', ''))[:80] if isinstance(out, dict) else ''
        return {'run_id': run_id, 'ok': bool(isinstance(out, dict) and out.get('ok')), 'reason': reason}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return {'run_id': run_id, 'ok': False, 'reason': 'complete_failed'}


# ---- the pass ------------------------------------------------------------------------------

def run_pass(client, exchange_dir, state_file, remote_root=None, now=None, complete=None,
             max_response_bytes=MAX_RESPONSE_BYTES, stale_seconds=STALE_SECONDS, profile=None):
    """complete: optional callable(run_id, response_path) -> dict. Returns the summary dict."""
    profile = profile or MEMORY_PROFILE
    remote_root = remote_root or profile.remote_root
    input_re, response_re = profile.input_re, profile.response_re
    now = time.time() if now is None else now
    folder = Path(exchange_dir)
    remote_in, remote_out = remote_root.rstrip('/') + '/in', remote_root.rstrip('/') + '/out'
    summary = {'ok': True, 'uploaded': [], 'downloaded': [], 'completed': [], 'cleaned': [],
               'rejected': [], 'stale_deleted': 0, 'errors': []}
    state = load_state(state_file)
    uploaded, rejected = state['uploaded'], state['rejected']
    dirty = False

    def fail(reason):
        summary['ok'] = False
        if reason not in summary['errors']:
            summary['errors'].append(reason)

    class Abort(Exception):
        pass

    def guard(exc):
        fail(exc.reason)
        if exc.fatal:
            raise Abort()

    inputs = local_ids(folder, input_re)
    summary['pending'] = len(inputs)
    try:
        # 1. upload
        for run_id, path in sorted(inputs.items()):
            if run_id in uploaded:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                fail('local_input_unreadable:' + run_id)
                continue
            if len(data) > MAX_INPUT_BYTES:
                fail('input_too_large:' + run_id)
                continue
            try:
                client.upload('%s/%s' % (remote_in, path.name), data, overwrite=False)
            except DropboxError as exc:
                guard(exc)
                continue
            uploaded[run_id] = {'bytes': len(data), 'uploaded_at': int(now)}
            summary['uploaded'].append({'run_id': run_id, 'bytes': len(data)})
            dirty = True
            try:
                save_state(state_file, state)
            except OSError:
                fail('state_write_failed')

        # 2. download responses for pending runs
        out_files = []
        try:
            out_files = client.list_folder(remote_out)
        except DropboxError as exc:
            guard(exc)
        for entry in sorted(out_files, key=lambda item: item['name']):
            match = response_re.fullmatch(entry['name'])
            if not match:
                continue
            run_id = match.group(1)
            if run_id not in inputs:
                continue
            remote_path = '%s/%s' % (remote_out, entry['name'])
            local_response = folder / entry['name']
            if local_response.exists():
                try:
                    client.delete(remote_path)
                    entry['deleted'] = True
                except DropboxError as exc:
                    guard(exc)
                continue
            signature = [entry.get('size'), entry.get('modified')]
            if rejected.get(run_id, {}).get('sig') == signature:
                continue
            reason, data = None, b''
            if entry.get('size', 0) > max_response_bytes:
                reason = 'response_too_large'
            else:
                try:
                    data = client.download(remote_path)
                except DropboxError as exc:
                    guard(exc)
                    continue
                if len(data) > max_response_bytes:
                    reason = 'response_too_large'
                else:
                    try:
                        json.loads(data.decode('utf-8'))
                    except (ValueError, UnicodeDecodeError):
                        reason = 'response_invalid_json'
            if reason:
                rejected[run_id] = {'sig': signature, 'reason': reason}
                dirty = True
                summary['rejected'].append({'run_id': run_id, 'reason': reason, 'bytes': entry.get('size', len(data))})
                fail('%s:%s' % (reason, run_id))
                continue
            try:
                atomic_private_write(local_response, data)
            except OSError:
                fail('local_write_failed:' + run_id)
                continue
            summary['downloaded'].append({'run_id': run_id, 'bytes': len(data)})
            rejected.pop(run_id, None)
            dirty = True
            try:
                client.delete(remote_path)
                entry['deleted'] = True
            except DropboxError as exc:
                guard(exc)
            if complete:
                summary['completed'].append(complete(run_id, local_response))

        # 3a. cleanup: local input gone -> drop remote copy + state
        gone = [run_id for run_id in list(uploaded) if run_id not in inputs]
        for run_id in sorted(gone):
            try:
                client.delete('%s/%s' % (remote_in, profile.input_name(run_id)))
                for entry in out_files:
                    if response_re.fullmatch(entry['name']) and response_re.fullmatch(entry['name']).group(1) == run_id \
                            and not entry.get('deleted'):
                        client.delete('%s/%s' % (remote_out, entry['name']))
                        entry['deleted'] = True
            except DropboxError as exc:
                guard(exc)
                continue
            uploaded.pop(run_id, None)
            rejected.pop(run_id, None)
            summary['cleaned'].append(run_id)
            dirty = True
        for run_id in list(rejected):
            if run_id not in inputs:
                rejected.pop(run_id)
                dirty = True

        # 3b. stale remote files (>24h, id has no local input)
        for subfolder, listing in ((remote_in, None), (remote_out, out_files)):
            try:
                listing = client.list_folder(subfolder) if listing is None or subfolder == remote_in else listing
            except DropboxError as exc:
                guard(exc)
                continue
            for entry in listing:
                if entry.get('deleted') or not profile.stale_re.fullmatch(entry['name']):
                    continue
                run_id = profile.id_in_name.match(entry['name']).group(1)
                if run_id in inputs or now - entry.get('modified', now) < stale_seconds:
                    continue
                try:
                    client.delete('%s/%s' % (subfolder, entry['name']))
                    summary['stale_deleted'] += 1
                    summary.setdefault('stale', []).append({'run_id': run_id, 'where': subfolder.rsplit('/', 1)[1],
                                                            'bytes': entry.get('size', 0)})
                except DropboxError as exc:
                    guard(exc)
    except Abort:
        pass
    finally:
        if dirty:
            try:
                save_state(state_file, state)
            except OSError:
                fail('state_write_failed')
    return summary


def main(argv=None, client=None, profile=None):
    profile = profile or MEMORY_PROFILE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(HERE / 'automation.json'))
    parser.add_argument('--exchange-dir', help='default: the profile\'s folder resolved from --config')
    parser.add_argument('--state-file', help='default: <exchange-dir>/%s' % profile.state_name)
    parser.add_argument('--remote-root', default=profile.remote_root)
    parser.add_argument('--dropbox-config-dir')
    parser.add_argument('--complete', action='store_true',
                        help='after downloading a response, run %s complete (default off)' % profile.executor)
    args = parser.parse_args(argv)
    started = time.time()
    summary = {'ok': False, 'errors': []}
    code = 1
    try:
        if profile.enabled and not args.exchange_dir and not profile.enabled(args.config):
            print(json.dumps({'ok': True, 'skipped': 'off', 'elapsed_ms': 0}, sort_keys=True))
            return 0
        folder = Path(args.exchange_dir) if args.exchange_dir else profile.resolve_dir(args.config)
        state_file = Path(args.state_file) if args.state_file else folder / profile.state_name
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = os.open(str(folder / profile.lock_name), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                summary = {'ok': True, 'skipped': 'locked'}
                code = 0
            else:
                complete = None
                if args.complete:
                    complete = lambda run_id, path: run_complete(run_id, path, args.config, profile=profile, timeout=profile.complete_timeout)
                try:
                    client = client or RealDropbox(args.dropbox_config_dir)
                    summary = run_pass(client, folder, state_file, args.remote_root, complete=complete, profile=profile)
                except DropboxError as exc:
                    summary = {'ok': False, 'errors': [exc.reason]}
                code = 0 if summary['ok'] else 1
        finally:
            os.close(lock)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        summary = {'ok': False, 'errors': ['bridge_setup_failed:' + type(exc).__name__]}
    summary['elapsed_ms'] = int((time.time() - started) * 1000)
    print(json.dumps(summary, sort_keys=True, ensure_ascii=True))
    return code


if __name__ == '__main__':
    sys.exit(main())
