"""Versioned work allocation. Clinical evidence is selected as metadata only."""
import re
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect
from governance.models import Activation, Candidate, Operation, Review, Store
from governance.services import DomainError, _uuid, require, write_audit
from governance.views import _body, protected
from vetcontrols.compiler import _digest, _normalize
from .models import (Alert, ClinicalRevision, Encounter, MedicationEntry, ReceptionRevision,
                     Suggestion, SuggestionDecision, WorkDivision, WorkDivisionRevision, WorkDivisionReview)
from .services import _object, _text

WRITE = 'collaboration.assignment.write'
REVIEW = 'collaboration.assignment.review'
DIMENSIONS = {'content_generation': ['human', 'ai', 'demo', 'rule_engine', 'unknown'],
              'data_entry': ['frontdesk', 'doctor', 'manager'],
              'human_decision': ['doctor', 'manager']}
# Each source is a persisted successful business event, not an access-log entry.
# Role denotes the enforced role of that operation, not today's account role.
SOURCES = {
    'content_generation': (Suggestion, 'encounter_id', None, None, 'created_at', None),
    'clinical_entry': (ClinicalRevision, 'record__encounter_id', 'actor_id', 'doctor', 'created_at', None),
    'medication_entry': (MedicationEntry, 'record__encounter_id', 'actor_id', 'doctor', 'created_at', None),
    'reception_entry': (ReceptionRevision, 'encounter_id', 'actor_id', 'frontdesk', 'created_at', None),
    'suggestion_decision': (SuggestionDecision, 'suggestion__encounter_id', 'actor_id', 'doctor', 'created_at', 'outcome'),
    'rule_review': (Review, 'candidate_id', 'actor_id', 'manager', 'created_at', 'decision'),
    'rule_activation': (Activation, 'candidate_id', 'actor_id', 'manager', 'created_at', None),
    'alert_handling': (Alert, 'hit__evaluation__encounter_id', 'handler_id', 'doctor', 'handled_at', None),
}


def context(actor, payload):
    ref, stage = _uuid(payload.get('work_item_ref')), payload.get('stage_ref')
    if type(stage) is not str or stage not in SOURCES:
        raise DomainError('INPUT_INVALID', 422)
    model = Candidate if stage in ['rule_review', 'rule_activation'] else Encounter
    if not model.objects.filter(pk=ref, store_id=actor.store_id).exists():
        raise DomainError('TARGET_UNAVAILABLE', 403)
    return ref, stage


def evidence(ref, stage, selected=None):
    model, link, actor_field, role, time_field, result_field = SOURCES[stage]
    rows = model.objects.filter(**{link: ref})
    if stage == 'alert_handling':
        rows = rows.filter(handled_at__isnull=False, handler__isnull=False)
    if selected is not None:
        if type(selected) is not list or len(selected) > 100 or any(type(s) is not str for s in selected):
            raise DomainError('INPUT_INVALID', 422)
        refs = [_uuid(s) for s in selected]
        if len(set(refs)) != len(refs):
            raise DomainError('INPUT_INVALID', 422)
        rows = rows.filter(pk__in=refs)
    fields = ['pk', time_field]
    if actor_field:
        fields.append(actor_field)
    if result_field:
        fields.append(result_field)
    if stage == 'content_generation':
        fields += ['source', 'source_version']
    items = []
    for row in rows.order_by(time_field, 'pk').values(*fields):
        dimensions = {key: None for key in DIMENSIONS}
        if actor_field:
            dimensions['data_entry'] = role
            if stage in ['suggestion_decision', 'rule_review', 'rule_activation', 'alert_handling']:
                dimensions['human_decision'] = role
        if stage == 'content_generation':
            # This implementation has only a labelled demonstration source.
            # Unknown/injected type='ai' is not proof of a connected model.
            source = row['source']
            dimensions['content_generation'] = ('demo' if source.get('type') == 'demo'
                and source.get('version') == row['source_version'] and source.get('label') else None)
        items.append({'source_event_ref': str(row['pk']), 'stage_ref': stage,
                      'actor_ref': row[actor_field] if actor_field else None,
                      'actor_role': role, 'occurred_at': row[time_field].isoformat(),
                      'result': row[result_field] if result_field else 'completed', 'dimensions': dimensions})
    if selected is not None and len(items) != len(selected):
        raise DomainError('EVIDENCE_UNAVAILABLE', 422)
    return items


def compare(plan, actual):
    missing, differences = [], []
    for dimension, expected in plan.items():
        if expected is None:
            continue
        values = {item['dimensions'][dimension] for item in actual if item['dimensions'][dimension] not in [None, 'unknown']}
        if not values:
            missing.append(dimension)
        elif values != {expected}:
            differences.append({'dimension': dimension, 'planned': expected, 'actual': sorted(values)})
    return {'state': 'not_comparable' if missing else 'pending_review' if differences else 'consistent',
            'missing_dimensions': missing, 'differences': differences}


