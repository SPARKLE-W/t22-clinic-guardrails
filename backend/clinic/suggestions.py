"""Saved demo suggestions and doctor decisions; no source can write a record."""
from django.db import transaction
from governance.models import Operation
from governance.services import DomainError, _uuid, require
from vetcontrols.compiler import _digest, _normalize
from .guards import evaluate
from .export_projection import note_encounter_lock, refresh_projection
from .models import ClinicalCheck, ClinicalRecord, ClinicalRevision, MedicationEntry, Suggestion, SuggestionDecision
from .services import (_extra_values, _object, _own, _persist_guard_alerts, _policy_head_for_share, _refusal,
                       _template, _text)
from .writes import _audit, _validated

ADOPT = 'clinical.suggestion.adopt'
DECLINE = 'clinical.suggestion.decline'


def decision_item(row):
    return {'decision_ref':str(row.pk), 'outcome':row.outcome, 'actor_ref':row.actor_id,
            'created_at':row.created_at.isoformat(), 'reason':row.reason,
            'adopted_content':row.adopted_content, 'record_ref':str(row.record_id) if row.record_id else None,
            'version_ref':str(row.version_id) if row.version_id else None,
            'medication_ref':str(row.medication_id) if row.medication_id else None}


def suggestion_item(row):
    try:
        decision = row.decision
    except SuggestionDecision.DoesNotExist:
        decision = None
    return {'suggestion_ref':str(row.pk), 'encounter_ref':str(row.encounter_id),
            'content':row.content, 'source':row.source, 'status':decision.outcome if decision else 'pending',
            'decision':decision_item(decision) if decision else None}


def refusal(actor_id, error, ref, permission):
    row = Suggestion.objects.filter(pk=_uuid(ref)).only('encounter_id').first()
    return _refusal(actor_id, permission, error, str(row.encounter_id) if row else None)


def listing(actor_id, ref):
    try:
        with transaction.atomic():
            actor = require(actor_id, 'clinical.read', lock=True)
            row = _own(actor, ref, clinical=True, lock=True)
            record = ClinicalRecord.objects.filter(encounter=row).first()
            if record:
                note_encounter_lock(record, row)
            _template(record.template if record else row.template, actor.store_id)
            items = [suggestion_item(item) for item in Suggestion.objects.filter(encounter=row).select_related('decision').order_by('created_at','id')]
            _audit(actor, 'clinical.read', row, 'success', 'COMPLETED', record)
            return {'items':items}
    except DomainError as error:
        _refusal(actor_id, 'clinical.read', error, ref)
        raise


