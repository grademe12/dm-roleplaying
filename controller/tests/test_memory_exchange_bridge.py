"""Dropbox exchange bridge: fake in-memory Dropbox, temp dirs, no network."""
import fcntl
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import stat
import tempfile
import unittest

import memory_exchange_bridge as bridge

ROOT = '/yujin-project/memory-exchange'
A = 'a' * 32
B = 'b' * 32
C = 'c' * 32
NOW = 2_000_000_000.0


class FakeDropbox(bridge.DropboxClient):
    def __init__(self):
        self.files = {}  # path -> (bytes, modified)
        self.calls = []
        self.fail_with = None

    def put(self, path, data, modified=NOW):
        self.files[path] = (data, modified)

    def _maybe_fail(self):
        if self.fail_with:
            raise self.fail_with

    def list_folder(self, path):
        self._maybe_fail()
        prefix = path.rstrip('/') + '/'
        return [{'name': p[len(prefix):], 'size': len(v[0]), 'modified': v[1]}
                for p, v in sorted(self.files.items()) if p.startswith(prefix) and '/' not in p[len(prefix):]]

    def upload(self, path, data, overwrite=False):
        self._maybe_fail()
        self.calls.append(('upload', path))
        if path in self.files and not overwrite:
            return False
        self.files[path] = (data, NOW)
        return True

    def download(self, path):
        self._maybe_fail()
        self.calls.append(('download', path))
        return self.files[path][0]

    def delete(self, path):
        self._maybe_fail()
        self.calls.append(('delete', path))
        self.files.pop(path, None)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / 'automation-state'
        self.dir.mkdir()
        (self.dir / 'episodic-memory.sqlite3').write_bytes(b'sqlite-do-not-upload')
        (self.dir / ('memory-http-response-%s.json' % A)).write_text('{}')
        self.state = self.dir / 'memory-bridge-state.json'
        self.dbx = FakeDropbox()

    def local_input(self, run_id, body='{"secret":"dm text"}'):
        path = self.dir / ('memory-model-input-%s.json' % run_id)
        path.write_text(body)
        return path

    def run_pass(self, **kw):
        kw.setdefault('now', NOW)
        return bridge.run_pass(self.dbx, self.dir, self.state, ROOT, **kw)

    def remote_in(self, run_id):
        return '%s/in/memory-model-input-%s.json' % (ROOT, run_id)

    def remote_out(self, run_id, suffix=''):
        return '%s/out/memory-model-response-%s.json%s' % (ROOT, run_id, suffix)

    def local_response(self, run_id):
        return self.dir / ('memory-model-response-%s.json' % run_id)

    def test_upload_once_and_only_inputs(self):
        self.local_input(A)
        (self.dir / 'memory-model-input-short.json').write_text('x')
        (self.dir / ('memory-model-input-%s.json.tmp' % B)).write_text('x')
        first = self.run_pass()
        self.assertTrue(first['ok'])
        self.assertEqual(first['uploaded'], [{'run_id': A, 'bytes': len('{"secret":"dm text"}')}])
        self.assertEqual(list(self.dbx.files), [self.remote_in(A)])
        second = self.run_pass()
        self.assertEqual(second['uploaded'], [])
        self.assertEqual([c for c in self.dbx.calls if c[0] == 'upload'], [('upload', self.remote_in(A))])
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertIn(A, json.loads(self.state.read_text())['uploaded'])

    def test_upload_idempotent_when_remote_exists_and_state_lost(self):
        self.local_input(A)
        self.dbx.put(self.remote_in(A), b'old')
        self.assertTrue(self.run_pass()['ok'])
        self.assertEqual(self.dbx.files[self.remote_in(A)][0], b'old')  # no overwrite
        self.assertIn(A, json.loads(self.state.read_text())['uploaded'])

    def test_download_writes_0600_atomically_then_deletes_remote(self):
        self.local_input(A)
        self.run_pass()
        payload = json.dumps({'schema': 'x', 'run_id': A}).encode()
        self.dbx.put(self.remote_out(A), payload)
        summary = self.run_pass()
        self.assertEqual(summary['downloaded'], [{'run_id': A, 'bytes': len(payload)}])
        target = self.local_response(A)
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertNotIn(self.remote_out(A), self.dbx.files)
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith('.memory-bridge-')], [])

    def test_failed_replace_leaves_no_partial_file(self):
        self.local_input(A)
        self.dbx.put(self.remote_out(A), b'{}')
        real = os.replace
        def boom(src, dst, *a, **k):
            if Path(dst).name.startswith('memory-model-response-'):
                raise OSError('disk')
            return real(src, dst, *a, **k)
        bridge.os.replace = boom
        try:
            summary = self.run_pass()
        finally:
            bridge.os.replace = real
        self.assertFalse(summary['ok'])
        self.assertFalse(self.local_response(A).exists())
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith('.memory-bridge-')], [])
        self.assertIn(self.remote_out(A), self.dbx.files)  # remote kept for retry

    def test_ignores_bad_names_and_part_files(self):
        self.local_input(A)
        for name in ('memory-model-response-%s.json.part' % A, 'memory-model-response-%s.json ' % A,
                     'memory-model-response-%s.json\n' % A, 'memory-model-response-%s.JSON' % A,
                     'memory-model-response-%s.json' % ('A' * 32), 'memory-model-response-%s.json' % ('a' * 31),
                     'x-memory-model-response-%s.json' % A, 'memory-model-input-%s.json' % A):
            self.dbx.put('%s/out/%s' % (ROOT, name), b'{}')
        summary = self.run_pass()
        self.assertEqual(summary['downloaded'], [])
        self.assertFalse(self.local_response(A).exists())
        self.assertEqual([c for c in self.dbx.calls if c[0] == 'download'], [])

    def test_ignores_response_for_unknown_run(self):
        self.local_input(A)
        self.dbx.put(self.remote_out(B), b'{}')
        summary = self.run_pass()
        self.assertEqual(summary['downloaded'], [])
        self.assertFalse(self.local_response(B).exists())
        self.assertIn(self.remote_out(B), self.dbx.files)  # fresh -> not stale yet

    def test_existing_local_response_not_overwritten(self):
        self.local_input(A)
        self.local_response(A).write_text('{"mine":1}')
        self.dbx.put(self.remote_out(A), b'{"theirs":1}')
        summary = self.run_pass()
        self.assertEqual(summary['downloaded'], [])
        self.assertEqual(self.local_response(A).read_text(), '{"mine":1}')
        self.assertNotIn(self.remote_out(A), self.dbx.files)

    def test_invalid_json_rejected_once_until_changed(self):
        self.local_input(A)
        self.dbx.put(self.remote_out(A), b'{not json', modified=NOW)
        first = self.run_pass()
        self.assertFalse(first['ok'])
        self.assertEqual(first['rejected'], [{'run_id': A, 'reason': 'response_invalid_json', 'bytes': 9}])
        self.assertFalse(self.local_response(A).exists())
        self.run_pass()
        self.assertEqual(len([c for c in self.dbx.calls if c[0] == 'download']), 1)  # not re-downloaded
        self.dbx.put(self.remote_out(A), b'{"ok":true}', modified=NOW + 5)  # corrected upload
        fixed = self.run_pass()
        self.assertEqual(fixed['downloaded'][0]['run_id'], A)
        self.assertEqual(self.local_response(A).read_bytes(), b'{"ok":true}')

    def test_oversize_rejected_without_download(self):
        self.local_input(A)
        self.dbx.put(self.remote_out(A), b'{"x":"' + b'a' * 100 + b'"}')
        summary = self.run_pass(max_response_bytes=50)
        self.assertEqual(summary['rejected'][0]['reason'], 'response_too_large')
        self.assertFalse(self.local_response(A).exists())
        self.assertEqual([c for c in self.dbx.calls if c[0] == 'download'], [])

    def test_default_limit_is_4mb(self):
        self.assertEqual(bridge.MAX_RESPONSE_BYTES, 4 * 1024 * 1024)

    def test_cleanup_after_local_deletion(self):
        path = self.local_input(A)
        self.local_input(B)
        self.run_pass()
        self.dbx.put(self.remote_out(A), b'{}', modified=NOW)  # late response for a finished run
        path.unlink()
        summary = self.run_pass()
        self.assertEqual(summary['cleaned'], [A])
        self.assertNotIn(self.remote_in(A), self.dbx.files)
        self.assertNotIn(self.remote_out(A), self.dbx.files)
        self.assertIn(self.remote_in(B), self.dbx.files)
        self.assertEqual(sorted(json.loads(self.state.read_text())['uploaded']), [B])

    def test_cleanup_failure_keeps_state_and_reports(self):
        path = self.local_input(A)
        self.run_pass()
        path.unlink()
        self.dbx.fail_with = bridge.DropboxError('dropbox_http_409_x')
        summary = self.run_pass()
        self.assertFalse(summary['ok'])
        self.assertIn(A, json.loads(self.state.read_text())['uploaded'])

    def test_stale_cleanup_by_age_and_local_input(self):
        self.local_input(C)
        old = NOW - 25 * 3600
        self.dbx.put(self.remote_in(A), b'x', modified=old)
        self.dbx.put(self.remote_out(A), b'{}', modified=old)
        self.dbx.put(self.remote_out(B), b'{}', modified=NOW - 3600)  # fresh
        self.dbx.put(self.remote_in(C), b'x', modified=old)  # has local input
        self.dbx.put(self.remote_out(B, '.part'), b'x', modified=old)  # abandoned partial upload
        self.dbx.put('%s/in/unrelated.txt' % ROOT, b'x', modified=old)
        state = {'uploaded': {C: {'bytes': 1, 'uploaded_at': 1}}, 'rejected': {}}
        self.state.write_text(json.dumps(state))
        summary = self.run_pass()
        self.assertEqual(summary['stale_deleted'], 3)
        self.assertEqual(sorted(self.dbx.files), sorted([self.remote_out(B), self.remote_in(C),
                                                         '%s/in/unrelated.txt' % ROOT]))
        self.assertTrue(all(set(item) == {'run_id', 'where', 'bytes'} for item in summary['stale']))

    def test_dropbox_error_fails_soft_without_local_files(self):
        self.local_input(A)
        self.dbx.fail_with = bridge.DropboxError('dropbox_network_URLError', fatal=True)
        summary = self.run_pass()
        self.assertFalse(summary['ok'])
        self.assertEqual(summary['errors'], ['dropbox_network_URLError'])
        self.assertFalse(self.local_response(A).exists())
        self.assertEqual(bridge.load_state(self.state)['uploaded'], {})

    def test_summary_never_contains_contents(self):
        self.local_input(A)
        self.run_pass()
        self.dbx.put(self.remote_out(A), b'{"memories":"PRIVATE-RESPONSE"}')
        text = json.dumps(self.run_pass())
        self.assertNotIn('PRIVATE', text)
        self.assertNotIn('dm text', text)

    def test_complete_hook_runs_only_when_given(self):
        self.local_input(A)
        self.dbx.put(self.remote_out(A), b'{}')
        seen = []
        summary = self.run_pass(complete=lambda run_id, path: seen.append((run_id, path.name)) or
                                {'run_id': run_id, 'ok': True, 'reason': ''})
        self.assertEqual(seen, [(A, 'memory-model-response-%s.json' % A)])
        self.assertEqual(summary['completed'][0]['ok'], True)

    def run_main(self, *extra):
        out = io.StringIO()
        with redirect_stdout(out):
            code = bridge.main(['--exchange-dir', str(self.dir), '--state-file', str(self.state), *extra],
                               client=self.dbx)
        return code, json.loads(out.getvalue())

    def test_main_prints_one_json_line_and_exit_codes(self):
        self.local_input(A)
        code, summary = self.run_main()
        self.assertEqual(code, 0)
        self.assertTrue(summary['ok'])
        self.assertEqual(summary['uploaded'][0]['run_id'], A)
        self.dbx.fail_with = bridge.DropboxError('dropbox_http_500', fatal=True)
        code, summary = self.run_main()
        self.assertEqual(code, 1)
        self.assertFalse(summary['ok'])

    def test_lock_prevents_overlap(self):
        self.local_input(A)
        fd = os.open(str(self.dir / 'memory-bridge.lock'), os.O_WRONLY | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, summary = self.run_main()
        self.assertEqual(summary['skipped'], 'locked')
        self.assertEqual(code, 0)
        self.assertEqual(self.dbx.calls, [])
        fcntl.flock(fd, fcntl.LOCK_UN)
        code, summary = self.run_main()
        self.assertEqual(summary['uploaded'][0]['run_id'], A)

    def test_credential_dir_requires_all_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg = home / '.config' / 'shared-dropbox'
            cfg.mkdir(parents=True)
            with self.assertRaises(bridge.DropboxError):
                bridge.credential_dir(env={}, home=home)
            for name in bridge.CRED_FILES:
                (cfg / name).write_text('x')
            self.assertEqual(bridge.credential_dir(env={}, home=home), cfg)


if __name__ == '__main__':
    unittest.main()
