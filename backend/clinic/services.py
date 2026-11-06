"""Reception writes and scoped reads; no clinical write operation in this slice."""
import csv
import io
import json
import uuid
from django.db import IntegrityError, connection, transaction
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Prefetch, prefetch_related_objects
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from governance.models import Account, Activation, Candidate, Operation, PolicyHead
from governance.services import DomainError, _uuid, require, write_audit
from vetcontrols.compiler import _digest, _normalize, _tree_check
from .models import (Alert, AlertOutbox, AuditTarget, ClinicalRecord, ClinicalRevision, Encounter, GuardHit,
                     PetIdentity, ReceptionRevision, SuggestionDecision)

PERMISSIONS = {'create': 'encounter.create', 'correct': 'encounter.correct', 'mark': 'encounter.mark_error'}


def _adopted_suggestion_prefetch(lookup='suggestion_decisions'):
    queryset = (SuggestionDecision.objects.filter(outcome='adopted')
                .select_related('suggestion')
                .only('id', 'record_id', 'suggestion_id', 'outcome', 'adopted_content',
                      'actor_id', 'created_at', 'reason', 'version_id', 'medication_id',
                      'suggestion__id', 'suggestion__source'))
    return Prefetch(lookup, queryset=queryset, to_attr='prefetched_adopted_suggestions')


def _related_rows(record, relation, attribute):
    if hasattr(record, attribute):
        return getattr(record, attribute)
    return getattr(record, relation).all()


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


def _persist_guard_alerts(evaluation, hits):
    guard_hits = [GuardHit(evaluation=evaluation, **item) for item in hits]
    GuardHit.objects.bulk_create(guard_hits)
    alerts = [Alert(hit=hit) for hit in guard_hits]
    Alert.objects.bulk_create(alerts)
    AlertOutbox.objects.bulk_create([AlertOutbox(alert=alert) for alert in alerts])


def _extra_values(template, supplied, existing=None):
    fields = {item['id']: item['type'] for item in template['clinical']['extra_fields']}
    if type(supplied) is not dict or set(supplied) - set(fields):
        raise DomainError('INPUT_FIELDS_INVALID', 422)
    values = dict(existing or {})
    for name, value in supplied.items():
        if value is None:
            values.pop(name, None)
        elif fields[name] == 'text' and type(value) is str and len(value) <= 10000:
            values[name] = value
        elif fields[name] == 'integer' and type(value) is int and -(2**63) <= value < 2**63:
            values[name] = value
        elif fields[name] == 'boolean' and type(value) is bool:
            values[name] = value
        else:
            raise DomainError('INPUT_INVALID', 422)
    return values


def _template(candidate, store_id):
    try:
        payload = candidate.envelope['payload']
        template = payload['demand']['templates']
        valid = candidate.store_id == store_id and _digest(payload) == candidate.digest
        valid = valid and Activation.objects.filter(
            candidate=candidate, digest=candidate.digest, store_id=store_id,
            candidate__review__digest=candidate.digest, candidate__review__decision='approve',
        ).exists()
        if not valid or not template['clinical']['first_record_still_requires_diagnosis']:
            raise ValueError()
        return template
    except (KeyError, TypeError, ValueError, AttributeError):
        raise DomainError('TEMPLATE_UNAVAILABLE', 503) from None


def _policy_head_for_share(store_id):
    if connection.vendor == 'mysql':
        table = connection.ops.quote_name(PolicyHead._meta.db_table)
        store_column = connection.ops.quote_name(PolicyHead._meta.get_field('store').column)
        rows = PolicyHead.objects.raw(
            f'SELECT * FROM {table} WHERE {store_column} = %s FOR SHARE',
            [PolicyHead._meta.get_field('store').target_field.get_db_prep_value(store_id, connection)])
        return next(iter(rows), None)
    return PolicyHead.objects.select_for_update().filter(store_id=store_id).first()


