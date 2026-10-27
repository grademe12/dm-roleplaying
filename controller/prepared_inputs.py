"""Session inputs with advisory budgets and scoped consumption acknowledgements.

Budgets are chosen before delivery, independent of security classifications or
retry counters. Source content is never rewritten to change a screening result.
"""
import copy
import hashlib
from pathlib import Path
import uuid

from common import ControlError, encode, integer, require
import instrumentation
import memory_cues
import episodic_memory
import character_brief

BUNDLE_BYTES = 48 * 1024
COMPONENT_BYTES = 32 * 1024
MANIFEST_BYTES = 48 * 1024
MAX_READS = 64
# Recent DM messages included by prepare/read-context when no limit is given.
# 30 keeps ~20+ minutes of agreements (times, who pays) in view; prepare
# still accepts --context-limit between MIN_CONTEXT_LIMIT and MAX_CONTEXT_LIMIT.
MIN_CONTEXT_LIMIT = 6
MAX_CONTEXT_LIMIT = 30
DEFAULT_CONTEXT_LIMIT = 30


def size(value):
    return len(encode(value).encode('utf-8'))


def recall_limit(query, requested=None):
    """Keep normal recall breadth; page size is an explicit caller choice."""
    if requested is not None:
        integer(requested,'limit',1)
        require(requested<=10,'limit_too_large')
        return requested
    return 10


def version(messages):
    # Cache poll timestamps/health do not change the semantic message version.
    return instrumentation.digest(messages)


def message_meta(message):
    result={k:message[k] for k in ('id','direction','status','observed_ms','timestamp_ms','reply_due_ms') if k in message}
    observation=message.get('observation') or {}
    result['channel']=observation.get('channel','instagram')
    return result


def state_content(snapshot):
    """Keep authoritative fields intact; policy text has its own reader."""
    result=copy.deepcopy(snapshot)
    result.pop('core_rules',None)
    return result


def minimal_snapshot(snapshot):
    result={k:copy.deepcopy(snapshot[k]) for k in
            ('ok','reason','revision','now_ms','work_manifest','conversation_mode',
             'interaction',
             'send_blocked','dispatch_hold','core_rules_hash','core_rules_changed',
             'weekly_plan_present','next_cursor','has_more','projection') if k in snapshot}
    result['state']={k:v for k,v in snapshot.get('state',{}).items()
                     if k in ('revision','advanced_to_ms','next_decision_ms','fatigue','mood','hunger','social_fatigue')}
    activity=snapshot.get('activity')
    result['activity']=({k:activity[k] for k in ('id','status','attention','start_ms','planned_end_ms','actual_end_ms') if k in activity}
                        if activity else None)
    for key,fields in (('messages',None),('jobs',('id','group_id','status','channel','due_ms')),
                       ('events',('id','kind','cursor','received_ms'))):
        rows=snapshot.get(key,[])
        result[key]=[message_meta(r) if fields is None else {k:r[k] for k in fields if k in r} for r in rows[:50]]
        result[key+'_count']=len(rows)
        result[key+'_has_more']=len(rows)>50
    result['omitted_state_requires_read']=True
    return result


def deduplicate(result):
    """Only remove exact duplicates; drafts and unavailable source text remain."""
    contexts=result['contexts']
    for message in result['snapshot'].get('messages',[]):
        channel=(message.get('observation') or {}).get('channel','instagram')
        source=next((m for m in contexts.get(channel,{}).get('messages',[]) if m['id']==message['id']),None)
        if source is not None and source.get('text')==message.get('text'):
            message.pop('text',None)
            message['body_ref']={'channel':channel,'message_id':message['id']}


def check_documents(runner, documents):
    for name, hashed in documents.items():
        content=document_content(runner,runner.state(),name).encode('utf-8')
        require(hashlib.sha256(content).hexdigest()==hashed,
                'prepare_inputs_changed')


