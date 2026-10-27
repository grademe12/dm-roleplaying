"""Optional, read-only character-document consumer with turn-pinned versions.

No model calls, editor lease or writes to source/editor databases. Off/shadow do
not even inspect the optional store. Real source corrections trigger at most one
fallback preparation for a turn; publication alone never invalidates a reply.
"""
import copy
import hashlib
import sqlite3
from pathlib import Path

from common import ControlError, decode, encode, require
import character_documents as documents
import character_sources

ROOT=Path(__file__).resolve().parent
ERRORS=(OSError,sqlite3.Error,ControlError,ValueError,TypeError,KeyError,IndexError)


def consumer_templates():
    baseline=documents.seed()
    extra=decode((ROOT/'design/character-consumer-slots.json').read_text(encoding='utf-8'))
    require(extra['schema']=='character-consumer-slots-v1','invalid_character_consumer_slots')
    hashes={**baseline['source_files'],**extra['source_files']}
    templates=dict(baseline['templates'])
    for name,expected in hashes.items():
        require(name in ('CORE_RULES.md','design/character/profile.md','design/character/goals.md'),
                'invalid_character_consumer_slots')
        body=(ROOT/name).read_text(encoding='utf-8')
        require(hashlib.sha256(body.encode()).hexdigest()==expected,'character_seed_source_changed')
        templates.setdefault(name,body)
    for slot in extra['replacements']:
        body=templates[slot['path']]
        require(body.count(slot['text'])==1,'invalid_character_consumer_slots')
        templates[slot['path']]=body.replace(slot['text'],slot['replacement'])
    for name,body in templates.items():
        for item in baseline['items']:
            body=body.replace('{{character:'+item['item_id']+'}}',
                '〔현재 '+item['item_id']+': character_brief의 해당 항목 참조〕')
        templates[name]=body
    return baseline,templates,hashes


def select(cfg,state,channels,now):
    raw=cfg.get('character_documents')
    if not isinstance(raw,dict) or raw.get('mode','off') in ('off','shadow'):return None,None
    if state.get('character_suspended_turn')==state['turn_id']:return None,'suspended_for_turn'
    try:
        opts=documents.settings(cfg)
        previous=state.get('character_selection')
        if previous and previous['turn_id']==state['turn_id']:
            if not set(channels)<=set(previous['brief']['channels']):
                state['character_suspended_turn']=state['turn_id']
                return None,'channel_set_changed'
            try:validity=character_sources.revalidate(cfg,previous['dependencies'],now)
            except ERRORS:validity='unavailable'
            if validity=='changed':
                state['character_suspended_turn']=state['turn_id']
                return None,'source_changed'
            return previous,None
        baseline,templates,hashes=consumer_templates()
        guidance=(ROOT/'runbooks/character-brief.md').read_text(encoding='utf-8')
        require(opts['binding']['persona']==baseline['persona'],'character_seed_persona_mismatch')
        with documents.connection(cfg) as db:
            current=documents.current_document(db)
        wanted={(b['kind'],b['id']) for item in current['items']
                if item['scope'] in ('personal',*channels) for b in item['basis']}
        snapshot=character_sources.collect(cfg,now,wanted=wanted)
        document=documents.view(cfg,now,revision=current['revision'],snapshot=snapshot)
        require(document['seed_version']==baseline['version'],'character_seed_changed')
        available={(r['kind'],r['id']):r for r in snapshot['refs'].values()}
        limit=raw.get('reply_bytes',65536)
        require(type(limit) is int and 4096<=limit<=1048576,'invalid_character_reply_bytes')
        brief={'revision':document['revision'],'seed_version':baseline['version'],
               'channels':{},
               'interpretation':'가변 서술과 대화 참고 자료다. 실행·발송·호칭 권한과 현재 원장을 변경하지 않는다. baseline은 시작 설정이며 후속 확인 근거가 우선한다.'}
        def size():return len(encode({'brief':brief,'fixed_documents':templates}).encode())
        require(size()<=limit,'character_fixed_documents_too_large')
        dependencies={}
        for channel in sorted(channels):
            # A channel-specific item shadows personal even if stale/retracted;
            # never resurrect a contradicted personal default in that channel.
            items={i['item_id']:i for i in document['items'] if i['scope']=='personal'}
            items.update({i['item_id']:i for i in document['items'] if i['scope']==channel})
            entry={'items':[],'omitted':[]}
            brief['channels'][channel]=entry
            for item in sorted(items.values(),key=lambda i:(i['status']=='baseline',i['item_id'])):
                if item['basis_validity']!='current':
                    entry['omitted'].append({'item_id':item['item_id'],'reason':'basis_'+item['basis_validity']});continue
                selected={k:copy.deepcopy(item[k]) for k in ('item_id','section','scope','version','status','epistemic',
                    'current_description','response_implication','basis','review_condition')}
                selected['review_due']=bool(item['review_condition']['at_ms'] is not None and item['review_condition']['at_ms']<=now)
                entry['items'].append(selected)
                if size()>limit:
                    entry['items'].pop();entry['omitted'].append({'item_id':item['item_id'],'reason':'reply_budget'});continue
                for b in item['basis']:
                    dependencies[(b['kind'],b['id'])]=available[(b['kind'],b['id'])]
        # Metadata growth is bounded too; never cut a source/item mid-string.
        require(size()<=limit,'character_brief_too_large')
        return {'turn_id':state['turn_id'],'brief':brief,'source_hashes':hashes,
                'dependencies':list(dependencies.values()),'guidance':guidance,'fixed_documents':templates},None
    except ERRORS as exc:
        return None,exc.reason if isinstance(exc,ControlError) else 'character_brief_unavailable'


