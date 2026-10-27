#!/usr/bin/env python3
"""Offline recall evaluation for the episodic sidecar (retrieval step 0).

Runs labelled questions against a copy of the sidecar and reports whether the expected
memory keys land in the top k, for the current query builder and the earlier one
(`--compare-legacy`, query and ranking as of before 2026-10-26). Ranking is
episodic_memory.candidates; origin version checks and the
display byte budget are skipped, so this measures ranking only. Read-only.

Cases JSON:
  {"cases": [{"id": "duo-3h", "group": "hours", "channel": "instagram", "now_ms": 0,
              "targets": ["user DM being answered"],
              "messages": [{"direction": "incoming|outgoing", "text": "..."}],
              "expected_keys": ["event:duo"]}]}

Real cases and sidecar copies contain DM text: keep them outside Git (conversations/).
`--list --output FILE` writes the active memories (with summaries) to a private file to
help label expected_keys. That listing can stand in for the sidecar: pass the .jsonl
instead of the .sqlite3 and the ranking columns are rebuilt in memory.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'controller'))
import episodic_memory  # noqa: E402


def legacy_terms(targets, messages):
    """The query builder before 2026-10-26: own replies included, no filtering."""
    value = ' '.join(targets + [m.get('text', '') for m in messages[-6:][::-1]])
    words = re.findall(r'[\w가-힣]+', value.lower())
    terms = [w for w in words if len(w) >= 2]
    terms += [w[i:i+3] for w in words if len(w) > 3 for i in range(len(w)-2)]
    return list(dict.fromkeys(terms))[:32]


def legacy_candidates(db, channel, targets, messages, now, categories=None):
    """The ranking before 2026-10-26: equal-weight substring matches plus importance/2."""
    args = [channel, 'personal', now]
    where = "channel IN (?,?) AND active=1 AND status!='retracted' AND occurred_ms<=?"
    if categories:
        where += ' AND category IN (' + ','.join('?' for _ in categories) + ')'
        args.extend(categories)
    terms = legacy_terms(targets, messages)
    matched = episodic_memory.lookup_rows(db, where, args, terms, 128) if terms else []
    fallback = list(db.execute('SELECT * FROM memories WHERE ' + where + ''' AND
        (kind='working' OR status IN ('planned','ongoing') OR (importance>=4 AND occurred_ms>=?))
        ORDER BY (kind='working') DESC,importance DESC,recorded_ms DESC LIMIT 12''', args + [now - 86400000]))
    rows = {r['id']: r for r in matched + fallback}
    def score(row):
        return ((100 if row['kind'] == 'working' else 0) + row['importance'] / 2
                + sum(2 + min(len(t), 10) / 10 for t in terms if t in row['search_text']))
    return sorted(rows.values(), key=lambda r: (score(r), r['recorded_ms'], r['id']), reverse=True)


def open_sidecar(path):
    path = Path(path)
    if path.suffix == '.jsonl':
        return listing_sidecar(path)
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


def listing_sidecar(path):
    """In-memory sidecar from a --list file; search_text is rebuilt as the organizer stores it."""
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript(episodic_memory.SQL)
    for index, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines()):
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row['keywords'], str):  # listings before 2026-10-27 kept the stored JSON text
            row['keywords'] = json.loads(row['keywords'])
        search = ' '.join([row['summary'], *row['keywords'], row['category']]).lower()
        db.execute('INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (f'listed-{index}', row['key'], row['channel'], row['kind'], row['category'], row['status'],
                    row['importance'], row['summary'], json.dumps(row['keywords'], ensure_ascii=False), '[]', '',
                    row['occurred_ms'], row.get('recorded_ms', row['occurred_ms']), 1, None, search))
    return db


def ranked_keys(rows, k):
    keys = []
    for row in rows:
        if row['key'] not in keys:
            keys.append(row['key'])
        if len(keys) >= k:
            break
    return keys


def evaluate(db, case, k, legacy=False):
    targets, messages = case.get('targets', []), case.get('messages', [])
    if legacy:
        rows = legacy_candidates(db, case['channel'], targets, messages, case['now_ms'], case.get('categories'))
    else:
        rows = episodic_memory.candidates(db, case['channel'], episodic_memory.build_query(targets, messages),
                                          case['now_ms'], case.get('categories'))
    keys = ranked_keys(rows, k)
    expected = case['expected_keys']
    ranks = [keys.index(key) + 1 for key in expected if key in keys]
    return {'hit': bool(ranks), 'all': len(ranks) == len(expected), 'first_rank': min(ranks) if ranks else None}


def summarize(results):
    groups = {}
    for result in results:
        groups.setdefault(result['group'], []).append(result)
    groups['(all)'] = results
    return {name: {'cases': len(items), 'hit': sum(r['hit'] for r in items),
                   'all': sum(r['all'] for r in items)} for name, items in groups.items()}


def list_memories(db, output):
    rows = db.execute('''SELECT key,channel,kind,category,status,importance,occurred_ms,recorded_ms,summary,keywords
                         FROM memories WHERE active=1 ORDER BY channel,occurred_ms''').fetchall()
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        for row in rows:
            handle.write(json.dumps({**dict(row), 'keywords': json.loads(row['keywords'])},
                                    ensure_ascii=False) + '\n')
    return len(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Episodic recall evaluation')
    parser.add_argument('sidecar', help='copy of episodic-memory.sqlite3')
    parser.add_argument('--cases', help='labelled cases JSON')
    parser.add_argument('-k', type=int, default=6, help='top k (live max_items default 6)')
    parser.add_argument('--compare-legacy', action='store_true')
    parser.add_argument('--list', action='store_true', help='write active memories to --output')
    parser.add_argument('--output', help='private output file for --list')
    args = parser.parse_args(argv)
    db = open_sidecar(args.sidecar)
    if args.list:
        if not args.output:
            parser.error('--list requires --output')
        print(f'memories written: {list_memories(db, args.output)}')
        return 0
    if not args.cases:
        parser.error('--cases is required')
    cases = json.loads(Path(args.cases).read_text(encoding='utf-8-sig'))['cases']
    runs = [('current', False)] + ([('legacy', True)] if args.compare_legacy else [])
    for name, legacy in runs:
        results = []
        for case in cases:
            result = evaluate(db, case, args.k, legacy)
            results.append({'id': case['id'], 'group': case.get('group', 'default'), **result})
        print(f'== {name} (top {args.k})')
        for result in results:
            print(f"  {result['id']}: hit={result['hit']} all={result['all']} first_rank={result['first_rank']}")
        for group, stats in summarize(results).items():
            print(f"  [{group}] recall@{args.k} {stats['hit']}/{stats['cases']}, all-expected {stats['all']}/{stats['cases']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
