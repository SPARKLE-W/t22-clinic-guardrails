"""Measure one complete authorized 4000-record CSV download."""
import argparse
import csv
import hashlib
import io
import json
import time
import uuid
from pathlib import Path

import requests

NAMESPACE = uuid.UUID('23863255-7eb6-4692-96e2-cb87d8dbf54c')


def ref(number):
    return str(uuid.uuid5(NAMESPACE, f'20261024:record:0:{number}'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--private-accounts', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base', default='http://127.0.0.1:18830')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Result already exists')
    private = json.loads(args.private_accounts.read_text(encoding='utf-8'))
    if not private.get('synthetic_only'):
        parser.error('Only synthetic fixture is allowed')
    session = requests.Session()
    session.get(args.base + '/app', timeout=20).raise_for_status()
    username = 'load_1_doctor_1'
    response = session.post(args.base + '/api/login',
                            json={'username': username, 'password': private['password']},
                            headers={'X-CSRFToken': session.cookies['csrftoken']}, timeout=20)
    response.raise_for_status()
    start = time.perf_counter()
    response = session.post(args.base + '/api/exports/clinical-records',
                            json={'scope': {'record_refs': [ref(n) for n in range(4000)]},
                                  'purpose': '合成正式负载单次全范围探针'},
                            headers={'X-CSRFToken': session.cookies['csrftoken']},
                            timeout=120, stream=True)
    first = None
    chunks = []
    for chunk in response.iter_content(chunk_size=65536):
        if chunk:
            if first is None:
                first = time.perf_counter()
            chunks.append(chunk)
    end = time.perf_counter()
    body = b''.join(chunks)
    valid = response.status_code == 200
    if valid:
        valid = (len(body) == int(response.headers['Content-Length']) == int(response.headers['X-Byte-Count']) and
                 hashlib.sha256(body).hexdigest() == response.headers['X-Content-SHA256'])
        rows = len(list(csv.reader(io.StringIO(body.decode('utf-8'))))) - 1
        valid = valid and rows == 4000
    else:
        rows = None
    result = {'status': response.status_code, 'ok': valid, 'rows': rows, 'bytes': len(body),
              'first_byte_ms': (first - start) * 1000 if first else None,
              'complete_ms': (end - start) * 1000,
              'sha256_match': valid if response.status_code == 200 else None}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