def document_content(runner,state,name):
    raw=root_path(runner,name).read_text(encoding='utf-8')
    return character_brief.document_text(state,name,raw)


def document_tokens(state, tokens):
    tokens = [] if tokens is None else tokens
    require(isinstance(tokens,list) and all(isinstance(t,str) for t in tokens),
            'invalid_document_receipt')
    found = {}
    for token in tokens:
        offer = state.get('document_receipts',{}).get(instrumentation.digest(token))
        require(offer is not None and offer['kind']=='document', 'invalid_document_receipt')
        found[token] = offer
    return found


def reuse_document(runner, turn_id, input_id, name, token):
    """Explicit retained-context reuse, including optional documents across turns."""
    with runner.owned():
        state = active(runner, turn_id, input_id)
        offer = document_tokens(state, [token])[token]
        require(offer['name'] == name, 'invalid_document_receipt')
        check_documents(runner, {name: offer['sha256']})
        state['input_offers'][instrumentation.digest(token)] = offer
        import gate
        gate.write_json(runner.path, state)
        return {'receipt': token, 'name': name, 'sha256': offer['sha256']}


def configure(state,result,queries,context_limit,mode,core_path,document_receipts=None):
    require(mode in ('auto','bundled','compact'),'invalid_prepare_mode')
    source_ids={c:ctx.pop('source_message_ids',ctx['message_ids'])
                for c,ctx in result['contexts'].items()}
    source_sizes={key:size(result[key]) for key in ('snapshot','contexts','recalls','documents')}
    deduplicate(result)
    documents=dict(result['documents'])
    supplied=document_tokens(state,document_receipts)
    reused={name:offer['sha256'] for offer in supplied.values()
            for name,content in documents.items()
            if offer['name']==name and offer['sha256']==hashlib.sha256(content.encode()).hexdigest()}
    state['prepared_document_hashes']={name:hashlib.sha256(content.encode()).hexdigest()
                                       for name,content in documents.items()}
    result['documents']={name:content for name,content in documents.items() if name not in reused}
    result['reused_documents']=reused
    result.update(input_mode='bundled',input_budget_bytes=BUNDLE_BYTES)
    bundled_size=size(result)
    selected='compact' if mode=='compact' or mode=='auto' and bundled_size>BUNDLE_BYTES else 'bundled'
    if selected=='bundled':
        result['document_receipts']={offer['name']:token for token,offer in supplied.items()
                                    if reused.get(offer['name'])==offer['sha256']}
        for name,content in result['documents'].items():
            token=uuid.uuid4().hex
            state.setdefault('document_receipts',{})[instrumentation.digest(token)]={
                'kind':'document','name':name,'sha256':hashlib.sha256(content.encode()).hexdigest()}
            result['document_receipts'][name]=token
        result['input_budget_exceeded']=False
        result['input_budget_exceeded']=size(result)>BUNDLE_BYTES
        state.update(input_mode='bundled',input_catalog={},input_offers={})
        return source_sizes
    # A caller-supplied hash alone never satisfies a compact session's policy read.
    documents['CORE_RULES.md']=Path(core_path).read_text(encoding='utf-8')
    require(hashlib.sha256(documents['CORE_RULES.md'].encode()).hexdigest()==result['snapshot']['core_rules_hash'],
            'rules_changed')
    documents['CORE_RULES.md']=character_brief.document_text(state,'CORE_RULES.md',documents['CORE_RULES.md'])
    snapshot=state_content(result['snapshot'])
    # Store section sizes, never raw state or conversation bodies, in the catalog.
    catalog={'contexts':{},'documents':{},'queries':queries,'revision':snapshot['revision'],
             'view':snapshot['projection']['view'],
             'state_sections':{k:len(v) if isinstance(v,list) else None for k,v in snapshot.items()}}
    if result.get('repetition_cues') is not None:
        # Optional section: readable via read-state, never required at commit.
        catalog['state_sections']['repetition_cues'] = None
    contexts={}
    for channel,context in result['contexts'].items():
        catalog['contexts'][channel]={'version':context['source_version'],'ids':context['message_ids'],
                                      'required_recent_ids':context['message_ids'][-context_limit:],
                                      'known_ids':source_ids[channel]}
        contexts[channel]={k:v for k,v in context.items() if k not in ('messages','sha256','source_message_ids')}
        contexts[channel].update(available_message_ids=context['message_ids'],version=context['source_version'])
        # An unavailable backlog is reported, not silently marked consumed.
        missing=contexts[channel].get('missing_target_ids',[])
        contexts[channel]['missing_target_count']=len(missing)
        contexts[channel]['missing_target_ids']=missing[:50]
    for name,content in documents.items():
        catalog['documents'][name]={'sha256':hashlib.sha256(content.encode()).hexdigest(),
                                    'bytes':len(content.encode()),'required':True}
    # A receipt belongs to this wake's session file, not a turn. Only documents
    # with identical bytes may cross turns; state/context receipts never do.
    reusable={token:offer for token,offer in state.get('document_receipts',{}).items()
              if offer['name'] in catalog['documents'] and
                 offer['sha256']==catalog['documents'][offer['name']]['sha256']}
    for name,entry in catalog['documents'].items():
        entry['reusable_receipt']=any(o['name']==name for o in reusable.values())
    catalog['required_state_sections']=required_sections(snapshot)
    input_id=uuid.uuid4().hex
    state.update(input_mode='compact',input_id=input_id,input_catalog=catalog,input_offers=reusable,context_ids={})
    recall_manifest=[{'index':i,'result_count':len(r['facts']),'bytes':size(r)}
                     for i,r in enumerate(result['recalls'])]
    result.update(input_mode='compact',input_id=input_id,input_budget_bytes=MANIFEST_BYTES,
        snapshot=minimal_snapshot(result['snapshot']),contexts=contexts,documents={},recalls=[],
        input_manifest={'documents':catalog['documents'],'state_sections':catalog['state_sections'],
                        'recalls':recall_manifest,
                        'required_state_sections':catalog['required_state_sections'],
                        'component_limit_bytes':COMPONENT_BYTES,'max_reads':None,
                        'budget_is_advisory':True,'recommended_max_reads':MAX_READS})
    result['next_input_action']='read-bundle'
    result['input_budget_exceeded']=False
    result['input_budget_exceeded']=size(result)>MANIFEST_BYTES
    return source_sizes


