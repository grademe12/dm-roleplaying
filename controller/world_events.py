"""Structured event intake. No model, network, scheduler, or dispatch authority.

The database functions are shared by the two controllers; their transaction and
projection adapters remain local. Only explicit author intake can assert events.
"""
import hashlib
import re
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from common import ControlError, decode, encode, integer, require, text

MAX_BYTES, MAX_EVENTS, MAX_VISIBLE = 65536, 20, 8
PREFIX = 'world-event:'
SQL = (
    '''CREATE TABLE IF NOT EXISTS event_imports (
       session_id TEXT NOT NULL REFERENCES sessions(id), id TEXT NOT NULL,
       digest TEXT NOT NULL, source_kind TEXT NOT NULL, producer_id TEXT NOT NULL,
       instruction_ref TEXT, received_ms INTEGER NOT NULL, result_json TEXT NOT NULL,
       PRIMARY KEY(session_id,id))''',
    '''CREATE TABLE IF NOT EXISTS world_events (
       session_id TEXT NOT NULL REFERENCES sessions(id), id TEXT NOT NULL,
       request_id TEXT NOT NULL, digest TEXT NOT NULL, mode TEXT NOT NULL,
       status TEXT NOT NULL CHECK(status IN ('pending','occurred','rejected','cancelled','expired')),
       payload_json TEXT NOT NULL, occurred_ms INTEGER, not_before_ms INTEGER,
       expires_ms INTEGER, review_after_ms INTEGER, fact_id TEXT,
       created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
       PRIMARY KEY(session_id,id),
       FOREIGN KEY(session_id,request_id) REFERENCES event_imports(session_id,id))''',
    '''CREATE INDEX IF NOT EXISTS world_event_pending
       ON world_events(session_id,status,expires_ms,created_ms)''',
    '''CREATE INDEX IF NOT EXISTS world_event_recent
       ON world_events(session_id,status,occurred_ms DESC,id)''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS world_event_fact
       ON world_events(session_id,fact_id) WHERE fact_id IS NOT NULL''',
    '''CREATE TABLE IF NOT EXISTS world_event_transitions (
       session_id TEXT NOT NULL, event_id TEXT NOT NULL, sequence INTEGER NOT NULL,
       at_ms INTEGER NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL,
       PRIMARY KEY(session_id,event_id,sequence),
       FOREIGN KEY(session_id,event_id) REFERENCES world_events(session_id,id))''',
    '''CREATE TABLE IF NOT EXISTS world_event_shares (
       session_id TEXT NOT NULL, event_id TEXT NOT NULL, job_id TEXT NOT NULL,
       PRIMARY KEY(session_id,event_id,job_id),
       FOREIGN KEY(session_id,event_id) REFERENCES world_events(session_id,id),
       FOREIGN KEY(session_id,job_id) REFERENCES dm_jobs(session_id,id))''',
)
EVENT_FIELDS = {'event_id','mode','summary','occurred_at','not_before','expires_at',
                'participants','known_by','location','context_refs','parent_event_ids',
                'narrative_excerpt','source_ref','followup_suggestions','conditions',
                'experience','scene_scope'}
STATES = ('pending','occurred','rejected','cancelled','expired')


def initialize(db):
    for statement in SQL:
        db.execute(statement)


def obj(value, allowed, required=()):
    require(isinstance(value,dict) and set(value)<=set(allowed) and set(required)<=set(value),
            'event_invalid_fields')
    return value


def string(value, maximum=500):
    text(value,'event_text')
    require(len(value)<=maximum,'event_text_too_long')
    return value


def identifier(value):
    require(isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}',value),
            'event_invalid_id')
    return value


def strings(value, maximum=16):
    require(isinstance(value,list) and len(value)<=maximum,'event_invalid_list')
    result=[string(v,120) for v in value]
    require(len(set(result))==len(result),'event_duplicate_value')
    return result


def timestamp(value):
    require(isinstance(value,str) and len(value)<=40 and
            re.search(r'T.*(?:Z|[+-]\d\d:\d\d)$',value),'event_invalid_time')
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        require(parsed.utcoffset() is not None,'event_invalid_time')
        result=int(parsed.timestamp()*1000)
    except (ValueError,OverflowError,OSError):
        require(False,'event_invalid_time')
    return integer(result,'event_time')


def digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


@contextmanager
def event_error(index, eid):
    try:
        yield
    except ControlError as exc:
        safe_id=eid if isinstance(eid,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}',eid) else None
        raise ControlError(exc.reason,encode({'event_index':index,'event_id':safe_id,'code':exc.reason})) from exc


def normalize(document):
    obj(document,{'schema_version','request_id','events'},{'schema_version','request_id','events'})
    require(type(document['schema_version']) is int and document['schema_version']==1,'event_schema_version')
    require(len(encode(document).encode('utf-8'))<=MAX_BYTES,'event_input_too_large')
    identifier(document['request_id'])
    require(isinstance(document['events'],list) and 1<=len(document['events'])<=MAX_EVENTS,'event_batch_size')
    events=[]
    for index,raw in enumerate(document['events']):
        with event_error(index,raw.get('event_id') if isinstance(raw,dict) else None):
            obj(raw,EVENT_FIELDS,{'event_id','mode','summary','known_by'})
            e={**raw}
            identifier(e['event_id']); string(e['summary'])
            require(e['mode'] in ('occurred','scene','candidate'),'event_invalid_mode')
            e['known_by']=strings(e['known_by'])
            e['participants']=strings(e.get('participants',[]))
            e['parent_event_ids']=[identifier(v) for v in strings(e.get('parent_event_ids',[]))]
            for key,maximum in (('location',120),('narrative_excerpt',2000),('source_ref',500)):
                if key in e: string(e[key],maximum)
            e['experience']=e.get('experience','direct')
            e['scene_scope']=e.get('scene_scope','self_only')
            require(e['experience'] in ('direct','reported','internal'),'event_invalid_experience')
            require(e['scene_scope'] in ('self_only','user_scene'),'event_invalid_scope')
            e['followup_suggestions']=strings(e.get('followup_suggestions',[]),8)
            refs=e.setdefault('context_refs',[])
            require(isinstance(refs,list) and len(refs)<=16,'event_invalid_refs')
            for ref in refs:
                obj(ref,{'kind','id'},{'kind','id'})
                require(ref['kind'] in ('fact','activity','appointment','event'),'event_invalid_ref_kind')
                string(ref['id'],256)
            conditions=e.setdefault('conditions',{})
            obj(conditions,{'location','activity_id','category'})
            for value in conditions.values(): string(value,120)
            if e['mode']=='occurred':
                require('occurred_at' in e and not set(e)&{'not_before','expires_at'} and not conditions,
                        'event_invalid_occurrence')
                e['occurred_ms']=timestamp(e.pop('occurred_at'))
            else:
                require('occurred_at' not in e and 'not_before' in e and 'expires_at' in e,'event_time_window_required')
                e['not_before_ms']=timestamp(e.pop('not_before'))
                e['expires_ms']=timestamp(e.pop('expires_at'))
                require(e['not_before_ms']<e['expires_ms'],'event_invalid_window')
            events.append(e)
    require(len({e['event_id'] for e in events})==len(events),'event_duplicate_id')
    return {'schema_version':1,'request_id':document['request_id'],'events':events}


def state(db,session):
    row=db.execute('SELECT revision FROM sessions WHERE id=?',(session,)).fetchone()
    require(row is not None,'session_not_found')
    return row[0]


def get(db,session,eid):
    row=db.execute('SELECT * FROM world_events WHERE session_id=? AND id=?',(session,identifier(eid))).fetchone()
    require(row is not None,'world_event_not_found')
    return row


def transition(db,session,eid,now,action,reason):
    sequence=db.execute('SELECT COALESCE(max(sequence),0)+1 FROM world_event_transitions '
                        'WHERE session_id=? AND event_id=?',(session,eid)).fetchone()[0]
    db.execute('INSERT INTO world_event_transitions VALUES (?,?,?,?,?,?)',
               (session,eid,sequence,now,action,string(reason)))


