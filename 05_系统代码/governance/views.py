import functools
import json
import mimetypes
from pathlib import Path

from django.contrib.auth import authenticate, login, logout
from django.db import DatabaseError
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from vetcontrols.compiler import parse_text
from .models import AuditEvent, Candidate, PolicyHead
from .services import ACTIVATE, GENERATE, REVIEW, ROLES, DomainError, detail, mutate, require, write_audit

FRONTEND_ROOT = Path(__file__).resolve().parent.parent / 'frontend'


def error(code, status):
    return JsonResponse({'ok': False, 'error': {'code': code}}, status=status)


def _body(request):
    try:
        value = parse_text(request.body)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise DomainError('INPUT_INVALID', 422) from None
    if type(value) is not dict:
        raise DomainError('INPUT_INVALID', 422)
    return value


def protected(permission, methods, database_preflight=True):
    """Skip the entry DB read only when the handler rechecks via its service."""
    def wrap(view):
        @functools.wraps(view)
        def call(request, *args, **kwargs):
            try:
                if request.method not in methods:
                    return error('METHOD_NOT_ALLOWED', 405)
                if not request.user.is_authenticated:
                    raise DomainError('AUTH_REQUIRED', 401)
                if database_preflight:
                    actor = require(request.user.pk, permission)
                else:
                    actor = request.user
                    if not actor.is_active:
                        raise DomainError('AUTH_REQUIRED', 401)
                    if permission not in ROLES or actor.role != ROLES[permission]:
                        raise DomainError('PERMISSION_DENIED', 403)
                request.authorized_actor = actor
                return view(request, *args, **kwargs)
            except DomainError as exc:
                try:
                    if not exc.audit_recorded:
                        target = kwargs.get('candidate_id') or kwargs.get('encounter_id')
                        alert_ref = kwargs.get('alert_id') or (request.GET.get('alert_ref') if permission == 'clinical.alert.handle' else None)
                        record_ref = request.GET.get('record_ref') if permission == 'clinical.alert.handle' else None
                        if kwargs.get('suggestion_id') and request.user.is_authenticated:
                            from clinic.suggestions import refusal
                            refusal(request.user.pk,exc,str(kwargs['suggestion_id']),permission)
                        elif (alert_ref or record_ref) and request.user.is_authenticated:
                            from clinic.writes import alert_refusal
                            alert_refusal(request.user.pk, exc, str(alert_ref) if alert_ref else None, permission, record_ref)
                        elif kwargs.get('encounter_id') and request.user.is_authenticated:
                            from clinic.services import _refusal
                            _refusal(request.user.pk, permission, exc, str(kwargs['encounter_id']))
                        else:
                            event_store = exc.event_store
                            if event_store is None and request.user.is_authenticated:
                                event_store = request.user.store_id
                            write_audit(request.user if request.user.is_authenticated else None, permission, 'request_refused', exc.code,
                                        target=target, store_id=event_store)
                except DatabaseError:
                    return error('AUDIT_UNAVAILABLE', 503)
                return error(exc.code, exc.status)
            except DatabaseError:
                return error('PERSISTENCE_UNCONFIRMED', 503)
        call.permission_key = permission
        call.central_entry = True
        return call
    return wrap


@ensure_csrf_cookie
def home(request):
    return render(request, 'governance/home.html')


@ensure_csrf_cookie
def console(request):
    """Serve the same-origin SPA shell and initialise the CSRF cookie."""
    if request.method != 'GET':
        return error('METHOD_NOT_ALLOWED', 405)
    index = FRONTEND_ROOT / 'index.html'
    if not index.is_file():
        raise Http404()
    response = FileResponse(open(index, 'rb'), content_type='text/html; charset=utf-8')
    response['Cache-Control'] = 'no-store'
    return response


def frontend_asset(request, path):
    """Serve only files under frontend/assets; never expose the rest of the tree."""
    if request.method != 'GET':
        return error('METHOD_NOT_ALLOWED', 405)
    base = (FRONTEND_ROOT / 'assets').resolve()
    target = (base / path).resolve()
    if base not in target.parents or not target.is_file():
        raise Http404()
    content_type = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
    response = FileResponse(open(target, 'rb'), content_type=content_type)
    response['Cache-Control'] = 'no-cache'
    return response


def session_info(request):
    """Minimal current-account identity; no clinical content and no extra scopes."""
    if request.method != 'GET':
        return error('METHOD_NOT_ALLOWED', 405)
    if not request.user.is_authenticated or not request.user.is_active:
        return JsonResponse({'authenticated': False})
    return JsonResponse({'authenticated': True, 'username': request.user.username, 'role': request.user.role,
                         'store_ref': str(request.user.store_id), 'store_name': request.user.store.name})


