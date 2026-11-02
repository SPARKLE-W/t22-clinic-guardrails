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

    def test_every_protected_post_route_rejects_malformed_json_with_audit(self):
        routes = [(str(pattern.pattern), pattern.callback) for pattern in get_resolver().url_patterns
                  if getattr(pattern.callback, 'permission_key', None)]
        post_routes = [(route, callback) for route, callback in routes
                       if 'POST' in inspect.getclosurevars(callback).nonlocals['methods']]
        self.assertEqual(20, len(post_routes))
        for route, callback in post_routes:
            permission = callback.permission_key
            path = '/' + re.sub(r'<uuid:[^>]+>', str(uuid.UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')), route)
            client = Client()
            client.force_login(self.accounts[ROLES[permission]])
            with self.subTest(route=route):
                audits = AuditEvent.objects.filter(actor=self.accounts[ROLES[permission]],
                                                  action=permission, normalized_reason='INPUT_INVALID')
                before = audits.count()
                response = client.generic('POST', path, data='{', content_type='application/json')
                self.assertEqual(422, response.status_code)
                self.assertEqual('INPUT_INVALID', response.json()['error']['code'])
                self.assertEqual(before + 1, audits.count())

    def test_every_protected_get_route_rejects_unknown_query_field(self):
        routes = [(str(pattern.pattern), pattern.callback) for pattern in get_resolver().url_patterns
                  if getattr(pattern.callback, 'permission_key', None)]
        get_routes = [(route, callback) for route, callback in routes
                      if 'GET' in inspect.getclosurevars(callback).nonlocals['methods']]
        self.assertEqual(15, len(get_routes))
        for route, callback in get_routes:
            permission = callback.permission_key
            path = '/' + re.sub(r'<uuid:[^>]+>', str(uuid.UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')), route)
            client = Client()
            client.force_login(self.accounts[ROLES[permission]])
            with self.subTest(route=route):
                before = AuditEvent.objects.filter(actor=self.accounts[ROLES[permission]],
                                                   action=permission).count()
                response = client.get(path, {'unexpected': '1'})
                self.assertEqual(422, response.status_code)
                self.assertIn(response.json()['error']['code'], {'INPUT_INVALID', 'INPUT_FIELDS_INVALID'})
                audits = AuditEvent.objects.filter(actor=self.accounts[ROLES[permission]], action=permission)
                self.assertEqual(before + 1, audits.count())
                self.assertEqual(response.json()['error']['code'], audits.order_by('-created_at', '-pk').first().normalized_reason)
