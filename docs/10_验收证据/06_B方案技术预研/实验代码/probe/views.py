import json
import uuid
from django.contrib.auth import authenticate, login
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from . import services

REGISTERED = set(services.PERMISSIONS.values()) | {'clinical.read','export.clinical','audit.metadata.read','encounter.create'}


@ensure_csrf_cookie
@csrf_protect
def login_view(request):
    if request.method=='GET': return JsonResponse({'csrf':get_token(request)})
    if request.method!='POST': return JsonResponse({'code':'METHOD_NOT_ALLOWED'},status=405)
    try: data=json.loads(request.body)
    except (ValueError,TypeError): return JsonResponse({'code':'INPUT_INVALID'},status=422)
    if not isinstance(data,dict) or any(not isinstance(data.get(k),str) or not data[k] for k in ['username','password']):
        return JsonResponse({'code':'INPUT_INVALID'},status=422)
    user = authenticate(request,username=data.get('username'),password=data.get('password'))
    if user is None: return JsonResponse({'code':'AUTH_REQUIRED'},status=401)
    login(request,user)
    return JsonResponse({'code':'OK'})


class RegisteredRoute(BasePermission):
    def has_permission(self, request, view):
        registered = getattr(view,'permission_key',None) in REGISTERED
        if not registered:
            principal = services._principal(request.user)
            if principal:
                with transaction.atomic():
                    services._audit(principal,'PRE.unregistered_endpoint','unregistered',result='denied',reason='PERMISSION_UNREGISTERED')
        return registered


class ProbeView(APIView):
    permission_key = None
    permission_classes = [IsAuthenticated,RegisteredRoute]

    def respond(self, result):
        return Response(result,status=result['status'])

    def fields_ok(self, request, allowed):
        return isinstance(request.data,dict) and not (set(request.data)-set(allowed))


class ReadView(ProbeView):
    permission_key = 'clinical.read'
    def get(self, request, encounter_id):
        return self.respond(services.read_medical(request.user,encounter_id))


class WriteView(ProbeView):
    mode = None
    def post(self, request, encounter_id):
        allowed = {'operation_key','expected_version','reason'}
        allowed |= {'diagnosis','examination','correction'} if self.mode=='diagnosis' else set()
        allowed |= {'drug','amount','unit','original_id','diagnosis'} if self.mode in ['append','correct'] else set()
        allowed |= {'suggestion_id','diagnosis'} if self.mode in ['adopt','decline'] else set()
        if not self.fields_ok(request,allowed): return self.respond(services.outcome(422,'FIELD_FORBIDDEN'))
        for field in ['operation_key','reason','diagnosis','examination','drug','amount','unit']:
            if field in request.data and not isinstance(request.data[field],str):
                return self.respond(services.outcome(422,'INPUT_INVALID'))
        for field in ['suggestion_id','original_id']:
            if field in request.data:
                try: uuid.UUID(request.data[field])
                except (ValueError,TypeError,AttributeError): return self.respond(services.outcome(422,'INPUT_INVALID'))
        if 'expected_version' in request.data and type(request.data['expected_version']) is not int:
            return self.respond(services.outcome(422,'INPUT_INVALID'))
        if 'correction' in request.data and type(request.data['correction']) is not bool:
            return self.respond(services.outcome(422,'INPUT_INVALID'))
        data = dict(request.data)
        key = data.pop('operation_key',None)
        return self.respond(services.write_medical(request.user,encounter_id,self.mode,key,data))


class ExportView(ProbeView):
    permission_key = 'export.clinical'
    def post(self, request):
        if not self.fields_ok(request,{'record_ids','purpose'}): return self.respond(services.outcome(422,'FIELD_FORBIDDEN'))
        result = services.export_medical(request.user,request.data.get('record_ids'),request.data.get('purpose'))
        if result['status']!=200: return self.respond(result)
        response = HttpResponse(result['csv'].encode('utf-8'),content_type='text/csv; charset=utf-8')
        response['Content-Length'] = result['utf8_bytes']
        return response


class AuditView(ProbeView):
    permission_key = 'audit.metadata.read'
    def get(self, request): return self.respond(services.audit_metadata(request.user))


class ReceptionView(ProbeView):
    permission_key = 'encounter.create'
    def post(self, request):
        if not self.fields_ok(request,{'operation_key','pet_id','species','complaint','doctor_id','pet_name'}):
            return self.respond(services.outcome(422,'FIELD_FORBIDDEN'))
        data = dict(request.data)
        key = data.pop('operation_key',None)
        return self.respond(services.create_reception(request.user,key,data))


def scan_registry(patterns):
    missing=[]
    for entry in patterns:
        callback=entry.callback
        cls=getattr(callback,'cls',None)
        if cls and issubclass(cls,ProbeView):
            key=getattr(callback,'initkwargs',{}).get('permission_key',getattr(cls,'permission_key',None))
            if key not in REGISTERED: missing.append(str(entry.pattern))
    return missing
