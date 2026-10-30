"""Governance transactions. All identity comes from the current account row."""
import uuid
from django.db import connection, transaction
from django.utils import timezone
from vetcontrols import compile_demand, verify_candidate
from vetcontrols.compiler import _digest, _normalize, _tree_check, parse_text, PERMISSIONS
from .models import Account, Activation, AuditEvent, Candidate, Demand, Operation, PolicyHead, Review

GENERATE = 'governance.candidate.generate'
REVIEW = 'governance.candidate.review'
ACTIVATE = 'governance.candidate.activate'
ROLES = {binding['permission']: binding['role'] for binding in PERMISSIONS['bindings']}


class DomainError(Exception):
    def __init__(self, code, status=409):
        self.code = code
        self.status = status
        self.audit_recorded = False
        self.event_store = None
        super().__init__(code)


def write_audit(actor, action, outcome, normalized_reason, target=None, store_id=None):
    return AuditEvent.objects.create(actor=actor, actor_role=actor.role if actor else '', action=action,
                                     outcome=outcome, normalized_reason=normalized_reason, target=target, store_id=store_id)


def require(actor_id, permission, lock=False):
    if lock and connection.vendor == 'mysql':
        table = connection.ops.quote_name(Account._meta.db_table)
        pk = connection.ops.quote_name(Account._meta.pk.column)
        active = connection.ops.quote_name(Account._meta.get_field('is_active').column)
        rows = Account.objects.raw(
            f'SELECT * FROM {table} WHERE {pk} = %s AND {active} = %s FOR SHARE',
            [actor_id, True])
        actor = next(iter(rows), None)
    else:
        rows = Account.objects.select_for_update() if lock else Account.objects
        actor = rows.filter(pk=actor_id, is_active=True).first()
    if not actor:
        raise DomainError('AUTH_REQUIRED', 401)
    if permission not in ROLES or actor.role != ROLES[permission]:
        raise DomainError('PERMISSION_DENIED', 403)
    return actor


def _uuid(value):
    try:
        if type(value) is not str:
            raise ValueError()
        return uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise DomainError('INVALID_REFERENCE', 422) from None


def _check_candidate(candidate):
    if not verify_candidate(candidate.envelope)['ok'] or candidate.digest != candidate.envelope['content_digest']:
        raise DomainError('CONTENT_CHANGED')
    definition = candidate.envelope['payload']['demand']
    if _uuid(definition['scope']['store_id']) != candidate.store_id:
        raise DomainError('CONTENT_CHANGED')


def _target(actor, ref):
    candidate = Candidate.objects.select_for_update().filter(pk=_uuid(ref)).first()
    if not candidate or candidate.store_id != actor.store_id:
        raise DomainError('TARGET_UNAVAILABLE', 403)
    try:
        _check_candidate(candidate)
    except DomainError as error:
        error.event_store = actor.store_id
        raise
    return candidate


def _fields(payload, expected):
    if set(payload) != set(expected):
        raise DomainError('INPUT_FIELDS_INVALID', 422)


def _shape(permission, payload):
    try:
        _tree_check(payload)
    except (ValueError, RecursionError, UnicodeError, OverflowError):
        raise DomainError('INPUT_INVALID', 422) from None
    if type(payload) is not dict:
        raise DomainError('INPUT_INVALID', 422)
    reason = payload.get('reason')
    if type(reason) is not str or not reason.strip() or len(reason) > 2000:
        raise DomainError('REASON_REQUIRED', 422)
    if permission == GENERATE:
        action = 'generate' if 'definition' in payload else 'submit'
        _fields(payload, ['definition', 'reason'] if action == 'generate' else ['candidate_ref', 'reason'])
    elif permission == REVIEW:
        action = 'review'
        _fields(payload, ['candidate_ref', 'content_digest', 'decision', 'reason'])
        if payload['decision'] not in ('approve', 'reject') or type(payload['content_digest']) is not str:
            raise DomainError('INPUT_INVALID', 422)
    elif permission == ACTIVATE:
        action = 'activate'
        _fields(payload, ['candidate_ref', 'expected_generation', 'reason'])
        if type(payload['expected_generation']) is not int or payload['expected_generation'] < 0:
            raise DomainError('INPUT_INVALID', 422)
    else:
        raise DomainError('PERMISSION_DENIED', 403)
    return action


