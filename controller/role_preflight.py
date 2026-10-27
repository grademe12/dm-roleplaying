"""Pure response checks shared by external pre-submit validation and intake.

No claims, receipts, source DB writes, model calls or automatic field repairs.
Final freshness/Store validation is still required when the proposal is applied.
"""
import copy

from common import ControlError, require
import decision_contract
import model_adapter
import role_contract as contract
import reply_generator
from store import validate_review_checks


SOURCE_ERRORS = {'role_writer_document_not_read', 'generator_memory_not_read'}
SEMANTIC_ERRORS = {'conversation_review_failed'}


def future(decision, now):
    # Format check only: a next_decision_ms that is already past at submit
    # time is no longer rejected here — Store.apply re-bases it on the
    # commit clock (handoff 2026-10-26 §2A).
    if 'next_decision_ms' in decision:
        require(type(decision['next_decision_ms']) is int, 'next_decision_not_future')


def resume_life(output, payload):
    value = copy.deepcopy(output)
    if 'resume_life' not in value:
        return value, payload.get('life_proposal', {})
    require(payload.get('life_revalidation_required') is True, 'role_resume_unavailable')
    old = (payload.get('resume_candidate') or {}).get('life')
    require(old is not None, 'role_resume_unavailable')
    approved = value.pop('resume_life')
    if approved is None:
        require(not value, 'role_invalid_context')
        return None, None
    proposal = contract.life_output({'operations': approved})
    clocks = {'advance_to_ms', 'next_decision_ms'}
    require({k:v for k,v in proposal.items() if k not in clocks} ==
            {k:v for k,v in old.items() if k not in clocks}, 'role_resume_life_changed')
    return value, proposal


def validate(request, response, now):
    result = model_adapter.validate_result(response, request)
    output = copy.deepcopy(result['output'])
    require(isinstance(output, dict) and output.pop('input_read', None) is True, 'role_input_not_read')
    require(now < request['deadline_ms'], 'role_request_expired')
    role = request['task'].removeprefix('persona_')
    require(role in contract.ROLES, 'role_unknown_task')
    payload = request['input']
    if set(output) == {'read'}:
        require(role != 'writer', 'role_writer_cannot_read_tools')
        require(isinstance(output['read'], list) and output['read'], 'role_invalid_reads')
        for item in output['read']:
            require(isinstance(item, dict) and set(item) <= {'kind', 'options'}
                    and isinstance(item.get('kind'), str) and isinstance(item.get('options', {}), dict),
                    'invalid_executor_read')
        return {'ok': True, 'reason': 'valid', 'role': role}
    if role == 'life':
        future(contract.life_output(output), now)
    elif role == 'context':
        require(not payload.get('life_revalidation_required') or 'resume_life' in output,
                'role_resume_confirmation_required')
        output, life = resume_life(output, payload)
        if output is None:
            return {'ok': True, 'reason': 'resume_declined', 'role': role}
        decision, briefs = contract.context_output(output, life)
        future(decision, now)
        for brief in briefs.values():
            require(set(brief['documents']) <= payload['documents'].keys(), 'role_writer_document_not_read')
    elif role == 'writer':
        contract.writer_output(output)
    elif output.get('verdict') in ('revise', 'replan', 'approve_warning'):
        # These are semantic judgments; accepting their shape does not approve them.
        verdict = output['verdict']
        if verdict == 'revise':
            require(set(output) == {'verdict', 'feedback'} and isinstance(output['feedback'], list)
                    and output['feedback'], 'role_invalid_revision')
            seen=set()
            for item in output['feedback']:
                require(isinstance(item, dict) and set(item) == {'index', 'reason'}
                        and type(item['index']) is int and str(item['index']) in payload['drafts']
                        and item['index'] not in seen
                        and isinstance(item['reason'], str) and item['reason'].strip(), 'role_invalid_revision')
                seen.add(item['index'])
        elif verdict == 'replan':
            require(set(output) == {'verdict', 'target', 'reason'} and output['target'] in ('life', 'context')
                    and isinstance(output['reason'], str) and output['reason'].strip(), 'role_invalid_replan')
        else:
            require(set(output) == {'verdict', 'reason'} and isinstance(output['reason'], str)
                    and output['reason'].strip() and payload.get('warning'), 'executor_warning_review_required')
    else:
        drafts = {int(i): {'output': value, 'generator_id': 'preflight-only'}
                  for i, value in payload['drafts'].items()}
        decision, _ = contract.review_output(output, payload['decision'], drafts)
        future(decision, now)
        for item in output['reviews']:
            validate_review_checks(item['conversation_review']['checks'])
            index = item['index']
            dm = decision['dm_decisions'][index]
            reply_generator.validate_parts(drafts[index]['output'], dm['parts'])
        record = {'wake_id': request['request_id'], 'turn_id': payload['prepared']['turn_id'],
                  'prepared': payload['prepared'], 'inputs': payload['inputs']}
        decision_contract.expand(record, decision)
    return {'ok': True, 'reason': 'valid', 'role': role}


def check(request, response, now):
    try:
        return validate(request, response, now)
    except (ControlError, ValueError, KeyError, TypeError) as exc:
        reason = exc.reason if isinstance(exc, ControlError) else 'role_invalid_envelope'
        kind = 'source' if reason in SOURCE_ERRORS else 'semantic' if reason in SEMANTIC_ERRORS else 'format'
        target = 'context' if reason in SOURCE_ERRORS else None
        return {'ok': False, 'reason': reason, 'repair_kind': kind, 'target': target}