def read_bundle(runner,turn_id,input_id,document_receipts=None,input_receipts=None):
    """One model tool roundtrip for the ordinary compact input set.

    Individual readers retain their existing revision/context checks. No partial
    bundle is returned after an error, and generated receipts are never treated
    as delivered without the caller submitting them.
    """
    with runner.owned():
        state=active(runner,turn_id,input_id)
        catalog=copy.deepcopy(state['input_catalog'])
        supplied=document_tokens(state,document_receipts)
        tokens=[] if input_receipts is None else input_receipts
        require(isinstance(tokens,list) and all(isinstance(t,str) for t in tokens),'invalid_input_receipt')
        retained={}
        for token in tokens:
            offer=state['input_offers'].get(instrumentation.digest(token))
            require(offer is not None,'invalid_input_receipt')
            retained[token]=offer
            if offer['kind']=='document':
                check_documents(runner,{offer['name']:offer['sha256']})
                supplied[token]=offer
    receipts=list(retained)
    returned_documents={}
    reused={}
    for token,offer in supplied.items():
        entry=catalog['documents'].get(offer['name'])
        if entry and entry['sha256']==offer['sha256']:
            if token not in receipts:receipts.append(token)
            reused[offer['name']]=offer['sha256']
            returned_documents[offer['name']]=token
    components=[]
    def collect(kind,**options):
        item=read(runner,turn_id,input_id,kind,**options)
        receipts.append(item['receipt'])
        components.append({'kind':kind,'content':item['content']})
        if kind=='document':
            returned_documents[options['name']]=item['receipt']
    def section_read(key):
        spans=[o['sections'][key] for o in retained.values()
               if o['kind']=='state' and key in o['sections']]
        if not spans:return False
        count=catalog['state_sections'][key]
        if count is None:return True
        end=0
        for start,stop in sorted(spans):
            if start>end:return False
            end=max(end,stop)
        return end==count
    missing=[key for key in catalog['required_state_sections'] if not section_read(key)]
    if missing:collect('state',sections=missing)
    for name in catalog['documents']:
        if name not in reused:
            collect('document',name=name)
    unavailable={}
    for channel,entry in catalog['contexts'].items():
        with runner.owned():
            active(runner,turn_id,input_id)
            context=runner.context(channel)
        if not context['ready']:
            unavailable[channel]=context['reason']
            continue
        read_ids={mid for o in retained.values() if o['kind']=='context' and o['channel']==channel
                  for mid in o['ids']}
        missing_ids=[mid for mid in entry['ids'] if mid not in read_ids]
        for offset in range(0,len(missing_ids),30):
            collect('context',channel=channel,message_ids=missing_ids[offset:offset+30])
    # Exact duplicate selectors need only one returned recall, preserving content.
    seen=set()
    for index,query in enumerate(catalog['queries']):
        selector=instrumentation.digest(query)
        if selector not in seen:
            identity={'keywords':query,'memory':False,'order':'newest','offset':0,
                      'before_ms':None,'limit':recall_limit(query)}
            if not any(o['kind']=='recall' and o['query_digest']==instrumentation.digest(identity)
                       for o in retained.values()):
                collect('recall',index=index)
            seen.add(selector)
    with runner.owned():
        state=active(runner,turn_id,input_id)
        check_documents(runner,{name:entry['sha256'] for name,entry in catalog['documents'].items()})
        check_documents(runner,{o['name']:o['sha256'] for o in retained.values() if o['kind']=='document'})
        for channel,entry in catalog['contexts'].items():
            if channel not in unavailable:
                context=runner.context(channel)
                require(context['ready'],'prepare_inputs_changed')
                check_context(context,entry)
    result={'ok':True,'reason':'input_bundle','turn_id':turn_id,'input_id':input_id,
            'components':components,'input_receipts':receipts,'reused_documents':reused,
            'document_receipts':returned_documents,'unavailable_contexts':unavailable,
            'reused_input_count':len(retained)}
    result['input_advisory']={'recommended_bytes':BUNDLE_BYTES,'budget_exceeded':size(result)>BUNDLE_BYTES}
    runner.event('input_bundle_finished',turn_id,input_id=input_id,
                 component_count=len(components),output_bytes=size(result),
                 reused_document_count=len(reused),reused_input_count=len(retained))
    return result


