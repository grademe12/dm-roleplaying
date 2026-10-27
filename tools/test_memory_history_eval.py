"""Offline prompt preservation and isolation checks, with no model calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run_memory_chain_eval as chain
import run_memory_history_eval as history


class MemoryHistoryEvalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='history-eval-',dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        for name,value in [('memory_chain_cases',history.memory_history_cases),('SOURCE_FILES',history.SOURCE_FILES)]:
            patcher=patch.object(chain,name,value);patcher.start();self.addCleanup(patcher.stop)
        self.root=Path(self.temp.name);self.run=self.root/'run'
        chain.prepare(self.run)
        self.case='history-revoked-alias'

    def finish_empty(self):
        while chain.next_input(self.run,self.case).get('next')!='question':
            response=self.root/'candidate.json';chain.write(response,{'memory_ops':[]})
            chain.submit(self.run,self.case,response,'harness-no-model',{},'harness_test')
        chain.question(self.run,self.case)

    def test_new_variants_are_empty_and_frozen_extraction_prompt_excludes_rubrics(self):
        self.assertEqual(len(chain.read(self.run/'reference.json')),5)
        frozen=history.freeze_round(self.run,self.case,'candidate')
        path=Path(frozen['path']);body=chain.read(path)
        self.assertNotIn('question',body['model_input'])
        self.assertNotIn('rubric',path.read_text())
        self.assertEqual(body['model_input']['active_memory_context']['slack']['items'],[])
        self.assertEqual(frozen,history.freeze_round(self.run,self.case,'candidate'))
        with chain.opened(self.run,self.case) as (_,_,_,store):
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM continuity_memories').fetchone()[0],0)

    def test_frozen_round_contains_complete_raw_query_and_result_with_constant_wrapper(self):
        self.finish_empty()
        first=chain.read(Path(history.freeze_round(self.run,self.case,'query')['path']))
        request={'operation':'memory-history-search','arguments':{'channel':'slack','keywords':['별무리']}}
        raw=(json.dumps(request,ensure_ascii=False,indent=4)+'\n').encode('utf-8')
        chain.query(self.run,self.case,request,raw)
        frozen=history.freeze_round(self.run,self.case,'query');path=Path(frozen['path'])
        body=chain.read(path)
        self.assertEqual(body['wrapper'],first['wrapper'])
        self.assertEqual(body['transcript'][0]['request_utf8'].encode(),raw)
        self.assertEqual(body['transcript'][0]['result_utf8'],(self.run/self.case/'query-0.json').read_text())
        self.assertNotIn('rubric',path.read_text())
        self.assertEqual(chain.digest(path.read_bytes()),frozen['sha256'])

    def test_modified_round_is_rejected_and_empty_extraction_stays_empty(self):
        self.finish_empty()
        path=Path(history.freeze_round(self.run,self.case,'query')['path'])
        path.write_bytes(path.read_bytes()+b'\n')
        with self.assertRaisesRegex(ValueError,'differs'):
            history.freeze_round(self.run,self.case,'query')
        result=chain.query(self.run,self.case,{'operation':'memory-history-search',
            'arguments':{'channel':'slack','keywords':['별무리']}})
        self.assertEqual(result['returned_ids'],[])


if __name__=='__main__':unittest.main()
