"""Offline recall evaluation on a synthetic sidecar; no production data."""
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sqlite3
import tempfile
import unittest

import memory_recall_eval as evaluation
import episodic_memory

NOW = 1_800_000_000_000


class MemoryRecallEvalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='recall-eval-', dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sidecar = self.root / 'sidecar.sqlite3'
        db = sqlite3.connect(self.sidecar)
        db.executescript(episodic_memory.SQL)
        for key, category, summary in (
                ('event:duo', 'shared_experience', '듀오 승급 같이 게임'),
                ('daily:noise', 'daily', '회사 화장실 생각 뜨거워 처음부터 끝까지 천천히 제대로 끊지 말해줘')):
            db.execute('INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (key + '-v1', key, 'instagram', 'episode', category, 'completed', 3, summary, '[]', '[]',
                        '', NOW - 7_200_000, NOW - 7_200_000, 1, None, summary))
        db.commit()
        db.close()
        own = '회사 화장실에서 생각하니까 뜨거워지잖아 처음부터 끝까지 천천히 제대로 봐 끊지 말고 계속 말해줘'
        self.cases = self.root / 'cases.json'
        self.cases.write_text(json.dumps({'cases': [{
            'id': 'duo', 'group': 'hours', 'channel': 'instagram', 'now_ms': NOW,
            'targets': ['듀오 승급 또 할래?'],
            'messages': [{'direction': 'outgoing', 'text': own}, {'direction': 'incoming', 'text': '듀오 승급 또 할래?'}],
            'expected_keys': ['event:duo']}]}, ensure_ascii=False), encoding='utf-8')

    def test_current_ranks_user_topic_first_and_legacy_does_not(self):
        db = evaluation.open_sidecar(self.sidecar)
        case = json.loads(self.cases.read_text(encoding='utf-8'))['cases'][0]
        self.assertEqual(evaluation.evaluate(db, case, 1)['first_rank'], 1)
        self.assertFalse(evaluation.evaluate(db, case, 1, legacy=True)['hit'])

    def test_report_and_private_listing(self):
        out = io.StringIO()
        with redirect_stdout(out):
            evaluation.main([str(self.sidecar), '--cases', str(self.cases), '-k', '1', '--compare-legacy'])
        self.assertIn('[hours] recall@1 1/1', out.getvalue())
        self.assertIn('== legacy', out.getvalue())
        listing = self.root / 'memories.jsonl'
        with redirect_stdout(io.StringIO()):
            evaluation.main([str(self.sidecar), '--list', '--output', str(listing)])
        self.assertEqual(listing.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(listing.read_text(encoding='utf-8').splitlines()), 2)
        # The listing stands in for the sidecar with the same ranking.
        out = io.StringIO()
        with redirect_stdout(out):
            evaluation.main([str(listing), '--cases', str(self.cases), '-k', '1'])
        self.assertIn('[hours] recall@1 1/1', out.getvalue())

    def test_listing_with_stored_keyword_text_is_accepted(self):
        listing = self.root / 'old.jsonl'
        listing.write_text(json.dumps({
            'key': 'event:duo', 'channel': 'instagram', 'kind': 'episode', 'category': 'shared_experience',
            'status': 'completed', 'importance': 3, 'occurred_ms': NOW - 1000, 'summary': '듀오 승급',
            'keywords': json.dumps(['듀오'], ensure_ascii=False)}, ensure_ascii=False) + '\n', encoding='utf-8')
        db = evaluation.open_sidecar(listing)
        case = json.loads(self.cases.read_text(encoding='utf-8'))['cases'][0]
        self.assertEqual(evaluation.evaluate(db, case, 1)['first_rank'], 1)


if __name__ == '__main__':
    unittest.main()
