import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE','settings')
from django.core.wsgi import get_wsgi_application
from waitress import serve

if __name__=='__main__':
    serve(get_wsgi_application(),host='127.0.0.1',port=18722,threads=32,connection_limit=512,channel_timeout=45)
