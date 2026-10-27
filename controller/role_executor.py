#!/usr/bin/env python3
"""Opt-in life/context/writer/review model routing over MuseExecutor.

Only the writer sees wording work. Code owns receipts, turn continuation, exact
commit/recovery and model handoffs; no production scheduler is changed here.
"""
import argparse
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from common import ControlError, encode, file_lock, integer, now_ms, require, required_text
import gate
import driver_ownership
import model_adapter
import muse_executor
import prepared_inputs
import repetition_cues
import reply_generator
import role_contract as contract
import role_preflight
import role_resume

ERRORS=(ControlError,OSError,sqlite3.Error,ValueError,KeyError,TypeError)
MODEL_REPAIR={'role_owned_fields','role_invalid_time','role_invalid_metrics','role_invalid_operations',
    'role_operation_collision','role_invalid_life','role_invalid_context','role_missing_times','role_invalid_dm',
    'role_invalid_briefs','role_invalid_brief','role_invalid_brief_index','role_missing_brief',
    'role_context_wrote_draft','role_existing_draft_changed','reply_channel_required','role_input_not_read',
    'role_invalid_writer','role_invalid_review','role_invalid_review_index','role_missing_review',
    'role_invalid_revision','role_invalid_replan','invalid_compact_review','generator_parts_changed',
    'executor_warning_review_required','invalid_parts','invalid_output',
    'empty','invalid_role_metric_reason'}
CONTEXT_REPAIR={'generator_memory_not_read','role_writer_document_not_read'}
ROOT=Path(__file__).resolve().parent


def settings(cfg):
    raw=cfg.get('role_execution')
    if raw is None or isinstance(raw,dict) and not raw.get('enabled',False):return {'enabled':False}
    require(isinstance(raw,dict) and raw.get('enabled') is True,'invalid_role_settings')
    result={'max_model_calls':32,'max_revisions':3,'max_format_repairs':12,'max_transport_attempts':2,
            'resume_ttl_ms':2*3600_000,**raw}
    for key in ('max_model_calls','max_revisions','max_format_repairs','max_transport_attempts'):
        integer(result[key],key,1);require(result[key]<=128,'invalid_'+key)
    integer(result['resume_ttl_ms'],'resume_ttl_ms',1)
    require(result['resume_ttl_ms']<=86400_000,'invalid_resume_ttl_ms')
    adapters=result.get('adapters')
    require(isinstance(adapters,dict) and set(adapters)==set(contract.ROLES)
            and all(isinstance(v,str) and v for v in adapters.values()),'role_adapters_required')
    result['path']=Path(result.get('path',Path(cfg['state_dir'])/'role-execution')).expanduser().resolve()
    return result


def load_config(path):
    cfg=muse_executor.load_config(path)
    if isinstance(cfg.get('reply_generator'),dict):
        for key in ('rules_file','key_file'):
            if cfg['reply_generator'].get(key):
                item=Path(cfg['reply_generator'][key]).expanduser()
                cfg['reply_generator'][key]=str(item if item.is_absolute() else Path(path).resolve().parent/item)
    if isinstance(cfg.get('role_execution'),dict):
        for key in ('path','rules_file'):
            if isinstance(cfg['role_execution'].get(key),str) and cfg['role_execution'][key]:
                item=Path(cfg['role_execution'][key]).expanduser()
                cfg['role_execution'][key]=str(item if item.is_absolute() else Path(path).resolve().parent/item)
    return model_adapter.resolve_paths(cfg,path)


def save(path,value):muse_executor.private_write(Path(path),encode(value))


def note(folder,clock,phase,**metadata):
    try:
        with os.fdopen(os.open(folder/'trace.jsonl',os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600),'a') as stream:
            os.fchmod(stream.fileno(),0o600);stream.write(encode({'at_ms':clock(),'phase':phase,**metadata})+'\n')
    except OSError:pass


def read_response(path):
    with Path(path).open('rb') as stream:raw=stream.read(model_adapter.MAX_RESPONSE_BYTES+1)
    require(len(raw)<=model_adapter.MAX_RESPONSE_BYTES,'role_response_too_large')
    return json.loads(raw.decode('utf-8'))


