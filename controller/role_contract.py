"""Role boundaries over the existing compact decision contract; no Store writes."""
import copy
import re

from common import ControlError, encode, require, text
import decision_contract
import instrumentation
import memory_cues
import prepared_inputs
import reply_generator

ROLES=('life','context','writer','review')
LIFE_FIELDS={'advance_to_ms','next_decision_ms','activities','weekly_plans','appointments',
             'facts','action_intents','world_event_ops','in_person_ops','processed_event_ids',
             'metric_deltas','metric_reason'}
CONTEXT_FIELDS={'advance_to_ms','next_decision_ms','dm_decisions','relationships','facts','memory_ops',
                'action_intents','conversation_updates','continuation_ops','processed_event_ids',
                'metric_deltas','metric_reason','in_person_ops'}
POST_FIELDS={'facts','memory_ops','action_intents','conversation_updates','relationships'}


def digest(value):return instrumentation.digest(value)


def record_input(record):
    return {'prepared':record['prepared'],'inputs':copy.deepcopy(record['inputs']),
            'documents':copy.deepcopy(record['documents']),
            'decision_contract':decision_contract.describe(record),
            'schema_sections':copy.deepcopy(record.get('schema_sections',{})),
            'available_documents':record['available_documents']}


def check_operations(value,allowed):
    require(isinstance(value,dict) and set(value)<=allowed,'role_owned_fields')
    for key,item in value.items():
        if key in ('advance_to_ms','next_decision_ms'):
            require(type(item) is int and item>=0,'role_invalid_time')
        elif key=='metric_deltas':require(isinstance(item,dict),'role_invalid_metrics')
        elif key=='metric_reason':text(item,'role_metric_reason')
        else:require(isinstance(item,list),'role_invalid_operations')
    return copy.deepcopy(value)


def merge(*proposals):
    """Whole operation arrays remain ordered; never overwrite another role's work."""
    result={}
    for proposal in proposals:
        for key,value in proposal.items():
            if key=='next_decision_ms' and key in result:result[key]=min(result[key],value)
            elif key=='metric_reason' and key in result and result[key]!=value:result[key]+='\n'+value
            elif isinstance(value,list):
                for item in value:
                    if item not in result.setdefault(key,[]):result[key].append(copy.deepcopy(item))
            elif isinstance(value,dict):
                target=result.setdefault(key,{})
                if key=='metric_deltas':
                    for metric,delta in value.items():
                        require(type(delta) in (int,float),'role_invalid_metrics')
                        target[metric]=target.get(metric,0)+delta
                else:
                    require(not any(k in target and target[k]!=v for k,v in value.items()),'role_operation_collision')
                    target.update(copy.deepcopy(value))
            else:
                require(key not in result or result[key]==value,'role_operation_collision')
                result[key]=value
    return result


def life_output(output):
    require(isinstance(output,dict) and set(output)=={'operations'},'role_invalid_life')
    return check_operations(output['operations'],LIFE_FIELDS)


def context_output(output,life):
    require(isinstance(output,dict) and set(output)=={'decision','reply_briefs'},'role_invalid_context')
    decision=merge(life,check_operations(output['decision'],CONTEXT_FIELDS))
    require(all(type(decision.get(k)) is int for k in ('advance_to_ms','next_decision_ms')),'role_missing_times')
    dms=decision.get('dm_decisions',[]);briefs=output['reply_briefs']
    require(all(isinstance(dm,dict) for dm in dms),'role_invalid_dm')
    require(isinstance(briefs,list),'role_invalid_briefs')
    wanted={i for i,dm in enumerate(dms) if dm.get('action') in ('reply','initiate')}
    selected={}
    for brief in briefs:
        require(isinstance(brief,dict) and set(brief)=={'index','situation','memory_fact_ids','documents'},
                'role_invalid_brief')
        index=brief['index'];require(type(index) is int and index in wanted and index not in selected,'role_invalid_brief_index')
        text(brief['situation'],'role_situation')
        for key in ('memory_fact_ids','documents'):
            require(isinstance(brief[key],list) and all(isinstance(x,str) for x in brief[key])
                    and len(set(brief[key]))==len(brief[key]),'role_invalid_brief')
        selected[index]=copy.deepcopy(brief)
    require(set(selected)==wanted,'role_missing_brief')
    for index in wanted:
        dm=dms[index]
        require(not ({'parts','generator_id','conversation_review','conversation_plan'} & dm.keys()),'role_context_wrote_draft')
        require(dm.get('channel') in ('slack','instagram'),'reply_channel_required')
    # Recovery of existing partial groups may need original full reviews but may
    # never quietly rewrite an already sent/newly generated message.
    for dm in dms:
        require(dm.get('action')!='revalidate_group' or 'parts' not in dm,'role_existing_draft_changed')
    return decision,selected


