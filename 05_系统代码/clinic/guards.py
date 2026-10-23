"""Trusted clinical-path checks; requests never provide checker facts."""
from governance.services import DomainError


def condition(node, facts):
    op = node['op']
    if op in ['all', 'any']:
        # Evaluate every operand, so unavailable facts cannot hide behind short-circuiting.
        values = [condition(child, facts) for child in node['args']]
        return all(values) if op == 'all' else any(values)
    if node['fact'] not in facts:
        raise DomainError('RULE_FACT_UNAVAILABLE', 503)
    value = facts[node['fact']]
    if op == 'present':
        return True
    candidates = [node['value']] if op == 'eq' else node['values']
    return any(type(value) is type(item) and value == item for item in candidates)


def evaluate(policy, template, values, mode, medication=None):
    clinical = template['clinical']
    facts = {
        'policy.pin_valid': True, 'template.reference_resolved': True,
        'template.interpretation_matches_reference': True,
        'template.required_fields_valid': all(values.get(field, '').strip() for field in clinical['required']),
        'template.medication_group_valid': True,  # This route cannot accept medication fields.
        'medication.append_plan_only': True, 'medication.original_link_valid': True,
        'medication.correction_reason_valid': True,
        'actor.actual_identity_bound': True, 'actor.actual_time_server_owned': True,
        'suggestion.saved_context_valid': True, 'suggestion.transition_allowed': True,
        'suggestion.source_retention_planned': True, 'suggestion.adoption_reason_valid': True,
        'suggestion.no_direct_ai_write': True,  # No suggestion operation is accepted here.
        'audit.facility_ready': True, 'audit.required_plan_complete': True,
        'audit.same_transaction_planned': True,
        'operation.key_bound': True, 'operation.digest_bound': True,
        'operation.current_authorization_checked': True, 'operation.claim_state': 'execute_owner',
        'recovery.local_evidence_plan_ready': True, 'recovery.guard_refusal_plan_ready': True,
        'principal.role': 'doctor', 'operation.mode': mode,
    }
    if medication is not None:
        facts['template.medication_group_valid'] = all(medication['values'].get(field, '').strip() for field in clinical['medication']['all_or_none'])
        facts['medication.original_link_valid'] = medication['original_valid']
        facts['medication.correction_reason_valid'] = medication['reason_valid']
    results, hits = [], []
    available = {'referenced_template', 'append_only_medication', 'human_adoption_and_retention',
                 'audit_and_authorized_export', 'durable_alert_and_idempotency'}
    rules = policy['safety_redlines'] + policy['additional_rules']
    for rule in rules:
        if 'clinical_change' not in rule['phases']:
            continue
        if rule['checker'] not in available:
            raise DomainError('RULE_EXECUTION_UNAVAILABLE', 503)
        selected = 'when' not in rule or condition(rule['when'], facts)
        fields = [field for field in clinical['required'] if not values.get(field, '').strip()] if selected and rule['checker'] == 'referenced_template' else []
        if selected and medication is not None:
            if rule['checker'] == 'referenced_template':
                fields += [field for field in clinical['medication']['all_or_none'] if not medication['values'].get(field, '').strip()]
            if rule['checker'] == 'append_only_medication':
                fields = ([] if medication['original_valid'] else ['original_entry_ref']) + ([] if medication['reason_valid'] else ['reason'])
        result = 'blocked' if fields else 'passed' if selected else 'not_selected'
        if rule['checker'] == 'human_adoption_and_retention' or (rule['checker'] == 'append_only_medication' and medication is None):
            result = 'not_applicable'
        results.append({'rule_ref': rule['id'], 'rule_version': rule['version'], 'result': result})
        hit_values = {**values, **(medication['values'] if medication is not None else {})}
        hits.extend({'rule_ref': rule['id'], 'rule_version': rule['version'], 'field': field, 'value': hit_values.get(field)} for field in fields)
    return results, hits
