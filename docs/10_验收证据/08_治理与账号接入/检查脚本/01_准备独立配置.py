"""One-time local provision; never print credentials or touch probe schemas."""
import json
import secrets
from pathlib import Path
import subprocess

private = Path.home() / '.cache/t22-runtime/private'
target = private / 'app.json'
if target.exists():
    print('Existing private app configuration reused.')
else:
    runtime = json.loads((private.parent / 'runtime.json').read_text(encoding='utf-8-sig'))
    command = [str(Path(runtime['mysql_home']) / 'bin/mysql.exe'), '--defaults-extra-file=' + str(private / 'mysql-root.cnf'),
               '--protocol=MEMORY', '--shared-memory-base-name=t22_probe_admin', '--batch', '--skip-column-names']

    def sql(statement):
        result = subprocess.run(command, input=statement, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            (private / 'app-provision-error.log').write_text(result.stderr, encoding='utf-8')
            raise RuntimeError('Local provisioning failed; private log retained.')
        return result.stdout.strip()

    count = sql("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema IN ('t22_app','t22_app_test');")
    if int(count):
        raise RuntimeError('Unowned nonempty app database; refusing to provision over it.')
    password = secrets.token_hex(32)
    sql("CREATE DATABASE IF NOT EXISTS t22_app CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;\n"
        "CREATE USER 't22_app'@'127.0.0.1' IDENTIFIED BY '" + password + "';\n"
        "GRANT ALL ON t22_app.* TO 't22_app'@'127.0.0.1';\n"
        "GRANT ALL ON t22_app_test.* TO 't22_app'@'127.0.0.1';")
    config = {'secret_key': secrets.token_urlsafe(48), 'database': {
        'HOST': '127.0.0.1', 'PORT': 13322, 'NAME': 't22_app', 'USER': 't22_app', 'PASSWORD': password}}
    target.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
    print('Provisioned isolated app database and private configuration; no probe tables modified.')
