"""Persistent, body-free wake hints. Local CLI may be exposed over restricted SSH.

This database owns no conversations, decisions, or send authority. A captured
offer fences ACKs to its binding, consumer, epoch and exact upper watermark.
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

from common import ControlError, integer, require, text


def timestamp(value):
    require(isinstance(value, str) and re.fullmatch(r'[0-9]{10,}\.[0-9]{6}', value),
            'invalid_slack_timestamp')
    return value


def binding(value):
    require(isinstance(value, dict) and set(value) == {'persona', 'workspace', 'channel'},
            'invalid_doorbell_binding')
    for part in value.values():
        require(isinstance(part, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', part),
                'invalid_doorbell_binding')
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


class Doorbell:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()

    def connect(self):
        # RPC never silently recreates a lost ledger (which would reset ACKs).
        require(self.path.is_file(), 'doorbell_not_initialized')
        db = sqlite3.connect(self.path.as_uri()+'?mode=rw', uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        return db

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE meta(epoch TEXT NOT NULL);
                CREATE TABLE hints(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    binding TEXT NOT NULL, event_id TEXT NOT NULL, message_ts TEXT,
                    detected_ms INTEGER NOT NULL, UNIQUE(binding,event_id));
                CREATE INDEX hints_binding ON hints(binding,seq);
                CREATE TABLE consumers(binding TEXT NOT NULL, consumer TEXT NOT NULL,
                    ack_seq INTEGER NOT NULL, PRIMARY KEY(binding,consumer));
                CREATE TABLE offers(id TEXT PRIMARY KEY, binding TEXT NOT NULL,
                    consumer TEXT NOT NULL, through_seq INTEGER NOT NULL,
                    created_ms INTEGER NOT NULL, acked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE health(binding TEXT PRIMARY KEY, at_ms INTEGER NOT NULL,
                    connected INTEGER NOT NULL, last_envelope_ms INTEGER);
            ''')
            db.execute('INSERT INTO meta VALUES (?)', (uuid.uuid4().hex,))
        return {'ok': True, 'reason': 'initialized'}

    def ring(self, target, event_id, message_ts=None, now=None):
        key = binding(target)
        require(len(text(event_id)) <= 180, 'invalid_doorbell_event')
        if message_ts is not None:
            timestamp(message_ts)
        now = time.time_ns()//1_000_000 if now is None else integer(now)
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO hints(binding,event_id,message_ts,detected_ms) VALUES (?,?,?,?) '
                       'ON CONFLICT(binding,event_id) DO NOTHING', (key,event_id,message_ts,now))
            seq = db.execute('SELECT seq FROM hints WHERE binding=? AND event_id=?', (key,event_id)).fetchone()[0]
        return {'ok': True, 'seq': seq}

    def heartbeat(self, target, connected, envelope_ms=None, now=None):
        require(type(connected) is bool, 'invalid_doorbell_health')
        if envelope_ms is not None:
            integer(envelope_ms)
        now = time.time_ns()//1_000_000 if now is None else integer(now)
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO health VALUES (?,?,?,?) ON CONFLICT(binding) DO UPDATE SET '
                       'at_ms=excluded.at_ms,connected=excluded.connected,'
                       'last_envelope_ms=COALESCE(excluded.last_envelope_ms,health.last_envelope_ms)',
                       (binding(target),now,int(connected),envelope_ms))

    def snapshot(self, target, consumer, now=None):
        key = binding(target)
        require(re.fullmatch(r'[A-Za-z0-9_-]{1,80}', text(consumer)), 'invalid_consumer')
        now = time.time_ns()//1_000_000 if now is None else integer(now)
        with closing(self.connect()) as db, db:
            # A write transaction also fences concurrent snapshots/ACKs.
            db.execute('BEGIN IMMEDIATE')
            epoch = db.execute('SELECT epoch FROM meta').fetchone()[0]
            ack = db.execute('SELECT ack_seq FROM consumers WHERE binding=? AND consumer=?',
                             (key,consumer)).fetchone()
            ack = ack[0] if ack else 0
            rows = db.execute('SELECT seq,message_ts,detected_ms FROM hints WHERE binding=? '
                              'AND seq>? ORDER BY seq LIMIT 101', (key,ack)).fetchall()
            selected = rows[:100]
            through = selected[-1]['seq'] if selected else ack
            offer = None
            if selected:
                # Reuse an outstanding offer instead of allocating one every poll.
                old = db.execute('SELECT id FROM offers WHERE binding=? AND consumer=? '
                                 'AND through_seq=? AND acked=0 LIMIT 1', (key,consumer,through)).fetchone()
                offer = old[0] if old else uuid.uuid4().hex
                if not old:
                    db.execute('INSERT INTO offers(id,binding,consumer,through_seq,created_ms) '
                               'VALUES (?,?,?,?,?)', (offer,key,consumer,through,now))
            health = db.execute('SELECT at_ms,connected,last_envelope_ms FROM health WHERE binding=?',
                                (key,)).fetchone()
        return {'ok': True, 'epoch': epoch, 'binding': target, 'consumer': consumer,
                'offer_id': offer, 'through_seq': through, 'ack_seq': ack,
                'hints': [dict(row) for row in selected], 'has_more': len(rows)>100,
                'health': dict(health) if health else None}

    def acknowledge(self, target, consumer, epoch, offer_id, through_seq):
        key = binding(target)
        integer(through_seq)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            require(db.execute('SELECT epoch FROM meta').fetchone()[0] == epoch, 'doorbell_epoch_changed')
            offer = db.execute('SELECT * FROM offers WHERE id=?', (offer_id,)).fetchone()
            require(offer is not None and offer['binding'] == key and offer['consumer'] == consumer
                    and offer['through_seq'] == through_seq, 'doorbell_ack_mismatch')
            db.execute('INSERT INTO consumers VALUES (?,?,?) ON CONFLICT(binding,consumer) '
                       'DO UPDATE SET ack_seq=max(ack_seq,excluded.ack_seq)', (key,consumer,through_seq))
            db.execute('UPDATE offers SET acked=1 WHERE id=?', (offer_id,))
        return {'ok': True, 'ack_seq': through_seq}

    def request(self, doc):
        require(isinstance(doc, dict), 'invalid_doorbell_request')
        op = doc.get('operation')
        allowed = {'snapshot': {'operation','binding','consumer'},
                   'ack': {'operation','binding','consumer','epoch','offer_id','through_seq'}}
        require(op in allowed and set(doc) == allowed[op], 'invalid_doorbell_request')
        if op == 'snapshot':
            return self.snapshot(doc['binding'], doc['consumer'])
        return self.acknowledge(doc['binding'],doc['consumer'],doc['epoch'],doc['offer_id'],doc['through_seq'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--init', action='store_true')
    args = parser.parse_args(argv)
    try:
        ledger = Doorbell(args.db)
        if args.init:
            result = ledger.initialize()
        else:
            raw = sys.stdin.read(16385)
            require(len(raw.encode('utf-8'))<=16384, 'doorbell_request_too_large')
            result = ledger.request(json.loads(raw))
    except (ControlError,OSError,ValueError,TypeError,KeyError,sqlite3.Error) as exc:
        result = {'ok': False, 'reason': exc.reason if isinstance(exc,ControlError) else 'doorbell_unavailable'}
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
