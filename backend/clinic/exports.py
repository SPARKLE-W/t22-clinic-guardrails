"""Generate complete authorized CSV before writing the required export audit."""
import csv
import hashlib
import io
import json
import logging
from collections import defaultdict
from time import perf_counter

import orjson
from django.db import DatabaseError, connection, transaction
from django.db.models import CharField, Count, Q
from django.db.models.functions import Cast
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
from .export_projection import validated_prefix as _projection_prefix

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
_RECORD_FIELDS = ('id', 'encounter_id', 'template_id', 'current_version', 'export_generation',
                  'encounter__store_id', 'encounter__template_id', 'encounter__created_at',
                  'encounter__doctor_id', 'encounter__receptionist_id', 'encounter__error_mark',
                  'encounter__rebuilt_id')


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
        output = io.BytesIO()
        text = io.TextIOWrapper(output, encoding='utf-8', newline='', write_through=True)
        writer = csv.DictWriter(text, fieldnames=columns, lineterminator='\r\n')
        writer.writeheader()
        writer.writerows(rows)
        text.flush()
        return output.getvalue()
    except (csv.Error, UnicodeError, TypeError, ValueError):
        logger.exception('CSV export generation failed')
        raise DomainError('EXPORT_GENERATION_UNAVAILABLE', 503) from None


def _csv_values(columns, rows):
    try:
        output = io.BytesIO()
        text = io.TextIOWrapper(output, encoding='utf-8', newline='', write_through=True)
        writer = csv.writer(text, lineterminator='\r\n')
        writer.writerow(columns)
        for row in rows:
            if isinstance(row, bytes):
                text.flush()
                output.write(row)
            else:
                writer.writerow(row)
        text.flush()
        return output.getvalue()
    except (csv.Error, UnicodeError, TypeError, ValueError):
        logger.exception('CSV export generation failed')
        raise DomainError('EXPORT_GENERATION_UNAVAILABLE', 503) from None


def _csv_line(values):
    output = io.StringIO(newline='')
    csv.writer(output, lineterminator='\r\n').writerow(values)
    return output.getvalue().encode('utf-8')


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


def _audit_clinical_targets(event, refs):
    if not refs:
        return
    if connection.vendor != 'mysql':
        AuditTarget.objects.bulk_create([AuditTarget(event=event, target=ref, kind='record') for ref in refs])
        return
    # The IDs came from the authorized export snapshot; keep one target per row.
    quote = connection.ops.quote_name
    record_field = ClinicalRecord._meta.pk
    event_id = AuditTarget._meta.get_field('event').target_field.get_db_prep_value(event.pk, connection)
    targets = [record_field.get_db_prep_value(ref, connection) for ref in refs]
    markers = ','.join(['%s'] * len(targets))
    with connection.cursor() as cursor:
        cursor.execute(
            f'INSERT INTO {quote(AuditTarget._meta.db_table)} (event_id, target, kind) '
            f'SELECT %s, id, %s FROM {quote(ClinicalRecord._meta.db_table)} WHERE id IN ({markers})',
            [event_id, 'record', *targets])
        if cursor.rowcount != len(targets):
            raise DatabaseError('Clinical export audit target count mismatch')


def _detail_values(queryset, fields, uuid_fields):
    """Read export-only identifiers as text, avoiding UUID object round trips."""
    aliases = {'export_text_' + field: Cast(field, CharField(max_length=36))
               for field in uuid_fields}
    for row in queryset.annotate(**aliases).values(*fields, *aliases):
        for field in uuid_fields:
            value = row.pop('export_text_' + field)
            if value is not None and len(value) == 32:
                value = f'{value[:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:]}'
            row[field] = value
        yield row


def _record_values(queryset):
    # Keep the primary UUID for audit binding; metadata identifiers only need
    # canonical text for signatures and CSV, not UUID object round trips.
    textual = ('encounter_id', 'template_id', 'encounter__store_id',
               'encounter__template_id', 'encounter__rebuilt_id')
    aliases = {'export_record_' + field.replace('__', '_'): Cast(field, CharField(max_length=36))
               for field in textual}
    rows = list(queryset.annotate(**aliases).values(
        *(field for field in _RECORD_FIELDS if field not in textual), *aliases,
        'export_projection__source_signature', 'export_projection__prefix',
        'export_projection__prefix_digest').order_by('encounter__created_at', 'pk'))
    for row in rows:
        for field in textual:
            value = row.pop('export_record_' + field.replace('__', '_'))
            if value is not None:
                value = value.lower()
                if len(value) == 32:
                    value = f'{value[:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:]}'
            row[field] = value
    return rows


