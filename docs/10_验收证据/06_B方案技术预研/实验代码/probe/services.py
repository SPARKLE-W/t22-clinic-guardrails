"""PRE mechanisms only: not a complete control compiler or medical product."""
import csv
import hashlib
import io
import json
import uuid
from django.contrib.auth.models import User
from django.db import DatabaseError, connection, transaction
from .models import Profile, PolicyHead, Encounter, Record, Revision, Medication, Suggestion, Decision, Operation, Audit, AuditTarget, Alert, Outbox

PERMISSIONS = {'diagnosis':'clinical.diagnosis.write','append':'medication.record',
               'correct':'medication.record','adopt':'clinical.suggestion.adopt','decline':'clinical.suggestion.decline'}


class AuditPersistenceError(Exception):
    pass


def outcome(status, code, **values):
    return {'status':status,'code':code,**values}


def _principal(user):
    if not user or not User.objects.filter(pk=user.pk,is_active=True).exists():
        return None
    return Profile.objects.filter(user_id=user.pk).first()


def _audit(p, action, target, result='success', reason='OK', record_ids=(), details=None, fault=False):
    # fault causes a real NOT NULL database error, not a mocked commit result.
    try:
        event = Audit.objects.create(event_store_id=p.store_id,actor_id=p.user_id,role=p.role,
                    action=None if fault else action,result=result,reason_code=reason,
                    target_ref=str(target),private_details=details or {})
        AuditTarget.objects.bulk_create([AuditTarget(audit=event,record_id=rid) for rid in record_ids])
    except DatabaseError as exc:
        raise AuditPersistenceError from exc
    return event


def _deny(p, target, action):
    # This helper is for clinical requests only: C-11 event belongs to requester.
    with transaction.atomic():
        _audit(p,action,target,result='denied',reason='SCOPE_DENIED')
    return outcome(403,'SCOPE_DENIED')


def _owns(p, encounter_id):
    return p.role=='doctor' and Encounter.objects.filter(pk=encounter_id,store_id=p.store_id,doctor_id=p.user_id).exists()


def _shared_encounters(ids):
    ordered = sorted(uuid.UUID(str(value)).hex for value in ids)
    placeholders = ','.join(['%s']*len(ordered))
    with connection.cursor() as c:
        c.execute(f'SELECT id FROM probe_encounter WHERE id IN ({placeholders}) ORDER BY id FOR SHARE',ordered)
        c.fetchall()


def _record_data(record):
    return {'id':str(record.id),'encounter_id':str(record.encounter_id),
            'diagnosis':record.diagnosis,'examination':record.examination,'adopted_text':record.adopted_text,
            'version':record.version,'template_version':record.template_version,
            'revisions':list(Revision.objects.filter(record=record).order_by('version').values('version','content','actor_id','reason','created_at')),
            'medications':list(Medication.objects.filter(record=record).order_by('created_at','id').values('id','original_id','drug','amount','unit','actor_id','reason','created_at'))}


def read_medical(user, encounter_id, *, audit_fault=False):
    try:
        p = _principal(user)
        if p is None: return outcome(401,'AUTH_REQUIRED')
        if not _owns(p,encounter_id): return _deny(p,encounter_id,'clinical.read')
        with transaction.atomic():
            _shared_encounters([encounter_id])
            record = Record.objects.filter(encounter_id=encounter_id).first()
            data = _record_data(record) if record else None
            _audit(p,'clinical.read',encounter_id,record_ids=[record.id] if record else [],fault=audit_fault)
        return outcome(200,'OK',record=data)
    except AuditPersistenceError:
        return outcome(503,'AUDIT_UNAVAILABLE')
    except DatabaseError:
        return outcome(503,'PERSISTENCE_UNCONFIRMED')


def _acquire_operation(p, permission, key, fingerprint):
    # InnoDB's unique-key conflict waits for the first transaction. The second
    # authoritative check happens after this statement, never in a stale fast read.
    with connection.cursor() as c:
        c.execute('INSERT INTO probe_operation (actor_id,permission,operation_key,fingerprint,outcome,status) VALUES (%s,%s,%s,%s,%s,0) ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)',
                  [p.user_id,permission,key,fingerprint,'{}'])
    return Operation.objects.select_for_update().get(actor_id=p.user_id,permission=permission,operation_key=key)


def _finish(op, response):
    op.status = response['status']
    op.outcome = response
    op.save(update_fields=['status','outcome'])
    return response


