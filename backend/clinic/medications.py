"""Medication commands only insert entries; there is no update/delete route."""
from django.db import transaction
from governance.models import Operation
from governance.services import DomainError, _uuid, require
from vetcontrols.compiler import _digest, _normalize
from .guards import evaluate
from .export_projection import note_encounter_lock, refresh_projection
from .models import ClinicalCheck, ClinicalRecord, ClinicalRevision, MedicationEntry
from .services import _object, _own, _persist_guard_alerts, _policy_head_for_share, _refusal, _text
from .writes import _audit, _validated

PERMISSION = 'medication.record'


def mutate(actor_id, mode, key, payload, ref):
    try:
        with transaction.atomic():
            actor = require(actor_id, PERMISSION, lock=True)
            operation_key = _uuid(key)
            head = _policy_head_for_share(actor.store_id)
            row = _own(actor, ref, clinical=True, lock=True)
            _object(payload, ['drug', 'amount', 'unit'] + (['original_entry_ref', 'reason'] if mode == 'correct' else []))
            values = {field: _text(payload.get(field, ''), maximum, False) for field, maximum in [('drug',2000),('amount',256),('unit',100)]}
            digest = _digest(_normalize({'mode':mode,'target':ref,'payload':payload,'store':str(actor.store_id)}))
            previous = Operation.objects.filter(actor=actor, permission=PERMISSION, key=operation_key).first()
            if previous:
                if previous.digest != digest:
                    raise DomainError('OPERATION_CONTENT_CONFLICT')
                return previous.result
            record = ClinicalRecord.objects.filter(encounter=row).first()
            if not record or (row.error_mark and mode == 'append'):
                raise DomainError('STATE_CONFLICT')
            original, reason = None, ''
            if mode == 'correct':
                reason = _text(payload.get('reason', ''), 2000, False)
                if 'original_entry_ref' in payload:
                    original = MedicationEntry.objects.filter(pk=_uuid(payload['original_entry_ref']),record=record).first()
                    if not original:
                        raise DomainError('TARGET_UNAVAILABLE',403)
                if row.error_mark and not original:
                    raise DomainError('STATE_CONFLICT')
            if not head or not head.candidate_id:
                raise DomainError('POLICY_UNAVAILABLE',503)
            template_candidate = record.template
            template = _validated(template_candidate,actor.store_id)
            policy_candidate = template_candidate if head.candidate_id == template_candidate.pk else head.candidate
            if policy_candidate.pk != template_candidate.pk:
                _validated(policy_candidate,actor.store_id)
            current = ClinicalRevision.objects.filter(record=record,version=record.current_version).first()
            if not current:
                raise DomainError('RECORD_UNAVAILABLE',503)
            checks,hits = evaluate(policy_candidate.envelope['payload']['demand'],template,
                {'diagnosis':current.diagnosis,'examination':current.examination},mode,
                medication={'values':values,'original_valid':mode == 'append' or original is not None,
                            'reason_valid':mode == 'append' or bool(reason.strip())})
            evaluation = ClinicalCheck.objects.create(encounter=row,record=record,actor=actor,mode='medication.'+mode,
                template=template_candidate,policy=policy_candidate,policy_digest=policy_candidate.digest,
                original_version=current,status='blocked' if hits else 'passed',checks=checks)
            if hits:
                _persist_guard_alerts(evaluation, hits)
                _audit(actor,PERMISSION,row,'refused','GUARD_BLOCKED',record)
                result = {'ok':False,'error':{'code':'GUARD_BLOCKED'},'event_ref':str(evaluation.pk)}
            else:
                note_encounter_lock(record, row)
                entry = MedicationEntry.objects.create(record=record,original=original,evaluation=evaluation,
                                                       actor=actor,reason=reason,**values)
                refresh_projection(record, row, template_candidate, template)
                _audit(actor,PERMISSION,row,'success','COMPLETED',record)
                result = {'saved_ref':str(entry.pk),'record_ref':str(record.pk),'operation_state':'saved',
                          'original_entry_ref':str(original.pk) if original else None,
                          'correction_entry_ref':str(entry.pk) if original else None,
                          'responsibility_metadata':{'actor_ref':actor.pk,'created_at':entry.created_at.isoformat()}}
            Operation.objects.create(actor=actor,permission=PERMISSION,key=operation_key,digest=digest,result=result)
            return result
    except DomainError as error:
        _refusal(actor_id,PERMISSION,error,ref)
        raise
