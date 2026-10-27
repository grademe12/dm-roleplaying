"""Read-only evidence snapshots for the shadow character-document editor."""
from pathlib import Path
import sqlite3

from common import ControlError, decode, encode, require
import memory_origins as origins
import episodic_memory

CHANNELS = ('personal', 'slack', 'instagram')


def wrap(kind, ident, data, channel='personal', roots=(), at=0, trigger=True):
    require(channel in CHANNELS, 'character_source_channel')
    result={'kind':kind,'id':ident,'channel':channel,'data':data,'roots':list(roots),'at_ms':at}
    result['version']=origins.digest({**{k:v for k,v in result.items() if k!='at_ms'},
        'roots':[{k:v for k,v in r.items() if k!='at_ms'} for r in roots]})
    result['signal']=origins.digest([kind,ident,result['version']]) if trigger else None
    return result


def root_message(src, session, ident, excerpt):
    row=src.execute('SELECT * FROM dm_messages WHERE session_id=? AND id=?',(session,ident)).fetchone()
    if row is None:return None
    info=decode(row['observation_json'])
    if info.get('identity') not in ('platform','verified_composite') or info.get('review_required'):return None
    if excerpt not in row['text']:return None
    data={'text':row['text'],'direction':info.get('direction','incoming'),
          'channel':info.get('channel','instagram'),'observed_ms':row['observed_ms']}
    return {'kind':'message','id':ident,'version':origins.digest(data),'channel':data['channel'],
            'independent':data['direction']=='incoming','source':'verified_dm',
            'excerpt':excerpt,'at_ms':row['observed_ms']}


def root_event(src, session, ident, now):
    ref=origins.resolve(src,session,'world_event',ident,'personal',now)
    if ref is None:return None
    return {'kind':'world_event','id':ident,'version':ref['version'],'channel':'personal',
            'independent':True,'source':'author_event','excerpt':ref['data']['summary'],
            'at_ms':ref['data']['occurred_ms']}


def root_job(src, session, ident, excerpt):
    row=src.execute('SELECT * FROM dm_jobs WHERE session_id=? AND id=?',(session,ident)).fetchone()
    if row is None or row['status']!='confirmed' or excerpt not in row['text']:return None
    channel='instagram'
    if origins.has(src,'group_channels') and 'group_id' in row.keys():
        group=src.execute('SELECT channel FROM group_channels WHERE session_id=? AND group_id=?',(session,row['group_id'])).fetchone()
        if group:channel=group[0]
    data={'text':row['text'],'channel':channel,'status':'confirmed'}
    return {'kind':'job','id':ident,'version':origins.digest(data),'channel':channel,'independent':False,
            'source':'self_utterance','excerpt':excerpt,'at_ms':row['updated_ms']}


