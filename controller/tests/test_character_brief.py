"""Real SQLite integration: pinned character documents through both input modes."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from common import ControlError,encode
import character_brief as brief
import character_documents as docs
import character_sources as sources
import gate
import muse_executor
import prepared_inputs
import reply_generator as gen
import test_character_documents as editing
import test_reply_generator as wording


class CharacterBriefTests(unittest.TestCase):
    def setUp(self):
        self.editor=editing.CharacterDocumentTests('test_empty_operations_acknowledge_work_without_creating_document_version')
        self.editor.setUp();self.addCleanup(self.editor.doCleanups)
        self.c=self.editor.c
        self.c.cfg['episodic_memory']={'mode':'off'}

    def activate(self):
        p,result=self.editor.publish()
        self.c.cfg['character_documents']['mode']='enforce'
        return result

    def prepare(self,mode='bundled'):
        c=self.c;c.ingest();c.recent();return c.runner.prepare(mode=mode)

    def generator(self,prepared,tokens=None,transport=None):
        c=self.c;rules=Path(c.tmp.name)/'rules.md';rules.write_text('FIXED_RULES_MARKER')
        c.cfg['reply_generator']={'enabled':True,'endpoint':'https://llm.example.test/v1/chat/completions','rules_file':str(rules)}
        req={'schema':'reply-gen-v1','turn_id':prepared['turn_id'],
             'channel':'instagram','situation':'rest','memories':[],'target_message_ids':['m1']}
        calls=[]
        def fake(body,*args):calls.append(body);return wording.response()
        result=gen.generate(c.runner,prepared['turn_id'],req,tokens,transport or fake)
        return result,calls

    def correct_source(self):
        with self.c.store.connect(True) as db:
            db.execute("UPDATE world_events SET payload_json=json_set(payload_json,'$.summary','제출을 취소하고 다시 작성 중이다') WHERE id='thesis'")

    def test_off_and_shadow_do_not_read_optional_store_or_change_input(self):
        c=self.c
        for mode in ('off','shadow'):
            c.cfg['character_documents']['mode']=mode
            with patch.object(brief,'consumer_templates',side_effect=AssertionError('optional read')):
                prepared=self.prepare()
            self.assertNotIn('character_brief',prepared['snapshot'])
        self.assertFalse(docs.settings(c.cfg)['path'].exists())

    def test_enforce_has_current_items_and_fixed_documents_without_original_assertions(self):
        result=self.activate();prepared=self.prepare();b=prepared['snapshot']['character_brief']
        self.assertEqual(b['revision'],result['revision'])
        self.assertNotIn('fixed_documents',b)
        item=next(i for i in b['channels']['instagram']['items'] if i['item_id']=='goals:thesis')
        self.assertIn('피드백',item['current_description'])
        profile=prepared['documents']['design/character/profile.md']
        self.assertNotIn('아직 오프라인에서 직접 만난 적은 없다.',profile)
        self.assertIn('고유 말투의 정본',profile)
        self.assertNotIn('겉으로는 사용자를 편한 게임 친구로 대하지만',prepared['documents']['CORE_RULES.md'])
        self.assertIn('관계를 특정 방향으로 유도하지 않고',prepared['documents']['CORE_RULES.md'])
        self.assertNotIn('core_rules',prepared['snapshot'])

    def test_generator_receives_the_same_revision_and_dynamic_items(self):
        self.activate();prepared=self.prepare();result,calls=self.generator(prepared)
        self.assertTrue(result['ok'])
        body=calls[0];fixed=body['messages'][0]['content'];dynamic=json.loads(body['messages'][1]['content'])
        self.assertEqual(dynamic['character_brief']['revision'],prepared['snapshot']['character_brief']['revision'])
        self.assertNotIn('피드백을 기다리는 중',fixed)
        self.assertNotIn('fixed_documents',dynamic['character_brief'])
        self.assertIn('피드백을 기다리는 중',encode(dynamic))
        self.assertIn('FIXED_RULES_MARKER',fixed)

    def test_compact_bundle_returns_pinned_brief_and_rendered_document_receipts(self):
        self.activate();p=self.prepare('compact');c=self.c
        self.assertIn('character_brief',p['input_manifest']['required_state_sections'])
        bundle=prepared_inputs.read_bundle(c.runner,p['turn_id'],p['input_id'])
        state=next(v['content'] for v in bundle['components'] if v['kind']=='state')
        self.assertEqual(state['character_brief'],c.runner.state()['character_selection']['brief'])
        core=next(v['content']['text'] for v in bundle['components'] if v['kind']=='document' and v['content']['name']=='CORE_RULES.md')
        self.assertNotIn('겉으로는 사용자를 편한 게임 친구로 대하지만',core)
        result,calls=self.generator(p,bundle['input_receipts']);self.assertTrue(result['ok'])
        self.assertEqual(json.loads(calls[0]['messages'][1]['content'])['character_brief']['revision'],state['character_brief']['revision'])
        prepared_inputs.check_documents(c.runner,c.runner.state()['prepared_document_hashes'])

    def test_compact_generator_requires_consumption_of_brief(self):
        self.activate();p=self.prepare('compact');c=self.c
        tokens=[c.runner.read_input(p['turn_id'],p['input_id'],'context',channel='instagram')['receipt']]
        with self.assertRaisesRegex(ControlError,'character_brief_input_required'):self.generator(p,tokens)

    def test_publication_does_not_replace_selected_version_until_next_turn(self):
        first=self.activate();p=self.prepare();c=self.c
        self.editor.put('career',summary='취업 준비를 잠시 보류했다')
        packet=self.editor.packet();op=self.editor.operation(packet)
        op['current_description']='논문 피드백을 받아 수정하는 중이다.'
        second=docs.complete(c.cfg,self.editor.response(packet,[op]),c.clock)
        self.assertNotEqual(first['revision'],second['revision'])
        again=c.runner.prepare(mode='bundled')
        self.assertEqual(again['snapshot']['character_brief']['revision'],first['revision'])
        result,calls=self.generator(again);self.assertTrue(result['ok'])
        self.assertEqual(json.loads(calls[0]['messages'][1]['content'])['character_brief']['revision'],first['revision'])
        state=c.runner.state();state['turn_id']=None;gate.write_json(c.runner.path,state)
        next_turn=c.runner.prepare(mode='bundled')
        self.assertEqual(next_turn['snapshot']['character_brief']['revision'],second['revision'])

    def test_source_correction_causes_one_reprepare_then_original_path_continues(self):
        self.activate();p=self.prepare();self.correct_source();c=self.c
        with self.assertRaisesRegex(ControlError,'character_reprepare_required'):
            prepared_inputs.fresh(c.runner,c.runner.state())
        again=c.runner.prepare(mode='bundled')
        self.assertEqual(again['turn_id'],p['turn_id'])
        self.assertNotIn('character_brief',again['snapshot'])
        self.assertNotIn('character_brief',self.generator(again)[1][0]['messages'][1]['content'])
        prepared_inputs.fresh(c.runner,c.runner.state())

    def test_source_correction_is_checked_at_bundled_commit_too(self):
        self.activate();p=self.prepare();c=self.c;decision=c.draft();self.correct_source()
        with self.assertRaisesRegex(ControlError,'character_reprepare_required'):
            c.runner.commit(p['turn_id'],decision)
        self.assertEqual(c.runner.state()['character_suspended_turn'],p['turn_id'])

    def test_optional_missing_corrupt_or_wrong_binding_store_falls_back(self):
        c=self.c;c.cfg['character_documents']['mode']='enforce'
        p=self.prepare();self.assertNotIn('character_brief',p['snapshot'])
        path=docs.settings(c.cfg)['path'];path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'broken sqlite')
        p=c.runner.prepare(mode='bundled');self.assertNotIn('character_brief',p['snapshot'])
        result,_=self.generator(p);self.assertTrue(result['ok'])

    def test_optional_outage_after_prepare_preserves_validated_turn_without_waiting(self):
        self.activate();p=self.prepare()
        with patch.object(sources,'revalidate',side_effect=OSError('temporary outage')):
            result,calls=self.generator(p)
        self.assertTrue(result['ok']);self.assertIn('character_brief',json.loads(calls[0]['messages'][1]['content']))

    def test_stale_item_is_omitted_without_reverting_it_to_seed(self):
        self.activate();self.correct_source();p=self.prepare();channel=p['snapshot']['character_brief']['channels']['instagram']
        self.assertNotIn('goals:thesis',[i['item_id'] for i in channel['items']])
        self.assertIn({'item_id':'goals:thesis','reason':'basis_changed'},channel['omitted'])
        self.assertNotIn('지도교수에게 초안을 보여 준다',p['documents']['design/character/goals.md'])

    def test_channel_override_does_not_leak_and_stale_override_never_resurrects_personal(self):
        self.activate();c=self.c
        self.editor.put('slack-event',summary='슬랙에서만 쓰는 관계 설명')
        p=self.editor.packet();op=self.editor.operation(p,item_id='relationship:description',section='relationship',scope='slack',supersedes=None,current_description='슬랙 전용 설명')
        docs.complete(c.cfg,self.editor.response(p,[op]),c.clock)
        selection,reason=brief.select(c.cfg,{'turn_id':'one'},['slack','instagram'],c.clock)
        self.assertIsNone(reason);b=selection['brief']
        self.assertIn('슬랙 전용 설명',encode(b['channels']['slack']))
        self.assertNotIn('슬랙 전용 설명',encode(b['channels']['instagram']))
        with c.store.connect(True) as db:db.execute("UPDATE world_events SET status='cancelled' WHERE id='slack-event'")
        selection,_=brief.select(c.cfg,{'turn_id':'two'},['slack','instagram'],c.clock)
        self.assertNotIn('relationship:description',[i['item_id'] for i in selection['brief']['channels']['slack']['items']])

    def test_budget_and_invalid_settings_fall_back_without_model_or_writes(self):
        self.activate();c=self.c;c.cfg['character_documents']['reply_bytes']=4096
        before=Path(c.cfg['db']).read_bytes();sidecar=docs.settings(c.cfg)['path'].read_bytes()
        p=self.prepare();self.assertNotIn('character_brief',p['snapshot'])
        # Preparation normally advances controller clocks; consumer selection itself never writes.
        before=Path(c.cfg['db']).read_bytes()
        selection,reason=brief.select(c.cfg,{'turn_id':'new'},['instagram'],c.clock)
        self.assertIsNone(selection);self.assertEqual(reason,'character_fixed_documents_too_large')
        self.assertEqual(before,Path(c.cfg['db']).read_bytes());self.assertEqual(sidecar,docs.settings(c.cfg)['path'].read_bytes())

    def test_same_template_keeps_generator_prefix_cache_across_document_versions(self):
        self.activate();p=self.prepare();_,calls=self.generator(p);a=calls[0]
        c=self.c;self.editor.put('new',summary='새 논문 수정 내용')
        packet=self.editor.packet();op=self.editor.operation(packet,current_description='수정본을 준비한다.')
        docs.complete(c.cfg,self.editor.response(packet,[op]),c.clock)
        state=c.runner.state();state['turn_id']=None;gate.write_json(c.runner.path,state)
        p=c.runner.prepare(mode='bundled');_,calls=self.generator(p);b=calls[0]
        self.assertEqual(a['messages'][0],b['messages'][0]);self.assertEqual(a['prompt_cache_key'],b['prompt_cache_key'])
        self.assertNotEqual(a['messages'][1],b['messages'][1])

    def test_caller_cannot_supply_their_own_brief_or_fixed_rules(self):
        self.activate();p=self.prepare();c=self.c
        rules=Path(c.tmp.name)/'rules.md';rules.write_text('FIXED')
        c.cfg['reply_generator']={'enabled':True,'endpoint':'https://llm.example.test/v1/chat/completions','rules_file':str(rules)}
        request={'schema':'reply-gen-v1','turn_id':p['turn_id'],'channel':'instagram',
                 'situation':'rest','target_message_ids':['m1'],
                 'character_brief':{'fixed_documents':{'design/character/profile.md':'EVIL_MARKER'}}}
        dynamic,_=gen.source(c.runner,c.runner.state(),request,None)
        self.assertNotIn('EVIL_MARKER',encode(dynamic))
        self.assertEqual(dynamic['character_brief']['revision'],p['snapshot']['character_brief']['revision'])

    def test_missing_optional_guidance_and_unavailable_editor_lock_do_not_block(self):
        self.activate();original=Path.read_text
        def missing(path,*a,**kw):
            if path.name=='character-brief.md':raise FileNotFoundError('guidance')
            return original(path,*a,**kw)
        with patch.object(Path,'read_text',missing):p=self.prepare()
        self.assertNotIn('character_brief',p['snapshot'])
        # Reader uses no editor file lock; a background pending model run is fine.
        p=self.c.runner.prepare(mode='bundled')
        with patch.object(docs,'file_lock',side_effect=AssertionError('editor lock acquired')):
            result,_=self.generator(p)
        self.assertTrue(result['ok'])

    def test_file_executor_prepares_and_recovers_with_new_brief_receipts(self):
        self.activate();c=self.c;c.ingest();c.recent()
        executor=muse_executor.MuseExecutor(c.runner)
        first=executor.prepare()
        text=Path(first['input_file']).read_text()
        self.assertIn('character_brief',text)
        self.assertNotIn('아직 오프라인에서 직접 만난 적은 없다.',text)
        self.correct_source()
        response={'request_id':first['request_id'],'input_sha256':first['input_sha256'],
                  'input_read':True,'action':'commit','decision':c.draft()}
        with self.assertRaisesRegex(ControlError,'character_reprepare_required'):executor.respond(response)
        self.assertIn('character_reprepare_required',muse_executor.REPREPARE)
        again=executor.prepare(retain_context=False)
        self.assertNotEqual(first['input_sha256'],again['input_sha256'])
        self.assertNotIn('character_brief',c.runner.state()['input_catalog']['state_sections'])
        response.update(request_id=again['request_id'],input_sha256=again['input_sha256'],decision=c.draft())
        with patch.object(c.runner,'finish',return_value={'next':'done'}):
            result=executor.respond(response)
        self.assertTrue(result['committed'])

    def test_both_cli_loaders_resolve_character_store_relative_to_config(self):
        c=self.c;cfg=Path(c.tmp.name)/'config.json'
        cfg.write_text(json.dumps({**c.cfg,'character_documents':{'mode':'enforce','path':'state/character.sqlite3'}}))
        actual=muse_executor.load_config(cfg)['character_documents']['path']
        self.assertEqual(Path(actual).resolve(),(cfg.parent/'state/character.sqlite3').resolve())

if __name__=='__main__':unittest.main()
