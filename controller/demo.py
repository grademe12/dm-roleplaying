"""Runs a simulated evening in a temporary DB, never contacts Instagram."""
import tempfile
from pathlib import Path

from common import encode, now_ms
from dispatch import Dispatcher, MockTransport
from store import Store


def main():
    with tempfile.TemporaryDirectory(prefix='controller-demo-',dir=Path(__file__).parent) as folder:
        store=Store(Path(folder)/'demo.db')
        now=now_ms()
        store.seed({},now)
        activity={'id':'evening-game','category':'gaming','label':'명조','location':'home',
                  'start_ms':now,'planned_end_ms':now+3600000,'attention':'focused',
                  'interruptibility':'at_break','details':{'reason':'example evening',
                    'rates_per_hour':{'fatigue':5}}}
        store.apply('main',{'decision_id':'start','expected_revision':0,'advance_to_ms':now,
                           'next_decision_ms':now+3600000,'activities':[activity]},now)
        store.ingest('main',{'read_ok':True,'messages':[{'id':'demo-message','text':'오늘 뭐 해요?',
                           'observation':{'identity':'platform'}}]},now+1000)
        snap=store.tick('main',now+1000)
        store.apply('main',{'decision_id':'defer','expected_revision':snap['revision'],
             'advance_to_ms':now+1000,'next_decision_ms':now+3600000,
             'processed_event_ids':['dm:demo-message'],
             'dm_decisions':[{'action':'defer','message_ids':['demo-message'],
                 'due_ms':now+3600000,'reason':'example: finish activity first'}]},now+1000)
        later=now+3600000
        snap=store.tick('main',later)
        store.apply('main',{'decision_id':'finish','expected_revision':snap['revision'],
             'advance_to_ms':later,'next_decision_ms':later+1800000,
             'activities':[{'id':'evening-game','status':'completed','actual_end_ms':later,'reason':'finished'},
                 {'id':'rest','category':'rest','label':'휴식','location':'home','start_ms':later,
                  'planned_end_ms':later+1800000,'details':{'reason':'example break'}}],
             'dm_decisions':[{'action':'reply','group_id':'demo-reply','message_ids':['demo-message'],
                  'due_ms':later,'parts':['게임 좀 하다가 지금 쉬고 있어요.'],'reason':'example response'}]},later)
        result=Dispatcher(store,lambda:later).send_due('main',MockTransport())
        print(encode({'demo':'mock only; no real DM sent','dispatch':result,
                      'final_state':store.tick('main',later)['state']}))


if __name__=='__main__': main()