class RoleExecutor:
    def __init__(self,cfg,session,clock=now_ms,transport=None):
        self.cfg=cfg;self.opts=settings(cfg);require(self.opts['enabled'],'role_execution_off')
        self.session=session;self.executor=muse_executor.MuseExecutor(session);self.clock=clock;self.transport=transport
        self.folder=self.opts['path'];self.folder.mkdir(parents=True,exist_ok=True,mode=0o700);self.folder.chmod(0o700)
        self.path=self.folder/('run-'+session.wake+'.json')
        self.binding={'db':str(Path(cfg['db']).resolve()),'session':session.session,'wake':session.wake,
                      'persona':cfg.get('persona_id','yujin'),'state_dir':str(session.state_dir.resolve())}

    def load(self):
        state=gate.load_json(self.path,None)
        if state:require(state['binding']==self.binding,'role_binding_mismatch')
        return state

    def persist(self,state):
        state.setdefault('created_ms',self.clock());state['updated_ms']=self.clock();save(self.path,state)

    def fresh(self):
        with self.session.owned():prepared_inputs.fresh(self.session,self.session.state())

    def start(self,previous=None):
        prepared=self.executor.prepare(decision_format='compact-v1')
        if prepared.get('next')=='done':return {'phase':'done','binding':self.binding,'result':prepared,'accepted':{}}
        record=self.executor.load();manifest=record['prepared']['snapshot']['work_manifest']
        legacy=self.cfg.get('reply_generator') or {}
        rules_path=self.opts.get('rules_file') or legacy.get('rules_file')
        require(not legacy.get('enabled') or bool(rules_path),'generator_rules_file_required')
        writing_rules=required_text(Path(rules_path).expanduser()) if rules_path else None
        require(writing_rules is None or bool(writing_rules.strip()),'generator_rules_empty')
        state={'binding':self.binding,'phase':'life' if manifest['life_required'] or manifest['primary']=='life' else 'context',
               'turn_id':record['turn_id'],'generation':self.session.state()['reply_source']['generation'],'life':{},'decision':None,'briefs':{},'drafts':{},'draft_cache':{},
               'pending':None,'calls':0,'revisions':0,'format_repairs':0,'accepted':(previous or {}).get('accepted',{}),
               'feedback':None,'submission':None,'writing_rules':writing_rules}
        candidate=role_resume.find_candidate(self,record)
        state['resume_candidate']=candidate
        state['cache_disabled']=bool(candidate and candidate.get('cache_disabled'))
        if candidate:
            names={name for brief in candidate['briefs'].values() for name in brief.get('documents',[])}
            for name in sorted(names & set(record['available_documents']) - record['documents'].keys()):
                self.executor.collect(record,{'kind':'document','options':{'name':name}})
            if names:
                self.executor.publish(record);record=self.executor.load()
        if state['phase']=='life' and candidate and candidate['life']:
            state.update(phase='context',life_revalidation_required=True)
        if candidate:note(self.folder,self.clock,'resume_candidate',source_wake=candidate['source_wake'])
        self.persist(state);return state

    def wrapper(self,record,action,**fields):
        return {'request_id':record['request_id'],'input_sha256':record['input_sha256'],
                'input_read':True,'action':action,**fields}

    def payload(self,state,role,index=None):
        record=self.executor.load()
        if role=='writer':
            brief=state['briefs'][str(index)]
            payload,binding=contract.writer_input(self.session,record,state['decision'],brief,index)
            if state.get('writing_rules') is not None:payload['writing_rules']=state['writing_rules']
            if state.get('feedback'):payload['controller_feedback']=state['feedback']
            if state.get('writer_feedback',{}).get(str(index)):
                payload['revision_feedback']=state['writer_feedback'][str(index)]
                payload['revision_attempt']=state['revisions']
            return payload,binding
        payload=contract.record_input(record)
        payload['control_now_ms']=self.clock()
        if state.get('writing_rules') is not None:payload['writing_rules']=state['writing_rules']
        payload['life_proposal']=state['life'];payload['feedback']=state['feedback']
        if role=='context':
            payload.update(role_output_contract=context_card(),resume_candidate=state.get('resume_candidate'),
                           life_revalidation_required=state.get('life_revalidation_required',False),
                           previous_decision=state.get('decision'),previous_briefs=state.get('briefs'))
        elif role=='life':payload['role_output_contract']={'operations':sorted(contract.LIFE_FIELDS)}
        else:
            payload.update(decision=state['decision'],drafts={str(i):d['output'] for i,d in state['drafts'].items()},
                           role_output_contract=review_card(),warning=record.get('warning'))
            rep_metrics=self.repetition_review(state,record)
            if rep_metrics is not None:payload['repetition_check']=rep_metrics
        return payload,None

    def repetition_review(self,state,record):
        """Draft-vs-recent repetition metrics for the review role.

        None when the guard is off or no check data was prepared, so review
        inputs stay unchanged in the default configuration."""
        try:
            with self.session.owned():session_state=self.session.state(record['turn_id'])
        except ERRORS:return None
        checks=(session_state or {}).get('repetition_check') or {}
        if not checks:return None
        opts=repetition_cues.guard_settings(self.cfg)
        result={}
        for index,draft in state['drafts'].items():
            dm=state['decision']['dm_decisions'][int(index)]
            check=checks.get(dm.get('channel'))
            if check is None:continue
            metrics=repetition_cues.check_drafts(draft['output'].get('parts',[]),check,opts)
            result[str(index)]={key:metrics[key] for key in
                ('verbatim_max','phrase_hits','opener_repeat','closer_repeat',
                 'draft_chars','prev_turn_chars','turns_considered')}
        return result or None

    def new_task(self,state,role,index=None):
        require(state['calls']<self.opts['max_model_calls'],'role_call_budget')
        self.fresh()
        payload,binding=self.payload(state,role,index)
        instructions=(ROOT/'runbooks/roles'/str(role+'.md')).read_text(encoding='utf-8')
        opts=model_adapter.options(self.cfg,self.opts['adapters'][role])
        with self.session.owned():budget=self.session.budget(self.session.state())
        require(budget['remaining_ms']>5000,'role_time_budget')
        ident=uuid.uuid4().hex
        sha=contract.digest({'role':role,'input':payload,'instructions':instructions,
                             'binding':self.binding,'turn_id':state['turn_id']})
        req=model_adapter.request('persona_'+role,ident,sha,instructions,payload,opts,
                                  self.clock()+budget['remaining_ms']-1000)
        pending={'id':ident,'role':role,'index':index,'request':req,'options':opts,'writer_binding':binding,
                 'prepared_ms':self.clock(),
                 'executor_request_id':self.executor.load()['request_id']}
        if opts['backend']=='file':save(self.folder/('role-model-input-'+ident+'.json'),req)
        state['pending']=pending;state['calls']+=1;self.persist(state)
        note(self.folder,self.clock,'role_prepared',request_id=ident,role=role,model=opts.get('model'),
             input_sha256=sha,input_bytes=len(encode(req).encode()))
        return pending

    def pickup_split(self,pending):
        """Session-wait vs model-work split for one attempt, from the pickup
        marker the session leaves when it picks the input up (role_response.py
        pickup). Empty when no marker exists, so uninstrumented sessions and
        reused drafts keep the old trace shape."""
        if not pending:return {}
        marker=gate.load_json(self.folder/('role-model-pickup-'+pending['id']+'.json'),None)
        if not isinstance(marker,dict) or type(marker.get('picked_up_ms')) is not int:return {}
        prepared=pending.get('prepared_ms')
        if type(prepared) is not int:return {}
        return {'pickup_wait_ms':max(0,marker['picked_up_ms']-prepared),
                'model_work_ms':max(0,self.clock()-marker['picked_up_ms'])}

    def repair(self,state,reason,output=None,target=None,mechanical=False):
        pending=state.get('pending')
        split=self.pickup_split(pending)
        key='format_repairs' if mechanical else 'revisions'
        state[key]=state.get(key,0)+1
        require(state[key]<=self.opts['max_format_repairs' if mechanical else 'max_revisions'],
                'role_format_budget' if mechanical else 'role_revision_budget')
        role=target or pending['role']
        feedback={'kind':'role_contract_error','reason':reason}
        if output is not None:feedback['rejected_output']=copy.deepcopy(output)
        state.update(phase=role,feedback=feedback,pending=None)
        if role=='context':state['drafts']={}
        if pending:
            state.setdefault('rejected',[]).append(pending['id'])
        self.persist(state)
        if pending:self.cleanup(pending['id'])
        note(self.folder,self.clock,'role_format_correction' if mechanical else 'role_correction',role=role,reason=reason,
             format_repairs=state.get('format_repairs',0),revisions=state['revisions'],
             wake_id=self.session.wake,turn_id=state.get('turn_id'),**split)

    def accept_or_repair(self,state,value):
        result=role_preflight.check(state['pending']['request'],value,self.clock())
        if not result['ok']:
            if result['reason']=='role_request_expired':raise ControlError(result['reason'])
            self.repair(state,result['reason'],value.get('output') if isinstance(value,dict) else None,
                        target=result.get('target'),mechanical=result['repair_kind']!='semantic')
            return
        try:self.accept(state,value)
        except ControlError as exc:
            if exc.reason not in MODEL_REPAIR:raise
            self.repair(state,exc.reason,value.get('output'))

    def cleanup(self,ident):
        for part in ('input','response','pickup'):
            try:(self.folder/('role-model-'+part+'-'+ident+'.json')).unlink(missing_ok=True)
            except OSError:pass

    def register_writer(self,state,pending,output):
        index=pending['index'];record=self.executor.load();binding=pending['writer_binding']
        ident='gen-'+contract.digest([self.binding,state['turn_id'],pending['request']['input_sha256']])[:32]
        value={'binding':binding,'request':pending['request']['input']['reply'],
               'prompt_hash':pending['request']['input_sha256'],'attempts':1,
               'result':{'ok':True,**output,'generator_id':ident},'role_request_id':pending['id']}
        path=reply_generator.record_path(self.session,ident)
        old=gate.load_json(path,None)
        require(old is None or old==value,'role_writer_identity_collision')
        save(path,value)
        draft={'output':output,'generator_id':ident,'input_sha256':pending['request']['input_sha256']}
        state['drafts'][str(index)]=draft
        # Cache only the exact scoped writing payload in this turn/generation.
        state['draft_cache'][contract.digest(pending['request']['input'])]=draft

    def reuse_writer(self,state,index,payload,binding):
        if state.get('cache_disabled'):return False
        instructions=(ROOT/'runbooks/roles/writer.md').read_text(encoding='utf-8')
        opts=model_adapter.options(self.cfg,self.opts['adapters']['writer'])
        cached=role_resume.find_draft(self,payload,instructions,opts)
        if cached is None:return False
        self.fresh()
        sha=contract.digest({'role':'writer','input':payload,'instructions':instructions,'binding':self.binding,
                             'turn_id':state['turn_id']})
        pending={'id':'reused-'+cached['key'][:24],'index':index,'writer_binding':binding,
                 'request':{'input':payload,'input_sha256':sha}}
        self.register_writer(state,pending,cached['output'])
        state['drafts'][str(index)]['recovery_key']=cached['key'];self.persist(state)
        note(self.folder,self.clock,'writer_draft_reused',index=index,source_wake=cached['source_wake'],
             source_created_ms=cached['created_ms'])
        return True

    def accept(self,state,value):
        pending=state['pending'];req=pending['request']
        normalized=model_adapter.validate_result(value,req);output=copy.deepcopy(normalized['output'])
        require(isinstance(output,dict) and output.pop('input_read',None) is True,'role_input_not_read')
        require(self.clock()<req['deadline_ms'],'role_request_expired')
        self.fresh();require(isinstance(output,dict),'role_output_invalid')
        role=pending['role'];record=self.executor.load()
        if set(output)=={'read'}:
            require(role!='writer','role_writer_cannot_read_tools')
            require(isinstance(output['read'],list) and output['read'],'role_invalid_reads')
            # A completed read can be replayed after a crash before state save.
            if record['request_id']==pending['executor_request_id']:
                self.executor.respond(self.wrapper(record,'read',requests=output['read']))
        elif role=='life':
            state['life']=contract.life_output(output);state['phase']='context';state['feedback']=None
        elif role=='context':
            output,life=role_preflight.resume_life(output,pending['request']['input'])
            if output is None:
                state.update(phase='life',life_revalidation_required=False)
            else:
                state['life']=life
                state['life_revalidation_required']=False
                decision,briefs=contract.context_output(output,life)
                state.update(decision=decision,briefs={str(k):v for k,v in briefs.items()},drafts={},phase='writer',feedback=None)
        elif role=='writer':
            out=contract.writer_output(output)
            self.register_writer(state,pending,out)
            try:state['drafts'][str(pending['index'])]['recovery_key']=role_resume.remember(self,pending,out)
            except (OSError,ValueError,TypeError):
                note(self.folder,self.clock,'draft_cache_unavailable',request_id=pending['id'])
        else:
            if output.get('verdict')=='revise':
                require(set(output)=={'verdict','feedback'} and isinstance(output['feedback'],list) and output['feedback'],'role_invalid_revision')
                state['revisions']+=1;require(state['revisions']<=self.opts['max_revisions'],'role_revision_budget')
                for item in output['feedback']:
                    require(isinstance(item,dict) and set(item)=={'index','reason'},'role_invalid_revision')
                    index=str(item['index']);require(index in state['drafts'] and isinstance(item['reason'],str) and item['reason'].strip(),'role_invalid_revision')
                    old_draft=state['drafts'].pop(index)
                    state['draft_cache']={k:v for k,v in state['draft_cache'].items()
                                          if v['generator_id']!=old_draft['generator_id']}
                    try:role_resume.invalidate(self,old_draft)
                    except (OSError,ValueError):state['cache_disabled']=True
                    state.setdefault('writer_feedback',{})[index]=item['reason']
                state.update(phase='writer',feedback=None)
            elif output.get('verdict')=='replan':
                require(set(output)=={'verdict','target','reason'} and output['target'] in ('life','context')
                        and isinstance(output['reason'],str) and output['reason'].strip(),'role_invalid_replan')
                state['revisions']+=1;require(state['revisions']<=self.opts['max_revisions'],'role_revision_budget')
                state.update(phase=output['target'],feedback={'reason':output['reason']},drafts={})
            elif output.get('verdict')=='approve_warning':
                require(set(output)=={'verdict','reason'} and isinstance(output['reason'],str) and output['reason'].strip()
                        and record.get('warning'),'executor_warning_review_required')
                state.update(phase='commit',submission=self.wrapper(record,'approve_warning',reason=output['reason']))
            else:
                decision,ended=contract.review_output(output,state['decision'],{int(i):d for i,d in state['drafts'].items()})
                state.update(phase='commit',submission=self.wrapper(record,'commit',decision=decision,conversation_ended=ended))
        state['accepted'][pending['id']]=contract.digest(value)
        split=self.pickup_split(pending)
        state['pending']=None;self.persist(state);self.cleanup(pending['id'])
        note(self.folder,self.clock,'role_completed',role=role,request_id=pending['id'],usage=normalized['usage'],
             **split)

    def pending_result(self,state):
        p=state['pending'];return {'ok':True,'reason':'model_handoff','next':'model','wake_id':self.session.wake,
            'role':p['role'],'model':p['options'].get('model'),'run_id':p['id'],'request_id':p['id'],
            'input_file':str(self.folder/('role-model-input-'+p['id']+'.json')),
            'response_file':str(self.folder/('role-model-response-'+p['id']+'.json')),
            'deadline_ms':p['request']['deadline_ms']}

    def controller_feedback(self,state,record):
        require(record.get('feedback') is not None,'role_feedback_missing')
        state.update(phase='review',feedback={'kind':'controller_feedback',
            'feedback':copy.deepcopy(record['feedback']),'recovery':copy.deepcopy(self.session.last_recovery),
            'submitted':copy.deepcopy(state['submission']),'warning':record.get('warning')},submission=None)
        self.persist(state)

    def commit(self,state):
        # The underlying executor journals the exact expanded decision and uses
        # existing idempotency/finish recovery. No model call while committing.
        record=self.executor.load()
        if record['stage'] in ('committing','completed'):result=self.executor.recover()
        else:
            current=self.session.state()
            require(record['stage']!='invalidated' and current['reply_source']['generation']==state['generation'],
                    'prepare_inputs_changed')
            if record['request_id']!=state['submission']['request_id']:
                self.controller_feedback(state,record);return None
            try:result=self.executor.respond(state['submission'])
            except sqlite3.IntegrityError:
                # A rejected operation (e.g. two active activities) is model
                # feedback, not a transport retry of the same invalid decision.
                current=self.executor.load()
                result=self.executor.publish(current,{'kind':'validation_error',
                    'reason':'controller_integrity_error','response':current.get('response')})
                self.session.last_recovery={'reason':'controller_integrity_error','next':'correct_and_retry_commit'}
        if result.get('next')=='muse':
            self.controller_feedback(state,self.executor.load());return None
        state.update(phase='done',result=result);self.persist(state)
        note(self.folder,self.clock,'turn_completed',turn_id=state['turn_id'],committed=result.get('committed',False),
             next=result.get('after',{}).get('next'))
        return result

    def abandon(self,state,reason):
        # Release only this claim; leave incoming messages and existing jobs in
        # their current state for the normal gate retry/backstop. No goodbye.
        with self.session.owned():
            self.session.store.end_conversation(self.session.session,self.clock())
            gate.write_json(self.session.state_dir/'wake_ack.json',{
                'wake_id':self.session.wake,'phase':'failed','at_ms':self.clock(),'reason':reason})
            self.session.event('ack_failed',state['turn_id'],reason=reason)
        if state.get('pending'):self.cleanup(state['pending']['id'])
        result={'ok':False,'reason':reason,'next':'done','retry_via':'gate'}
        state.update(phase='done',pending=None,result=result);self.persist(state)
        note(self.folder,self.clock,'role_abandoned',reason=reason,calls=state['calls'],
             revisions=state['revisions'],format_repairs=state.get('format_repairs',0),
             wake_id=self.session.wake,turn_id=state.get('turn_id'))
        return result

    def run(self):
        driver_ownership.check(self.cfg)
        with file_lock(self.folder/'driver','role-execution',private=True):
            state=self.load() or self.start()
            for unused in range(self.opts['max_model_calls']*3+12):
                try:
                    if state['phase']=='done':return state['result']
                    if state['phase']=='commit':
                        result=self.commit(state)
                        if result is not None:return result
                        continue
                    if state['pending']:
                        p=state['pending'];response=self.folder/('role-model-response-'+p['id']+'.json')
                        if response.is_file():
                            try:value=read_response(response)
                            except (ValueError,UnicodeError):
                                self.repair(state,'role_invalid_envelope',mechanical=True);continue
                            self.accept_or_repair(state,value);continue
                        require(self.clock()<p['request']['deadline_ms'],'role_request_expired')
                        self.fresh()
                        if p['options']['backend']=='file':return self.pending_result(state)
                        require(p.get('attempts',0)<self.opts['max_transport_attempts'],'role_transport_exhausted')
                        p['attempts']=p.get('attempts',0)+1;self.persist(state)
                        started=time.monotonic()
                        timeout=min(p['options']['timeout_seconds'],(p['request']['deadline_ms']-self.clock())/1000)
                        value=model_adapter.invoke(p['options'],p['request'],timeout,self.transport)
                        save(response,value)  # Keep a paid result before any further source checks/writes.
                        note(self.folder,self.clock,'role_model_response',role=p['role'],request_id=p['id'],
                             elapsed_ms=int((time.monotonic()-started)*1000),usage=value['usage'])
                        self.accept_or_repair(state,value);continue
                    role=state['phase'];index=None
                    if role=='writer':
                        missing=sorted(int(i) for i in state['briefs'] if i not in state['drafts'])
                        if not missing:state['phase']='review';self.persist(state);continue
                        index=missing[0];payload,binding=self.payload(state,'writer',index)
                        cached=state['draft_cache'].get(contract.digest(payload))
                        if cached:
                            state['drafts'][str(index)]=cached;self.persist(state);continue
                        if self.reuse_writer(state,index,payload,binding):continue
                    self.new_task(state,role,index)
                except ControlError as exc:
                    if exc.reason in ('role_call_budget','role_revision_budget','role_format_budget','role_time_budget','role_request_expired','role_transport_exhausted'):
                        return self.abandon(state,exc.reason)
                    if exc.reason in CONTEXT_REPAIR:
                        try:self.repair(state,exc.reason,target='context',mechanical=True)
                        except ControlError as budget_error:
                            if budget_error.reason=='role_format_budget':return self.abandon(state,budget_error.reason)
                            raise
                        continue
                    if exc.reason in ('model_output_invalid','role_response_too_large'):
                        try:self.repair(state,exc.reason,mechanical=True)
                        except ControlError as budget_error:
                            if budget_error.reason=='role_format_budget':return self.abandon(state,budget_error.reason)
                            raise
                        continue
                    if exc.reason in muse_executor.REPREPARE:
                        self.cleanup(state['pending']['id']) if state.get('pending') else None
                        state=self.start(state)
                        return {'ok':True,'reason':'inputs_changed','next':'run','same_wake':True,'wake_id':self.session.wake}
                    raise
            return self.abandon(state,'role_step_budget')

    def complete(self,ident,path):
        require(bool(re.fullmatch('[0-9a-f]{32}',ident)),'invalid_role_request_id')
        with file_lock(self.folder/'driver','role-execution',private=True):
            state=self.load();require(state is not None,'role_run_missing')
            if ident in state['accepted']:
                if Path(path).is_file():require(contract.digest(read_response(path))==state['accepted'][ident],'role_response_changed')
                return {'ok':True,'reason':'already_accepted','next':'run','replayed':True,'wake_id':self.session.wake}
            with self.session.owned():pass
            require(ident not in state.get('rejected',[]),'role_request_superseded')
            require((state.get('pending') or {}).get('id')==ident,'role_request_mismatch')
            value=read_response(path)
            normalized=model_adapter.validate_result(value,state['pending']['request'])
            require(self.clock()<state['pending']['request']['deadline_ms'],'role_request_expired')
            target=self.folder/('role-model-response-'+ident+'.json');save(target,normalized)
        return self.run()

    def next_turn(self):
        with file_lock(self.folder/'driver','role-execution',private=True):
            state=self.load();require(state and state['phase']=='done','role_turn_not_finished')
            require(state['result'].get('after',{}).get('next')=='prepare','role_no_next_turn')
            self.start(state)
        return self.run()


