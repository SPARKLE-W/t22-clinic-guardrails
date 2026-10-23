"""Fixed loopback WSGI serving profile for the isolated formal-load run."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / '05_系统代码'))

if os.environ.get('T22_FORMAL_LOAD') != '1':
    raise SystemExit('Explicit formal-load mode required')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'vetapp.settings')

from django.core.wsgi import get_wsgi_application
from waitress import serve


if __name__ == '__main__':
    serve(get_wsgi_application(), host='127.0.0.1', port=18830,
          threads=120, connection_limit=512, backlog=1024,
          channel_timeout=60)
