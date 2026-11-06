"""Atomic clinical writes, guard refusals and doctor alert handling."""
from django.db import transaction
from django.utils import timezone
from governance.models import Operation
from governance.services import DomainError, _uuid, require, write_audit
from vetcontrols.compiler import _digest, _normalize, is_verified_candidate
from .guards import evaluate
from .export_projection import note_encounter_lock, refresh_projection
from .models import Alert, AuditTarget, ClinicalCheck, ClinicalRecord, ClinicalRevision
from .services import (_clinical, _clinical_prefix_payload, _extra_values, _object, _own, _persist_guard_alerts, _policy_head_for_share,
                       _refusal, _template, _text, read)

WRITE = 'clinical.diagnosis.write'
ALERT = 'clinical.alert.handle'


def _validated(candidate, store_id):
    try:
        if not is_verified_candidate(candidate.envelope) or candidate.digest != candidate.envelope['content_digest']:
            raise DomainError('CONTENT_CHANGED')
        return _template(candidate, store_id)
    except (DomainError, AttributeError, KeyError, TypeError, ValueError):
        raise DomainError('POLICY_UNAVAILABLE', 503) from None


def _audit(actor, permission, row, result, reason, record=None):
    event = write_audit(actor, permission, result, reason, row.pk, actor.store_id)
    targets = [AuditTarget(event=event, target=row.pk, kind='encounter')]
    if record:
        targets.append(AuditTarget(event=event, target=record.pk, kind='record'))
    AuditTarget.objects.bulk_create(targets)
    return event


def diagnosis(actor_id, mode, key, payload, ref):
    try:
        with transaction.atomic():
            actor = require(actor_id, WRITE, lock=True)
            operation_key = _uuid(key)
            head = _policy_head_for_share(actor.store_id)
            row = _own(actor, ref, clinical=True, lock=True)
            allowed = ['diagnosis', 'examination', 'extra_fields']
            if mode == 'correct':
                allowed += ['original_content_ref', 'original_version_ref', 'reason']
            _object(payload, allowed, ['original_content_ref', 'original_version_ref', 'reason'] if mode == 'correct' else [])
            for field in ['diagnosis', 'examination']:
                if field in payload:
                    _text(payload[field], 10000, False)
            digest = _digest(_normalize({'mode': mode, 'target': ref, 'payload': payload, 'store': str(actor.store_id)}))
            previous = Operation.objects.filter(actor=actor, permission=WRITE, key=operation_key).first()
            if previous:
                if previous.digest != digest:
                    raise DomainError('OPERATION_CONTENT_CONFLICT')
                return previous.result
            record = ClinicalRecord.objects.filter(encounter=row).first()
            current = None
            if mode == 'record':
                if row.error_mark or record:
                    raise DomainError('STATE_CONFLICT')
                values = {'diagnosis': payload.get('diagnosis', ''), 'examination': payload.get('examination', '')}
            else:
                if not record:
                    raise DomainError('STATE_CONFLICT')
                _text(payload['reason'], 2000)
                current = ClinicalRevision.objects.filter(record=record, version=record.current_version).first()
                if not current:
                    raise DomainError('RECORD_UNAVAILABLE', 503)
                if _uuid(payload['original_content_ref']) != record.pk or _uuid(payload['original_version_ref']) != current.pk:
                    raise DomainError('STATE_OR_VERSION_CONFLICT')
                if not (set(payload) & {'diagnosis', 'examination'} or payload.get('extra_fields')):
                    raise DomainError('INPUT_FIELDS_INVALID', 422)
                values = {field: payload.get(field, getattr(current, field)) for field in ['diagnosis', 'examination']}
            if not head or not head.candidate_id:
                raise DomainError('POLICY_UNAVAILABLE', 503)
            template_candidate = record.template if record else row.template
            template = _validated(template_candidate, actor.store_id)
            policy_candidate = template_candidate if head.candidate_id == template_candidate.pk else head.candidate
            if policy_candidate.pk != template_candidate.pk:
                _validated(policy_candidate, actor.store_id)
            extra_fields = _extra_values(template, payload.get('extra_fields', {}), current.extra_fields if current else None)
            checks, hits = evaluate(policy_candidate.envelope['payload']['demand'], template, values, mode)
            check = ClinicalCheck(encounter=row, record=record, original_version=current, actor=actor, mode=mode,
                         template=template_candidate, policy=policy_candidate, policy_digest=policy_candidate.digest,
                         status='blocked' if hits else 'passed', checks=checks)
            if hits:
                check.save(force_insert=True)
                _persist_guard_alerts(check, hits)
                _audit(actor, WRITE, row, 'refused', 'GUARD_BLOCKED', record)
                result = {'ok': False, 'error': {'code': 'GUARD_BLOCKED'}, 'event_ref': str(check.pk)}
            else:
                if not record:
                    record = ClinicalRecord.objects.create(encounter=row, template=template_candidate)
                else:
                    record.current_version += 1
                note_encounter_lock(record, row)
                version = ClinicalRevision.objects.create(record=record, version=record.current_version, actor=actor,
                                                          reason=payload.get('reason', ''), extra_fields=extra_fields, **values)
                check.record = record
                check.result_version = version
                check.save(force_insert=True)
                prefix = refresh_projection(record, row, template_candidate, template, save_version=current is not None)
                _audit(actor, WRITE, row, 'success', 'COMPLETED', record)
                # Load through the same explicit, doctor-only serializer as reads.
                row.clinicalrecord = record
                result = _clinical_prefix_payload(prefix, record.pk, row.pk, template['version'])
                if result is None:
                    result = _clinical(row, {template_candidate.pk: template})
            Operation.objects.create(actor=actor, permission=WRITE, key=operation_key, digest=digest, result=result)
            return result
    except DomainError as error:
        _refusal(actor_id, WRITE, error, ref)
        raise


