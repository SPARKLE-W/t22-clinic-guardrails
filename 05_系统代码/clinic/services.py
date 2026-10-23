"""Reception writes and scoped reads; no clinical write operation in this slice."""
import uuid
from django.db import IntegrityError, transaction
from django.db.models import prefetch_related_objects
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from governance.models import Account, Activation, Candidate, Operation, PolicyHead, Review
from governance.services import DomainError, _uuid, require, write_audit
from vetcontrols.compiler import _digest, _normalize, _tree_check
from .models import AuditTarget, ClinicalRecord, ClinicalRevision, Encounter, PetIdentity, ReceptionRevision

PERMISSIONS = {'create': 'encounter.create', 'correct': 'encounter.correct', 'mark': 'encounter.mark_error'}


def _object(value, allowed, required=()):
    try:
        _tree_check(value)
    except (ValueError, RecursionError, UnicodeError, OverflowError):
        raise DomainError('INPUT_INVALID', 422) from None
    if type(value) is not dict or set(value) - set(allowed) or not set(required) <= set(value):
        raise DomainError('INPUT_FIELDS_INVALID', 422)


def _text(value, maximum, required=True):
    if type(value) is not str or len(value) > maximum or (required and not value.strip()):
        raise DomainError('INPUT_INVALID', 422)
    return value


def _template(candidate, store_id):
    try:
        payload = candidate.envelope['payload']
        template = payload['demand']['templates']
        valid = candidate.store_id == store_id and _digest(payload) == candidate.digest
        valid = valid and Review.objects.filter(candidate=candidate, digest=candidate.digest, decision='approve').exists()
        valid = valid and Activation.objects.filter(candidate=candidate, digest=candidate.digest, store_id=store_id).exists()
        if not valid or not template['clinical']['first_record_still_requires_diagnosis']:
            raise ValueError()
        return template
    except (KeyError, TypeError, ValueError, AttributeError):
        raise DomainError('TEMPLATE_UNAVAILABLE', 503) from None


def _own(actor, ref, clinical=False, lock=False):
    rows = Encounter.objects.select_for_update() if lock else Encounter.objects
    row = rows.filter(pk=_uuid(ref)).first()
    owner = row.doctor_id if clinical and row else row.receptionist_id if row else None
    if not row or row.store_id != actor.store_id or owner != actor.pk:
        error = DomainError('TARGET_UNAVAILABLE', 403)
        if row:
            error.event_store = actor.store_id  # L or X11, never the foreign target store.
        raise error
    return row


def _reception(row):
    revisions = sorted(row.receptionrevision_set.all(), key=lambda r: r.version)
    return {'encounter_ref': str(row.pk), 'store_ref': str(row.store_id), 'pet_ref': str(row.pet_id),
            'receptionist_ref': row.receptionist_id, 'doctor_ref': row.doctor_id, 'created_at': row.created_at.isoformat(),
            'species': row.species, 'complaint': row.complaint, 'pet_name': row.pet_name,
            'error_mark': row.error_mark, 'error_reason': row.error_reason,
            'related_encounter_refs': [str(row.rebuilt_id)] if row.rebuilt_id else [],
            'reception_versions': [{'version_ref': str(r.pk), 'version': r.version, 'species': r.species, 'complaint': r.complaint,
                                    'pet_name': r.pet_name, 'actor_ref': r.actor_id, 'created_at': r.created_at.isoformat(), 'reason': r.reason} for r in revisions]}


def _refusal(actor_id, permission, error, ref=None):
    actor = Account.objects.filter(pk=actor_id).first()
    target = None
    if ref:
        try:
            target = _uuid(ref)
        except DomainError:
            pass
    # This reads opaque target references only; no clinical values enter the event.
    exists = target and Encounter.objects.filter(pk=target).exists()
    if exists and actor and actor.is_active:
        error.event_store = actor.store_id
    with transaction.atomic():
        event = write_audit(actor, permission, 'refused', error.code, target, error.event_store)
        if exists:
            links = [AuditTarget(event=event, target=target, kind='encounter')]
            record_ref = ClinicalRecord.objects.filter(encounter_id=target).values_list('pk', flat=True).first()
            if record_ref:
                links.append(AuditTarget(event=event, target=record_ref, kind='record'))
            AuditTarget.objects.bulk_create(links)
    error.audit_recorded = True
    return event