def mutate(actor_id, mode, key, payload, ref):
    permission = ADOPT if mode == 'adopt' else DECLINE
    try:
        with transaction.atomic():
            actor = require(actor_id, permission, lock=True)
            operation_key = _uuid(key)
            head = _policy_head_for_share(actor.store_id) if mode == 'adopt' else None
            saved = Suggestion.objects.filter(pk=_uuid(ref)).only('encounter_id').first()
            if not saved:
                raise DomainError('TARGET_UNAVAILABLE',403)
            row = _own(actor, str(saved.encounter_id), clinical=True, lock=True)
            saved = Suggestion.objects.select_for_update().get(pk=ref)
            allowed = ['encounter_ref','suggestion_ref','record_ref','reason'] + (['adopted_content'] if mode == 'adopt' else ['decision'])
            _object(payload,allowed,['encounter_ref','suggestion_ref'] + (['adopted_content'] if mode == 'adopt' else ['decision']))
            if _uuid(payload['encounter_ref']) != row.pk or _uuid(payload['suggestion_ref']) != saved.pk:
                raise DomainError('TARGET_UNAVAILABLE',403)
            reason = _text(payload.get('reason',''),2000,False)
            digest = _digest(_normalize({'mode':mode,'target':ref,'payload':payload,'store':str(actor.store_id)}))
            previous = Operation.objects.filter(actor=actor,permission=permission,key=operation_key).first()
            if previous:
                if previous.digest != digest:
                    raise DomainError('OPERATION_CONTENT_CONFLICT')
                return previous.result
            if SuggestionDecision.objects.filter(suggestion=saved).exists():
                raise DomainError('DECISION_FINAL')
            record = ClinicalRecord.objects.filter(encounter=row).first()
            if 'record_ref' in payload and (not record or _uuid(payload['record_ref']) != record.pk):
                raise DomainError('STATE_OR_VERSION_CONFLICT')
            check, version, medication = None, None, None
            adopted = {}
            if mode == 'decline':
                if payload['decision'] != 'declined':
                    raise DomainError('INPUT_INVALID',422)
            else:
                if row.error_mark:
                    raise DomainError('STATE_CONFLICT')
                adopted = payload['adopted_content']
                _object(adopted,['diagnosis','examination','extra_fields','medication','original_version_ref'])
                if not (set(adopted) & {'diagnosis','examination','medication'} or adopted.get('extra_fields')):
                    raise DomainError('INPUT_FIELDS_INVALID',422)
                for field in ['diagnosis','examination']:
                    if field in adopted:
                        _text(adopted[field],10000,False)
                med_values = None
                if 'medication' in adopted:
                    _object(adopted['medication'],['drug','amount','unit'])
                    med_values = {field:_text(adopted['medication'].get(field,''),maximum,False)
                                  for field,maximum in [('drug',2000),('amount',256),('unit',100)]}
                current = ClinicalRevision.objects.filter(record=record,version=record.current_version).first() if record else None
                if record:
                    if not current:
                        raise DomainError('RECORD_UNAVAILABLE',503)
                    if 'record_ref' not in payload or 'original_version_ref' not in adopted or _uuid(adopted['original_version_ref']) != current.pk:
                        raise DomainError('STATE_OR_VERSION_CONFLICT')
                elif 'original_version_ref' in adopted:
                    raise DomainError('STATE_OR_VERSION_CONFLICT')
                values = {field:adopted.get(field,getattr(current,field,'') if current else '') for field in ['diagnosis','examination']}
                if not head or not head.candidate_id:
                    raise DomainError('POLICY_UNAVAILABLE',503)
                template_candidate = record.template if record else row.template
                template = _validated(template_candidate,actor.store_id)
                policy_candidate = template_candidate if head.candidate_id == template_candidate.pk else head.candidate
                if policy_candidate.pk != template_candidate.pk:
                    _validated(policy_candidate,actor.store_id)
                extra_fields = _extra_values(template,adopted.get('extra_fields',{}),current.extra_fields if current else None)
                if saved.source.get('type') != 'demo' or not saved.source.get('label') or saved.source.get('version') != saved.source_version:
                    raise DomainError('SUGGESTION_SOURCE_UNAVAILABLE',503)
                checks,hits = evaluate(policy_candidate.envelope['payload']['demand'],template,values,'adopt',
                    medication={'values':med_values,'original_valid':True,'reason_valid':True} if med_values is not None else None,
                    suggestion={'reason_valid':bool(reason.strip()),'reason':reason})
                check = ClinicalCheck.objects.create(encounter=row,record=record,original_version=current,actor=actor,mode='suggestion.adopt',
                    template=template_candidate,policy=policy_candidate,policy_digest=policy_candidate.digest,status='blocked' if hits else 'passed',checks=checks)
                if hits:
                    _persist_guard_alerts(check, hits)
                    _audit(actor,permission,row,'refused','GUARD_BLOCKED',record)
                    result = {'ok':False,'error':{'code':'GUARD_BLOCKED'},'event_ref':str(check.pk)}
                    Operation.objects.create(actor=actor,permission=permission,key=operation_key,digest=digest,result=result)
                    return result
                if not record:
                    record = ClinicalRecord.objects.create(encounter=row,template=template_candidate)
                    note_encounter_lock(record, row)
                if current is None or set(adopted) & {'diagnosis','examination'} or adopted.get('extra_fields'):
                    if current:
                        record.current_version += 1
                    version = ClinicalRevision.objects.create(record=record,version=record.current_version,actor=actor,
                                                              reason=reason,extra_fields=extra_fields,**values)
                else:
                    version = current
                check.record,check.result_version = record,version
                check.save(update_fields=['record','result_version'])
                if med_values is not None:
                    medication = MedicationEntry.objects.create(record=record,evaluation=check,actor=actor,reason=reason,**med_values)
            decision = SuggestionDecision.objects.create(suggestion=saved,outcome='adopted' if mode == 'adopt' else 'declined',actor=actor,
                reason=reason,adopted_content=adopted,record=record,version=version,medication=medication,evaluation=check)
            if mode == 'adopt':
                refresh_projection(record, row, template_candidate, template,
                                   save_version=current is not None and version.pk != current.pk)
            _audit(actor,permission,row,'success','COMPLETED',record)
            result = {'saved_ref':str(decision.pk),'record_ref':str(record.pk) if record else None,
                'version_ref':str(version.pk) if version else None,'operation_state':decision.outcome,
                'responsibility_metadata':{'actor_ref':actor.pk,'created_at':decision.created_at.isoformat()}}
            Operation.objects.create(actor=actor,permission=permission,key=operation_key,digest=digest,result=result)
            return result
    except DomainError as error:
        refusal(actor_id,error,ref,permission)
        raise
