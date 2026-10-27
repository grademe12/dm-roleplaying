"""Optional generator: offline protocol, source/commit binding and failure recovery."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import urllib.error

from common import ControlError, encode
import decision_session
import gate
import prepared_inputs
import reply_generator as gen
import test_decision_session as lifecycle
import test_controller as baseline


def response(parts=None, **kwargs):
    value = {'parts': ['안녕'] if parts is None else parts, 'note': 'wording'}
    value.update(kwargs)
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': encode(value)}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 10,
                      'prompt_tokens_details': {'cached_tokens': 50}}, 'cost': {'usd': 0.001}}


class ReplyGeneratorTests(unittest.TestCase):
    tearDown = lifecycle.DecisionSessionTests.tearDown
    snapshot = lifecycle.DecisionSessionTests.snapshot
    decision = lifecycle.DecisionSessionTests.decision
    review = lifecycle.DecisionSessionTests.review
    ingest = lifecycle.DecisionSessionTests.ingest
    activity = lifecycle.DecisionSessionTests.activity
    free = lifecycle.DecisionSessionTests.free
    sleep = lifecycle.DecisionSessionTests.sleep
    recent = lifecycle.DecisionSessionTests.recent
    draft = lifecycle.DecisionSessionTests.draft

    def setUp(self):
        lifecycle.DecisionSessionTests.setUp(self)
        self.rules = Path(self.tmp.name) / 'rules.md'
        self.rules.write_text('FIXED_RULES_MARKER\n', encoding='utf-8')
        self.key = Path(self.tmp.name) / 'key'
        self.key.write_text('synthetic-secret', encoding='utf-8'); self.key.chmod(0o600)
        self.cfg['reply_generator'] = {'enabled': True, 'endpoint': 'https://llm.example.test/v1/chat/completions',
                                       'key_file': str(self.key), 'rules_file': str(self.rules)}
        self.ingest(); self.recent()
        self.prepared = self.runner.prepare(mode='bundled', decision_format='compact-v1')
        self.request = {'schema': 'reply-gen-v1', 'turn_id': self.prepared['turn_id'],
                        'channel': 'instagram', 'situation': 'resting at home',
                        'memories': ['a selected source'], 'target_message_ids': ['m1']}
        self.calls = []

    def transport(self, body, cfg, timeout):
        # Gate and DB writes remain available during the HTTP call.
        with gate.gate_lock(self.state_dir), self.store.connect(True) as db:
            db.execute('SELECT revision FROM sessions')
        self.calls.append((body, timeout))
        return response()

    def generate(self, request=None, **kwargs):
        return gen.generate(self.runner, self.prepared['turn_id'], request or self.request,
                            transport=kwargs.pop('transport', self.transport), **kwargs)

    def commit_generated(self, generated, *, compact=False, parts=None):
        draft = self.draft(body='안녕')
        dm = draft['dm_decisions'][0]
        dm['generator_id'] = generated['generator_id']
        if parts is None:
            dm.pop('parts')
        else:
            dm['parts'] = parts
        if compact:
            draft.pop('decision_id'); draft.pop('expected_revision')
            review = dm['conversation_review']; context = review['context']
            dm['channel'] = 'instagram'
            dm['conversation_review'] = {**{k: context[k] for k in ('previous_self', 'user_uptake', 'topic')},
                                         'memory': review['memory'], 'checks': review['checks']}
        with patch.object(self.runner, 'finish', return_value={'next': 'done'}):
            return self.runner.commit(self.prepared['turn_id'], draft,
                decision_format='compact-v1' if compact else 'full',
                decision_generation=self.prepared['decision_generation'] if compact else None)

    def test_fixed_prefix_order_stays_identical_and_dynamic_input_follows_it(self):
        first = self.generate()
        request = {**self.request, 'situation': 'different situation', 'memories': ['different memory']}
        second = self.generate(request)
        a, b = [item[0] for item in self.calls]
        self.assertEqual(a['messages'][0], b['messages'][0])
        self.assertEqual(a['prompt_cache_key'], b['prompt_cache_key'])
        fixed = a['messages'][0]['content']
        profile = (gen.ROOT / 'design/character/profile.md').read_text(encoding='utf-8')
        shared = self.prepared['documents']['design/conversation-rules.md']
        self.assertEqual(fixed, profile + '\n\n' + gen.RULES + '\n\n' + shared
                         + '\n\n' + self.rules.read_text(encoding='utf-8'))
        self.assertNotIn('different situation', fixed)
        self.assertNotIn(self.prepared['turn_id'], fixed)
        self.assertEqual(a['prompt_cache_retention'], '24h')
        self.assertNotEqual(first['generator_id'], second['generator_id'])

    def test_shared_rule_change_on_new_prepare_changes_generator_cache_and_request(self):
        first = self.generate()
        original = Path.read_text
        def modified(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            return value + '\nUPDATED_SHARED_RULE' if path.name == 'conversation-rules.md' else value
        with patch.object(Path, 'read_text', modified):
            self.prepared = self.runner.prepare(mode='bundled', decision_format='compact-v1')
            self.request['turn_id'] = self.prepared['turn_id']
            second = self.generate()
        a, b = [call[0] for call in self.calls]
        self.assertNotEqual(a['prompt_cache_key'], b['prompt_cache_key'])
        self.assertNotEqual(first['generator_id'], second['generator_id'])
        self.assertIn('UPDATED_SHARED_RULE', b['messages'][0]['content'])
        self.assertNotIn('UPDATED_SHARED_RULE', a['messages'][0]['content'])

    def test_raw_history_comes_from_prepared_source_not_copied_request(self):
        self.generate({**self.request, 'history': [{'text': 'invented replacement'}]})
        dynamic = json.loads(self.calls[0][0]['messages'][1]['content'])
        self.assertEqual(dynamic['history'][0]['id'], 'm1')
        self.assertEqual(dynamic['history'][0]['text'], 'hello')
        self.assertEqual(dynamic['memories'], self.request['memories'])

    def test_same_request_reuses_private_result_without_second_call(self):
        first = self.generate(); second = self.generate()
        self.assertTrue(second['reused']); self.assertEqual(len(self.calls), 1)
        self.assertEqual(first['parts'], second['parts'])
        path = gen.record_path(self.runner, first['generator_id'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        logs = ''.join(p.read_text() for p in self.state_dir.glob('decision-trace-*.jsonl'))
        for private in ('synthetic-secret', 'a selected source', 'FIXED_RULES_MARKER', '안녕'):
            self.assertNotIn(private, logs)

    def test_stored_text_is_attached_and_generator_id_is_in_decision_record(self):
        generated = self.generate()
        self.assertTrue(self.commit_generated(generated)['committed'])
        with self.store.connect() as db:
            job = db.execute('SELECT text FROM dm_jobs').fetchone()
            fact = db.execute("SELECT details_json FROM facts WHERE id LIKE 'decision:%' ORDER BY recorded_ms DESC,rowid DESC LIMIT 1").fetchone()
        self.assertEqual(job['text'], '안녕')
        self.assertIn(generated['generator_id'], fact['details_json'])

    def test_compact_commit_attaches_text_before_review_expansion(self):
        generated = self.generate()
        self.assertTrue(self.commit_generated(generated, compact=True)['committed'])

    def test_muse_cannot_edit_generated_text_or_target(self):
        generated = self.generate()
        with self.assertRaisesRegex(ControlError, 'generator_parts_changed'):
            self.commit_generated(generated, parts=['edited text'])
        draft = self.draft(body='안녕'); draft['dm_decisions'][0].update(generator_id=generated['generator_id'], message_ids=[])
        with self.assertRaisesRegex(ControlError, 'generator_binding_mismatch'):
            self.runner.commit(self.prepared['turn_id'], draft)
        self.assertEqual(self.snapshot()['jobs'], [])

    def test_reprepare_invalidates_commit_even_if_revision_is_same(self):
        generated = self.generate()
        self.runner.prepare()
        with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
            self.commit_generated(generated)

    def test_paid_result_survives_other_change_during_http_but_is_not_applied(self):
        def new_input(*args):
            self.ingest('o2', direction='outgoing'); self.recent(('m1',), outgoing=('o2',))
            return response()
        with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
            self.generate(transport=new_input)
        records = list(gen.folder(self.runner).glob('gen-*.json'))
        self.assertEqual(len(records), 1)
        self.assertTrue(gate.load_json(records[0], {})['result']['ok'])
        self.assertEqual(self.snapshot()['jobs'], [])

    def test_expired_claim_cannot_apply_paid_result(self):
        wake = gate.load_json(self.state_dir / 'wake.json', {})
        wake['claim_deadline_ms'] = self.clock + 120000
        gate.write_json(self.state_dir / 'wake.json', wake)
        def expire(*args):
            self.clock += 120001
            return response()
        with self.assertRaisesRegex(ControlError, 'stale_claim'):
            self.generate(transport=expire)
        self.assertTrue(gate.load_json(next(gen.folder(self.runner).glob('gen-*.json')), {})['result']['ok'])

    def test_missing_rules_and_disabled_generator_never_call_network(self):
        self.rules.unlink()
        result = self.generate()
        self.assertEqual(result['reason'], 'generator_rules_unavailable'); self.assertEqual(result['next'], 'defer')
        self.cfg['reply_generator']['enabled'] = False
        self.assertEqual(self.generate()['next'], 'muse')
        self.assertEqual(self.calls, [])

    def test_current_budget_is_checked_instead_of_prepare_snapshot(self):
        wake = gate.load_json(self.state_dir / 'wake.json', {})
        wake['claim_deadline_ms'] = self.clock + 80000
        gate.write_json(self.state_dir / 'wake.json', wake)
        result = self.generate()
        self.assertEqual(result['reason'], 'generator_budget'); self.assertEqual(self.calls, [])

    def test_timeout_recalls_once_then_defers(self):
        with patch.object(gen, 'call_http', side_effect=ControlError('timeout')) as call:
            first = gen.generate(self.runner, self.prepared['turn_id'], self.request)
            second = gen.generate(self.runner, self.prepared['turn_id'], self.request)
            third = gen.generate(self.runner, self.prepared['turn_id'], self.request)
        self.assertEqual(first['reason'], 'timeout'); self.assertEqual(first['next'], 'defer')
        self.assertNotIn('reused', second); self.assertEqual(second['reason'], 'timeout')
        self.assertTrue(third['reused']); self.assertEqual(third['next'], 'defer')
        self.assertEqual(call.call_count, 2)
        record = gate.load_json(gen.record_path(self.runner, first['generator_id']), {})
        self.assertEqual(record['attempts'], 2)
        self.assertEqual([r['reason'] for r in record['earlier_attempts']], ['timeout'])

    def test_interrupted_request_is_recalled_once_in_same_prepare(self):
        with self.assertRaises(SystemExit):
            self.generate(transport=lambda *args: (_ for _ in ()).throw(SystemExit()))
        result = self.generate()
        self.assertTrue(result['ok']); self.assertNotIn('reused', result)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.generate()['reused']); self.assertEqual(len(self.calls), 1)

    def test_output_failures_are_not_recalled_without_feedback(self):
        raw = response(); raw['choices'][0]['finish_reason'] = 'length'
        first = self.generate(transport=lambda *args: raw)
        self.assertEqual(first['reason'], 'incomplete')
        self.assertTrue(self.generate()['reused']); self.assertEqual(self.calls, [])

    def test_budget_refusal_does_not_consume_regeneration(self):
        first = self.generate()
        request = {**self.request, 'regenerate_of': first['generator_id'], 'feedback': {'reason': 'missed question'}}
        wake = gate.load_json(self.state_dir / 'wake.json', {})
        wake['claim_deadline_ms'] = self.clock + 80000
        gate.write_json(self.state_dir / 'wake.json', wake)
        self.assertEqual(self.generate(request)['reason'], 'generator_budget')
        wake['claim_deadline_ms'] = self.clock + 600000
        gate.write_json(self.state_dir / 'wake.json', wake)
        self.assertTrue(self.generate(request)['ok']); self.assertEqual(len(self.calls), 2)

    def test_interrupted_regeneration_can_be_recalled(self):
        first = self.generate()
        request = {**self.request, 'regenerate_of': first['generator_id'], 'feedback': {'reason': 'missed question'}}
        with self.assertRaises(SystemExit):
            self.generate(request, transport=lambda *args: (_ for _ in ()).throw(SystemExit()))
        self.assertTrue(self.generate(request)['ok']); self.assertEqual(len(self.calls), 2)

    def test_enabled_generator_rejects_direct_reply_wording(self):
        with self.assertRaisesRegex(ControlError, 'generator_required'):
            with patch.object(self.runner, 'finish', return_value={'next': 'done'}):
                self.runner.commit(self.prepared['turn_id'], self.draft(body='직접 쓴 문장'))
        self.assertEqual(self.snapshot()['jobs'], [])
        self.cfg['reply_generator']['enabled'] = False
        with patch.object(self.runner, 'finish', return_value={'next': 'done'}):
            self.assertTrue(self.runner.commit(self.prepared['turn_id'], self.draft(body='직접 쓴 문장'))['committed'])

    def test_muse_failure_policy_is_not_configurable(self):
        self.cfg['reply_generator']['failure_policy'] = 'muse'
        with self.assertRaisesRegex(ControlError, 'invalid_reply_generator_config'):
            self.generate()
        self.cfg['reply_generator']['failure_policy'] = 'defer'
        self.assertTrue(self.generate()['ok'])

    def test_committed_generated_reply_replays_without_second_job_or_generation(self):
        generated = self.generate()
        self.assertTrue(self.commit_generated(generated, compact=True)['committed'])
        self.assertTrue(self.commit_generated(generated, compact=True)['committed'])
        self.assertEqual(len(self.snapshot()['jobs']), 1)
        self.assertEqual(len(self.calls), 1)

    def test_provider_rejection_does_not_start_a_fallback_generation(self):
        raw = response(); raw['choices'][0]['finish_reason'] = 'content_filter'
        result = self.generate(transport=lambda *args: raw)
        self.assertEqual(result['reason'], 'content_filter'); self.assertEqual(result['next'], 'defer')
        with self.assertRaisesRegex(ControlError, 'regeneration_not_allowed'):
            self.generate({**self.request, 'regenerate_of': result['generator_id'], 'feedback': {'reason': 'retry'}})

    def test_feedback_can_regenerate_once_and_preserves_original(self):
        first = self.generate()
        request = {**self.request, 'regenerate_of': first['generator_id'], 'feedback': {'reason': 'missed question'}}
        second = self.generate(request)
        self.assertTrue(second['ok']); self.assertEqual(len(self.calls), 2)
        with self.assertRaisesRegex(ControlError, 'regeneration_not_allowed'):
            self.generate({**request, 'regenerate_of': second['generator_id']})
        with self.assertRaisesRegex(ControlError, 'regeneration_limit'):
            self.generate({**request, 'feedback': {'reason': 'different feedback'}})

    def test_split_inputs_require_actually_submitted_context_receipts(self):
        self.prepared = self.runner.prepare(mode='compact')
        with self.assertRaisesRegex(ControlError, 'input_receipts_required'):
            self.generate()
        bundle = prepared_inputs.read_bundle(self.runner, self.prepared['turn_id'], self.prepared['input_id'])
        self.assertTrue(self.generate(input_receipts=bundle['input_receipts'])['ok'])

    def test_key_permissions_and_subprocess_timeout_are_enforced(self):
        cfg = gen.settings(self.cfg)
        self.key.chmod(0o644)
        with self.assertRaisesRegex(ControlError, 'generator_key_permissions'):
            gen.call_http({}, cfg, 1)
        self.key.chmod(0o600)
        with patch.object(gen.subprocess, 'run', side_effect=subprocess.TimeoutExpired('worker', 1)) as run:
            with self.assertRaisesRegex(ControlError, 'timeout'):
                gen.call_http({}, cfg, 1)
        self.assertNotIn('synthetic-secret', str(run.call_args.args))
        self.assertEqual(run.call_args.kwargs['timeout'], 1)

    def test_http_worker_serializes_json_and_classifies_http_errors(self):
        stream = io.BytesIO(encode(response()).encode('utf-8'))
        class CM:
            def __enter__(self): return stream
            def __exit__(self, *args): return False
        payload = {'endpoint': 'https://llm.example.test/v1/chat/completions', 'body': {'model': 'test'},
                   'key': 'synthetic-secret', 'timeout': 2}
        with patch.object(gen.urllib.request, 'build_opener') as opener:
            opener.return_value.open.return_value = CM()
            self.assertTrue(gen.http_worker(payload)['ok'])
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(json.loads(request.data), {'model': 'test'})
            self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-secret')
            opener.return_value.open.side_effect = urllib.error.HTTPError('test', 429, 'bad', {}, None)
            self.assertEqual(gen.http_worker(payload)['reason'], 'http_429')

    def test_json_codeblocks_and_invalid_outputs(self):
        valid = response()
        valid['choices'][0]['message']['content'] = '```json\n' + valid['choices'][0]['message']['content'] + '\n```'
        self.assertEqual(gen.validate_output(valid)['parts'], ['안녕'])
        for raw, reason in ((response([]), 'invalid_parts'), (response(['x'] * 6), 'invalid_parts'),
                            (response([' ']), 'empty'), (response([{'photo': True}]), 'invalid_parts')):
            with self.subTest(reason=reason), self.assertRaisesRegex(ControlError, reason):
                gen.validate_output(raw)
        for finish in ('length', 'tool_calls', 'content_filter'):
            raw = response(); raw['choices'][0]['finish_reason'] = finish
            with self.subTest(finish=finish), self.assertRaises(ControlError):
                gen.validate_output(raw)

    def test_cli_routes_to_current_session_without_external_execution(self):
        config = Path(self.tmp.name) / 'config.json'; config.write_text(encode(self.cfg))
        request = Path(self.tmp.name) / 'request.json'; request.write_text(encode(self.request))
        output = io.StringIO()
        with patch.object(decision_session, 'DecisionSession', return_value=self.runner), \
             patch.object(gen, 'call_http', return_value=response()), redirect_stdout(output):
            code = decision_session.main(['--config', str(config), '--wake-id', 'wake-1',
                'generate-reply', '--turn-id', self.prepared['turn_id'], '--request', '@' + str(request)])
        self.assertEqual(code, 0, output.getvalue()); self.assertTrue(json.loads(output.getvalue())['ok'])
