"""Credential-bearing VM bridge. Ingest hints before ACK; never invoke a model.

The runtime hook has no credentials: it reads only the resulting local DB.
The broker is a JSON stdin/stdout command, normally restricted, noninteractive
SSH to doorbell.py. No shell interpolation or new network API is required.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

from common import ControlError, file_lock, integer, require
from doorbell import binding


def durable_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as handle:
            json.dump(value,handle,ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary,path)
        if os.name != 'nt':
            fd = os.open(path.parent,os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def state_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    require(isinstance(value,dict), 'wake_state_invalid')
    return value


def broker_command(argv, request):
    require(isinstance(argv,list) and argv and all(isinstance(x,str) and x for x in argv),
            'doorbell_broker_not_configured')
    try:
        result = subprocess.run(argv,input=json.dumps(request),capture_output=True,
                                text=True,encoding='utf-8',timeout=10,check=False)
        require(result.returncode == 0, 'doorbell_broker_failed')
        require(len(result.stdout.encode('utf-8')) <= 65536, 'doorbell_response_too_large')
        value = json.loads(result.stdout)
        require(isinstance(value,dict) and value.get('ok') is True, 'doorbell_broker_failed')
        return value
    except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
        raise ControlError('doorbell_broker_unavailable') from exc


class WakeReceiver:
    def __init__(self,cfg,*,broker=None,sync=None,clock=None):
        self.cfg = cfg
        self.settings = cfg.get('wake',{})
        self.path = Path(cfg['state_dir'])/'wake-receiver.json'
        self.clock = clock or (lambda: time.time_ns()//1_000_000)
        self.broker = broker or (lambda req: broker_command(self.settings.get('broker_argv'),req))
        if sync is None:
            from slack.receive_once import synchronize
            self.sync = lambda offer: synchronize(cfg,offer)
        else:
            self.sync = sync

    def cycle(self):
        if self.settings.get('enabled') is not True:
            return {'ok': True, 'state': 'disabled'}
        with file_lock(self.cfg['db'],'doorbell_receive'):
            return self._cycle()

    def _cycle(self):
        now = self.clock()
        target = self.settings['binding']
        binding(target)
        consumer = self.settings['consumer']
        state = state_json(self.path)
        owner = {'binding': target, 'consumer': consumer}
        require(not state or state.get('owner') == owner, 'wake_receiver_binding_changed')
        state['owner'] = owner
        result = {'ok': True, 'state': 'idle', 'broker': 'ok', 'received': False, 'acked': False}
        try:
            offer = self.broker({'operation':'snapshot',**owner})
            require(offer.get('ok') is True and offer.get('binding') == target
                    and offer.get('consumer') == consumer, 'doorbell_response_binding_mismatch')
            require(isinstance(offer.get('epoch'),str) and bool(offer['epoch']), 'doorbell_invalid_epoch')
            require(state.get('epoch',offer['epoch']) == offer['epoch'], 'doorbell_epoch_changed')
            state['epoch'] = offer['epoch']
            require(isinstance(offer.get('hints'),list) and len(offer['hints']) <= 100,
                    'doorbell_invalid_hints')
            health = offer.get('health') or {}
            at = health.get('at_ms')
            result['detector'] = ('healthy' if health.get('connected') == 1 and type(at) is int
                                  and 0<=now-at<=30000 else 'stale_or_disconnected')
            result['last_envelope_ms'] = health.get('last_envelope_ms')
            if not state.get('pending') and offer['hints']:
                integer(offer['through_seq'],minimum=1)
                require(bool(offer.get('offer_id')), 'doorbell_invalid_offer')
                require(all(type(h.get('seq')) is int and 0<h['seq']<=offer['through_seq']
                            for h in offer['hints']), 'doorbell_invalid_hints')
                state['pending'] = offer
                if not state.get('sync_failed'):
                    state['next_sync_ms'] = now
        except (ControlError,OSError,ValueError,KeyError,TypeError,sqlite3.Error) as exc:
            result.update(ok=False,broker=exc.reason if isinstance(exc,ControlError) else 'doorbell_unavailable')
        # Preserve the captured batch/epoch before any external operation.
        durable_json(self.path,state)
        pending = state.get('pending')
        handoff = state.get('handoff')
        covered = bool(pending and handoff == {'epoch':pending['epoch'],'through_seq':pending['through_seq']})
        if not covered and now >= state.get('next_sync_ms',0):
            retry_ms = self.settings.get('sync_retry_ms',15000)
            try:
                report = self.sync(pending)
                if type(report.get('retry_after_ms')) is int and report['retry_after_ms']>0:
                    retry_ms = max(retry_ms,min(report['retry_after_ms'],86400000))
                require(report.get('ok') is True and report.get('complete') is True
                        and report.get('binding') == target, 'receive_sync_incomplete')
                if pending:
                    require(report.get('epoch') == pending['epoch'] and
                            report.get('through_seq') == pending['through_seq'], 'receive_coverage_mismatch')
                    state['handoff'] = {'epoch':pending['epoch'],'through_seq':pending['through_seq']}
                    covered = True
                state['last_sync_ms'] = now
                state['sync_failed'] = False
                state['next_sync_ms'] = now+self.settings.get('sync_interval_ms',60000)
                result.update(received=True,state='ingested')
                result['fallback_recovered_count'] = report.get('new_incoming_count',0) if pending is None else 0
            except (ControlError,OSError,ValueError,KeyError,TypeError,sqlite3.Error) as exc:
                result.update(ok=False,state=exc.reason if isinstance(exc,ControlError) else 'receive_sync_failed')
                state['sync_failed'] = True
                state['next_sync_ms'] = now+retry_ms
            durable_json(self.path,state)
        if pending and covered and result['broker'] == 'ok':
            try:
                # ACK only this persisted offer, never the broker's current head.
                ack = self.broker({'operation':'ack',**owner,'epoch':pending['epoch'],
                                   'offer_id':pending['offer_id'],'through_seq':pending['through_seq']})
                require(ack.get('ok') is True and ack.get('ack_seq',-1)>=pending['through_seq'],
                        'doorbell_ack_failed')
                state['pending'] = None
                state['handoff'] = None
                # New hints arriving during sync/ACK are eligible immediately.
                state['next_sync_ms'] = now
                durable_json(self.path,state)
                result.update(acked=True,state='handed_off')
            except (ControlError,OSError,ValueError,KeyError,TypeError) as exc:
                result.update(ok=False,state=exc.reason if isinstance(exc,ControlError) else 'doorbell_ack_failed')
        result['at_ms'] = now
        durable_json(Path(self.cfg['state_dir'])/'wake-receiver-health.json',result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--once',action='store_true')
    args = parser.parse_args(argv)
    cfg = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    runner = WakeReceiver(cfg)
    while True:
        try:
            result = runner.cycle()
        except (ControlError,OSError,ValueError,KeyError,TypeError,sqlite3.Error):
            result = {'ok':False,'state':'wake_receiver_failed'}
        print(json.dumps(result),flush=True)
        if args.once:
            return 0 if result['ok'] else 1
        time.sleep(15)


if __name__ == '__main__':
    sys.exit(main())
