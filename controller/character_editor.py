#!/usr/bin/env python3
"""Independent character editor. It never acquires a DM decision claim."""
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from common import ControlError, encode, now_ms, require
import character_documents as documents
import model_adapter


def load_config(path):
    path=Path(path).expanduser().resolve();cfg=json.loads(path.read_text(encoding='utf-8-sig'))
    for key in ('db','state_dir'):
        if cfg.get(key):
            p=Path(cfg[key]).expanduser();cfg[key]=str(p if p.is_absolute() else path.parent/p)
    for section in ('character_documents','episodic_memory'):
        opts=cfg.get(section)
        if not isinstance(opts,dict):continue
        for key in ('path','key_file'):
            if isinstance(opts.get(key),str) and opts[key]:
                p=Path(opts[key]).expanduser();opts[key]=str(p if p.is_absolute() else path.parent/p)
    return model_adapter.resolve_paths(cfg,path)


def files(cfg,run_id):
    require(isinstance(run_id,str) and re.fullmatch('[0-9a-f]{32}',run_id),'invalid_character_run')
    folder=documents.settings(cfg)['path'].parent/'character-editor'
    # Same file-name rule as the memory organizer's exchange files, with the editor's own prefix.
    return {'input':folder/('character-model-input-'+run_id+'.json'),'response':folder/('character-model-response-'+run_id+'.json')}


def cleanup(paths,result):
    if result.get('reason')=='busy':return result
    for p in paths.values():
        try:p.unlink(missing_ok=True)
        except OSError:result={**result,'cleanup_pending':str(p)}
    return result


def note(cfg,run_id,phase,**fields):
    try:
        path=documents.settings(cfg)['path'].parent/'character-editor.jsonl'
        with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600),'a',encoding='utf-8') as stream:
            os.fchmod(stream.fileno(),0o600)
            stream.write(encode({'at_ms':now_ms(),'task':'character_documents','run_id':run_id,'phase':phase,**fields})+'\n')
    except (OSError,ControlError,TypeError,ValueError):pass


def finish(cfg,run_id,path,clock=now_ms):
    paths=files(cfg,run_id);binding=documents.response_binding(cfg,run_id)
    if binding['status']=='completed' and not Path(path).is_file():
        result={**binding['result'],'replayed':True}
    else:
        try:
            value=json.loads(Path(path).read_text(encoding='utf-8'))
            if isinstance(value,dict) and value.get('schema')==documents.SCHEMA:output=value
            else:output=model_adapter.validate_result(value,binding)['output']
            require(isinstance(output,dict) and output.get('run_id')==run_id,'character_input_mismatch')
            result=documents.complete(cfg,output,clock())
        except (OSError,ValueError,TypeError,KeyError,ControlError) as exc:
            reason=exc.reason if isinstance(exc,ControlError) else 'character_response_unavailable' if isinstance(exc,OSError) else 'invalid_character_response'
            if reason=='busy':return {'ok':True,'reason':'busy','run_id':run_id,'response_file':str(path)}
            # A database lock/I/O error propagates without deleting the paid result.
            documents.fail(cfg,run_id,clock(),reason)
            note(cfg,run_id,'failed',reason=reason)
            return cleanup(paths,{'ok':False,'reason':reason,'run_id':run_id})
    try:
        result={**result,**documents.export(cfg,clock(),result['revision'])}
    except (OSError,sqlite3.Error,ControlError):
        result={**result,'export_pending':True,'next':'export'}
    note(cfg,run_id,'completed',reason=result.get('reason'),revision=result['revision'],changed_items=result['changed_items'])
    return cleanup(paths,result)