def context_card():
    return {'decision':'compact-v1 operations; no parts/generator_id/conversation_review/conversation_plan on new reply/initiate',
            'allowed_fields':sorted(contract.CONTEXT_FIELDS),
            'documents':'Select only keys present in input.documents, not merely available_documents; use read otherwise.',
            'time':'Choose advance_to_ms/next_decision_ms as intended times; if already past at submission, the Store re-bases them on the commit time and keeps the intended gap.',
            'resume_life':'Only when life_revalidation_required: explicitly revalidate the candidate life operations. '
                          'Return resume_life=operations with unchanged non-clock content and model-authored current clocks, '
                          'plus the usual decision/reply_briefs. To decline return only {resume_life:null} to run life again. '
                          'Historical proposals are not applied facts. Never copy expired authority/reviews.',
            'reply_briefs':[{'index':0,'situation':'verified situation and intended response direction',
                             'memory_fact_ids':[],'documents':[]}],
            'read':'Alternatively {"read":[existing executor read requests]}'}


def review_card():
    return {'verdict':'approve|revise|replan|approve_warning',
            'approve':{'reviews':[{'index':0,'conversation_review':'exact compact review with four model-authored checks',
                                   'conversation_plan':'existing object only when needed'}],
                       'post_write':sorted(contract.POST_FIELDS),'conversation_ended':'boolean, intentional dialogue ending only'},
            'revise':{'feedback':[{'index':0,'reason':'specific correction for writer'}]},
            'replan':{'target':'life|context','reason':'specific state/plan error'},
            'approve_warning':'reason for exact stored warning; never automatic',
            'read':'Alternatively {"read":[existing executor read requests]}'}


