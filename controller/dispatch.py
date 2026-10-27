"""Serialized dispatch, strict normalized transport contract, conservative recovery."""
import subprocess
import time
import uuid
from datetime import datetime

from common import ControlError, decode, encode, file_lock, integer, now_ms, require, text
from message_limits import MAX_PARTS
import persona
from pathlib import Path
from slack_delivery import mark_unresolved, prepare_attempt, reserve_claim, verified_result
from store import DEFAULT_CHANNEL, KST, bump, conversation_mode, rowdict, session_row, valid_channel
import conversation_flow
import continuations
import delivery_flow
import morning_contact
import memory_continuity
import in_person
JOB_CHANNEL = "COALESCE((SELECT channel FROM group_channels gc WHERE gc.session_id=j.session_id AND gc.group_id=j.group_id),'instagram')"


def normalize_result(value):
    require(isinstance(value,dict),'invalid_transport_result')
    status=value.get('status')
    require(status in ('confirmed','submitted','failed','unknown'),'invalid_result_status')
    evidence=value.get('evidence')
    if status=='confirmed':
        text(value.get('provider_message_id'),'provider_message_id')
        text(evidence,'confirmation_evidence')
    if status=='failed':
        require(value.get('not_sent') is True,'failure_not_proven')
        text(evidence,'failure_evidence')
    if value.get('retry_after_ms') is not None:
        integer(value['retry_after_ms'],'retry_after_ms',1)
    return value


class MockTransport:
    mode='mock'
    timeout=30
    def __init__(self, scenario='confirmed'):
        require(scenario in ('confirmed','submitted','failed','unknown','429'),'invalid_mock_scenario')
        self.scenario=scenario

    def send(self, job):
        if self.scenario=='429':
            return {'status':'failed','not_sent':True,'code':429,'evidence':'mock rejection'}
        if self.scenario=='confirmed':
            return {'status':'confirmed','provider_message_id':'mock:'+job['id'],
                    'evidence':'mock only; no external message sent'}
        if self.scenario=='failed':
            return {'status':'failed','not_sent':True,'evidence':'mock rejection'}
        return {'status':self.scenario,'evidence':'mock only'}


class ExternalTransport:
    mode='external'
    def __init__(self, config):
        require(config.get('contract') in ('dm-send-v1','dm-send-v2'),'unknown_transport_contract')
        self.contract=config['contract']
        require(config.get('enabled') is True,'external_transport_disabled')
        self.argv=config.get('argv')
        require(isinstance(self.argv,list) and len(self.argv)>0 and all(isinstance(s,str) and s for s in self.argv),'invalid_transport_argv')
        self.timeout=config.get('timeout_seconds',30)
        require(type(self.timeout) in (int,float) and 0<self.timeout<=30,'invalid_transport_timeout')

    def send(self, job):
        request={'contract':self.contract,'sender':'persona','recipient':'user',
                 'text':job['text'],'job_id':job['id'],'idempotency_key':job['dedupe_key']}
        if self.contract=='dm-send-v2':
            request['channel']=job.get('channel',DEFAULT_CHANNEL)
            if job.get('delivery_receipt') is not None:
                request['delivery_receipt']=job['delivery_receipt']
        try:
            process=subprocess.run(self.argv,input=encode(request),encoding='utf-8',
                capture_output=True,timeout=self.timeout,shell=False,close_fds=True)
        except subprocess.TimeoutExpired:
            return {'status':'unknown','evidence':'transport_timeout'}
        except OSError:
            return {'status':'failed','not_sent':True,'evidence':'transport_process_not_started'}
        try:
            result=normalize_result(decode(process.stdout))
            if process.returncode != 0 and result['status'] not in ('failed','unknown'):
                return {'status':'unknown','evidence':'exit_result_conflict'}
            return result
        except (ControlError,ValueError,TypeError):
            return {'status':'unknown','evidence':'invalid_transport_output','exit_code':process.returncode}