def serialize(division):
    revisions = list(division.revisions.order_by('version'))
    latest = revisions[-1]
    reviews = WorkDivisionReview.objects.filter(revision__division=division).select_related('revision').order_by('created_at', 'pk')
    return {'work_item_ref': str(division.work_item_ref), 'stage_ref': division.stage_ref,
            'plan_version_ref': str(latest.plan_version_ref), 'actual_version_ref': str(latest.actual_version_ref),
            **latest.snapshot,
            'maintenance_history': [dict(plan_version_ref=str(r.plan_version_ref), actual_version_ref=str(r.actual_version_ref),
                                        actor_ref=r.actor_id, created_at=r.created_at.isoformat(), reason=r.reason,
                                        **r.snapshot) for r in revisions],
            'review_history': [dict(plan_version_ref=str(r.revision.plan_version_ref), actual_version_ref=str(r.revision.actual_version_ref),
                                   actor_ref=r.actor_id, created_at=r.created_at.isoformat(), conclusion=r.conclusion, reason=r.reason) for r in reviews]}


def mutate(actor_id, payload, key, review=False):
    permission = REVIEW if review else WRITE
    with transaction.atomic():
        actor = require(actor_id, permission, lock=True)
        # Serialize two managers creating/updating the same store's registry.
        Store.objects.select_for_update().get(pk=actor.store_id)
        fields = ['work_item_ref', 'stage_ref', 'plan_version_ref', 'actual_version_ref', 'reason']
        fields += ['redacted_evidence', 'conclusion'] if review else ['plan_dimensions', 'source_event_refs', 'manual_actual_evidence']
        _object(payload, fields, fields)
        _text(payload['reason'], 2000)
        key = _uuid(key)
        ref, stage = context(actor, payload)
        digest = _digest(_normalize({'store': str(actor.store_id), 'payload': payload}))
        previous = Operation.objects.filter(actor=actor, permission=permission, key=key).first()
        if previous:
            if previous.digest != digest:
                raise DomainError('OPERATION_CONTENT_CONFLICT')
            return previous.result
        division = WorkDivision.objects.filter(store=actor.store, work_item_ref=ref, stage_ref=stage).first()
        latest = division.revisions.order_by('-version').first() if division else None
        for name in ['plan_version_ref', 'actual_version_ref']:
            if payload[name] != (str(getattr(latest, name)) if latest else None):
                raise DomainError('WORK_VERSION_CONFLICT')
        if review:
            selected = payload['redacted_evidence']
            if type(selected) is not list or len(selected) > 100 or any(type(s) is not str for s in selected):
                raise DomainError('INPUT_INVALID', 422)
            refs = [_uuid(s) for s in selected]
            if len(set(refs)) != len(refs):
                raise DomainError('INPUT_INVALID', 422)
            if not latest or latest.snapshot['comparison']['state'] == 'not_comparable':
                raise DomainError('EVIDENCE_INSUFFICIENT')
            if selected != latest.snapshot['source_event_refs']:
                raise DomainError('EVIDENCE_VERSION_CONFLICT')
            if evidence(ref, stage, selected) != latest.snapshot['actual_execution_metadata']:
                raise DomainError('EVIDENCE_VERSION_CONFLICT')
            if payload['conclusion'] not in ['confirmed_consistent', 'confirmed_difference', 'needs_followup']:
                raise DomainError('INPUT_INVALID', 422)
            if payload['conclusion'] == 'confirmed_consistent' and latest.snapshot['comparison']['state'] != 'consistent':
                raise DomainError('REVIEW_CONCLUSION_CONFLICT')
            if payload['conclusion'] == 'confirmed_difference' and latest.snapshot['comparison']['state'] != 'pending_review':
                raise DomainError('REVIEW_CONCLUSION_CONFLICT')
            WorkDivisionReview.objects.create(revision=latest, actor=actor, conclusion=payload['conclusion'], reason=payload['reason'])
        else:
            plan = payload['plan_dimensions']
            _object(plan, DIMENSIONS, DIMENSIONS)
            if not any(value is not None for value in plan.values()):
                raise DomainError('INPUT_INVALID', 422)
            for dimension, value in plan.items():
                if value is not None and value not in DIMENSIONS[dimension]:
                    raise DomainError('INPUT_INVALID', 422)
            manual = payload['manual_actual_evidence']
            if type(manual) is not list or len(manual) > 30:
                raise DomainError('INPUT_INVALID', 422)
            provenance = []
            for item in manual:
                _object(item, ['dimension', 'value', 'source_ref'], ['dimension', 'value', 'source_ref'])
                if type(item['dimension']) is not str or item['dimension'] not in DIMENSIONS or item['value'] not in DIMENSIONS[item['dimension']]:
                    raise DomainError('INPUT_INVALID', 422)
                _text(item['source_ref'], 200)
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}', item['source_ref']):
                    raise DomainError('INPUT_INVALID', 422)
                provenance.append({**item, 'kind': 'manager_declaration', 'recorded_by': actor.pk})
            actual = evidence(ref, stage, payload['source_event_refs'])
            snapshot = {'plan_dimensions': plan, 'source_event_refs': [item['source_event_ref'] for item in actual],
                        'actual_execution_metadata': actual, 'manual_evidence_provenance': provenance,
                        'comparison': compare(plan, actual)}
            if not division:
                division = WorkDivision.objects.create(store=actor.store, work_item_ref=ref, stage_ref=stage)
            division.current_version += 1
            division.save(update_fields=['current_version'])
            WorkDivisionRevision.objects.create(division=division, version=division.current_version, actor=actor,
                                                reason=payload['reason'], snapshot=snapshot)
        write_audit(actor, permission, 'success', 'COMPLETED', division.pk, actor.store_id)
        result = serialize(division)
        Operation.objects.create(actor=actor, permission=permission, key=key, digest=digest, result=result)
        return result