def mutate(actor_id, mode, key, payload, encounter_ref=None):
    permission = PERMISSIONS.get(mode, '')
    own_store = None
    try:
        with transaction.atomic():
            actor = require(actor_id, permission, lock=True)
            operation_key = _uuid(key)
            if mode == 'create':
                _object(payload, ['pet_identity_choice', 'pet_ref', 'species', 'complaint', 'doctor_ref', 'pet_name', 'template_ref'],
                        ['pet_identity_choice', 'pet_ref', 'species', 'complaint', 'doctor_ref'])
                row = None
            else:
                row = _own(actor, encounter_ref, lock=True)
                own_store = actor.store_id
                _object(payload, ['species', 'complaint', 'pet_name', 'original_version_ref', 'reason'] if mode == 'correct' else ['reason', 'rebuilt_encounter_ref'],
                        ['original_version_ref', 'reason'] if mode == 'correct' else ['reason'])
                _text(payload['reason'], 2000)
            digest = _digest(_normalize({'mode': mode, 'target': encounter_ref, 'payload': payload, 'store': str(actor.store_id)}))
            previous = Operation.objects.filter(actor=actor, permission=permission, key=operation_key).first()
            if previous:
                if previous.digest != digest:
                    raise DomainError('OPERATION_CONTENT_CONFLICT')
                _own(actor, previous.result['encounter_ref'])
                return previous.result
            if mode == 'create':
                head = PolicyHead.objects.select_for_update().filter(store=actor.store).first()
                if not head or not head.candidate_id:
                    raise DomainError('TEMPLATE_UNAVAILABLE', 503)
                candidate = Candidate.objects.get(pk=head.candidate_id)
                _template(candidate, actor.store_id)
                if 'template_ref' in payload and _uuid(payload['template_ref']) != candidate.pk:
                    raise DomainError('INVALID_TEMPLATE', 422)
                doctor_ref = payload['doctor_ref']
                if type(doctor_ref) not in (int, str) or not str(doctor_ref).isdigit() or len(str(doctor_ref)) > 16:
                    raise DomainError('INVALID_REFERENCE', 422)
                doctor = Account.objects.filter(pk=int(doctor_ref), store=actor.store, role='doctor', is_active=True).first()
                if not doctor:
                    raise DomainError('DOCTOR_UNAVAILABLE', 403)
                _text(payload['species'], 100)
                _text(payload['complaint'], 10000)
                _text(payload.get('pet_name', ''), 100, False)
                pet_ref = _uuid(payload['pet_ref'])
                if payload['pet_identity_choice'] == 'new':
                    pet = PetIdentity.objects.create(pk=pet_ref, store=actor.store)
                elif payload['pet_identity_choice'] == 'known':
                    pet = PetIdentity.objects.filter(pk=pet_ref, store=actor.store).first()
                    if not pet:
                        raise DomainError('PET_UNAVAILABLE', 403)
                else:
                    raise DomainError('INPUT_INVALID', 422)
                row = Encounter.objects.create(store=actor.store, pet=pet, receptionist=actor, doctor=doctor, template=candidate,
                                               species=payload['species'], complaint=payload['complaint'], pet_name=payload.get('pet_name', ''))
                own_store = actor.store_id
            elif mode == 'correct':
                _template(row.template, actor.store_id)
                original = ReceptionRevision.objects.get(encounter=row, version=row.version)
                if _uuid(payload['original_version_ref']) != original.pk:
                    raise DomainError('STATE_OR_VERSION_CONFLICT')
                changed = set(payload) & {'species', 'complaint', 'pet_name'}
                if not changed:
                    raise DomainError('INPUT_FIELDS_INVALID', 422)
                for field in changed:
                    setattr(row, field, _text(payload[field], 10000 if field == 'complaint' else 100, field != 'pet_name'))
                row.version += 1
                row.save(update_fields=[*changed, 'version'])
            else:
                if row.error_mark:
                    raise DomainError('STATE_CONFLICT')
                if 'rebuilt_encounter_ref' in payload:
                    rebuilt = _own(actor, payload['rebuilt_encounter_ref'])
                    if rebuilt.pk == row.pk:
                        raise DomainError('INPUT_INVALID', 422)
                    row.rebuilt = rebuilt
                row.error_mark, row.error_reason, row.error_actor, row.error_at = True, payload['reason'], actor, timezone.now()
                row.save(update_fields=['error_mark', 'error_reason', 'error_actor', 'error_at', 'rebuilt'])
            if mode in ['create', 'correct']:
                ReceptionRevision.objects.create(encounter=row, version=row.version, species=row.species, complaint=row.complaint,
                                                 pet_name=row.pet_name, actor=actor, reason=payload.get('reason', ''))
            result = _reception(row)
            event = write_audit(actor, permission, 'success', 'COMPLETED', row.pk, actor.store_id)
            AuditTarget.objects.create(event=event, target=row.pk, kind='encounter')
            Operation.objects.create(actor=actor, permission=permission, key=operation_key, digest=digest, result=result)
            return result
    except IntegrityError:
        error = DomainError('REFERENCE_CONFLICT')
        error.event_store = own_store
        _refusal(actor_id, permission, error, encounter_ref)
        raise error from None
    except DomainError as error:
        error.event_store = error.event_store or own_store
        _refusal(actor_id, permission, error, encounter_ref)
        raise


