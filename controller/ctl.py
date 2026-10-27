#!/usr/bin/env python3
"""JSON CLI. No model, scheduler, or Instagram credentials embedded."""
import argparse
import hashlib
import json
import re
import uuid
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from common import ControlError, decode, encode, now_ms, require
from message_limits import MAX_PARTS
import persona
from dispatch import Dispatcher, ExternalTransport, MockTransport
from store import Store
import slack_delivery
import conversation_flow
import topic_materials
import topic_producer
import instrumentation
import world_events
import memory_continuity


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ControlError('invalid_arguments', message)


def document(value):
    if value == '@-':
        return decode(sys.stdin.read())
    if value.startswith('@'):
        return decode(Path(value[1:]).expanduser().read_text(encoding='utf-8-sig'))
    return decode(value)


def main(argv=None):
    apply_telemetry = {}
    command_started_ms = now_ms()
    started_monotonic_ns = time.monotonic_ns()
    if hasattr(sys.stdout,'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser=Parser(description='Persona DM controller (Python 3.11+, standard library).')
    parser.add_argument('--db',default=str(Path(__file__).parent/'controller.db'))
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('seed','ingest','tick','apply','send-due','recover-stale','reconcile','recall','status','backup','maintenance',
                 'wait-dm','end-conversation','release-lease','intent-inspect',
                 'slack-delivery-configure','slack-delivery-pending','slack-delivery-observe',
                 'slack-delivery-reconcile','slack-delivery-inspect','slack-history-claim','slack-history-backoff',
                 'slack-history-sync','slack-delivery-report',
                 'conversation-configure','conversation-context','conversation-preflight',
                 'topic-materials','topic-search-claim','topic-search-result',
                 'topic-producer-claim','topic-producer-result','topic-producer-inspect',
                 'event-schema','event-import','event-inspect','event-list','event-cancel',
                 'memory-configure','memory-context','memory-recall','memory-history-search','memory-diagnostics'):
        p=sub.add_parser(name)
        p.add_argument('--session',default='main')
        if name=='event-import':
            p.add_argument('--input',required=True)
            p.add_argument('--instruction-ref',required=True)
            p.add_argument('--dry-run',action='store_true')
        if name in ('event-inspect','event-cancel'): p.add_argument('--id',required=True)
        if name in ('event-inspect','event-list'): p.add_argument('--operator',action='store_true')
        if name=='event-list':
            p.add_argument('--offset',type=int,default=0)
            p.add_argument('--limit',type=int,default=20)
        if name=='event-cancel': p.add_argument('--reason',required=True)
        if name=='seed':
            p.add_argument('--import',dest='data',default='{}')
            p.add_argument('--dry-run',action='store_true')
        if name=='ingest': p.add_argument('--batch',required=True)
        if name=='apply': p.add_argument('--decision',required=True)
        if name in ('tick','apply'):
            p.add_argument('--turn-id')
            p.add_argument('--wake-id')
        if name=='conversation-preflight': p.add_argument('--decision',required=True)
        if name=='conversation-configure': p.add_argument('--settings',required=True)
        if name=='memory-configure': p.add_argument('--settings',required=True)
        if name=='memory-diagnostics':
            p.add_argument('--decision-id')
            p.add_argument('--offset',type=int,default=0)
            p.add_argument('--limit',type=int,default=20)
        if name in ('memory-context','memory-history-search'):
            p.add_argument('--channel',choices=('slack','instagram'),required=True)
            p.add_argument('--scope',action='append',dest='scopes')
        if name=='memory-context':
            p.add_argument('--key')
            p.add_argument('--history',action='store_true')
        if name in ('memory-context','memory-recall','memory-history-search'):
            p.add_argument('--offset',type=int,default=0)
            p.add_argument('--limit',type=int,default=6 if name=='memory-context' else 10)
        if name in ('memory-recall','memory-history-search'):
            p.add_argument('--keywords',nargs='+',required=True)
            p.add_argument('--order',choices=('newest','oldest'),default='newest')
            p.add_argument('--before-ms',type=int)
        if name in ('conversation-context','topic-materials'):
            p.add_argument('--offset',type=int,default=0)
            p.add_argument('--limit',type=int,default=30)
        if name=='conversation-context': p.add_argument('--subject')
        if name in ('topic-search-result','topic-producer-result'): p.add_argument('--result',required=True)
        if name=='topic-producer-inspect': p.add_argument('--limit',type=int,default=20)
        if name=='tick':
            p.add_argument('--view',choices=('full','dm','life','gate'),default='full')
            p.add_argument('--known-core-rules-hash')
            p.add_argument('--after',type=int,default=0)
            p.add_argument('--limit',type=int,default=50)
        if name=='send-due':
            p.add_argument('--transport-config')
            p.add_argument('--mock',choices=('confirmed','submitted','failed','unknown','429'),default='confirmed')
            p.add_argument('--channel',default=persona.DEFAULT_CHANNEL)
            p.add_argument('--dry-run',action='store_true')
            p.add_argument('--limit',type=int,default=MAX_PARTS)
        if name=='reconcile': p.add_argument('--evidence',required=True)
        if name=='slack-delivery-configure': p.add_argument('--settings',required=True)
        if name=='slack-delivery-observe': p.add_argument('--observation',required=True)
        if name=='slack-delivery-reconcile': p.add_argument('--job-id',required=True)
        if name=='slack-delivery-inspect': p.add_argument('--job-id',required=True)
        if name=='slack-history-backoff': p.add_argument('--retry-after-ms',required=True,type=int)
        if name=='slack-history-sync': p.add_argument('--blocked',required=True,choices=('true','false'))
        if name=='recall':
            p.add_argument('--keywords',nargs='*',default=[])
            p.add_argument('--limit',type=int,default=10)
            p.add_argument('--offset',type=int,default=0)
        if name=='status': p.add_argument('--human',action='store_true')
        if name=='backup': p.add_argument('--output',required=True)
        if name=='wait-dm': p.add_argument('--timeout',type=int,default=180)
        if name=='release-lease': p.add_argument('--owner',required=True)
        if name=='intent-inspect': p.add_argument('--id',required=True)
    args=None
    try:
        args=parser.parse_args(argv)
        for field in ('turn_id','wake_id'):
            value=getattr(args,field,None)
            require(value is None or re.fullmatch(r'[A-Za-z0-9_:-]{1,80}',value),'invalid_trace_id')
        if args.command=='tick' and args.view in ('dm','life') and not args.turn_id:
            args.turn_id=uuid.uuid4().hex
        store=Store(args.db)
        dispatcher=Dispatcher(store)
        now=now_ms()
        cmd=args.command
        if cmd=='event-schema': result={'ok':True,'reason':'event_schema','schema':world_events.schema()}
        elif cmd=='event-import': result=store.event_import(args.session,document(args.input),now,args.instruction_ref,args.dry_run)
        elif cmd=='event-inspect': result=store.event_inspect(args.session,args.id,now,args.operator)
        elif cmd=='event-list': result=store.event_list(args.session,now,args.offset,args.limit,args.operator)
        elif cmd=='event-cancel': result=store.event_cancel(args.session,args.id,now,args.reason)
        elif cmd=='seed': result=store.seed(document(args.data),now,args.session,args.dry_run)
        elif cmd=='ingest': result=store.ingest(args.session,document(args.batch),now)
        elif cmd=='tick':
            result=store.tick(args.session,now,args.after,args.limit,args.view,args.known_core_rules_hash)
            if args.turn_id: result['turn_id']=args.turn_id
        elif cmd=='apply': result=store.apply(args.session,document(args.decision),now,telemetry=apply_telemetry)
        elif cmd=='conversation-preflight': result=store.apply(args.session,document(args.decision),now,dry_run=True,telemetry=apply_telemetry)
        elif cmd=='conversation-configure': result=conversation_flow.configure(store,args.session,document(args.settings),now)
        elif cmd=='memory-configure': result=memory_continuity.configure(store,args.session,document(args.settings),now)
        elif cmd=='memory-context': result=store.memory_context(args.session,args.channel,args.scopes,
            key=args.key,history=args.history,offset=args.offset,limit=args.limit)
        elif cmd=='memory-recall': result=store.memory_recall(args.session,args.keywords,
            order=args.order,offset=args.offset,limit=args.limit,before_ms=args.before_ms)
        elif cmd=='memory-history-search': result=store.memory_history_search(args.session,args.channel,args.keywords,
            args.scopes,order=args.order,offset=args.offset,limit=args.limit,before_ms=args.before_ms)
        elif cmd=='memory-diagnostics': result=store.memory_diagnostics(args.session,args.decision_id,args.offset,args.limit)
        elif cmd=='conversation-context': result=conversation_flow.context(store,args.session,now,args.subject,args.offset,args.limit)
        elif cmd=='topic-materials': result=topic_materials.materials(store,args.session,now,args.offset,args.limit)
        elif cmd=='topic-producer-claim': result=topic_producer.claim(store,args.session,now)
        elif cmd=='topic-producer-result': result=topic_producer.result(store,args.session,document(args.result),now)
        elif cmd=='topic-producer-inspect': result=topic_producer.inspect(store,args.session,now,args.limit)
        elif cmd=='topic-search-claim': result=topic_materials.claim(store,args.session,now)
        elif cmd=='topic-search-result': result=topic_materials.result(store,args.session,document(args.result),now)
        elif cmd=='recover-stale': result=dispatcher.recover(args.session)
        elif cmd=='reconcile': result=dispatcher.reconcile(args.session,document(args.evidence))
        elif cmd=='intent-inspect': result=store.intent_inspect(args.session,args.id,now)
        elif cmd=='slack-delivery-configure': result=slack_delivery.configure(store,args.session,document(args.settings),now)
        elif cmd=='slack-delivery-pending': result=slack_delivery.pending(store,args.session,now)
        elif cmd=='slack-delivery-observe': result=slack_delivery.observe(store,args.session,document(args.observation),now)
        elif cmd=='slack-delivery-reconcile': result=dispatcher.reconcile_slack_delivery(args.session,args.job_id)
        elif cmd=='slack-delivery-inspect': result=slack_delivery.inspect_attempt(store,args.session,args.job_id)
        elif cmd=='slack-delivery-report': result=slack_delivery.report(store,args.session,now)
        elif cmd=='slack-history-claim': result=slack_delivery.history_budget(store,args.session,now)
        elif cmd=='slack-history-backoff': result=slack_delivery.history_budget(store,args.session,now,args.retry_after_ms)
        elif cmd=='slack-history-sync': result=slack_delivery.history_sync(store,args.session,args.blocked=='true',now)
        elif cmd=='recall': result=store.recall(args.session,args.keywords,args.limit,offset=args.offset)
        elif cmd=='backup': result=store.backup(args.output)
        elif cmd=='maintenance': result=store.maintenance(now)
        elif cmd=='wait-dm':
            result=store.wait_dm(args.session,args.timeout,now_ms,time.sleep)
        elif cmd=='end-conversation': result=store.end_conversation(args.session,now)
        elif cmd=='release-lease': result=store.release_lease(args.session,args.owner,now)
        elif cmd=='send-due':
            transport=ExternalTransport(document(args.transport_config)) if args.transport_config else MockTransport(args.mock)
            result=dispatcher.send_due(args.session,transport,args.limit,args.dry_run,args.channel)
        else:
            snap=store.tick(args.session,now)
            result={'ok':True,'reason':'status','revision':snap['revision'],
                'summary':f"activity={snap['activity']['label'] if snap['activity'] else 'unknown'}; unhandled={len(snap['messages'])}; queue={len(snap['jobs'])}; blocked={snap['send_blocked']}; hold={snap['dispatch_hold']}",
                'next_decision_ms':snap['state']['next_decision_ms'],
                'advanced_to_ms':snap['state']['advanced_to_ms'],
                'needs_decision':snap['needs_decision']}
        result.setdefault('revision',None)
        code=0
    except ControlError as exc:
        result={'ok':False,'reason':exc.reason,'revision':None}
        if apply_telemetry:
            apply_telemetry['warning_codes'] = instrumentation.error_warning_codes(exc)
        if exc.detail: result['detail']=exc.detail
        code={'revision_conflict':2,'busy':3}.get(exc.reason,1)
    except sqlite3.IntegrityError as exc:
        result={'ok':False,'reason':'constraint_violation','detail':str(exc),'revision':None}; code=1
    except (KeyError,ValueError,TypeError,OverflowError,OSError,sqlite3.Error) as exc:
        result={'ok':False,'reason':'input_or_storage_error','detail':str(exc),'revision':None}; code=1
    if args is not None:
        # Summary only: never log message text, private activities, argv, or raw tool output.
        try:
            command_finished_ms = now_ms()
            folder=Path(args.db).expanduser().resolve().parent/'logs'
            folder.mkdir(parents=True,exist_ok=True)
            stamp=datetime.now(timezone.utc)
            summary={'at':stamp.isoformat(),'command':args.command,
                     'started_ms':command_started_ms,'finished_ms':command_finished_ms,
                     'elapsed_ms':(time.monotonic_ns()-started_monotonic_ns)//1_000_000,
                     'session_hash':hashlib.sha256(args.session.encode()).hexdigest()[:12],
                     'ok':result['ok'],'reason':result['reason'],'revision':result.get('revision')}
            for field in ('turn_id','wake_id'):
                value=getattr(args,field,None)
                if value and re.fullmatch(r'[A-Za-z0-9_:-]{1,80}',value): summary[field]=value
            if args.command=='tick' and result['ok']:
                summary.update(view=args.view,output_bytes=len(encode(result).encode('utf-8')),
                               work_route=result['work_manifest']['primary'])
            if args.command in ('apply', 'conversation-preflight'):
                summary.update(apply_telemetry)
            if args.command=='event-import' and result['ok']:
                summary.update(event_request_id=result['request_id'],event_count=len(result['events']),
                               event_replay_count=sum(e['replayed'] for e in result['events']))
            if args.command == 'recall' and result['ok']:
                summary.update(instrumentation.recall_metadata(args.keywords, result))
            if args.command == 'wait-dm' and result['ok']:
                summary['waited_ms'] = result.get('waited_ms')
                summary['new_message_count'] = len(result.get('new_message_ids', []))
            with (folder/(stamp.strftime('%Y-%m-%d')+'.log')).open('a',encoding='utf-8') as handle:
                handle.write(encode(summary)+'\n')
        except OSError:
            print('Unable to write summary log.',file=sys.stderr)
    if args is not None and args.command=='status' and args.human and result['ok']:
        print(result['summary'])
    else:
        print(encode(result))
    return code


if __name__=='__main__':
    raise SystemExit(main())