def validate_refs(db,session,e):
    tables={'fact':'facts','activity':'activities','appointment':'appointments','event':'world_events'}
    for ref in e['context_refs']:
        require(db.execute('SELECT 1 FROM '+tables[ref['kind']]+' WHERE session_id=? AND id=?',
                           (session,ref['id'])).fetchone() is not None,'event_reference_missing')
    for parent in e['parent_event_ids']:
        row=get(db,session,parent)
        require(row['status']=='occurred','event_parent_not_occurred')
    if 'activity_id' in e['conditions']:
        require(db.execute('SELECT 1 FROM activities WHERE session_id=? AND id=?',
                           (session,e['conditions']['activity_id'])).fetchone() is not None,'event_reference_missing')


def validate_location(db,session,e,at):
    if e['experience']!='direct' or 'self' not in e['participants'] or not e.get('location'): return
    rows=db.execute("SELECT location FROM activities WHERE session_id=? AND status IN "
                    "('in_progress','completed','interrupted') AND start_ms<=? "
                    "AND COALESCE(actual_end_ms,planned_end_ms)>?",(session,at,at)).fetchall()
    for row in rows:
        require(row[0]=='unknown' or e['location']==row[0],'event_location_conflict')


def publish(db,session,e,at,now):
    require(at<=now,'event_future_occurrence')
    validate_location(db,session,e,at)
    fid=None
    # Off-screen author events exist in the world ledger, not in the persona's
    # personal facts. This also prevents older readers leaking them on rollback.
    if 'self' in e['known_by']:
        fid=PREFIX+e['event_id']
        require(db.execute('SELECT 1 FROM facts WHERE session_id=? AND id=?',(session,fid)).fetchone() is None,
                'event_fact_collision')
        details={**e,'kind':'world_event','world_event_id':e['event_id']}
        db.execute('INSERT INTO facts VALUES (?,?,?,?,?,?,?)',
                   (session,fid,at,now,'simulated','world_event:'+e['event_id']+' '+e['summary'],encode(details)))
    db.execute("UPDATE world_events SET status='occurred',occurred_ms=?,fact_id=?,updated_ms=? "
               'WHERE session_id=? AND id=?',(at,fid,now,session,e['event_id']))
    transition(db,session,e['event_id'],now,'occurred','explicit event occurrence')
    return fid


def import_batch(db,session,document,now,instruction_ref):
    """Author-only v1 intake; the automatic producer adapter is a later patch."""
    revision=state(db,session)
    instruction_ref=string(instruction_ref,500)
    doc=normalize(document)
    request_digest=digest({'document':doc,'instruction_ref':instruction_ref})
    old=db.execute('SELECT * FROM event_imports WHERE session_id=? AND id=?',(session,doc['request_id'])).fetchone()
    if old:
        require(old['digest']==request_digest,'event_request_collision')
        return {**decode(old['result_json']),'reason':'event_import_replayed','revision':revision},False
    db.execute('INSERT INTO event_imports VALUES (?,?,?,?,?,?,?,?)',
               (session,doc['request_id'],request_digest,'author','operator_cli',instruction_ref,now,'{}'))
    results=[]
    for index,e in enumerate(doc['events']):
        with event_error(index,e['event_id']):
            eid=e['event_id']; hashed=digest(e)
            old=db.execute('SELECT * FROM world_events WHERE session_id=? AND id=?',(session,eid)).fetchone()
            if old:
                require(old['digest']==hashed,'event_identity_collision')
                results.append({'event_id':eid,'status':old['status'],'fact_id':old['fact_id'],'replayed':True})
                continue
            validate_refs(db,session,e)
            require(e['scene_scope']=='user_scene' or 'user' not in e['participants'],'event_user_scope_required')
            if e['mode']=='occurred':
                require(e['occurred_ms']<=now,'event_future_occurrence')
            else:
                require(e['expires_ms']>now,'event_window_expired')
            db.execute('INSERT INTO world_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (session,eid,doc['request_id'],hashed,e['mode'],'pending',encode(e),None,
                        e.get('not_before_ms'),e.get('expires_ms'),e.get('not_before_ms'),None,now,now))
            transition(db,session,eid,now,'imported','author intake')
            fid=publish(db,session,e,e['occurred_ms'],now) if e['mode']=='occurred' else None
            results.append({'event_id':eid,'status':'occurred' if e['mode']=='occurred' else 'pending','fact_id':fid,'replayed':False})
    changed=any(not r['replayed'] for r in results)
    result={'ok':True,'reason':'events_imported','request_id':doc['request_id'],
            'revision':revision+int(changed),'events':results}
    db.execute('UPDATE event_imports SET result_json=? WHERE session_id=? AND id=?',
               (encode(result),session,doc['request_id']))
    if changed: db.execute('UPDATE sessions SET revision=revision+1 WHERE id=?',(session,))
    return result,changed