def _clinical(row, templates):
    try:
        record = row.clinicalrecord
    except ClinicalRecord.DoesNotExist:
        return None
    if record.template_id not in templates:
        templates[record.template_id] = _template(record.template, row.store_id)
    template = templates[record.template_id]
    revisions = sorted(record.clinicalrevision_set.all(), key=lambda r: r.version)
    current = next((r for r in revisions if r.version == record.current_version), None)
    if not current or not current.diagnosis.strip():
        raise DomainError('RECORD_UNAVAILABLE', 503)
    return {'record_ref': str(record.pk), 'encounter_ref': str(row.pk), 'template_version_ref': template['version'],
            'field_meanings': template['clinical']['field_types'], 'diagnosis': current.diagnosis, 'examination': current.examination,
            'medication_chain': [{'entry_ref':str(item.pk),'original_entry_ref':str(item.original_id) if item.original_id else None,
                                 'drug':item.drug,'amount':item.amount,'unit':item.unit,'actor_ref':item.actor_id,
                                 'created_at':item.created_at.isoformat(),'reason':item.reason}
                                for item in sorted(record.medicationentry_set.all(),key=lambda item:(item.created_at,str(item.pk)))],
            'clinical_versions': [{'version_ref': str(r.pk), 'version': r.version, 'diagnosis': r.diagnosis, 'examination': r.examination,
                                   'actor_ref': r.actor_id, 'created_at': r.created_at.isoformat(), 'reason': r.reason} for r in revisions],
            'adoptions': [{'decision_ref':str(item.pk),'suggestion_ref':str(item.suggestion_id),'source':item.suggestion.source,
                           'adopted_content':item.adopted_content,'actor_ref':item.actor_id,'created_at':item.created_at.isoformat(),
                           'reason':item.reason,'version_ref':str(item.version_id),'medication_ref':str(item.medication_id) if item.medication_id else None}
                          for item in sorted(record.suggestion_decisions.all(),key=lambda item:(item.created_at,str(item.pk))) if item.outcome == 'adopted']}


