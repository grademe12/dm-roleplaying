"""Small discovery cues and explicit, read-backed generator sources.

No extraction, semantic ranking, memory writes, policy changes or network calls.
"""
import re

from common import decode, encode, require
import memory_continuity
import world_events

MAX_BYTES = 3072
FRAME_BYTES = 1024
TOPICS = 4
VISIBLE = world_events.VISIBLE_FACT_SQL + ' AND ' + memory_continuity.VISIBLE_FACT_SQL


def size(value):
    return len(encode(value).encode('utf-8'))


def frame(row):
    if row is None:
        return None
    details = decode(row['details_json'])
    result = {'fact_id':row['id'], 'key':row['topic_key'], 'recorded_ms':row['recorded_ms'],
              'context_only':True, 'state':{}, 'omitted_fields':[]}
    for key in ('frame','current','callbacks','open_threads','evidence_message_ids','previous_id'):
        if key not in details:
            continue
        result['state'][key] = details[key]
        if size(result) > FRAME_BYTES:
            del result['state'][key]
            result['omitted_fields'].append(key)
    return result


def snapshot(db, session, now, channels):
    """Latest actual versions; cues are pointers, never proof of completeness."""
    # Match the topic marker literally. Ranking happens before channel filtering:
    # an old version must not reappear when the latest belongs to another channel.
    rows = list(db.execute(f'''SELECT * FROM (
        SELECT id,summary,details_json,recorded_ms,
          substr(summary,1,instr(summary||' ',' ')-1) AS topic_key,
          row_number() OVER (PARTITION BY substr(summary,1,instr(summary||' ',' ')-1)
                            ORDER BY recorded_ms DESC,id DESC) AS version_rank
        FROM facts WHERE session_id=? AND summary LIKE ? ESCAPE '\\'
          AND source IN ('observed','simulated') AND COALESCE(occurred_ms,recorded_ms)<=?
          AND {VISIBLE}) WHERE version_rank=1 ORDER BY recorded_ms DESC,id DESC''',
        (session,'topic\\_state:%',now)))
    mode = memory_continuity.policy(db,session)['mode']
    result = {}
    # Leave room for channel names and the caller's recent-context counts.
    channel_budget = MAX_BYTES // max(1,len(channels)) - 128
    for channel in sorted(channels):
        visible = []
        current = None
        for row in rows:
            if not re.fullmatch(r'topic_state:[^\s]+',row['topic_key']):
                continue
            details = decode(row['details_json'])
            if not isinstance(details,dict) or details.get('channel',channel) != channel:
                continue
            if row['topic_key'].startswith('topic_state:conversation:'):
                if row['topic_key'] == 'topic_state:conversation:'+channel:
                    current = row
                continue
            visible.append(row)
        cue = {'advisory_only':True, 'memory_mode':mode, 'conversation_frame':frame(current),
               'topics':[], 'topic_count':len(visible), 'topics_has_more':bool(visible),
               'topic_order':'latest_recorded', 'search_is_semantic':False}
        for row in visible[:TOPICS]:
            item = {'key':row['topic_key'], 'fact_id':row['id']}
            candidate = [*cue['topics'],item]
            if size({**cue,'topics':candidate}) > channel_budget:
                break
            cue['topics'] = candidate
        cue['topics_has_more'] = len(cue['topics']) < len(visible)
        # A pathological legacy ID is optional input, not a reason to fail a reply.
        if size(cue) > channel_budget:
            cue['conversation_frame'] = None
            cue['frame_omitted_for_size'] = current is not None
        result[channel] = cue
    return result


def sources(db, session, channel, ids):
    """Copy exact selected facts, keeping provenance and memory interpretation."""
    result = []
    cfg = memory_continuity.policy(db,session)
    for fid in ids:
        row = db.execute(f'SELECT * FROM facts WHERE session_id=? AND id=? AND {VISIBLE}',
                         (session,fid)).fetchone()
        require(row is not None and not fid.startswith('decision:'),'generator_memory_source_unavailable')
        item = dict(row)
        item['details'] = decode(item.pop('details_json'))
        details = item['details']
        require(not isinstance(details,dict) or details.get('channel',channel) == channel,
                'generator_memory_channel_mismatch')
        key = row['summary'].split(' ',1)[0]
        if key.startswith('topic_state:conversation:'):
            require(key == 'topic_state:conversation:'+channel,'generator_memory_channel_mismatch')
        if fid.startswith(memory_continuity.PREFIX):
            memory = db.execute('SELECT * FROM continuity_memories WHERE session_id=? AND id=?',
                                (session,fid[len(memory_continuity.PREFIX):])).fetchone()
            require(memory is not None and memory['status'] == 'active' and cfg['mode'] == 'enforce'
                    and memory['channel'] == channel and memory['scope'] in cfg['scopes'],
                    'generator_memory_source_unavailable')
            item['memory'] = memory_continuity.render(memory)
        result.append(item)
    return result


def fact_ids(content):
    """Only delivered facts / currently active memory records, never metadata search."""
    ids = [f['id'] for f in content.get('facts',[])]
    ids += [i['fact_id'] for i in content.get('items',[])
            if i.get('status') == 'active' and i.get('fact_id')]
    for page in content.get('memory_context',{}).values():
        ids += [i['fact_id'] for i in page.get('items',[])
                if i.get('status') == 'active' and i.get('fact_id')]
    return sorted(set(ids))
