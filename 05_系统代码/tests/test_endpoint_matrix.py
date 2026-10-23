"""Exercise the shared authorization boundary across every protected HTTP route."""
import inspect
import re
import uuid

from django.test import Client, TestCase
from django.urls import get_resolver

from governance.models import Account, AuditEvent, Store
from governance.services import ROLES


class ProtectedEndpointMatrixTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        store = Store.objects.create(name='授权矩阵合成门店')
        cls.accounts = {
            role: Account.objects.create_user(username='matrix-' + role, store=store, role=role)
            for role in ('frontdesk', 'doctor', 'manager')
        }

    def test_every_protected_route_rejects_anonymous_wrong_role_and_wrong_method(self):
        routes = [(str(pattern.pattern), pattern.callback) for pattern in get_resolver().url_patterns
                  if getattr(pattern.callback, 'permission_key', None)]
        self.assertEqual(34, len(routes))
        for route, callback in routes:
            permission = callback.permission_key
            allowed_methods = inspect.getclosurevars(callback).nonlocals['methods']
            path = '/' + re.sub(r'<uuid:[^>]+>', str(uuid.UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')), route)
            wrong_role = next(role for role in self.accounts if role != ROLES[permission])
            for method in allowed_methods:
                with self.subTest(route=route, method=method, negative='anonymous'):
                    response = Client().generic(method, path, data='{}', content_type='application/json')
                    self.assertEqual(401, response.status_code)
                    self.assertEqual('AUTH_REQUIRED', response.json()['error']['code'])
                with self.subTest(route=route, method=method, negative='wrong_role'):
                    client = Client()
                    client.force_login(self.accounts[wrong_role])
                    response = client.generic(method, path, data='{}', content_type='application/json')
                    self.assertEqual(403, response.status_code)
                    self.assertEqual('PERMISSION_DENIED', response.json()['error']['code'])
                    self.assertTrue(AuditEvent.objects.filter(actor=self.accounts[wrong_role],
                                    action=permission, normalized_reason='PERMISSION_DENIED',
                                    store=self.accounts[wrong_role].store).exists())
            with self.subTest(route=route, negative='wrong_method'):
                response = Client().generic('PATCH', path, data='{}', content_type='application/json')
                self.assertEqual(405, response.status_code)
                self.assertEqual('METHOD_NOT_ALLOWED', response.json()['error']['code'])