def _alert_item(alert):
    check = alert.hit.evaluation
    return {'alert_ref': str(alert.pk), 'event_ref': str(check.pk),
            'record_ref': str(check.record_id) if check.record_id else None,
            'rule_ref': alert.hit.rule_ref, 'hit_field': alert.hit.field,
            'handling_state': 'handled' if alert.handled_at else 'unhandled',
            'handler_ref': alert.handler_id, 'handling_result': alert.handling_result,
            'handled_at': alert.handled_at.isoformat() if alert.handled_at else None}


def alert_refusal(actor_id, error, ref=None, permission=ALERT, record_ref=None):
    try:
        alert = Alert.objects.select_related('hit__evaluation').filter(pk=_uuid(ref)).first() if ref else None
        record = ClinicalRecord.objects.filter(pk=_uuid(record_ref)).first() if record_ref else None
    except DomainError:
        alert, record = None, None
    with transaction.atomic():
        encounter_ref = str(alert.hit.evaluation.encounter_id) if alert else str(record.encounter_id) if record else None
        event = _refusal(actor_id, permission, error, encounter_ref)
        if alert:
            AuditTarget.objects.create(event=event, target=alert.pk, kind='alert')


def alert_encounter(actor_id, ref):
    alert = Alert.objects.select_related('hit__evaluation').filter(pk=_uuid(ref)).first()
    if not alert:
        raise DomainError('TARGET_UNAVAILABLE', 403)
    return read(actor_id, clinical=True, ref=str(alert.hit.evaluation.encounter_id))


def alerts(actor_id, query):
    with transaction.atomic():
        actor = require(actor_id, ALERT, lock=True)
        _object(query, ['alert_ref', 'handling_state', 'record_ref', 'rule_ref'])
        rows = Alert.objects.filter(hit__evaluation__encounter__store_id=actor.store_id,
                                    hit__evaluation__encounter__doctor_id=actor.pk)
        if 'alert_ref' in query:
            rows = rows.filter(pk=_uuid(query['alert_ref']))
            if not rows.exists():
                raise DomainError('TARGET_UNAVAILABLE', 403)
        if 'record_ref' in query:
            record = ClinicalRecord.objects.filter(pk=_uuid(query['record_ref'])).first()
            if not record:
                raise DomainError('TARGET_UNAVAILABLE', 403)
            _own(actor, str(record.encounter_id), clinical=True, lock=True)
            rows = rows.filter(hit__evaluation__encounter_id=record.encounter_id)
        if 'rule_ref' in query:
            rows = rows.filter(hit__rule_ref=_text(query['rule_ref'], 100))
        if 'handling_state' in query:
            if query['handling_state'] not in ['handled', 'unhandled']:
                raise DomainError('INPUT_INVALID', 422)
            rows = rows.filter(handled_at__isnull=query['handling_state'] == 'unhandled')
        items = list(rows.select_for_update().select_related('hit__evaluation').order_by('hit__evaluation__created_at', 'id'))
        for alert in items:
            _audit(actor, ALERT, alert.hit.evaluation.encounter, 'success', 'COMPLETED', alert.hit.evaluation.record)
        return {'items': [_alert_item(alert) for alert in items]}


def resolve(actor_id, key, payload, ref):
    with transaction.atomic():
        actor = require(actor_id, ALERT, lock=True)
        operation_key = _uuid(key)
        _object(payload, ['alert_ref', 'handling_result'], ['alert_ref', 'handling_result'])
        if _uuid(payload['alert_ref']) != _uuid(ref):
            raise DomainError('TARGET_UNAVAILABLE', 403)
        _text(payload['handling_result'], 2000)
        alert = Alert.objects.select_related('hit__evaluation').filter(pk=_uuid(ref)).first()
        if not alert:
            raise DomainError('TARGET_UNAVAILABLE', 403)
        row = _own(actor, str(alert.hit.evaluation.encounter_id), clinical=True, lock=True)
        alert = Alert.objects.select_for_update().select_related('hit__evaluation').get(pk=alert.pk)
        digest = _digest({'target': ref, 'payload': payload, 'store': str(actor.store_id)})
        previous = Operation.objects.filter(actor=actor, permission=ALERT, key=operation_key).first()
        if previous:
            if previous.digest != digest:
                raise DomainError('OPERATION_CONTENT_CONFLICT')
            return previous.result
        if alert.handled_at:
            raise DomainError('STATE_CONFLICT')
        alert.handler, alert.handled_at, alert.handling_result = actor, timezone.now(), payload['handling_result']
        alert.save(update_fields=['handler', 'handled_at', 'handling_result'])
        _audit(actor, ALERT, row, 'success', 'COMPLETED', alert.hit.evaluation.record)
        result = _alert_item(alert)
        Operation.objects.create(actor=actor, permission=ALERT, key=operation_key, digest=digest, result=result)
        return result