def _apply(actor, head, candidate, action, payload):
    if action == 'generate':
        result = compile_demand(payload['definition'])
        report = {'ok': result['ok'], 'issues': result['issues']}
        demand = Demand.objects.create(store=actor.store, actor=actor, reason=payload['reason'],
                                       definition=result['candidate']['payload']['demand'] if result['ok'] else None,
                                       input_digest=_digest(_normalize(payload['definition'])), report=report)
        output = {'input_package_ref': str(demand.pk), 'validation_results': report}
        if result['ok']:
            candidate = Candidate.objects.create(store=actor.store, demand=demand, envelope=result['candidate'], digest=result['candidate']['content_digest'])
            output.update(candidate_ref=str(candidate.pk), content_version_ref=candidate.digest, state='draft')
        return output
    if action == 'submit':
        if candidate.state != 'draft':
            raise DomainError('STATE_CONFLICT')
        candidate.state = 'submitted'
        candidate.submitted_by = actor
        candidate.submitted_at = timezone.now()
        candidate.submit_reason = payload['reason']
        candidate.save(update_fields=['state', 'submitted_by', 'submitted_at', 'submit_reason'])
    elif action == 'review':
        if candidate.state != 'submitted' or candidate.digest != payload['content_digest']:
            raise DomainError('STATE_OR_VERSION_CONFLICT')
        Review.objects.create(candidate=candidate, actor=actor, decision=payload['decision'], digest=candidate.digest, reason=payload['reason'])
        candidate.state = 'approved' if payload['decision'] == 'approve' else 'rejected'
        candidate.save(update_fields=['state'])
    elif action == 'activate':
        review = Review.objects.filter(candidate=candidate, decision='approve', digest=candidate.digest).first()
        if candidate.state != 'approved' or not review:
            raise DomainError('APPROVAL_REQUIRED')
        if head.generation != payload['expected_generation']:
            raise DomainError('POLICY_VERSION_CONFLICT')
        head.generation += 1
        head.candidate = candidate
        head.save(update_fields=['generation', 'candidate'])
        Activation.objects.create(store=actor.store, candidate=candidate, actor=actor, digest=candidate.digest,
                                  generation=head.generation, reason=payload['reason'])
    return {'candidate_ref': str(candidate.pk), 'content_version_ref': candidate.digest,
            'state': candidate.state, 'generation': head.generation, 'active_candidate_ref': str(head.candidate_id) if head.candidate_id else None}


def mutate(actor_id, permission, operation_id, payload):
    verified_store = None
    try:
        with transaction.atomic():
            actor = require(actor_id, permission, lock=True)
            action = _shape(permission, payload)
            key = _uuid(operation_id)
            head = PolicyHead.objects.select_for_update().filter(store_id=actor.store_id).first()
            if not head:
                raise DomainError('POLICY_FACILITY_MISSING', 503)
            candidate = None
            if action == 'generate':
                definition = payload['definition']
                if type(definition) is str:
                    try:
                        definition = parse_text(definition)
                    except (ValueError, UnicodeError, RecursionError, OverflowError):
                        raise DomainError('INPUT_INVALID', 422) from None
                    payload = {**payload, 'definition': definition}
                scope = definition.get('scope') if type(definition) is dict else None
                if type(scope) is not dict or _uuid(scope.get('store_id')) != actor.store_id:
                    raise DomainError('SCOPE_DENIED', 403)
                verified_store = actor.store_id
            else:
                candidate = _target(actor, payload['candidate_ref'])
                verified_store = actor.store_id
            digest = _digest(_normalize({'permission': permission, 'action': action, 'payload': payload, 'store': str(actor.store_id)}))
            previous = Operation.objects.filter(actor=actor, permission=permission, key=key).first()
            if previous:
                if previous.digest != digest:
                    raise DomainError('OPERATION_CONTENT_CONFLICT')
                return previous.result
            result = _apply(actor, head, candidate, action, payload)
            write_audit(actor, permission + '.' + action, 'success' if result.get('validation_results', {}).get('ok', True) else 'validation_failed',
                        'COMPLETED' if result.get('validation_results', {}).get('ok', True) else 'CONTROL_INVALID',
                        target=_uuid(result.get('candidate_ref', result.get('input_package_ref'))), store_id=actor.store_id)
            Operation.objects.create(actor=actor, permission=permission, key=key, digest=digest, result=result)
            return result
    except DomainError as error:
        # Known local context uses L; unknown/cross-store governance uses U.
        # A failed transaction never consumes its operation key.
        actor = Account.objects.filter(pk=actor_id).first()
        target = None
        if type(payload) is dict and type(payload.get('candidate_ref')) is str:
            try:
                target = _uuid(payload['candidate_ref'])
            except DomainError:
                pass
        write_audit(actor, permission, 'refused', error.code, target,
                    error.event_store or verified_store or (actor.store_id if actor else None))
        error.audit_recorded = True
        raise


def detail(actor_id, candidate_id):
    with transaction.atomic():
        actor = require(actor_id, REVIEW, lock=True)
        candidate = _target(actor, candidate_id)
        head = PolicyHead.objects.get(store=actor.store)
        review = Review.objects.filter(candidate=candidate).first()
        active = Candidate.objects.filter(pk=head.candidate_id).first() if head.candidate_id else None
        current = candidate.envelope['payload']['demand']
        before = active.envelope['payload']['demand'] if active else {}
        differences = [key for key in sorted(current) if current[key] != before.get(key)]
        result = {'candidate_ref': str(candidate.pk), 'input_package_ref': str(candidate.demand_id), 'content_version_ref': candidate.digest,
                  'state': candidate.state, 'generation': head.generation, 'is_active': head.candidate_id == candidate.pk,
                  'control_definition': current, 'validation_results': candidate.demand.report, 'definition_differences': differences,
                  'governance_history': {'generated_by': candidate.demand.actor_id, 'generated_at': candidate.demand.created_at.isoformat(),
                                         'generation_reason': candidate.demand.reason, 'submitted_by': candidate.submitted_by_id,
                                         'submitted_at': candidate.submitted_at.isoformat() if candidate.submitted_at else None,
                                         'submit_reason': candidate.submit_reason,
                                         'review': {'actor': review.actor_id, 'at': review.created_at.isoformat(), 'decision': review.decision, 'reason': review.reason} if review else None,
                                         'activations': list(Activation.objects.filter(candidate=candidate).values('generation', 'actor_id', 'reason', 'created_at'))}}
        write_audit(actor, REVIEW + '.read', 'success', 'COMPLETED', candidate.pk, actor.store_id)
        return result