def _own(actor, ref, clinical=False, lock=False, join_read_templates=False):
    rows = Encounter.objects
    if join_read_templates:
        related = ('template', 'clinicalrecord__export_projection') if clinical else ('template',)
        rows = rows.select_related(*related)
    rows = rows.select_for_update() if lock else rows
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
    if actor and actor.is_active and error.event_store is None:
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
                doctor_ref = payload['doctor_ref']
                if type(doctor_ref) not in (int, str) or not str(doctor_ref).isdigit() or len(str(doctor_ref)) > 16:
                    raise DomainError('INVALID_REFERENCE', 422)
                # The shared account check keeps assignment valid until commit while
                # allowing the doctor's concurrent clinical requests to proceed.
                try:
                    doctor = require(int(doctor_ref), 'clinical.read', lock=True)
                except DomainError:
                    raise DomainError('DOCTOR_UNAVAILABLE', 403) from None
                if doctor.store_id != actor.store_id:
                    raise DomainError('DOCTOR_UNAVAILABLE', 403)
                head = _policy_head_for_share(actor.store_id)
                if not head or not head.candidate_id:
                    raise DomainError('TEMPLATE_UNAVAILABLE', 503)
                candidate = Candidate.objects.get(pk=head.candidate_id)
                _template(candidate, actor.store_id)
                if 'template_ref' in payload and _uuid(payload['template_ref']) != candidate.pk:
                    raise DomainError('INVALID_TEMPLATE', 422)
                _text(payload['species'], 100)
                _text(payload['complaint'], 10000)
                _text(payload.get('pet_name', ''), 100, False)
                pet_ref = _uuid(payload['pet_ref'])
                if payload['pet_identity_choice'] == 'new':
                    pet = PetIdentity.objects.create(pk=pet_ref, store_id=actor.store_id)
                elif payload['pet_identity_choice'] == 'known':
                    pet = PetIdentity.objects.filter(pk=pet_ref, store_id=actor.store_id).first()
                    if not pet:
                        raise DomainError('PET_UNAVAILABLE', 403)
                else:
                    raise DomainError('INPUT_INVALID', 422)
                row = Encounter.objects.create(store_id=actor.store_id, pet=pet, receptionist=actor, doctor=doctor, template=candidate,
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


def _clinical(row, templates, field_meanings_cache=None):
    try:
        record = row.clinicalrecord
    except ClinicalRecord.DoesNotExist:
        return None
    if record.template_id not in templates:
        candidate = row.template if record.template_id == row.template_id else record.template
        templates[record.template_id] = _template(candidate, row.store_id)
    template = templates[record.template_id]
    revisions = sorted(_related_rows(record, 'clinicalrevision_set', 'prefetched_clinical_revisions'), key=lambda r: r.version)
    current = next((r for r in revisions if r.version == record.current_version), None)
    if not current or not current.diagnosis.strip():
        raise DomainError('RECORD_UNAVAILABLE', 503)
    meanings = field_meanings_cache.get(record.template_id) if field_meanings_cache is not None else None
    if meanings is None:
        meanings = {**template['clinical']['field_types'],
                    **{item['id']: item['type'] for item in template['clinical']['extra_fields']}}
        if field_meanings_cache is not None:
            field_meanings_cache[record.template_id] = meanings
    return {'record_ref': str(record.pk), 'encounter_ref': str(row.pk), 'template_version_ref': template['version'],
            'field_meanings': meanings, 'diagnosis': current.diagnosis, 'examination': current.examination,
            'extra_fields': current.extra_fields,
            'medication_chain': [{'entry_ref':str(item.pk),'original_entry_ref':str(item.original_id) if item.original_id else None,
                                 'drug':item.drug,'amount':item.amount,'unit':item.unit,'actor_ref':item.actor_id,
                                 'created_at':item.created_at.isoformat(),'reason':item.reason}
                                for item in sorted(_related_rows(record, 'medicationentry_set', 'prefetched_medication_entries'),key=lambda item:(item.created_at,str(item.pk)))],
            'clinical_versions': [{'version_ref': str(r.pk), 'version': r.version, 'diagnosis': r.diagnosis, 'examination': r.examination,
                                   'extra_fields': r.extra_fields,
                                   'actor_ref': r.actor_id, 'created_at': r.created_at.isoformat(), 'reason': r.reason} for r in revisions],
            'adoptions': [{'decision_ref':str(item.pk),'suggestion_ref':str(item.suggestion_id),'source':item.suggestion.source,
                           'adopted_content':item.adopted_content,'actor_ref':item.actor_id,'created_at':item.created_at.isoformat(),
                           'reason':item.reason,'version_ref':str(item.version_id),'medication_ref':str(item.medication_id) if item.medication_id else None}
                          for item in sorted(_related_rows(record, 'suggestion_decisions', 'prefetched_adopted_suggestions'),key=lambda item:(item.created_at,str(item.pk))) if item.outcome == 'adopted']}


def _projected_clinical(row):
    """Reuse the transactional export snapshot for an authorized single read."""
    from .export_projection import validated_prefix

    try:
        record = row.clinicalrecord
        projection = record.export_projection
    except ObjectDoesNotExist:
        return None
    value = {'id': record.pk, 'encounter_id': row.pk, 'template_id': record.template_id,
                'current_version': record.current_version, 'export_generation': record.export_generation,
                'encounter__store_id': row.store_id, 'encounter__template_id': row.template_id,
                'encounter__doctor_id': row.doctor_id, 'encounter__receptionist_id': row.receptionist_id,
                'encounter__created_at': row.created_at}
    value.update({'export_projection__prefix': projection.prefix,
                  'export_projection__prefix_digest': projection.prefix_digest,
                  'export_projection__source_signature': projection.source_signature})
    candidate = row.template if row.template_id == row.clinicalrecord.template_id else row.clinicalrecord.template
    template = _template(candidate, row.store_id)
    prefix = validated_prefix(value, candidate.digest)
    if prefix is None:
        return None
    return _clinical_prefix_payload(prefix, record.pk, row.pk, template['version'])


def _clinical_prefix_payload(prefix, record_ref, encounter_ref, template_version):
    """Decode the same clinical fields from a verified or just-generated prefix."""
    try:
        records = list(csv.reader(io.StringIO(prefix.decode('utf-8'), newline=''), strict=True))
        if len(records) != 1 or len(records[0]) != 11:
            return None
        cells = records[0]
        if cells[:3] != [str(record_ref), str(encounter_ref), template_version] or not cells[4].strip():
            return None
        decoded = {key: json.loads(cells[index]) for key, index in
                   [('field_meanings', 3), ('extra_fields', 6), ('adoptions', 7),
                    ('medication_chain', 9), ('clinical_versions', 10)]}
        if any(type(decoded[key]) is not dict for key in ('field_meanings', 'extra_fields')):
            return None
        if any(type(decoded[key]) is not list for key in ('adoptions', 'medication_chain', 'clinical_versions')):
            return None
        return {'record_ref': cells[0], 'encounter_ref': cells[1], 'template_version_ref': cells[2],
                'diagnosis': cells[4], 'examination': cells[5], **decoded}
    except (ValueError, UnicodeError, csv.Error):
        return None


def read(actor_id, clinical=False, ref=None, query=None):
    permission = 'clinical.read' if clinical else 'encounter.read'
    explicit_ref = ref
    try:
        with transaction.atomic():
            actor = require(actor_id, permission)
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
                rows = [_own(actor, explicit_ref, clinical, join_read_templates=True)]
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
                rows = list(values[(page-1)*50:page*50])
            projected = _projected_clinical(rows[0]) if clinical and explicit_ref else None
            relations = ['template', 'receptionrevision_set']
            if clinical and projected is None:
                if not explicit_ref:
                    relations.append('clinicalrecord__template')
                relations += [Prefetch('clinicalrecord__clinicalrevision_set', to_attr='prefetched_clinical_revisions'),
                              Prefetch('clinicalrecord__medicationentry_set', to_attr='prefetched_medication_entries'),
                              _adopted_suggestion_prefetch('clinicalrecord__suggestion_decisions')]
            prefetch_related_objects(rows, *relations)
            items, targets, templates, field_meanings = [], [], {}, {}
            for row in rows:
                if row.template_id not in templates:
                    templates[row.template_id] = _template(row.template, row.store_id)
                reception = _reception(row)
                record = (projected if projected is not None else _clinical(row, templates, field_meanings)) if clinical else None
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
