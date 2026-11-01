"""Generate complete authorized CSV before writing the required export audit."""
import csv
import hashlib
import io
import json
import logging
from django.db import transaction
from django.db.models import Count, Q, prefetch_related_objects
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_protect
from governance.models import Candidate
from governance.services import DomainError, _uuid, require, write_audit
from governance.views import _body, protected
from .models import AuditTarget, ClinicalRecord, Encounter, ExportRequest, GuardHit
from .services import _clinical, _object, _text

CLINICAL = 'export.clinical'
CLINICAL_COLUMNS = ['record_ref', 'encounter_ref', 'template_version_ref', 'field_meanings',
                    'diagnosis', 'examination', 'extra_fields', 'adopted_content', 'responsibility_metadata',
                    'medication_chain', 'clinical_versions', 'error_mark', 'related_encounter_refs']
DETAIL = 'export.guard_detail'
SUMMARY = 'export.guard_summary'
DETAIL_COLUMNS = ['event_ref', 'record_ref', 'rule_ref', 'hit_field', 'handling_state',
                  'handler_ref', 'handled_at', 'handling_result']
SUMMARY_COLUMNS = ['time_range', 'rule_ref_filter', 'handling_state_filter', 'group_by',
                   'rule_ref', 'handling_state', 'hit_count', 'alert_count', 'unhandled_count']
logger = logging.getLogger(__name__)


def _range(interval):
    _object(interval, ['from', 'to'], ['from', 'to'])
    try:
        start, end = parse_datetime(interval['from']), parse_datetime(interval['to'])
        if start is None or end is None or timezone.is_naive(start) or timezone.is_naive(end) or start >= end:
            raise ValueError()
    except (TypeError, ValueError):
        raise DomainError('INPUT_INVALID', 422) from None
    return start, end


def _csv(columns, rows):
    try:
        output = io.StringIO(newline='')
        writer = csv.DictWriter(output, fieldnames=columns, lineterminator='\r\n')
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue().encode('utf-8')
    except (csv.Error, UnicodeError, TypeError, ValueError):
        logger.exception('CSV export generation failed')
        raise DomainError('EXPORT_GENERATION_UNAVAILABLE', 503) from None


def _refusal(actor, permission, error):
    kind, refs = error.export_targets
    model = ClinicalRecord if kind == 'record' else Encounter
    with transaction.atomic():
        known = model.objects.filter(pk__in=refs).values_list('pk', flat=True)
        event = write_audit(actor, permission, 'request_refused', error.code,
                            store_id=actor.store_id)
        AuditTarget.objects.bulk_create([AuditTarget(event=event, target=ref, kind=kind) for ref in known])
    error.audit_recorded = True


