"""Real SQLite/apply/send tests for the conversation repetition contract."""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
import unittest

import test_controller as baseline
from common import ControlError, decode
from dispatch import MockTransport
from store import Store
import conversation_flow as flow


class ConversationFlowTests(unittest.TestCase):
    tearDown = baseline.ControllerTests.tearDown
    snapshot = baseline.ControllerTests.snapshot
    decision = baseline.ControllerTests.decision
    review = baseline.ControllerTests.review
    ingest = baseline.ControllerTests.ingest
    activity = baseline.ControllerTests.activity

    def setUp(self):
        baseline.ControllerTests.setUp(self)
        flow.configure(self.store, 'main', {'mode': 'enforce'}, self.clock)
        self.serial = 0

    def register(self, kind='statement', facet='premise', point='premise', scope='stable',
                 material='demo-series', subject='series:demo', source='experience'):
        self.serial += 1
        fid = 'f'+str(self.serial)
        fact = {'id': fid, 'source': 'simulated', 'summary': fid,
                'occurred_ms': self.clock, 'details': {'point_keys': [point]}}
        updates = []
        if material:
            updates.append({'op': 'register_material', 'ref': 'material', 'canonical_key': material,
                            'subject_key': subject, 'kind': source, 'fact_ids': [fid]})
        updates.append({'op': 'register_unit', 'ref': 'unit', 'kind': kind, 'subject_key': subject,
                        'facet_key': facet, 'scope_key': scope, 'summary': facet,
                        'fact_ids': [fid], 'point_keys': [point],
                        **({'material_id': '@material'} if material else {})})
        result = self.store.apply('main', self.decision(facts=[fact], conversation_updates=updates), self.clock)
        return result['conversation_refs']['unit'], fid

    def plan(self, unit, body='새 이야기', mode='introduce', basis=None, index=0):
        return {'version': 1, 'units': [{'unit_id': unit, 'mode': mode,
                'spans': [{'part_index': index, 'excerpt': body}], 'basis': basis or {}}]}

    def queue(self, unit, gid='g1', mid='m1', body='새 이야기', mode='introduce', basis=None, parts=None):
        self.ingest(mid)
        dm = {'action': 'reply', 'message_ids': [mid], 'group_id': gid, 'parts': parts or [body],
              'due_ms': self.clock, 'reason': 'ordinary exchange',
              'conversation_plan': self.plan(unit, body, mode, basis)}
        d = self.decision(dm_decisions=[dm])
        self.store.apply('main', d, self.clock)
        return d

    def send(self, scenario='confirmed', channel='instagram', limit=3):
        self.clock += 60000
        return self.dispatch.send_due('main', MockTransport(scenario), limit=limit, channel=channel)

    def uses(self):
        return flow.context(self.store, 'main', self.clock, subject='series:demo')['uses']

    def test_default_is_off_and_configuration_is_persistent(self):
        other = self.path.parent/'off.db'
        store = Store(other); store.seed({}, self.clock)
        self.assertEqual(store.tick('main', self.clock)['conversation_policy']['mode'], 'off')
        self.assertEqual(Store(self.path).tick('main', self.clock)['conversation_policy']['mode'], 'enforce')

    def test_missing_plan_rolls_back_entire_decision(self):
        self.ingest()
        dm = {'action':'reply','message_ids':['m1'],'group_id':'g','parts':['hello'],
              'due_ms':self.clock,'reason':'reply'}
        revision = self.snapshot()['revision']
        with self.assertRaisesRegex(ControlError, 'conversation_plan_required'):
            self.store.apply('main', self.decision(dm_decisions=[dm], facts=[
                {'id':'should-not-exist','source':'simulated','summary':'rollback'}]), self.clock)
        self.assertEqual(self.snapshot()['revision'], revision)
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM facts WHERE id='should-not-exist'").fetchone())

    def test_registration_resolves_refs_and_cannot_rename_used_points(self):
        first, _ = self.register()
        second, _ = self.register(facet='different-words')
        self.assertEqual(first, second)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM conversation_materials').fetchone()[0], 1)

    def test_malformed_fact_metadata_is_a_controlled_atomic_failure(self):
        for details in ('not-an-object', {'point_keys':'not-a-list'}):
            with self.subTest(details=details), self.assertRaises(ControlError):
                self.store.apply('main',self.decision(facts=[{
                    'id':'bad','source':'simulated','summary':'bad','details':details}],
                    conversation_updates=[{'op':'register_unit','ref':'u','subject_key':'series:demo',
                        'kind':'statement','facet_key':'x','scope_key':'stable','summary':'x','fact_ids':['bad']}]),self.clock)
            with self.store.connect() as db:
                self.assertIsNone(db.execute("SELECT 1 FROM facts WHERE id='bad'").fetchone())

    def test_preflight_rolls_back_registrations_jobs_facts_and_revision(self):
        self.ingest()
        d = self.decision(facts=[{'id':'fact','source':'simulated','summary':'fact'}],
            conversation_updates=[{'op':'register_unit','ref':'unit','kind':'statement',
                'subject_key':'series:demo','facet_key':'thought','scope_key':'stable','summary':'thought','fact_ids':['fact']}],
            dm_decisions=[{'action':'reply','message_ids':['m1'],'group_id':'g','parts':['생각'],
                'due_ms':self.clock,'reason':'r','conversation_plan':self.plan('@unit', '생각')}])
        revision = self.snapshot()['revision']
        result = self.store.apply('main', d, self.clock, dry_run=True)
        self.assertEqual(result['revision'], revision)
        self.assertIn('unit', result['conversation_refs'])
        with self.store.connect() as db:
            for table in ('conversation_units', 'conversation_uses', 'reply_groups'):
                self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT 1 FROM facts WHERE id='fact'").fetchone())
        committed = self.store.apply('main', d, self.clock)
        self.assertEqual(committed['conversation_refs'], result['conversation_refs'])

    def test_pending_reservation_blocks_other_channel_without_preflight(self):
        unit, _ = self.register(); self.queue(unit)
        self.ingest('s2', observation={'identity':'platform','channel':'slack'})
        dm = {'action':'reply','message_ids':['s2'],'group_id':'g2','parts':['다른 표현'],
              'due_ms':self.clock,'reason':'r','conversation_plan':self.plan(unit,'다른 표현')}
        with self.assertRaisesRegex(ControlError,'conversation_unit_reserved'):
            self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock)
        self.assertEqual(len(self.uses()),1)

    def test_confirmed_introduction_is_blocked_even_after_cooldown(self):
        unit, _ = self.register(); self.queue(unit); self.send()
        self.clock += 25*flow.HOUR
        with self.assertRaisesRegex(ControlError,'conversation_material_already_shared'):
            self.queue(unit,'g2','m2')

    def test_recent_close_anchors_reintroduction_even_when_delivery_was_old(self):
        unit, _ = self.register(); self.queue(unit); self.send()
        use=self.uses()[0]['id']; self.clock+=25*flow.HOUR
        op=self.evidence(unit,use,'close',body='그 얘기는 여기까지')
        self.store.apply('main',self.decision(conversation_updates=[op]),self.clock)
        with self.assertRaisesRegex(ControlError,'conversation_recent_reintroduction'):
            self.queue(unit,'g2','m2',mode='reintroduce',basis={
                'prior_use_ids':[use],'request_refs':[{'message_id':'m2','excerpt':'hello'}]})

    def test_open_flow_can_continue(self):
        unit, _ = self.register(); self.queue(unit); self.send()
        self.queue(unit,'g2','m2',mode='continue',basis={'prior_use_ids':[self.uses()[0]['id']]})

    def test_failed_not_sent_releases_but_submitted_reserves(self):
        unit, _ = self.register(); self.queue(unit); self.send('failed')
        self.assertEqual(self.uses()[0]['status'],'released')
        self.queue(unit,'g2','m2')
        # Failure's original group is failed, not sendable; only g2 is due.
        self.send('submitted')
        states={u['group_id']:u['status'] for u in self.uses()}
        self.assertEqual(states['g2'],'reserved')

    def test_proactive_dm_uses_same_guard(self):
        unit, _ = self.register()
        dm={'action':'initiate','group_id':'first','channel':'instagram','parts':['소재'],
            'due_ms':self.clock,'reason':'new thought','conversation_plan':self.plan(unit,'소재')}
        self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock); self.send()
        dm['group_id']='second'; dm['due_ms']=self.clock
        with self.assertRaisesRegex(ControlError,'conversation_material_already_shared'):
            self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock)

    def test_additive_migration_preserves_legacy_jobs_and_revision(self):
        flow.configure(self.store,'main',{'mode':'off'},self.clock); self.ingest()
        dm={'action':'reply','message_ids':['m1'],'group_id':'legacy','parts':['hello'],
            'due_ms':self.clock,'reason':'r'}
        self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock)
        revision=self.snapshot()['revision']
        with self.store.connect(True) as db:
            for table in ('conversation_evidence','conversation_use_jobs','conversation_uses',
                          'conversation_groups','conversation_units','conversation_aliases',
                          'conversation_materials','conversation_policy','topic_search_runs'):
                db.execute('DROP TABLE '+table)
        reopened=Store(self.path)
        snap=reopened.tick('main',self.clock)
        self.assertEqual(snap['revision'],revision)
        self.assertEqual(snap['jobs'][0]['id'],'legacy:0')
        self.assertEqual(snap['conversation_policy']['mode'],'off')
        with reopened.connect() as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_confirmed_delivery_and_decision_replay_are_idempotent(self):
        unit, _ = self.register(); d = self.queue(unit)
        self.assertEqual(self.store.apply('main',d,self.clock)['reason'],'already_applied')
        self.send()
        first = self.uses()[0]['first_confirmed_ms']
        self.clock += 1000
        self.dispatch.send_due('main',MockTransport())
        self.assertEqual(self.uses()[0]['first_confirmed_ms'],first)
        self.assertEqual(self.uses()[0]['status'],'confirmed')

    def test_unknown_is_reserved_until_positive_manual_reconcile(self):
        unit, _ = self.register(); self.queue(unit); self.send('unknown')
        self.assertIsNone(self.uses()[0]['first_confirmed_ms'])
        self.assertEqual(self.uses()[0]['status'],'reserved')
        with self.store.connect() as db:
            job = dict(db.execute("SELECT * FROM dm_jobs WHERE id='g1:0'").fetchone())
        self.dispatch.reconcile('main',{'expected_revision':self.snapshot()['revision'],
            'job_id':job['id'],'attempt_token':job['attempt_token'],
            'result':{'status':'confirmed','provider_message_id':'actual-receipt','evidence':'positive receipt'}})
        self.assertEqual(self.uses()[0]['status'],'confirmed')

    def test_slack_receipt_reconcile_updates_same_ledger(self):
        unit, _ = self.register()
        self.ingest('s1',observation={'identity':'platform','channel':'slack'})
        dm={'action':'reply','message_ids':['s1'],'group_id':'g1','parts':['소식'],
            'due_ms':self.clock,'reason':'r','conversation_plan':self.plan(unit,'소식')}
        self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock)
        self.send('unknown',channel='slack')
        result={'status':'confirmed','provider_message_id':'1791946860.000001','evidence':'mock verified receipt'}
        with patch('dispatch.verified_result',return_value=result):
            self.dispatch.reconcile_slack_delivery('main','g1:0')
            self.dispatch.reconcile_slack_delivery('main','g1:0')
        self.assertEqual(self.uses()[0]['status'],'confirmed')

    def test_cancellation_releases_only_unsent_use(self):
        unit, _ = self.register(); self.queue(unit)
        self.store.apply('main',self.decision(dm_decisions=[{
            'action':'cancel_group','group_id':'g1','reason':'changed context'}]),self.clock)
        self.assertEqual(self.uses()[0]['status'],'released')
        # Original m1 is replyable again after cancellation.
        dm={'action':'reply','message_ids':['m1'],'group_id':'replacement','parts':['새 이야기'],
            'due_ms':self.clock,'reason':'r','conversation_plan':self.plan(unit)}
        self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock)

    def test_partial_delivery_keeps_delivered_part_and_releases_other_unit(self):
        one, _ = self.register(); two, _ = self.register(facet='new-detail',point='detail')
        self.ingest()
        plan={'version':1,'units':self.plan(one,'첫 부분')['units']+
              self.plan(two,'둘째 부분',index=1)['units']}
        dm={'action':'reply','message_ids':['m1'],'group_id':'g1','parts':['첫 부분','둘째 부분'],
            'due_ms':self.clock,'reason':'r','conversation_plan':plan}
        self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock)
        self.send(limit=1)
        self.store.apply('main',self.decision(dm_decisions=[{
            'action':'close_partial_group','group_id':'g1','reason':'enough'}]),self.clock)
        by_unit={u['unit_id']:u for u in self.uses()}
        self.assertEqual(by_unit[one]['status'],'confirmed')
        self.assertEqual(by_unit[two]['status'],'released')

    def test_one_unit_spanning_parts_stays_partial_after_close(self):
        unit, _ = self.register(); self.ingest()
        plan=self.plan(unit,'첫 부분')
        plan['units'][0]['spans'].append({'part_index':1,'excerpt':'둘째 부분'})
        dm={'action':'reply','message_ids':['m1'],'group_id':'g1','parts':['첫 부분','둘째 부분'],
            'due_ms':self.clock,'reason':'r','conversation_plan':plan}
        self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock); self.send(limit=1)
        self.store.apply('main',self.decision(dm_decisions=[{
            'action':'close_partial_group','group_id':'g1','reason':'enough'}]),self.clock)
        self.assertEqual(self.uses()[0]['status'],'partial')
        self.assertFalse(self.uses()[0]['unresolved'])

    def evidence(self, unit, use, kind, mid='answer', body='응, 이미 봤어'):
        self.ingest(mid,text=body)
        return {'op':'record_evidence','unit_id':unit,'use_id':use,'kind':kind,
                'refs':[{'message_id':mid,'excerpt':body}]}

    def test_answered_question_is_not_reasked_after_time_or_material_change(self):
        unit, _ = self.register(kind='question',facet='has-watched')
        self.queue(unit,body='봤어?'); self.send()
        op=self.evidence(unit,self.uses()[0]['id'],'answer')
        self.store.apply('main',self.decision(conversation_updates=[op]),self.clock)
        self.clock += 48*flow.HOUR
        same, _ = self.register(kind='question',facet='has-watched',material='another-source')
        self.assertEqual(same,unit)
        with self.assertRaisesRegex(ControlError,'conversation_question_answered'):
            self.queue(unit,'g2','m2',body='혹시 시청했어?')

    def test_answer_requires_actual_incoming_after_delivery(self):
        unit, _ = self.register(kind='question',facet='has-watched')
        self.ingest('old',text='봤어')
        self.queue(unit); self.send()
        op={'op':'record_evidence','unit_id':unit,'use_id':self.uses()[0]['id'],'kind':'answer',
            'refs':[{'message_id':'old','excerpt':'봤어'}]}
        with self.assertRaisesRegex(ControlError,'conversation_evidence_precedes_use'):
            self.store.apply('main',self.decision(conversation_updates=[op]),self.clock)
        op['refs']=[{'message_id':'missing','excerpt':'봤어'}]
        with self.assertRaisesRegex(ControlError,'conversation_request_invalid'):
            self.store.apply('main',self.decision(conversation_updates=[op]),self.clock)

    def test_user_reexplanation_uses_real_target_not_blanket_override(self):
        unit, _ = self.register(); self.queue(unit); self.send()
        self.ingest('m2',text='그 설정 다시 설명해줘')
        dm={'action':'reply','message_ids':['m2'],'group_id':'g2','parts':['다시 설명'],
            'due_ms':self.clock,'reason':'requested',
            'conversation_plan':self.plan(unit,'다시 설명','answer',
                {'request_refs':[{'message_id':'m2','excerpt':'다시 설명해줘'}]})}
        self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock)

    def test_exception_cannot_use_another_incoming_message(self):
        unit, _ = self.register(); self.queue(unit); self.send()
        self.ingest('other',text='다시 설명해줘'); self.ingest('m2')
        dm={'action':'reply','message_ids':['m2'],'group_id':'g2','parts':['다시 설명'],
            'due_ms':self.clock,'reason':'r',
            'conversation_plan':self.plan(unit,'다시 설명','answer',
                {'request_refs':[{'message_id':'other','excerpt':'다시 설명해줘'}]})}
        with self.assertRaisesRegex(ControlError,'conversation_request_not_target'):
            self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock)

    def test_closed_flow_cannot_be_reopened_as_continue(self):
        unit, _ = self.register(); self.queue(unit); self.send()
        use=self.uses()[0]['id']
        op=self.evidence(unit,use,'close',body='그 얘기는 여기까지 하자')
        self.store.apply('main',self.decision(conversation_updates=[op]),self.clock)
        with self.assertRaisesRegex(ControlError,'conversation_exception_invalid'):
            self.queue(unit,'g2','m2',mode='continue',basis={'prior_use_ids':[use]})

    def test_genuine_new_point_can_continue_same_subject(self):
        one, _ = self.register(); self.queue(one); self.send()
        prior=self.uses()[0]['id']; self.clock += 1000
        two, fid = self.register(facet='episode-2-opinion',point='episode-2')
        self.queue(two,'g2','m2',body='새 회차 감상',mode='update',
                   basis={'prior_use_ids':[prior],'fact_ids':[fid],'point_keys':['episode-2']})

    def test_old_fact_and_point_do_not_bypass_as_update(self):
        unit, fid = self.register(); self.queue(unit); self.send()
        with self.assertRaisesRegex(ControlError,'conversation_exception_invalid'):
            self.queue(unit,'g2','m2',mode='update',basis={
                'prior_use_ids':[self.uses()[0]['id']],'fact_ids':[fid],'point_keys':['premise']})

    def test_scope_separates_state_questions_from_different_days(self):
        one, _ = self.register(kind='question',facet='had-lunch',scope='2026-10-17')
        two, _ = self.register(kind='question',facet='had-lunch',scope='2026-10-18')
        self.assertNotEqual(one,two)

    def test_shadow_reports_known_conflict_but_enforce_blocks_same_input(self):
        unit, _ = self.register(); self.queue(unit); self.send()
        flow.configure(self.store,'main',{'mode':'shadow'},self.clock)
        d=self.queue(unit,'g2','m2')
        with self.store.connect() as db:
            warnings=decode(db.execute("SELECT warnings_json FROM conversation_groups WHERE group_id='g2'").fetchone()[0])
        self.assertEqual(warnings[0]['reason'],'conversation_material_already_shared')

    def test_plan_coverage_and_ambiguous_spans_are_rejected(self):
        unit, _ = self.register(); self.ingest()
        dm={'action':'reply','message_ids':['m1'],'group_id':'g','parts':['새 이야기 그리고 질문'],
            'due_ms':self.clock,'reason':'r','conversation_plan':self.plan(unit)}
        with self.assertRaisesRegex(ControlError,'conversation_plan_mismatch'):
            self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock)
        dm['parts']=['새 이야기 새 이야기']
        dm['conversation_review']=self.review(dm)
        with self.assertRaisesRegex(ControlError,'conversation_span_ambiguous'):
            self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock)

    def test_same_decision_two_groups_cannot_reserve_same_unit(self):
        unit, _ = self.register(); self.ingest('m1'); self.ingest('m2')
        dms=[{'action':'reply','message_ids':[mid],'group_id':'g'+mid,'parts':['소재'],
              'due_ms':self.clock,'reason':'r','conversation_plan':self.plan(unit,'소재')}
             for mid in ('m1','m2')]
        with self.assertRaisesRegex(ControlError,'conversation_unit_reserved'):
            self.store.apply('main',self.decision(dm_decisions=dms),self.clock)
        self.assertEqual(self.uses(),[])

    def test_preflight_is_not_an_authorization_for_a_later_duplicate(self):
        unit, _ = self.register(); self.ingest('m1')
        dm={'action':'reply','message_ids':['m1'],'group_id':'g1','parts':['소재'],
            'due_ms':self.clock,'reason':'r','conversation_plan':self.plan(unit,'소재')}
        d=self.decision(dm_decisions=[dm])
        self.store.apply('main',d,self.clock,dry_run=True)
        self.queue(unit,'g2','m2')
        d['expected_revision']=self.snapshot()['revision']; d['decision_id']='after-race'
        with self.assertRaisesRegex(ControlError,'conversation_unit_reserved'):
            self.store.apply('main',d,self.clock)

    def test_confirmed_mapping_cannot_be_removed_by_revalidation(self):
        unit, _ = self.register(); self.ingest()
        plan=self.plan(unit,'소재')
        plan['non_substantive']=[{'mode':'acknowledge','spans':[{'part_index':1,'excerpt':'응'}]}]
        dm={'action':'reply','message_ids':['m1'],'group_id':'g1','parts':['소재','응'],
            'due_ms':self.clock,'reason':'r','conversation_plan':plan}
        self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock); self.send(limit=1)
        revised={'version':1,'units':[],'non_substantive':[
            {'mode':'acknowledge','spans':[{'part_index':0,'excerpt':'소재'},{'part_index':1,'excerpt':'응'}]}]}
        dm={'action':'revalidate_group','group_id':'g1','reason':'r','conversation_plan':revised}
        with self.assertRaisesRegex(ControlError,'conversation_confirmed_mapping_immutable'):
            self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock)

    def test_short_acknowledgement_needs_no_fake_topic(self):
        self.ingest()
        dm={'action':'reply','message_ids':['m1'],'group_id':'g','parts':['응, 다녀와'],
            'due_ms':self.clock,'reason':'closing',
            'conversation_plan':{'version':1,'units':[],
                'non_substantive':[{'mode':'close','spans':[{'part_index':0,'excerpt':'응, 다녀와'}]}]}}
        self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock)
        self.assertEqual(self.uses(),[])

    def test_uncertain_mapping_stays_immutable_until_actual_receipt(self):
        unit, _ = self.register(); self.ingest()
        plan = self.plan(unit,'소재')
        plan['non_substantive']=[{'mode':'acknowledge','spans':[{'part_index':1,'excerpt':'응'}]}]
        dm={'action':'reply','message_ids':['m1'],'group_id':'g1','parts':['소재','응'],
            'due_ms':self.clock,'reason':'r','conversation_plan':plan}
        self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock)
        self.send('unknown',limit=1)
        revised={'version':1,'units':[],'non_substantive':[
            {'mode':'acknowledge','spans':[{'part_index':0,'excerpt':'소재'},{'part_index':1,'excerpt':'응'}]}]}
        with self.assertRaisesRegex(ControlError,'conversation_confirmed_mapping_immutable'):
            self.store.apply('main',self.decision(dm_decisions=[{'action':'revalidate_group',
                'group_id':'g1','reason':'new review','conversation_plan':revised}]),self.clock)
        self.assertEqual(self.uses()[0]['status'],'reserved')

    def test_revalidation_ignores_own_reservation(self):
        unit, _ = self.register(); self.queue(unit)
        dm={'action':'revalidate_group','group_id':'g1','reason':'still fits',
            'conversation_plan':self.plan(unit)}
        self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock)
        self.assertEqual(len(self.uses()),1)

    def test_legacy_group_can_be_revalidated_after_enabling(self):
        flow.configure(self.store,'main',{'mode':'off'},self.clock)
        self.ingest()
        dm={'action':'reply','message_ids':['m1'],'group_id':'legacy','parts':['hello'],
            'due_ms':self.clock,'reason':'r'}
        self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock)
        flow.configure(self.store,'main',{'mode':'enforce'},self.clock)
        self.store.apply('main',self.decision(dm_decisions=[{
            'action':'revalidate_group','group_id':'legacy','reason':'old queue'}]),self.clock)

    def test_context_pagination_does_not_limit_guards(self):
        unit, _ = self.register(); self.queue(unit); self.send()
        page=flow.context(self.store,'main',self.clock,limit=1)
        self.assertEqual(len(page['uses']),1)
        self.assertFalse(page['has_more'])
        self.clock += 49*flow.HOUR
        page=flow.context(self.store,'main',self.clock)
        self.assertEqual(page['uses'],[])
        with self.assertRaisesRegex(ControlError,'conversation_material_already_shared'):
            self.queue(unit,'g2','m2')

    def test_preflight_cli_returns_failure_without_queueing(self):
        unit, _ = self.register(); self.queue(unit); self.send(); self.ingest('m2')
        dm={'action':'reply','message_ids':['m2'],'group_id':'g2','parts':['새 이야기'],
            'due_ms':self.clock,'reason':'r','conversation_plan':self.plan(unit)}
        d=self.decision(dm_decisions=[dm]); d['next_decision_ms']=9999999999999
        # CLI uses wall time; the transaction still checks identity before scheduling.
        result=subprocess.run([sys.executable,str(Path(baseline.ROOT)/'ctl.py'),'--db',str(self.path),
            'conversation-preflight','--decision',json.dumps(d)],capture_output=True,text=True)
        self.assertEqual(json.loads(result.stdout)['reason'],'conversation_material_already_shared')
        self.assertEqual(len(self.uses()),1)

    def test_documented_fragment_is_an_executable_normal_conversation_contract(self):
        fragment=json.loads((Path(baseline.ROOT)/'examples/conversation-flow.example.json').read_text())
        self.ingest()
        dm={'action':'reply','message_ids':['m1'],'group_id':'example','parts':fragment['parts'],
            'due_ms':self.clock,'reason':'example contract','conversation_plan':fragment['conversation_plan']}
        result=self.store.apply('main',self.decision(facts=fragment['facts'],
            conversation_updates=fragment['conversation_updates'],dm_decisions=[dm]),self.clock)
        self.assertIn('opinion',result['conversation_refs'])
        self.assertEqual(result['conversation_groups']['example']['warnings'],[])


if __name__ == '__main__':
    unittest.main()
