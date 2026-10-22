import json
from django.http import JsonResponse
from governance.services import DomainError
from governance.views import _body, protected
from .services import auxiliary, mutate, read


def query(request):
    values = request.GET.dict()
    if any(len(request.GET.getlist(k)) != 1 for k in request.GET):
        raise DomainError('INPUT_INVALID', 422)
    try:
        if 'page' in values:
            values['page'] = int(values['page'])
        if 'time_range' in values:
            values['time_range'] = json.loads(values['time_range'])
    except (ValueError, TypeError):
        raise DomainError('INPUT_INVALID', 422) from None
    return values


@protected('encounter.create', ['POST'])
def create(request):
    return JsonResponse(mutate(request.user.pk, 'create', request.headers.get('Idempotency-Key'), _body(request)))


@protected('encounter.read', ['GET'])
def receptions(request):
    return JsonResponse(read(request.user.pk, query=query(request)))


@protected('encounter.read', ['GET'])
def reception(request, encounter_id):
    if request.GET:
        raise DomainError('INPUT_FIELDS_INVALID', 422)
    return JsonResponse(read(request.user.pk, ref=str(encounter_id)))


@protected('clinical.read', ['GET'])
def clinicals(request):
    return JsonResponse(read(request.user.pk, clinical=True, query=query(request)))


@protected('clinical.read', ['GET'])
def clinical(request, encounter_id):
    if request.GET:
        raise DomainError('INPUT_FIELDS_INVALID', 422)
    return JsonResponse(read(request.user.pk, clinical=True, ref=str(encounter_id)))


def change(mode):
    permission = 'encounter.correct' if mode == 'correct' else 'encounter.mark_error'
    @protected(permission, ['POST'])
    def view(request, encounter_id):
        return JsonResponse(mutate(request.user.pk, mode, request.headers.get('Idempotency-Key'), _body(request), str(encounter_id)))
    return view


@protected('encounter.create', ['GET'])
def doctors(request):
    return JsonResponse(auxiliary(request.user.pk, 'doctor_options', query(request)))


@protected('encounter.create', ['POST'])
def pet_check(request):
    return JsonResponse(auxiliary(request.user.pk, 'verify_pet_ref', _body(request)))