@csrf_protect
def signin(request):
    if request.method != 'POST':
        return error('METHOD_NOT_ALLOWED', 405)
    try:
        values = _body(request) if request.content_type == 'application/json' else request.POST.dict()
        if set(values) != {'username', 'password'} or any(type(v) is not str or not v or len(v) > 256 for v in values.values()):
            return error('INPUT_INVALID', 422)
        user = authenticate(request, username=values['username'], password=values['password'])
        if user is None:
            write_audit(None, 'identity.login', 'refused', 'INVALID_CREDENTIALS')
            return error('INVALID_CREDENTIALS', 401)
        if user.role not in ['manager', 'doctor', 'frontdesk']:
            return error('PERMISSION_DENIED', 403)
        write_audit(user, 'identity.login', 'success', 'COMPLETED', store_id=user.store_id)
        login(request, user)
        return JsonResponse({'ok': True, 'username': user.username, 'role': user.role, 'store_ref': str(user.store_id)})
    except DomainError as exc:
        return error(exc.code, exc.status)
    except DatabaseError:
        return error('AUTH_FACILITY_UNAVAILABLE', 503)


@csrf_protect
def signout(request):
    if request.method != 'POST':
        return error('METHOD_NOT_ALLOWED', 405)
    logout(request)
    return JsonResponse({'ok': True})


@protected(REVIEW, ['GET'])
def candidates(request):
    if request.GET:
        raise DomainError('INPUT_FIELDS_INVALID', 422)
    actor = request.authorized_actor
    head = PolicyHead.objects.get(store=actor.store)
    rows = Candidate.objects.filter(store=actor.store).order_by('-demand__created_at')[:100]
    return JsonResponse({'ok': True, 'generation': head.generation, 'active_candidate_ref': str(head.candidate_id) if head.candidate_id else None,
                         'items': [{'candidate_ref': str(row.pk), 'content_version_ref': row.digest, 'state': row.state,
                                    'version': row.envelope['package_version'], 'created_at': row.demand.created_at.isoformat()} for row in rows]})


@protected(REVIEW, ['GET'], database_preflight=False)
def candidate_detail(request, candidate_id):
    if request.GET:
        raise DomainError('INPUT_FIELDS_INVALID', 422)
    return JsonResponse({'ok': True, **detail(request.user.pk, str(candidate_id))})


@protected(GENERATE, ['GET'])
def example(request):
    if request.GET:
        raise DomainError('INPUT_FIELDS_INVALID', 422)
    definition = json.loads((Path(__file__).parents[1] / 'examples/01_完整定义需求_v0.2.json').read_text(encoding='utf-8'))
    definition['scope']['store_id'] = str(request.user.store_id)
    return JsonResponse({'definition': definition})


def mutation_view(permission, action=None):
    @protected(permission, ['POST'], database_preflight=False)
    def view(request):
        payload = _body(request)
        if action and ('definition' in payload) != (action == 'generate'):
            raise DomainError('INPUT_FIELDS_INVALID', 422)
        key = request.headers.get('Idempotency-Key')
        return JsonResponse({'ok': True, **mutate(request.user.pk, permission, key, payload)})
    return view


@protected('audit.metadata.read', ['GET'])
def audit(request):
    if set(request.GET) - {'record_ref', 'operator_ref'} or any(len(request.GET.getlist(k)) != 1 for k in request.GET):
        raise DomainError('INPUT_FIELDS_INVALID', 422)
    actor = request.authorized_actor
    rows = AuditEvent.objects.filter(store=actor.store)
    if 'operator_ref' in request.GET:
        operator = request.GET['operator_ref']
        if not operator.isdigit() or len(operator) > 16:
            raise DomainError('INVALID_REFERENCE', 422)
        rows = rows.filter(actor_id=int(operator))
    if 'record_ref' in request.GET:
        from clinic.models import AuditTarget
        from .services import _uuid
        targets = AuditTarget.objects.filter(kind='record', target=_uuid(request.GET['record_ref'])).values('event_id')
        rows = rows.filter(pk__in=targets)
    rows = rows.order_by('-created_at')[:100]
    return JsonResponse({'ok': True, 'items': [{'event_ref': str(row.pk), 'event_at': row.created_at.isoformat(),
                         'event_store_ref': str(row.store_id), 'operator_ref': row.actor_id, 'operator_role': row.actor_role,
                         'action': row.action, 'outcome': row.outcome, 'normalized_reason': row.normalized_reason,
                         'target_ref': str(row.target) if row.target else None} for row in rows]})


for public_view in [home, signin, signout, console, frontend_asset, session_info]:
    public_view.public_entry = True
