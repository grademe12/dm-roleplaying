"""Independent news collection inbox. Never changes a prepared reply's revision.

Network/model work belongs to a separately scheduled adapter. Publication happens
only at a decision owner's next prepare boundary; optional failures are isolated.
"""
from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import sqlite3
import uuid

from common import ControlError, decode, encode, file_lock, integer, require
import conversation_flow as flow

KST = timezone(timedelta(hours=9))
SUBJECTS = (
    ('games:overwatch', ('overwatch', '오버워치', '오버워치2'), '오버워치 2 공식 패치 노트 영웅 밸런스 소식'),
    ('games:stardew', ('stardew', '스타듀밸리', '스타듀 밸리'), '스타듀 밸리 공식 업데이트 소식'),
    ('culture:screen', ('영화', '드라마', 'screen'), '범죄 추리 영화 드라마 공식 공개 소식'),
    ('culture:music', ('음악', 'music'), '국내 인디 록 밴드 공식 공연 발매 소식'),
    ('life:food', ('음식', 'food'), '편의점 신상 음식 공식 출시 소식'),
    ('technology:gaming', ('게임장비', 'PC'), '게임 주변기기 공식 발표 소식'),
)
SCHEMA = '''
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, token TEXT NOT NULL,
 day_key TEXT NOT NULL, started_ms INTEGER NOT NULL, deadline_ms INTEGER NOT NULL,
 status TEXT NOT NULL, finished_ms INTEGER, task_json TEXT NOT NULL,
 result_digest TEXT, result_json TEXT, publication_json TEXT);
CREATE INDEX IF NOT EXISTS producer_session_time ON runs(session_id,started_ms);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, at_ms INTEGER NOT NULL,
 phase TEXT NOT NULL, reason TEXT NOT NULL, run_id TEXT, details_json TEXT NOT NULL);
'''
IMPORT_SQL = '''CREATE TABLE IF NOT EXISTS topic_producer_imports (
 session_id TEXT NOT NULL, run_id TEXT NOT NULL, result_digest TEXT NOT NULL,
 published_ms INTEGER NOT NULL, outcome_json TEXT NOT NULL,
 PRIMARY KEY(session_id,run_id))'''


def inbox_path(path):
    return Path(str(Path(path).resolve())+'.topic-producer.db')


@contextmanager
def inbox(path):
    target=inbox_path(path)
    fd=os.open(target,os.O_CREAT|os.O_RDWR,0o600);os.close(fd)
    with closing(sqlite3.connect(str(target),timeout=.1,isolation_level=None)) as db:
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(SCHEMA)
        db.execute('BEGIN IMMEDIATE')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise


@contextmanager
def core(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)) as db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN')
        yield db


def audit(db,session,now,phase,reason,run_id=None,**details):
    db.execute('INSERT INTO events(session_id,at_ms,phase,reason,run_id,details_json) VALUES (?,?,?,?,?,?)',
               (session,now,phase,reason,run_id,encode(details)))


def note(path,session,now,phase,reason,run_id=None,**details):
    try:
        with inbox(path) as db: audit(db,session,now,phase,reason,run_id,**details)
    except (OSError,sqlite3.Error):
        pass  # Diagnostics never change a reply or undo completed work.


def usage(path,session,day):
    """Read-only shared budget contribution for the legacy collector."""
    if not inbox_path(path).is_file(): return 0,None,False
    with closing(sqlite3.connect(inbox_path(path).as_uri()+'?mode=ro',uri=True,timeout=.1)) as db:
        count=db.execute('SELECT count(*) FROM runs WHERE session_id=? AND day_key=?',(session,day)).fetchone()[0]
        latest=db.execute('SELECT max(started_ms) FROM runs WHERE session_id=?',(session,)).fetchone()[0]
    return count,latest,True


def active(path,session,now):
    if not inbox_path(path).is_file(): return False
    with closing(sqlite3.connect(inbox_path(path).as_uri()+'?mode=ro',uri=True,timeout=.1)) as db:
        return db.execute("SELECT 1 FROM runs WHERE session_id=? AND status='started' AND deadline_ms>?",
                          (session,now)).fetchone() is not None


