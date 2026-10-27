"""Real SQLite shadow editing; the production reply pipeline and source DBs stay intact."""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

from common import ControlError
import character_documents as docs
import character_editor as editor
import character_sources as sources
import episodic_memory as memory
import model_adapter
import test_episodic_memory as fixtures
from test_world_events import iso


class CharacterDocumentTests(unittest.TestCase):
    def setUp(self):
        self.c=fixtures.EpisodicMemoryTests('test_no_controller_writes_and_idempotent_replay')
        self.c.setUp();self.addCleanup(self.c.tearDown)
        self.c.cfg['character_documents']={'mode':'shadow','model_adapter':'character-file'}
        self.c.cfg['model_adapters']={'character-file':{'backend':'file'}}

    def put(self,eid='thesis',**changes):
        c=self.c
        item={'event_id':eid,'mode':'occurred','summary':'논문 초안을 제출했다','known_by':['self'],
              'participants':['self'],'occurred_at':iso(c.clock)}
        item.update(changes)
        if item['mode']!='occurred':
            item.pop('occurred_at',None);item.update(not_before=iso(c.clock),expires_at=iso(c.clock+3600000))
        c.store.event_import('main',{'schema_version':1,'request_id':eid,'events':[item]},c.clock,'user:fixture')

    def packet(self):
        c=self.c;r=docs.prepare(c.cfg,c.clock);self.assertEqual(r['reason'],'prepared');return r['packet']

    def response(self,p,operations=()):
        return {k:p[k] for k in ('schema','run_id','input_sha256','base_revision')}|{'operations':list(operations)}

    def operation(self,p,**changes):
        ref=p['changed_sources'][0]
        before=next(i for i in p['current_document']['items'] if i['item_id']=='goals:thesis' and i['scope']=='personal')
        quote=ref['data'].get('summary') or ref['data'].get('status') or ref['data'].get('text') or 'unavailable'
        return {'operation':'upsert','item_id':'goals:thesis','section':'goals','scope':'personal',
                'supersedes':before['version'],'current_description':'초안 제출 후 피드백을 기다리는 중이다.',
                'response_implication':'초안 작성 전으로 되돌려 해석하지 않는다.','status':'ongoing','epistemic':'author_asserted',
                'basis':[{'kind':ref['kind'],'id':ref['id'],'version':ref['version'],'excerpt':quote}],
                'review_condition':{'on_source_change':True,'at_ms':None},'change_reason':'초안 제출 사건이 확인됐다.',**changes}

    def publish(self):
        self.put();p=self.packet();r=docs.complete(self.c.cfg,self.response(p,[self.operation(p)]),self.c.clock)
        return p,r

    def assert_reason(self,reason,fn):
        with self.assertRaises(ControlError) as caught:fn()
        self.assertEqual(caught.exception.reason,reason)

    def test_seed_partitions_exact_variable_spans_without_altering_originals(self):
        baseline=docs.seed();self.assertEqual(len(baseline['items']),7)
        for name,template in baseline['templates'].items():
            body=(docs.ROOT/name).read_text()
            # Reconstruct exact bytes by applying recorded slots in source order.
            result=[];end=0
            for slot in sorted((s for s in baseline['slots'] if s['path']==name),key=lambda s:s['start']):
                result.extend([body[end:slot['start']], '{{character:'+slot['item_id']+'}}']);end=slot['start']+len(slot['text'])
            result.append(body[end:]);self.assertEqual(''.join(result),template)
        fixed='\n'.join(baseline['templates'].values())
        self.assertIn('대화를 일부러 어떤 방향으로 유도하지 않는다',fixed)
        self.assertIn('기록에 없는 오프라인 추억',fixed)
        self.assertIn('고유 말투의 정본',fixed)

    def test_off_ignores_broken_unused_settings_and_never_reads_seed_or_sources(self):
        c=self.c;c.cfg['character_documents']={'mode':'off','path':[],'lease_seconds':0}
        with patch.object(docs,'seed',side_effect=AssertionError('seed read')),patch.object(sources,'collect',side_effect=AssertionError('source read')):
            self.assertEqual(editor.run(c.cfg)['reason'],'off')
        self.assertFalse((c.state_dir/'character-documents.sqlite3').exists())

    def test_invalid_mode_is_rejected_and_enforce_is_explicit(self):
        c=self.c;c.cfg['character_documents']['mode']='enforce'
        self.assertEqual(docs.settings(c.cfg)['mode'],'enforce')
        c.cfg['character_documents']['mode']='invalid'
        self.assert_reason('invalid_character_mode',lambda:docs.settings(c.cfg))

    def test_empty_work_never_resolves_adapter_or_calls_model(self):
        c=self.c;c.cfg['character_documents']['model_adapter']='missing'
        with patch.object(model_adapter,'options',side_effect=AssertionError('adapter read')),patch.object(model_adapter,'invoke',side_effect=AssertionError('model woke')):
            self.assertEqual(editor.run(c.cfg,lambda:c.clock)['reason'],'no_work')

    def test_publish_replay_and_exact_diff_preserve_sources_and_other_items(self):
        c=self.c;self.put();before=Path(c.cfg['db']).read_bytes();p=self.packet();response=self.response(p,[self.operation(p)])
        result=docs.complete(c.cfg,response,c.clock);self.assertEqual(result['changed_items'],1)
        self.assertTrue(docs.complete(c.cfg,response,c.clock)['replayed'])
        doc=docs.view(c.cfg,c.clock);self.assertEqual(doc['revision'],result['revision'])
        self.assertEqual(len(doc['items']),7);self.assertEqual(len(doc['changes']),1)
        self.assertEqual(doc['changes'][0]['before']['item_id'],'goals:thesis')
        self.assertEqual(doc['changes'][0]['after']['current_description'],'초안 제출 후 피드백을 기다리는 중이다.')
        career=next(i for i in doc['items'] if i['item_id']=='goals:career')
        self.assertEqual(career['version'],next(i for i in p['current_document']['items'] if i['item_id']=='goals:career')['version'])
        self.assertEqual(Path(c.cfg['db']).read_bytes(),before)
        self.assertEqual(docs.prepare(c.cfg,c.clock)['reason'],'no_work')
        self.assertEqual(docs.settings(c.cfg)['path'].stat().st_mode&0o777,0o600)

    def test_empty_operations_acknowledge_work_without_creating_document_version(self):
        c=self.c;self.put();p=self.packet();result=docs.complete(c.cfg,self.response(p),c.clock)
        self.assertEqual(result['reason'],'no_change');self.assertEqual(result['revision'],p['base_revision'])
        self.assertEqual(docs.prepare(c.cfg,c.clock)['reason'],'no_work')
        with docs.connection(c.cfg) as db:self.assertEqual(db.execute('SELECT count(*) FROM versions').fetchone()[0],1)

    def test_goal_complete_then_followup_and_retraction_keep_version_chain(self):
        c=self.c;p,first=self.publish();c.clock+=1000;self.put('feedback',summary='교수 피드백을 받아 수정하기로 했다')
        p=self.packet();op=self.operation(p,current_description='피드백을 반영하며 수정하는 중이다.',status='ongoing')
        second=docs.complete(c.cfg,self.response(p,[op]),c.clock)
        self.assertNotEqual(first['revision'],second['revision'])
        c.clock+=1000;self.put('cancel',summary='해당 수정 과제를 취소했다');p=self.packet()
        op=self.operation(p,operation='retract',status='retracted',current_description='이 수정 과제는 취소된 상태다.')
        docs.complete(c.cfg,self.response(p,[op]),c.clock)
        item=next(i for i in docs.view(c.cfg,c.clock)['items'] if i['item_id']=='goals:thesis')
        self.assertEqual(item['status'],'retracted')
        with docs.connection(c.cfg) as db:self.assertEqual(db.execute('SELECT count(*) FROM versions').fetchone()[0],4)

    def test_author_events_unknown_or_pending_are_not_work(self):
        c=self.c;self.put('hidden',known_by=['friend']);self.put('later',mode='scene')
        self.assertEqual(editor.run(c.cfg,lambda:c.clock)['reason'],'no_work')
        self.assertNotIn('hidden',json.dumps(sources.collect(c.cfg,c.clock)['refs']))

    def test_channel_only_evidence_cannot_update_personal_description(self):
        c=self.c;c.remember();p=self.packet();ref=p['changed_sources'][0]
        self.assertEqual(ref['channel'],'instagram')
        op=self.operation(p,epistemic='recorded')
        self.assert_reason('character_scope_expansion',lambda:docs.complete(c.cfg,self.response(p,[op]),c.clock))

    def test_channel_overlay_is_separate_from_personal_seed(self):
        c=self.c;c.remember();p=self.packet()
        op=self.operation(p,scope='instagram',supersedes=None,epistemic='recorded')
        docs.complete(c.cfg,self.response(p,[op]),c.clock)
        items=docs.view(c.cfg,c.clock)['items'];self.assertEqual(len(items),8)
        self.assertEqual(next(i for i in items if i['item_id']=='goals:thesis' and i['scope']=='personal')['status'],'baseline')

    def test_wrong_excerpt_and_unread_source_are_rejected_atomically(self):
        c=self.c;self.put();p=self.packet();op=self.operation(p);op['basis'][0]['excerpt']='not in source'
        self.assert_reason('character_excerpt_mismatch',lambda:docs.complete(c.cfg,self.response(p,[op]),c.clock))
        with docs.connection(c.cfg) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM versions').fetchone()[0],1)
            self.assertIsNone(db.execute('SELECT ack FROM observations').fetchone()[0])

    def test_protected_section_and_fixed_item_cannot_be_overwritten(self):
        c=self.c;self.put();p=self.packet();op=self.operation(p,section='execution',item_id='core:skip-review')
        self.assert_reason('character_owned_section_required',lambda:docs.complete(c.cfg,self.response(p,[op]),c.clock))

    def test_self_understanding_is_tentative_not_a_new_canonical_trait(self):
        c=self.c;self.put();p=self.packet()
        op=self.operation(p,item_id='self:reflection',section='self_understanding',epistemic='recorded')
        op['supersedes']=next(i for i in p['current_document']['items'] if i['item_id']=='self:reflection')['version']
        self.assert_reason('character_reflection_not_fact',lambda:docs.complete(c.cfg,self.response(p,[op]),c.clock))

    def test_source_changes_reject_paid_output_but_unrelated_event_does_not(self):
        c=self.c;self.put();p=self.packet();response=self.response(p,[self.operation(p)])
        self.put('unrelated',summary='새 원두를 골랐다')
        self.assertTrue(docs.complete(c.cfg,response,c.clock)['ok'])
        p=self.packet();op=self.operation(p)
        with c.store.connect(True) as db:
            db.execute("UPDATE world_events SET payload_json=json_set(payload_json,'$.known_by',json('[\"friend\"]')) WHERE id='unrelated'")
        self.assert_reason('character_source_changed',lambda:docs.complete(c.cfg,self.response(p,[op]),c.clock))

    def test_one_scheduled_review_is_consumed_even_without_changes(self):
        c=self.c;self.put();p=self.packet();op=self.operation(p,review_condition={'on_source_change':True,'at_ms':c.clock+1000})
        docs.complete(c.cfg,self.response(p,[op]),c.clock);self.assertEqual(docs.prepare(c.cfg,c.clock)['reason'],'no_work')
        c.clock+=1000;p=self.packet();self.assertTrue(p['review_due_versions'])
        docs.complete(c.cfg,self.response(p),c.clock)
        self.assertEqual(docs.prepare(c.cfg,c.clock)['reason'],'no_work')

    def test_transient_failure_backoff_attention_and_manual_retry(self):
        c=self.c;self.put()
        for _ in range(3):
            p=self.packet();docs.fail(c.cfg,p['run_id'],c.clock,'network_error')
            next_=docs.prepare(c.cfg,c.clock)
            if next_['reason']=='retry_later':c.clock+=next_['retry_after_ms']
        self.assertEqual(next_['reason'],'needs_attention')
        docs.retry(c.cfg);self.assertEqual(docs.prepare(c.cfg,c.clock)['reason'],'prepared')

    def test_busy_and_expired_run_do_not_launch_another_model(self):
        c=self.c;self.put();first=editor.run(c.cfg,lambda:c.clock)
        with patch.object(model_adapter,'options',side_effect=AssertionError('duplicate model')):
            self.assertEqual(editor.run(c.cfg,lambda:c.clock)['next'],'await_response')
        c.clock=first['deadline_ms']
        self.assertEqual(editor.run(c.cfg,lambda:c.clock)['reason'],'retry_later')

    def test_bootstrap_ignores_old_new_work_but_keeps_old_reference_context(self):
        c=self.c
        with c.store.connect(True) as db:
            db.execute('INSERT INTO facts VALUES (?,?,?,?,?,?,?)',('main','old',None,c.clock-3*86400000,'canon','오래된 설정','{}'))
        self.assertEqual(docs.prepare(c.cfg,c.clock)['reason'],'no_work')
        self.put();p=self.packet();self.assertIn('old',[r['id'] for r in p['reference_context']])

    def test_self_only_episodic_restatement_does_not_trigger_model(self):
        c=self.c;c.remember();p=self.packet();docs.complete(c.cfg,self.response(p),c.clock)
        with memory.connection(c.cfg,True) as db:
            db.execute("UPDATE memories SET summary='같은 근거의 다른 표현',recorded_ms=recorded_ms+1")
        self.assertEqual(docs.prepare(c.cfg,c.clock)['reason'],'no_work')

    def test_relationship_revision_is_stable_when_only_time_or_controller_revision_moves(self):
        c=self.c
        with c.store.connect(True) as db:db.execute('INSERT INTO relationships VALUES (?,?,?,?)',('main','self','user','{"trust":10}'))
        p=self.packet();docs.complete(c.cfg,self.response(p),c.clock)
        c.clock+=60000
        with c.store.connect(True) as db:db.execute('UPDATE sessions SET revision=revision+1')
        self.assertEqual(docs.prepare(c.cfg,c.clock)['reason'],'no_work')

    def test_export_private_markdown_and_diff_and_source_binding(self):
        c=self.c;_,result=self.publish();paths=docs.export(c.cfg,c.clock)
        self.assertIn('초안 제출 후',Path(paths['document_file']).read_text())
        self.assertEqual(Path(paths['document_file']).stat().st_mode&0o777,0o600)
        self.assertEqual(json.loads(Path(paths['diff_file']).read_text())[0]['item_id'],'goals:thesis')
        c.cfg['persona_id']='other-persona'
        self.assert_reason('character_binding_changed',lambda:docs.view(c.cfg,c.clock))

    def test_file_worker_completes_and_resumes_paid_response_after_busy(self):
        c=self.c;self.put();first=editor.run(c.cfg,lambda:c.clock)
        req=json.loads(Path(first['input_file']).read_text());response=self.response(req['input'],[self.operation(req['input'])])
        docs.private_write(first['response_file'],response)
        with patch.object(docs,'complete',side_effect=ControlError('busy')):
            self.assertEqual(editor.run(c.cfg,lambda:c.clock)['reason'],'busy')
        c.cfg['model_adapters']={}
        with patch.object(model_adapter,'options',side_effect=AssertionError('paid twice')):
            result=editor.run(c.cfg,lambda:c.clock)
        self.assertTrue(result['ok']);self.assertTrue(Path(result['document_file']).is_file())
        self.assertFalse(Path(first['input_file']).exists())
        self.assertTrue(editor.finish(c.cfg,first['run_id'],first['response_file'],lambda:c.clock)['replayed'])

    def test_generic_http_adapter_and_usage_log_do_not_expose_source_text(self):
        c=self.c;self.put(summary='private-source-marker')
        c.cfg['model_adapters']['character-file']={'backend':'http','model':'other','endpoint':'http://unused/chat'}
        def fake(body,*args):
            p=json.loads(body['messages'][1]['content'])
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(self.response(p,[self.operation(p)]))}}],
                    'usage':{'prompt_tokens':10,'completion_tokens':20}}
        result=editor.run(c.cfg,lambda:c.clock,fake);self.assertTrue(result['ok'])
        log=(docs.settings(c.cfg)['path'].parent/'character-editor.jsonl').read_text()
        self.assertNotIn('private-source-marker',log);self.assertNotIn('current_description',log)
        self.assertIn('prompt_tokens',log)

    def test_command_adapter_can_return_empty_changes(self):
        c=self.c;self.put()
        program='import json,sys;r=json.load(sys.stdin);p=r["input"];o={k:p[k] for k in ("schema","run_id","input_sha256","base_revision")};o["operations"]=[];print(json.dumps({"schema":"persona-model-response-v1","request_id":r["request_id"],"input_sha256":r["input_sha256"],"output":o}))'
        c.cfg['model_adapters']['character-file']={'backend':'command','argv':[sys.executable,'-c',program]}
        self.assertEqual(editor.run(c.cfg,lambda:c.clock)['reason'],'no_change')

    def test_shadow_publication_does_not_change_prepared_reply_or_fixed_inputs(self):
        c=self.c;c.ingest();c.recent();before=c.runner.prepare(mode='bundled');self.publish()
        after=c.runner.prepare(mode='bundled')
        self.assertEqual(before['documents'],after['documents'])
        self.assertNotIn('character_brief',after['snapshot'])
        self.assertNotIn('character_documents',after['snapshot'])
        for item in ('design/character/profile.md','design/character/goals.md'):
            self.assertEqual(hashlib.sha256((docs.ROOT/item).read_bytes()).hexdigest(),docs.seed()['source_files'][item])

    def test_cli_single_json_and_gate_state_unchanged(self):
        c=self.c;self.put();cfg=Path(c.tmp.name)/'config.json';cfg.write_text(json.dumps(c.cfg))
        paths=[c.state_dir/name for name in ('wake.json','gate_state.json','wake_ack.json')]
        before={p:p.read_bytes() for p in paths}
        out=io.StringIO()
        with patch.object(editor,'now_ms',return_value=c.clock),redirect_stdout(out):
            rc=editor.main(['--config',str(cfg),'run'])
        self.assertEqual(rc,0);self.assertEqual(json.loads(out.getvalue())['next'],'model')
        self.assertEqual({p:p.read_bytes() for p in paths},before)

    def test_oversized_source_is_reported_without_spinning_or_blocking_other_work(self):
        c=self.c;c.cfg['character_documents']['batch_bytes']=4096
        with c.store.connect(True) as db:
            db.execute('INSERT INTO facts VALUES (?,?,?,?,?,?,?)',('main','large',None,c.clock,'canon','large','{"text":"'+'a'*10000+'"}'))
        r=editor.run(c.cfg,lambda:c.clock);self.assertEqual(r['reason'],'needs_attention')
        self.put();p=self.packet();self.assertEqual(p['changed_sources'][0]['kind'],'world_event')

    def test_missing_episode_store_does_not_invent_deletions_or_block_author_work(self):
        c=self.c;c.remember();p=self.packet();docs.complete(c.cfg,self.response(p),c.clock)
        memory.settings(c.cfg)['path'].unlink()
        r=docs.prepare(c.cfg,c.clock);self.assertEqual(r['reason'],'no_work')
        self.assertIn('episode',r['unavailable_kinds'])
        self.put();p=self.packet();self.assertEqual(p['changed_sources'][0]['kind'],'world_event')
        self.assertFalse(any(r.get('missing') for r in p['changed_sources']))

    def test_published_basis_changed_is_marked_without_replacing_or_erasing_text(self):
        c=self.c;_,result=self.publish()
        with c.store.connect(True) as db:
            db.execute("UPDATE world_events SET payload_json=json_set(payload_json,'$.known_by',json('[\"friend\"]')) WHERE id='thesis'")
        item=next(i for i in docs.view(c.cfg,c.clock)['items'] if i['item_id']=='goals:thesis')
        self.assertEqual(item['basis_validity'],'changed')
        p=self.packet();op=self.operation(p,operation='retract',status='retracted',epistemic='tentative',
            current_description='기존 근거를 더 이상 현재 자료로 확인할 수 없다.')
        self.assertTrue(p['changed_sources'][0]['missing'])
        self.assertTrue(docs.complete(c.cfg,self.response(p,[op]),c.clock)['ok'])

    def test_paid_model_result_is_retained_across_database_io_failure(self):
        c=self.c;self.put();first=editor.run(c.cfg,lambda:c.clock)
        req=json.loads(Path(first['input_file']).read_text());docs.private_write(first['response_file'],self.response(req['input'],[self.operation(req['input'])]))
        with patch.object(docs,'complete',side_effect=sqlite3.OperationalError('disk busy')):
            with self.assertRaises(sqlite3.OperationalError):editor.run(c.cfg,lambda:c.clock)
        self.assertTrue(Path(first['response_file']).exists())
        self.assertTrue(editor.run(c.cfg,lambda:c.clock)['ok'])

    def test_export_failure_does_not_undo_published_result_or_require_new_model(self):
        c=self.c;self.put();first=editor.run(c.cfg,lambda:c.clock)
        req=json.loads(Path(first['input_file']).read_text());docs.private_write(first['response_file'],self.response(req['input'],[self.operation(req['input'])]))
        with patch.object(docs,'export',side_effect=OSError('disk busy')):
            result=editor.run(c.cfg,lambda:c.clock)
        self.assertTrue(result['ok']);self.assertTrue(result['export_pending'])
        self.assertEqual(editor.run(c.cfg,lambda:c.clock)['reason'],'no_work')
        self.assertTrue(Path(docs.export(c.cfg,c.clock,result['revision'])['document_file']).exists())

    def test_original_memory_and_controller_files_are_read_only_during_editing(self):
        c=self.c;c.remember();paths=[Path(c.cfg['db']),memory.settings(c.cfg)['path']]
        before={p:p.read_bytes() for p in paths};packet=self.packet()
        op=self.operation(packet,scope='instagram',supersedes=None,epistemic='reported')
        docs.complete(c.cfg,self.response(packet,[op]),c.clock);docs.export(c.cfg,c.clock)
        self.assertEqual({p:p.read_bytes() for p in paths},before)

    def test_single_channel_self_utterance_cannot_trigger_external_confirmation_loop(self):
        c=self.c;c.ingest('echo',direction='outgoing',text='나 좀 성장한 것 같아',
                         observation={'identity':'platform','channel':'instagram'})
        p=memory.prepare(c.cfg,c.clock)['packet']
        memory.complete(c.cfg,c.response(p,[c.item(p,summary='스스로 성장한 것 같다고 표현했다')]),c.clock)
        refs=sources.collect(c.cfg,c.clock)['refs']
        episode=next(r for r in refs.values() if r['kind']=='episode')
        self.assertFalse(any(r['independent'] for r in episode['roots']))
        self.assertEqual(editor.run(c.cfg,lambda:c.clock)['reason'],'no_work')

    def test_reworded_memory_with_same_roots_keeps_published_basis_current(self):
        c=self.c;c.remember();p=self.packet()
        op=self.operation(p,scope='instagram',supersedes=None,epistemic='reported')
        docs.complete(c.cfg,self.response(p,[op]),c.clock)
        with memory.connection(c.cfg,True) as db:
            db.execute("UPDATE memories SET summary='같은 근거의 표현 정리',recorded_ms=recorded_ms+1")
        self.assertEqual(docs.prepare(c.cfg,c.clock)['reason'],'no_work')
        item=next(i for i in docs.view(c.cfg,c.clock)['items'] if i['scope']=='instagram')
        self.assertEqual(item['basis_validity'],'current')

    def test_more_self_replies_on_same_incoming_roots_do_not_rewake_editor(self):
        c=self.c;c.remember();p=self.packet();docs.complete(c.cfg,self.response(p),c.clock)
        c.ingest('echo',direction='outgoing',text='아까 말한 걸 다시 정리하면 이렇다',
                 observation={'identity':'platform','channel':'instagram'})
        p=memory.prepare(c.cfg,c.clock)['packet'];previous=p['existing_memories'][0]
        original=previous['evidence'][0]
        item=c.item(p,supersedes=previous['id'],evidence=[
            {'seq':original['seq'],'excerpt':original['excerpt']},
            {'seq':p['new_sources'][0]['seq'],'excerpt':p['new_sources'][0]['text']}])
        memory.complete(c.cfg,c.response(p,[item]),c.clock)
        self.assertEqual(docs.prepare(c.cfg,c.clock)['reason'],'no_work')
