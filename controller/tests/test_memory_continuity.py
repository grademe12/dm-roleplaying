"""Real SQLite and mock-send tests for evidence, delivery, scope, and rollback."""
import json
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import ControlError
from dispatch import Dispatcher, MockTransport
from store import Store, validate_conversation_review
import memory_continuity as memory
import test_controller as baseline

T = baseline.T


class MemoryContinuityTests(unittest.TestCase):
    decision = baseline.ControllerTests.decision
    review = baseline.ControllerTests.review
    snapshot = baseline.ControllerTests.snapshot

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='memory-', dir=ROOT / 'tests')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'test.db'
        self.store = Store(self.path)
        self.store.seed({}, T)
        self.clock = T
        self.dispatch = Dispatcher(self.store, lambda: self.clock)

    def enable(self, mode='enforce', scopes=None):
        return memory.configure(self.store, 'main', {'mode': mode, 'scopes': scopes or ['private_dm']}, self.clock)

    def incoming(self, mid='s1', body='그 설명에서는 정확한 명칭을 써줘.', channel='slack', identity='platform'):
        return self.store.ingest('main', {'read_ok': True, 'messages': [
            {'id': mid, 'text': body, 'observed_ms': self.clock,
             'observation': {'identity': identity, 'channel': channel}}]}, self.clock)

    def op(self, mid='p1', **values):
        return {'op': 'remember', 'id': mid, 'kind': 'preference', 'key': 'wording',
                'scope': 'private_dm', 'channel': 'slack', 'summary': '이 설명에서는 정확한 명칭을 선호',
                'scope_note': '해당 설명 맥락에 한함; 전체 말투 규칙이 아님',
                'evidence': [{'message_id': 's1', 'excerpt': '정확한 명칭을 써줘'}], **values}

    def apply(self, ops=None, **kwargs):
        return self.store.apply('main', self.decision(memory_ops=ops or [], **kwargs), self.clock)

    def reply(self, parts=None, gid='g1', mid='s1'):
        return {'action': 'reply', 'group_id': gid, 'message_ids': [mid],
                'due_ms': self.clock, 'parts': parts or ['응, 정확한 명칭으로 말할게.'], 'reason': 'synthetic test'}

    def utterance(self, mid='u1', gid='g1', part=0, excerpt='정확한 명칭으로 말할게'):
        return self.op(mid, kind='utterance', key='wording-offer', summary='정확한 명칭으로 말하겠다고 발언함',
                       scope_note='발언 증거; 영구 선호나 약속 완료가 아님', evidence=[],
                       on_send={'group_id': gid, 'part_index': part, 'excerpt': excerpt})

    def rows(self, history=False, key=None, channel='slack', scopes=None):
        return self.store.memory_context('main', channel, scopes, history=history, key=key)['items']

    def test_default_off_has_no_injection_and_refuses_collection(self):
        self.incoming()
        self.assertNotIn('memory_context', self.snapshot())
        with self.assertRaisesRegex(ControlError, 'memory_policy_off'):
            self.apply([self.op()])
        self.assertEqual(self.rows(), [])

    def test_preference_is_atomic_scoped_and_visible_in_enforce_only(self):
        self.enable(); self.incoming()
        result = self.apply([self.op(scope='explanation')])
        self.assertEqual(result['memory_results'][0]['status'], 'active')
        self.assertEqual(self.snapshot()['memory_context']['slack']['items'], [])
        self.assertEqual(self.rows(scopes=['explanation'])[0]['id'], 'p1')
        self.assertEqual(self.rows(channel='instagram', scopes=['explanation']), [])
        self.enable(scopes=['private_dm', 'explanation'])
        self.assertEqual(self.snapshot()['memory_context']['slack']['items'][0]['id'], 'p1')
        self.assertEqual(self.store.recall('main', ['memory_preference'])['facts'][0]['id'], 'memory:p1')
        self.enable('shadow')
        self.assertNotIn('memory_context', self.snapshot())
        self.assertEqual(self.store.recall('main', ['memory_preference'])['facts'], [])
        self.assertEqual(self.store.memory_recall('main', ['memory_preference'])['facts'][0]['id'], 'memory:p1')

    def test_wrong_evidence_is_rejected_without_partial_writes(self):
        self.enable(); self.incoming()
        for changes, reason in [({'evidence': [{'message_id': 'missing', 'excerpt': '정확한'}]}, 'not_found'),
                                ({'evidence': [{'message_id': 's1', 'excerpt': '없는 문장'}]}, 'excerpt_mismatch'),
                                ({'channel': 'instagram'}, 'channel_mismatch'),
                                ({'evidence': []}, 'evidence_required')]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ControlError, reason):
                self.apply([self.op(**changes)])
        self.assertEqual(self.rows(), [])

    def test_unverified_incoming_is_not_a_memory_source(self):
        self.enable(); self.incoming(identity='uncertain')
        with self.assertRaisesRegex(ControlError, 'not_verified'):
            self.apply([self.op()])

    def test_nickname_requires_incoming_and_confirmed_acceptance(self):
        self.enable(); self.incoming(body='우리 애기라고 불러도 돼?')
        candidate = self.op('n1', kind='nickname', key='nickname:애기', summary='애칭 애기를 수락함',
                            scope_note='사용자와의 개인 DM에서 수락; 매번 사용은 아님',
                            evidence=[{'message_id': 's1', 'excerpt': '애기라고 불러도 돼?'}])
        with self.assertRaisesRegex(ControlError, 'confirmed_send_required'):
            self.apply([candidate])
        candidate['on_send'] = {'group_id': 'g1', 'part_index': 0, 'excerpt': '그렇게 불러도 좋아'}
        self.apply([candidate], dm_decisions=[self.reply(['응, 그렇게 불러도 좋아.'])])
        self.assertEqual(self.rows(), [])
        self.dispatch.send_due('main', MockTransport(), channel='slack', limit=1)
        self.assertEqual(self.rows()[0]['id'], 'n1')

    def test_selected_utterance_waits_for_its_own_part(self):
        self.enable(); self.incoming()
        self.apply([self.utterance(part=1)], dm_decisions=[self.reply(['응, 들었어.', '정확한 명칭으로 말할게.'])])
        self.dispatch.send_due('main', MockTransport(), channel='slack', limit=1)
        self.assertEqual(self.rows(), [])
        self.clock += 60_000
        self.dispatch.send_due('main', MockTransport(), channel='slack', limit=1)
        self.assertEqual(self.rows()[0]['kind'], 'utterance')
        remembered = self.store.recall('main', ['self_said:wording-offer'])['facts'][0]
        self.assertEqual(remembered['id'], 'memory:u1')

    def test_utterance_can_omit_incoming_evidence_without_losing_current_reply(self):
        self.enable(); self.incoming()
        self.apply(dm_decisions=[self.reply()])
        self.dispatch.send_due('main', MockTransport(), channel='slack', limit=1)
        self.clock += 60_000
        self.incoming('s2', '그 설명은 어떻게 되고 있어?')
        operation = self.utterance()
        del operation['evidence']
        result = self.apply([operation], dm_decisions=[self.reply(gid='g2', mid='s2')])
        self.assertEqual(result['memory_results'][0]['status'], 'active')
        self.assertEqual(self.rows()[0]['evidence'], [])
        self.assertEqual(self.rows()[0]['job_id'], 'g1:0')
        self.dispatch.send_due('main', MockTransport(), channel='slack', limit=1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM dm_jobs WHERE id='g2:0'").fetchone()[0], 'confirmed')

    def test_optional_utterance_evidence_does_not_relax_required_provenance(self):
        self.enable(); self.incoming()
        operation = self.utterance()
        del operation['evidence']
        for changed, reason in [({**operation, 'on_send': None}, 'confirmed_send_required'),
                                ({**operation, 'evidence': None}, 'invalid_memory_evidence'),
                                ({**operation, 'on_send': {**operation['on_send'], 'excerpt': '없는 발언'}}, 'excerpt_mismatch')]:
            with self.subTest(changed=changed), self.assertRaisesRegex(ControlError, reason):
                self.apply([changed], dm_decisions=[self.reply()])
        preference = self.op()
        del preference['evidence']
        with self.assertRaisesRegex(ControlError, 'invalid_memory_remember'):
            self.apply([preference], dm_decisions=[self.reply()])
        self.assertEqual(self.snapshot()['jobs'], [])
        self.apply([operation], dm_decisions=[self.reply()])
        self.dispatch.send_due('main', MockTransport('unknown'), channel='slack', limit=1)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.rows(history=True, key='wording-offer')[0]['status'], 'pending')

    def test_other_channel_preference_is_collected_in_source_channel(self):
        self.enable(); self.incoming(channel='instagram')
        self.apply([self.op(channel='instagram')])
        self.assertEqual(self.rows(channel='slack'), [])
        self.assertEqual(self.rows(channel='instagram')[0]['id'], 'p1')

    def test_unknown_send_is_not_speech_until_positive_reconcile(self):
        self.enable(); self.incoming()
        self.apply([self.utterance()], dm_decisions=[self.reply()])
        self.dispatch.send_due('main', MockTransport('unknown'), channel='slack', limit=1)
        self.assertEqual(self.rows(), [])
        with self.store.connect() as db:
            job = dict(db.execute("SELECT * FROM dm_jobs WHERE session_id='main'").fetchone())
        self.dispatch.reconcile('main', {'job_id': job['id'], 'source': 'manual',
            'expected_revision': self.snapshot()['revision'], 'attempt_token': job['attempt_token'],
            'result': {'status': 'confirmed', 'provider_message_id': 'positive', 'evidence': 'mock positive receipt'}})
        self.assertEqual(self.rows()[0]['id'], 'u1')

    def test_failed_send_cancels_candidate_without_fact(self):
        self.enable(); self.incoming()
        self.apply([self.utterance()], dm_decisions=[self.reply()])
        self.dispatch.send_due('main', MockTransport('failed'), channel='slack', limit=1)
        self.assertEqual(self.rows(history=True, key='wording-offer')[0]['status'], 'cancelled')
        self.assertEqual(self.store.recall('main', ['self_said:wording-offer'])['facts'], [])

    def test_invalid_memory_rolls_back_dm_and_prior_memory(self):
        self.enable(); self.incoming()
        revision = self.snapshot()['revision']
        with self.assertRaisesRegex(ControlError, 'excerpt_mismatch'):
            self.apply([self.op(), self.op('bad', key='other', evidence=[{'message_id': 's1', 'excerpt': '없음'}])],
                       dm_decisions=[self.reply()])
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.snapshot()['revision'], revision)
        self.assertEqual(self.snapshot()['jobs'], [])

    def test_preflight_does_not_keep_memories(self):
        self.enable(); self.incoming()
        self.store.apply('main', self.decision(memory_ops=[self.op()]), self.clock, dry_run=True)
        self.assertEqual(self.rows(), [])

    def test_shadow_bad_memory_preserves_reply_delivery_and_committed_diagnostic(self):
        self.enable('shadow');self.incoming()
        bad=self.op(evidence=[{'message_id':'s1','excerpt':'없는 구절'}])
        draft=self.decision(memory_ops=[bad],dm_decisions=[self.reply()],processed_event_ids=['dm:s1'])
        telemetry={}
        result=self.store.apply('main',draft,self.clock,telemetry=telemetry)
        self.assertEqual(result['reason'],'applied')
        self.assertEqual(result['memory_results'],[])
        self.assertEqual(result['memory_report']['status'],'shadow_rejected')
        self.assertEqual(result['memory_report']['error_code'],'memory_evidence_excerpt_mismatch')
        self.assertEqual(telemetry['memory_status'],'shadow_rejected')
        self.assertEqual(self.rows(),[])
        self.assertEqual(len(self.snapshot()['jobs']),1)
        self.dispatch.send_due('main',MockTransport(),channel='slack',limit=1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM dm_jobs WHERE id='g1:0'").fetchone()[0],'confirmed')
            self.assertEqual(db.execute("SELECT status FROM dm_messages WHERE id='s1'").fetchone()[0],'handled')
        replay=self.store.apply('main',draft,self.clock)
        self.assertEqual(replay['reason'],'already_applied')
        diagnostics=self.store.memory_diagnostics('main',draft['decision_id'])
        self.assertEqual(diagnostics['total'],1)
        self.assertEqual(diagnostics['items'][0]['error_code'],'memory_evidence_excerpt_mismatch')
        self.assertEqual(diagnostics['items'][0]['operation_count'],1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM dm_jobs WHERE session_id='main'").fetchone()[0],1)

    def test_shadow_batch_rolls_back_good_memory_and_revoke_when_later_memory_is_bad(self):
        self.enable('shadow');self.incoming();self.apply([self.op('existing')])
        ops=[{'op':'revoke','id':'existing','reason':'test'},self.op('new',key='other'),
             self.op('bad',key='third',evidence=[{'message_id':'s1','excerpt':'없는 구절'}])]
        result=self.store.apply('main',self.decision(memory_ops=ops,dm_decisions=[self.reply()]),self.clock)
        self.assertEqual(result['memory_report']['operation_count'],3)
        self.assertEqual([r['id'] for r in self.rows()],['existing'])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM facts WHERE id IN ('memory:new','memory:bad')").fetchone()[0],0)
        self.dispatch.send_due('main',MockTransport(),channel='slack',limit=1)
        self.assertEqual(self.store.memory_diagnostics('main')['total'],1)

    def test_shadow_malformed_memory_batch_does_not_reject_valid_reply(self):
        self.enable('shadow')
        for index,ops in enumerate((None,{},[{}]*13)):
            with self.subTest(ops=ops):
                mid='s'+str(index);gid='reply'+str(index)
                self.incoming(mid)
                draft=self.decision(memory_ops=ops,dm_decisions=[self.reply(mid=mid,gid=gid)])
                result=self.store.apply('main',draft,self.clock)
                self.assertEqual(result['memory_report']['error_code'],'invalid_memory_ops')
                self.assertEqual(result['memory_results'],[])
                sent=self.dispatch.send_due('main',MockTransport(),channel='slack',limit=1)
                self.assertEqual(sent['results'][0]['job_id'],gid+':0')
                self.clock+=60000
        self.assertEqual(self.store.memory_diagnostics('main')['total'],3)

    def test_off_and_enforce_keep_strict_memory_validation_and_reply_rollback(self):
        self.incoming()
        for mode,reason in [('off','memory_policy_off'),('enforce','memory_evidence_excerpt_mismatch')]:
            self.enable(mode)
            before=self.snapshot()['revision']
            draft=self.decision(memory_ops=[self.op(evidence=[{'message_id':'s1','excerpt':'없는 구절'}])],
                                dm_decisions=[self.reply()])
            with self.subTest(mode=mode),self.assertRaisesRegex(ControlError,reason):
                self.store.apply('main',draft,self.clock)
            self.assertEqual(self.snapshot()['revision'],before)
            self.assertEqual(self.snapshot()['jobs'],[])
            self.assertEqual(self.store.memory_diagnostics('main')['total'],0)

    def test_shadow_does_not_swallow_database_errors(self):
        self.enable('shadow');self.incoming()
        draft=self.decision(memory_ops=[self.op()],dm_decisions=[self.reply()])
        with patch.object(memory,'apply_ops',side_effect=sqlite3.OperationalError('injected database failure')):
            with self.assertRaisesRegex(sqlite3.OperationalError,'injected'):
                self.store.apply('main',draft,self.clock)
        self.assertEqual(self.snapshot()['jobs'],[])
        self.assertEqual(self.store.memory_diagnostics('main')['total'],0)

    def test_shadow_does_not_swallow_later_nonmemory_validation_errors(self):
        self.enable('shadow');self.incoming()
        draft=self.decision(memory_ops=[{'op':'bad'}],dm_decisions=[self.reply()],advance_to_ms=self.clock-1)
        with self.assertRaisesRegex(ControlError,'invalid_advance_time'):
            self.store.apply('main',draft,self.clock)
        self.assertEqual(self.snapshot()['jobs'],[])
        self.assertEqual(self.store.memory_diagnostics('main')['total'],0)

    def test_shadow_preflight_returns_rejection_without_persisting_reply_or_diagnostic(self):
        self.enable('shadow');self.incoming()
        draft=self.decision(memory_ops=[{'op':'bad'}],dm_decisions=[self.reply()])
        before=self.snapshot()['revision']
        result=self.store.apply('main',draft,self.clock,dry_run=True)
        self.assertEqual(result['reason'],'conversation_preflight')
        self.assertEqual(result['memory_report']['status'],'shadow_rejected')
        self.assertEqual(self.snapshot()['revision'],before)
        self.assertEqual(self.snapshot()['jobs'],[])
        self.assertEqual(self.store.memory_diagnostics('main')['total'],0)
        self.store.apply('main',draft,self.clock)
        self.assertEqual(self.store.memory_diagnostics('main')['total'],1)

    def test_diagnostic_table_upgrade_preserves_existing_policy_memory_and_revision(self):
        self.enable('shadow');self.incoming();self.apply([self.op()])
        before=self.snapshot()['revision']
        with self.store.connect(True) as db:db.execute('DROP TABLE continuity_diagnostics')
        upgraded=Store(self.path)
        self.assertEqual(upgraded.memory_context('main','slack')['mode'],'shadow')
        self.assertEqual(upgraded.memory_context('main','slack')['items'][0]['id'],'p1')
        self.assertEqual(upgraded.memory_diagnostics('main')['total'],0)
        self.assertEqual(self.snapshot()['revision'],before)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT version FROM controller_meta').fetchone()[0],'0.3')

    def test_diagnostic_cli_pages_without_exposing_candidate_text(self):
        self.enable('shadow');self.incoming()
        ids=[]
        for _ in range(2):
            draft=self.decision(memory_ops=[self.op(summary='private-candidate-marker',evidence=[])])
            self.store.apply('main',draft,self.clock);ids.append(draft['decision_id']);self.clock+=1000
        before=self.snapshot()['revision']
        argv=[sys.executable,str(ROOT/'ctl.py'),'--db',str(self.path),'memory-diagnostics']
        result=subprocess.run([*argv,'--limit','1'],check=True,capture_output=True,text=True)
        page=json.loads(result.stdout)
        self.assertEqual(page['total'],2);self.assertTrue(page['has_more'])
        self.assertNotIn('private-candidate-marker',result.stdout)
        result=subprocess.run([*argv,'--decision-id',ids[0]],check=True,capture_output=True,text=True)
        self.assertEqual(json.loads(result.stdout)['items'][0]['decision_id'],ids[0])
        self.assertEqual(self.snapshot()['revision'],before)

    def test_versions_are_explicit_and_history_is_preserved(self):
        self.enable(); self.incoming()
        self.apply([self.op()])
        changed = self.op('p2', summary='새 교정에 따른 현재 표현 선호')
        with self.assertRaisesRegex(ControlError, 'explicit_version'):
            self.apply([changed])
        changed['supersedes'] = 'p1'
        self.apply([changed])
        self.assertEqual([r['id'] for r in self.rows()], ['p2'])
        self.assertEqual({r['status'] for r in self.rows(history=True, key='wording')}, {'active', 'superseded'})
        self.assertEqual([f['id'] for f in self.store.recall('main', ['memory_preference'])['facts']], ['memory:p2'])
        with self.store.connect() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM facts WHERE id='memory:p1'").fetchone())

    def test_revocation_hides_but_does_not_erase_record(self):
        self.enable(); self.incoming(); self.apply([self.op()])
        self.apply([{'op': 'revoke', 'id': 'p1', 'reason': '사용자가 선호를 철회함'}])
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.rows(history=True, key='wording')[0]['status'], 'revoked')
        self.assertEqual(self.store.memory_recall('main', ['memory_preference'])['facts'], [])

    def test_off_cancels_pending_even_if_send_confirms_after_reenable(self):
        self.enable(); self.incoming()
        self.apply([self.utterance()], dm_decisions=[self.reply()])
        self.enable('off'); self.enable()
        self.dispatch.send_due('main', MockTransport(), channel='slack', limit=1)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.rows(history=True, key='wording-offer')[0]['status'], 'cancelled')

    def test_changed_pending_text_does_not_roll_back_real_receipt(self):
        self.enable(); self.incoming()
        self.apply([self.utterance()], dm_decisions=[self.reply()])
        with self.store.connect(True) as db:
            db.execute("UPDATE dm_jobs SET text='다른 문장으로 바뀜' WHERE session_id='main'")
        self.dispatch.send_due('main', MockTransport(), channel='slack', limit=1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM dm_jobs WHERE session_id='main'").fetchone()[0], 'confirmed')
        self.assertEqual(self.rows(history=True, key='wording-offer')[0]['status'], 'cancelled')

    def test_general_facts_cannot_forge_memory_namespace(self):
        with self.assertRaisesRegex(ControlError, 'memory_fact_owned'):
            self.apply(facts=[{'id': 'memory:fake', 'source': 'observed', 'summary': 'fake',
                               'details': {'evidence': 'not a typed memory'}}])

    def test_utterance_is_not_factual_basis_for_identity(self):
        self.enable(); self.incoming()
        self.apply([self.utterance()], dm_decisions=[self.reply()])
        self.dispatch.send_due('main', MockTransport(), channel='slack', limit=1)
        dm = self.reply(['정확한 명칭으로 말할게.'])
        dm['conversation_review'] = self.review(dm)
        dm['conversation_review']['checks']['self_consistency']['claims'] = [
            {'fact_id': 'memory:u1', 'excerpt': '정확한 명칭'}]
        with self.store.connect() as db, self.assertRaisesRegex(ControlError, 'requires_factual_basis'):
            validate_conversation_review(db, 'main', dm, dm['parts'], ['s1'], 'slack')

    def test_details_and_oldest_page_recover_records_past_top_ten(self):
        self.enable()
        facts = [{'id': 'old-hesitation', 'source': 'simulated', 'summary': '사진 이력',
                  'details': {'outcome': '미뤘다가 나중에 이행함'}}]
        self.apply(facts=facts)
        self.clock += 1000
        for n in range(11):
            self.apply(facts=[{'id': f'new-{n}', 'source': 'simulated', 'summary': '사진 이력 최근 대화', 'details': {}}])
            self.clock += 1000
        self.assertNotIn('old-hesitation', [f['id'] for f in self.store.recall('main', ['사진 이력'])['facts']])
        oldest = self.store.memory_recall('main', ['사진 이력'], order='oldest', limit=1)
        self.assertEqual(oldest['facts'][0]['id'], 'old-hesitation')
        self.assertTrue(oldest['has_more'])
        self.assertEqual(self.store.memory_recall('main', ['미뤘다가'])['facts'][0]['id'], 'old-hesitation')
        self.assertEqual(self.store.memory_recall('main', ['사진 이력'], before_ms=T+1)['total'], 1)

    def test_inactive_or_wrong_scope_memory_cannot_support_identity_claim(self):
        self.enable(); self.incoming(); self.apply([self.op()])
        with self.store.connect() as db:
            self.assertTrue(memory.valid_factual_basis(db, 'main', 'memory:p1', 'slack'))
            self.assertFalse(memory.valid_factual_basis(db, 'main', 'memory:p1', 'instagram'))
        for mode, selected in [('shadow', ['private_dm']), ('off', ['private_dm']), ('enforce', ['other'])]:
            self.enable(mode, selected)
            with self.store.connect() as db:
                self.assertFalse(memory.valid_factual_basis(db, 'main', 'memory:p1', 'slack'))
        self.enable()
        self.apply([{'op': 'revoke', 'id': 'p1', 'reason': '선호 철회'}])
        dm = self.reply()
        dm['conversation_review'] = self.review(dm)
        dm['conversation_review']['checks']['self_consistency']['claims'] = [
            {'fact_id': 'memory:p1', 'excerpt': '정확한 명칭'}]
        with self.store.connect() as db, self.assertRaisesRegex(ControlError, 'requires_factual_basis'):
            validate_conversation_review(db, 'main', dm, dm['parts'], ['s1'], 'slack')

    def test_revoked_predecessor_cancels_pending_replacement_without_losing_receipt(self):
        self.enable(); self.incoming(); self.apply([self.op()])
        self.apply([self.op('p2', supersedes='p1', on_send={
            'group_id': 'g1', 'part_index': 0, 'excerpt': '정확한 명칭'})], dm_decisions=[self.reply()])
        self.assertEqual([r['id'] for r in self.rows()], ['p1'])
        self.apply([{'op': 'revoke', 'id': 'p1', 'reason': '사용자가 철회'}])
        self.dispatch.send_due('main', MockTransport(), channel='slack', limit=1)
        self.assertEqual(self.rows(), [])
        history = {r['id']: r for r in self.rows(history=True, key='wording')}
        self.assertEqual(history['p2']['status'], 'cancelled')
        self.assertEqual(history['p2']['reason'], 'memory_version_changed')
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM dm_jobs WHERE session_id='main'").fetchone()[0], 'confirmed')

    def test_snapshot_byte_budget_keeps_complete_evidence_and_explicit_paging(self):
        self.enable()
        for i in range(3):
            self.incoming('src' + str(i), '한' * 200)
        evidence = [{'message_id': 'src' + str(i), 'excerpt': '한' * 200} for i in range(3)]
        for i in range(4):
            self.apply([self.op('large' + str(i), key='key' + str(i), summary='설' * 400,
                                scope_note='범' * 240, evidence=evidence)])
        snapshot = self.snapshot()['memory_context']
        self.assertLessEqual(len(json.dumps(snapshot, ensure_ascii=False).encode('utf-8')), memory.MAX_SNAPSHOT_BYTES)
        page = snapshot['slack']
        self.assertGreater(len(page['items']), 0)
        self.assertTrue(page['has_more'])
        self.assertEqual(page['total'], 4)
        for item in page['items']:
            self.assertEqual(item['evidence'], evidence)
        rest = self.store.memory_context('main', 'slack', offset=len(page['items']))['items']
        self.assertEqual(len(page['items']) + len(rest), 4)
        with self.assertRaisesRegex(ControlError, 'op_too_large'):
            self.apply([{**self.op('oversized'), 'unused': '한' * 3000}])

    def test_expanded_search_preserves_world_event_known_by_filter(self):
        from datetime import datetime, timezone
        self.enable()
        self.store.event_import('main', {'schema_version': 1, 'request_id': 'hidden-memory', 'events': [{
            'event_id': 'hidden-detail', 'mode': 'occurred', 'summary': '비공개 친구의 기록',
            'known_by': ['friend'], 'participants': ['friend'],
            'occurred_at': datetime.fromtimestamp(self.clock / 1000, timezone.utc).isoformat(),
            'followup_suggestions': ['검색에 보여서는 안 됨']}]}, self.clock, 'synthetic test instruction')
        self.assertEqual(self.store.memory_recall('main', ['비공개', '검색에'])['facts'], [])

    def test_history_discovers_revoked_key_from_evidence_without_reactivating_it(self):
        self.enable(); self.incoming()
        self.apply([self.op(key='opaque:v1',summary='설명 형식',scope_note='이 설명에서만')])
        self.clock += 1000
        self.apply([{'op':'revoke','id':'p1','reason':'사용자 철회'}])
        revision=self.snapshot()['revision']
        page=self.store.memory_history_search('main','slack',['정확한 명칭'])
        self.assertEqual([(r['id'],r['key'],r['status']) for r in page['items']],
                         [('p1','opaque:v1','revoked')])
        self.assertNotIn('evidence',page['items'][0])
        history=self.rows(history=True,key=page['items'][0]['key'])
        self.assertEqual(history[0]['evidence'][0]['message_id'],'s1')
        self.assertEqual(self.rows(),[])
        self.assertEqual(self.store.recall('main',['정확한 명칭'])['facts'],[])
        self.assertEqual(self.store.memory_recall('main',['정확한 명칭'])['facts'],[])
        self.assertEqual(self.snapshot()['revision'],revision)

    def test_history_versions_have_stable_pages_and_lifecycle_cutoff(self):
        self.enable();self.incoming();self.apply([self.op()])
        self.clock+=1000
        self.apply([self.op('p2',supersedes='p1')])
        page=self.store.memory_history_search('main','slack',['정확한'],limit=1,order='oldest')
        self.assertEqual(page['items'][0]['id'],'p1')
        self.assertEqual(page['items'][0]['status'],'superseded')
        self.assertTrue(page['has_more'])
        self.assertEqual(self.store.memory_history_search('main','slack',['정확한'],limit=1,
                         order='oldest',offset=1)['items'][0]['id'],'p2')
        self.assertEqual(self.store.memory_history_search('main','slack',['정확한'],
                         before_ms=self.clock)['total'],0)
        self.assertEqual(self.store.memory_history_search('main','slack',['정확한'],
                         before_ms=self.clock+1)['total'],2)

    def test_history_discovery_keeps_session_scope_and_channel_boundaries(self):
        self.enable();self.incoming();self.incoming('i1',channel='instagram')
        self.apply([self.op(),self.op('other',scope='other'),self.op('insta',channel='instagram',
            evidence=[{'message_id':'i1','excerpt':'정확한 명칭을 써줘'}])])
        with self.store.connect(True) as db:
            columns=[r['name'] for r in db.execute('PRAGMA table_info(sessions)')]
            values=list(db.execute("SELECT * FROM sessions WHERE id='main'").fetchone())
            values[columns.index('id')]='other-session'
            db.execute('INSERT INTO sessions VALUES ('+','.join('?' for _ in values)+')',values)
        self.assertEqual([r['id'] for r in self.store.memory_history_search('main','slack',['정확한'])['items']],['p1'])
        self.assertEqual([r['id'] for r in self.store.memory_history_search('main','slack',['정확한'],['other'])['items']],['other'])
        self.assertEqual([r['id'] for r in self.store.memory_history_search('main','instagram',['정확한'])['items']],['insta'])
        self.assertEqual(self.store.memory_history_search('other-session','slack',['정확한'])['items'],[])

    def test_history_does_not_promote_unknown_or_revoked_pending_candidates(self):
        self.enable();self.incoming()
        self.apply([self.utterance()],dm_decisions=[self.reply()])
        self.dispatch.send_due('main',MockTransport('unknown'),channel='slack',limit=1)
        self.assertEqual(self.store.memory_history_search('main','slack',['정확한'])['items'],[])
        self.apply([{'op':'revoke','id':'u1','reason':'후보 철회'}])
        self.assertEqual(self.store.memory_history_search('main','slack',['정확한'])['items'],[])
        self.assertEqual(self.rows(history=True,key='wording-offer')[0]['status'],'revoked')

    def test_history_is_explicit_diagnostic_in_off_mode_and_has_no_injection(self):
        self.enable();self.incoming();self.apply([self.op()]);self.enable('off')
        result=self.store.memory_history_search('main','slack',['정확한'])
        self.assertEqual(result['mode'],'off')
        self.assertEqual(result['items'][0]['id'],'p1')
        self.assertNotIn('memory_context',self.snapshot())
        self.assertEqual(self.store.recall('main',['정확한'])['facts'],[])

    def test_history_literal_search_and_invalid_pages(self):
        self.enable();self.incoming();self.apply([self.op(summary='비율 100%와 snake_case')])
        self.assertEqual(self.store.memory_history_search('main','slack',['100%'])['total'],1)
        self.assertEqual(self.store.memory_history_search('main','slack',['100_'])['total'],0)
        self.assertEqual(self.store.memory_history_search('main','slack',['snake_case'])['total'],1)
        for args in ({'keywords':[]},{'keywords':['']},{'keywords':['x']*11},{'limit':11},
                     {'offset':-1},{'order':'bad'},{'before_ms':True},{'selected_scopes':[]}):
            options={'keywords':['정확한'],**args}
            with self.subTest(args=args),self.assertRaises(ControlError):
                self.store.memory_history_search('main','slack',**options)

    def test_cli_history_key_discovery_then_exact_history(self):
        self.enable();self.incoming();self.apply([self.op(key='hidden-key')])
        self.apply([{'op':'revoke','id':'p1','reason':'철회'}])
        argv=[sys.executable,str(ROOT/'ctl.py'),'--db',str(self.path)]
        result=subprocess.run([*argv,'memory-history-search','--channel','slack','--keywords','정확한'],
                              capture_output=True,encoding='utf-8',check=True)
        key=json.loads(result.stdout)['items'][0]['key']
        result=subprocess.run([*argv,'memory-context','--channel','slack','--key',key,'--history'],
                              capture_output=True,encoding='utf-8',check=True)
        self.assertEqual(json.loads(result.stdout)['items'][0]['status'],'revoked')

    def test_cli_reads_without_external_api(self):
        self.enable(); self.incoming(); self.apply([self.op()])
        result = subprocess.run([sys.executable, str(ROOT / 'ctl.py'), '--db', str(self.path),
                                 'memory-context', '--channel', 'slack'], capture_output=True, encoding='utf-8', check=True)
        self.assertEqual(json.loads(result.stdout)['items'][0]['id'], 'p1')


if __name__ == '__main__':
    unittest.main()
