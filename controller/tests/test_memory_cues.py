"""Real SQLite and mock generator checks for conversational callbacks, not live LLM quality."""
import json
import unittest

from common import ControlError,encode
import gate
import memory_cues
import memory_continuity
import prepared_inputs
import test_decision_session as lifecycle
import test_reply_generator as generator


class MemoryCueTests(unittest.TestCase):
    def setUp(self):
        self.c=lifecycle.DecisionSessionTests()
        self.c.setUp();self.addCleanup(self.c.tearDown)

    def add(self, fid, key, details=None, *, when=None, source='simulated'):
        c=self.c
        with c.store.connect(True) as db:
            db.execute('INSERT INTO facts VALUES (?,?,?,?,?,?,?)',
                       ('main',fid,c.clock if when is None else when,c.clock,source,key+' current topic',
                        encode(details or {})))

    def cue(self, channels=('slack',)):
        c=self.c
        with c.store.connect() as db:
            return memory_cues.snapshot(db,'main',c.clock,channels)

    def shared_joke(self, channel='slack', **changes):
        self.add('frame-9','topic_state:conversation:'+channel,{
            'channel':channel,'frame':'서로 취향 순위를 바꾸어 부르며 장난치는 중. 실제 서열이 아님.',
            'callbacks':['상대가 9순위라며 놀렸고 서로 장난으로 받아쳤음.'],
            'evidence_message_ids':['prior-joke'],**changes})

    def test_rank_callback_frame_survives_small_recent_window(self):
        c=self.c;self.shared_joke()
        c.ingest('s-new',text='그럼 넌 8순위',observation={'identity':'platform','channel':'slack'})
        c.recent(('s-new',),channel='slack')
        cache=c.state_dir/'recent_thread_slack.json'
        data=gate.load_json(cache,{})
        data['messages'][0]['text']='그럼 넌 8순위';gate.write_json(cache,data)
        before=c.snapshot()['revision']
        for mode in ('bundled','compact'):
            with self.subTest(mode=mode):
                p=c.runner.prepare(mode=mode)
                cue=p['memory_cues']['slack']
                self.assertIn('9순위',encode(cue['conversation_frame']))
                self.assertTrue(cue['conversation_frame']['context_only'])
                self.assertIsNone(p['memory_cues']['instagram']['conversation_frame'])
                self.assertEqual(cue['memory_mode'],'off')
        self.assertEqual(c.snapshot()['revision'],before)

    def test_topic_versions_are_collapsed_and_exact_keys_are_available(self):
        self.add('a','topic_state:overwatch',{'current':'old'})
        self.c.clock+=1
        self.add('b','topic_state:overwatch',{'current':'new'})
        self.add('c','topic_statex:not-topic')
        cue=self.cue()['slack']
        self.assertEqual(cue['topic_count'],1)
        self.assertEqual(cue['topics'],[{'key':'topic_state:overwatch','fact_id':'b'}])

    def test_topic_listing_reports_more_instead_of_pretending_complete(self):
        for i in range(9): self.add(f'f{i}',f'topic_state:subject{i}')
        cue=self.cue()['slack']
        self.assertEqual(len(cue['topics']),4)
        self.assertEqual(cue['topic_count'],9)
        self.assertTrue(cue['topics_has_more'])

    def test_other_channel_latest_version_does_not_revive_old_version(self):
        self.add('old','topic_state:game',{'channel':'slack'})
        self.c.clock+=1
        self.add('new','topic_state:game',{'channel':'instagram'})
        self.assertEqual(self.cue()['slack']['topics'],[])

    def test_frame_updates_and_end_of_joke_replace_the_prior_state(self):
        self.shared_joke()
        self.c.clock+=1
        self.add('frame-ended','topic_state:conversation:slack',{
            'channel':'slack','frame':'지금은 진지하게 다른 주제로 전환함.',
            'callbacks':[],'previous_id':'frame-9'})
        current=self.cue()['slack']['conversation_frame']
        self.assertEqual(current['fact_id'],'frame-ended')
        self.assertEqual(current['state']['callbacks'],[])
        self.assertNotIn('9순위',encode(current))

    def test_future_and_proposed_records_are_not_current_memory(self):
        self.add('future','topic_state:future',when=self.c.clock+1000)
        self.add('proposal','topic_state:proposal',source='proposed')
        self.assertEqual(self.cue()['slack']['topic_count'],0)

    def test_large_frame_keeps_whole_fields_and_stays_bounded(self):
        self.shared_joke(frame='x'*10000,callbacks=['small exact callback'])
        cue=self.cue(('slack','instagram'))
        frame=cue['slack']['conversation_frame']
        self.assertNotIn('frame',frame['state'])
        self.assertIn('frame',frame['omitted_fields'])
        self.assertEqual(frame['state']['callbacks'],['small exact callback'])
        self.assertLessEqual(memory_cues.size(cue),memory_cues.MAX_BYTES)

    def test_cues_do_not_add_wakes_or_make_memory_ops_mandatory(self):
        c=self.c;self.shared_joke()
        before=c.snapshot()
        c.runner.prepare(mode='compact')
        after=c.snapshot()
        self.assertEqual(before['revision'],after['revision'])
        self.assertEqual(before['needs_decision'],after['needs_decision'])
        self.assertNotIn('memory_cues',c.store.tick('main',c.clock,view='gate'))
        c.store.apply('main',c.decision(),c.clock)

    def test_shadow_policy_remains_shadow_and_does_not_expose_candidates(self):
        c=self.c
        memory_continuity.configure(c.store,'main',{'mode':'shadow'},c.clock)
        cue=self.cue()['slack']
        self.assertEqual(cue['memory_mode'],'shadow')
        self.assertNotIn('memory_context',cue)


