"""Generate complete authorized CSV before writing the required export audit."""
import csv
import hashlib
import io
import json
import logging
from collections import defaultdict

import orjson
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_protect
from governance.models import Candidate
from governance.services import DomainError, _uuid, require, write_audit
from governance.views import _body, protected
from .models import (AuditTarget, ClinicalRecord, ClinicalRevision, Encounter, ExportRequest,
                     GuardHit, MedicationEntry, SuggestionDecision)
from .services import _object, _template, _text

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


def _csv_values(columns, rows):
    try:
        output = io.StringIO(newline='')
        writer = csv.writer(output, lineterminator='\r\n')
        writer.writerow(columns)
        writer.writerows(rows)
        return output.getvalue().encode('utf-8')
    except (csv.Error, UnicodeError, TypeError, ValueError):
        logger.exception('CSV export generation failed')
        raise DomainError('EXPORT_GENERATION_UNAVAILABLE', 503) from None


def _json_text(value):
    try:
        return orjson.dumps(value, option=orjson.OPT_SORT_KEYS).decode('utf-8')
    except orjson.JSONEncodeError:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


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
        rows = list(rows.values(
            'id', 'encounter_id', 'template_id', 'current_version', 'encounter__store_id',
            'encounter__template_id', 'encounter__created_at', 'encounter__doctor_id',
            'encounter__receptionist_id', 'encounter__error_mark', 'encounter__rebuilt_id',
        ).order_by('encounter__created_at', 'pk'))
        if 'record_refs' in scope and len(rows) != len(refs):
            error = DomainError('TARGET_UNAVAILABLE', 403)
            error.export_targets = ('record', refs)
            raise error
        template_rows = Candidate.objects.only('id', 'store_id', 'envelope', 'digest').in_bulk(
            {row['template_id'] for row in rows})
        record_ids = [row['id'] for row in rows]
        revisions, medications, adoptions = defaultdict(list), defaultdict(list), defaultdict(list)
        for item in ClinicalRevision.objects.filter(record_id__in=record_ids).values(
                'id', 'record_id', 'version', 'diagnosis', 'examination', 'extra_fields',
                'actor_id', 'created_at', 'reason').order_by('record_id', 'version'):
            revisions[item.pop('record_id')].append(item)
        for item in MedicationEntry.objects.filter(record_id__in=record_ids).values(
                'id', 'record_id', 'original_id', 'drug', 'amount', 'unit', 'actor_id',
                'created_at', 'reason').order_by('record_id', 'created_at', 'id'):
            medications[item.pop('record_id')].append(item)
        for item in SuggestionDecision.objects.filter(record_id__in=record_ids, outcome='adopted').values(
                'id', 'record_id', 'suggestion_id', 'outcome', 'adopted_content', 'actor_id',
                'created_at', 'reason', 'version_id', 'medication_id', 'suggestion__source').order_by(
                    'record_id', 'created_at', 'id'):
            item['source'] = item.pop('suggestion__source')
            adoptions[item.pop('record_id')].append(item)
        templates = {}
        for row in rows:
            template_id = row['template_id']
            if template_id not in templates:
                try:
                    templates[template_id] = _template(template_rows[template_id], actor.store_id)
                except (KeyError, AttributeError, TypeError, UnicodeError):
                    logger.exception('Clinical export generation failed')
                    raise DomainError('EXPORT_GENERATION_UNAVAILABLE', 503) from None
        store_id = actor.store_id
    field_meanings, encoded_field_meanings = {}, {}
    target_refs = []
    encode = _json_text

    def export_rows():
        for row in rows:
            record_id = row['id']
            template_id = row['template_id']
            try:
                template = templates[template_id]
                meanings = field_meanings.get(template_id)
                if meanings is None:
                    clinical_template = template['clinical']
                    meanings = {**clinical_template['field_types'],
                                **{item['id']: item['type'] for item in clinical_template['extra_fields']}}
                    field_meanings[template_id] = meanings
                current = next((revision for revision in revisions[record_id]
                                if revision['version'] == row['current_version']), None)
                if not current or not current['diagnosis'].strip():
                    raise DomainError('RECORD_UNAVAILABLE', 503)
                revision_rows = revisions[record_id]
                medication_rows = medications[record_id]
                adoption_rows = adoptions[record_id]
            except (KeyError, AttributeError, TypeError, UnicodeError):
                logger.exception('Clinical export generation failed')
                raise DomainError('EXPORT_GENERATION_UNAVAILABLE', 503) from None
            encoded_meanings = encoded_field_meanings.get(template_id)
            if encoded_meanings is None:
                encoded_meanings = encode(meanings)
                encoded_field_meanings[template_id] = encoded_meanings
            clinical_versions = [{'version_ref': str(item['id']), 'version': item['version'],
                                  'diagnosis': item['diagnosis'], 'examination': item['examination'],
                                  'extra_fields': item['extra_fields'], 'actor_ref': item['actor_id'],
                                  'created_at': item['created_at'].isoformat(), 'reason': item['reason']}
                                 for item in revision_rows]
            medication_chain = [{'entry_ref': str(item['id']),
                                 'original_entry_ref': str(item['original_id']) if item['original_id'] else None,
                                 'drug': item['drug'], 'amount': item['amount'], 'unit': item['unit'],
                                 'actor_ref': item['actor_id'], 'created_at': item['created_at'].isoformat(),
                                 'reason': item['reason']} for item in medication_rows]
            adopted_content = [{'decision_ref': str(item['id']), 'suggestion_ref': str(item['suggestion_id']),
                                'source': item['source'], 'adopted_content': item['adopted_content'],
                                'actor_ref': item['actor_id'], 'created_at': item['created_at'].isoformat(),
                                'reason': item['reason'], 'version_ref': str(item['version_id']),
                                'medication_ref': str(item['medication_id']) if item['medication_id'] else None}
                               for item in adoption_rows if item['outcome'] == 'adopted']
            encounter_id = row['encounter_id']
            target_refs.append(record_id)
            yield (str(record_id), str(encounter_id), template['version'], encoded_meanings,
                   current['diagnosis'], current['examination'], encode(current['extra_fields']),
                   encode(adopted_content),
                   encode({'doctor_ref': row['encounter__doctor_id'],
                           'receptionist_ref': row['encounter__receptionist_id'],
                           'created_at': row['encounter__created_at'].isoformat()}),
                   encode(medication_chain), encode(clinical_versions),
                   'true' if row['encounter__error_mark'] else 'false',
                   encode([str(row['encounter__rebuilt_id'])] if row['encounter__rebuilt_id'] else []))

    body = _csv_values(CLINICAL_COLUMNS, export_rows())
    with transaction.atomic():
        actor = require(actor_id, CLINICAL, lock=True)
        if actor.store_id != store_id:
            raise DomainError('TARGET_UNAVAILABLE', 403)
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
            selected = hits.order_by('evaluation__created_at', 'pk').values(
                'id', 'evaluation__record_id', 'rule_ref', 'field',
                'alert__handler_id', 'alert__handled_at', 'alert__handling_result')
            rows = []
            for hit in selected:
                handled_at = hit['alert__handled_at']
                record_id = hit['evaluation__record_id']
                rows.append({'event_ref': str(hit['id']), 'record_ref': str(record_id) if record_id else '',
                             'rule_ref': hit['rule_ref'], 'hit_field': hit['field'],
                             'handling_state': 'handled' if handled_at else 'unhandled',
                             'handler_ref': hit['alert__handler_id'] or '',
                             'handled_at': handled_at.isoformat() if handled_at else '',
                             'handling_result': hit['alert__handling_result'] if handled_at else ''})
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
        store_id = actor.store_id
    body = _csv(columns, rows)
    with transaction.atomic():
        actor = require(actor_id, permission, lock=True)
        if actor.store_id != store_id:
            raise DomainError('TARGET_UNAVAILABLE', 403)
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
