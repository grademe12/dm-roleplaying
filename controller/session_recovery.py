"""Recovery advice only; every retry still passes the normal commit guards."""

REPREPARE = frozenset({
    'revision_conflict', 'prepare_inputs_changed', 'prepare_required',
    'input_prepare_required', 'reply_context_not_ready', 'rules_changed',
    'memory_inputs_changed', 'episodic_reprepare_required', 'character_reprepare_required',
})
MISSING_INPUTS = frozenset({
    'input_receipts_required', 'invalid_input_receipt', 'state_input_required',
    'document_input_required', 'message_input_required', 'episodic_memory_input_required',
    'character_brief_input_required',
})


def advice(reason, *, transient=False):
    if reason == 'stale_claim':
        next_action = 'stop'
    elif reason in REPREPARE:
        next_action = 'prepare_and_review_changes'
    elif reason in MISSING_INPUTS:
        next_action = 'complete_inputs_and_retry_commit'
    elif transient or reason == 'busy':
        next_action = 'retry_same_commit'
    elif reason in {'decision_identity_collision', 'compact_decision_cache_mismatch',
                    'decision_generation_mismatch', 'invalid_session_turn'}:
        next_action = 'inspect_error'
    else:
        # This also covers semantic rejection: the model must review/correct its
        # decision. It is not permission to weaken policy or auto-approve warnings.
        next_action = 'correct_and_retry_commit'
    return {'next': next_action, 'reason': reason}