def _load_histories(record_ids):
    revisions, medications, adoptions = defaultdict(list), defaultdict(list), defaultdict(list)
    for item in _detail_values(
            ClinicalRevision.objects.filter(record_id__in=record_ids).order_by('record_id', 'version'),
            ['version', 'diagnosis', 'examination', 'extra_fields', 'actor_id', 'created_at', 'reason'],
            ['id', 'record_id']):
        revisions[item.pop('record_id')].append(item)
    for item in _detail_values(
            MedicationEntry.objects.filter(record_id__in=record_ids).order_by('record_id', 'created_at', 'id'),
            ['drug', 'amount', 'unit', 'actor_id', 'created_at', 'reason'],
            ['id', 'record_id', 'original_id']):
        medications[item.pop('record_id')].append(item)
    for item in _detail_values(
            SuggestionDecision.objects.filter(record_id__in=record_ids, outcome='adopted').order_by(
                'record_id', 'created_at', 'id'),
            ['outcome', 'adopted_content', 'actor_id', 'created_at', 'reason', 'suggestion__source'],
            ['id', 'record_id', 'suggestion_id', 'version_id', 'medication_id']):
        item['source'] = item.pop('suggestion__source')
        adoptions[item.pop('record_id')].append(item)
    return revisions, medications, adoptions


def _clinical_rows(rows, templates, histories):
    revisions, medications, adoptions = histories
    field_meanings, encoded_field_meanings = {}, {}
    encode = _json_text
    for row in rows:
        if row.get('_projection_prefix') is not None:
            yield row['_projection_prefix'] + b',' + _csv_line([
                'true' if row['encounter__error_mark'] else 'false',
                _json_text([str(row['encounter__rebuilt_id'])] if row['encounter__rebuilt_id'] else [])])
            continue
        record_id = row['id']
        record_key = str(record_id)
        template_id = row['template_id']
        try:
            template = templates[template_id]
            meanings = field_meanings.get(template_id)
            if meanings is None:
                clinical_template = template['clinical']
                meanings = {**clinical_template['field_types'],
                            **{item['id']: item['type'] for item in clinical_template['extra_fields']}}
                field_meanings[template_id] = meanings
            current = next((revision for revision in revisions[record_key]
                            if revision['version'] == row['current_version']), None)
            if not current or not current['diagnosis'].strip():
                raise DomainError('RECORD_UNAVAILABLE', 503)
            revision_rows = revisions[record_key]
            medication_rows = medications[record_key]
            adoption_rows = adoptions[record_key]
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
        yield (str(record_id), str(encounter_id), template['version'], encoded_meanings,
               current['diagnosis'], current['examination'], encode(current['extra_fields']),
               encode(adopted_content),
               encode({'doctor_ref': row['encounter__doctor_id'],
                       'receptionist_ref': row['encounter__receptionist_id'],
                       'created_at': row['encounter__created_at'].isoformat()}),
               encode(medication_chain), encode(clinical_versions),
               'true' if row['encounter__error_mark'] else 'false',
               encode([str(row['encounter__rebuilt_id'])] if row['encounter__rebuilt_id'] else []))



def clinical(actor_id, payload, timings=None):
    started = perf_counter() if timings is not None else None
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
        rows = _record_values(rows)
        if 'record_refs' in scope and len(rows) != len(refs):
            error = DomainError('TARGET_UNAVAILABLE', 403)
            error.export_targets = ('record', refs)
            raise error
        for row in rows:
            row['template_id'] = str(row['template_id'])
        template_rows = {str(key): value for key, value in Candidate.objects.only(
            'id', 'store_id', 'envelope', 'digest').in_bulk({row['template_id'] for row in rows}).items()}
        templates = {}
        for row in rows:
            template_id = row['template_id']
            if template_id not in templates:
                try:
                    templates[template_id] = _template(template_rows[template_id], actor.store_id)
                except (KeyError, AttributeError, TypeError, UnicodeError):
                    logger.exception('Clinical export generation failed')
                    raise DomainError('EXPORT_GENERATION_UNAVAILABLE', 503) from None
        for row in rows:
            row['_projection_prefix'] = _projection_prefix(row, template_rows[row['template_id']].digest)
        histories = _load_histories([row['id'] for row in rows if row['_projection_prefix'] is None])
        store_id = actor.store_id
    if timings is not None:
        timings['export-snapshot'] = (perf_counter() - started) * 1000
    target_refs = [row['id'] for row in rows]
    started = perf_counter() if timings is not None else None
    body = _csv_values(CLINICAL_COLUMNS, _clinical_rows(rows, templates, histories))
    if timings is not None:
        timings['export-csv'] = (perf_counter() - started) * 1000
    with transaction.atomic():
        actor = require(actor_id, CLINICAL, lock=True)
        if actor.store_id != store_id:
            raise DomainError('TARGET_UNAVAILABLE', 403)
        entry = ExportRequest.objects.create(store=actor.store, actor=actor, permission=CLINICAL,
                                              scope=scope, purpose=payload['purpose'], record_count=len(target_refs),
                                              row_count=len(target_refs), byte_count=len(body))
        event = write_audit(actor, CLINICAL, 'success', 'COMPLETED', entry.pk, actor.store_id)
        started = perf_counter() if timings is not None else None
        _audit_clinical_targets(event, target_refs)
        if timings is not None:
            timings['export-audit-targets'] = (perf_counter() - started) * 1000
    return body, entry.pk, len(target_refs)


@protected(CLINICAL, ['POST'], database_preflight=False)
@csrf_protect
def clinical_records(request):
    try:
        body, ref, count = clinical(request.user.pk, _body(request), request.META.get('t22.export_timings'))
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
