"""Create deterministic synthetic formal-load data in an empty isolated database."""
import argparse
import json
import os
import secrets
import sys
import uuid
from pathlib import Path

CODE = Path(__file__).resolve().parents[3] / '05_系统代码'
sys.path.insert(0, str(CODE))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'vetapp.settings')

import django
django.setup()

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from governance.models import Account, Activation, Candidate, Demand, PolicyHead, Review, Store
from clinic.models import (Alert, AlertOutbox, ClinicalCheck, ClinicalRecord,
                           ClinicalRevision, Encounter, GuardHit, MedicationEntry,
                           PetIdentity, ReceptionRevision)
from vetcontrols import compile_demand


NAMESPACE = uuid.UUID('23863255-7eb6-4692-96e2-cb87d8dbf54c')
SEED = 20261024


def ref(kind, store, number):
    return uuid.uuid5(NAMESPACE, f'{SEED}:{kind}:{store}:{number}')


def add_many(model, objects):
    for start in range(0, len(objects), 500):
        model.objects.bulk_create(objects[start:start + 500], batch_size=500)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--private-output', type=Path, required=True)
    args = parser.parse_args()
    output = args.private_output.resolve()
    if os.environ.get('T22_FORMAL_LOAD') != '1' or not settings.DATABASES['default']['NAME'].startswith('t22_perf_'):
        parser.error('Explicit isolated formal-load database is required')
    if output.exists() or not output.parent.is_dir() or output.is_relative_to(CODE.parent):
        parser.error('Use a new private output file outside the repository')
    if Store.objects.exists() or Account.objects.exists() or Encounter.objects.exists():
        parser.error('Formal-load database is not empty; existing data was preserved')

    credential = secrets.token_urlsafe(24)
    accounts = []
    manifest = {'seed': SEED, 'database': settings.DATABASES['default']['NAME'], 'stores': []}
    with transaction.atomic():
        for shop in range(5):
            store = Store.objects.create(id=ref('store', shop, 0), name=f'合成验收门店{shop + 1}')
            roles = {}
            for role in ('manager', 'frontdesk', 'doctor'):
                count = 2 if role == 'doctor' else 1
                roles[role] = []
                for person in range(count):
                    username = f'load_{shop + 1}_{role}_{person + 1}'
                    account = Account.objects.create_user(username=username, password=credential,
                                                          store=store, role=role)
                    roles[role].append(account)
                    accounts.append({'username': username, 'role': role, 'store_ref': str(store.pk),
                                     'account_ref': account.pk})

            definition = json.loads((CODE / 'examples/01_完整定义需求_v0.2.json').read_text(encoding='utf-8'))
            definition['scope']['store_id'] = str(store.pk)
            compiled = compile_demand(definition)
            if not compiled['ok']:
                raise RuntimeError('Synthetic template compilation failed')
            demand = Demand.objects.create(store=store, actor=roles['manager'][0], reason='合成正式负载准备',
                                           input_digest=compiled['candidate']['input_digest'], definition=definition,
                                           report={'ok': True, 'synthetic': True})
            template = Candidate.objects.create(store=store, demand=demand, envelope=compiled['candidate'],
                                                digest=compiled['candidate']['content_digest'], state='approved',
                                                submitted_by=roles['manager'][0], submitted_at=timezone.now(),
                                                submit_reason='合成正式负载准备')
            Review.objects.create(candidate=template, actor=roles['manager'][0], decision='approve',
                                  digest=template.digest, reason='仅合成压测数据')
            PolicyHead.objects.create(store=store, candidate=template, generation=1)
            Activation.objects.create(store=store, candidate=template, actor=roles['manager'][0],
                                      digest=template.digest, generation=1, reason='仅合成压测数据')

            pets = [PetIdentity(id=ref('pet', shop, number), store=store) for number in range(1200)]
            add_many(PetIdentity, pets)
            encounters, receptions, records, revisions = [], [], [], []
            checks, hits, alerts, outbox, meds = [], [], [], [], []
            for number in range(4000):
                doctor = roles['doctor'][0] if shop == 0 else roles['doctor'][number % 2]
                encounter_id = ref('encounter', shop, number)
                record_id = ref('record', shop, number)
                pet = pets[number % len(pets)]
                revised = number % 20 == 0
                encounter = Encounter(id=encounter_id, store=store, pet=pet,
                                      receptionist=roles['frontdesk'][0], doctor=doctor, template=template,
                                      species='猫' if number % 2 else '犬', complaint=f'合成主诉 {shop}-{number}',
                                      pet_name=f'合成宠物 {number % 1200}')
                record = ClinicalRecord(id=record_id, encounter=encounter, template=template,
                                        current_version=2 if revised else 1)
                encounters.append(encounter)
                receptions.append(ReceptionRevision(id=ref('reception', shop, number), encounter=encounter,
                                                    version=1, species=encounter.species, complaint=encounter.complaint,
                                                    pet_name=encounter.pet_name, actor=roles['frontdesk'][0]))
                records.append(record)
                revisions.append(ClinicalRevision(id=ref('revision1', shop, number), record=record, version=1,
                                                  diagnosis=f'合成诊断 {shop}-{number}',
                                                  examination='合成检查' if number % 4 == 0 else '', actor=doctor))
                if revised:
                    revisions.append(ClinicalRevision(id=ref('revision2', shop, number), record=record, version=2,
                                                      diagnosis=f'合成更正诊断 {shop}-{number}', actor=doctor,
                                                      reason='合成更正'))
                if number % 10 == 0:
                    check = ClinicalCheck(id=ref('medcheck', shop, number), encounter=encounter, record=record,
                                          actor=doctor, mode='medication', template=template, policy=template,
                                          policy_digest=template.digest, status='accepted', checks=[])
                    checks.append(check)
                    meds.append(MedicationEntry(id=ref('medication', shop, number), record=record, evaluation=check,
                                                drug='合成药品', amount='1', unit='片', actor=doctor))
                if shop == 0 or number % 2 == 0:
                    check = ClinicalCheck(id=ref('guardcheck', shop, number), encounter=encounter, record=record,
                                          actor=doctor, mode='record', template=template, policy=template,
                                          policy_digest=template.digest, status='blocked', checks=[])
                    hit = GuardHit(id=ref('hit', shop, number), evaluation=check, rule_ref='SR-02',
                                   rule_version='1', field='diagnosis', value=None)
                    alert = Alert(id=ref('alert', shop, number), hit=hit)
                    checks.append(check)
                    hits.append(hit)
                    alerts.append(alert)
                    outbox.append(AlertOutbox(id=ref('outbox', shop, number), alert=alert))
            for model, values in ((Encounter, encounters), (ReceptionRevision, receptions),
                                  (ClinicalRecord, records), (ClinicalRevision, revisions),
                                  (ClinicalCheck, checks), (MedicationEntry, meds), (GuardHit, hits),
                                  (Alert, alerts), (AlertOutbox, outbox)):
                add_many(model, values)
            manifest['stores'].append({'store_ref': str(store.pk), 'pets': len(pets),
                                       'encounters': len(encounters), 'records': len(records),
                                       'clinical_revisions': len(revisions), 'medications': len(meds),
                                       'guard_hits': len(hits), 'alerts': len(alerts), 'outbox_pending': len(outbox),
                                       'doctor_record_counts': [4000, 0] if shop == 0 else [2000, 2000]})
    private = {'synthetic_only': True, 'password': credential, 'accounts': accounts,
               'manifest': manifest}
    with output.open('x', encoding='utf-8') as stream:
        json.dump(private, stream, ensure_ascii=False, indent=2)
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == '__main__':
    main()