def required_sections(snapshot):
    """Require decision inputs, not unrelated diagnostics or background ledgers."""
    required={'state','activity','messages','jobs','work_manifest','conversation_mode','episodic_memory',
              'send_blocked','dispatch_hold','conversation_policy','memory_policy','memory_context','character_brief'}
    manifest=snapshot.get('work_manifest',{})
    if (snapshot.get('interaction',{}).get('active') or
            snapshot.get('interaction',{}).get('candidate_appointment_id')):
        required.add('interaction')
    if snapshot.get('continuations'):
        required.update(('continuations','continuation_due_ids'))
    if manifest.get('life_required') or manifest.get('primary')=='life':
        required.update(('appointments','weekly_plan','activity_history','action_intents',
                         'action_intent_due_ids','action_intent_followups','morning_contact','world_events',
                         'appointment_lifecycle'))
    return sorted(required & snapshot.keys())


def check_context(context,catalog):
    selected=[m for m in context['messages'] if m['id'] in catalog['ids']]
    require(version(selected)==catalog['version'],'prepare_inputs_changed')
    # Incoming messages that arrived after preparation are answered by the next turn.
    # Any other new entry (e.g. an outgoing echo) still invalidates. Edits to old, unused
    # cache entries do not invalidate an otherwise unchanged selected conversation.
    known=set(catalog.get('known_ids',catalog['ids']))
    require(all(m['id'] in known or m.get('direction')=='incoming' for m in context['messages']),
            'prepare_inputs_changed')


