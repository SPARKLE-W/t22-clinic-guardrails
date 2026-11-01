"""Loopback smoke test for default and profiled formal-load service modes."""
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = ROOT / '05_系统代码'
SERVICE = Path(__file__).with_name('05_正式负载服务.py')
sys.path.insert(0, str(APP_ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'vetapp.settings')

import django

django.setup()

from governance.models import Account, Store


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def assert_profile(response, expected):
    profiled = bool(response.headers.get('X-T22-Server-PID'))
    if profiled != expected:
        raise AssertionError(f'Expected profile={expected}, got profile={profiled}')
    if not expected:
        if 'Server-Timing' in response.headers:
            raise AssertionError('Default mode unexpectedly exposed profile headers')
        return None
    timing = response.headers.get('Server-Timing', '')
    for stage in ('app', 'sql', 'commit', 'queue'):
        if f'{stage};dur=' not in timing:
            raise AssertionError(f'Missing {stage} timing header')
    if response.headers.get('X-T22-Profile-Scope') != 'through-start-response; iterable-excluded':
        raise AssertionError('Unexpected profile scope')
    for header in ('X-T22-SQL-Count', 'X-T22-Process-CPU', 'X-T22-App-Start', 'X-T22-App-End'):
        if header not in response.headers:
            raise AssertionError(f'Missing {header}')
    return timing


def run_mode(profile, username, password):
    port = free_port()
    command = [sys.executable, str(SERVICE), '--port', str(port), '--threads', '2']
    if profile:
        command.append('--profile')
    environment = os.environ.copy()
    environment['T22_FORMAL_LOAD'] = '1'
    process = subprocess.Popen(command, cwd=ROOT, env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding='utf-8', errors='replace')
    session = requests.Session()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output, _ = process.communicate()
                raise RuntimeError(f'Formal-load service exited early: {output[-2000:]}')
            try:
                response = session.get(f'http://127.0.0.1:{port}/', timeout=2)
                break
            except requests.RequestException:
                time.sleep(0.1)
        if response is None:
            raise TimeoutError('Formal-load service did not become ready on loopback')
        if response.status_code != 200:
            raise AssertionError(f'Expected HTTP 200, got {response.status_code}')
        assert_profile(response, profile)
        response.close()
        if profile:
            csrf = session.cookies.get('csrftoken')
            login = session.post(f'http://127.0.0.1:{port}/api/login',
                                 json={'username': username, 'password': password},
                                 headers={'X-CSRFToken': csrf}, timeout=5)
            if login.status_code != 200:
                raise AssertionError(f'Synthetic doctor login failed with HTTP {login.status_code}')
            login.close()
            response = session.get(f'http://127.0.0.1:{port}/api/clinical/encounters', timeout=5)
            if response.status_code != 200:
                raise AssertionError(f'Authenticated clinical query failed with HTTP {response.status_code}')
            timing = assert_profile(response, True)
            if int(response.headers['X-T22-SQL-Count']) < 1:
                raise AssertionError('The database query did not increment the profiled SQL count')
            commit = re.search(r'(?:^|,)\s*commit;dur=([0-9]+(?:\.[0-9]+)?)', timing)
            if not commit or float(commit.group(1)) <= 0:
                raise AssertionError('The atomic audited request did not report commit time')
            response.close()
    finally:
        session.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


if os.environ.get('T22_APP_CONFIG') is None:
    raise SystemExit('CI app configuration must be set before this smoke test')
store = Store.objects.create(name=f'CI profile smoke {uuid.uuid4().hex[:12]}')
username = f'profile_smoke_{uuid.uuid4().hex[:12]}'
password = secrets.token_urlsafe(24)
Account.objects.create_user(username=username, password=password, store=store, role='doctor')
run_mode(False, username, password)
run_mode(True, username, password)
print('Default and profiled formal-load service modes passed loopback smoke.')
