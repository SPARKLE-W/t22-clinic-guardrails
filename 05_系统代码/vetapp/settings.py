"""Local course application. Credentials and databases are outside source."""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ['T22_APP_CONFIG'])
config = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
SECRET_KEY = config['secret_key']
DEBUG = False
ALLOWED_HOSTS = ['127.0.0.1', 'localhost', 'testserver']
ROOT_URLCONF = 'vetapp.urls'
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions', 'rest_framework', 'governance', 'clinic']
AUTH_USER_MODEL = 'governance.Account'
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware',
              'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware',
              'django.contrib.auth.middleware.AuthenticationMiddleware', 'governance.entry.PermissionEntryMiddleware']
DATABASES = {'default': {**config['database'], 'ENGINE': 'django.db.backends.mysql', 'CONN_MAX_AGE': 0,
                        'OPTIONS': {'charset': 'utf8mb4', 'isolation_level': 'read committed',
                                    'init_command': "SET sql_mode='STRICT_TRANS_TABLES', default_storage_engine=INNODB"},
                        'TEST': {'NAME': 't22_app_test'}}}
database_name = DATABASES['default']['NAME']
isolated_load = os.environ.get('T22_FORMAL_LOAD') == '1' and database_name.startswith('t22_perf_')
if database_name != 't22_app' and not isolated_load:
    raise RuntimeError('Use the isolated t22_app database; existing probe databases are excluded.')
if isolated_load:
    DATABASES['default']['TEST']['NAME'] = database_name + '_test'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
USE_TZ = True
TIME_ZONE = 'Asia/Shanghai'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Strict'
CSRF_COOKIE_SAMESITE = 'Strict'
# Loopback HTTP only. Use secure cookies when deploying behind HTTPS.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SESSION_COOKIE_AGE = 8 * 60 * 60
DATA_UPLOAD_MAX_MEMORY_SIZE = 1200000
REST_FRAMEWORK = {'DEFAULT_AUTHENTICATION_CLASSES': ['rest_framework.authentication.SessionAuthentication'],
                  'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.IsAuthenticated'],
                  'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer']}
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'APP_DIRS': True,
              'OPTIONS': {'context_processors': ['django.template.context_processors.request', 'django.contrib.auth.context_processors.auth']}}]
LOGGING = {'version': 1, 'disable_existing_loggers': False, 'handlers': {'null': {'class': 'logging.NullHandler'}},
           'loggers': {'django.request': {'handlers': ['null'], 'propagate': False}}}
