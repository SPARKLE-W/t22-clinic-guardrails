"""Check frozen bytes and tracked files for the local runtime's actual secrets."""
import hashlib
import json
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[4]
frozen = {
    '03_需求规格说明书.md': 'FE95C5BF2495B763EA2C7075C46CBE5D367F7F82401E2E1818EF81BF9243DAE8',
    '03_需求规格说明书.docx': '844778341B7EB43907FF43CB08AD6B76DB6A9ACE2C77D9B02A028C895958B011',
}
for name, expected in frozen.items():
    if hashlib.sha256((root/name).read_bytes()).hexdigest().upper() != expected:
        raise RuntimeError('Frozen baseline changed: '+name)

secrets = set()
def collect(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in ['password', 'secret_key'] and isinstance(item, str) and len(item) >= 8:
                secrets.add(item.encode())
            else:
                collect(item)
    elif isinstance(value, list):
        for item in value:
            collect(item)

private = Path.home()/'.cache/t22-runtime/private'
for name in ['app.json', 'app-mq.json', 'app-demo-accounts.json']:
    collect(json.loads((private/name).read_text(encoding='utf-8-sig')))
files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
for name in filter(None, files):
    data = (root/name).read_bytes()
    if any(secret in data for secret in secrets):
        raise RuntimeError('Private credential detected in tracked file: '+name)
print(json.dumps({'frozen_baselines':len(frozen), 'tracked_files_checked':len(list(filter(None, files))),
                  'actual_runtime_secret_check':'passed'}))
