"""Injected experiences and real DM remain typed, scoped and origin-linked end to end."""
import json
from pathlib import Path
import sqlite3
import unittest

from common import ControlError
import episodic_memory as mem
import memory_origins as origins
import memory_continuity
import memory_organizer as worker
import test_episodic_memory as fixtures
from test_world_events import iso


class MemoryOriginTests(unittest.TestCase):
    def setUp(self):
        self.c=fixtures.EpisodicMemoryTests('test_no_controller_writes_and_idempotent_replay')
        self.c.setUp();self.addCleanup(self.c.tearDown)

    def event(self,eid='feedback',**changes):
        c=self.c
        item={'event_id':eid,'mode':'occurred','summary':'시안 글자가 작다는 피드백을 받음',
              'occurred_at':iso(c.clock),'known_by':['self'],'participants':['self'],
              'narrative_excerpt':'친구가 시안의 글자가 작다고 말했다.'}
        item.update(changes)
        if item['mode']!='occurred':
            item.pop('occurred_at',None);item.update(not_before=iso(c.clock),expires_at=iso(c.clock+3600000))
        return item

    def put(self,*events,request='import'):
        c=self.c
        return c.store.event_import('main',{'schema_version':1,'request_id':request,'events':list(events or [self.event()])},
                                    c.clock,'user:fixture-author')

    def packet(self):
        c=self.c;result=mem.prepare(c.cfg,c.clock)
        self.assertEqual(result['reason'],'prepared');return result['packet']

    def author_memory(self,packet):
        c=self.c;s=packet['new_sources'][0];eid=s['origin']['id']
        return c.item(packet,key=origins.event_key(eid),category='daily',summary='시안 글자 크기에 대한 피드백을 받은 경험',
                      interpretation='작가가 주입한 확정 시뮬레이션 사건')

    def finish_author(self):
        p=self.packet();mem.complete(self.c.cfg,self.c.response(p,[self.author_memory(p)]),self.c.clock);return p

    def refs(self,p,kind):return [r for r in p['reference_context']['items'] if r['kind']==kind]

    def assert_reason(self,reason,fn):
        with self.assertRaises(ControlError) as caught:fn()
        self.assertEqual(caught.exception.reason,reason)

    def test_injected_event_alone_is_processed_and_recalled_in_both_channels(self):
        c=self.c;self.put();before=Path(c.cfg['db']).read_bytes();p=self.finish_author()
        self.assertEqual(p['channel'],'personal');self.assertEqual(p['scope'],'personal')
        self.assertEqual(p['new_sources'][0]['source_kind'],'author_event')
        self.assertEqual(mem.prepare(c.cfg,c.clock)['reason'],'no_work')
        for channel in ('slack','instagram'):
            recall=mem.recall(c.cfg,channel,'시안 글자',c.clock)
            self.assertEqual(len(recall['items']),1);item=recall['items'][0]
            self.assertEqual(item['scope'],'personal');self.assertEqual(item['provenance'],'author_event')
            self.assertEqual(item['origin_refs'][0]['id'],'feedback')
            self.assertEqual(item['evidence'][0]['source_kind'],'author_event')
            mem.validate(c.cfg,{channel:recall},c.clock)
        self.assertEqual(Path(c.cfg['db']).read_bytes(),before)

    def test_replayed_import_never_creates_another_source_or_model_work(self):
        c=self.c;self.put();self.finish_author();self.put(request='replayed-request')
        self.assertEqual(mem.prepare(c.cfg,c.clock)['reason'],'no_work')
        with mem.connection(c.cfg) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sources').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM memories').fetchone()[0],1)

    def test_newly_published_old_event_uses_publication_bootstrap_boundary(self):
        c=self.c;mem.prepare(c.cfg,c.clock)
        self.put(self.event(occurred_at=iso(c.clock-90*86400000)))
        p=self.packet();self.assertEqual(p['new_sources'][0]['occurred_ms'],c.clock-90*86400000)

    def test_unknown_pending_cancelled_and_expired_events_never_enter_input(self):
        c=self.c
        self.put(self.event('hidden',known_by=['friend'],narrative_excerpt='hidden-secret'),
                 self.event('pending',mode='scene'),self.event('candidate',mode='candidate'))
        c.store.event_cancel('main','candidate',c.clock,'cancelled')
        self.assertEqual(mem.prepare(c.cfg,c.clock)['reason'],'no_work')
        c.ingest(text='시안은 어때?');p=self.packet()
        self.assertEqual(self.refs(p,'world_event'),[]);self.assertNotIn('hidden-secret',json.dumps(p))
        mem.complete(c.cfg,c.response(p),c.clock);c.clock+=3600001
        self.assertEqual(mem.prepare(c.cfg,c.clock)['reason'],'no_work')

    def test_scene_only_enters_after_realization_and_preserves_parent(self):
        c=self.c;self.put(self.event('parent'));self.finish_author()
        self.put(self.event('later',mode='scene',parent_event_ids=['parent']),request='scene')
        self.assertEqual(mem.prepare(c.cfg,c.clock)['reason'],'no_work')
        c.store.apply('main',c.decision(world_event_ops=[{'action':'realize','event_id':'later','reason':'scene happened'}]),c.clock)
        p=self.packet();self.assertEqual(p['new_sources'][0]['origin']['id'],'later')
        self.assertEqual(p['new_sources'][0]['origin']['data']['parent_event_ids'],['parent'])

    def test_followup_is_linked_and_replaces_only_this_channels_display(self):
        c=self.c;self.put();self.finish_author();c.ingest(text='그 시안 글자 수정은 끝났어?')
        p=self.packet();self.assertEqual(p['channel'],'instagram')
        self.assertIn('feedback',[r['id'] for r in self.refs(p,'world_event')])
        item=c.item(p,key='event:feedback',summary='시안 피드백의 후속 진행 상황을 DM으로 물음',
                    origin_refs=[{'kind':'world_event','id':'feedback'}])
        mem.complete(c.cfg,c.response(p,[item]),c.clock)
        local=mem.recall(c.cfg,'instagram','시안 글자',c.clock)['items']
        other=mem.recall(c.cfg,'slack','시안 글자',c.clock)['items']
        self.assertEqual(len(local),1);self.assertEqual(local[0]['channel'],'instagram')
        self.assertEqual(local[0]['origin_context'][0]['known_by'],['self'])
        self.assertEqual(local[0]['origin_context'][0]['source'],'author_event')
        self.assertEqual(len(other),1);self.assertEqual(other[0]['channel'],'personal')
        self.assertNotIn('후속 진행',other[0]['summary'])
        c.ingest('m2',text='응 이제 수정했어');p=self.packet()
        previous=next(m for m in p['existing_memories'] if m['key']=='event:feedback')
        item=c.item(p,key='event:feedback',summary='시안 수정 완료를 대화에서 보고함',supersedes=previous['id'])
        mem.complete(c.cfg,c.response(p,[item]),c.clock)
        local=mem.recall(c.cfg,'instagram','시안',c.clock)['items']
        self.assertEqual(len(local),1);self.assertEqual(local[0]['origin_refs'][0]['id'],'feedback')
        self.assertEqual(local[0]['summary'],'시안 수정 완료를 대화에서 보고함')

    def test_wrong_event_key_or_unread_origin_is_rejected(self):
        c=self.c;self.put();p=self.packet();item=self.author_memory(p);item['key']='duplicate:feedback'
        self.assert_reason('memory_origin_key_mismatch',lambda:mem.complete(c.cfg,c.response(p,[item]),c.clock))
        p=self.packet();item=self.author_memory(p);item['origin_refs']=[{'kind':'world_event','id':'hidden'}]
        self.assert_reason('memory_origin_not_read',lambda:mem.complete(c.cfg,c.response(p,[item]),c.clock))

    def test_long_event_id_has_a_valid_stable_memory_key(self):
        eid='a'*120;self.put(self.event(eid));p=self.finish_author()
        result=mem.recall(self.c.cfg,'slack','시안',self.c.clock)['items'][0]
        self.assertLessEqual(len(result['key']),100);self.assertEqual(result['origin_refs'][0]['id'],eid)

    def test_existing_fact_is_read_only_context_and_can_be_linked(self):
        c=self.c
        c.store.apply('main',c.decision(facts=[{'id':'hobby:book','source':'simulated','summary':'최근에 읽은 책은 은하수 여행',
                                             'details':{'genre':'science fiction'}}]),c.clock)
        c.ingest(text='그 은하수 여행 책 얘기 좀 해줘');p=self.packet()
        self.assertIn('hobby:book',[r['id'] for r in self.refs(p,'fact')])
        item=c.item(p,key='book:discussion',origin_refs=[{'kind':'fact','id':'hobby:book'}])
        mem.complete(c.cfg,c.response(p,[item]),c.clock)
        self.assertEqual(mem.recall(c.cfg,'instagram','듀오',c.clock)['items'][0]['origin_refs'][0]['kind'],'fact')

    def preference(self):
        c=self.c;c.ingest(text='이 채널에서는 그 호칭을 써도 돼')
        memory_continuity.configure(c.store,'main',{'mode':'enforce'},c.clock)
        c.store.apply('main',c.decision(memory_ops=[{'op':'remember','id':'pref','kind':'preference','key':'nickname',
            'scope':'private_dm','channel':'instagram','summary':'호칭 허용','scope_note':'이 채널에서만',
            'evidence':[{'message_id':'m1','excerpt':'호칭을 써도 돼'}]}]),c.clock)

    def revoke(self):
        c=self.c;c.store.apply('main',c.decision(memory_ops=[{'op':'revoke','id':'pref','reason':'사용자가 철회'}]),c.clock)

    def test_revoked_continuity_invalidates_pending_result_without_poisoning_source(self):
        c=self.c;self.preference();p=self.packet()
        self.assertEqual(self.refs(p,'continuity')[0]['data']['status'],'active')
        item=c.item(p,key='preference:context',origin_refs=[{'kind':'continuity','id':'pref'}]);self.revoke()
        self.assert_reason('memory_origins_changed',lambda:mem.complete(c.cfg,c.response(p,[item]),c.clock))
        with mem.connection(c.cfg) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM failures').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM quarantined').fetchone()[0],0)
        wait=mem.prepare(c.cfg,c.clock);self.assertEqual(wait['reason'],'retry_later');c.clock+=wait['retry_after_ms']
        p=self.packet();self.assertEqual(self.refs(p,'continuity')[0]['data']['status'],'revoked')

    def test_revoked_origin_hides_old_summary_and_selected_reply_requires_refresh(self):
        c=self.c;self.preference();p=self.packet()
        item=c.item(p,key='preference:context',origin_refs=[{'kind':'continuity','id':'pref'}])
        mem.complete(c.cfg,c.response(p,[item]),c.clock)
        old=mem.recall(c.cfg,'instagram','듀오',c.clock);self.assertEqual(len(old['items']),1)
        self.revoke();new=mem.recall(c.cfg,'instagram','듀오',c.clock)
        self.assertEqual(new['items'],[]);self.assertEqual(new['stale_origin_count'],1)
        self.assert_reason('memory_inputs_changed',lambda:mem.validate(c.cfg,{'instagram':old},c.clock))
        c.ingest('m2',text='아까 그 호칭 허용은 철회했어');p=self.packet()
        previous=p['existing_memories'][0]
        refreshed=c.item(p,key=previous['key'],summary='호칭 허용은 철회된 상태',supersedes=previous['id'])
        mem.complete(c.cfg,c.response(p,[refreshed]),c.clock)
        self.assertEqual(mem.recall(c.cfg,'instagram','호칭',c.clock)['items'][0]['summary'],'호칭 허용은 철회된 상태')

    def test_unrelated_new_event_does_not_invalidate_selected_memory(self):
        c=self.c;self.put();self.finish_author();old=mem.recall(c.cfg,'slack','시안',c.clock)
        self.put(self.event('unrelated',summary='커피 원두를 골랐다'),request='another')
        mem.validate(c.cfg,{'slack':old},c.clock)

    def test_channel_context_does_not_read_other_channel_or_off_policy(self):
        c=self.c;self.preference();p=self.packet();mem.complete(c.cfg,c.response(p),c.clock)
        c.ingest('slack:1',text='호칭에 대해',observation={'identity':'platform','channel':'slack','slack_ts':'1'})
        p=self.packet();self.assertEqual(self.refs(p,'continuity'),[]);mem.complete(c.cfg,c.response(p),c.clock)
        memory_continuity.configure(c.store,'main',{'mode':'shadow'},c.clock)
        c.ingest('m2',text='호칭');p=self.packet();self.assertEqual(self.refs(p,'continuity'),[])

    def test_file_executor_accepts_typed_author_output_end_to_end(self):
        c=self.c;self.put();c.cfg['episodic_memory']['model_adapter']='file'
        c.cfg['model_adapters']={'file':{'backend':'file'}}
        first=worker.run(c.cfg,lambda:c.clock);packet=json.loads(Path(first['input_file']).read_text())['input']
        response=c.response(packet,[self.author_memory(packet)])
        worker.private_write(first['response_file'],response)
        self.assertTrue(worker.run(c.cfg,lambda:c.clock)['ok'])
        self.assertEqual(worker.run(c.cfg,lambda:c.clock)['reason'],'no_work')

    def test_old_sidecar_upgrade_is_additive_and_does_not_reprocess_dm(self):
        c=self.c;c.ingest();p=self.packet();mem.complete(c.cfg,c.response(p),c.clock)
        with sqlite3.connect(mem.settings(c.cfg)['path']) as db:
            db.execute('DROP TABLE memory_origin_links');db.execute('DROP TABLE source_origins')
        self.assertEqual(mem.prepare(c.cfg,c.clock)['reason'],'no_work')
        self.put();self.assertEqual(self.packet()['channel'],'personal')


    def test_real_reply_prepare_and_generator_receive_personal_origin(self):
        import reply_generator
        c=self.c;self.put();self.finish_author();c.ingest(text='시안 글자 이야기를 다시 해줘');c.recent()
        p=c.runner.prepare(mode='bundled')
        item=p['snapshot']['episodic_memory']['instagram']['items'][0]
        self.assertEqual(item['origin_refs'][0]['id'],'feedback')
        request={'schema':'reply-gen-v1','turn_id':p['turn_id'],'channel':'instagram',
                 'situation':'시안 피드백','target_message_ids':['m1']}
        dynamic,_=reply_generator.source(c.runner,c.runner.state(p['turn_id']),request,None)
        self.assertEqual(dynamic['episodic_memory']['items'][0]['provenance'],'author_event')
        self.assertNotIn('reference_context',dynamic)

    def test_scopes_removed_from_policy_hide_linked_summary(self):
        c=self.c;self.preference();p=self.packet()
        item=c.item(p,key='preference:context',origin_refs=[{'kind':'continuity','id':'pref'}])
        mem.complete(c.cfg,c.response(p,[item]),c.clock)
        memory_continuity.configure(c.store,'main',{'scopes':['another_scope']},c.clock)
        self.assertEqual(mem.recall(c.cfg,'instagram','듀오',c.clock)['items'],[])
        c.ingest('m2',text='호칭 선호');p=self.packet()
        self.assertEqual(self.refs(p,'continuity'),[])
        previous=p['existing_memories'][0]
        item=c.item(p,key=previous['key'],status='retracted',supersedes=previous['id'])
        self.assertTrue(mem.complete(c.cfg,c.response(p,[item]),c.clock)['ok'])

    def test_reference_budget_marks_omission_without_truncating_or_blocking_dm(self):
        c=self.c;c.cfg['episodic_memory']['batch_bytes']=4096
        facts=[{'id':'large:'+str(i),'source':'simulated','summary':'책 이야기 '+str(i),
                'details':{'notes':'긴 내용 '*1000}} for i in range(3)]
        c.store.apply('main',c.decision(facts=facts),c.clock)
        c.ingest(text='책 이야기');p=self.packet()
        self.assertFalse(any(r['id'].startswith('large:') for r in p['reference_context']['items']))
        self.assertEqual(p['reference_context']['omitted'],3)
        self.assertEqual(p['new_sources'][0]['text'],'책 이야기')
        self.assertTrue(mem.complete(c.cfg,c.response(p),c.clock)['ok'])

    def test_old_sidecar_without_origin_tables_remains_readable_before_upgrade(self):
        c=self.c;c.remember()
        with sqlite3.connect(mem.settings(c.cfg)['path']) as db:
            db.execute('DROP TABLE memory_origin_links');db.execute('DROP TABLE source_origins')
        result=mem.recall(c.cfg,'instagram','듀오',c.clock)
        self.assertEqual(len(result['items']),1);self.assertNotIn('reason',result)
        mem.validate(c.cfg,{'instagram':result},c.clock)


    def test_personal_root_is_retained_if_channel_followup_exceeds_recall_budget(self):
        c=self.c;self.put();self.finish_author();c.ingest(text='시안 글자 수정 후속 질문');p=self.packet()
        item=c.item(p,key='event:feedback',origin_refs=[{'kind':'world_event','id':'feedback'}])
        mem.complete(c.cfg,c.response(p,[item]),c.clock)
        with mem.connection(c.cfg,True) as db:
            db.execute("UPDATE memories SET interpretation=? WHERE channel='instagram'",('very large '*1000,))
        result=mem.recall(c.cfg,'instagram','시안',c.clock)
        self.assertEqual(len(result['items']),1);self.assertEqual(result['items'][0]['channel'],'personal')


    def test_retracted_channel_followup_does_not_resurrect_personal_root(self):
        c=self.c;self.put();self.finish_author();c.ingest(text='시안 피드백 이야기 후속');p=self.packet()
        item=c.item(p,key='event:feedback',origin_refs=[{'kind':'world_event','id':'feedback'}])
        mem.complete(c.cfg,c.response(p,[item]),c.clock)
        c.ingest('m2',text='그 후속 내용은 철회할게');p=self.packet()
        previous=p['existing_memories'][0]
        item=c.item(p,key=previous['key'],status='retracted',supersedes=previous['id'])
        mem.complete(c.cfg,c.response(p,[item]),c.clock)
        self.assertEqual(mem.recall(c.cfg,'instagram','시안',c.clock)['items'],[])
        self.assertEqual(len(mem.recall(c.cfg,'slack','시안',c.clock)['items']),1)


if __name__=='__main__':unittest.main()