def preview(store,session,document,now,instruction_ref):
    """Clone before connect/migration: dry-run cannot change the source schema."""
    require(store.path.is_file(),'database_missing')
    with tempfile.TemporaryDirectory(prefix='event-preview-') as folder:
        target=Path(folder)/'preview.db'
        with closing(sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True)) as source:
            with closing(sqlite3.connect(target)) as dest: source.backup(dest)
        shadow=type(store)(target)
        with shadow.connect(True) as db:
            revision=state(db,session)
            result,_=import_batch(db,session,document,now,instruction_ref)
            db.rollback()
        return {**result,'reason':'event_import_preview','revision':revision}


def effective_status(row,now):
    return 'expired' if row['status']=='pending' and row['expires_ms']<=now else row['status']


def shares(db,session,eid):
    return [dict(r) for r in db.execute('''SELECT j.id AS job_id,j.status,j.updated_ms
        FROM world_event_shares s JOIN dm_jobs j ON j.session_id=s.session_id AND j.id=s.job_id
        WHERE s.session_id=? AND s.event_id=? ORDER BY j.updated_ms DESC,j.id LIMIT 21''',(session,eid))]


def render(db,session,row,now,full=False):
    e=decode(row['payload_json'])
    linked=shares(db,session,row['id'])
    result={'event_id':row['id'],'mode':row['mode'],'status':effective_status(row,now),
            'summary':e['summary'],'occurred_ms':row['occurred_ms'],'fact_id':row['fact_id'],
            'not_before_ms':row['not_before_ms'],'expires_ms':row['expires_ms'],
            'review_after_ms':row['review_after_ms'],'known_by':e['known_by'],
            'shares':linked[:20],'shares_has_more':len(linked)>20}
    if full:
        result['payload']=e
        result['transitions']=[dict(r) for r in db.execute('SELECT sequence,at_ms,action,reason '
            'FROM world_event_transitions WHERE session_id=? AND event_id=? ORDER BY sequence',(session,row['id']))]
        result['source']=dict(db.execute('SELECT source_kind,producer_id,instruction_ref,received_ms '
            'FROM event_imports WHERE session_id=? AND id=?',(session,row['request_id'])).fetchone())
    return result


def snapshot(db,session,now,candidates=True):
    # Apply knowledge and temporal filters in SQL, before LIMIT.
    recent=db.execute('''SELECT e.* FROM world_events e WHERE e.session_id=? AND e.status='occurred'
        AND EXISTS (SELECT 1 FROM json_each(e.payload_json,'$.known_by') WHERE value='self')
        ORDER BY e.occurred_ms DESC,e.id LIMIT ?''',(session,MAX_VISIBLE+1)).fetchall()
    result={'recent':[render(db,session,r,now) for r in recent[:MAX_VISIBLE]],
            'recent_has_more':len(recent)>MAX_VISIBLE,'advisory_only':True}
    if candidates:
        rows=db.execute('''SELECT * FROM world_events WHERE session_id=? AND status='pending'
            AND not_before_ms<=? AND review_after_ms<=? AND expires_ms>?
            ORDER BY expires_ms,id LIMIT ?''',(session,now,now,now,MAX_VISIBLE+1)).fetchall()
        result.update(pending=[{**render(db,session,r,now),'payload':decode(r['payload_json'])} for r in rows[:MAX_VISIBLE]],
                      pending_has_more=len(rows)>MAX_VISIBLE)
    return result


def inspect(db,session,eid,now,operator=False):
    revision=state(db,session); row=get(db,session,eid)
    require(operator or (row['status']=='occurred' and 'self' in decode(row['payload_json'])['known_by']),
            'event_not_known_to_self')
    return {'ok':True,'reason':'world_event','revision':revision,'event':render(db,session,row,now,full=operator)}


