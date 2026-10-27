"""New small trial and its auditor stay isolated and preserve diagnostic failures."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import audit_memory_history_trial as auditor
import run_memory_chain_eval as chain
import run_memory_history_eval as history


class MemoryQueryContractEvalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='query-contract-test-',dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.run=self.root/'run'

    def test_cli_prepares_two_fresh_cases_with_contract_source_hash_and_no_seeded_memory(self):
        script=Path(__file__).parent/'run_memory_query_contract_eval.py'
        subprocess.run([sys.executable,str(script),'prepare','--output',str(self.run)],
                       check=True,capture_output=True)
        with auditor.history_contract('query-contract'):
            cases=chain.read(self.run/'reference.json')
            self.assertEqual(len(cases),2)
            self.assertEqual(sum(len(c['events']) for c in cases),5)
            self.assertIn('tools/memory_query_contract.py',chain.read(self.run/'manifest.json')['source_sha256'])
            for case in cases:
                with chain.opened(self.run,case['id']) as (_,_,_,store):
                    with store.connect() as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM continuity_memories').fetchone()[0],0)
        result=subprocess.run([sys.executable,str(script),'freeze-round','--run',str(self.run),
            '--case',cases[0]['id'],'--stage','candidate'],check=True,capture_output=True,text=True)
        prompt=chain.read(Path(json.loads(result.stdout)['path']))
        self.assertNotIn('rubric',prompt['model_input'])
        self.assertNotIn('question',prompt['model_input'])

    def test_authored_failure_and_corrected_retry_replay_without_becoming_model_results(self):
        with auditor.history_contract('query-contract'):
            chain.prepare(self.run)
            for case in chain.read(self.run/'reference.json'):
                cid=case['id']
                for _ in case['events']:
                    history.freeze_round(self.run,cid,'candidate')
                    candidate=self.root/'candidate.json';chain.write(candidate,{'memory_ops':[]})
                    chain.submit(self.run,cid,candidate,'harness-no-model',{},'harness_test')
                chain.question(self.run,cid);history.freeze_round(self.run,cid,'query')
                bad=chain.query(self.run,cid,{'operation':'memory-history-search','arguments':{'keywords':['애칭']}})
                self.assertEqual(bad['error_details']['missing_fields'],['channel'])
                history.freeze_round(self.run,cid,'query')
                fixed=chain.query(self.run,cid,{'operation':'memory-history-search',
                    'arguments':{'keywords':['애칭'],'channel':'slack'}})
                self.assertTrue(fixed['ok']);self.assertEqual(fixed['returned_ids'],[])
                history.freeze_round(self.run,cid,'query')
                response=self.root/'answer.json';chain.write(response,{'answer':'확인 못 함','found_ids':[]})
                chain.answer(self.run,cid,response,'harness-no-model',{},'harness_test')
            before=auditor.original_audit.hashes(self.run)
            result=auditor.audit(self.run,profile='query-contract')
            self.assertEqual(result['case_steps'],5)
            self.assertEqual(result['queries'],4)
            self.assertFalse(result['model_called'])
            self.assertEqual(result['frozen_prompt_count'],11)
            self.assertEqual(auditor.original_audit.hashes(self.run),before)
            self.assertTrue(all(c['answer_checks'][0]['passed'] is None for c in chain.read(self.run/'reviews.json')))


if __name__=='__main__':unittest.main()