def query(request):
    if any(len(request.GET.getlist(key)) != 1 for key in request.GET):
        raise DomainError('INPUT_INVALID', 422)
    return request.GET.dict()


@protected(WRITE, ['GET', 'POST'], database_preflight=False)
@csrf_protect
def registry(request):
    try:
        if request.method == 'POST':
            if request.GET:
                raise DomainError('INPUT_FIELDS_INVALID', 422)
            return JsonResponse(mutate(request.user.pk, _body(request), request.headers.get('Idempotency-Key')))
        with transaction.atomic():
            actor = require(request.user.pk, WRITE, lock=True)
            args = query(request)
            _object(args, ['work_item_ref', 'stage_ref', 'plan_version_ref', 'actual_version_ref', 'comparison_state', 'page'])
            rows = WorkDivision.objects.filter(store=actor.store).order_by('id')
            if 'work_item_ref' in args:
                rows = rows.filter(work_item_ref=_uuid(args['work_item_ref']))
            if 'stage_ref' in args:
                if args['stage_ref'] not in SOURCES:
                    raise DomainError('INPUT_INVALID', 422)
                rows = rows.filter(stage_ref=args['stage_ref'])
            # Filters refer to the current comparison, never a stale history row.
            from django.db.models import F
            rows = rows.filter(revisions__version=F('current_version'))
            for name in ['plan_version_ref', 'actual_version_ref']:
                if name in args:
                    rows = rows.filter(**{'revisions__'+name: _uuid(args[name]), 'revisions__version': F('current_version')})
            if 'comparison_state' in args:
                if args['comparison_state'] not in ['consistent', 'pending_review', 'not_comparable']:
                    raise DomainError('INPUT_INVALID', 422)
                rows = rows.filter(revisions__snapshot__comparison__state=args['comparison_state'], revisions__version=F('current_version'))
            page = args.get('page', '1')
            if not page.isascii() or not page.isdigit() or len(page) > 8 or int(page) < 1:
                raise DomainError('INPUT_INVALID', 422)
            page = int(page)
            total = rows.count()
            items = [serialize(row) for row in rows[(page-1)*50:page*50]]
            write_audit(actor, WRITE, 'success', 'COMPLETED', store_id=actor.store_id)
            return JsonResponse({'items': items, 'page': page, 'page_size': 50, 'total': total})
    except DomainError as error:
        error.event_store = request.user.store_id
        raise


@protected(REVIEW, ['POST'], database_preflight=False)
@csrf_protect
def review_registry(request):
    try:
        if request.GET:
            raise DomainError('INPUT_FIELDS_INVALID', 422)
        return JsonResponse(mutate(request.user.pk, _body(request), request.headers.get('Idempotency-Key'), review=True))
    except DomainError as error:
        error.event_store = request.user.store_id
        raise


@protected(WRITE, ['GET'], database_preflight=False)
def available_evidence(request):
    try:
        with transaction.atomic():
            actor = require(request.user.pk, WRITE, lock=True)
            args = query(request)
            _object(args, ['work_item_ref', 'stage_ref'], ['work_item_ref', 'stage_ref'])
            ref, stage = context(actor, args)
            items = evidence(ref, stage)
            write_audit(actor, WRITE, 'success', 'COMPLETED', store_id=actor.store_id)
            return JsonResponse({'work_item_ref': str(ref), 'stage_ref': stage,
                                 'source_event_refs': [item['source_event_ref'] for item in items], 'actual_execution_metadata': items})
    except DomainError as error:
        error.event_store = request.user.store_id
        raise