def listing(db,session,now,offset=0,limit=20,operator=False):
    revision=state(db,session)
    integer(offset,'offset'); integer(limit,'limit',1); require(limit<=50,'limit_too_large')
    visible='' if operator else " AND status='occurred' AND EXISTS (SELECT 1 FROM json_each(payload_json,'$.known_by') WHERE value='self')"
    rows=db.execute('SELECT * FROM world_events WHERE session_id=?'+visible+
                    ' ORDER BY created_ms DESC,id LIMIT ? OFFSET ?',(session,limit+1,offset)).fetchall()
    return {'ok':True,'reason':'world_events','revision':revision,'events':[render(db,session,r,now) for r in rows[:limit]],
            'has_more':len(rows)>limit,'next_offset':offset+limit if len(rows)>limit else None}


def cancel(db,session,eid,now,reason):
    revision=state(db,session); string(reason)
    row=get(db,session,eid)
    if row['status']=='cancelled': return {'ok':True,'reason':'event_already_cancelled','revision':revision}
    require(effective_status(row,now)=='pending','event_not_pending')
    db.execute("UPDATE world_events SET status='cancelled',updated_ms=? WHERE session_id=? AND id=?",(now,session,eid))
    transition(db,session,eid,now,'cancelled',reason)
    db.execute('UPDATE sessions SET revision=revision+1 WHERE id=?',(session,))
    return {'ok':True,'reason':'event_cancelled','revision':revision+1}


def apply_ops(db,session,ops,now,phase='all'):
    require(isinstance(ops,list) and len(ops)<=MAX_EVENTS,'event_invalid_operations')
    require(phase in ('all','events','shares'),'event_invalid_phase')
    for op in ops:
        obj(op,{'action','event_id','reason','occurred_at','review_at','job_ids'},
            {'action','event_id','reason'})
        action=op['action']; string(op['reason'])
        require(action in ('realize','defer','reject','share'),'event_invalid_action')
        extras={'realize':{'occurred_at'},'defer':{'review_at'},'reject':set(),'share':{'job_ids'}}[action]
        require(set(op)<=extras|{'action','event_id','reason'},'event_invalid_fields')
        if phase=='events' and action=='share' or phase=='shares' and action!='share': continue
        row=get(db,session,op['event_id']); e=decode(row['payload_json'])
        if action=='share':
            require(row['status']=='occurred' and 'self' in e['known_by'],'event_not_known_to_self')
            jobs=strings(op.get('job_ids'),20)
            require(jobs,'event_share_jobs_required')
            for jid in jobs:
                job=db.execute('SELECT * FROM dm_jobs WHERE session_id=? AND id=?',(session,jid)).fetchone()
                require(job is not None and job['status']!='cancelled','event_share_job_invalid')
                db.execute('INSERT OR IGNORE INTO world_event_shares VALUES (?,?,?)',(session,row['id'],jid))
            continue
        require(effective_status(row,now)=='pending','event_not_pending')
        if action=='reject':
            require(row['mode']=='candidate','event_scene_cannot_be_rejected')
            db.execute("UPDATE world_events SET status='rejected',updated_ms=? WHERE session_id=? AND id=?",(now,session,row['id']))
        elif action=='defer':
            review=timestamp(op.get('review_at'))
            require(now<review<row['expires_ms'],'event_invalid_review_time')
            db.execute('UPDATE world_events SET review_after_ms=?,updated_ms=? WHERE session_id=? AND id=?',
                       (review,now,session,row['id']))
        else:
            at=timestamp(op['occurred_at']) if 'occurred_at' in op else now
            require(row['not_before_ms']<=at<row['expires_ms'] and at<=now,'event_outside_window')
            require(row['review_after_ms']<=now,'event_review_deferred')
            validate_refs(db,session,e)
            active=db.execute("SELECT * FROM activities WHERE session_id=? AND status='in_progress'",(session,)).fetchone()
            for key,value in e['conditions'].items():
                column={'activity_id':'id','category':'category','location':'location'}[key]
                require(active is not None and active[column]==value and active['start_ms']<=at<active['planned_end_ms'],
                        'event_condition_mismatch')
            publish(db,session,e,at,now)
        transition(db,session,row['id'],now,action,op['reason'])


