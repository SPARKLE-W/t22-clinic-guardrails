"""Store-scoped counts only; never load clinical bodies for reporting."""
import json
from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from governance.services import DomainError, require, write_audit
from governance.views import protected
from .models import Encounter, GuardHit
from .services import _object

PERMISSION = 'report.store_summary.read'


def summary(actor_id, query):
    with transaction.atomic():
        actor = require(actor_id, PERMISSION, lock=True)
        _object(query, ['time_range', 'group_by'])
        group_by = query.get('group_by', 'rule')
        if group_by not in ['rule', 'handling_state']:
            raise DomainError('INPUT_INVALID', 422)
        interval = query.get('time_range')
        # Store restriction precedes aggregation; no caller-supplied store/ref.
        encounters = Encounter.objects.filter(store_id=actor.store_id)
        hits = GuardHit.objects.filter(evaluation__encounter__store_id=actor.store_id)
        if 'time_range' in query:
            _object(interval, ['from', 'to'], ['from', 'to'])
            try:
                start, end = [parse_datetime(interval[key]) for key in ['from', 'to']]
                if start is None or end is None or timezone.is_naive(start) or timezone.is_naive(end) or start >= end:
                    raise ValueError()
            except (TypeError, ValueError):
                raise DomainError('INPUT_INVALID', 422) from None
            encounters = encounters.filter(created_at__gte=start, created_at__lt=end)
            hits = hits.filter(evaluation__created_at__gte=start, evaluation__created_at__lt=end)
        counts = encounters.aggregate(encounter_created_count=Count('pk'),
                                      error_marked_count=Count('pk', filter=Q(error_mark=True)))
        rules = list(hits.values('rule_ref').annotate(
            hit_count=Count('pk'), alert_count=Count('alert'),
            unhandled_count=Count('alert', filter=Q(alert__handled_at__isnull=True))).order_by('rule_ref'))
        totals = {key: sum(row[key] for row in rules) for key in ['hit_count', 'alert_count', 'unhandled_count']}
        groups = rules if group_by == 'rule' else [
            {'handling_state': 'unhandled', 'alert_count': totals['unhandled_count']},
            {'handling_state': 'handled', 'alert_count': totals['alert_count'] - totals['unhandled_count']}]
        write_audit(actor, PERMISSION, 'success', 'COMPLETED', store_id=actor.store_id)
        return {'time_range': interval, **counts, **totals,
                'grouped_counts': {'group_by': group_by, 'items': groups}}


@protected(PERMISSION, ['GET'])
def store_summary(request):
    try:
        if any(len(request.GET.getlist(key)) != 1 for key in request.GET):
            raise DomainError('INPUT_INVALID', 422)
        query = request.GET.dict()
        if 'time_range' in query:
            try:
                query['time_range'] = json.loads(query['time_range'])
            except (ValueError, TypeError, RecursionError):
                raise DomainError('INPUT_INVALID', 422) from None
        return JsonResponse(summary(request.user.pk, query))
    except DomainError as error:
        error.event_store = request.user.store_id
        raise
