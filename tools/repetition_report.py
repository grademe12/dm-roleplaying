#!/usr/bin/env python3
"""Repetition baseline report (design: operations/repetition-guard-plan-20261026.md).

Reads an operator transcript Markdown (`**HH:MM 화자:** 본문` lines) or a
recent_thread*.json cache, replays the persona's turns in order, and prints
per-turn overlap metrics plus the day's top repeated phrases, using the same
repetition_cues functions as the live guard. Read-only; real conversation
files stay outside Git (conversations/ is gitignored).
"""
import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'controller'))
import repetition_cues  # noqa: E402

LINE = re.compile(r'^\*\*(\d{1,2}:\d{2})\s+(.+?):\*\*\s*(.*)$')


def load_messages(path, speaker):
    if path.suffix == '.json':
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        messages = []
        for item in data.get('messages', []):
            direction = item.get('direction')
            if direction not in ('incoming', 'outgoing'):
                direction = 'outgoing' if item.get('speaker') == speaker else 'incoming'
            messages.append({'direction': direction, 'text': item.get('text') or ''})
        return messages
    messages = []
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        match = LINE.match(line.strip())
        if not match:
            continue
        _, name, text = match.groups()
        messages.append({'direction': 'outgoing' if name == speaker else 'incoming',
                         'text': text})
    return messages


def main(argv=None):
    parser = argparse.ArgumentParser(description='Reply repetition baseline report')
    parser.add_argument('input', help='transcript .md or recent_thread .json')
    parser.add_argument('--speaker', required=True, help='persona speaker name (transcript) ')
    parser.add_argument('--recent-turns', type=int, default=repetition_cues.DEFAULTS['recent_turns'])
    parser.add_argument('--min-turns', type=int, default=repetition_cues.DEFAULTS['min_turns_for_phrase'])
    parser.add_argument('--phrase-hit-turns', type=int, default=repetition_cues.DEFAULTS['phrase_hit_turns'])
    args = parser.parse_args(argv)
    opts = {**repetition_cues.DEFAULTS, 'mode': 'observe',
            'recent_turns': args.recent_turns, 'min_turns_for_phrase': args.min_turns,
            'phrase_hit_turns': args.phrase_hit_turns}
    messages = load_messages(Path(args.input), args.speaker)
    turns = repetition_cues.build_turns(messages, 1_000_000)
    print(f'turns: {len(turns)}')
    for index, turn in enumerate(turns):
        window = turns[max(0, index - opts['recent_turns']):index]
        if not window:
            print(f'#{index + 1}: (기준 없음) chars={sum(len(b) for b in turn)}')
            continue
        check = repetition_cues.analyze_turns('report', window, opts)['check']
        metrics = repetition_cues.check_drafts(turn, check, opts)
        print(f"#{index + 1}: verbatim_max={metrics['verbatim_max']} "
              f"phrase_hits={metrics['phrase_hits']} "
              f"opener_repeat={metrics['opener_repeat']} closer_repeat={metrics['closer_repeat']} "
              f"chars={metrics['draft_chars']} prev_chars={metrics['prev_turn_chars']}")
    phrases = repetition_cues.repeated_phrases(turns, opts)
    print('top phrases:')
    for phrase in phrases:
        print(f"  {phrase['turns']} turns: {phrase['text']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