def expire(db,now):
    rows=db.execute("SELECT session_id,id,expires_ms FROM world_events WHERE status='pending' AND expires_ms<=?",(now,)).fetchall()
    for r in rows:
        db.execute("UPDATE world_events SET status='expired',updated_ms=? WHERE session_id=? AND id=?",(now,r['session_id'],r['id']))
        transition(db,r['session_id'],r['id'],now,'expired','event time window ended')
    # Expiration has no decision-relevant effect: reads already exclude these.
    return len(rows)


VISIBLE_FACT_SQL = """NOT EXISTS (SELECT 1 FROM world_events we WHERE we.session_id=facts.session_id
    AND we.fact_id=facts.id AND NOT EXISTS
    (SELECT 1 FROM json_each(we.payload_json,'$.known_by') WHERE value='self'))"""


def guard_fact(db,session,f):
    """A normal facts write cannot forge or mutate an injector-owned fact."""
    fid=f.get('id'); details=f.get('details',{})
    if not isinstance(fid,str): return
    kind=details.get('kind') if isinstance(details,dict) else None
    if not fid.startswith(PREFIX) and kind!='world_event': return
    row=db.execute('SELECT * FROM facts WHERE session_id=? AND id=?',(session,f['id'])).fetchone()
    require(row is not None and row['source']==f['source'] and row['summary']==f['summary']
            and row['occurred_ms']==f.get('occurred_ms') and decode(row['details_json'])==f.get('details',{}),
            'event_fact_owned_by_injector')


def schema():
    """Provider-neutral authoring contract; exported by event-schema without a DB."""
    ident={'type':'string','pattern':r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$'}
    short={'type':'string','minLength':1,'maxLength':120}
    names={'type':'array','maxItems':16,'uniqueItems':True,'items':short}
    time={'type':'string','format':'date-time','maxLength':40,
          'pattern':r'T.*(?:Z|[+-]\d\d:\d\d)$'}
    ref={'type':'object','additionalProperties':False,'required':['kind','id'],
         'properties':{'kind':{'enum':['fact','activity','appointment','event']},
                       'id':{'type':'string','minLength':1,'maxLength':256}}}
    properties={'event_id':ident,'mode':{'enum':['occurred','scene','candidate']},
        'summary':{'type':'string','minLength':1,'maxLength':500},
        'known_by':names,'participants':names,'parent_event_ids':{**names,'items':ident},
        'location':short,'occurred_at':time,'not_before':time,'expires_at':time,
        'context_refs':{'type':'array','maxItems':16,'items':ref},
        'narrative_excerpt':{'type':'string','minLength':1,'maxLength':2000},
        'source_ref':{'type':'string','minLength':1,'maxLength':500},
        'followup_suggestions':{'type':'array','maxItems':8,'uniqueItems':True,'items':short},
        'experience':{'enum':['direct','reported','internal']},
        'scene_scope':{'enum':['self_only','user_scene']},
        'conditions':{'type':'object','additionalProperties':False,
                      'properties':{k:short for k in ('location','activity_id','category')}}}
    event={'type':'object','additionalProperties':False,'properties':properties,
        'required':['event_id','mode','summary','known_by'],
        'oneOf':[
            {'properties':{'mode':{'const':'occurred'},'conditions':{'maxProperties':0}},
             'required':['occurred_at'],'not':{'anyOf':[{'required':['not_before']},{'required':['expires_at']}]}},
            {'properties':{'mode':{'enum':['scene','candidate']}},'required':['not_before','expires_at'],
             'not':{'required':['occurred_at']}}]}
    return {'$schema':'https://json-schema.org/draft/2020-12/schema','title':'World event input v1',
        'type':'object','additionalProperties':False,'required':['schema_version','request_id','events'],
        'properties':{'schema_version':{'const':1,'type':'integer'},'request_id':ident,
                      'events':{'type':'array','minItems':1,'maxItems':MAX_EVENTS,'items':event}}}
