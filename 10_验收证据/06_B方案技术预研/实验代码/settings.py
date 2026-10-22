"""Isolated PRE lab. Never load production records or design JSON as active policy."""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
RUNTIME = Path(os.environ.get('T22_RUNTIME', str(Path.home()/'.cache'/'t22-runtime')))
connections_file = RUNTIME/'private'/'connections.json'
config = json.loads(connections_file.read_text(encoding='utf-8-sig'))
db = config['mysql']
secret_path = RUNTIME/'private'/'django.key'
if not secret_path.exists():
    import secrets
    secret_path.write_text(secrets.token_urlsafe(48), encoding='utf-8')
SECRET_KEY = secret_path.read_text(encoding='utf-8')
DEBUG = False
ALLOWED_HOSTS = ['127.0.0.1', 'localhost', 'testserver']
ROOT_URLCONF = 'urls'
INSTALLED_APPS = ['django.contrib.auth','django.contrib.contenttypes','django.contrib.sessions','rest_framework','probe']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware','django.contrib.sessions.middleware.SessionMiddleware','django.middleware.common.CommonMiddleware','django.middleware.csrf.CsrfViewMiddleware','django.contrib.auth.middleware.AuthenticationMiddleware']
DATABASES = {'default':{
    'ENGINE':'django.db.backends.mysql','HOST':db['host'],'PORT':db['port'],
    'NAME':db['database'],'USER':db['user'],'PASSWORD':db['password'],
    'CONN_MAX_AGE':60,'CONN_HEALTH_CHECKS':True,
    'OPTIONS':{'charset':'utf8mb4','isolation_level':'read committed','init_command':"SET sql_mode='STRICT_TRANS_TABLES', default_storage_engine=INNODB"},
    'TEST':{'NAME':'t22_probe_test'},
}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
USE_TZ = True
TIME_ZONE = 'UTC'
PASSWORD_HASHERS = ['django.contrib.auth.hashers.PBKDF2PasswordHasher']
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Strict'
SESSION_COOKIE_SECURE = False  # loopback HTTP lab only; product HTTPS setting is separate.
CSRF_COOKIE_SAMESITE = 'Strict'
REST_FRAMEWORK = {'DEFAULT_AUTHENTICATION_CLASSES':['rest_framework.authentication.SessionAuthentication'],
                  'DEFAULT_PERMISSION_CLASSES':['rest_framework.permissions.IsAuthenticated'],
                  'DEFAULT_RENDERER_CLASSES':['rest_framework.renderers.JSONRenderer']}
LOGGING = {'version':1,'disable_existing_loggers':False,'handlers':{'null':{'class':'logging.NullHandler'}},'loggers':{'django.request':{'handlers':['null'],'propagate':False}}}
