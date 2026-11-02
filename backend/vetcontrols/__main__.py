"""Offline CLI: emit drafts and safe reports to a new directory only."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from .compiler import MAX_BYTES, compile_text, parse_text, verify_candidate


def _error(code, message):
    return {'ok': False, 'candidate': None, 'issues': [{'code': code, 'path': '$', 'message': message}]}


def _write(path, value, compact=False):
    options = {'separators': (',', ':')} if compact else {'indent': 2}
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, **options) + '\n', encoding='utf-8', newline='\n')


def main():
    parser = argparse.ArgumentParser(description='T22 static compiler; no submission, approval or activation.')
    subparsers = parser.add_subparsers(dest='operation', required=True)
    compile_parser = subparsers.add_parser('compile')
    compile_parser.add_argument('--input', type=Path, required=True)
    verify_parser = subparsers.add_parser('verify')
    verify_parser.add_argument('--candidate', type=Path, required=True)
    for child in [compile_parser, verify_parser]:
        child.add_argument('--output', type=Path, required=True, help='New result directory; existing directory is refused.')
    args = parser.parse_args()
    try:
        args.output.mkdir(exist_ok=False)
    except OSError:
        print(json.dumps({'ok': False, 'code': 'OUTPUT_UNAVAILABLE', 'message': 'Use a new directory under an existing writable parent.'}))
        return 2
    raw_digest = None
    try:
        source = args.input if args.operation == 'compile' else args.candidate
        with source.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            result = _error('INPUT_TOO_LARGE', 'Input exceeds the 1 MiB definition limit.')
        else:
            raw_digest = hashlib.sha256(raw).hexdigest().upper()
            if args.operation == 'compile':
                result = compile_text(raw)
            else:
                try:
                    result = verify_candidate(parse_text(raw))
                except (ValueError, UnicodeError, RecursionError, OverflowError):
                    result = _error('JSON_INVALID', 'Candidate is not bounded strict UTF-8 JSON.')
    except OSError:
        result = _error('INPUT_UNAVAILABLE', 'Input could not be read.')
    report = {'operation': args.operation, 'ok': result['ok'], 'raw_input_sha256': raw_digest,
              'issues': result['issues'], 'scope': 'static_only_no_runtime_or_quality_acceptance'}
    if result['ok']:
        report['content_digest'] = result['candidate']['content_digest']
        report['state'] = result['candidate']['governance']['state']
    try:
        _write(args.output / 'report.json', report)
        if result['ok'] and args.operation == 'compile':
            temporary = args.output / 'candidate.pending'
            _write(temporary, result['candidate'], compact=True)
            temporary.replace(args.output / 'candidate.json')
    except OSError:
        print(json.dumps({'ok': False, 'code': 'OUTPUT_WRITE_FAILED'}))
        return 2
    print(json.dumps({'ok': result['ok'], 'operation': args.operation, 'issues': len(result['issues']),
                      'scope': 'static_only'}, ensure_ascii=False))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    sys.exit(main())