def active(runner,turn_id,input_id):
    state=runner.state(turn_id)
    require(state.get('input_mode')=='compact' and state.get('input_id')==input_id,'input_prepare_required')
    fresh(runner,state)
    return state


def fresh(runner,state):
    character_brief.validate_reply(runner,state)
    episodic_memory.validate_reply(runner, state)
    revision=runner.store.tick(runner.session,runner.clock(),limit=1,view='gate')['revision']
    require(revision==state['prepared_revision'] or
            runner.store.only_incoming_since(runner.session,state['prepared_revision'],revision),
            'prepare_inputs_changed')


def root_path(runner,name):
    root=Path(__file__).resolve().parent
    require(isinstance(name,str) and not Path(name).is_absolute() and '..' not in Path(name).parts,'invalid_input_document')
    require(name in ('CORE_RULES.md','API.md') or name.startswith(('runbooks/','design/')),'invalid_input_document')
    path=(root/name).resolve()
    require(path.is_relative_to(root) and path.suffix=='.md' and path.is_file(),'invalid_input_document')
    return path


def issue(runner,state,kind,content,metadata,read_id):
    fresh(runner,state)
    token=uuid.uuid4().hex
    result={'ok':True,'reason':'input_component','turn_id':state['turn_id'],
            'input_id':state['input_id'],'kind':kind,'content':content,'receipt':token}
    result['input_advisory']={'recommended_bytes':COMPONENT_BYTES,
        'budget_exceeded':False,
        'read_count_exceeded':len(state['input_offers'])>=MAX_READS}
    result['input_advisory']['budget_exceeded']=size(result)>COMPONENT_BYTES
    offer={'kind':kind,**metadata}
    state['input_offers'][instrumentation.digest(token)]=offer
    if kind=='document':
        state.setdefault('document_receipts',{})[instrumentation.digest(token)]=offer
    import gate
    gate.write_json(runner.path,state)
    runner.event('input_read_finished',state['turn_id'],input_id=state['input_id'],component=kind,
                 read_id=read_id,output_bytes=size(result),
                 budget_exceeded=result['input_advisory']['budget_exceeded'],read_count=len(state['input_offers']))
    return result


