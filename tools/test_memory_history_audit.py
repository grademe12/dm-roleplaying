"""Verify frozen prompt and replay checks using disposable synthetic output, never Muse originals."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import audit_memory_history_trial as audit
import run_memory_chain_eval as chain
import run_memory_history_eval as history


class MemoryHistoryAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='history-audit-test-',dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.trial=self.root/'trial'
        corpus=[c for c in history.memory_history_cases.cases() if c['id']=='history-quoted-no-commitment']
        for obj,name,value in [(chain,'memory_chain_cases',history.memory_history_cases),
                               (chain,'SOURCE_FILES',history.SOURCE_FILES)]:
            p=patch.object(obj,name,value);p.start();self.addCleanup(p.stop)
        p=patch.object(history.memory_history_cases,'cases',return_value=corpus)
        p.start();self.addCleanup(p.stop)
        chain.prepare(self.trial);self.case=corpus[0]['id'];self.path=self.trial/self.case
        history.freeze_round(self.trial,self.case,'candidate')
        candidate=self.root/'candidate.json';chain.write(candidate,{'memory_ops':[]})
        chain.submit(self.trial,self.case,candidate,'harness-no-model',{},'harness_test')
        chain.question(self.trial,self.case);history.freeze_round(self.trial,self.case,'query')
        chain.query(self.trial,self.case,{'operation':'memory-history-search',
            'arguments':{'channel':'slack','keywords':['약속']}})
        history.freeze_round(self.trial,self.case,'query')
        answer=self.root/'answer.json';chain.write(answer,{'answer':'확인 못 함','found_ids':[]})
        chain.answer(self.trial,self.case,answer,'harness-no-model',{},'harness_test')

    def test_replay_and_complete_prompts_preserve_original_package(self):
        before=audit.original_audit.hashes(self.trial)
        result=audit.audit(self.trial)
        self.assertEqual(result['frozen_prompt_count'],3)
        self.assertEqual(result['case_steps'],1)
        self.assertEqual(result['queries'],1)
        self.assertFalse(result['model_called'])
        self.assertFalse(result['original_database_opened_by_store'])
        self.assertEqual(audit.original_audit.hashes(self.trial),before)

    def test_changed_candidate_prompt_cannot_pass_with_valid_model_input(self):
        p=self.path/'candidate-prompt-0.json';value=chain.read(p)
        value['model_input']['instruction']='different instruction'
        chain.write(p,value)
        with self.assertRaises(AssertionError):audit.audit(self.trial)

    def test_changed_query_transcript_cannot_pass_with_unchanged_raw_query(self):
        p=self.path/'query-prompt-1.json';value=chain.read(p)
        value['transcript'][0]['result_utf8']='{"invented":true}'
        chain.write(p,value)
        with self.assertRaises(AssertionError):audit.audit(self.trial)


if __name__=='__main__':unittest.main()
