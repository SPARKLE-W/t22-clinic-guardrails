"""Print counts and sample digests without exposing synthetic clinical text."""
import argparse
import hashlib
import json
from pathlib import Path

import MySQLdb


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app-config', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.app_config.read_text(encoding='utf-8'))['database']
    db = MySQLdb.connect(host=config['HOST'], port=int(config['PORT']), user=config['USER'],
                         passwd=config['PASSWORD'], db=config['NAME'])
    try:
        cursor = db.cursor()
        result = {}
        for table in ('clinic_encounter', 'clinic_clinicalrecord', 'clinic_clinicalrevision',
                      'governance_auditevent', 'clinic_alertoutbox', 'clinic_deliveryreceipt'):
            cursor.execute('SELECT COUNT(*) FROM ' + table)
            result[table] = cursor.fetchone()[0]
        cursor.execute('SELECT id, record_id, version, diagnosis, examination '
                       'FROM clinic_clinicalrevision ORDER BY id LIMIT 100')
        result['revision_sample_sha256'] = hashlib.sha256(repr(cursor.fetchall()).encode('utf-8')).hexdigest().upper()
        cursor.execute('SELECT id, store_id, actor_id, action, outcome, normalized_reason '
                       'FROM governance_auditevent ORDER BY id LIMIT 100')
        result['audit_sample_sha256'] = hashlib.sha256(repr(cursor.fetchall()).encode('utf-8')).hexdigest().upper()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == '__main__':
    main()
