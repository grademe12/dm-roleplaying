"""Real SQLite checks for bounded inputs and explicit read acknowledgements."""
import copy
import hashlib
import json
from pathlib import Path
from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import patch

from common import ControlError, encode
import gate
import prepared_inputs as inputs
import decision_session
import test_decision_session as session_tests


class PreparedInputTests(unittest.TestCase):
    def stage_producer_candidate(self):
        import topic_producer
        run=topic_producer.claim(self.store,'main',self.clock)
        fact={'id':'producer-news','source':'observed','summary':'ref:producer-news',
              'details':{'evidence':'mock verified source','url':'https://example.test/producer',
                         'publisher':'Mock','body_verified':True,'checked_ms':self.clock,
                         'published_ms':None,'event_ms':None,'point_keys':['announcement']}}
        material={'canonical_key':'producer:event','subject_key':run['task']['subject_key'],
                  'kind':'news','fact_ids':[fact['id']],'checked_ms':self.clock,
                  'refresh_after_ms':self.clock+3600000}
        topic_producer.result(self.store,'main',{'run_id':run['run_id'],'token':run['token'],
            'status':'success','facts':[fact],'materials':[material]},self.clock)

    def test_parallel_producer_staging_does_not_invalidate_reply_receipts(self):
        import conversation_flow
        conversation_flow.configure(self.store,'main',{'search_enabled':True,'search_tool_verified':True},self.clock)
        prepared=self.prepare()
        self.stage_producer_candidate()
        self.assertTrue(self.commit(prepared,self.required(prepared))['committed'])

    def test_prepare_publishes_inbox_before_snapshot(self):
        import conversation_flow
        import topic_materials
        conversation_flow.configure(self.store,'main',{'search_enabled':True,'search_tool_verified':True},self.clock)
        self.stage_producer_candidate()
        before=self.snapshot()['revision']
        prepared=self.prepare()
        self.assertGreater(prepared['snapshot']['revision'],before)
        self.assertEqual(len(topic_materials.materials(self.store,'main',self.clock)['materials']),1)
        self.assertTrue(self.commit(prepared,self.required(prepared))['committed'])

    def test_broken_producer_inbox_does_not_block_prepare_or_reply(self):
        import topic_producer
        topic_producer.inbox_path(self.store.path).write_bytes(b'broken optional inbox')
        prepared=self.prepare()
        self.assertTrue(self.commit(prepared,self.required(prepared))['committed'])

    def large_topic_history(self):
        facts=[{'id':f'large-topic-{i:03}', 'source':'simulated',
                'summary':'topic_state:diary neutral byte-budget fixture',
                'details':{'evidence':'synthetic load test, no private content','notes':'x'*4000}}
               for i in range(329)]
        self.store.apply('main',self.decision(facts=facts[:-1]),self.clock)
        self.clock+=1
        self.store.apply('main',self.decision(facts=facts[-1:]),self.clock)
        self.ingest();self.recent()
        return ['topic_state:diary']

    def test_large_topic_history_keeps_default_breadth_and_explicit_pages(self):
        query=self.large_topic_history()
        self.assertGreater(inputs.size(self.store.recall('main',query)),inputs.COMPONENT_BYTES)
        auto=self.runner.prepare(queries=[query])
        count=(auto['input_manifest']['recalls'][0]['result_count'] if auto['input_mode']=='compact'
               else len(auto['recalls'][0]['facts']))
        self.assertEqual(count,10)
        prepared=self.runner.prepare(queries=[query],mode='compact')
        first=self.read(prepared,'recall',index=0)
        self.assertEqual(len(first['content']['facts']),10)
        self.assertEqual(first['content']['facts'][0]['id'],'large-topic-328')
        self.assertTrue(first['content']['has_more'])
        self.assertGreater(inputs.size(first),inputs.COMPONENT_BYTES)
        self.assertTrue(first['input_advisory']['budget_exceeded'])
        second=self.read(prepared,'recall',keywords=query,offset=first['content']['next_offset'])
        self.assertNotEqual(first['content']['facts'][0]['id'],second['content']['facts'][0]['id'])
        last=self.read(prepared,'recall',keywords=query,offset=328)
        self.assertFalse(last['content']['has_more'])
        self.assertIsNone(last['content']['next_offset'])

    def test_large_mixed_recall_and_explicit_pages_can_commit_without_size_failure(self):
        query=self.large_topic_history()
        prepared=self.runner.prepare(mode='compact')
        mixed=self.read(prepared,'recall',keywords=[*query,'neutral'])
        self.assertEqual(len(mixed['content']['facts']),10)
        self.assertTrue(mixed['input_advisory']['budget_exceeded'])
        self.assertTrue(all(f['details']['notes']=='x'*4000 for f in mixed['content']['facts']))
        one=self.read(prepared,'recall',keywords=query,limit=2)
        two=self.read(prepared,'recall',keywords=query,limit=2,offset=2)
        self.assertEqual(len(one['content']['facts']),2)
        self.assertFalse({f['id'] for f in one['content']['facts']} &
                         {f['id'] for f in two['content']['facts']})
        receipts=self.required(prepared)+[mixed['receipt'],one['receipt'],two['receipt']]
        self.assertTrue(self.commit(prepared,receipts)['committed'])
        self.assertEqual(inputs.COMPONENT_BYTES,32768)

    def test_recall_cli_accepts_pages_and_rejects_invalid_selectors(self):
        query=self.large_topic_history()
        prepared=self.runner.prepare(mode='compact')
        config=Path(self.tmp.name)/'automation.json';config.write_text(json.dumps(self.cfg))
        args=['--config',str(config),'--wake-id','wake-1','read-recall',
              '--turn-id',prepared['turn_id'],'--input-id',prepared['input_id'],
              '--keywords',*query,'--limit','2','--offset','2']
        output=io.StringIO()
        with redirect_stdout(output),patch.object(decision_session,'DecisionSession',return_value=self.runner):
            code=decision_session.main(args)
        self.assertEqual(code,0,output.getvalue())
        page=json.loads(output.getvalue())
        self.assertEqual(page['content']['offset'],2)
        self.assertEqual(len(page['content']['facts']),2)
        for options in ({'limit':0},{'limit':11},{'offset':-1}):
            with self.subTest(options=options),self.assertRaises(ControlError):
                self.read(prepared,'recall',keywords=query,**options)

    def test_memory_recall_honors_explicit_page_size(self):
        import memory_continuity
        memory_continuity.configure(self.store,'main',{'mode':'enforce'},self.clock)
        query=self.large_topic_history()
        prepared=self.runner.prepare(mode='compact')
        page=self.read(prepared,'recall',keywords=query,memory=True,limit=2,offset=2)
        self.assertEqual(len(page['content']['facts']),2)
        self.assertEqual(page['content']['offset'],2)
        self.assertLessEqual(inputs.size(page),inputs.COMPONENT_BYTES)

    def test_real_api_document_larger_than_budget_is_read_unchanged(self):
        prepared=self.prepare()
        result=self.read(prepared,'document',name='API.md')
        actual=(Path(inputs.__file__).parent/'API.md').read_text(encoding='utf-8')
        self.assertGreater(len(actual.encode('utf-8')),inputs.COMPONENT_BYTES)
        self.assertEqual(result['content']['text'],actual)
        self.assertTrue(result['input_advisory']['budget_exceeded'])
        self.assertTrue(self.commit(prepared,self.required(prepared)+[result['receipt']])['committed'])

    def test_unused_old_cache_edit_does_not_restart_current_reply(self):
        prepared=self.prepare(('old-a','old-b','m1','a','b','c','d','e','f'),context_limit=6)
        self.assertNotIn('old-a',prepared['contexts']['instagram']['available_message_ids'])
        receipts=self.required(prepared,context=False)
        receipts.append(self.read(prepared,'context',channel='instagram',limit=30)['receipt'])
        path=self.state_dir/'recent_thread.json';data=gate.load_json(path,{})
        data['messages'][0]['text']='unrelated old cache edit';gate.write_json(path,data)
        self.read(prepared,'context',channel='instagram',limit=30)
        self.assertTrue(self.commit(prepared,receipts)['committed'])

    def test_default_recent_context_is_30_and_smaller_limit_still_applies(self):
        self.assertEqual(inputs.DEFAULT_CONTEXT_LIMIT,30)
        older=tuple('old-'+str(i) for i in range(39))
        prepared=self.prepare(older+('m1',))
        ids=prepared['contexts']['instagram']['available_message_ids']
        self.assertEqual(len(ids),30)
        self.assertEqual(ids,list(older[-29:])+['m1'])
        state=self.runner.state()
        self.assertEqual(state['input_catalog']['contexts']['instagram']['required_recent_ids'],ids)
        # read-context without --limit returns the whole default window in one page.
        context=self.read(prepared,'context',channel='instagram')
        self.assertEqual([m['id'] for m in context['content']['messages']],ids)
        self.assertTrue(self.commit(prepared,self.required(prepared,context=False)+[context['receipt']])['committed'])

    def test_explicit_smaller_context_limit_keeps_six(self):
        older=tuple('old-'+str(i) for i in range(39))
        prepared=self.prepare(older+('m1',),context_limit=6)
        self.assertEqual(prepared['contexts']['instagram']['available_message_ids'],list(older[-5:])+['m1'])
        for bad in (5,31):
            with self.assertRaisesRegex(ControlError,'invalid_context_limit'):
                self.runner.prepare(mode='compact',context_limit=bad)

    def test_thirty_long_messages_only_raise_advisory_and_can_commit(self):
        self.ingest()
        body='토요일 두 시에 학교 앞 역에서 보고 점심은 내가 살게, 그다음 연구실 가서 데이터 처리 프로그램 같이 짜자. '*6
        messages=[{'id':'old-'+str(i),'direction':'incoming' if i%2 else 'outgoing','text':body} for i in range(29)]
        messages.append({'id':'m1','direction':'incoming','text':'hello'})
        gate.write_json(self.state_dir/'recent_thread.json',{'fetched_ms':self.clock,'messages':messages})
        prepared=self.runner.prepare()
        self.assertEqual(len(prepared['contexts']['instagram'].get('available_message_ids')
                             or prepared['contexts']['instagram']['message_ids']),30)
        if prepared['input_mode']=='compact':
            bundle=inputs.read_bundle(self.runner,prepared['turn_id'],prepared['input_id'])
            pages=[c for c in bundle['components'] if c['kind']=='context']
            self.assertEqual(sum(len(c['content']['messages']) for c in pages),30)
            receipts=bundle['input_receipts']
        else:
            receipts=None
        with patch.object(self.runner,'finish',return_value={'next':'done'}):
            result=self.runner.commit(prepared['turn_id'],self.draft(),input_receipts=receipts)
        self.assertTrue(result['committed'])

    def test_cli_context_limit_defaults_to_30(self):
        captured={}
        def fake(self_,queries,known,context_limit,*rest):
            captured['limit']=context_limit
            return {'ok':True,'reason':'prepared'}
        config=Path(self.tmp.name)/'automation.json'
        config.write_text(json.dumps({**self.cfg}))
        with patch.object(decision_session.DecisionSession,'prepare',fake), redirect_stdout(io.StringIO()):
            decision_session.main(['--config',str(config),'--wake-id','wake-1','prepare'])
        self.assertEqual(captured['limit'],30)
        with patch.object(decision_session.DecisionSession,'prepare',fake), redirect_stdout(io.StringIO()):
            decision_session.main(['--config',str(config),'--wake-id','wake-1','prepare','--context-limit','12'])
        self.assertEqual(captured['limit'],12)

    def test_document_receipts_cross_turns_but_state_receipts_do_not(self):
        prepared=self.prepare()
        docs=[self.read(prepared,'document',name=n)['receipt']
              for n in prepared['input_manifest']['documents']]
        new=self.runner.prepare(mode='compact')
        self.assertTrue(all(d['reusable_receipt'] for d in new['input_manifest']['documents'].values()))
        receipts=docs+[self.read(new,'state')['receipt'],
                       self.read(new,'context',channel='instagram')['receipt']]
        self.assertTrue(self.commit(new,receipts)['committed'])
        state=self.runner.state();state['turn_id']=None;gate.write_json(self.runner.path,state)
        other=self.runner.prepare(mode='compact')
        self.assertTrue(any(d['reusable_receipt'] for d in other['input_manifest']['documents'].values()))
        with self.assertRaisesRegex(ControlError,'invalid_input_receipt'):
            inputs.validate(self.runner,self.runner.state(),{},receipts)
        bundle=inputs.read_bundle(self.runner,other['turn_id'],other['input_id'],docs)
        self.assertIn('CORE_RULES.md',bundle['reused_documents'])
        inputs.validate(self.runner,self.runner.state(),{},bundle['input_receipts'])

    def test_failed_prepare_preserves_prior_generation_and_can_commit(self):
        prepared=self.prepare()
        receipts=self.required(prepared)
        before=self.runner.path.read_bytes()
        with patch.object(self.runner.store,'recall',side_effect=OSError('read failed')):
            with self.assertRaises(OSError):
                self.runner.prepare(queries=[['topic_state:book']])
        self.assertEqual(before,self.runner.path.read_bytes())
        self.assertTrue(self.commit(prepared,receipts)['committed'])

    def test_failed_prepare_does_not_make_old_revision_current(self):
        prepared=self.prepare()
        receipts=self.required(prepared)
        draft=self.draft()
        draft['decision_id']='pending-draft'
        self.store.apply('main',self.decision(),self.clock)
        with patch.object(self.runner.store,'recall',side_effect=OSError('read failed')):
            with self.assertRaises(OSError):
                self.runner.prepare(queries=[['topic_state:book']])
        with self.assertRaisesRegex(ControlError,'prepare_inputs_changed'):
            self.runner.commit(prepared['turn_id'],draft,input_receipts=receipts)

    def test_bundle_delivers_required_inputs_and_deduplicates_recall(self):
        prepared=self.prepare(queries=[['topic_state:book'],['topic_state:book']])
        with patch.object(self.runner.store,'recall',wraps=self.runner.store.recall) as recall:
            bundle=inputs.read_bundle(self.runner,prepared['turn_id'],prepared['input_id'])
        self.assertEqual(recall.call_count,1)
        self.assertEqual(sum(c['kind']=='recall' for c in bundle['components']),1)
        self.assertIn('CORE_RULES.md',bundle['document_receipts'])
        self.assertTrue(self.commit(prepared,bundle['input_receipts'])['committed'])

    def test_bundle_pages_context_and_reports_other_unavailable_channel(self):
        self.ingest();self.recent(tuple(['old-'+str(i) for i in range(35)]+['m1']))
        # An old target expands context beyond the normal 30-message page.
        self.ingest('old-0')
        self.cfg['slack']={'enabled':True}
        prepared=self.runner.prepare(mode='compact')
        bundle=inputs.read_bundle(self.runner,prepared['turn_id'],prepared['input_id'])
        pages=[c for c in bundle['components'] if c['kind']=='context']
        self.assertEqual(sum(len(c['content']['messages']) for c in pages),36)
        self.assertEqual(len(pages),2)
        self.assertIn('slack',bundle['unavailable_contexts'])
        self.assertTrue(self.commit(prepared,bundle['input_receipts'])['committed'])

    def test_bundle_detects_mutation_between_components(self):
        prepared=self.prepare()
        original=inputs.read
        def changed(*args,**kwargs):
            result=original(*args,**kwargs)
            if args[3]=='state':
                self.ingest('new-message', direction='outgoing')
            return result
        with patch.object(inputs,'read',side_effect=changed):
            with self.assertRaisesRegex(ControlError,'prepare_inputs_changed'):
                inputs.read_bundle(self.runner,prepared['turn_id'],prepared['input_id'])
        self.assertEqual(self.snapshot()['jobs'],[])

    def test_bundled_prepare_reuses_only_explicit_received_document_tokens(self):
        self.ingest();self.recent()
        first=self.runner.prepare(mode='bundled')
        tokens=list(first['document_receipts'].values())
        second=self.runner.prepare(mode='bundled',document_receipts=tokens)
        self.assertEqual(second['documents'],{})
        self.assertIn('CORE_RULES.md',second['reused_documents'])
        third=self.runner.prepare(mode='bundled')
        self.assertIn('CORE_RULES.md',third['documents'])
        with self.assertRaisesRegex(ControlError,'invalid_document_receipt'):
            self.runner.prepare(document_receipts=['unknown-token'])
        self.assertEqual(self.runner.state()['prepared_revision'],third['snapshot']['revision'])

    def test_document_reuse_is_not_transferable_to_another_wake(self):
        first=self.prepare()
        token=self.read(first,'document',name='CORE_RULES.md')['receipt']
        for name in ('wake','gate_state'):
            gate.write_json(self.state_dir/(name+'.json'),{'wake_id':'wake-2'})
        gate.write_json(self.state_dir/'wake_ack.json',
                        {'wake_id':'wake-2','phase':'started','at_ms':self.clock})
        other=decision_session.DecisionSession(self.cfg,'wake-2',lambda:self.clock,self.sleep)
        with self.assertRaisesRegex(ControlError,'invalid_document_receipt'):
            other.prepare(document_receipts=[token])

    def test_reused_bundled_document_edit_blocks_commit(self):
        self.ingest();self.recent()
        first=self.runner.prepare(mode='bundled')
        second=self.runner.prepare(mode='bundled',
                                   document_receipts=list(first['document_receipts'].values()))
        original=inputs.root_path
        replacement=Path(self.tmp.name)/'edited.md'
        replacement.write_text('changed policy')
        with patch.object(inputs,'root_path',
                          side_effect=lambda runner,name: replacement if name=='CORE_RULES.md'
                          else original(runner,name)):
            with self.assertRaisesRegex(ControlError,'prepare_inputs_changed'):
                self.runner.commit(second['turn_id'],self.draft())

    def test_bundle_cli_returns_receipts_for_commit(self):
        prepared=self.prepare()
        config=Path(self.tmp.name)/'automation.json';config.write_text(json.dumps(self.cfg))
        output=io.StringIO()
        with redirect_stdout(output),patch.object(decision_session,'DecisionSession',return_value=self.runner):
            code=decision_session.main(['--config',str(config),'--wake-id','wake-1','read-bundle',
                '--turn-id',prepared['turn_id'],'--input-id',prepared['input_id']])
        self.assertEqual(code,0,output.getvalue())
        bundle=json.loads(output.getvalue())
        self.assertTrue(self.commit(prepared,bundle['input_receipts'])['committed'])

    def test_repeated_reads_do_not_exhaust_required_input_or_receipt_budget(self):
        prepared=self.prepare()
        receipts=[self.read(prepared,'state')['receipt'] for _ in range(65)]
        receipts += [self.read(prepared,'document',name=n)['receipt']
                     for n in prepared['input_manifest']['documents']]
        receipts.append(self.read(prepared,'context',channel='instagram')['receipt'])
        self.assertTrue(self.commit(prepared,receipts)['committed'])

    def test_compact_shadow_memory_failure_preserves_receipts_and_reply_delivery(self):
        import memory_continuity
        memory_continuity.configure(self.store,'main',{'mode':'shadow'},self.clock)
        prepared=self.prepare();draft=self.draft();draft['memory_ops']=[{'op':'bad'}]
        with self.assertRaises(ControlError):
            self.runner.commit(prepared['turn_id'],draft,input_receipts=[])
        self.assertEqual(self.store.memory_diagnostics('main')['total'],0)
        result=self.runner.commit(prepared['turn_id'],draft,input_receipts=self.required(prepared),
                                  conversation_ended=True)
        self.assertTrue(result['committed'])
        self.assertEqual(result['commit']['memory_report']['status'],'shadow_rejected')
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM dm_jobs WHERE id='g1:0'").fetchone()[0],'confirmed')

    def test_active_memories_are_frozen_into_compact_state_and_require_receipt(self):
        import memory_continuity
        memory_continuity.configure(self.store,'main',{'mode':'enforce'},self.clock)
        self.ingest(text='설명에서는 정확한 명칭을 써줘'); self.recent()
        self.store.apply('main',self.decision(memory_ops=[{
            'op':'remember','id':'pref','kind':'preference','key':'wording','scope':'private_dm',
            'channel':'instagram','summary':'이 설명에서는 정확한 명칭을 선호',
            'scope_note':'해당 설명에서만 적용',
            'evidence':[{'message_id':'m1','excerpt':'정확한 명칭을 써줘'}]}]),self.clock)
        prepared=self.runner.prepare(mode='compact')
        self.assertIn('memory_context',prepared['input_manifest']['state_sections'])
        self.assertIn('runbooks/memory-continuity.md',prepared['input_manifest']['documents'])
        result=self.runner.read_input(prepared['turn_id'],prepared['input_id'],'state',sections=['memory_context'])
        self.assertEqual(result['content']['memory_context']['instagram']['items'][0]['id'],'pref')
        receipts=self.required(prepared)
        self.assertTrue(self.commit(prepared,receipts)['committed'])

    def test_compact_memory_recall_reads_details_with_revision_and_receipt(self):
        import memory_continuity
        memory_continuity.configure(self.store,'main',{'mode':'enforce'},self.clock)
        self.ingest(); self.recent()
        self.store.apply('main',self.decision(facts=[{
            'id':'prior','source':'simulated','summary':'previous explanation',
            'details':{'choice':'unique-detail-marker'}}]),self.clock)
        prepared=self.runner.prepare(mode='compact')
        result=self.runner.read_input(prepared['turn_id'],prepared['input_id'],'recall',
            keywords=['unique-detail-marker'],memory=True,order='oldest')
        self.assertEqual(result['content']['facts'][0]['id'],'prior')
        self.assertEqual(result['content']['revision'],prepared['snapshot']['revision'])
        self.assertIsInstance(result['receipt'],str)

    def history_fixture(self):
        import memory_continuity
        memory_continuity.configure(self.store,'main',{'mode':'enforce'},self.clock)
        self.ingest(text='설명에서는 정확한 명칭을 써줘');self.recent()
        self.store.apply('main',self.decision(memory_ops=[{
            'op':'remember','id':'pref','kind':'preference','key':'opaque-style','scope':'private_dm',
            'channel':'instagram','summary':'설명 형식','scope_note':'해당 설명에서만',
            'evidence':[{'message_id':'m1','excerpt':'정확한 명칭을 써줘'}]}]),self.clock)
        self.store.apply('main',self.decision(memory_ops=[{'op':'revoke','id':'pref','reason':'철회'}]),self.clock)
        return self.runner.prepare(mode='compact')

    def test_compact_history_discovery_and_evidence_reads_issue_distinct_receipts(self):
        prepared=self.history_fixture()
        page=self.read(prepared,'memory-history-search',channel='instagram',keywords=['정확한'])
        item=page['content']['items'][0]
        self.assertEqual(item['key'],'opaque-style')
        self.assertEqual(item['status'],'revoked')
        context=self.read(prepared,'memory-context',channel='instagram',key=item['key'],history=True)
        self.assertEqual(context['content']['items'][0]['evidence'][0]['message_id'],'m1')
        self.assertEqual(context['content']['revision'],prepared['snapshot']['revision'])
        receipts=self.required(prepared)+[page['receipt'],context['receipt']]
        self.assertNotEqual(page['receipt'],context['receipt'])
        self.assertTrue(self.commit(prepared,receipts)['committed'])

    def test_compact_history_read_rejects_changed_revision_and_oversized_response(self):
        prepared=self.history_fixture()
        with patch.object(inputs,'COMPONENT_BYTES',100):
            result=self.read(prepared,'memory-history-search',channel='instagram',keywords=['정확한'])
            self.assertTrue(result['input_advisory']['budget_exceeded'])
            self.assertTrue(result['content']['items'])
        self.assertTrue(self.runner.state()['input_offers'])
        self.store.apply('main',self.decision(),self.clock)
        for kind,args in [('memory-history-search',{'keywords':['정확한']}),
                          ('memory-context',{'key':'opaque-style','history':True})]:
            with self.subTest(kind=kind),self.assertRaisesRegex(ControlError,'prepare_inputs_changed'):
                self.read(prepared,kind,channel='instagram',**args)

    def test_compact_history_cli_reaches_both_readers(self):
        prepared=self.history_fixture()
        config=Path(self.tmp.name)/'automation.json';config.write_text(json.dumps(self.cfg))
        args=['--config',str(config),'--wake-id','wake-1']
        ids=['--turn-id',prepared['turn_id'],'--input-id',prepared['input_id']]
        for action,query in [('read-memory-history-search',['--keywords','정확한']),
                             ('read-memory-context',['--key','opaque-style','--history'])]:
            output=io.StringIO()
            with redirect_stdout(output),patch.object(decision_session,'DecisionSession',return_value=self.runner):
                code=decision_session.main([*args,action,*ids,'--channel','instagram',*query])
            self.assertEqual(code,0,output.getvalue())
            self.assertEqual(json.loads(output.getvalue())['content']['items'][0]['id'],'pref')

    setUp = session_tests.DecisionSessionTests.setUp
    tearDown = session_tests.DecisionSessionTests.tearDown
    snapshot = session_tests.DecisionSessionTests.snapshot
    decision = session_tests.DecisionSessionTests.decision
    review = session_tests.DecisionSessionTests.review
    ingest = session_tests.DecisionSessionTests.ingest
    activity = session_tests.DecisionSessionTests.activity
    free = session_tests.DecisionSessionTests.free
    sleep = session_tests.DecisionSessionTests.sleep
    recent = session_tests.DecisionSessionTests.recent
    draft = session_tests.DecisionSessionTests.draft
    traces = session_tests.DecisionSessionTests.traces

    def prepare(self, ids=('m1',), **kwargs):
        self.ingest(); self.recent(ids)
        return self.runner.prepare(mode='compact', **kwargs)

    def read(self, prepared, kind, **kwargs):
        return self.runner.read_input(prepared['turn_id'], prepared['input_id'], kind, **kwargs)

    def required(self, prepared, context=True):
        receipts = [self.read(prepared, 'state')['receipt']]
        receipts += [self.read(prepared, 'document', name=name)['receipt']
                     for name in prepared['input_manifest']['documents']]
        if context:
            receipts.append(self.read(prepared, 'context', channel='instagram')['receipt'])
        return receipts

    def commit(self, prepared, receipts):
        with patch.object(self.runner, 'finish', return_value={'next': 'done'}):
            return self.runner.commit(prepared['turn_id'], self.draft(), input_receipts=receipts)

    def test_explicit_bundle_preserves_exact_duplicate_reference(self):
        self.ingest(); self.recent()
        prepared = self.runner.prepare(mode='bundled')
        self.assertEqual(prepared['input_mode'], 'bundled')
        message = prepared['snapshot']['messages'][0]
        self.assertNotIn('text', message)
        self.assertEqual(message['body_ref'], {'channel': 'instagram', 'message_id': 'm1'})
        self.assertEqual(prepared['contexts']['instagram']['messages'][0]['text'], 'hello')
        self.assertIn('CORE_RULES.md', prepared['documents'])

    def test_prepared_views_include_confirmed_appointments_within_a_week(self):
        day=86400000
        appts=[{'id':aid,'start_ms':self.clock+offset,'end_ms':self.clock+offset+3600000,
                'status':status,'fixed':False,'location':'cafe'}
               for aid,offset,status in (('confirmed-4d',4*day,'confirmed'),
                                         ('confirmed-9d',9*day,'confirmed'),
                                         ('proposed-2d',2*day,'proposed'))]
        self.store.apply('main',self.decision(appointments=appts),self.clock)
        self.ingest(); self.recent()
        bundled=self.runner.prepare(mode='bundled')
        self.assertEqual([a['id'] for a in bundled['snapshot']['appointments']],['confirmed-4d'])
        with self.store.connect(True) as db:
            db.execute('UPDATE sessions SET next_decision_ms=?',(self.clock,))
        compact=self.runner.prepare(mode='compact')
        self.assertIn('appointments',compact['input_manifest']['required_state_sections'])
        state=self.read(compact,'state',sections=['appointments'])['content']
        self.assertEqual([a['id'] for a in state['appointments']],['confirmed-4d'])

    def test_different_cache_text_is_not_silently_substituted(self):
        self.ingest(text='authoritative'); self.recent()
        self.assertEqual(self.runner.prepare(mode='bundled')['snapshot']['messages'][0]['text'], 'authoritative')

    def test_auto_budget_and_explicit_bundle_limit_are_independent_of_attempt(self):
        self.ingest(); self.recent()
        path = self.state_dir / 'recent_thread.json'
        data = gate.load_json(path, {}); data['messages'][0]['text'] = 'x' * inputs.BUNDLE_BYTES
        gate.write_json(path, data)
        prepared = self.runner.prepare()
        self.assertEqual(prepared['input_mode'], 'compact')
        self.assertLessEqual(inputs.size(prepared), inputs.MANIFEST_BYTES)
        bundled=self.runner.prepare(mode='bundled')
        self.assertEqual(bundled['input_mode'],'bundled')
        self.assertTrue(bundled['input_budget_exceeded'])
        self.assertIn(data['messages'][0]['text'],encode(bundled))
        self.assertIsNotNone(self.runner.state()['prepared_revision'])

    def test_compact_manifest_contains_no_bodies_or_policy_or_recall_text(self):
        self.ingest(text='private_dm_marker'); self.recent()
        original = self.runner.store.tick
        def snapshot(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get('view') == 'life':
                result['jobs'] = [{'id':'j', 'status':'pending', 'channel':'instagram',
                                   'text':'private_draft_marker'}]
                result['events'][0]['payload'] = {'text':'private_event_marker'}
            return result
        with patch.object(self.runner.store, 'tick', side_effect=snapshot):
            prepared = self.runner.prepare([['private_query_marker']], mode='compact')
        encoded = encode(prepared)
        for marker in ('private_dm_marker', 'private_draft_marker', 'private_event_marker', 'private_query_marker'):
            self.assertNotIn(marker, encoded)
        self.assertEqual(prepared['documents'], {})
        self.assertEqual(prepared['recalls'], [])
        self.assertEqual(self.runner.state()['context_ids'], {})
        self.assertIn('result_count', prepared['input_manifest']['recalls'][0])

    def test_manifest_ids_do_not_count_as_read_and_no_decision_is_written(self):
        prepared = self.prepare()
        for receipts, reason in ((None, 'input_receipts_required'), ([], 'state_input_required')):
            with self.assertRaisesRegex(ControlError, reason):
                self.commit(prepared, receipts)
        self.assertEqual(self.snapshot()['jobs'], [])

    def test_read_all_required_inputs_then_commit_and_replay_without_receipts(self):
        prepared = self.prepare()
        receipts = self.required(prepared)
        draft = self.draft()
        with patch.object(self.runner, 'finish', return_value={'next':'done'}):
            self.assertTrue(self.runner.commit(prepared['turn_id'], draft, input_receipts=receipts)['committed'])
            (self.state_dir / 'recent_thread.json').unlink()
            replay = self.runner.commit(prepared['turn_id'], draft)
        self.assertEqual(replay['commit']['reason'], 'already_applied')
        self.assertEqual(len(self.snapshot()['jobs']), 1)

    def test_known_policy_hash_does_not_replace_compact_document_ack(self):
        self.ingest(); self.recent()
        hashed = self.runner.prepare()['snapshot']['core_rules_hash']
        prepared = self.runner.prepare(known_core_rules_hash=hashed, mode='compact')
        self.assertIn('CORE_RULES.md', prepared['input_manifest']['documents'])
        receipts = self.required(prepared)
        with self.assertRaisesRegex(ControlError, 'document_input_required'):
            self.commit(prepared, receipts[:1] + receipts[2:])
        self.assertTrue(self.commit(prepared, receipts)['committed'])

    def test_receipt_is_stored_only_as_hash_and_not_emitted_in_trace(self):
        prepared = self.prepare()
        token = self.read(prepared, 'context', channel='instagram')['receipt']
        state_text = self.runner.path.read_text()
        self.assertNotIn(token, state_text)
        self.assertIn(inputs.instrumentation.digest(token), state_text)
        logs = '\n'.join(p.read_text() for p in self.state_dir.glob('decision-trace-*.jsonl'))
        self.assertNotIn(token, logs)
        self.assertIn('input_read_started', logs)
        self.assertIn('input_read_finished', logs)
        self.assertIn('read_id', logs)

    def test_issued_but_unacknowledged_context_receipt_does_not_credit_context(self):
        prepared = self.prepare()
        receipts = self.required(prepared, context=False)
        self.read(prepared, 'context', channel='instagram')
        with self.assertRaisesRegex(ControlError, 'message_input_required'):
            self.commit(prepared, receipts)
        self.assertEqual(self.snapshot()['jobs'], [])

    def test_invalid_and_prior_prepare_receipts_cannot_be_reused(self):
        prepared = self.prepare()
        receipts = self.required(prepared)
        with self.assertRaisesRegex(ControlError, 'invalid_input_receipt'):
            self.commit(prepared, ['invented'])
        new = self.runner.prepare(mode='compact')
        self.assertEqual(prepared['turn_id'], new['turn_id'])
        with self.assertRaisesRegex(ControlError, 'input_prepare_required'):
            self.read(prepared, 'state')
        with self.assertRaisesRegex(ControlError, 'invalid_input_receipt'):
            self.commit(new, receipts)

    def test_recent_context_is_required_beyond_only_target(self):
        prepared = self.prepare(('old1','old2','m1'))
        receipts = self.required(prepared, context=False)
        receipts.append(self.read(prepared, 'context', channel='instagram', message_ids=['m1'])['receipt'])
        with self.assertRaisesRegex(ControlError, 'message_input_required'):
            self.commit(prepared, receipts)
        receipts.append(self.read(prepared, 'context', channel='instagram', message_ids=['old1','old2'])['receipt'])
        self.assertTrue(self.commit(prepared, receipts)['committed'])

    def test_context_pages_preserve_source_order_and_earliest_pending_target(self):
        ids = ('m1', *['old' + str(i) for i in range(9)])
        prepared = self.prepare(ids)
        self.assertEqual(prepared['contexts']['instagram']['available_message_ids'], list(ids))
        page = self.read(prepared, 'context', channel='instagram', offset=6, limit=4)
        self.assertEqual([m['id'] for m in page['content']['messages']], list(ids[6:]))
        picked = self.read(prepared, 'context', channel='instagram', message_ids=['old8','m1'])
        self.assertEqual([m['id'] for m in picked['content']['messages']], ['m1','old8'])

    def test_new_edited_reordered_or_removed_messages_require_new_prepare(self):
        prepared = self.prepare(('old','m1'))
        receipts = self.required(prepared)
        path = self.state_dir / 'recent_thread.json'; original = gate.load_json(path, {})
        variants = []
        data = copy.deepcopy(original); data['messages'].append({'id':'new','text':'new'}); variants.append(data)
        data = copy.deepcopy(original); data['messages'][1]['text'] = 'edited'; variants.append(data)
        data = copy.deepcopy(original); data['messages'].reverse(); variants.append(data)
        data = copy.deepcopy(original); data['messages'].pop(); variants.append(data)
        for data in variants:
            with self.subTest(messages=data['messages']):
                gate.write_json(path, data)
                with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
                    self.read(prepared, 'context', channel='instagram')
                with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
                    self.commit(prepared, receipts)

    def test_poll_timestamp_change_alone_is_allowed(self):
        prepared = self.prepare(); receipts = self.required(prepared)
        self.clock += 1000
        self.recent()
        self.assertTrue(self.commit(prepared, receipts)['committed'])

    def test_stale_or_missing_context_cannot_issue_or_commit(self):
        prepared = self.prepare(); receipts = self.required(prepared)
        self.clock += 600001
        with self.assertRaisesRegex(ControlError, 'reply_context_not_ready'):
            self.read(prepared, 'context', channel='instagram')
        with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
            self.commit(prepared, receipts)

    def test_large_message_is_read_in_full_and_does_not_block_commit(self):
        self.ingest(); self.recent()
        path = self.state_dir / 'recent_thread.json'; data = gate.load_json(path, {})
        data['messages'][0]['text'] = '가' * inputs.COMPONENT_BYTES
        gate.write_json(path, data)
        prepared = self.runner.prepare(mode='compact')
        result=self.read(prepared,'context',channel='instagram',limit=1)
        self.assertEqual(result['content']['messages'][0]['text'],data['messages'][0]['text'])
        self.assertTrue(result['input_advisory']['budget_exceeded'])
        receipts=self.required(prepared,context=False)+[result['receipt']]
        self.assertTrue(self.commit(prepared,receipts)['committed'])
        self.assertEqual(gate.load_json(path, {})['messages'][0]['text'], data['messages'][0]['text'])

    def test_state_fields_and_list_pages_need_complete_acknowledgements(self):
        prepared = self.prepare()
        receipts = [self.read(prepared, 'document', name=name)['receipt']
                    for name in prepared['input_manifest']['documents']]
        receipts.append(self.read(prepared, 'context', channel='instagram')['receipt'])
        sections = [k for k in prepared['input_manifest']['state_sections'] if k != 'messages']
        receipts.append(self.read(prepared, 'state', sections=sections)['receipt'])
        with self.assertRaisesRegex(ControlError, 'state_input_required'):
            self.commit(prepared, receipts)
        page = self.read(prepared, 'state', sections=['messages'], offset=0, limit=1)
        self.assertEqual(page['content']['messages'][0]['text'], 'hello')
        receipts.append(page['receipt'])
        self.assertTrue(self.commit(prepared, receipts)['committed'])

    def test_draft_and_event_information_remains_available_in_state_reader(self):
        prepared = self.prepare()
        actual = self.read(prepared, 'state')['content']
        expected = self.runner.store.tick('main', self.clock, view='dm')
        self.assertEqual(actual['messages'], expected['messages'])
        self.assertEqual(actual['jobs'], expected['jobs'])
        self.assertEqual(actual['events'], expected['events'])

    def test_document_is_read_exactly_and_detects_change_at_commit(self):
        prepared = self.prepare(); receipts = self.required(prepared)
        document = self.read(prepared, 'document', name='CORE_RULES.md')
        self.assertEqual(hashlib.sha256(document['content']['text'].encode()).hexdigest(),
                         prepared['input_manifest']['documents']['CORE_RULES.md']['sha256'])
        changed = Path(self.tmp.name) / 'changed.md'; changed.write_text('changed')
        with patch.object(inputs, 'root_path', return_value=changed):
            with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
                self.read(prepared, 'document', name='CORE_RULES.md')
            with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
                self.commit(prepared, receipts)

    def test_document_paths_cannot_escape_or_read_other_files(self):
        prepared = self.prepare()
        for name in ('/etc/passwd', '../CORE_RULES.md', 'design/../../README.md',
                     'automation.json', 'design/does-not-exist.md'):
            with self.subTest(name=name), self.assertRaisesRegex(ControlError, 'invalid_input_document'):
                self.read(prepared, 'document', name=name)
        root = Path(self.tmp.name) / 'sandbox'; (root / 'design').mkdir(parents=True)
        outside = Path(self.tmp.name) / 'outside.md'; outside.write_text('outside')
        (root / 'design' / 'escape.md').symlink_to(outside)
        with patch.object(inputs, '__file__', str(root / 'prepared_inputs.py')):
            with self.assertRaisesRegex(ControlError, 'invalid_input_document'):
                self.read(prepared, 'document', name='design/escape.md')

    def test_recall_read_is_revision_bound_and_logs_only_statistics(self):
        prepared = self.prepare(queries=[['private_lookup_marker']])
        result = self.read(prepared, 'recall', index=0)
        self.assertEqual(result['content']['revision'], prepared['snapshot']['revision'])
        self.assertEqual(self.read(prepared, 'recall', keywords=[])['content']['facts'],
                         self.store.recall('main', [])['facts'])
        logs = '\n'.join(p.read_text() for p in self.state_dir.glob('decision-trace-*.jsonl'))
        self.assertNotIn('private_lookup_marker', logs)
        self.assertTrue(any(t['phase']=='input_recall' and 'result_count' in t for t in self.traces()))
        original = self.runner.store.recall
        def race(*args, **kwargs):
            self.ingest('m2'); return original(*args, **kwargs)
        with patch.object(self.runner.store, 'recall', side_effect=race):
            with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
                self.read(prepared, 'recall', index=0)

    def test_database_or_claim_change_invalidates_reader(self):
        prepared = self.prepare(); self.ingest('m2')
        with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
            self.read(prepared, 'state')
        gate.write_json(self.state_dir / 'wake.json', {'wake_id':'replacement'})
        with self.assertRaisesRegex(ControlError, 'stale_claim'):
            self.read(prepared, 'document', name='CORE_RULES.md')

    def test_read_operations_leave_revision_and_jobs_unchanged(self):
        prepared = self.prepare(); before = self.snapshot()['revision']
        self.required(prepared)
        self.read(prepared, 'recall', keywords=[])
        self.assertEqual(before, self.snapshot()['revision'])
        self.assertEqual(self.snapshot()['jobs'], [])

    def test_life_compact_keeps_activity_history_and_selection_documents(self):
        self.clock += 3601000
        self.recent(())
        prepared = self.runner.prepare(mode='compact')
        self.assertEqual(prepared['snapshot']['work_manifest']['primary'], 'life')
        self.assertIn('runbooks/activity-selection.md', prepared['input_manifest']['documents'])
        state = self.read(prepared, 'state')
        self.assertIn('duration_guidance', state['content']['activity_history'])
        receipts = self.required(prepared, context=False)
        inputs.validate(self.runner, self.runner.state(), self.decision(), receipts)

    def test_state_page_gap_does_not_pass_even_with_last_page_acknowledged(self):
        self.ingest('m2')
        prepared = self.prepare(('m1','m2'))
        receipts = [self.read(prepared, 'document', name=name)['receipt']
                    for name in prepared['input_manifest']['documents']]
        receipts.append(self.read(prepared, 'context', channel='instagram')['receipt'])
        sections = [k for k in prepared['input_manifest']['state_sections'] if k != 'messages']
        receipts.append(self.read(prepared, 'state', sections=sections)['receipt'])
        receipts.append(self.read(prepared, 'state', sections=['messages'], offset=1, limit=1)['receipt'])
        with self.assertRaisesRegex(ControlError, 'state_input_required'):
            self.commit(prepared, receipts)

    def test_invalid_selectors_and_read_limit_cannot_issue_extra_receipts(self):
        prepared = self.prepare()
        for kind, args in (
                ('context', {'channel':'instagram','message_ids':['missing']}),
                ('context', {'channel':'instagram','offset':-1}),
                ('state', {'sections':['state'],'offset':1,'limit':1}),
                ('recall', {'index':-1}), ('recall', {'index':3})):
            with self.subTest(kind=kind, args=args), self.assertRaises(ControlError):
                self.read(prepared, kind, **args)
        self.assertEqual(self.runner.state()['input_offers'], {})
        with patch.object(inputs, 'MAX_READS', 1):
            self.read(prepared, 'state')
            result=self.read(prepared, 'context', channel='instagram')
            self.assertTrue(result['input_advisory']['read_count_exceeded'])
        self.assertEqual(len(self.runner.state()['input_offers']), 2)

    def test_changed_optional_document_also_invalidates_ack(self):
        prepared = self.prepare(); receipts = self.required(prepared)
        optional = self.read(prepared, 'document', name='design/biographical-continuity.md')
        receipts.append(optional['receipt'])
        old_path = inputs.root_path
        changed = Path(self.tmp.name) / 'optional.md'; changed.write_text('changed')
        def lookup(runner, name):
            return changed if name == 'design/biographical-continuity.md' else old_path(runner, name)
        with patch.object(inputs, 'root_path', side_effect=lookup):
            with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
                self.commit(prepared, receipts)

    def test_empty_healthy_context_allows_initiate_without_fake_read(self):
        self.recent(())
        prepared = self.runner.prepare(mode='compact')
        receipts = self.required(prepared, context=False)
        draft = self.decision(dm_decisions=[{'action':'initiate','channel':'instagram','group_id':'g1',
            'parts':['hello'],'due_ms':self.clock,'reason':'check in'}])
        with patch.object(self.runner, 'finish', return_value={'next':'done'}):
            result = self.runner.commit(prepared['turn_id'], draft, input_receipts=receipts)
        self.assertTrue(result['committed'])

    def test_cli_compact_read_and_receipt_file_commit(self):
        self.ingest(); self.recent()
        config = Path(self.tmp.name) / 'automation.json'; config.write_text(json.dumps(self.cfg))
        command = ['--config', str(config), '--wake-id', 'wake-1']
        def run(*args):
            output = io.StringIO()
            with redirect_stdout(output), patch.object(decision_session, 'DecisionSession', return_value=self.runner):
                code = decision_session.main([*command, *args])
            self.assertEqual(code, 0, output.getvalue())
            return json.loads(output.getvalue())
        prepared = run('prepare', '--mode', 'compact')
        args = ('--turn-id', prepared['turn_id'], '--input-id', prepared['input_id'])
        receipts = [run('read-state', *args)['receipt']]
        receipts += [run('read-document', *args, '--name', name)['receipt']
                     for name in prepared['input_manifest']['documents']]
        receipts.append(run('read-context', *args, '--channel', 'instagram')['receipt'])
        self.assertEqual(run('read-recall', *args, '--keywords')['kind'], 'recall')
        draft = self.draft()
        draft_path = Path(self.tmp.name) / 'draft.json'; draft_path.write_text(json.dumps(draft))
        receipt_path = Path(self.tmp.name) / 'receipts.json'; receipt_path.write_text(json.dumps(receipts))
        result = run('commit', '--turn-id', prepared['turn_id'], '--decision', '@'+str(draft_path),
                     '--input-receipts', '@'+str(receipt_path), '--conversation-ended')
        self.assertTrue(result['committed'])

    def test_auto_uses_bundle_when_acknowledged_policy_documents_make_input_small(self):
        self.ingest();self.recent()
        first=self.runner.prepare(mode='bundled')
        small=self.runner.prepare(document_receipts=list(first['document_receipts'].values()))
        self.assertEqual(small['input_mode'],'bundled')
        self.assertIn('design/conversation-rules.md',small['reused_documents'])
        self.assertFalse(small['input_budget_exceeded'])

    def test_larger_policy_switches_auto_to_compact_and_can_still_commit(self):
        self.ingest();self.recent()
        name='design/conversation-rules.md'
        original=decision_session.required_text;root=inputs.root_path
        document=Path(self.tmp.name)/'larger-policy.md'
        document.write_text(original(decision_session.ROOT/name)+'\n'+'추가 원문 지침\n'*400)
        def policy(path):
            return document.read_text() if path==decision_session.ROOT/name else original(path)
        def lookup(runner,requested):
            return document if requested==name else root(runner,requested)
        with patch.object(decision_session,'required_text',side_effect=policy),patch.object(inputs,'root_path',side_effect=lookup):
            prepared=self.runner.prepare()
            self.assertEqual(prepared['input_mode'],'compact')
            bundle=inputs.read_bundle(self.runner,prepared['turn_id'],prepared['input_id'])
            delivered=next(c['content']['text'] for c in bundle['components']
                           if c['kind']=='document' and c['content']['name']==name)
            self.assertEqual(delivered,document.read_text())
            self.assertIn('뜬금없는 질문',delivered)
            self.assertIn('연결되는 질문',delivered)
            self.assertTrue(self.commit(prepared,bundle['input_receipts'])['committed'])


if __name__ == '__main__':
    unittest.main()
