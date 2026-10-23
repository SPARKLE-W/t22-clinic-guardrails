"""Controlled synthetic source initialization; never adopts or writes clinical data."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from governance.services import write_audit
from clinic.models import AuditTarget, Encounter, Suggestion

SOURCE = {'type':'demo','version':'demo-v1','label':'合成演示建议，非真实模型输出，不用于真实诊疗','generation_method':'synthetic_fixture'}
SAMPLES = [
    ('diagnosis',{'diagnosis':'合成演示诊断，待医生判断','examination':'合成演示检查，非真实检查结果'}),
    ('medication',{'diagnosis':'合成演示诊断，待医生判断','medication':{'drug':'合成演示用药','amount':'1','unit':'演示单位'}}),
]


class Command(BaseCommand):
    help = 'Save two explicitly synthetic suggestions for a chosen demo encounter, without clinical writes.'

    def add_arguments(self, parser):
        parser.add_argument('--encounter-ref',required=True)

    def handle(self, *args, **options):
        from governance.services import _uuid, DomainError
        try:
            ref = _uuid(options['encounter_ref'])
        except DomainError as error:
            raise CommandError(error.code) from None
        with transaction.atomic():
            row = Encounter.objects.select_for_update().filter(pk=ref).first()
            if not row:
                raise CommandError('Encounter not found; no suggestions saved.')
            count = 0
            for sample_key,content in SAMPLES:
                _,created = Suggestion.objects.get_or_create(encounter=row,source_version=SOURCE['version'],sample_key=sample_key,
                    defaults={'source':SOURCE,'content':content})
                count += int(created)
            if count:
                event = write_audit(None,'suggestion.demo_seed','success','SYNTHETIC_SOURCE_INITIALIZED',row.pk,row.store_id)
                AuditTarget.objects.create(event=event,target=row.pk,kind='encounter')
            self.stdout.write(f'Saved {count} synthetic suggestions; no clinical records were written.')