def prepare(runner,state,snapshot,documents_out,channels):
    selection,reason=select(runner.cfg,state,channels,runner.clock())
    state['character_selection']=selection
    if selection:
        snapshot['character_brief']=copy.deepcopy(selection['brief'])
        # The raw CORE hash remains its source-file freshness token. The rendered
        # text has its own ordinary document receipt/hash, never that raw hash.
        snapshot.pop('core_rules',None)
        documents_out.update(selection['fixed_documents'])
        documents_out['runbooks/character-brief.md']=selection['guidance']
        runner.event('character_brief_selected',state['turn_id'],revision=selection['brief']['revision'],
                     selected_count=sum(len(c['items']) for c in selection['brief']['channels'].values()),
                     omitted_count=sum(len(c['omitted']) for c in selection['brief']['channels'].values()))
    elif reason:
        runner.event('character_brief_degraded',state['turn_id'],reason=reason)


def document_text(state,name,raw):
    selection=state.get('character_selection')
    if not selection or name not in selection['fixed_documents']:return raw
    expected=selection['source_hashes'][name]
    require(hashlib.sha256(raw.encode()).hexdigest()==expected,'prepare_inputs_changed')
    return selection['fixed_documents'][name]


def generator_context(state,channel):
    selection=state.get('character_selection')
    if not selection:return None
    brief=selection['brief']
    return {k:copy.deepcopy(v) for k,v in brief.items() if k!='channels'}|{
        'fixed_documents':copy.deepcopy(selection['fixed_documents']),
        'channel':channel,**copy.deepcopy(brief['channels'][channel])}


def validate_reply(runner,state):
    selection=state.get('character_selection')
    if not selection:return
    try:validity=character_sources.revalidate(runner.cfg,selection['dependencies'],runner.clock())
    except ERRORS:
        # An optional read outage cannot stop dialogue; this turn retains the
        # already validated snapshot. The next turn can fall back independently.
        return
    if validity!='changed':return
    state['character_suspended_turn']=state['turn_id']
    from gate import write_json
    write_json(runner.path,state)
    runner.event('character_brief_degraded',state['turn_id'],reason='source_changed',next='prepare')
    raise ControlError('character_reprepare_required')