def read(runner,turn_id,input_id,kind,**options):
    require(kind in ('context','document','state','recall','memory-context','memory-history-search'),
            'invalid_input_component')
    with runner.owned():
        state=active(runner,turn_id,input_id)
        read_id=uuid.uuid4().hex
        runner.event('input_read_started',turn_id,input_id=input_id,component=kind,read_id=read_id,
                     selector_digest=instrumentation.digest(options),
                     document=options.get('name') if options.get('name') in state['input_catalog']['documents'] else None,
                     channel=options.get('channel') if kind=='context' else None)
        try:
            if kind=='context':
                channel=options['channel']
                require(channel in state['input_catalog']['contexts'],'input_channel_not_prepared')
                context=runner.context(channel)
                catalog=state['input_catalog']['contexts'][channel]
                require(context['ready'],'reply_context_not_ready')
                check_context(context,catalog)
                messages=[m for m in context['messages'] if m['id'] in catalog['ids']]
                ids=options.get('message_ids')
                if ids is None:
                    offset=options.get('offset',0);limit=options.get('limit',DEFAULT_CONTEXT_LIMIT)
                    integer(offset,'offset');integer(limit,'limit',1);require(limit<=30,'limit_too_large')
                    ids=catalog['ids'][offset:offset+limit]
                require(isinstance(ids,list) and 1<=len(ids)<=30 and all(isinstance(i,str) for i in ids)
                        and len(ids)==len(set(ids)) and set(ids)<=set(catalog['ids']),'invalid_input_message_ids')
                selected=[m for m in messages if m['id'] in ids]
                return issue(runner,state,kind,{'channel':channel,'messages':selected},
                             {'channel':channel,'ids':[m['id'] for m in selected],'version':catalog['version']},read_id)
            if kind=='document':
                name=options['name'];content=document_content(runner,state,name)
                hashed=hashlib.sha256(content.encode()).hexdigest()
                old=state['input_catalog']['documents'].get(name)
                require(old is None or old['sha256']==hashed,'prepare_inputs_changed')
                return issue(runner,state,kind,{'name':name,'text':content},{'name':name,'sha256':hashed},read_id)
            if kind in ('memory-context','memory-history-search'):
                channel=options['channel']
                selectors={'offset':options.get('offset',0),'limit':options.get('limit',6)}
                if kind=='memory-context':
                    selectors.update(key=options.get('key'),history=options.get('history',False))
                    result=runner.store.memory_context(runner.session,channel,options.get('scopes'),**selectors)
                else:
                    selectors.update(order=options.get('order','newest'),before_ms=options.get('before_ms'))
                    result=runner.store.memory_history_search(runner.session,channel,options.get('keywords'),
                                                             options.get('scopes'),**selectors)
                require(result['revision']==state['prepared_revision'],'prepare_inputs_changed')
                runner.event('input_memory_read',turn_id,read_id=read_id,operation=kind,channel=channel,
                             result_count=len(result['items']),output_bytes=size(result))
                return issue(runner,state,kind,result,
                             {'query_digest':instrumentation.digest(options),
                              'fact_ids':memory_cues.fact_ids(result) if kind=='memory-context' else []},read_id)
            if kind=='recall':
                if options.get('index') is not None:
                    index=options['index'];integer(index,'index')
                    require(index<len(state['input_catalog']['queries']),'invalid_recall_index')
                    query=state['input_catalog']['queries'][index]
                else:
                    query=options.get('keywords')
                    require(isinstance(query,list) and all(isinstance(q,str) for q in query),'invalid_recall_queries')
                limit=recall_limit(query,options.get('limit'))
                if options.get('memory'):
                    result=runner.store.memory_recall(runner.session,query,
                        order=options.get('order','newest'),offset=options.get('offset',0),limit=limit,
                        before_ms=options.get('before_ms'))
                else:
                    require(options.get('order','newest')=='newest'
                            and options.get('before_ms') is None,'memory_recall_option_required')
                    result=runner.store.recall(runner.session,query,limit=limit,offset=options.get('offset',0))
                require(result['revision']==state['prepared_revision'],'prepare_inputs_changed')
                runner.event('input_recall',turn_id,read_id=read_id,
                             **instrumentation.recall_metadata(query,result))
                query_identity={'keywords':query,'memory':bool(options.get('memory')),
                                'order':options.get('order','newest'),'offset':options.get('offset',0),
                                'before_ms':options.get('before_ms'),'limit':limit}
                return issue(runner,state,kind,result,{'query_digest':instrumentation.digest(query_identity),
                                                     'fact_ids':memory_cues.fact_ids(result)},read_id)
            snapshot=runner.store.tick(runner.session,runner.clock(),view=state['input_catalog']['view'],
                                       known_core_rules_hash=state['input_catalog']['documents']['CORE_RULES.md']['sha256'])
            require(snapshot['revision']==state['prepared_revision'],'prepare_inputs_changed')
            content=state_content(snapshot)
            if state.get('episodic_memory'):
                content['episodic_memory'] = copy.deepcopy(state['episodic_memory'])
            if state.get('repetition_cues'):
                content['repetition_cues'] = copy.deepcopy(state['repetition_cues'])
            if state.get('character_selection'):
                content['character_brief']=copy.deepcopy(state['character_selection']['brief'])
            sections=options.get('sections') or list(state['input_catalog']['state_sections'])
            require(isinstance(sections,list) and sections and all(k in content and
                    k in state['input_catalog']['state_sections'] for k in sections),'invalid_state_sections')
            offset=options.get('offset',0);limit=options.get('limit')
            integer(offset,'offset')
            coverage={k:None for k in sections}
            if limit is not None or offset:
                require(len(sections)==1 and isinstance(content[sections[0]],list),'invalid_state_page')
                integer(limit,'limit',1);require(limit<=200,'limit_too_large')
            for key in sections:
                if isinstance(content[key],list):
                    count=state['input_catalog']['state_sections'][key]
                    require(len(content[key])==count,'prepare_inputs_changed')
                    stop=count if limit is None else min(count,offset+limit)
                    require(offset<=count,'invalid_state_page')
                    coverage[key]=[offset,stop]
                    content[key]=content[key][offset:stop]
            selected = {k:content[k] for k in sections}
            return issue(runner,state,kind,selected,
                         {'sections':coverage,'fact_ids':memory_cues.fact_ids(selected)},read_id)
        except (ControlError,OSError,ValueError,KeyError,TypeError) as exc:
            runner.event('input_read_failed',turn_id,input_id=input_id,component=kind,
                         read_id=read_id,
                         reason=exc.reason if isinstance(exc,ControlError) else 'input_component_io_error')
            raise