def _reject(op, p, encounter, code, status=409):
    _audit(p,op.permission,encounter.id,result='rejected',reason=code)
    return _finish(op,outcome(status,code))


def _guard_reject(op, p, encounter, version, hits):
    for rule_id, field in sorted(set(hits)):
        alert = Alert.objects.create(encounter=encounter,attempt=op,rule_id=rule_id,rule_version=version,field=field)
        Outbox.objects.create(alert=alert)
    _audit(p,op.permission,encounter.id,result='rejected',reason='GUARD_REJECTED')
    return _finish(op,outcome(422,'GUARD_REJECTED',hits=[{'rule_id':r,'field':f} for r,f in sorted(set(hits))]))


def _nonempty(value):
    return isinstance(value,str) and bool(value.strip())


def write_medical(user, encounter_id, mode, operation_key, data, *, audit_fault=False, before_commit=None):
    try:
        p = _principal(user)
        if p is None: return outcome(401,'AUTH_REQUIRED')
        if mode not in PERMISSIONS: return outcome(403,'PERMISSION_UNREGISTERED')
        permission = PERMISSIONS[mode]
        if not _owns(p,encounter_id): return _deny(p,encounter_id,permission)
        if not isinstance(operation_key,str) or not 1 <= len(operation_key) <= 64:
            return outcome(422,'INPUT_INVALID')
        fingerprint = hashlib.sha256(json.dumps({'mode':mode,'target':str(uuid.UUID(str(encounter_id))),'data':data},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with transaction.atomic():
            op = _acquire_operation(p,permission,operation_key,fingerprint)
            if op.fingerprint != fingerprint: return outcome(409,'OPERATION_CONTENT_CONFLICT')
            if op.status: return op.outcome
            with connection.cursor() as c:
                c.execute('SELECT version FROM probe_policyhead WHERE store_id=%s FOR SHARE',[p.store_id])
                pinned = c.fetchone()
            if pinned is None or pinned[0] < 1:
                return _reject(op,p,Encounter.objects.get(pk=encounter_id),'POLICY_UNAVAILABLE',503)
            policy_version = pinned[0]
            encounter = Encounter.objects.select_for_update().get(pk=encounter_id)
            record = Record.objects.select_for_update().filter(encounter=encounter).first()
            suggestion = None
            if mode in ['adopt','decline']:
                suggestion = Suggestion.objects.select_for_update().filter(pk=data.get('suggestion_id'),encounter=encounter).first()
                if suggestion is None: return _reject(op,p,encounter,'SCOPE_DENIED',403)
                if Decision.objects.filter(suggestion=suggestion).exists(): return _reject(op,p,encounter,'DECISION_FINAL')
            if encounter.misregistered and not (mode=='correct' or mode=='decline' or (mode=='diagnosis' and data.get('correction') is True and record is not None)):
                return _reject(op,p,encounter,'MISREGISTERED')
            expected = data.get('expected_version')
            if mode != 'decline' and (type(expected) is not int or expected != (record.version if record else 0)):
                return _reject(op,p,encounter,'VERSION_CONFLICT')
            original = None
            if mode=='correct':
                original = Medication.objects.select_for_update().filter(pk=data.get('original_id'),record=record).first()
                if original is None: return _reject(op,p,encounter,'SCOPE_DENIED',403)
            hits = []
            if mode=='diagnosis' and not _nonempty(data.get('diagnosis')): hits.append(('SR-02','diagnosis'))
            if mode in ['append','correct']:
                for field in ['drug','amount','unit']:
                    if not _nonempty(data.get(field)): hits.append(('SR-02',field))
                if mode=='correct' and not _nonempty(data.get('reason')): hits.append(('SR-03','reason'))
            if mode=='diagnosis' and record is not None and not _nonempty(data.get('reason')): hits.append(('SR-02','reason'))
            if mode=='adopt' and not _nonempty(data.get('reason')): hits.append(('SR-04','reason'))
            if mode not in ['diagnosis','decline'] and record is None and not _nonempty(data.get('diagnosis')): hits.append(('SR-02','diagnosis'))
            if hits: return _guard_reject(op,p,encounter,policy_version,hits)
            if mode=='decline':
                decision = Decision.objects.create(suggestion=suggestion,actor=user,state='declined',reason=data.get('reason',''))
                _audit(p,permission,encounter.id,record_ids=[record.id] if record else [],fault=audit_fault)
                return _finish(op,outcome(200,'SAVED',decision_id=decision.id))
            if record is None:
                record = Record(encounter=encounter,diagnosis=data['diagnosis'],version=0)
            if mode=='diagnosis':
                record.diagnosis = data['diagnosis']
                record.examination = data.get('examination',record.examination)
            record.version += 1
            record.save()
            values = {'record_id':str(record.id),'version':record.version,'policy_version':policy_version}
            if mode in ['append','correct']:
                item = Medication.objects.create(record=record,original=original,drug=data['drug'],amount=data['amount'],unit=data['unit'],actor=user,reason=data.get('reason',''))
                values['medication_id'] = str(item.id)
            if mode=='adopt':
                decision = Decision.objects.create(suggestion=suggestion,actor=user,state='adopted',reason=data['reason'])
                record.adopted_text += ('\n' if record.adopted_text else '') + suggestion.text
                record.save(update_fields=['adopted_text'])
                values['decision_id'] = decision.id
            Revision.objects.create(record=record,version=record.version,actor=user,reason=data.get('reason',''),content={'diagnosis':record.diagnosis,'examination':record.examination,'adopted_text':record.adopted_text})
            _audit(p,permission,encounter.id,record_ids=[record.id],fault=audit_fault)
            if before_commit: before_commit()  # internal concurrency hook; no HTTP exposure.
            response = _finish(op,outcome(200,'SAVED',**values))
        return response
    except AuditPersistenceError:
        return outcome(503,'AUDIT_UNAVAILABLE')
    except DatabaseError:
        return outcome(503,'PERSISTENCE_UNCONFIRMED')


def export_medical(user, ids, purpose, *, audit_fault=False, before_expose=None):
    try:
        p = _principal(user)
        if p is None: return outcome(401,'AUTH_REQUIRED')
        if not isinstance(ids,list) or not ids or not _nonempty(purpose): return outcome(422,'INPUT_INVALID')
        try:
            normalized = {uuid.UUID(value) for value in ids if isinstance(value,str)}
            if len(normalized)!=len(set(ids)): return outcome(422,'INPUT_INVALID')
        except (ValueError,TypeError,AttributeError):
            return outcome(422,'INPUT_INVALID')
        if p.role!='doctor': return _deny(p,'export','export.clinical')
        with transaction.atomic():
            records = list(Record.objects.filter(id__in=normalized,encounter__store_id=p.store_id,encounter__doctor_id=p.user_id).order_by('id'))
            if len(records)!=len(normalized): return _deny(p,'export','export.clinical')
            _shared_encounters([r.encounter_id for r in records])
            # Re-read after shared parent locks, so all revisions/medications
            # correspond to one protected, complete set of content versions.
            records = list(Record.objects.filter(id__in=normalized).order_by('id'))
            revisions_by_record, medications_by_record = {}, {}
            for rev in Revision.objects.filter(record_id__in=normalized).order_by('record_id','version'):
                revisions_by_record.setdefault(rev.record_id, []).append(rev)
            for item in Medication.objects.filter(record_id__in=normalized).order_by('record_id','created_at','id'):
                medications_by_record.setdefault(item.record_id, []).append(item)
            buffer = io.StringIO(newline='')
            writer = csv.DictWriter(buffer,fieldnames=['kind','record_id','encounter_id','template_version','version','original_id','actor_id','created_at','reason','payload'])
            writer.writeheader()
            for record in records:
                base = {'record_id':str(record.id),'encounter_id':str(record.encounter_id),'template_version':record.template_version}
                content = {'diagnosis':record.diagnosis,'examination':record.examination,'adopted_text':record.adopted_text}
                writer.writerow({**base,'kind':'record','version':record.version,'payload':json.dumps(content,ensure_ascii=False)})
                for rev in revisions_by_record.get(record.id, []):
                    writer.writerow({**base,'kind':'revision','version':rev.version,'actor_id':rev.actor_id,'created_at':rev.created_at.isoformat(),'reason':rev.reason,'payload':json.dumps(rev.content,ensure_ascii=False)})
                for item in medications_by_record.get(record.id, []):
                    writer.writerow({**base,'kind':'medication','original_id':str(item.original_id or ''),'actor_id':item.actor_id,'created_at':item.created_at.isoformat(),'reason':item.reason,'payload':json.dumps({'id':str(item.id),'drug':item.drug,'amount':item.amount,'unit':item.unit},ensure_ascii=False)})
            if before_expose: before_expose()  # internal adversarial authorization fixture only.
            current = _principal(user)
            if current is None: return outcome(401,'AUTH_REQUIRED')
            allowed_now = current.role=='doctor' and Record.objects.filter(id__in=normalized,encounter__store_id=current.store_id,encounter__doctor_id=current.user_id).count()==len(normalized)
            if not allowed_now: return _deny(current,'export','export.clinical')
            _audit(current,'export.clinical','export',record_ids=[r.id for r in records],details={'purpose':purpose,'scope':[str(r.id) for r in records]},fault=audit_fault)
            text = buffer.getvalue()
        return outcome(200,'OK',csv=text,record_count=len(records),utf8_bytes=len(text.encode('utf-8')))
    except AuditPersistenceError:
        return outcome(503,'AUDIT_UNAVAILABLE')
    except DatabaseError:
        return outcome(503,'PERSISTENCE_UNCONFIRMED')


def audit_metadata(user):
    p = _principal(user)
    if p is None: return outcome(401,'AUTH_REQUIRED')
    if p.role!='manager': return outcome(403,'SCOPE_DENIED')
    fields = ['id','created_at','event_store_id','actor_id','role','action','result','reason_code','target_ref']
    return outcome(200,'OK',events=list(Audit.objects.filter(event_store_id=p.store_id).order_by('id').values(*fields)))


def mark_error(user, encounter_id, reason, *, before_commit=None):
    p = _principal(user)
    if p is None: return outcome(401,'AUTH_REQUIRED')
    if p.role!='frontdesk' or not _nonempty(reason): return outcome(403,'SCOPE_DENIED')
    with transaction.atomic():
        encounter = Encounter.objects.select_for_update().filter(pk=encounter_id,store_id=p.store_id,receptionist_id=p.user_id).first()
        if encounter is None: return _deny(p,encounter_id,'encounter.mark_error')
        encounter.misregistered = True
        encounter.save(update_fields=['misregistered'])
        _audit(p,'encounter.mark_error',encounter.id,details={'reason':reason})
        if before_commit: before_commit()
    return outcome(200,'SAVED')


def activate_probe_version(user, version):
    # Technical concurrency fixture only, not a runtime approval/activation API.
    p = _principal(user)
    if p is None: return outcome(401,'AUTH_REQUIRED')
    if p.role!='manager': return outcome(403,'SCOPE_DENIED')
    with transaction.atomic():
        head = PolicyHead.objects.select_for_update().get(store_id=p.store_id)
        head.version = version
        head.save(update_fields=['version'])
    return outcome(200,'PRE_FIXTURE_VERSION_CHANGED')


def create_reception(user, operation_key, data):
    p = _principal(user)
    if p is None: return outcome(401,'AUTH_REQUIRED')
    if p.role!='frontdesk': return outcome(403,'SCOPE_DENIED')
    if not isinstance(operation_key,str) or not 1 <= len(operation_key) <= 64:
        return outcome(422,'INPUT_INVALID')
    if any(not _nonempty(data.get(field)) for field in ['pet_id','species','complaint']):
        return outcome(422,'INPUT_INVALID')
    if not Profile.objects.filter(user_id=data.get('doctor_id'),store_id=p.store_id,role='doctor',user__is_active=True).exists():
        return outcome(403,'SCOPE_DENIED')
    fingerprint = hashlib.sha256(json.dumps({'mode':'create','target':'new','data':data},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    try:
        with transaction.atomic():
            op = _acquire_operation(p,'encounter.create',operation_key,fingerprint)
            if op.fingerprint!=fingerprint: return outcome(409,'OPERATION_CONTENT_CONFLICT')
            if op.status: return op.outcome
            encounter = Encounter.objects.create(store_id=p.store_id,receptionist_id=p.user_id,doctor_id=data['doctor_id'],pet_id=data['pet_id'],species=data['species'],complaint=data['complaint'],pet_name=data.get('pet_name',''))
            _audit(p,'encounter.create',encounter.id)
            response = _finish(op,outcome(200,'SAVED',encounter_id=str(encounter.id)))
        return response
    except AuditPersistenceError:
        return outcome(503,'AUDIT_UNAVAILABLE')
    except DatabaseError:
        return outcome(503,'PERSISTENCE_UNCONFIRMED')
