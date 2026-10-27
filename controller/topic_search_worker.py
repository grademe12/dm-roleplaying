#!/usr/bin/env python3
"""One low-priority scheduled collection. No DM gate claim or inline search.

The configured adapter must use Muse's verified search/body-reading tools and
emit {status, facts, materials}. No provider, credentials or tool are invented.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

from common import ControlError, encode, now_ms
from store import Store
import topic_producer as producer


def invoke(argv,request,seconds):
    proc=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          text=True,encoding='utf-8',start_new_session=os.name!='nt')
    try:
        output,_=proc.communicate(encode(request),timeout=max(.001,seconds))
        if proc.returncode!=0:return {'status':'failed'}
        value=json.loads(output)
        if not isinstance(value,dict) or set(value)-{'status','facts','materials'}:
            return {'status':'failed'}
        if value.get('status') not in ('success','failed','no_candidates','timeout'):
            return {'status':'failed'}
        return value
    except subprocess.TimeoutExpired:
        if os.name!='nt':
            try:os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        else:proc.kill()
        try:
            proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            # An escaped descendant may retain a pipe; do not wait forever.
            proc.stdout.close();proc.stderr.close()
            proc.wait(timeout=1)
        return {'status':'timeout'}
    except (ValueError,TypeError):
        return {'status':'failed'}


def run(cfg,clock=now_ms):
    store=Store(cfg['db']);session=cfg.get('session','main')
    job_id='job-'+uuid.uuid4().hex
    producer.note(store.path,session,clock(),'worker','topic_producer_worker_started',job_id=job_id)
    argv=cfg.get('topic_search',{}).get('worker_argv')
    if not isinstance(argv,list) or not argv or not all(isinstance(x,str) and x for x in argv):
        producer.note(store.path,session,clock(),'worker','topic_producer_adapter_not_configured',job_id=job_id)
        return {'ok':True,'claimed':False,'reason':'topic_producer_adapter_not_configured'}
    run=producer.claim(store,session,clock(),job_id=job_id)
    if not run['claimed']:return run
    started=time.monotonic()
    request={k:run[k] for k in ('run_id','started_ms','deadline_ms','max_materials','task')}
    try:
        remaining=max(0,run['deadline_ms']-clock())/1000
        value=invoke(argv,request,remaining) if remaining>0 else {'status':'timeout'}
        doc={**value,'run_id':run['run_id'],'token':run['token']}
        outcome=producer.result(store,session,doc,clock())
    except (ControlError,OSError,ValueError,TypeError,KeyError,sqlite3.Error) as exc:
        producer.note(store.path,session,clock(),'worker','topic_producer_adapter_failed',run['run_id'],
                      code=exc.reason if isinstance(exc,ControlError) else type(exc).__name__)
        outcome=producer.result(store,session,{'run_id':run['run_id'],'token':run['token'],'status':'failed'},clock())
    # Never return the claim token or captured tool output to scheduler logs.
    elapsed=int((time.monotonic()-started)*1000)
    producer.note(store.path,session,clock(),'worker','topic_producer_worker_finished',run['run_id'],job_id=job_id,elapsed_ms=elapsed,status=outcome['status'])
    return {**outcome,'job_id':job_id,'elapsed_ms':elapsed}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    args=parser.parse_args(argv)
    try:
        cfg=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
        output=run(cfg)
    except (OSError,ValueError,KeyError,TypeError,ControlError,sqlite3.Error):
        output={'ok':False,'reason':'topic_producer_worker_unavailable'}
    print(encode(output))
    return 0 if output['ok'] else 1


if __name__=='__main__':sys.exit(main())
