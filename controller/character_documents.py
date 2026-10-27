"""Versioned character documents; editor writes only its independent sidecar."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid

from common import ControlError, decode, encode, file_lock, integer, require, text
import character_sources

ROOT=Path(__file__).resolve().parent
SEED_PATH=ROOT/'design/character-document-seed.json'
SCHEMA='character-editor-v1'
SECTIONS=('goals','relationship','self_understanding')
SCOPES=('personal','slack','instagram')
SQL='''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations(seq INTEGER PRIMARY KEY AUTOINCREMENT,key TEXT UNIQUE NOT NULL,
 ref_json TEXT NOT NULL,signal TEXT,ack TEXT,updated_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,parent TEXT,created_ms INTEGER NOT NULL,
 document_json TEXT NOT NULL,changes_json TEXT NOT NULL,run_id TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,deadline_ms INTEGER NOT NULL,status TEXT NOT NULL,
 digest TEXT NOT NULL,packet TEXT NOT NULL,response_digest TEXT,result TEXT);
'''


def digest(value):return hashlib.sha256(encode(value).encode()).hexdigest()


def private_write(path,value,raw=False):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,tmp=tempfile.mkstemp(prefix='.character-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            stream.write(value if raw else encode(value));stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,path)
    finally:Path(tmp).unlink(missing_ok=True)


def settings(cfg):
    raw=cfg.get('character_documents')
    if raw is None:return {'mode':'off'}
    require(isinstance(raw,dict),'invalid_character_settings')
    if raw.get('mode','off')=='off':return {'mode':'off'}
    require(raw.get('mode') in ('shadow','enforce'),'invalid_character_mode')
    value={'lease_seconds':600,'bootstrap_hours':24,'batch_size':32,'batch_bytes':98304,'max_attempts':3,**raw}
    for key,low,high in (('lease_seconds',30,1800),('bootstrap_hours',1,720),('batch_size',1,128),
                         ('batch_bytes',4096,1048576),('max_attempts',1,10)):
        integer(value[key],key,low);require(value[key]<=high,'invalid_'+key)
    source=Path(cfg['db']).expanduser().resolve()
    state=Path(cfg.get('state_dir',source.parent/'automation-state')).expanduser().resolve()
    value['path']=Path(value.get('path',state/'character-documents.sqlite3')).expanduser().resolve()
    require(value['path']!=source,'character_source_path_collision')
    memory=cfg.get('episodic_memory',{})
    if isinstance(memory,dict):
        other=Path(memory.get('path',state/'episodic-memory.sqlite3')).expanduser().resolve()
        require(value['path']!=other,'character_memory_path_collision')
    value['binding']={'source':str(source),'session':cfg.get('session','main'),'persona':cfg.get('persona_id','yujin')}
    return value


def seed():
    value=decode(SEED_PATH.read_text(encoding='utf-8'))
    require(value['schema']=='character-seed-v1','invalid_character_seed')
    templates={};ids={item['item_id'] for item in value['items']}
    require(len(ids)==len(value['items']),'invalid_character_seed')
    for filename,expected in value['source_files'].items():
        require(filename in ('design/character/profile.md','design/character/goals.md'),'invalid_character_seed')
        body=(ROOT/filename).read_text(encoding='utf-8')
        require(hashlib.sha256(body.encode()).hexdigest()==expected,'character_seed_source_changed')
        slots=sorted((s for s in value['slots'] if s['path']==filename),key=lambda s:s['start'])
        end=0
        for slot in slots:
            start=slot['start'];quote=slot['text']
            require(start>=end and body[start:start+len(quote)]==quote and slot['item_id'] in ids,'invalid_character_seed')
            end=start+len(quote)
        for slot in reversed(slots):
            start=slot['start'];body=body[:start]+'{{character:'+slot['item_id']+'}}'+body[start+len(slot['text']):]
        templates[filename]=body
    return {**value,'templates':templates,'version':digest(value)}


@contextmanager
def connection(cfg,write=False):
    opts=settings(cfg);require(opts['mode']!='off','character_documents_off')
    path=opts['path']
    if write:path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    db=sqlite3.connect(path.as_uri()+('?mode=rwc' if write else '?mode=ro'),uri=True,isolation_level=None,timeout=.25)
    db.row_factory=sqlite3.Row
    try:
        if write:
            path.chmod(0o600);db.executescript(SQL);db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT OR IGNORE INTO meta VALUES ('schema','1')")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('binding',?)",(encode(opts['binding']),))
        else:db.execute('BEGIN')
        require(db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0]=='1','character_schema_changed')
        require(decode(db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()[0])==opts['binding'],
                'character_binding_changed')
        yield db
        if write:db.commit()
    except Exception:
        if write:db.rollback()
        raise
    finally:db.close()


def getmeta(db,key,default=None):
    row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    return decode(row[0]) if row else default


def putmeta(db,key,value):db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,encode(value)))


def item_key(item):return item['scope']+'/'+item['item_id']


def current_document(db):
    revision=getmeta(db,'active_revision')
    row=db.execute('SELECT * FROM versions WHERE id=?',(revision,)).fetchone()
    require(row is not None,'character_document_missing')
    return decode(row['document_json'])


def initialize(db,cfg,snapshot,baseline,now):
    opts=settings(cfg)
    require(opts['binding']['persona']==baseline['persona'],'character_seed_persona_mismatch')
    peer=getmeta(db,'peer')
    require(peer is None or peer==snapshot['peer'],'character_peer_changed')
    putmeta(db,'peer',snapshot['peer'])
    if getmeta(db,'active_revision') is None:
        revision='seed-'+baseline['version']
        items=[{**i,'version':'seed-'+digest(i)} for i in baseline['items']]
        document={'revision':revision,'seed_version':baseline['version'],'items':items}
        db.execute('INSERT INTO versions VALUES (?,?,?,?,?,NULL)',(revision,None,now,encode(document),'[]'))
        putmeta(db,'active_revision',revision)
        putmeta(db,'since_ms',max(0,now-opts['bootstrap_hours']*3600000))
    require(current_document(db)['seed_version']==baseline['version'],'character_seed_changed')


def sync(db,snapshot,now):
    old={r['key']:r for r in db.execute('SELECT * FROM observations')};since=getmeta(db,'since_ms',0)
    for key,ref in snapshot['refs'].items():
        previous=old.pop(key,None)
        if previous:
            db.execute('UPDATE observations SET ref_json=?,signal=?,updated_ms=? WHERE key=?',
                       (encode(ref),ref['signal'],now,key))
        else:
            ack=ref['signal'] if ref['at_ms']<since else None
            db.execute('INSERT INTO observations(key,ref_json,signal,ack,updated_ms) VALUES (?,?,?,?,?)',
                       (key,encode(ref),ref['signal'],ack,now))
    for key,row in old.items():
        ref=decode(row['ref_json'])
        if ref['kind'] in snapshot['unavailable_kinds'] or ref.get('missing'):continue
        missing={'kind':ref['kind'],'id':ref['id'],'channel':ref['channel'],'data':{'status':'unavailable'},
                 'roots':[],'at_ms':now,'missing':True}
        missing['version']=digest([missing['kind'],missing['id'],'unavailable'])
        missing['signal']=missing['version']
        db.execute('UPDATE observations SET ref_json=?,signal=?,updated_ms=? WHERE key=?',(encode(missing),missing['signal'],now,key))


def failed(db,row,now,reason):
    if row is None or row['status']!='pending':return False
    packet=decode(row['packet']);retry=getmeta(db,'retry',{})
    count=retry.get('attempts',0)+1 if retry.get('fingerprint')==packet['work_fingerprint'] else 1
    delay=min(900000,15000*2**min(count-1,6))
    putmeta(db,'retry',{'fingerprint':packet['work_fingerprint'],'attempts':count,'not_before_ms':now+delay,'reason':reason})
    db.execute("UPDATE runs SET status='failed',result=? WHERE id=?",(encode({'reason':reason,'retry_after_ms':delay}),row['id']))
    return True


def fail(cfg,run_id,now,reason):
    if settings(cfg)['mode']=='off':return False
    with file_lock(settings(cfg)['path'],'character-editor',private=True),connection(cfg,True) as db:
        return failed(db,db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone(),now,reason)


def retry(cfg):
    with file_lock(settings(cfg)['path'],'character-editor',private=True),connection(cfg,True) as db:
        require(not db.execute("SELECT 1 FROM runs WHERE status='pending'").fetchone(),'busy')
        putmeta(db,'retry',{})
    return {'ok':True,'reason':'retry_scheduled'}


def prepare(cfg,now):
    opts=settings(cfg)
    if opts['mode']=='off':return {'ok':True,'reason':'off'}
    baseline=seed();snapshot=character_sources.collect(cfg,now)
    with file_lock(opts['path'],'character-editor',private=True),connection(cfg,True) as db:
        initialize(db,cfg,snapshot,baseline,now)
        for row in db.execute("SELECT * FROM runs WHERE status='pending' AND deadline_ms<=?",(now,)).fetchall():
            failed(db,row,now,'character_run_expired')
        row=db.execute("SELECT * FROM runs WHERE status='pending'").fetchone()
        if row:return {'ok':True,'reason':'busy','run_id':row['id']}
        sync(db,snapshot,now);document=current_document(db)
        sources=[];changes=[];oversized=[]
        pending=list(db.execute('SELECT * FROM observations WHERE signal IS NOT ack ORDER BY seq'))
        for row in pending:
            ref=decode(row['ref_json'])
            if ref['kind'] in snapshot['unavailable_kinds']:continue
            if len(encode([ref]).encode())>opts['batch_bytes']:
                oversized.append({'kind':ref['kind'],'id':ref['id']});continue
            if len(sources)>=opts['batch_size'] or len(encode(sources+[ref]).encode())>opts['batch_bytes']:continue
            sources.append(ref);changes.append({'key':row['key'],'signal':row['signal']})
        reviewed=getmeta(db,'reviewed',[])
        due=[i['version'] for i in document['items'] if i['status']!='retracted'
             and i['review_condition']['at_ms'] is not None and i['review_condition']['at_ms']<=now
             and i['version'] not in reviewed]
        if not sources and not due:
            return {'ok':True,'reason':'needs_attention' if oversized else 'no_work','oversized_sources':oversized,
                    'unavailable_kinds':snapshot['unavailable_kinds'],'source_diagnostics':snapshot['diagnostics']}
        # Prior dependencies get first place in bounded reference context.
        wanted={(b['kind'],b['id']) for item in document['items'] for b in item.get('basis',[])}
        context=[];available={(r['kind'],r['id']) for r in sources};context_bytes=opts['batch_bytes']//2
        rows=list(db.execute('SELECT ref_json FROM observations ORDER BY seq DESC'))
        refs=[decode(r[0]) for r in rows]
        refs.sort(key=lambda r:(r['kind'],r['id']) in wanted,reverse=True)
        for ref in refs:
            key=(ref['kind'],ref['id'])
            if key in available or ref['kind'] in snapshot['unavailable_kinds']:continue
            if len(encode(context+[ref]).encode())<=context_bytes:
                context.append(ref);available.add(key)
        fingerprint=digest([changes,due,document['revision'],baseline['version']])
        retry=getmeta(db,'retry',{})
        if retry.get('fingerprint')==fingerprint:
            if retry['attempts']>=opts['max_attempts']:return {'ok':True,'reason':'needs_attention','attempts':retry['attempts'],'last_reason':retry['reason']}
            if retry['not_before_ms']>now:return {'ok':True,'reason':'retry_later','retry_after_ms':retry['not_before_ms']-now}
        run_id=uuid.uuid4().hex;deadline=now+opts['lease_seconds']*1000
        packet={'schema':SCHEMA,'run_id':run_id,'base_revision':document['revision'],'seed_version':baseline['version'],
                'fixed_templates':baseline['templates'],'seed_items':baseline['items'],'current_document':document,
                'changed_sources':sources,'reference_context':context,'source_context_omitted':max(0,len(refs)-len(available)),
                'review_due_versions':due,'work_fingerprint':fingerprint,'now_ms':now,'changes':changes,
                'pending_omitted':max(0,len(pending)-len(changes)),'unavailable_kinds':snapshot['unavailable_kinds'],
                'source_diagnostics':snapshot['diagnostics']}
        sha=digest(packet)
        db.execute('INSERT INTO runs VALUES (?,?,?,?,?,NULL,NULL)',(run_id,deadline,'pending',sha,encode(packet)))
        return {'ok':True,'reason':'prepared','run_id':run_id,'deadline_ms':deadline,'packet':{**packet,'input_sha256':sha}}


def bounded(value,name,limit):
    value=text(value,name);require(len(value)<=limit,'character_'+name+'_too_long');return value


def response_binding(cfg,run_id):
    require(isinstance(run_id,str) and re.fullmatch('[0-9a-f]{32}',run_id),'invalid_character_run')
    with connection(cfg) as db:
        row=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone();require(row is not None,'character_run_not_found')
        return {'request_id':run_id,'input_sha256':row['digest'],'status':row['status'],
                'result':decode(row['result']) if row['result'] else None}


def apply_operations(packet,operations,now):
    require(isinstance(operations,list) and len(operations)<=32,'invalid_character_operations')
    items={item_key(i):i for i in packet['current_document']['items']}
    available={(r['kind'],r['id']):r for r in packet['changed_sources']+packet['reference_context']}
    changed={(r['kind'],r['id']) for r in packet['changed_sources']};seen=set();diff=[];dependencies=[]
    for op in operations:
        require(isinstance(op,dict) and set(op)=={'operation','item_id','section','scope','supersedes','current_description',
            'response_implication','status','epistemic','basis','review_condition','change_reason'},'invalid_character_operation')
        ident=bounded(op['item_id'],'item_id',120);section=op['section'];scope=op['scope']
        require(section in SECTIONS and scope in SCOPES,'character_owned_section_required')
        prefix={'goals':'goals:','relationship':'relationship:','self_understanding':'self:'}[section]
        require(ident.startswith(prefix) and re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]*',ident),'character_owned_item_required')
        key=scope+'/'+ident;require(key not in seen,'duplicate_character_item');seen.add(key)
        previous=items.get(key)
        require(op['supersedes']==(previous['version'] if previous else None),'character_version_changed')
        require(previous is None or previous['section']==section,'character_section_changed')
        require(op['operation'] in ('upsert','retract') and (op['operation']!='retract' or previous is not None),'invalid_character_operation')
        require(op['status'] in ('ongoing','completed','planned','uncertain','paused','retracted'),'invalid_character_status')
        require((op['operation']=='retract')==(op['status']=='retracted'),'invalid_character_status')
        require(op['epistemic'] in ('recorded','reported','author_asserted','tentative'),'invalid_character_epistemic')
        if section=='self_understanding':require(op['epistemic'] in ('reported','tentative'),'character_reflection_not_fact')
        description=bounded(op['current_description'],'description',1200)
        implication=op['response_implication']
        require(isinstance(implication,str) and len(implication)<=800,'invalid_character_implication')
        reason=bounded(op['change_reason'],'reason',500)
        condition=op['review_condition']
        require(isinstance(condition,dict) and set(condition)=={'on_source_change','at_ms'}
                and type(condition['on_source_change']) is bool,'invalid_character_review')
        if condition['at_ms'] is not None:
            integer(condition['at_ms'],'character_review_at',now+1)
        basis=op['basis'];require(isinstance(basis,list) and 1<=len(basis)<=8,'invalid_character_basis')
        selected=[];used=set()
        for b in basis:
            require(isinstance(b,dict) and set(b)=={'kind','id','version','excerpt'},'invalid_character_basis')
            pair=(b['kind'],b['id']);require(pair in available,'character_source_not_read')
            ref=available[pair];require(b['version']==ref['version'],'character_source_version_changed')
            require(pair not in used,'duplicate_character_basis');used.add(pair)
            quote=bounded(b['excerpt'],'excerpt',400)
            require(quote in encode(ref['data']),'character_excerpt_mismatch')
            require(ref['channel'] in ('personal',scope),'character_scope_expansion')
            selected.append(ref)
        require(bool(used & changed) or (previous and previous['version'] in packet['review_due_versions']),
                'character_new_basis_required')
        roots=[r for ref in selected for r in ref['roots']]
        if op['epistemic']=='author_asserted':require(any(r['source']=='author_event' for r in roots),'character_author_basis_required')
        if op['epistemic']=='recorded':require(any(r['independent'] for r in roots),'character_independent_basis_required')
        if any(ref.get('missing') for ref in selected):
            require(op['operation']=='retract' or op['epistemic']=='tentative','character_missing_basis')
        if op['status']=='completed' and all(r['kind']=='appointment' for r in selected):
            require(any(r['data']['status']=='completed' for r in selected),'character_unconfirmed_completion')
        item={'item_id':ident,'section':section,'scope':scope,'current_description':description,
              'response_implication':implication,'status':op['status'],'epistemic':op['epistemic'],
              'basis':basis,'review_condition':condition}
        # Only byte-identical semantic items can be recognized as no-change in code.
        old={k:v for k,v in (previous or {}).items() if k!='version'}
        if item==old:continue
        item['version']=uuid.uuid4().hex;items[key]=item;dependencies.extend(selected)
        diff.append({'item_id':ident,'scope':scope,'before':previous,'after':item,'change_reason':reason})
    return list(items.values()),diff,dependencies


def complete(cfg,response,now):
    try:return _complete(cfg,response,now)
    except (ControlError,ValueError,TypeError,KeyError) as exc:
        if isinstance(exc,ControlError) and exc.reason=='busy':raise
        if isinstance(response,dict) and isinstance(response.get('run_id'),str):
            fail(cfg,response['run_id'],now,exc.reason if isinstance(exc,ControlError) else 'invalid_character_response')
        raise


def _complete(cfg,response,now):
    require(isinstance(response,dict) and set(response)=={'schema','run_id','input_sha256','base_revision','operations'},'invalid_character_response')
    require(response['schema']==SCHEMA,'invalid_character_schema')
    require(isinstance(response['run_id'],str) and isinstance(response['input_sha256'],str),'invalid_character_response')
    opts=settings(cfg);require(opts['mode']!='off','character_documents_off')
    baseline=seed();snapshot=character_sources.collect(cfg,now)
    with file_lock(opts['path'],'character-editor',private=True),connection(cfg,True) as db:
        initialize(db,cfg,snapshot,baseline,now)
        row=db.execute('SELECT * FROM runs WHERE id=?',(response['run_id'],)).fetchone();require(row is not None,'character_run_not_found')
        sha=digest(response)
        if row['status']=='completed':
            require(row['response_digest']==sha,'character_response_changed');return {**decode(row['result']),'replayed':True}
        require(row['status']=='pending' and now<row['deadline_ms'],'character_run_expired')
        packet=decode(row['packet'])
        require(response['input_sha256']==row['digest'] and response['base_revision']==packet['base_revision'],'character_input_mismatch')
        require(getmeta(db,'active_revision')==packet['base_revision'],'character_document_changed')
        items,diff,dependencies=apply_operations(packet,response['operations'],now)
        state=character_sources.revalidate(cfg,dependencies,now)
        require(state=='current','character_source_'+state)
        revision=packet['base_revision']
        if diff:
            revision=uuid.uuid4().hex
            document={'revision':revision,'seed_version':baseline['version'],'items':items}
            db.execute('INSERT INTO versions VALUES (?,?,?,?,?,?)',(revision,packet['base_revision'],now,encode(document),encode(diff),row['id']))
            putmeta(db,'active_revision',revision)
        for change in packet['changes']:
            # Newer source observations remain unacknowledged for the next pass.
            db.execute('UPDATE observations SET ack=? WHERE key=?',(change['signal'],change['key']))
        putmeta(db,'reviewed',list(set(getmeta(db,'reviewed',[]))|set(packet['review_due_versions'])))
        putmeta(db,'retry',{})
        result={'ok':True,'reason':'published' if diff else 'no_change','run_id':row['id'],'revision':revision,'changed_items':len(diff),'mode':opts['mode']}
        db.execute("UPDATE runs SET status='completed',response_digest=?,result=? WHERE id=?",(sha,encode(result),row['id']))
        return result


def view(cfg,now,revision=None,snapshot=None):
    opts=settings(cfg)
    if opts['mode']=='off':return {'mode':'off','items':[]}
    snapshot=snapshot if snapshot is not None else character_sources.collect(cfg,now)
    with connection(cfg) as db:
        require(getmeta(db,'peer')==snapshot['peer'],'character_peer_changed')
        revision=revision or getmeta(db,'active_revision')
        row=db.execute('SELECT * FROM versions WHERE id=?',(revision,)).fetchone();require(row is not None,'character_document_missing')
        doc=decode(row['document_json']);result=[]
        for item in doc['items']:
            validity='current'
            if item['basis']:
                available={(r['kind'],r['id']):r for r in snapshot['refs'].values()}
                if any(b['kind'] in snapshot['unavailable_kinds'] for b in item['basis']):validity='unavailable'
                elif any((b['kind'],b['id']) not in available or available[(b['kind'],b['id'])]['version']!=b['version'] for b in item['basis']):validity='changed'
            result.append({**item,'basis_validity':validity})
        return {**doc,'mode':opts['mode'],'items':result,'changes':decode(row['changes_json']),
                'created_ms':row['created_ms'],'unavailable_kinds':snapshot['unavailable_kinds']}


def export(cfg,now,revision=None):
    document=view(cfg,now,revision);revision=document['revision']
    require(re.fullmatch(r'(seed-)?[0-9a-f]{32,64}',revision),'invalid_character_revision')
    folder=settings(cfg)['path'].parent/'character-documents';lines=['# 인물 문서 — '+document['mode'], '', '버전: '+revision,'',
        '버전별 검토 산출물. 실제 답장 적용은 해당 턴에 선택된 character_brief를 확인한다.','']
    for scope in SCOPES:
        lines+=['## '+scope,'']
        for section in SECTIONS:
            lines+=['### '+section,'']
            for item in document['items']:
                if item['scope']!=scope or item['section']!=section:continue
                lines+=['- '+item['item_id']+' ['+item['status']+', '+item['epistemic']+', 근거 '+item['basis_validity']+']',
                        '  '+item['current_description'].replace('\n','\n  '),
                        '  대화 참고: '+item['response_implication'].replace('\n','\n  '),'']
    path=folder/(revision+'.md');private_write(path,'\n'.join(lines),True)
    diff=folder/(revision+'.diff.json');private_write(diff,document['changes'])
    return {'document_file':str(path),'diff_file':str(diff),'revision':revision}