def read(actor_id, clinical=False, ref=None, query=None):
    permission = 'clinical.read' if clinical else 'encounter.read'
    explicit_ref = ref
    try:
        with transaction.atomic():
            actor = require(actor_id, permission, lock=True)
            query = {} if query is None else query
            _object(query, ['encounter_ref', 'record_ref', 'time_range', 'page'])
            if ref is not None and query:
                raise DomainError('INPUT_FIELDS_INVALID', 422)
            if 'encounter_ref' in query:
                _uuid(query['encounter_ref'])
                explicit_ref = query['encounter_ref']
            if 'record_ref' in query:
                if not clinical:
                    raise DomainError('INPUT_FIELDS_INVALID', 422)
                record = ClinicalRecord.objects.filter(pk=_uuid(query['record_ref'])).first()
                if not record:
                    raise DomainError('TARGET_UNAVAILABLE', 403)
                if explicit_ref and _uuid(explicit_ref) != record.encounter_id:
                    raise DomainError('TARGET_UNAVAILABLE', 403)
                explicit_ref = str(record.encounter_id)
            if explicit_ref:
                if 'time_range' in query or 'page' in query:
                    raise DomainError('INPUT_FIELDS_INVALID', 422)
                rows = [_own(actor, explicit_ref, clinical, lock=True)]
                total, page = 1, 1
            else:
                filters = {'store': actor.store, 'doctor' if clinical else 'receptionist': actor}
                values = Encounter.objects.filter(**filters).order_by('-created_at', '-id')
                if 'time_range' in query:
                    time_range = query['time_range']
                    _object(time_range, ['from', 'to'], ['from', 'to'])
                    try:
                        start, end = [parse_datetime(time_range[k]) for k in ['from', 'to']]
                        if start is None or end is None or timezone.is_naive(start) or timezone.is_naive(end) or start >= end:
                            raise ValueError()
                    except (TypeError, ValueError):
                        raise DomainError('INPUT_INVALID', 422) from None
                    values = values.filter(created_at__gte=start, created_at__lt=end)
                page = query.get('page', 1)
                if type(page) is not int or not 1 <= page <= 100000:
                    raise DomainError('INPUT_INVALID', 422)
                total = values.count()
                rows = list(values.select_for_update()[(page-1)*50:page*50])
            relations = ['template', 'receptionrevision_set']
            if clinical:
                relations += ['clinicalrecord__template', 'clinicalrecord__clinicalrevision_set', 'clinicalrecord__medicationentry_set',
                              'clinicalrecord__suggestion_decisions__suggestion']
            prefetch_related_objects(rows, *relations)
            items, targets, templates = [], [], {}
            for row in rows:
                if row.template_id not in templates:
                    templates[row.template_id] = _template(row.template, row.store_id)
                reception = _reception(row)
                record = _clinical(row, templates) if clinical else None
                items.append({'reception': reception, 'clinical': record} if clinical else reception)
                targets.append((row.pk, 'encounter'))
                if record:
                    targets.append((_uuid(record['record_ref']), 'record'))
            event = write_audit(actor, permission, 'success', 'COMPLETED', rows[0].pk if len(rows) == 1 else None, actor.store_id)
            AuditTarget.objects.bulk_create([AuditTarget(event=event, target=target, kind=kind) for target, kind in targets])
            return items[0] if ref else {'items': items, 'page': page, 'page_size': 50, 'total': total}
    except DomainError as error:
        _refusal(actor_id, permission, error, explicit_ref)
        raise


def auxiliary(actor_id, mode, payload=None):
    actor = require(actor_id, 'encounter.create')
    if mode == 'doctor_options':
        _object({} if payload is None else payload, [])
        return {'doctor_options': [{'doctor_ref': r.pk, 'display_name': r.username} for r in Account.objects.filter(store=actor.store, role='doctor', is_active=True).order_by('id')]}
    if mode == 'verify_pet_ref':
        _object(payload, ['pet_ref'], ['pet_ref'])
        ref = _uuid(payload['pet_ref'])
        return {'pet_ref': str(ref), 'associable': PetIdentity.objects.filter(pk=ref, store=actor.store).exists()}
    raise DomainError('PERMISSION_DENIED', 403)