def _response(body, ref, count, name):
    response = HttpResponse(body, content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{name}.csv"'
    response['Cache-Control'] = 'no-store'
    response['X-Export-Ref'] = str(ref)
    response['X-Record-Count'] = str(count)
    response['Content-Length'] = str(len(body))
    response['X-Byte-Count'] = str(len(body))
    response['X-Content-SHA256'] = hashlib.sha256(body).hexdigest()
    return response


def clinical(actor_id, payload):
    with transaction.atomic():
        actor = require(actor_id, CLINICAL)
        _object(payload, ['scope', 'purpose'], ['scope', 'purpose'])
        _text(payload['purpose'], 2000)
        scope = payload['scope']
        _object(scope, ['record_refs', 'time_range'])
        if len(scope) != 1:
            raise DomainError('INPUT_FIELDS_INVALID', 422)
        rows = ClinicalRecord.objects.filter(encounter__store_id=actor.store_id, encounter__doctor_id=actor.pk)
        if 'record_refs' in scope:
            refs = scope['record_refs']
            if type(refs) is not list or not refs or any(type(ref) is not str for ref in refs):
                raise DomainError('INPUT_INVALID', 422)
            refs = [_uuid(ref) for ref in refs]
            if len(set(refs)) != len(refs):
                raise DomainError('INPUT_INVALID', 422)
            rows = rows.filter(pk__in=refs)
        else:
            start, end = _range(scope['time_range'])
            rows = rows.filter(encounter__created_at__gte=start, encounter__created_at__lt=end)
        rows = list(rows.select_related('encounter').order_by('encounter__created_at', 'pk'))
        if 'record_refs' in scope and len(rows) != len(refs):
            error = DomainError('TARGET_UNAVAILABLE', 403)
            error.export_targets = ('record', refs)
            raise error
        prefetch_related_objects(rows, 'clinicalrevision_set', 'medicationentry_set',
                                 'suggestion_decisions__suggestion')
        template_rows = Candidate.objects.in_bulk({row.template_id for row in rows})
        templates, field_meanings, encoded_field_meanings = {}, {}, {}
        target_refs = []

        def export_rows():
            for record in rows:
                encounter = record.encounter
                encounter.clinicalrecord = record
                record.template = template_rows[record.template_id]
                try:
                    clinical = _clinical(encounter, templates, field_meanings)
                except (KeyError, AttributeError, TypeError, UnicodeError):
                    logger.exception('Clinical export generation failed')
                    raise DomainError('EXPORT_GENERATION_UNAVAILABLE', 503) from None
                if clinical is None:
                    raise DomainError('RECORD_UNAVAILABLE', 503)
                encode = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                encoded_meanings = encoded_field_meanings.get(record.template_id)
                if encoded_meanings is None:
                    encoded_meanings = encode(clinical['field_meanings'])
                    encoded_field_meanings[record.template_id] = encoded_meanings
                target_refs.append(str(record.pk))
                yield {'record_ref': str(record.pk), 'encounter_ref': str(encounter.pk),
                       'template_version_ref': clinical['template_version_ref'],
                       'field_meanings': encoded_meanings, 'diagnosis': clinical['diagnosis'],
                       'examination': clinical['examination'], 'extra_fields': encode(clinical['extra_fields']),
                       'adopted_content': encode(clinical['adoptions']),
                       'responsibility_metadata': encode({'doctor_ref': encounter.doctor_id,
                                                          'receptionist_ref': encounter.receptionist_id,
                                                          'created_at': encounter.created_at.isoformat()}),
                       'medication_chain': encode(clinical['medication_chain']),
                       'clinical_versions': encode(clinical['clinical_versions']),
                       'error_mark': 'true' if encounter.error_mark else 'false',
                       'related_encounter_refs': encode([str(encounter.rebuilt_id)] if encounter.rebuilt_id else [])}

        body = _csv(CLINICAL_COLUMNS, export_rows())
        entry = ExportRequest.objects.create(store=actor.store, actor=actor, permission=CLINICAL,
                                              scope=scope, purpose=payload['purpose'], record_count=len(target_refs),
                                              row_count=len(target_refs), byte_count=len(body))
        event = write_audit(actor, CLINICAL, 'success', 'COMPLETED', entry.pk, actor.store_id)
        AuditTarget.objects.bulk_create([AuditTarget(event=event, target=ref, kind='record')
                                         for ref in target_refs])
    return body, entry.pk, len(target_refs)


@protected(CLINICAL, ['POST'], database_preflight=False)
@csrf_protect
def clinical_records(request):
    try:
        body, ref, count = clinical(request.user.pk, _body(request))
        return _response(body, ref, count, 'clinical-records')
    except DomainError as error:
        error.event_store = request.user.store_id
        if hasattr(error, 'export_targets'):
            _refusal(request.user, CLINICAL, error)
        raise


def guard(actor_id, permission, payload):
    with transaction.atomic():
        actor = require(actor_id, permission)
        allowed = ['scope', 'time_range', 'rule_ref', 'handling_state'] if permission == DETAIL else [
            'time_range', 'rule_ref', 'handling_state', 'group_by']
        _object(payload, allowed)
        hits = GuardHit.objects.filter(evaluation__encounter__store_id=actor.store_id)
        if permission == DETAIL:
            hits = hits.filter(evaluation__encounter__doctor_id=actor.pk)
            if 'scope' in payload:
                scope = payload['scope']
                _object(scope, ['encounter_refs'], ['encounter_refs'])
                refs = scope['encounter_refs']
                if type(refs) is not list or not refs or any(type(ref) is not str for ref in refs):
                    raise DomainError('INPUT_INVALID', 422)
                refs = [_uuid(ref) for ref in refs]
                if len(set(refs)) != len(refs):
                    raise DomainError('INPUT_INVALID', 422)
                found = Encounter.objects.filter(pk__in=refs, store_id=actor.store_id, doctor_id=actor.pk).count()
                if found != len(refs):
                    error = DomainError('TARGET_UNAVAILABLE', 403)
                    error.export_targets = ('encounter', refs)
                    raise error
                hits = hits.filter(evaluation__encounter_id__in=refs)
        if 'time_range' in payload:
            start, end = _range(payload['time_range'])
            hits = hits.filter(evaluation__created_at__gte=start, evaluation__created_at__lt=end)
        if 'rule_ref' in payload:
            _text(payload['rule_ref'], 100)
            hits = hits.filter(rule_ref=payload['rule_ref'])
        if 'handling_state' in payload:
            if payload['handling_state'] not in ['unhandled', 'handled']:
                raise DomainError('INPUT_INVALID', 422)
            hits = hits.filter(alert__handled_at__isnull=payload['handling_state'] == 'unhandled')
        if permission == DETAIL:
            selected = hits.select_related('evaluation', 'alert').order_by('evaluation__created_at', 'pk')
            rows = []
            for hit in selected:
                try:
                    alert = hit.alert
                except type(hit).alert.RelatedObjectDoesNotExist:
                    alert = None
                rows.append({'event_ref': str(hit.pk), 'record_ref': str(hit.evaluation.record_id) if hit.evaluation.record_id else '',
                             'rule_ref': hit.rule_ref, 'hit_field': hit.field,
                             'handling_state': 'handled' if alert and alert.handled_at else 'unhandled',
                             'handler_ref': alert.handler_id if alert and alert.handler_id else '',
                             'handled_at': alert.handled_at.isoformat() if alert and alert.handled_at else '',
                             'handling_result': alert.handling_result if alert and alert.handled_at else ''})
            columns = DETAIL_COLUMNS
        else:
            group_by = payload.get('group_by', 'rule')
            if group_by not in ['rule', 'handling_state']:
                raise DomainError('INPUT_INVALID', 422)
            group_rows = (list(hits.values('rule_ref').annotate(
                hit_count=Count('pk'), alert_count=Count('alert'),
                unhandled_count=Count('alert', filter=Q(alert__handled_at__isnull=True))).order_by('rule_ref'))
                if group_by == 'rule' else
                [])
            filters = {'time_range': json.dumps(payload.get('time_range'), ensure_ascii=False, sort_keys=True) if 'time_range' in payload else '',
                       'rule_ref_filter': payload.get('rule_ref', ''), 'handling_state_filter': payload.get('handling_state', ''),
                       'group_by': group_by}
            rows = []
            for group in group_rows:
                if group_by == 'rule':
                    rows.append({**filters, 'rule_ref': group['rule_ref'], 'handling_state': '',
                                 'hit_count': group['hit_count'], 'alert_count': group['alert_count'],
                                 'unhandled_count': group['unhandled_count']})
            if group_by == 'handling_state':
                totals = hits.aggregate(hit_count=Count('pk'), alert_count=Count('alert'),
                                        unhandled_count=Count('alert', filter=Q(alert__handled_at__isnull=True)),
                                        unhandled_hits=Count('pk', filter=Q(alert__handled_at__isnull=True)))
                for state, hit_count, alert_count in [
                    ('unhandled', totals['unhandled_hits'], totals['unhandled_count']),
                    ('handled', totals['hit_count'] - totals['unhandled_hits'],
                     totals['alert_count'] - totals['unhandled_count'])]:
                    if hit_count:
                        rows.append({**filters, 'rule_ref': '', 'handling_state': state,
                                     'hit_count': hit_count, 'alert_count': alert_count,
                                     'unhandled_count': alert_count if state == 'unhandled' else 0})
            columns = SUMMARY_COLUMNS
        body = _csv(columns, rows)
        entry = ExportRequest.objects.create(store=actor.store, actor=actor, permission=permission,
                                              scope=payload, record_count=sum(int(row.get('hit_count', 1)) for row in rows),
                                              row_count=len(rows), byte_count=len(body))
        event = write_audit(actor, permission, 'success', 'COMPLETED', entry.pk, actor.store_id)
        if permission == DETAIL:
            AuditTarget.objects.bulk_create([AuditTarget(event=event, target=record_ref, kind='record')
                                             for record_ref in {row['record_ref'] for row in rows if row['record_ref']}])
    return body, entry.pk, entry.record_count


@protected(DETAIL, ['POST'], database_preflight=False)
@csrf_protect
def alert_hits(request):
    try:
        body, ref, count = guard(request.user.pk, DETAIL, _body(request))
        return _response(body, ref, count, 'alert-hits')
    except DomainError as error:
        error.event_store = request.user.store_id
        if hasattr(error, 'export_targets'):
            _refusal(request.user, DETAIL, error)
        raise


@protected(SUMMARY, ['POST'], database_preflight=False)
@csrf_protect
def alert_summary(request):
    try:
        body, ref, count = guard(request.user.pk, SUMMARY, _body(request))
        return _response(body, ref, count, 'alert-summary')
    except DomainError as error:
        error.event_store = request.user.store_id
        raise