def subject_id(value):
    for sid,aliases,_ in SUBJECTS:
        if value==sid or value in aliases: return sid
    return None


def choose_task(db,source,session,now):
    history=list(db.execute('SELECT task_json,started_ms FROM runs WHERE session_id=? ORDER BY started_ms,id',(session,)))
    tasks=[decode(r['task_json']) for r in history]
    expired=[]
    for row in source.execute("SELECT id,subject_key,canonical_key FROM conversation_materials WHERE session_id=? AND kind='news' AND refresh_after_ms<=? ORDER BY checked_ms DESC LIMIT 32",(session,now)):
        if subject_id(row['subject_key']): expired.append(dict(row))
    # Leave every other run open to rotation so old articles cannot monopolize
    # the small budget. These are selection preferences, not rejection gates.
    if expired and (not tasks or tasks[-1]['strategy']!='refresh'):
        target=expired[0];sid=subject_id(target['subject_key']);strategy='refresh'
    else:
        target=None;strategy='rotate'
        sid=min(SUBJECTS,key=lambda s:(sum(t['subject_key']==s[0] for t in tasks),
                                       max((r['started_ms'] for r,t in zip(history,tasks) if t['subject_key']==s[0]),default=-1)))[0]
    _,aliases,query=next(s for s in SUBJECTS if s[0]==sid)
    # Only IDs of explicitly approved public topics: never private topic bodies
    # or a person's experience/progress as if it were a public news fact.
    refs=[]
    for alias in aliases:
        row=source.execute("SELECT id FROM facts WHERE session_id=? AND source<>'proposed' AND (summary=? OR summary LIKE ?) ORDER BY recorded_ms DESC,id DESC LIMIT 1",
                           (session,'topic_state:'+alias,'topic_state:'+alias+' %')).fetchone()
        if row:refs.append(row['id'])
    return {'subject_key':sid,'query':query,'strategy':strategy,'topic_state_ids':refs,
            'refresh_target':target,'instruction':'Search public sources and verify article bodies. These are candidate materials, not messages or personal experiences. No candidates is a valid outcome.'}


def claim(store,session,now,job_id=None):
    """No DM/dispatch/activity/lease checks, no gate claim and no core writes."""
    integer(now,'now_ms')
    job_id=job_id or 'claim-'+uuid.uuid4().hex
    try:
        with file_lock(store.path,'topic_search_budget'), core(store.path) as source, inbox(store.path) as db:
            require(source.execute('SELECT 1 FROM sessions WHERE id=?',(session,)).fetchone() is not None,'session_not_found')
            cfg=flow.policy(source,session)
            db.execute("UPDATE runs SET status='expired',finished_ms=? WHERE session_id=? AND status='started' AND deadline_ms<=?",(now,session,now))
            day=datetime.fromtimestamp(now/1000,KST).date().isoformat()
            local=db.execute('SELECT count(*) FROM runs WHERE session_id=? AND day_key=?',(session,day)).fetchone()[0]
            old=source.execute('SELECT count(*) FROM topic_search_runs WHERE session_id=? AND day_key=?',(session,day)).fetchone()[0]
            times=[source.execute('SELECT max(started_ms) FROM topic_search_runs WHERE session_id=?',(session,)).fetchone()[0],
                   db.execute('SELECT max(started_ms) FROM runs WHERE session_id=?',(session,)).fetchone()[0]]
            latest=max((t for t in times if t is not None),default=None)
            used=local+old
            running=(db.execute("SELECT 1 FROM runs WHERE session_id=? AND status='started' AND deadline_ms>?",(session,now)).fetchone() is not None
                     or source.execute("SELECT 1 FROM topic_search_runs WHERE session_id=? AND status='started' AND expires_ms>?",(session,now)).fetchone() is not None)
            reason=('topic_search_disabled' if not (cfg['search_enabled'] and cfg['search_tool_verified']) else
                    'topic_search_already_active' if running else
                    'topic_search_daily_budget' if used>=cfg['search_daily_limit'] else
                    'topic_search_cooldown' if latest is not None and latest+cfg['search_interval_ms']>now else None)
            midnight=datetime.fromtimestamp(now/1000,KST).replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)
            next_ms=max(latest+cfg['search_interval_ms'] if latest is not None else now,
                        int(midnight.timestamp()*1000) if used>=cfg['search_daily_limit'] else now)
            if reason:
                audit(db,session,now,'claim',reason,attempts_today=used,job_id=job_id)
                return {'ok':True,'claimed':False,'reason':reason,'next_search_ms':next_ms,'job_id':job_id}
            rid='producer-'+uuid.uuid4().hex;token=uuid.uuid4().hex;deadline=now+cfg['search_limit_ms']
            task=choose_task(db,source,session,now)
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (rid,session,token,day,now,deadline,'started',None,encode(task),None,None,None))
            audit(db,session,now,'claim','topic_producer_claimed',rid,subject_key=task['subject_key'],deadline_ms=deadline,job_id=job_id)
            return {'ok':True,'claimed':True,'reason':'topic_producer_claimed','run_id':rid,'token':token,
                    'started_ms':now,'deadline_ms':deadline,'max_materials':cfg['search_max_materials'],
                    'attempts_today':used+1,'task':task,'job_id':job_id}
    except (ControlError,OSError,sqlite3.Error) as exc:
        reason=exc.reason if isinstance(exc,ControlError) else 'topic_producer_storage_unavailable'
        note(store.path,session,now,'claim',reason,job_id=job_id)
        return {'ok':False if reason=='session_not_found' else True,'claimed':False,'reason':reason,'job_id':job_id}


