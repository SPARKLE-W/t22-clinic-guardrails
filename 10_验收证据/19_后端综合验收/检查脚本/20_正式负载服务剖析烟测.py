"""Loopback smoke test for default and profiled formal-load service modes."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[3]
SERVICE = Path(__file__).with_name('05_正式负载服务.py')


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def run_mode(profile):
    port = free_port()
    command = [sys.executable, str(SERVICE), '--port', str(port), '--threads', '2']
    if profile:
        command.append('--profile')
    environment = os.environ.copy()
    environment['T22_FORMAL_LOAD'] = '1'
    process = subprocess.Popen(command, cwd=ROOT, env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding='utf-8', errors='replace')
    response = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output, _ = process.communicate()
                raise RuntimeError(f'Formal-load service exited early: {output[-2000:]}')
            try:
                response = requests.get(f'http://127.0.0.1:{port}/', timeout=2)
                break
            except requests.RequestException:
                time.sleep(0.1)
        if response is None:
            raise TimeoutError('Formal-load service did not become ready on loopback')
        if response.status_code != 200:
            raise AssertionError(f'Expected HTTP 200, got {response.status_code}')
        profiled = bool(response.headers.get('X-T22-Server-PID'))
        if profiled != profile:
            raise AssertionError(f'Expected profile={profile}, got profile={profiled}')
        if not profile and 'Server-Timing' in response.headers:
            raise AssertionError('Default mode unexpectedly exposed profile headers')
        if profile:
            timing = response.headers.get('Server-Timing', '')
            for stage in ('app', 'sql', 'commit', 'queue'):
                if f'{stage};dur=' not in timing:
                    raise AssertionError(f'Missing {stage} timing header')
            if response.headers.get('X-T22-Profile-Scope') != 'through-start-response; iterable-excluded':
                raise AssertionError('Unexpected profile scope')
            for header in ('X-T22-SQL-Count', 'X-T22-Process-CPU',
                           'X-T22-App-Start', 'X-T22-App-End'):
                if header not in response.headers:
                    raise AssertionError(f'Missing {header}')
    finally:
        if response is not None:
            response.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


if os.environ.get('T22_APP_CONFIG') is None:
    raise SystemExit('CI app configuration must be set before this smoke test')
run_mode(False)
run_mode(True)
print('Default and profiled formal-load service modes passed loopback smoke.')