def entry(cfg,wake=None,clock=now_ms,transport=None,invocation_id=None):
    opts=settings(cfg)
    if not opts['enabled']:return {'ok':True,'reason':'off','next':'done'}
    driver_ownership.check(cfg)
    if wake is None:
        active=gate.load_json(opts['path']/'active.json',{})
        if active.get('wake') and gate.claim_is_current(Path(cfg['state_dir']),active['wake'],clock()):wake=active['wake']
        else:
            if active.get('wake') and re.fullmatch(r'[A-Za-z0-9_:-]{1,80}',active['wake']):
                old_path=opts['path']/('run-'+active['wake']+'.json')
                old=gate.load_json(old_path,{})
                pending=old.get('pending') or {}
                ident=pending.get('id','')
                if re.fullmatch('[0-9a-f]{32}',ident):
                    for part in ('input','response'):
                        (opts['path']/('role-model-'+part+'-'+ident+'.json')).unlink(missing_ok=True)
                    old.update(phase='abandoned',pending=None,result={'ok':False,'reason':'stale_claim','next':'stop'})
                    save(old_path,old)
            with redirect_stdout(io.StringIO()):
                code,claimed=gate.claim_work(cfg,invocation_id) if invocation_id else gate.claim_work(cfg)
            if code==gate.SILENT:return {'ok':True,'reason':'no_claim','next':'done'}
            require(code==gate.WAKE and claimed,'executor_claim_failed');wake=claimed['wake_id']
            save(opts['path']/'active.json',{'wake':wake})
    session=muse_executor.DecisionSession(cfg,wake,clock)
    worker=RoleExecutor(cfg,session,clock,transport)
    state=worker.load()
    if state and state['phase']=='done' and state['result'].get('after',{}).get('next')=='prepare':
        return worker.next_turn()
    return worker.run()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',required=True);parser.add_argument('--wake-id');parser.add_argument('--invocation-id')
    sub=parser.add_subparsers(dest='action',required=True);sub.add_parser('run');sub.add_parser('next-turn')
    complete=sub.add_parser('complete');complete.add_argument('--run-id',required=True);complete.add_argument('--response',required=True)
    args=parser.parse_args(argv)
    try:
        cfg=load_config(args.config);opts=settings(cfg)
        if not opts['enabled']:result={'ok':True,'reason':'off','next':'done'}
        elif args.action=='run':result=entry(cfg,args.wake_id,invocation_id=args.invocation_id)
        else:
            wake=args.wake_id
            if wake is None:
                matches=[]
                if args.action=='complete':
                    for path in opts['path'].glob('run-*.json'):
                        state=gate.load_json(path,{})
                        if (state.get('pending') or {}).get('id')==args.run_id or args.run_id in state.get('accepted',{}):matches.append(state['binding']['wake'])
                    require(len(matches)==1,'role_request_mismatch');wake=matches[0]
                else:wake=gate.load_json(opts['path']/'active.json',{}).get('wake')
            require(bool(wake),'wake_id_required')
            worker=RoleExecutor(cfg,muse_executor.DecisionSession(cfg,wake))
            result=worker.complete(args.run_id,args.response.removeprefix('@')) if args.action=='complete' else worker.next_turn()
    except ERRORS as exc:
        reason=exc.reason if isinstance(exc,ControlError) else 'role_executor_io_error'
        result={'ok':False,'reason':reason,'next':'stop' if reason=='stale_claim' else
                'prepare' if reason in muse_executor.REPREPARE else 'retry_same_request' if reason in
                ('busy','role_executor_io_error','network_error','model_timeout') else 'inspect'}
    print(encode(result));return 0 if result.get('ok') else 10 if result.get('reason')=='stale_claim' else 1


if __name__=='__main__':raise SystemExit(main())