def result(store,session,doc,now):
    flow.obj(doc,{'run_id','token','status','facts','materials'},{'run_id','token','status'})
    require(doc['status'] in ('success','no_candidates','failed','timeout'),'topic_search_result_invalid')
    digest=hashlib.sha256(encode(doc).encode()).hexdigest()
    with inbox(store.path) as db:
        row=db.execute('SELECT * FROM runs WHERE session_id=? AND id=?',(session,doc['run_id'])).fetchone()
        require(row is not None and row['token']==doc['token'],'topic_search_token_invalid')
        if row['result_digest']:
            require(row['result_digest']==digest,'topic_search_result_collision')
            return {'ok':True,'reason':'topic_producer_result_already_recorded','status':row['status']}
        # Late results still get an honest terminal record, never ready material.
        status=('expired' if now>row['deadline_ms'] or row['status']=='expired' else
                'ready' if doc['status']=='success' else doc['status'])
        facts=doc.get('facts',[]);materials=doc.get('materials',[])
        require(isinstance(facts,list) and len(facts)<=30 and isinstance(materials,list),'topic_search_result_invalid')
        if doc['status']=='success':
            require(0<len(materials)<=10,'topic_search_material_limit')
        else:
            require(not facts and not materials,'topic_search_failure_has_materials')
        # Token is a capability for the local result call, never persisted in
        # exported event logs or passed to the network adapter.
        payload={k:v for k,v in doc.items() if k not in ('token','run_id')}
        db.execute('UPDATE runs SET status=?,finished_ms=?,result_digest=?,result_json=? WHERE id=?',
                   (status,now,digest,encode(payload) if status=='ready' else None,row['id']))
        audit(db,session,now,'result','topic_producer_'+status,row['id'],candidate_count=len(materials),elapsed_ms=now-row['started_ms'])
        return {'ok':True,'reason':'topic_producer_result_recorded','status':status,'run_id':row['id']}


def inspect(store,session,now,limit=20):
    integer(limit,'limit',1);require(limit<=100,'limit_too_large')
    with inbox(store.path) as db:
        rows=db.execute('SELECT * FROM runs WHERE session_id=? ORDER BY started_ms DESC,id LIMIT ?', (session,limit)).fetchall()
        events=db.execute('SELECT at_ms,phase,reason,run_id,details_json FROM events WHERE session_id=? ORDER BY id DESC LIMIT ?', (session,limit)).fetchall()
        return {'ok':True,'reason':'topic_producer_inspect','runs':[
            {'run_id':r['id'],'status':'expired' if r['status']=='started' and r['deadline_ms']<now else r['status'],
             'started_ms':r['started_ms'],'deadline_ms':r['deadline_ms'],'finished_ms':r['finished_ms'],
             'task':decode(r['task_json']),'publication':decode(r['publication_json']) if r['publication_json'] else None} for r in rows],
            'events':[{**{k:r[k] for k in ('at_ms','phase','reason','run_id')},'details':decode(r['details_json'])} for r in events]}