BURST_GAP_MIN_MS, BURST_GAP_MAX_MS = 3000, 5000
LIVE_GROUP_GAP_MS = 5000
GLOBAL_SEND_GAP_MS = 3000
GROUP_GAPS_MS = {
    'instagram': {'live': LIVE_GROUP_GAP_MS, 'normal': 240000, 'quiet': 600000},
    'slack': {'live': LIVE_GROUP_GAP_MS, 'normal': 20000, 'quiet': 45000},
}
def burst_gap_ms(next_text):
    """Short pause before the next bubble of the same reply group, roughly typing time."""
    return max(BURST_GAP_MIN_MS, min(BURST_GAP_MAX_MS, 2000+150*len(next_text)))
class Dispatcher:
    def __init__(self, store, clock=now_ms, sleep=time.sleep):
        self.store,self.clock,self.sleep=store,clock,sleep

    def _profile(self, db, session, now, channel=None):
        return conversation_mode(db,session,now,channel)

    def _claim(self, session, mode, now, only_group=None, channel=DEFAULT_CHANNEL):
        with self.store.connect(True, operation='dispatch_claim', session=session) as db:
            state=session_row(db,session)
            require(db.execute('SELECT transport_mode FROM runtime_settings').fetchone()[0]==mode,'transport_mode_mismatch')
            expired = morning_contact.expire_pending(db,session,now)
            for gid in expired: conversation_flow.sync_group(db,session,gid,now)
            if expired: bump(db,session)
            advanced=delivery_flow.advance_session(db,session)
            if continuations.maintain(db,session,now) or advanced:
                bump(db,session)
            # A paginated history scan is background recovery, not a send lease.
            # Actual unresolved attempts and newly ingested messages are guarded below.
            activity=db.execute("SELECT attention FROM activities WHERE session_id=? AND status='in_progress'",(session,)).fetchone()
            if activity and activity['attention']=='unavailable': return None,'activity_unavailable'
            # 429 blocks only the channel that was rate limited (legacy session column = instagram).
            if channel==DEFAULT_CHANNEL and (state['send_blocked_until_ms'] or 0)>now: return None,'rate_limited'
            block=db.execute('SELECT blocked_until_ms FROM channel_blocks WHERE session_id=? AND channel=?',(session,channel)).fetchone()
            if block and block[0]>now: return None,'rate_limited'
            # Only a brief serialization gap is shared across messengers.
            if (state['next_send_after_ms'] or 0)>now: return None,'send_interval'
            pacing=db.execute('SELECT next_send_after_ms FROM channel_pacing WHERE session_id=? AND channel=?',
                              (session,channel)).fetchone()
            # Serialize live requests only. Receipt uncertainty does not stop conversation.
            if db.execute("SELECT 1 FROM dm_jobs WHERE session_id=? AND status='attempting'",(session,)).fetchone():
                return None,'dispatch_unresolved'
            last=db.execute("SELECT j.*,f.passed_ms AS flow_ms FROM dm_jobs j JOIN dm_flow_passes f "
                "ON f.session_id=j.session_id AND f.job_id=j.id WHERE j.session_id=? AND "+JOB_CHANNEL+
                "=? ORDER BY f.passed_ms DESC,j.rowid DESC LIMIT 1",(session,channel)).fetchone()
            last_run=continuations.owner(db,session,last['group_id']) if last else None
            jobs=db.execute("SELECT j.* FROM dm_jobs j WHERE j.session_id=? AND j.status='pending' AND j.due_ms<=? AND (? IS NULL OR j.group_id=?) AND "
                            +JOB_CHANNEL+"=? ORDER BY j.due_ms,j.group_id,j.part_index",
                            (session,now,only_group,only_group,channel)).fetchall()
            hold='send_interval' if pacing and pacing[0]>now else 'nothing_due'
            for job in jobs:
                group=db.execute('SELECT * FROM reply_groups WHERE session_id=? AND id=?',(session,job['group_id'])).fetchone()
                if group['status']!='active': continue
                if not in_person.group_current(db,session,job['group_id'],channel):
                    hold='draft_stale_scene'; continue
                continuation_hold=continuations.claim_hold(db,session,job['group_id'])
                if continuation_hold:
                    hold=continuation_hold; continue
                run=continuations.owner(db,session,job['group_id'])
                pace_due=pacing[0] if pacing else 0
                if last is not None and last['group_id']==job['group_id'] and last['part_index']<job['part_index']:
                    pace_due=last['flow_ms']+burst_gap_ms(job['text'])
                if (run is not None and last_run is not None and run['id']==last_run['id']
                        and run['group_generation']==last_run['group_generation']
                        and last['group_id']!=job['group_id']):
                    # The run's cadence applies only to its next group. Ordinary
                    # replies retain the channel's original pacing after a stop.
                    gap=run['interval_ms']
                    if self._profile(db,session,now,channel)=='quiet':
                        gap=max(gap,GROUP_GAPS_MS.get(channel,GROUP_GAPS_MS[DEFAULT_CHANNEL])['quiet'])
                    pace_due=last['flow_ms']+gap
                if pace_due>now:
                    hold='send_interval'; continue
                if group['observation_revision']!=state['dm_observation_revision']:
                    hold='draft_stale'; continue
                reviewed=db.execute('SELECT reviewed_ms FROM group_reviews WHERE session_id=? AND group_id=?',
                                    (session,job['group_id'])).fetchone()
                reviewed_ms=reviewed[0] if reviewed else group['created_ms']
                if db.execute('''SELECT 1 FROM activities WHERE session_id=? AND attention='unavailable'
                    AND status IN ('in_progress','completed','interrupted') AND start_ms>=? AND start_ms<=? LIMIT 1''',
                    (session,reviewed_ms,now)).fetchone():
                    hold='draft_stale_activity'; continue
                # Proactive (initiate) groups never had targets; observation_revision above
                # already holds them if a new incoming message arrived after the draft.
                proactive=db.execute('SELECT 1 FROM reply_targets WHERE session_id=? AND group_id=? LIMIT 1',
                                     (session,job['group_id'])).fetchone() is None
                if not proactive:
                    targets=db.execute('''SELECT m.* FROM dm_messages m JOIN reply_targets t
                        ON t.session_id=m.session_id AND t.message_id=m.id
                        WHERE t.session_id=? AND t.group_id=? AND t.active=1''',(session,job['group_id'])).fetchall()
                    if not targets or any(decode(m['observation_json']).get('review_required') for m in targets):
                        hold='target_requires_review'; continue
                    if any((m['reply_due_ms'] or 0)>now for m in targets):
                        hold='reply_not_due'; continue
                if db.execute('''SELECT 1 FROM dm_jobs j WHERE session_id=? AND group_id=? AND part_index<?
                    AND NOT EXISTS (SELECT 1 FROM dm_flow_passes f WHERE f.session_id=j.session_id AND f.job_id=j.id)''',
                    (session,job['group_id'],job['part_index'])).fetchone():
                    hold='previous_part_unconfirmed'; continue
                token=uuid.uuid4().hex
                db.execute("UPDATE dm_jobs SET status='attempting',attempt_token=?,attempt_started_ms=?,lease_expires_ms=?,updated_ms=? WHERE session_id=? AND id=? AND status='pending'",(token,now,now+120000,now,session,job['id']))
                delivery_receipt=prepare_attempt(db,session,job,token,now,channel)
                profile=self._profile(db,session,now,channel)
                db.execute('UPDATE sessions SET timing_profile=?,revision=revision+1 WHERE id=?',(profile,session))
                claimed=rowdict(db.execute('SELECT * FROM dm_jobs WHERE session_id=? AND id=?',(session,job['id'])).fetchone())
                claimed['channel']=channel
                if delivery_receipt is not None:
                    claimed['delivery_receipt']=delivery_receipt
                return claimed,'claimed'
            return None,hold

    def _complete_group(self, db, session, gid):
        conversation_flow.sync_group(db,session,gid,self.clock())
        delivery_flow.complete_group(db,session,gid)

    def _utterance_sent(self, db, session, job, result, now):
        """Keep marked lasting self claims after confirmed delivery, not every small-talk line."""
        memory_continuity.sync(db, session, now, job_id=job['id'])
        channel = db.execute('SELECT channel FROM group_channels WHERE session_id=? AND group_id=?',
                             (session, job['group_id'])).fetchone()
        for claim in db.execute('SELECT claim_index,fact_id,excerpt FROM job_self_claims '
                                'WHERE session_id=? AND job_id=? ORDER BY claim_index', (session, job['id'])):
            self.store._fact(db, session, {
                'id': 'self-said:' + job['id'] + ':' + str(claim['claim_index']),
                'source': 'observed', 'occurred_ms': now,
                'summary': 'self_said:' + claim['fact_id'] + ' ' + claim['excerpt'],
                'details': {'evidence': 'confirmed send ' + str(result['provider_message_id']),
                            'job_id': job['id'], 'group_id': job['group_id'],
                            'channel': channel['channel'] if channel else DEFAULT_CHANNEL,
                            'fact_id': claim['fact_id'], 'excerpt': claim['excerpt'],
                            'text': job['text']}}, now)

    def _record(self, session, job, result, now):
        result=normalize_result(result)
        with self.store.connect(True, operation='dispatch_result', session=session) as db:
            state=session_row(db,session)
            current=db.execute('SELECT * FROM dm_jobs WHERE session_id=? AND id=?',(session,job['id'])).fetchone()
            require(current is not None and current['status']=='attempting' and current['attempt_token']==job['attempt_token'],'attempt_token_conflict')
            db.execute('UPDATE dm_jobs SET status=?,updated_ms=?,result_json=?,provider_message_id=? WHERE session_id=? AND id=?',
                (result['status'],now,encode(result),result.get('provider_message_id'),session,job['id']))
            if result['status'] in ('submitted','unknown'):
                mark_unresolved(db,session,job,now)
            delivery_flow.record(db,session,job['id'],result['status'],now)
            following=db.execute("SELECT text FROM dm_jobs WHERE session_id=? AND group_id=? AND status='pending' ORDER BY part_index LIMIT 1",
                                 (session,job['group_id'])).fetchone()
            if result['status'] in delivery_flow.PASSED and result.get('code')!=429 and following is not None:
                gap=burst_gap_ms(following['text'])   # same group continues after a short pause
            else:
                channel=job.get('channel',DEFAULT_CHANNEL)
                gap=GROUP_GAPS_MS.get(channel,GROUP_GAPS_MS[DEFAULT_CHANNEL])[state['timing_profile']]
            # A message that certainly was not sent is not a human message, so it adds no pacing
            # (keeps one blocked messenger from slowing the other).
            if result['status']=='failed' and result.get('not_sent'):
                db.execute('UPDATE sessions SET revision=revision+1 WHERE id=?',(session,))
            else:
                db.execute('''INSERT INTO channel_pacing(session_id,channel,next_send_after_ms) VALUES (?,?,?)
                    ON CONFLICT(session_id,channel) DO UPDATE SET next_send_after_ms=excluded.next_send_after_ms''',
                    (session,job.get('channel',DEFAULT_CHANNEL),now+gap))
                db.execute('UPDATE sessions SET next_send_after_ms=?,revision=revision+1 WHERE id=?',
                    (now+GLOBAL_SEND_GAP_MS,session))
            if result.get('code')==429:
                until=now+result.get('retry_after_ms',3600000)
                channel=job.get('channel',DEFAULT_CHANNEL)
                if channel==DEFAULT_CHANNEL:
                    db.execute('UPDATE sessions SET send_blocked_until_ms=? WHERE id=?',(max(state['send_blocked_until_ms'] or 0,until),session))
                else:
                    db.execute('''INSERT INTO channel_blocks VALUES (?,?,?) ON CONFLICT(session_id,channel)
                        DO UPDATE SET blocked_until_ms=max(blocked_until_ms,excluded.blocked_until_ms)''',(session,channel,until))
            if result['status']=='confirmed':
                if ('channel' in job.keys() and job['channel'] == 'slack'):
                    reserve_claim(db,session,job,result['provider_message_id'])
                self._utterance_sent(db,session,job,result,now)
            else:
                memory_continuity.sync(db, session, now, job_id=job['id'])
            self._complete_group(db,session,job['group_id'])
            conversation_flow.sync_group(db,session,job['group_id'],now)
            continuations.after_result(db,session,job['group_id'],now)
            return session_row(db,session)['revision']

    def send_due(self, session, transport, limit=MAX_PARTS, dry_run=False, channel=DEFAULT_CHANNEL, *, deadline_ms=None):
        valid_channel(channel)
        integer(limit,'limit',1); require(limit<=MAX_PARTS,'limit_too_large')
        if deadline_ms is not None: integer(deadline_ms, 'deadline_ms')
        if dry_run:
            snapshot=self.store.tick(session,self.clock())
            return {'ok':True,'reason':'dry_run','revision':snapshot['revision'],
                    'pending_ids':[j['id'] for j in snapshot['jobs'] if j['status']=='pending' and j['channel']==channel],
                    'note':'No claims, transport calls or state changes.'}
        with file_lock(self.store.path,'send:'+session):
            results=[]; reason='limit_reached'; burst_group=None
            for _ in range(limit):
                if deadline_ms is not None and self.clock() + int(getattr(transport, 'timeout', 30) * 1000) >= deadline_ms:
                    reason='worker_budget'; break
                if burst_group is not None:
                    # Continue only the group just sent; other groups wait for the normal gap.
                    with self.store.connect() as db:
                        global_due=session_row(db,session)['next_send_after_ms'] or 0
                        channel_row=db.execute('SELECT next_send_after_ms FROM channel_pacing WHERE session_id=? AND channel=?',
                                               (session,channel)).fetchone()
                        wait=max(global_due,channel_row[0] if channel_row else 0)-self.clock()
                    if wait>BURST_GAP_MAX_MS:
                        reason='send_interval'; break
                    if deadline_ms is not None and self.clock() + max(0, wait) + int(getattr(transport, 'timeout', 30) * 1000) >= deadline_ms:
                        reason='worker_budget'; break
                    if wait>0: self.sleep(wait/1000)
                job,reason=self._claim(session,transport.mode,self.clock(),burst_group,channel)
                if job is None: break
                try:
                    result=normalize_result(transport.send(job))
                except Exception:
                    result={'status':'unknown','evidence':'transport_exception'}
                self._record(session,job,result,self.clock())
                results.append({'job_id':job['id'],'status':result['status'],'code':result.get('code')})
                if result.get('code')==429 or result['status'] not in delivery_flow.PASSED:
                    reason='rate_limited' if result.get('code')==429 else result['status']; break
                with self.store.connect() as db:
                    more=db.execute("SELECT 1 FROM dm_jobs WHERE session_id=? AND group_id=? AND status='pending'",
                                    (session,job['group_id'])).fetchone()
                if more is None:   # group finished; next group waits for the normal gap
                    reason='send_interval'; break
                burst_group=job['group_id']
            with self.store.connect() as db: revision=session_row(db,session)['revision']
            return {'ok':True,'reason':reason,'revision':revision,'results':results,
                    'transport_mode':transport.mode}

    def recover(self, session):
        now=self.clock()
        with file_lock(self.store.path,'send:'+session), self.store.connect(True, operation='dispatch_recover', session=session) as db:
            session_row(db,session)
            jobs=list(db.execute("SELECT * FROM dm_jobs WHERE session_id=? AND status='attempting' AND lease_expires_ms<=?",(session,now)))
            for job in jobs:
                db.execute("UPDATE dm_jobs SET status='unknown',updated_ms=?,result_json=? WHERE session_id=? AND id=?",(now,encode({'status':'unknown','evidence':'expired_attempt_lease'}),session,job['id']))
                mark_unresolved(db,session,job,now)
                delivery_flow.record(db,session,job['id'],'unknown',now)
            advanced=delivery_flow.advance_session(db,session)
            changed=continuations.maintain(db,session,now) or advanced
            if jobs or changed: bump(db,session)
            return {'ok':True,'reason':'recovered','revision':session_row(db,session)['revision'], 'recovered_ids':[j['id'] for j in jobs]}

    def reconcile(self, session, evidence):
        now=self.clock()
        result=normalize_result(evidence['result'])
        require(result['status'] in ('confirmed','failed','unknown'),'invalid_reconcile_status')
        with file_lock(self.store.path,'send:'+session), self.store.connect(True, operation='dispatch_reconcile', session=session) as db:
            state=session_row(db,session)
            require(evidence['expected_revision']==state['revision'],'revision_conflict')
            job=db.execute('SELECT * FROM dm_jobs WHERE session_id=? AND id=?',(session,evidence['job_id'])).fetchone()
            require(job is not None and job['status'] in ('submitted','unknown'),'job_not_reconcilable')
            require(evidence.get('attempt_token')==job['attempt_token'],'attempt_token_conflict')
            if result['status']=='confirmed' and db.execute('SELECT '+JOB_CHANNEL+' FROM dm_jobs j WHERE j.session_id=? AND j.id=?',(session,job['id'])).fetchone()[0]=='slack':
                reserve_claim(db,session,job,result['provider_message_id'])
            db.execute('UPDATE dm_jobs SET status=?,updated_ms=?,result_json=?,provider_message_id=? WHERE session_id=? AND id=?',(result['status'],now,encode(result),result.get('provider_message_id'),session,job['id']))
            delivery_flow.record(db,session,job['id'],result['status'],now)
            bump(db,session)
            if result['status']=='confirmed':
                if ('channel' in job.keys() and job['channel'] == 'slack'):
                    reserve_claim(db,session,job,result['provider_message_id'])
                self._utterance_sent(db,session,job,result,now)
                self._complete_group(db,session,job['group_id'])
            conversation_flow.sync_group(db,session,job['group_id'],now)
            continuations.after_result(db,session,job['group_id'],now)
            return {'ok':True,'reason':'reconciled','revision':state['revision']+1}


    def reconcile_slack_delivery(self, session, job_id):
        """Confirm only an exact persisted receipt under the normal send lock."""
        now = self.clock()
        with file_lock(self.store.path, 'send:'+session), self.store.connect(True, operation='slack_delivery_reconcile', session=session) as db:
            state = session_row(db, session)
            job = db.execute('SELECT * FROM dm_jobs WHERE session_id=? AND id=?', (session, job_id)).fetchone()
            require(job is not None, 'job_not_found')
            if job['status'] == 'confirmed':
                return {'ok': True, 'reason': 'delivery_already_confirmed', 'dispatch_recheck': False,
                        'revision': state['revision']}
            channel = db.execute('SELECT '+JOB_CHANNEL+' FROM dm_jobs j WHERE j.session_id=? AND j.id=?',
                                 (session,job_id)).fetchone()[0]
            require(channel == 'slack', 'delivery_wrong_channel')
            result = verified_result(db, session, job, now)
            require(db.execute("UPDATE dm_jobs SET status='confirmed',updated_ms=?,result_json=?,provider_message_id=? "
                "WHERE session_id=? AND id=? AND attempt_token=? AND status IN ('submitted','unknown')",
                (now, encode(result), result['provider_message_id'], session, job_id, job['attempt_token'])).rowcount == 1,
                'attempt_token_conflict')
            delivery_flow.record(db,session,job_id,'confirmed',now)
            self._utterance_sent(db, session, job, result, now)
            self._complete_group(db, session, job['group_id'])
            continuations.after_result(db,session,job['group_id'],now)
            bump(db, session)
            return {'ok': True, 'reason': 'delivery_confirmed', 'dispatch_recheck': True,
                    'revision': state['revision']+1, 'job_id': job_id}