def collect(cfg, now, wanted=None):
    """Index current evidence, not controller revisions or heartbeat timestamps.

    Editor discovery scans eligible rows; consumer freshness uses explicit source
    selectors. Large-DB discovery cost still needs measurement.
    Rows are not silently truncated or hidden behind a fixed first-page limit.
    """
    session=cfg.get('session','main');refs={};unavailable=[];diagnostics=[]
    def requested(kind):return wanted is None or any(k==kind for k,i in wanted)
    def selector(kind):
        if wanted is None:return '',[]
        ids=sorted(i for k,i in wanted if k==kind)
        return ' AND id IN ('+','.join('?' for _ in ids)+')',ids

    with origins.source(cfg) as src:
        peer=src.execute('SELECT sender,recipient FROM sessions WHERE id=?',(session,)).fetchone()
        require(peer is not None,'session_not_found')
        peer=dict(peer)
        if requested('world_event') and origins.has(src,'world_events'):
            clause,args=selector('world_event')
            for row in src.execute("SELECT * FROM world_events WHERE session_id=? AND status='occurred'"+clause,[session,*args]):
                ref=origins.event_ref(row,now)
                if ref:
                    root=root_event(src,session,row['id'],now)
                    refs['world_event:'+row['id']]=wrap('world_event',row['id'],ref['data'],roots=[root],at=row['updated_ms'])
        if requested('fact') and origins.has(src,'facts'):
            clause,args=selector('fact')
            for row in src.execute("SELECT * FROM facts WHERE session_id=? AND source IN ('canon','observed','simulated')"+clause,[session,*args]):
                if row['id']=='controller-initialized':continue
                details=decode(row['details_json'])
                channel=details.get('channel','personal') if isinstance(details,dict) else 'personal'
                if channel not in CHANNELS:continue
                ref=origins.fact_ref(row,channel,now)
                if ref:
                    # A model-authored observed/simulated fact is not independent
                    # corroboration merely because its source label says observed.
                    root={'kind':'fact','id':row['id'],'version':ref['version'],'channel':channel,
                          'independent':row['source']=='canon','source':row['source'],
                          'excerpt':row['summary'],'at_ms':row['recorded_ms']}
                    refs['fact:'+row['id']]=wrap('fact',row['id'],ref['data'],channel,[root],row['recorded_ms'])
        if requested('continuity') and origins.has(src,'continuity_memories') and origins.has(src,'continuity_policy'):
            clause,args=selector('continuity')
            for row in src.execute('SELECT * FROM continuity_memories WHERE session_id=?'+clause,[session,*args]):
                ref=origins.continuity_ref(src,session,row,row['channel'])
                if ref:
                    root={'kind':'continuity','id':row['id'],'version':ref['version'],'channel':row['channel'],
                          'independent':True,'source':'continuity_state','excerpt':row['status'],'at_ms':row['updated_ms']}
                    refs['continuity:'+row['id']]=wrap('continuity',row['id'],ref['data'],row['channel'],[root],row['updated_ms'])
        for kind,table in (('relationship','relationships'),('appointment','appointments'),('intent','action_intents')):
            if not requested(kind) or not origins.has(src,table):continue
            clause,args=selector(kind)
            if kind=='relationship' and wanted is not None:
                pairs=[decode(i) for k,i in wanted if k==kind]
                clause=' AND ('+' OR '.join('(subject_id=? AND other_id=?)' for _ in pairs)+')'
                args=[value for pair in pairs for value in pair]
            for row in src.execute('SELECT * FROM '+table+' WHERE session_id=?'+clause,[session,*args]):
                raw=dict(row);raw.pop('session_id',None)
                if kind=='relationship':ident=encode([raw['subject_id'],raw['other_id']])
                else:ident=raw['id']
                details=decode(raw.pop('details_json')) if 'details_json' in raw else {}
                if details:raw['details']=details
                # Review/updated times can move without the underlying intention changing.
                for field in ('updated_ms','review_at_ms','decision_id'):raw.pop(field,None)
                channel=details.get('channel','personal')
                if channel not in CHANNELS:continue
                at=row['created_ms'] if 'created_ms' in row.keys() else now
                root={'kind':kind,'id':ident,'version':origins.digest(raw),'channel':channel,
                      'independent':True,'source':'controller_state','excerpt':encode(raw),'at_ms':at}
                refs[kind+':'+ident]=wrap(kind,ident,raw,channel,[root],at)
        opts=episodic_memory.reply_settings(cfg)
        if opts['mode']=='off' and requested('episode'):
            unavailable.append('episode')
        if opts['mode']!='off' and requested('episode'):
            try:
                with episodic_memory.connection(cfg) as mem:
                    # One logical latest version per key; an inactive/retracted
                    # version remains visible as a state change, never as a fresh fact.
                    clause='';args=[]
                    if wanted is not None:
                        pairs=[decode(i) for k,i in wanted if k=='episode']
                        clause=' WHERE '+' OR '.join('(channel=? AND key=?)' for _ in pairs)
                        args=[value for pair in pairs for value in pair]
                    rows=mem.execute('''SELECT * FROM (SELECT *,row_number() OVER
                        (PARTITION BY channel,key ORDER BY recorded_ms DESC,rowid DESC) AS rank
                        FROM memories'''+clause+''') WHERE rank=1''',args)
                    for row in rows:
                        roots=[];valid=True
                        for e in decode(row['evidence']):
                            origin=e.get('origin')
                            if origin:root=root_event(src,session,origin['id'],now)
                            else:
                                root=root_message(src,session,e['source_id'],e['excerpt'])
                                if root is None:root=root_job(src,session,e['source_id'],e['excerpt'])
                            if root is None:valid=False;break
                            if root['channel'] not in ('personal',row['channel']):valid=False;break
                            roots.append(root)
                        links=origins.links(mem,row['id'])
                        if not origins.current(cfg,links,row['channel'],now):valid=False
                        if not valid:continue
                        data={k:row[k] for k in ('key','kind','category','status','summary','interpretation','active')}
                        data['origin_refs']=links;data['episode_id']=row['id']
                        logical=encode([row['channel'],row['key']])
                        ref=wrap('episode',logical,data,row['channel'],roots,row['recorded_ms'],False)
                        # Bind to the original evidence/status, not the organizer's
                        # wording or random version ID. Rephrasing the same roots
                        # must neither wake the editor nor invalidate its document.
                        root_versions=sorted({(r['kind'],r['id'],r['version']) for r in roots})
                        independent=sorted({(r['kind'],r['id'],r['version']) for r in roots if r['independent']})
                        ref['version']=origins.digest([logical,independent or root_versions,row['status'],row['active'],links])
                        if independent:ref['signal']=ref['version']
                        refs['episode:'+row['channel']+':'+row['key']]=ref
            except (OSError,sqlite3.Error,ControlError) as exc:
                unavailable.append('episode')
                diagnostics.append({'kind':'episode','reason':exc.reason if isinstance(exc,ControlError) else 'memory_store_unavailable'})
    return {'refs':refs,'peer':peer,'unavailable_kinds':unavailable,'diagnostics':diagnostics}


def revalidate(cfg, basis, now):
    if not basis:return 'current'
    snapshot=collect(cfg,now,wanted={(r['kind'],r['id']) for r in basis})
    current={(r['kind'],r['id']):r for r in snapshot['refs'].values()}
    unavailable=False
    for ref in basis:
        if ref['kind'] in snapshot['unavailable_kinds']:
            unavailable=True
            continue
        actual=current.get((ref['kind'],ref['id']))
        if ref.get('missing'):
            if actual is not None:return 'changed'
        elif actual is None or actual['version']!=ref['version']:return 'changed'
    return 'unavailable' if unavailable else 'current'
