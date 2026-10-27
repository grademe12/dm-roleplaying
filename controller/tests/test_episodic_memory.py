"""Real SQLite pipeline and fake model transport; no production data or provider calls."""
import json
from contextlib import redirect_stdout
import hashlib
import io
from unittest.mock import patch
from pathlib import Path
import unittest
from common import ControlError, encode
import episodic_memory as mem
import memory_organizer as worker
import prepared_inputs
import gate
import reply_generator
import test_decision_session as lifecycle


class EpisodicMemoryTests(unittest.TestCase):
    tearDown=lifecycle.DecisionSessionTests.tearDown
    snapshot=lifecycle.DecisionSessionTests.snapshot
    decision=lifecycle.DecisionSessionTests.decision
    review=lifecycle.DecisionSessionTests.review
    ingest=lifecycle.DecisionSessionTests.ingest
    activity=lifecycle.DecisionSessionTests.activity
    free=lifecycle.DecisionSessionTests.free
    sleep=lifecycle.DecisionSessionTests.sleep
    recent=lifecycle.DecisionSessionTests.recent
    draft=lifecycle.DecisionSessionTests.draft

    def setUp(self):
        lifecycle.DecisionSessionTests.setUp(self)
        self.cfg['episodic_memory']={'mode':'enforce','batch_size':100}

    def packet(self):return mem.prepare(self.cfg,self.clock)['packet']

    def response(self,p,items=()):
        return {k:p[k] for k in ('schema','run_id','input_sha256','through_seq')}|{'memories':list(items)}

    def item(self,p,key='event:duo',**changes):
        s=p['new_sources'][0]
        return {'key':key,'kind':'episode','category':'shared_experience','status':'completed','importance':4,
                'summary':'함께 게임하며 목표 등급에 도달했다는 대화','keywords':['듀오','승급','같이 게임'],
                'interpretation':'확인된 DM 내용, 외부 게임 검증은 아님',
                'evidence':[{'seq':s['seq'],'excerpt':s['text'][:80]}],'supersedes':None}|changes

    def remember(self):
        self.ingest(text='같이 목표 등급 찍었네');p=self.packet()
        response=self.response(p,[self.item(p)])
        mem.complete(self.cfg,response,self.clock)
        return p,response

    def test_no_controller_writes_and_idempotent_replay(self):
        self.ingest();revision=self.snapshot()['revision'];p=self.packet();response=self.response(p,[self.item(p)])
        result=mem.complete(self.cfg,response,self.clock)
        self.assertEqual(mem.complete(self.cfg,response,self.clock),result|{'replayed':True})
        self.assertEqual(self.snapshot()['revision'],revision)
        self.assertEqual(mem.prepare(self.cfg,self.clock)['reason'],'no_work')
        self.assertEqual(mem.settings(self.cfg)['path'].stat().st_mode&0o777,0o600)

    def test_invalid_batch_rolls_back_memories_and_checkpoint(self):
        self.ingest();p=self.packet()
        bad=self.item(p,key='bad',evidence=[{'seq':p['through_seq'],'excerpt':'not in source'}])
        with self.assertRaisesRegex(ControlError,'memory_evidence_mismatch'):
            mem.complete(self.cfg,self.response(p,[self.item(p),bad]),self.clock)
        with mem.connection(self.cfg) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM memories').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT seq FROM positions').fetchone()[0],0)
        retry=self.packet()
        self.assertTrue(mem.complete(self.cfg,self.response(retry,[self.item(retry)]),self.clock)['ok'])

    def test_off_ignores_unused_invalid_configuration_in_real_prepare(self):
        self.ingest();self.recent()
        for opts in (None, {'mode':'off','batch_size':0,'path':None}, {'batch_size':0}):
            self.cfg['episodic_memory']=opts
            self.assertEqual(mem.prepare(self.cfg,self.clock)['reason'],'off')
            self.assertNotIn('episodic_memory',self.runner.prepare(mode='bundled')['snapshot'])
        config=Path(self.tmp.name)/'off.json';config.write_text(json.dumps(self.cfg))
        self.assertEqual(worker.run(worker.load_config(config))['reason'],'off')
        self.assertFalse((self.state_dir/'episodic-memory.sqlite3').exists())
        self.cfg['episodic_memory']={'mode':'enforce','batch_size':0}
        with self.assertRaises(ControlError):mem.settings(self.cfg)

    def test_omitted_key_collision_is_read_on_retry_without_accepting_unread_version(self):
        self.remember();self.ingest('m2',text='다른 표현으로 후속 결과')
        # Simulate the real packet's bounded search omitting an old predecessor.
        with patch.object(mem,'lookup_rows',return_value=[]):
            for i in range(13):
                self.clock+=1000
                p=self.packet()
                mem.complete(self.cfg,self.response(p,[self.item(p,key='other:'+str(i),summary='다른 주제',keywords=['다른'])]),self.clock)
                self.ingest('later-'+str(i),text='새 주제')
            p=self.packet()
            self.assertNotIn('event:duo',[m['key'] for m in p['existing_memories']])
            with self.assertRaisesRegex(ControlError,'memory_version_changed'):
                mem.complete(self.cfg,self.response(p,[self.item(p)]),self.clock)
            retry=self.packet()
        previous=next(m for m in retry['existing_memories'] if m['key']=='event:duo')
        self.assertTrue(mem.complete(self.cfg,self.response(retry,[self.item(retry,supersedes=previous['id'])]),self.clock)['ok'])

    def test_failed_batch_splits_quarantines_and_manual_retry_preserves_progress(self):
        for i in range(4):self.ingest('batch-'+str(i),text='항목 '+str(i))
        lengths=[]
        for attempt in range(5):
            p=self.packet();lengths.append(len(p['new_sources']))
            mem.fail(self.cfg,p['run_id'],self.clock,'invalid_memory_response')
            # Repeated failure delivery must not increment the same lease twice.
            self.assertFalse(mem.fail(self.cfg,p['run_id'],self.clock,'invalid_memory_response'))
        self.assertEqual(lengths,[4,2,1,1,1])
        first=p['new_sources'][0]['seq']
        later=self.packet();self.assertNotEqual(later['new_sources'][0]['seq'],first)
        mem.complete(self.cfg,self.response(later),self.clock)
        result=mem.recall(self.cfg,'instagram','항목',self.clock)
        self.assertEqual(result['quarantined_count'],1);self.assertTrue(result['incomplete'])
        while True:
            remaining=mem.prepare(self.cfg,self.clock)
            if remaining['reason']=='needs_attention': break
            self.assertEqual(len(remaining['packet']['new_sources']),1)
            mem.complete(self.cfg,self.response(remaining['packet']),self.clock)
        mem.retry_quarantined(self.cfg,first,self.clock)
        retry=self.packet();self.assertEqual(retry['through_seq'],first)
        mem.complete(self.cfg,self.response(retry,[self.item(retry)]),self.clock)
        self.assertEqual(mem.recall(self.cfg,'instagram','항목',self.clock)['quarantined_count'],0)
        self.assertEqual(mem.prepare(self.cfg,self.clock)['reason'],'no_work')
        with mem.connection(self.cfg) as db:
            self.assertGreater(db.execute('SELECT seq FROM positions').fetchone()[0],first)

    def test_oversize_source_is_visible_gap_and_does_not_block_later_message(self):
        self.cfg['episodic_memory']['batch_bytes']=4096
        self.ingest('huge',text='가'*3000);self.ingest('small',text='다음 메시지')
        p=self.packet();self.assertEqual([s['source_id'] for s in p['new_sources']],['small'])
        mem.complete(self.cfg,self.response(p),self.clock)
        result=mem.recall(self.cfg,'instagram','메시지',self.clock)
        self.assertTrue(result['incomplete']);self.assertEqual(result['quarantined_count'],1)
        self.assertEqual(result['quarantined'][0]['reason'],'memory_source_exceeds_batch_budget')
        self.cfg['episodic_memory']['batch_bytes']=49152
        mem.retry_quarantined(self.cfg,result['quarantined'][0]['seq'],self.clock)
        p=self.packet();self.assertEqual(p['new_sources'][0]['source_id'],'huge')
        mem.complete(self.cfg,self.response(p),self.clock)
        self.assertEqual(mem.recall(self.cfg,'instagram','메시지',self.clock)['quarantined_count'],0)

    def test_unsupported_channel_cannot_starve_supported_channel(self):
        for i in range(205):self.ingest('unsupported-'+str(i),observation={'identity':'platform','channel':'other'})
        self.ingest('supported',text='정상 메시지')
        p=self.packet();self.assertEqual([s['source_id'] for s in p['new_sources']],['supported'])

    def test_native_truncated_output_is_failed_once_and_lock_is_private(self):
        self.ingest();p=self.packet()
        config=Path(self.tmp.name)/'automation.json';config.write_text(json.dumps(self.cfg))
        response_file=Path(self.tmp.name)/'response.json';response_file.write_text('{')
        args=['--config',str(config),'complete','--run-id',p['run_id'],'--response','@'+str(response_file)]
        with patch.object(worker,'now_ms',return_value=self.clock),redirect_stdout(io.StringIO()):
            self.assertEqual(worker.main(args),1);self.assertEqual(worker.main(args),1)
        with mem.connection(self.cfg) as db:
            self.assertEqual(db.execute('SELECT attempts FROM failures').fetchone()[0],1)
        locks=list(mem.settings(self.cfg)['path'].parent.glob('*.lock'))
        self.assertTrue(locks);self.assertTrue(all(p.stat().st_mode&0o777==0o600 for p in locks))
        self.assertNotEqual(self.packet()['run_id'],p['run_id'])

    def test_repeated_expiration_is_not_quarantined_and_other_channel_runs(self):
        self.ingest()
        for _ in range(3):
            p=self.packet();self.clock+=601000
            with self.assertRaisesRegex(ControlError,'memory_run_expired'):
                mem.complete(self.cfg,self.response(p),self.clock)
            retry=mem.prepare(self.cfg,self.clock)
            self.clock+=retry['retry_after_ms']
        self.clock-=1
        self.ingest('slack:1',observation={'identity':'platform','channel':'slack','slack_ts':'1'})
        p=self.packet();self.assertEqual(p['channel'],'slack')
        mem.complete(self.cfg,self.response(p),self.clock)
        self.assertEqual(mem.recall(self.cfg,'instagram','기억',self.clock)['quarantined_count'],0)

    def test_empty_extraction_and_expired_work(self):
        self.ingest();p=self.packet()
        self.assertEqual(mem.prepare(self.cfg,self.clock)['reason'],'busy');self.clock+=601000
        with self.assertRaisesRegex(ControlError,'memory_run_expired'):mem.complete(self.cfg,self.response(p),self.clock)
        waiting=mem.prepare(self.cfg,self.clock)
        self.assertEqual(waiting['reason'],'retry_later');self.clock+=waiting['retry_after_ms']
        retry=self.packet();self.assertNotEqual(retry['run_id'],p['run_id'])
        self.assertEqual(retry['through_seq'],p['through_seq'])
        mem.complete(self.cfg,self.response(retry),self.clock)
        self.assertEqual(mem.prepare(self.cfg,self.clock)['reason'],'no_work')

    def test_unknown_not_spoken_and_late_confirmation_captured(self):
        self.ingest();self.store.apply('main',self.draft('완료한 게임 기억나'),self.clock)
        p=self.packet();self.assertTrue(all(s['direction']=='incoming' for s in p['new_sources']))
        mem.complete(self.cfg,self.response(p),self.clock)
        with self.store.connect(True) as db:db.execute("UPDATE dm_jobs SET status='unknown'")
        self.assertEqual(mem.prepare(self.cfg,self.clock)['reason'],'no_work');self.clock+=10000
        with self.store.connect(True) as db:
            db.execute("UPDATE dm_jobs SET status='confirmed',provider_message_id='sent-1',updated_ms=?",(self.clock,))
        p=self.packet();self.assertEqual([s['text'] for s in p['new_sources']],['완료한 게임 기억나'])
        self.assertEqual(p['new_sources'][0]['direction'],'outgoing')

    def test_echo_deduplicated_and_unverified_excluded(self):
        self.ingest();self.store.apply('main',self.draft('함께 승급했다'),self.clock)
        with self.store.connect(True) as db:db.execute("UPDATE dm_jobs SET status='confirmed',provider_message_id='echo-1'")
        self.ingest('echo-1',direction='outgoing',text='함께 승급했다')
        self.ingest('uncertain',observation={'identity':'uncertain'})
        p=self.packet();self.assertEqual(sum(s['direction']=='outgoing' for s in p['new_sources']),1)
        self.assertNotIn('uncertain',[s['source_id'] for s in p['new_sources']])

    def test_query_keeps_user_topic_ahead_of_own_long_reply(self):
        own=('너무 예쁘다는 말 그렇게 대놓고 하면 나 진짜 기분 좋아서 어쩔 줄 모르겠네 ㅋㅋㅋ 회사 화장실에서 '
             '그러고 있는 거 생각하니까 나도 괜히 뜨거워지잖아 처음부터 끝까지 천천히 제대로 봐 끊지 말고 계속 말해줘')
        messages=[{'direction':'incoming','text':'어제 겐지 장인 영상 봤어?'},
                  {'direction':'outgoing','text':own},{'direction':'incoming','text':'듀오 또 할래?'}]
        query=mem.build_query(['듀오 또 할래?'],messages)
        self.assertEqual(query[:2],[('듀오 또 할래?',mem.TARGET_WEIGHT),('어제 겐지 장인 영상 봤어?',mem.USER_WEIGHT)])
        self.assertEqual(query[2][1],mem.OWN_WEIGHT)
        terms=mem.query_terms(query)
        self.assertLess(terms.index('듀오'),terms.index('겐지'))
        self.assertTrue({'너무','진짜','그렇게','ㅋㅋㅋ','괜히'}.isdisjoint(terms))
        self.assertLessEqual(len(terms),mem.QUERY_TERMS)
        self.assertEqual(mem.build_query(['듀오'],[]),[('듀오',mem.TARGET_WEIGHT)])

    def test_own_reply_words_do_not_outrank_user_topic(self):
        self.ingest(text='같이 목표 등급 찍었네');p=self.packet()
        noisy=self.item(p,key='daily:noise',category='daily',importance=4,
                        summary='회사 화장실 생각 뜨거워 처음부터 끝까지 천천히 제대로 끊지 계속 말해줘',
                        keywords=['회사','화장실','처음부터'])
        mem.complete(self.cfg,self.response(p,[self.item(p),noisy]),self.clock)
        own='회사 화장실에서 생각하니까 뜨거워지잖아 처음부터 끝까지 천천히 제대로 봐 끊지 말고 계속 말해줘'
        query=mem.build_query(['듀오 승급 또 할래?'],[{'direction':'outgoing','text':own},
                                                    {'direction':'incoming','text':'듀오 승급 또 할래?'}])
        result=mem.recall(self.cfg,'instagram',query,self.clock)
        self.assertEqual(result['items'][0]['key'],'event:duo')

    def test_particles_are_stripped_and_substrings_count_less(self):
        terms=dict(mem.weighted_terms('아침은 다리가 화장실에서'))
        self.assertEqual((terms['아침'],terms['다리'],terms['화장실']),(1.0,1.0,1.0))
        self.assertEqual(terms['장실에'],mem.SUBSTRING_WEIGHT)
        self.assertIsNone(mem.stem('다리'))

    def test_distinctive_term_beats_common_term_and_importance(self):
        self.ingest(text='같이 목표 등급 찍었네');p=self.packet()
        common=[self.item(p,key='daily:'+str(i),category='daily',importance=5,
                          summary='어제 만나고 나서 계속 생각났다는 이야기 '+str(i),keywords=['어제']) for i in range(4)]
        rare=self.item(p,key='daily:breakfast',category='daily',importance=1,
                       summary='사용자는 아침을 원래 안 먹는다',keywords=['아침안먹음'])
        mem.complete(self.cfg,self.response(p,[*common,rare]),self.clock)
        result=mem.recall(self.cfg,'instagram','어제 아침은 패스함',self.clock)
        self.assertEqual(result['items'][0]['key'],'daily:breakfast')

    def test_recency_breaks_ties_with_kind_half_life(self):
        row={'kind':'episode','status':'completed','occurred_ms':0}
        self.assertAlmostEqual(mem.recency(row,72*3600000),mem.RECENCY_WEIGHT/2)
        self.assertAlmostEqual(mem.recency(row|{'status':'ongoing'},168*3600000),mem.RECENCY_WEIGHT/2)
        self.assertAlmostEqual(mem.recency(row|{'kind':'fact'},720*3600000),mem.RECENCY_WEIGHT/2)
        self.assertEqual(mem.recency(row|{'kind':'working'},0),0)

    def test_capture_staleness_follows_organizer_interval(self):
        self.remember()
        self.assertFalse(mem.recall(self.cfg,'instagram','듀오',self.clock+120000)['incomplete'])
        self.assertTrue(mem.recall(self.cfg,'instagram','듀오',self.clock+661000)['incomplete'])
        self.cfg['episodic_memory']['capture_stale_seconds']=60
        self.assertTrue(mem.recall(self.cfg,'instagram','듀오',self.clock+61000)['incomplete'])
        self.cfg['episodic_memory']['capture_stale_seconds']=10
        self.assertEqual(mem.reply_settings(self.cfg)['diagnostic'],'invalid_capture_stale_seconds')

    def test_three_hours_and_310_messages_do_not_erase_episode(self):
        self.remember();self.clock+=3*3600000
        for i in range(310):self.ingest('noise-'+str(i),direction='outgoing',text='점심 메뉴 이야기 '+str(i))
        while True:
            value=mem.prepare(self.cfg,self.clock)
            if value['reason']=='no_work':break
            mem.complete(self.cfg,self.response(value['packet']),self.clock)
        result=mem.recall(self.cfg,'instagram','아까 듀오 또 할래?',self.clock)
        self.assertEqual(result['items'][0]['key'],'event:duo')
        self.assertEqual(result['items'][0]['evidence'][0]['source_id'],'m1')
        self.assertFalse(mem.recall(self.cfg,'slack','듀오',self.clock)['items'])
        self.assertFalse(mem.recall(self.cfg,'instagram','듀오',self.clock,['appointment'])['items'])
        question='아까 듀오 또 할래?'
        self.ingest('latest',text=question)
        gate.write_json(self.state_dir/'recent_thread.json',{'fetched_ms':self.clock,'messages':[
            *[{'id':'noise-'+str(i),'direction':'outgoing','text':'점심 메뉴 이야기 '+str(i)} for i in range(281,310)],
            {'id':'latest','direction':'incoming','text':question}]})
        prepared=self.runner.prepare(mode='compact')
        bundle=prepared_inputs.read_bundle(self.runner,prepared['turn_id'],prepared['input_id'])
        request={'schema':'reply-gen-v1','turn_id':prepared['turn_id'],'channel':'instagram',
                 'situation':'지난 공동 경험을 이어 이야기한다','target_message_ids':['latest']}
        dynamic,_=reply_generator.source(self.runner,self.runner.state(prepared['turn_id']),request,bundle['input_receipts'])
        self.assertEqual(len(dynamic['history']),30)
        self.assertNotIn('m1',[m['id'] for m in dynamic['history']])
        self.assertEqual(dynamic['episodic_memory']['items'][0]['evidence'][0]['source_id'],'m1')

    def test_shadow_off_and_wrong_persona(self):
        self.remember();self.cfg['episodic_memory']['mode']='shadow'
        result=mem.recall(self.cfg,'instagram','듀오',self.clock)
        self.assertEqual(result['items'],[]);self.assertEqual(result['candidate_count'],1)
        self.cfg['episodic_memory']['mode']='off'
        self.assertEqual(mem.recall(self.cfg,'instagram','듀오',self.clock)['items'],[])
        self.cfg['episodic_memory']['mode']='enforce';self.cfg['persona_id']='other-persona'
        self.assertEqual(mem.recall(self.cfg,'instagram','듀오',self.clock)['reason'],'memory_store_unavailable')

    def test_retraction_preserves_history_and_invalidates_selected(self):
        self.remember();snapshot={'instagram':mem.recall(self.cfg,'instagram','듀오',self.clock)}
        previous=snapshot['instagram']['items'][0]['id'];self.ingest('m2',text='그 게임 결과는 착각했어');p=self.packet()
        mem.complete(self.cfg,self.response(p,[self.item(p,status='retracted',supersedes=previous)]),self.clock)
        self.assertFalse(mem.recall(self.cfg,'instagram','듀오',self.clock)['items'])
        with self.assertRaisesRegex(ControlError,'memory_inputs_changed'):mem.validate(self.cfg,snapshot)
        with mem.connection(self.cfg) as db:self.assertEqual(db.execute('SELECT count(*) FROM memories').fetchone()[0],2)

    def test_unrelated_new_memory_does_not_invalidate_reply(self):
        self.remember();snapshot={'instagram':mem.recall(self.cfg,'instagram','듀오',self.clock)}
        self.ingest('m2',text='내일 점심 약속');p=self.packet()
        mem.complete(self.cfg,self.response(p,[self.item(p,key='appointment:lunch',category='appointment',status='planned')]),self.clock)
        mem.validate(self.cfg,snapshot)

    def test_bundle_generator_and_receipts_carry_same_evidence(self):
        self.remember();self.recent();p=self.runner.prepare(mode='compact')
        bundle=prepared_inputs.read_bundle(self.runner,p['turn_id'],p['input_id'])
        state=next(c['content'] for c in bundle['components'] if c['kind']=='state')
        selected=state['episodic_memory']['instagram']['items'];self.assertEqual(selected[0]['key'],'event:duo')
        rules=Path(self.tmp.name)/'rules.md';rules.write_text('test rules')
        self.cfg['reply_generator']={'enabled':True,'endpoint':'https://llm.example.test/v1/chat/completions','rules_file':str(rules)}
        request={'schema':'reply-gen-v1','turn_id':p['turn_id'],'channel':'instagram','situation':'이전 게임 이야기',
                 'target_message_ids':['m1']}
        calls=[]
        def transport(body,*args):
            calls.append(body)
            return {'choices':[{'finish_reason':'stop','message':{'content':encode({'parts':['그때 같이 했던 거 기억나']})}}]}
        result=reply_generator.generate(self.runner,p['turn_id'],request,bundle['input_receipts'],transport)
        self.assertTrue(result['ok'])
        self.assertEqual(json.loads(calls[0]['messages'][1]['content'])['episodic_memory']['items'],selected)
        context=prepared_inputs.read(self.runner,p['turn_id'],p['input_id'],'context',channel='instagram')
        with self.assertRaisesRegex(ControlError,'episodic_memory_input_required'):
            reply_generator.generate(self.runner,p['turn_id'],request,[context['receipt']],transport)

    def test_worker_failure_keeps_checkpoint_then_retries(self):
        self.ingest();self.cfg['episodic_memory'].update(model='configured-model',endpoint='https://llm.example.test/v1/chat/completions',key_file='unused-by-mock')
        def broken(*args):return {'choices':[{'finish_reason':'length','message':{'content':'{}'}}]}
        self.assertEqual(worker.run(self.cfg,lambda:self.clock,broken)['reason'],'memory_model_incomplete')
        def valid(body,*args):
            p=json.loads(body['messages'][1]['content'])
            return {'choices':[{'finish_reason':'stop','message':{'content':encode(self.response(p,[self.item(p)]))}}]}
        self.assertTrue(worker.run(self.cfg,lambda:self.clock,valid)['ok'])

    def test_missing_store_degrades_and_default_off_creates_nothing(self):
        self.assertTrue(mem.recall(self.cfg,'instagram','듀오',self.clock)['incomplete'])
        path=mem.settings(self.cfg)['path'];self.assertFalse(path.exists());self.cfg.pop('episodic_memory')
        self.assertEqual(mem.prepare(self.cfg,self.clock)['reason'],'off');self.assertFalse(path.exists())

    def test_source_database_bytes_preserved_and_peer_change_blocks_old_memory(self):
        self.ingest();before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        p=self.packet();mem.complete(self.cfg,self.response(p,[self.item(p)]),self.clock)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)
        old={'instagram':mem.recall(self.cfg,'instagram','듀오',self.clock)}
        with self.store.connect(True) as db:db.execute("UPDATE sessions SET recipient='different-peer' WHERE id='main'")
        self.assertEqual(mem.recall(self.cfg,'instagram','듀오',self.clock)['reason'],'memory_store_unavailable')
        with self.assertRaisesRegex(ControlError,'memory_inputs_changed'):mem.validate(self.cfg,old)

    def test_native_file_handoff_cli_keeps_source_out_of_stdout(self):
        self.ingest(text='private source marker')
        config=Path(self.tmp.name)/'automation.json';config.write_text(json.dumps(self.cfg))
        packet_file=Path(self.tmp.name)/'packet.json';response_file=Path(self.tmp.name)/'response.json'
        output=io.StringIO()
        with patch.object(worker,'now_ms',return_value=self.clock),redirect_stdout(output):
            self.assertEqual(worker.main(['--config',str(config),'prepare','--output',str(packet_file)]),0)
        self.assertNotIn('private source marker',output.getvalue())
        p=json.loads(packet_file.read_text());self.assertEqual(packet_file.stat().st_mode&0o777,0o600)
        worker.private_write(response_file,self.response(p,[self.item(p)]))
        with patch.object(worker,'now_ms',return_value=self.clock),redirect_stdout(io.StringIO()):
            self.assertEqual(worker.main(['--config',str(config),'complete','--response','@'+str(response_file)]),0)
        self.assertEqual(mem.recall(self.cfg,'instagram','듀오',self.clock)['items'][0]['key'],'event:duo')

    def test_commit_rejects_corrected_selected_memory_before_sending(self):
        self.remember();self.recent();prepared=self.runner.prepare(mode='bundled')
        draft=self.draft()
        previous=prepared['snapshot']['episodic_memory']['instagram']['items'][0]['id']
        self.ingest('m2',text='게임 결과 정정');p=self.packet()
        mem.complete(self.cfg,self.response(p,[self.item(p,supersedes=previous,status='retracted')]),self.clock)
        with self.assertRaisesRegex(ControlError,'memory_inputs_changed'):
            self.runner.commit(prepared['turn_id'],draft)
        self.assertEqual(self.snapshot()['jobs'],[])

    def test_old_related_memory_is_available_to_organizer_after_many_new_topics(self):
        self.remember()
        for i in range(30):
            self.ingest('other-'+str(i),direction='outgoing',text='점심 메뉴 이야기 '+str(i));p=self.packet()
            mem.complete(self.cfg,self.response(p,[self.item(p,key='other:'+str(i),summary='점심 이야기',keywords=['점심'])]),self.clock)
        self.ingest('latest',text='예전 듀오 승급 이야기');p=self.packet()
        self.assertIn('event:duo',[m['key'] for m in p['existing_memories']])

    def test_provider_echo_id_is_not_falsely_reported_as_unprocessed(self):
        self.ingest();self.store.apply('main',self.draft('함께 승급했다'),self.clock)
        with self.store.connect(True) as db:db.execute("UPDATE dm_jobs SET status='confirmed',provider_message_id='echo-1'")
        p=self.packet()
        self.assertEqual(mem.recall(self.cfg,'instagram','게임',self.clock,recent_ids=['echo-1'])['recent_unprocessed_ids'],['echo-1'])
        mem.complete(self.cfg,self.response(p),self.clock)
        self.assertEqual(mem.recall(self.cfg,'instagram','게임',self.clock,recent_ids=['echo-1'])['recent_unprocessed_ids'],[])

    def test_budget_preserves_whole_memory(self):
        self.remember();self.cfg['episodic_memory']['context_bytes']=1024
        value=mem.recall(self.cfg,'instagram','듀오',self.clock)
        self.assertLessEqual(len(encode(value['items']).encode()),1024)
        for item in value['items']:self.assertTrue(item['evidence'])

if __name__=='__main__':unittest.main()
