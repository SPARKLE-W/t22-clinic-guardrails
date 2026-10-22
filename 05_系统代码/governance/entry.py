"""Reject missing endpoint registration both before release and at runtime."""
from django.core.checks import Error, register
from django.db import DatabaseError
from django.http import JsonResponse
from django.urls import get_resolver
from django.utils.deprecation import MiddlewareMixin
from .services import ROLES, write_audit


def registered(view):
    return getattr(view, 'public_entry', False) or (
        getattr(view, 'central_entry', False) and getattr(view, 'permission_key', None) in ROLES)


@register()
def check_entries(app_configs, **kwargs):
    errors = []
    def scan(patterns):
        for pattern in patterns:
            if hasattr(pattern, 'url_patterns'):
                scan(pattern.url_patterns)
            elif not registered(pattern.callback):
                errors.append(Error('Endpoint lacks a registered central permission entry.', obj=str(pattern.pattern), id='governance.E001'))
    scan(get_resolver().url_patterns)
    return errors


class PermissionEntryMiddleware(MiddlewareMixin):
    def process_view(self, request, view, args, kwargs):
        if registered(view):
            return None
        try:
            write_audit(request.user if request.user.is_authenticated else None, 'entry.unregistered', 'refused', 'MISSING_PERMISSION_ENTRY')
        except DatabaseError:
            return JsonResponse({'ok': False, 'error': {'code': 'AUDIT_UNAVAILABLE'}}, status=503)
        return JsonResponse({'ok': False, 'error': {'code': 'MISSING_PERMISSION_ENTRY'}}, status=403)