def publish_pending(store,session,now):
    """Call only inside the decision owner lock, BEFORE preparing any inputs.

    A journal in the core transaction makes cross-DB acknowledgement retry safe.
    Invalid candidates use savepoints, so they cannot roll back unrelated news,
    and no collector validation failure can reject a normal reply.
    """
    try:
        if not inbox_path(store.path).is_file():return {'published':0,'runs':0}
        with inbox(store.path) as cache:
            rows=cache.execute("SELECT * FROM runs WHERE session_id=? AND status='ready' ORDER BY started_ms DESC,id LIMIT 2",(session,)).fetchall()
        total=0
        for run in rows:
            with store.connect(True,operation='topic_producer_publish',session=session,busy_timeout_ms=0) as db:
                db.execute(IMPORT_SQL)
                previous=db.execute('SELECT result_digest,outcome_json FROM topic_producer_imports WHERE session_id=? AND run_id=?',(session,run['id'])).fetchone()
                if previous:
                    require(previous['result_digest']==run['result_digest'],'topic_producer_import_collision')
                    outcome=decode(previous['outcome_json'])
                else:
                    cfg=flow.policy(db,session);payload=decode(run['result_json']);accepted=[];excluded=[]
                    facts={f['id']:f for f in payload.get('facts',[]) if isinstance(f,dict) and isinstance(f.get('id'),str)}
                    enabled=cfg['search_enabled'] and cfg['search_tool_verified']
                    for index,material in enumerate(payload.get('materials',[])):
                        db.execute('SAVEPOINT producer_candidate')
                        try:
                            require(enabled,'topic_search_disabled')
                            require(len(set(accepted))<cfg['search_max_materials'],'topic_search_material_limit')
                            require(isinstance(material,dict) and material.get('kind')=='news','topic_search_news_only')
                            # Subject rotation is guidance, not another rejection
                            # gate for otherwise valid, source-verified public news.
                            checked=material.get('checked_ms');refresh=material.get('refresh_after_ms',checked+cfg['material_fresh_ms'] if type(checked) is int else None)
                            require(type(checked) is int and run['started_ms']<=checked<=run['finished_ms'],'topic_check_time_mismatch')
                            require(type(refresh) is int and refresh>now,'conversation_material_stale')
                            flow.strings(material.get('fact_ids'))
                            for fid in material['fact_ids']:
                                fact=facts.get(fid)
                                require(isinstance(fact,dict) and fact.get('source')=='observed','topic_news_requires_observed')
                                require(isinstance(fact.get('details'),dict) and fact['details'].get('checked_ms')==checked,'topic_check_time_mismatch')
                                store._fact(db,session,fact,now)
                            accepted.append(flow.register_material(db,session,material,now))
                        except (ControlError,ValueError,TypeError,KeyError,OverflowError,sqlite3.IntegrityError) as exc:
                            db.execute('ROLLBACK TO producer_candidate')
                            excluded.append({'index':index,'reason':exc.reason if isinstance(exc,ControlError) else 'topic_producer_invalid_candidate'})
                        finally:
                            db.execute('RELEASE producer_candidate')
                    outcome={'material_ids':sorted(set(accepted)),'excluded':excluded,
                             'status':'published' if accepted else 'discarded'}
                    db.execute('INSERT INTO topic_producer_imports VALUES (?,?,?,?,?)',
                               (session,run['id'],run['result_digest'],now,encode(outcome)))
                    if accepted:db.execute('UPDATE sessions SET revision=revision+1 WHERE id=?',(session,))
            total+=len(outcome['material_ids'])
            with inbox(store.path) as cache:
                cache.execute('UPDATE runs SET status=?,publication_json=? WHERE id=?', (outcome['status'],encode(outcome),run['id']))
                audit(cache,session,now,'publish','topic_producer_'+outcome['status'],run['id'],**outcome)
        return {'published':total,'runs':len(rows)}
    except Exception:  # Optional collection must never abort reply preparation.
        note(store.path,session,now,'publish','topic_producer_publish_deferred')
        return {'published':0,'deferred':True}
