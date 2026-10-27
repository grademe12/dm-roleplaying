"""Mini-PC Socket listener: durable metadata only, no ingest, model or sending.

Run instead of the VM Socket listener after validating receive_once/receipts.
Slack distributes events among connections of the same app; parallel sockets
must not be assumed to each receive all events.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import signal
import sys
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from doorbell import Doorbell, binding, timestamp
from slack.slack_listener import SKIP_SUBTYPES, read_secret, ts_key


def record(ledger, cfg, payload, now=None):
    target = cfg['binding']
    binding(target)
    if payload.get('team_id') != target['workspace']:
        return 'ignored_workspace'
    msg = payload.get('event') or {}
    if (msg.get('type') != 'message' or msg.get('channel') != target['channel']
            or msg.get('hidden') or msg.get('subtype') in SKIP_SUBTYPES
            or (msg.get('thread_ts') and msg['thread_ts'] != msg.get('ts'))):
        return 'ignored_event'
    if msg.get('user') != cfg['peer_user_id'] or msg.get('bot_id'):
        return 'ignored_sender'
    ts = timestamp(msg.get('ts'))
    if ts_key(ts) <= ts_key(cfg['ingest_after_ts']):
        return 'before_boundary'
    # Event text/files/metadata are never copied into the doorbell ledger.
    ledger.ring(target, 'slack:'+ts, ts, now)
    return 'recorded'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    args = parser.parse_args(argv)
    cfg = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    binding(cfg['binding'])
    timestamp(cfg['ingest_after_ts'])
    ledger = Doorbell(cfg['db'])
    # Initialization is an explicit operator command; a missing ledger is fatal.
    with closing(ledger.connect()) as db:
        db.execute('SELECT epoch FROM meta').fetchone()
    from slack_sdk import WebClient
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse
    web = WebClient(token=read_secret(cfg['bot_token_path']),timeout=10)
    auth = web.auth_test()
    if not auth.get('ok') or auth.get('team_id') != cfg['binding']['workspace']:
        raise SystemExit('doorbell_identity_mismatch')
    client = SocketModeClient(app_token=read_secret(cfg['app_token_path']),web_client=web)
    stop = threading.Event()
    last_envelope = [None]

    def handle(socket, request):
        try:
            if request.type == 'events_api':
                record(ledger,cfg,request.payload or {})
            # Persist matching hints before ACK. Failed writes remain retryable.
            socket.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
            last_envelope[0] = time.time_ns()//1_000_000
        except Exception:
            print('doorbell_event_not_persisted',file=sys.stderr,flush=True)

    client.socket_mode_request_listeners.append(handle)
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,lambda *_: stop.set())
    try:
        client.connect()
        ledger.ring(cfg['binding'],'startup:'+str(time.time_ns()))
        while not stop.is_set():
            ledger.heartbeat(cfg['binding'],client.is_connected(),last_envelope[0])
            stop.wait(10)
    finally:
        client.close()
        ledger.heartbeat(cfg['binding'],False,last_envelope[0])
    return 0


if __name__ == '__main__':
    sys.exit(main())
