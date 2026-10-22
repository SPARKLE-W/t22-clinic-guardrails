"""Explicit synthetic data; no fabricated human approvals."""
import uuid
from django.contrib.auth.models import User
from .models import Store, Profile, PolicyHead, Encounter, Record, Revision

LAB_PASSWORD = 'PRE-only-local-fixture-2026!'


def create_case(tag='case'):
    prefix = tag + '_' + uuid.uuid4().hex[:10]
    store = Store.objects.create(code=prefix)
    PolicyHead.objects.create(store=store)
    result = {'store':store}
    for role in ['doctor','frontdesk','manager']:
        user = User.objects.create_user(username=prefix+'_'+role,password=LAB_PASSWORD)
        Profile.objects.create(user=user,store=store,role=role)
        result[role] = user
    encounter = Encounter.objects.create(store=store,doctor=result['doctor'],receptionist=result['frontdesk'],pet_id=prefix,complaint='PRE：合成主诉，非真实患者')
    record = Record.objects.create(encounter=encounter,diagnosis='PRE old diagnosis "quote",\n旧版')
    Revision.objects.create(record=record,version=1,content={'diagnosis':record.diagnosis,'examination':'','adopted_text':''},actor=result['doctor'])
    result.update(encounter=encounter,record=record)
    return result