def validate(runner,state,decision,tokens):
    require(isinstance(tokens,list) and all(isinstance(t,str) for t in tokens),
            'input_receipts_required')
    offers=state.get('input_offers',{})
    acknowledged=[]
    for token in tokens:
        offer=offers.get(instrumentation.digest(token))
        require(offer is not None,'invalid_input_receipt')
        acknowledged.append(offer)
    required=state['input_catalog'].get('required_state_sections',list(state['input_catalog']['state_sections']))
    for key in required:
        count=state['input_catalog']['state_sections'][key]
        reads=[o['sections'][key] for o in acknowledged if o['kind']=='state' and key in o['sections']]
        require(bool(reads),'state_input_required')
        if count is not None:
            intervals=sorted(reads)
            end=0
            for start,stop in intervals:
                require(start<=end,'state_input_required')
                end=max(end,stop)
            require(end==count,'state_input_required')
    documents={o['name']:o['sha256'] for o in acknowledged if o['kind']=='document'}
    for name,entry in state['input_catalog']['documents'].items():
        require(documents.get(name)==entry['sha256'],'document_input_required')
    for name,hashed in documents.items():
        content=document_content(runner,runner.state(),name).encode('utf-8')
        require(hashlib.sha256(content).hexdigest()==hashed,'prepare_inputs_changed')
    contexts={}
    for offer in acknowledged:
        if offer['kind']=='context':contexts.setdefault(offer['channel'],set()).update(offer['ids'])
    for dm in decision.get('dm_decisions',[]):
        if dm.get('action') not in ('reply','initiate','revalidate_group'):continue
        review=dm.get('conversation_review',{}).get('context',{})
        channel=dm.get('channel') or review.get('channel')
        require(channel in state['input_catalog']['contexts'],'reply_context_not_ready')
        catalog=state['input_catalog']['contexts'][channel]
        ctx=runner.context(channel)
        require(ctx['ready'],'prepare_inputs_changed')
        check_context(ctx,catalog)
        needed=set(dm.get('message_ids',[]))|set(review.get('recent_message_ids',[]))|set(catalog['required_recent_ids'])
        require(needed<=contexts.get(channel,set()),'message_input_required')
        contexts.setdefault(channel,set())
    return {c:list(ids) for c,ids in contexts.items()}
