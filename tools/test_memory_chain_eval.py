"""Harness-only synthetic outputs; these are never recorded as real model trials."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run_memory_chain_eval as chain
import memory_query_contract as queries


class MemoryChainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='memory-chain-', dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / 'run'
        chain.prepare(self.run)
        self.serial = 0

    def submit(self, case, ops):
        self.serial += 1
        file = self.root / f'output-{self.serial}.json'
        chain.write(file, {'memory_ops': ops})
        return chain.submit(self.run, case, file, 'harness-no-model', {'model_called': False}, 'harness_test')

    def finish_empty(self, case):
        while chain.next_input(self.run, case).get('next') != 'question': self.submit(case, [])

    def query(self, case, operation='memory-recall', **arguments):
        chain.question(self.run, case)
        return chain.query(self.run, case, {'operation': operation, 'arguments': arguments})

    def test_preparation_and_model_input_have_no_expected_candidates_or_rubrics(self):
        for case in chain.read(self.run / 'reference.json'):
            body = chain.next_input(self.run, case['id'])['model_input']
            self.assertNotIn('rubric', body)
            self.assertNotIn('question', body)
            self.assertNotIn('expected', body)
            self.assertEqual(body['active_memory_context']['slack']['items'], [])
        self.assertFalse(chain.read(self.run / 'manifest.json')['expected_memory_rows_seeded'])

    def test_next_is_idempotent_and_modified_input_is_rejected(self):
        first = chain.next_input(self.run, 'nickname-multiturn')
        self.assertEqual(first, chain.next_input(self.run, 'nickname-multiturn'))
        chain.write(self.run / 'nickname-multiturn/input-0.json', {'changed': True})
        with self.assertRaisesRegex(ValueError, 'modified'):
            chain.next_input(self.run, 'nickname-multiturn')

    def test_real_ingest_apply_confirmed_projection_and_query_are_connected(self):
        case = 'source-channel'; chain.next_input(self.run, case)
        operation = {'op': 'remember', 'id': 'test-pref', 'kind': 'preference', 'key': 'comment-style',
                     'scope': 'private_dm', 'channel': 'instagram', 'summary': '댓글 형식 선호',
                     'scope_note': 'Instagram 댓글에 한함',
                     'evidence': [{'message_id': 'source-0', 'excerpt': '존댓말로 한 줄씩 답해줘'}]}
        applied = self.submit(case, [operation])
        self.assertTrue(applied['apply_ok'], applied)
        self.assertEqual(applied['memory_context']['instagram']['items'][0]['id'], 'test-pref')
        self.assertEqual(applied['memory_context']['slack']['items'], [])
        found = self.query(case, keywords=['존댓말'])
        self.assertTrue(found['ok'])
        self.assertIn('memory:test-pref', found['returned_ids'])
        self.assertEqual(self.query(case, 'recall', keywords=['존댓말'])['returned_ids'], [])

    def test_wrong_ops_are_not_repaired_and_reply_rolls_back(self):
        case = 'source-channel'; chain.next_input(self.run, case)
        result = self.submit(case, [{'op': 'remember', 'id': 'broken'}])
        self.assertFalse(result['apply_ok'])
        with chain.opened(self.run, case) as (_, path, state, store):
            self.assertEqual(store.tick('main', 1792368000000)['jobs'], [])
            self.assertEqual(chain.read(path / 'candidate-0.json')['memory_ops'], [{'op': 'remember', 'id': 'broken'}])
        self.assertEqual(chain.next_input(self.run, case)['next'], 'question')

    def test_unknown_receipt_never_activates_nickname(self):
        case = 'unknown-acceptance'; chain.next_input(self.run, case)
        operation = {'op': 'remember', 'id': 'unknown-nick', 'kind': 'nickname', 'key': 'nickname:은방울',
            'scope': 'private_dm', 'channel': 'slack', 'summary': '애칭 수락 후보', 'scope_note': '개인 대화',
            'evidence': [{'message_id': 'm0', 'excerpt': '은방울이라고 불러도 될까'}],
            'on_send': {'group_id': 'g0', 'part_index': 0, 'excerpt': '은방울이라고 불러도 좋아'}}
        result = self.submit(case, [operation])
        self.assertTrue(result['apply_ok'], result)
        self.assertEqual(result['memory_context']['slack']['items'], [])
        returned = self.query(case, 'memory-context', channel='slack', key='nickname:은방울', history=True)
        self.assertEqual(returned['result']['items'][0]['status'], 'pending')

    def test_history_query_discovers_an_opaque_revoked_key_then_reads_evidence(self):
        case='revoked-nickname';chain.next_input(self.run,case)
        operation={'op':'remember','id':'n1','kind':'nickname','key':'opaque:nickname-v1',
            'scope':'private_dm','channel':'slack','summary':'애칭을 수락함','scope_note':'개인 DM',
            'evidence':[{'message_id':'m0','excerpt':'솔방울이라고 불러도 돼?'}],
            'on_send':{'group_id':'g0','part_index':0,'excerpt':'솔방울이라고 불러도 좋아'}}
        self.assertTrue(self.submit(case,[operation])['apply_ok'])
        chain.next_input(self.run,case)
        self.assertTrue(self.submit(case,[{'op':'revoke','id':'n1','reason':'철회'}])['apply_ok'])
        found=self.query(case,'memory-history-search',channel='slack',keywords=['솔방울'])
        self.assertTrue(found['ok'],found)
        self.assertEqual(found['returned_ids'],['n1'])
        self.assertEqual(found['result']['items'][0]['status'],'revoked')
        history=self.query(case,'memory-context',channel='slack',
                           key=found['result']['items'][0]['key'],history=True)
        self.assertEqual(history['result']['items'][0]['excerpt'],'솔방울이라고 불러도 좋아')
        self.assertEqual(self.query(case,keywords=['솔방울'])['returned_ids'],[])

    def test_empty_ops_remain_missing_and_cannot_be_replaced_by_expected_database(self):
        case = 'source-channel'; self.finish_empty(case)
        self.assertEqual(self.query(case, keywords=['존댓말'])['returned_ids'], [])
        self.assertEqual(chain.report(self.run)['semantic_grading'], 'pending')

    def test_read_allowlist_and_failed_search_do_not_prove_absence(self):
        case = 'quotation-hypothesis'; self.finish_empty(case)
        self.assertFalse(self.query(case, 'memory-configure', settings={'mode': 'off'})['ok'])
        self.assertFalse(self.query(case, keywords=['별장'], limit=20)['ok'])
        response = self.root / 'answer.json'; chain.write(response, {'answer': '확인되지 않아.', 'found_ids': []})
        check = chain.answer(self.run, case, response, 'harness-no-model', {}, 'harness_test')
        self.assertFalse(check['query_evidence_verified'])
        self.assertFalse(check['universal_absence_proven'])
        self.assertEqual(check['failed_queries'], 2)

    def test_successful_empty_query_is_distinct_from_failure_and_answers_are_immutable(self):
        case = 'quotation-hypothesis'; self.finish_empty(case)
        self.assertTrue(self.query(case, keywords=['별장'])['ok'])
        response = self.root / 'answer.json'; chain.write(response, {'answer': '확인된 약속은 못 찾았어.', 'found_ids': []})
        result = chain.answer(self.run, case, response, 'harness-no-model', {}, 'harness_test')
        self.assertTrue(result['query_evidence_verified'])
        self.assertFalse(result['universal_absence_proven'])
        with self.assertRaises(FileExistsError):
            chain.answer(self.run, case, response, 'harness-no-model', {}, 'harness_test')

    def test_question_input_contains_no_rubric_or_source_operations(self):
        case = 'selected-utterance'; self.finish_empty(case)
        body = chain.question(self.run, case)['model_input']
        self.assertNotIn('semantic_rubric', body)
        self.assertNotIn('memory_ops', body)
        self.assertNotIn('source_messages', body)
        self.assertEqual(body['query_contract']['limit_max'], 10)

    def test_malformed_query_bytes_are_preserved_and_counted_as_error(self):
        case = 'quotation-hypothesis'; self.finish_empty(case); chain.question(self.run, case)
        raw = b'{invalid model query'
        result = chain.query(self.run, case, None, raw)
        self.assertFalse(result['ok'])
        self.assertEqual((self.run / case / 'query-request-0.json').read_bytes(), raw)
        self.assertEqual(chain.read(self.run / case / 'state.json')['query_count'], 1)

    def test_published_contract_requires_channel_and_all_examples_are_valid_reads(self):
        case='quotation-hypothesis';self.finish_empty(case)
        contract=chain.question(self.run,case)['model_input']['query_contract']
        self.assertEqual(contract['version'],2)
        spec=contract['operation_schemas']['memory-history-search']
        self.assertEqual(spec['required'],['channel','keywords'])
        self.assertEqual(spec['properties']['channel']['enum'],['slack','instagram'])
        self.assertNotIn('default',spec['properties']['channel'])
        for operation,schema in contract['operation_schemas'].items():
            with self.subTest(operation=operation):
                result=chain.query(self.run,case,schema['example'])
                self.assertTrue(result['ok'],result)

    def test_missing_channel_keeps_original_bytes_and_requires_an_explicit_retry(self):
        case='source-channel';chain.next_input(self.run,case)
        operation={'op':'remember','id':'pref','kind':'preference','key':'channel-style',
            'scope':'private_dm','channel':'instagram','summary':'댓글 형식 선호','scope_note':'인스타 댓글',
            'evidence':[{'message_id':'source-0','excerpt':'존댓말로 한 줄씩 답해줘'}]}
        self.assertTrue(self.submit(case,[operation])['apply_ok'])
        chain.question(self.run,case)
        request={'operation':'memory-history-search','arguments':{'keywords':['존댓말']}}
        raw=(chain.json.dumps(request,ensure_ascii=False,indent=4)+'\n').encode()
        with patch.object(chain.Store,'memory_history_search',side_effect=AssertionError('must not execute')):
            result=chain.query(self.run,case,request,raw)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'],'invalid_query_arguments')
        self.assertEqual(result['error_details']['missing_fields'],['channel'])
        self.assertEqual(result['error_details']['allowed_values']['channel'],['slack','instagram'])
        self.assertNotIn('channel',result['request']['arguments'])
        self.assertEqual((self.run/case/'query-request-0.json').read_bytes(),raw)
        corrected={**request,'arguments':{**request['arguments'],'channel':'instagram'}}
        found=chain.query(self.run,case,corrected)
        self.assertTrue(found['ok'],found)
        self.assertEqual(found['returned_ids'],['pref'])
        self.assertEqual(chain.read(self.run/case/'query-0.json')['error'],'invalid_query_arguments')
        self.assertEqual(chain.read(self.run/case/'state.json')['query_count'],2)

    def test_argument_errors_identify_invalid_values_and_keep_the_read_allowlist(self):
        case='quotation-hypothesis';self.finish_empty(case)
        invalid=[('memory-history-search',{'channel':'all','keywords':['약속']},'channel'),
                 ('memory-history-search',{'channel':None,'keywords':['약속']},'channel'),
                 ('memory-recall',{'keywords':['약속'],'limit':True},'limit'),
                 ('memory-context',{'channel':'slack','history':'true'},'history')]
        for operation,args,field in invalid:
            with self.subTest(operation=operation,args=args):
                result=self.query(case,operation,**args)
                self.assertEqual(result['error'],'invalid_query_arguments')
                self.assertIn(field,[f['field'] for f in result['error_details']['invalid_fields']])
        missing=self.query(case,'memory-context',channel='slack',history=True)
        self.assertEqual(missing['error_details']['missing_fields'],['key'])
        unknown=self.query(case,'memory-context',channel='slack',session='other')
        self.assertEqual(unknown['error_details']['unknown_fields'],['session'])
        forbidden=self.query(case,'memory-configure',settings={'mode':'off'})
        self.assertEqual(forbidden['error'],'unsupported_query_operation')
        self.assertEqual(set(forbidden['error_details']['allowed_operations']),set(queries.SCHEMAS))

    def test_contract_validation_does_not_inject_defaults_or_mutate_request(self):
        request={'operation':'memory-history-search','arguments':{'channel':'instagram','keywords':['설명']}}
        before=chain.json.dumps(request,sort_keys=True)
        operation,args=queries.validate(request)
        self.assertEqual(operation,'memory-history-search')
        self.assertEqual(chain.json.dumps(request,sort_keys=True),before)
        self.assertEqual(set(args),{'channel','keywords'})
        published=queries.contract()
        published['operation_schemas']['memory-history-search']['properties']['channel']['enum'].append('all')
        self.assertEqual(queries.contract()['operation_schemas']['memory-history-search']['properties']['channel']['enum'],
                         ['slack','instagram'])


if __name__ == '__main__':
    unittest.main()
