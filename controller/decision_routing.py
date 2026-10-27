"""Read-only work selection and explicit snapshot projections. No dispatch authority."""

VIEWS = ('full', 'dm', 'life', 'gate')
LIFE_REASONS = ('morning_contact_due', 'activity_due', 'no_current_activity', 'appointment_due',
                'appointment_prepare_due','appointment_review_due','weekly_plan_missing')
DM_REASONS = ('proactive_check_due', 'reply_due', 'draft_needs_review', 'draft_stale')
REPAIR_REASONS = ('controller_error', 'poller_stale', 'poller_alert', 'message_review',
                  'dispatch_unresolved')
RUNBOOKS = {name: 'runbooks/' + name + '.md' for name in ('dm', 'life', 'background', 'repair')}


def work_manifest(snap, now, reasons=None):
    reasons = list(reasons if reasons is not None else snap.get('needs_decision', []))
    messages = snap.get('messages', [])
    incoming = [m['id'] for m in messages if m['status'] == 'received']
    due_intents = snap.get('action_intent_due_ids', [])
    life = [r for r in LIFE_REASONS if r in reasons]
    if due_intents:
        life.append('action_intent_due')
    if snap.get('continuation_due_ids'):
        life.append('continuation_due')
    scheduled = (snap.get('state') or {}).get('next_decision_ms')
    if 'decision_due' in reasons and scheduled is not None and scheduled <= now:
        life.append('scheduled_decision')
    unresolved = [j['id'] for j in snap.get('jobs', [])
                  if j['status'] in ('attempting', 'submitted', 'unknown')]
    repair = [r for r in REPAIR_REASONS if r in reasons]
    fatal = any(r in reasons for r in ('controller_error', 'poller_stale', 'poller_alert'))
    urgent = bool(incoming or any(r in reasons for r in DM_REASONS))
    primary = ('repair' if fatal else 'dm' if urgent else 'life' if life else
               'repair' if repair else 'idle')
    queued = any(m['status'] in ('received', 'queued') for m in messages)
    sending = any(j['status'] in ('pending', 'attempting')
                  for j in snap.get('jobs', []))
    attention = (snap.get('activity') or {}).get('attention')
    background_allowed = (not queued and not sending and not life and not repair
                          and not snap.get('conversation_lease_until_ms')
                          and attention in ('free', 'light'))
    return {'version': 1, 'primary': primary, 'runbook': RUNBOOKS.get(primary),
            'interaction_mode':snap.get('interaction',{}).get('mode','remote'),
            'in_person_candidate':bool(snap.get('interaction',{}).get('candidate_appointment_id')),
            'view': 'dm' if primary in ('dm', 'repair') else 'life',
            'life_required': life, 'action_intent_due_ids': due_intents,
            'continuation_due_ids': snap.get('continuation_due_ids', []),
            'repair': repair, 'unresolved_job_ids': unresolved,
            'background_allowed': background_allowed}


def project(snap, view):
    """Absence in a projection never means absence in the authoritative ledger."""
    omitted = []
    if view != 'full':
        omitted.append('core_rules')
        snap.pop('core_rules', None)
    if view in ('dm', 'gate'):
        if view=='gate':
            snap.pop('world_events',None)
        elif 'world_events' in snap:
            snap['world_events'].pop('pending',None)
            snap['world_events'].pop('pending_has_more',None)
        if view=='gate' or not snap['work_manifest']['life_required']:
            omitted.append('activity_history')
            snap.pop('activity_history',None)
        omitted.append('weekly_plan')
        snap.pop('weekly_plan', None)
        if view == 'gate':
            # Gate evaluation needs only routing metadata, never conversation content.
            snap['continuations'] = [{k:r.get(k) for k in
                ('id','channel','status','generation','buffered','review_at_ms','expires_ms')}
                for r in snap.get('continuations', [])]
            snap['messages'] = [{k: m.get(k) for k in
                                 ('id', 'status', 'observed_ms', 'reply_due_ms', 'observation')}
                                for m in snap['messages']]
            for m in snap['messages']:
                m['observation'] = {k: v for k, v in (m['observation'] or {}).items()
                                    if k in ('channel', 'review_required')}
            snap['jobs'] = [{k: j.get(k) for k in ('id', 'group_id', 'status', 'channel')}
                            for j in snap['jobs']]
            omitted.extend(('action_intents', 'action_intent_followups', 'conversation_policy'))
            for k in omitted:
                snap.pop(k, None)
        # Message bodies remain in dm.messages, not duplicated in event payloads.
        snap['events'] = [{k: v for k, v in e.items() if k != 'payload'} for e in snap['events']]
        omitted.append('events[].payload')
    snap['projection'] = {'view': view, 'omitted_fields': omitted}
    return snap