def run(cfg,clock=now_ms,transport=None):
    opts=documents.settings(cfg)
    if opts['mode']=='off':return {'ok':True,'reason':'off'}
    prepared=documents.prepare(cfg,clock())
    if prepared['reason'] not in ('prepared','busy'):return prepared
    run_id=prepared['run_id'];paths=files(cfg,run_id)
    if prepared['reason']=='busy':
        if paths['response'].is_file():return finish(cfg,run_id,paths['response'],clock)
        return {**prepared,'next':'await_response',**({'input_file':str(paths['input']),'response_file':str(paths['response'])}
                if paths['input'].is_file() else {})}
    started=time.monotonic();adapter={}
    try:
        adapter=model_adapter.options(cfg,opts.get('model_adapter'),legacy=opts)
        instructions=(documents.ROOT/'runbooks/character-editor.md').read_text(encoding='utf-8')
        packet=prepared['packet']
        req=model_adapter.request('character_documents',run_id,packet['input_sha256'],instructions,packet,adapter,prepared['deadline_ms'])
        documents.private_write(paths['input'],req)
        if adapter['backend']=='file':
            note(cfg,run_id,'handoff',backend='file',model=adapter.get('model'))
            return {'ok':True,'reason':'prepared','next':'model','run_id':run_id,'deadline_ms':prepared['deadline_ms'],
                    'input_file':str(paths['input']),'response_file':str(paths['response']),'response_format':'task-json'}
        timeout=min(adapter['timeout_seconds'],(prepared['deadline_ms']-clock())/1000-5)
        value=model_adapter.invoke(adapter,req,timeout,transport)
        documents.private_write(paths['response'],value)
        note(cfg,run_id,'model_response',backend=adapter['backend'],model=adapter.get('model'),usage=value['usage'],
             instructions_sha256=req['instructions_sha256'],elapsed_ms=int((time.monotonic()-started)*1000))
    except (OSError,ValueError,TypeError,KeyError,ControlError) as exc:
        reason=exc.reason if isinstance(exc,ControlError) else 'character_transport_failed'
        documents.fail(cfg,run_id,clock(),reason)
        note(cfg,run_id,'failed',reason=reason)
        return cleanup(paths,{'ok':False,'reason':reason,'run_id':run_id})
    return finish(cfg,run_id,paths['response'],clock)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',required=True)
    commands=parser.add_subparsers(dest='action',required=True)
    commands.add_parser('run');commands.add_parser('partition');commands.add_parser('retry')
    complete=commands.add_parser('complete');complete.add_argument('--run-id',required=True);complete.add_argument('--response',required=True)
    show=commands.add_parser('show');show.add_argument('--output',required=True);show.add_argument('--revision')
    export=commands.add_parser('export');export.add_argument('--revision')
    failure=commands.add_parser('fail');failure.add_argument('--run-id',required=True);failure.add_argument('--reason',default='character_worker_failed')
    args=parser.parse_args(argv)
    try:
        cfg=load_config(args.config)
        if args.action=='partition':
            path=Path(cfg.get('state_dir','.'))/'character-editor'/'partition.json'
            documents.private_write(path,documents.seed());result={'ok':True,'output_file':str(path)}
        elif args.action=='run':result=run(cfg,clock=now_ms)
        elif args.action=='retry':result=documents.retry(cfg)
        elif args.action=='complete':result=finish(cfg,args.run_id,args.response.removeprefix('@'),clock=now_ms)
        elif args.action=='show':
            value=documents.view(cfg,now_ms(),args.revision);documents.private_write(args.output,value)
            result={'ok':True,'output_file':str(Path(args.output).resolve()),'mode':value['mode']}
        elif args.action=='export':result={'ok':True,**documents.export(cfg,now_ms(),args.revision)}
        else:result={'ok':True,'recorded':documents.fail(cfg,args.run_id,now_ms(),args.reason)}
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,ControlError) as exc:
        result={'ok':False,'reason':exc.reason if isinstance(exc,ControlError) else 'character_editor_io_error'}
    print(encode(result));return 0 if result.get('ok') else 1


if __name__=='__main__':raise SystemExit(main())
