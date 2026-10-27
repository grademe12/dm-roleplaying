"""Read-only bridges to author events, facts and authoritative continuity state.

No source migrations/writes; global author events never masquerade as DM evidence.
"""
from contextlib import contextmanager
import hashlib
import sqlite3
from pathlib import Path

from common import decode, encode, require

PERSONAL = 'personal'
SQL = """
CREATE TABLE IF NOT EXISTS source_origins(seq INTEGER PRIMARY KEY REFERENCES sources(seq),
 kind TEXT NOT NULL,origin_id TEXT NOT NULL,version TEXT NOT NULL,reference_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS source_origin_identity ON source_origins(kind,origin_id,version);
CREATE TABLE IF NOT EXISTS memory_origin_links(memory_id TEXT NOT NULL REFERENCES memories(id),
 kind TEXT NOT NULL,origin_id TEXT NOT NULL,version TEXT NOT NULL,reference_json TEXT NOT NULL,
 PRIMARY KEY(memory_id,kind,origin_id));
"""


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


@contextmanager
def source(cfg):
    db=sqlite3.connect(Path(cfg['db']).expanduser().resolve().as_uri()+'?mode=ro',uri=True,timeout=.25)
    db.row_factory=sqlite3.Row
    try:
        db.execute('BEGIN')
        yield db
    finally:db.close()