def writer_input(session,record,decision,brief,index):
    dm=decision['dm_decisions'][index]
    request={'schema':'reply-gen-v1','turn_id':record['turn_id'],'channel':dm['channel'],
             'situation':brief['situation'],'memories':[],'memory_fact_ids':brief['memory_fact_ids'],
             'target_message_ids':dm.get('message_ids',[])}
    with session.owned():
        state=session.state(record['turn_id'])
        dynamic,binding=reply_generator.source(session,state,request,record['receipts'])
    names={'design/character/profile.md','design/character/goals.md','design/conversation-rules.md',*brief['documents']}
    require(names<=record['documents'].keys(),'role_writer_document_not_read')
    # All selected text is from acknowledged inputs. No arbitrary caller-written
    # memory strings and no other channel's raw conversation enter this packet.
    documents={name:record['documents'][name] for name in sorted(names)}
    current={}
    for item in record['inputs']:
        if item['kind']=='state':
            for name in ('now_ms','state','activity','interaction','conversation_mode'):
                if name in item['content']:current[name]=copy.deepcopy(item['content'][name])
    proposed={k:copy.deepcopy(v) for k,v in decision.items() if k not in ('dm_decisions','processed_event_ids')}
    # Channel-owned proposed memories/intentions are not global writing context.
    for key in ('memory_ops','action_intents','facts','conversation_updates'):
        if key in proposed:
            proposed[key]=[v for v in proposed[key] if isinstance(v,dict) and
                v.get('channel',(v.get('details') or {}).get('channel',dm['channel'])) in ('personal',dm['channel'])]
    dynamic['current']=current;dynamic['proposed_state']=proposed
    # The fixed documents have already been scoped/rendered by prepare.
    if dynamic.get('character_brief'):dynamic['character_brief'].pop('fixed_documents',None)
    return {'reply':dynamic,'documents':documents,'output_contract':{
        'parts':'1..5 text strings',
        'note':'optional string'}},binding


def writer_output(output):
    require(isinstance(output,dict),'role_invalid_writer')
    return reply_generator.validate_output({'choices':[{'finish_reason':'stop','message':{'content':encode(output)}}]})


def review_output(output,decision,drafts):
    require(isinstance(output,dict) and set(output)=={'verdict','reviews','post_write','conversation_ended'}
            and output['verdict']=='approve' and type(output['conversation_ended']) is bool,'role_invalid_review')
    result=merge(decision,check_operations(output['post_write'],POST_FIELDS));seen=set()
    require(isinstance(output['reviews'],list),'role_invalid_review')
    for review in output['reviews']:
        require(isinstance(review,dict) and {'index','conversation_review'}<=set(review)
                and set(review)<={'index','conversation_review','conversation_plan','parts'},'role_invalid_review')
        index=review['index'];require(type(index) is int and index in drafts and index not in seen,'role_invalid_review_index');seen.add(index)
        value=review['conversation_review'];require(isinstance(value,dict) and set(value)==decision_contract.REVIEW_FIELDS,'invalid_compact_review')
        # Boolean checks/evidence are authored by this role, never filled in code.
        dm=result['dm_decisions'][index];draft=drafts[index]
        parts=review.get('parts',draft['output']['parts'])
        require(isinstance(parts,list) and len(parts)==len(draft['output']['parts']),'generator_parts_changed')
        require(parts==draft['output']['parts'],'generator_parts_changed')
        dm.update(parts=copy.deepcopy(parts),conversation_review=copy.deepcopy(value),generator_id=draft['generator_id'])
        if 'conversation_plan' in review:dm['conversation_plan']=copy.deepcopy(review['conversation_plan'])
    require(seen==set(drafts),'role_missing_review')
    return result,output['conversation_ended']
