"""Run all Django tests on a dedicated test database, safe under Windows spawn."""
import os
import sys


if __name__ == '__main__':
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'vetapp.settings')
    sys.path.insert(0, os.getcwd())
    from django.conf import settings
    from django.core.management import execute_from_command_line

    settings.DATABASES['default']['TEST']['NAME'] = 't22_app_mq_test'
    assert settings.DATABASES['default']['TEST']['NAME'] == 't22_app_mq_test'
    execute_from_command_line(['manage.py', 'test', 'tests', 'governance', 'clinic', '--noinput', '-v1'])