class MemorySourceTests(unittest.TestCase):
    def setUp(self):
        self.c=generator.ReplyGeneratorTests()
        self.c.setUp();self.addCleanup(self.c.tearDown)

    def add_fact(self, fid='joke', channel=None):
        c=self.c
        details={'frame':'9순위는 서로 치던 드립이지 실제 서열이 아님.'}
        if channel: details['channel']=channel
        c.store.apply('main',c.decision(facts=[{
            'id':fid,'source':'simulated','occurred_ms':c.clock,
            'summary':'topic_state:conversation:instagram playful ranking','details':details}]),c.clock)

    def prepare(self, mode='bundled', queries=None):
        c=self.c
        c.prepared=c.runner.prepare(mode=mode,queries=queries)
        c.request['turn_id']=c.prepared['turn_id']
        return c.prepared

    def dynamic(self):
        return json.loads(self.c.calls[-1][0]['messages'][1]['content'])

    def test_shared_joke_reaches_generator_automatically_without_copying(self):
        c=self.c;self.add_fact();self.prepare()
        c.generate()
        self.assertIn('9순위',encode(self.dynamic()['conversation_frame']))
        self.assertEqual(self.dynamic()['memories'],c.request['memories'])

    def test_selected_bundled_recall_id_copies_exact_source(self):
        c=self.c;self.add_fact();self.prepare(queries=[['topic_state:conversation:instagram ']])
        c.generate({**c.request,'memory_fact_ids':['joke']})
        source=self.dynamic()['memory_sources'][0]
        self.assertEqual(source['id'],'joke')
        self.assertEqual(source['details']['frame'],'9순위는 서로 치던 드립이지 실제 서열이 아님.')
        self.assertEqual(source['source'],'simulated')

    def test_metadata_id_alone_is_not_a_full_fact_read(self):
        c=self.c;self.add_fact();self.prepare()
        with self.assertRaisesRegex(ControlError,'generator_memory_not_read'):
            c.generate({**c.request,'memory_fact_ids':['joke']})
        self.assertEqual(c.calls,[])

    def test_compact_recall_needs_its_actual_receipt(self):
        c=self.c;self.add_fact();p=self.prepare('compact')
        bundle=prepared_inputs.read_bundle(c.runner,p['turn_id'],p['input_id'])
        read=c.runner.read_input(p['turn_id'],p['input_id'],'recall',keywords=['topic_state:conversation:instagram '])
        request={**c.request,'memory_fact_ids':['joke']}
        with self.assertRaisesRegex(ControlError,'generator_memory_not_read'):
            c.generate(request,input_receipts=bundle['input_receipts'])
        c.generate(request,input_receipts=[*bundle['input_receipts'],read['receipt']])
        self.assertEqual(self.dynamic()['memory_sources'][0]['id'],'joke')

    def test_reprepare_does_not_reuse_old_read_ids(self):
        c=self.c;self.add_fact();self.prepare(queries=[['topic_state:conversation:instagram ']])
        self.prepare()
        with self.assertRaisesRegex(ControlError,'generator_memory_not_read'):
            c.generate({**c.request,'memory_fact_ids':['joke']})

    def test_explicit_other_channel_source_is_not_forwarded(self):
        c=self.c;self.add_fact(channel='slack');self.prepare(queries=[['topic_state:conversation:instagram ']])
        with self.assertRaisesRegex(ControlError,'generator_memory_channel_mismatch'):
            c.generate({**c.request,'memory_fact_ids':['joke']})
        self.assertEqual(c.calls,[])

    def test_channel_in_frame_key_is_respected_without_detail_metadata(self):
        c=self.c;self.add_fact()
        with c.store.connect() as db:
            with self.assertRaisesRegex(ControlError,'generator_memory_channel_mismatch'):
                memory_cues.sources(db,'main','slack',['joke'])

    def test_fixed_prefix_does_not_change_with_conversation_frame(self):
        c=self.c;c.generate()
        first=c.calls[-1][0]
        self.add_fact();self.prepare();c.generate()
        second=c.calls[-1][0]
        self.assertEqual(first['messages'][0],second['messages'][0])
        self.assertEqual(first['prompt_cache_key'],second['prompt_cache_key'])

    def remember_preference(self):
        c=self.c
        memory_continuity.configure(c.store,'main',{'mode':'enforce'},c.clock)
        c.store.apply('main',c.decision(memory_ops=[{
            'op':'remember','id':'pref','kind':'preference','key':'brief-explanations',
            'scope':'private_dm','channel':'instagram','summary':'short explanations',
            'scope_note':'only technical explanations',
            'evidence':[{'message_id':'m1','excerpt':'hello'}]}]),c.clock)

    def test_active_memory_context_can_be_selected_with_scope_and_provenance(self):
        c=self.c;self.remember_preference();self.prepare()
        c.generate({**c.request,'memory_fact_ids':['memory:pref']})
        source=self.dynamic()['memory_sources'][0]
        self.assertEqual(source['memory']['status'],'active')
        self.assertEqual(source['memory']['scope_note'],'only technical explanations')
        self.assertEqual(source['memory']['kind'],'preference')

    def test_compact_active_memory_context_read_is_usable(self):
        c=self.c;self.remember_preference();p=self.prepare('compact')
        bundle=prepared_inputs.read_bundle(c.runner,p['turn_id'],p['input_id'])
        c.generate({**c.request,'memory_fact_ids':['memory:pref']},input_receipts=bundle['input_receipts'])
        self.assertEqual(self.dynamic()['memory_sources'][0]['id'],'memory:pref')

    def test_revoked_memory_cannot_be_forwarded_as_active_source(self):
        c=self.c;self.remember_preference()
        c.store.apply('main',c.decision(memory_ops=[{'op':'revoke','id':'pref','reason':'withdrawn'}]),c.clock)
        with c.store.connect() as db:
            with self.assertRaisesRegex(ControlError,'generator_memory_source_unavailable'):
                memory_cues.sources(db,'main','instagram',['memory:pref'])

    def test_another_channel_cannot_select_the_active_memory(self):
        c=self.c;self.remember_preference()
        with c.store.connect() as db:
            with self.assertRaises(ControlError):
                memory_cues.sources(db,'main','slack',['memory:pref'])

    def test_new_source_ids_and_frame_are_not_copied_to_trace(self):
        c=self.c;self.add_fact();self.prepare(queries=[['topic_state:conversation:instagram ']])
        c.generate({**c.request,'memory_fact_ids':['joke']})
        logs=''.join(p.read_text() for p in c.state_dir.glob('decision-trace-*.jsonl'))
        self.assertNotIn('9순위',logs)
        self.assertNotIn('실제 서열',logs)


if __name__=='__main__': unittest.main()