def has(db,table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() is not None


def reference(kind,ident,data,scope='personal'):
    return {'kind':kind,'id':ident,'version':digest(data),'scope':scope,'data':data}


def event_key(ident):
    key='event:'+ident
    return key if len(key)<=100 else 'event:'+hashlib.sha256(ident.encode()).hexdigest()


def event_ref(row,now):
    payload=decode(row['payload_json'])
    if row['status']!='occurred' or row['occurred_ms'] is None or row['occurred_ms']>now or 'self' not in payload['known_by']:
        return None
    fields=('summary','participants','known_by','location','experience','scene_scope','narrative_excerpt',
            'parent_event_ids','context_refs','followup_suggestions','source_ref')
    data={key:payload[key] for key in fields if key in payload}
    data.update(event_id=row['id'],status='occurred',occurred_ms=row['occurred_ms'],fact_id=row['fact_id'],
                source='author_event',interpretation='설정으로 확정된 시뮬레이션 사건; 실제 DM 발언이나 상대의 수락 증거가 아님')
    return reference('world_event',row['id'],data)


def fact_ref(row,channel,now):
    if row['source'] not in ('canon','observed','simulated') or (row['occurred_ms'] or row['recorded_ms'])>now:return None
    if row['id'].startswith(('world-event:','memory:','decision:')):return None
    if row['summary'].startswith(('topic_state:','outfit_state:')):return None
    details=decode(row['details_json'])
    if isinstance(details,dict) and details.get('channel',channel)!=channel:return None
    data={key:row[key] for key in ('id','source','summary','occurred_ms','recorded_ms')}
    data['details']=details
    return reference('fact',row['id'],data,'private_dm' if isinstance(details,dict) and details.get('channel') else 'personal')


def continuity_ref(db,session,row,channel):
    if row['channel']!=channel:return None
    policy=db.execute('SELECT settings_json FROM continuity_policy WHERE session_id=?',(session,)).fetchone()
    if policy is None:return None
    settings=decode(policy[0])
    if settings.get('mode')!='enforce' or row['scope'] not in settings.get('scopes',['private_dm']):return None
    data={key:row[key] for key in ('id','kind','memory_key','scope','channel','summary','scope_note','status',
                                  'supersedes','updated_ms','reason')}
    return reference('continuity',row['id'],data,'private_dm')


def resolve(db,session,kind,ident,channel,now):
    table={'world_event':'world_events','fact':'facts','continuity':'continuity_memories'}.get(kind)
    if table is None or not has(db,table):return None
    row=db.execute('SELECT * FROM '+table+' WHERE session_id=? AND id=?',(session,ident)).fetchone()
    if row is None:return None
    if kind=='world_event':return event_ref(row,now)
    if kind=='fact':return fact_ref(row,channel,now)
    return continuity_ref(db,session,row,channel)


def capture(db,src,session,since,now):
    if not has(src,'world_events'):return
    # Publication, rather than occurrence, controls bootstrap: a newly injected old
    # experience must be processed too. Pending->occurred publication is captured once.
    rows=src.execute("""SELECT we.* FROM world_events we WHERE session_id=? AND status='occurred'
      AND updated_ms>=? AND updated_ms<=? AND occurred_ms<=?
      AND EXISTS(SELECT 1 FROM json_each(we.payload_json,'$.known_by') WHERE value='self')
      AND NOT EXISTS(SELECT 1 FROM mem.aliases a WHERE a.kind='world_event'
        AND a.id=we.id||':'||we.digest||':'||we.occurred_ms)
      ORDER BY updated_ms,id LIMIT 200""",(session,since,now,now)).fetchall()
    for row in rows:
        ref=event_ref(row,now)
        canonical=encode(['world_event',ref['id'],ref['version']])
        data=ref['data'];body=data['summary']
        if data.get('narrative_excerpt'):body+='\n'+data['narrative_excerpt']
        db.execute('INSERT OR IGNORE INTO sources(canonical,channel,direction,source_id,occurred_ms,captured_ms,text) VALUES (?,?,?,?,?,?,?)',
                   (canonical,PERSONAL,'author',ref['id'],row['occurred_ms'],now,body))
        seq=db.execute('SELECT seq FROM sources WHERE canonical=?',(canonical,)).fetchone()[0]
        db.execute('INSERT OR IGNORE INTO source_origins VALUES (?,?,?,?,?)',
                   (seq,ref['kind'],ref['id'],ref['version'],encode(ref)))
        alias=row['id']+':'+row['digest']+':'+str(row['occurred_ms'])
        db.execute('INSERT OR IGNORE INTO aliases VALUES (?,?,?)',('world_event',alias,seq))
        db.execute('INSERT OR IGNORE INTO positions VALUES (?,0)',(PERSONAL,))


def entry(db,row):
    value=dict(row);value.pop('canonical',None)
    origin=db.execute('SELECT reference_json FROM source_origins WHERE seq=?',(row['seq'],)).fetchone()
    if origin:value.update(source_kind='author_event',scope='personal',origin=decode(origin[0]))
    return value


def context(db,cfg,channel,terms,now,budget,required=()):
    session=cfg.get('session','main');candidates=[];total=0;mandatory={}
    with source(cfg) as src:
        for memory in required:
            for linked in links(db,memory['id']):
                ref=resolve(src,session,linked['kind'],linked['id'],channel,now)
                if ref:mandatory[(ref['kind'],ref['id'])]=ref
        for kind,table in (('world_event','world_events'),('fact','facts'),('continuity','continuity_memories')):
            if not has(src,table):continue
            if kind=='world_event':
                condition="status='occurred' AND occurred_ms<=? AND EXISTS(SELECT 1 FROM json_each(payload_json,'$.known_by') WHERE value='self')"
                args=[now];column='payload_json';order='occurred_ms'
            elif kind=='fact':
                condition="source IN ('canon','observed','simulated') AND COALESCE(occurred_ms,recorded_ms)<=? AND substr(id,1,12)!='world-event:' AND substr(id,1,7)!='memory:' AND substr(id,1,9)!='decision:' AND summary NOT LIKE 'topic_state:%' AND summary NOT LIKE 'outfit_state:%'"
                args=[now];column="summary||' '||details_json";order='recorded_ms'
                # Scope is checked in SQL before ranking; another channel cannot crowd this one out.
                condition+=" AND (json_type(details_json)!='object' OR json_extract(details_json,'$.channel') IS NULL OR json_extract(details_json,'$.channel')=?)"
                args.append(channel)
            else:
                if channel==PERSONAL or not has(src,'continuity_policy'):continue
                policy=src.execute('SELECT settings_json FROM continuity_policy WHERE session_id=?',(session,)).fetchone()
                if policy is None or decode(policy[0]).get('mode')!='enforce':continue
                scopes=decode(policy[0]).get('scopes',['private_dm'])
                if not scopes:continue
                condition='channel=? AND scope IN ('+','.join('?' for _ in scopes)+')';args=[channel]+scopes;column="summary||' '||memory_key";order='updated_ms'
            total+=src.execute('SELECT count(*) FROM '+table+' WHERE session_id=? AND '+condition,[session]+args).fetchone()[0]
            ranking='+'.join('CASE WHEN instr(lower('+column+'),?)>0 THEN 1 ELSE 0 END' for _ in terms) or '0'
            rows=src.execute('SELECT * FROM '+table+' WHERE session_id=? AND '+condition+
                             ' ORDER BY ('+ranking+') DESC,'+order+' DESC,id LIMIT 12',[session]+args+terms).fetchall()
            for row in rows:
                ref=resolve(src,session,kind,row['id'],channel,now)
                if ref:
                    rank=sum(term in encode(ref['data']).lower() for term in terms)
                    candidates.append((rank,ref))
    result=[]
    # Rank relevant references first; a revoked policy remains reference material.
    ordered=list(mandatory.values())+[ref for _,ref in sorted(candidates,key=lambda item:item[0],reverse=True)
                                      if (ref['kind'],ref['id']) not in mandatory]
    for ref in ordered:
        if len(encode(result+[ref]).encode())<=budget:result.append(ref)
    return {'items':result,'omitted':max(0,total-len(result)),'complete':total==len(result)}


def links(db,memory_id,full=False):
    if not has(db,'memory_origin_links'):return []
    rows=db.execute('SELECT * FROM memory_origin_links WHERE memory_id=? ORDER BY kind,origin_id',(memory_id,))
    return [decode(r['reference_json']) if full else {'kind':r['kind'],'id':r['origin_id'],'version':r['version']} for r in rows]


def descriptions(db,memory_id):
    result=[]
    for ref in links(db,memory_id,full=True):
        data=ref.get('data',{})
        item={key:ref[key] for key in ('kind','id','scope') if key in ref}
        item.update({key:data[key] for key in ('source','status','participants','known_by','experience','scene_scope','channel') if key in data})
        result.append(item)
    return result


def current(cfg,refs,channel,now):
    if not refs:return True
    with source(cfg) as src:
        for ref in refs:
            actual=resolve(src,cfg.get('session','main'),ref['kind'],ref['id'],channel,now)
            if actual is None or actual['version']!=ref['version']:return False
    return True


def select_links(item,packet,previous_links):
    available={(r['kind'],r['id']):r for r in packet.get('reference_context',{}).get('items',[])}
    for s in packet['new_sources']+packet['prior_context']:
        if 'origin' in s:available[(s['origin']['kind'],s['origin']['id'])]=s['origin']
    selected={}
    # A canonical event key also binds provenance if a compatible model omitted
    # the optional origin_refs field. Semantic association still belongs to the model.
    for ref in available.values():
        if ref['kind']=='world_event' and item['key']==event_key(ref['id']):
            selected[('world_event',ref['id'])]=ref
    explicit=item.get('origin_refs',[])
    require(isinstance(explicit,list) and len(explicit)<=8,'invalid_memory_origin_refs')
    for ref in explicit:
        require(isinstance(ref,dict) and set(ref)=={'kind','id'} and isinstance(ref['kind'],str) and isinstance(ref['id'],str),
                'invalid_memory_origin_refs')
        key=(ref['kind'],ref['id'])
        require(key in available,'memory_origin_not_read')
        selected[key]=available[key]
    evidence={e['seq'] for e in item['evidence']}
    for s in packet['new_sources']+packet['prior_context']:
        if s['seq'] in evidence and 'origin' in s:
            r=s['origin'];selected[(r['kind'],r['id'])]=r
    # Existing dependencies cannot silently vanish on a rewrite. A changed origin
    # must be explicitly read at its new version before this memory can be refreshed.
    for ref in previous_links:
        key=(ref['kind'],ref['id'])
        selected.setdefault(key,available.get(key,ref))
    refs=list(selected.values())
    events=[r for r in refs if r['kind']=='world_event']
    if item['kind']=='episode' and events:
        require(len(events)==1 and item['key']==event_key(events[0]['id']),'memory_origin_key_mismatch')
    if packet['channel']==PERSONAL:
        require(item['kind']=='episode' and len(events)==1,'memory_author_episode_required')
    return refs
