from django.core.management.base import BaseCommand
from clinic.export_projection import rebuild_projections
from clinic.models import ClinicalRecord


class Command(BaseCommand):
    help = 'Rebuild derived clinical export prefixes from authoritative source histories.'

    def add_arguments(self, parser):
        parser.add_argument('--batch-size', type=int, default=500)

    def handle(self, *args, **options):
        size = options['batch_size']
        if size < 1:
            raise ValueError('batch-size must be positive')
        ids = list(ClinicalRecord.objects.order_by('pk').values_list('pk', flat=True))
        count = 0
        for start in range(0, len(ids), size):
            count += rebuild_projections(ids[start:start + size])
        self.stdout.write(f'Rebuilt {count} clinical export projections.')
