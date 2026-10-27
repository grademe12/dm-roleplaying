#!/usr/bin/env python3
"""Background memory organizer: native Muse file handoff or explicitly configured HTTP.

No gate claim, controller writes, DM sends or model calls during reply preparation.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import re

import model_adapter

from common import ControlError, encode, now_ms, require
import episodic_memory as memory

ROOT = Path(__file__).resolve().parent


def load_config(path):
    path=Path(path).expanduser().resolve()
    cfg=json.loads(path.read_text(encoding='utf-8-sig'))
    for key in ('db','state_dir'):
        if cfg.get(key):
            p=Path(cfg[key]).expanduser()
            cfg[key]=str(p if p.is_absolute() else path.parent/p)
    opts=cfg.get('episodic_memory')
    if not isinstance(opts,dict) or opts.get('mode','off')=='off':return cfg
    for key in ('path','key_file'):
        if opts.get(key):
            p=Path(opts[key]).expanduser()
            opts[key]=str(p if p.is_absolute() else path.parent/p)
    return model_adapter.resolve_paths(cfg,path)


def private_write(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,temporary=tempfile.mkstemp(prefix='.memory-response-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            stream.write(encode(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def remove_response(path):
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def failed_response(cfg,path,run_id,clock,reason):
    if reason == 'busy':
        return {'ok':True,'reason':'busy','run_id':run_id,
                **({'response_file':str(path)} if path.is_file() else {})}
    # Record before deleting: contention while recording must preserve the file.
    memory.fail(cfg,run_id,clock(),reason)
    result={'ok':False,'reason':reason,'run_id':run_id}
    if not remove_response(path):result['cleanup_pending']=str(path)
    return result


def finish_response(cfg,path,run_id,clock):
    try:
        output=json.loads(path.read_text(encoding='utf-8'))
    except OSError:
        return failed_response(cfg,path,run_id,clock,'memory_response_unavailable')
    except ValueError:
        # The saved file was atomically written from parsed JSON. Later corruption
        # is a storage problem, not evidence that the original source is invalid.
        return failed_response(cfg,path,run_id,clock,'memory_saved_response_corrupt')
    try:
        memory.validate_response_identity(output,run_id)
        result=memory.complete(cfg,output,clock())
    except (ControlError,ValueError,TypeError,KeyError) as exc:
        reason=exc.reason if isinstance(exc,ControlError) else 'invalid_memory_response'
        return failed_response(cfg,path,run_id,clock,reason)
    if not remove_response(path):result={**result,'cleanup_pending':str(path)}
    return result


def model_files(opts,run_id):
    require(isinstance(run_id,str) and re.fullmatch(r'[0-9a-f]{32}',run_id), 'invalid_memory_run_id')
    folder=opts['path'].parent
    return {'input':folder/('memory-model-input-'+run_id+'.json'),
            'response':folder/('memory-model-response-'+run_id+'.json'),
            'canonical':folder/('memory-http-response-'+run_id+'.json')}


def model_reason(exc):
    reason=exc.reason if isinstance(exc,ControlError) else (
        'memory_transport_failed' if isinstance(exc,OSError) else 'invalid_memory_response')
    return {'model_output_invalid':'invalid_memory_response', 'model_input_mismatch':'memory_input_mismatch',
            'model_incomplete':'memory_model_incomplete', 'model_not_configured':'memory_model_not_configured'}.get(reason,reason)


def note(opts,run_id,phase,**metadata):
    # No source text, instructions, paths, raw backend output or credentials.
    try:
        path=opts['path'].parent/'memory-executor.jsonl'
        with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600),'a',encoding='utf-8') as stream:
            os.fchmod(stream.fileno(),0o600)
            stream.write(encode({'at_ms':now_ms(),'task':'episodic_memory','run_id':run_id,'phase':phase,**metadata})+'\n')
    except (OSError,ControlError,TypeError,ValueError):
        pass


def cleanup_model_files(files,result):
    if result.get('reason')=='busy':return result
    for path in files.values():
        if not remove_response(path) and 'cleanup_pending' not in result:
            result={**result,'cleanup_pending':str(path)}
    return result


def complete_model_file(cfg,run_id,path,clock=now_ms):
    opts=memory.settings(cfg)
    require(opts['mode']!='off','episodic_memory_off')
    files=model_files(opts,run_id)
    # Bind against the original DB lease, not a mutable config or model-supplied ID.
    binding=memory.response_binding(cfg,run_id)
    if files['canonical'].is_file():
        result=finish_response(cfg,files['canonical'],run_id,clock)
        return cleanup_model_files(files,result)
    if binding['status']=='completed' and not Path(path).is_file():
        return cleanup_model_files(files,{**binding['result'],'replayed':True})
    backend=None
    if files['input'].is_file():
        try:
            backend=json.loads(files['input'].read_text(encoding='utf-8')).get('backend')
        except (OSError,ValueError,AttributeError):
            pass
    try:
        value=json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError) as exc:
        reason=('memory_response_unavailable' if isinstance(exc,OSError) else
                'invalid_memory_response' if backend=='file' else 'memory_saved_response_corrupt')
        result=failed_response(cfg,files['canonical'],run_id,clock,reason)
        return cleanup_model_files(files,result)
    try:
        normalized=(model_adapter.result(binding,value) if isinstance(value,dict) and value.get('schema')==memory.SCHEMA
                    else model_adapter.validate_result(value,binding))
        memory.validate_response_identity(normalized['output'],run_id)
        private_write(files['canonical'],normalized['output'])
        result=finish_response(cfg,files['canonical'],run_id,clock)
    except OSError:
        # The normalized paid result is already on disk; retry without a new model call.
        return {'ok':False,'reason':'memory_response_unavailable','next':'resume',
                'run_id':run_id,'response_file':str(path)}
    except (ValueError,KeyError,TypeError,ControlError) as exc:
        result=failed_response(cfg,files['canonical'],run_id,clock,model_reason(exc))
    note(opts,run_id,'completed' if result.get('ok') and result.get('reason')!='busy' else 'completion',
         reason=result.get('reason'),ok=result.get('ok'))
    return cleanup_model_files(files,result)


def run(cfg,clock=now_ms,transport=None):
    opts=memory.settings(cfg)
    if opts['mode']=='off':return {'ok':True,'reason':'off'}
    # Preflight is deliberately before adapter validation, key reads and model work.
    prepared=memory.prepare(cfg,clock())
    if prepared['reason'] not in ('prepared','busy'):return prepared
    run_id=prepared['run_id'];files=model_files(opts,run_id)
    if prepared['reason']=='busy':
        if files['canonical'].is_file() or files['response'].is_file():
            return complete_model_file(cfg,run_id,files['response'],clock)
        return {**prepared, 'next':'await_response', **(
            {'input_file':str(files['input']),'response_file':str(files['response'])}
            if files['input'].is_file() else {})}
    packet=prepared['packet'];started=time.monotonic()
    adapter={};name=opts.get('model_adapter')
    try:
        adapter=model_adapter.options(cfg,name,legacy=opts)
        instructions=(ROOT/'runbooks/memory-organizer.md').read_text(encoding='utf-8')
        req=model_adapter.request('episodic_memory',run_id,packet['input_sha256'],instructions,packet,
                                  adapter,prepared['deadline_ms'])
        req['backend']=adapter['backend']
        private_write(files['input'],req)
        if adapter['backend']=='file':
            note(opts,run_id,'handoff',backend='file',adapter=name,model=adapter.get('model'))
            return {'ok':True,'reason':'prepared','next':'model','response_format':'task-json','run_id':run_id,
                    'input_sha256':packet['input_sha256'],'deadline_ms':prepared['deadline_ms'],
                    'input_file':str(files['input']),'response_file':str(files['response']),
                    'response_envelope':req['response_contract']}
        timeout=min(adapter['timeout_seconds'],(prepared['deadline_ms']-clock())/1000-5)
        call=transport
        value=model_adapter.invoke(adapter,req,timeout,call)
        private_write(files['response'],value)
        note(opts,run_id,'model_response',backend=adapter['backend'],adapter=name,model=adapter.get('model'),
             elapsed_ms=int((time.monotonic()-started)*1000),usage=value['usage'],
             instructions_sha256=req['instructions_sha256'])
        return complete_model_file(cfg,run_id,files['response'],clock)
    except (OSError,ValueError,KeyError,TypeError,IndexError,ControlError) as exc:
        reason=model_reason(exc)
        note(opts,run_id,'model_failed',backend=adapter.get('backend'),adapter=name,
             reason=reason,elapsed_ms=int((time.monotonic()-started)*1000))
        result=failed_response(cfg,files['canonical'],run_id,clock,reason)
        return cleanup_model_files(files,result)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    sub=parser.add_subparsers(dest='action',required=True)
    prepare=sub.add_parser('prepare');prepare.add_argument('--output',required=True)
    complete=sub.add_parser('complete');complete.add_argument('--response',required=True)
    complete.add_argument('--run-id')
    failure=sub.add_parser('fail');failure.add_argument('--run-id',required=True)
    retry=sub.add_parser('retry-quarantined')
    selection=retry.add_mutually_exclusive_group(required=True)
    selection.add_argument('--seq',type=int)
    selection.add_argument('--all',dest='all_sources',action='store_true')
    sub.add_parser('run')
    recall=sub.add_parser('recall');recall.add_argument('--channel',required=True,choices=('slack','instagram'))
    recall.add_argument('--query',required=True);recall.add_argument('--category',action='append',choices=memory.CATEGORIES)
    recall.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    try:
        cfg=load_config(args.config)
        if args.action=='prepare':
            result=memory.prepare(cfg,now_ms())
            if 'packet' in result:
                output=args.output.replace('{run_id}',result['run_id'])
                private_write(output,result.pop('packet'))
                result['input_file']=str(Path(output).resolve())
        elif args.action=='complete':
            path=args.response[1:] if args.response.startswith('@') else args.response
            try:
                response=json.loads(Path(path).read_text(encoding='utf-8'))
                memory.validate_response_identity(response,args.run_id)
                result=memory.complete(cfg,response,now_ms())
            except (OSError,ValueError,KeyError,TypeError,ControlError) as exc:
                reason = (exc.reason if isinstance(exc,ControlError) else
                          'memory_response_unavailable' if isinstance(exc,OSError) else 'invalid_memory_response')
                if args.run_id and reason != 'busy':
                    memory.fail(cfg,args.run_id,now_ms(),reason)
                raise
        elif args.action=='fail':
            result={'ok':True,'recorded':memory.fail(cfg,args.run_id,now_ms())}
        elif args.action=='retry-quarantined':
            result=memory.retry_quarantined(cfg,args.seq,now_ms(),all_sources=args.all_sources)
        elif args.action=='recall':
            value=memory.recall(cfg,args.channel,args.query,now_ms(),args.category)
            private_write(args.output,value)
            result={'ok':True,'mode':value['mode'],'items':len(value['items']),'output_file':str(Path(args.output).resolve())}
        else:result=run(cfg)
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error,ControlError) as exc:
        result={'ok':False,'reason':exc.reason if isinstance(exc,ControlError) else 'memory_organizer_io_error'}
    print(encode(result))
    return 0 if result.get('ok') else 1


if __name__=='__main__':raise SystemExit(main())
