"""Create synthetic accounts once; secrets are written only to an external file."""
import json
import secrets
import uuid
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from governance.models import Account, PolicyHead, Store


class Command(BaseCommand):
    help = 'Initialize two synthetic stores and three roles. Never resets existing accounts.'

    def add_arguments(self, parser):
        parser.add_argument('--credentials-file', type=Path, required=True)

    def handle(self, *args, **options):
        target = options['credentials_file'].resolve()
        source = Path(__file__).resolve().parents[3]
        if target.is_relative_to(source) or target.exists() or not target.parent.is_dir():
            raise CommandError('Use a new credential file outside the source tree under an existing private directory.')
        if Store.objects.exists() or Account.objects.exists():
            raise CommandError('Database already has accounts; refusal to overwrite or reset.')
        credentials = []
        try:
            with transaction.atomic():
                for label, store_id in [('A', '11111111-1111-4111-8111-111111111111'), ('B', '22222222-2222-4222-8222-222222222222')]:
                    store = Store.objects.create(pk=uuid.UUID(store_id), name=label + '店（演示）')
                    PolicyHead.objects.create(store=store)
                    for role in ['manager', 'doctor', 'frontdesk']:
                        username, password = label.lower() + '_' + role, secrets.token_urlsafe(18)
                        Account.objects.create_user(username=username, password=password, store=store, role=role)
                        credentials.append({'username': username, 'password': password, 'role': role, 'store_id': store_id})
                with target.open('x', encoding='utf-8') as stream:
                    stream.write(json.dumps(credentials, ensure_ascii=False, indent=2) + '\n')
        except OSError:
            raise CommandError('Credential file could not be saved; account transaction was rolled back.') from None
        self.stdout.write('Created 2 synthetic stores and 6 hashed accounts; credentials saved outside source. No candidate approved or activated.')
