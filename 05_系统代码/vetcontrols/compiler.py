"""Pure static control-definition compiler. No runtime authorization or activation."""
import hashlib
import json
import math
import re
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

DATA = Path(__file__).with_name('data')
MAX_BYTES = 1048576
MAX_NODES = 30000
MAX_DEPTH = 64


def _load(name):
    return json.loads((DATA / name).read_text(encoding='utf-8'))


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _normalize(value, key=''):
    if type(value) is dict:
        return {k: _normalize(v, k) for k, v in sorted(value.items())}
    if type(value) is list:
        items = [_normalize(v) for v in value]
        # Contract lists are sets; the screen width/height tuple is ordered.
        return items if key == 'minimum_screen_resolution' else sorted(items, key=lambda v: json.dumps(v, sort_keys=True))
    return value


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest().upper()


FLOOR = _normalize(_load('base_floor.json'))
PERMISSIONS = _normalize(_load('permission_floor.json')['authorization'])
REGISTRY = _load('registry.json')
SCHEMA = _load('demand.schema.json')
Draft202012Validator.check_schema(SCHEMA)
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
CHECKERS = {item['checker']: item for item in REGISTRY['checkers']}
FACTS = REGISTRY['facts']
FLOOR_DIGEST = _digest({'base': FLOOR, 'authorization': PERMISSIONS, 'registry': REGISTRY, 'schema': SCHEMA})


def _check_assets():
    if SCHEMA['properties']['safety_redlines']['const'] != FLOOR['safety_redlines']:
        raise RuntimeError('Shipped schema and safety floor disagree')
    if SCHEMA['properties']['authorization']['const'] != PERMISSIONS:
        raise RuntimeError('Shipped schema and permission floor disagree')
    if len(CHECKERS) != 10 or REGISTRY['srs_sha256'] != FLOOR['srs']['sha256']:
        raise RuntimeError('Shipped registry is inconsistent')
    for rule in FLOOR['safety_redlines']:
        checker = CHECKERS[rule['checker']]
        for key in ['id', 'failure_class']:
            if checker[key] != rule[key]:
                raise RuntimeError('Checker identity disagrees with floor')
        if set(checker['phases']) != set(rule['phases']):
            raise RuntimeError('Checker phases disagree with floor')


_check_assets()


def _path(parts):
    result = '$'
    for part in parts:
        if type(part) is int:
            result += '[' + str(part) + ']'
        else:
            # Never echo an arbitrary property string as an error/log message.
            safe = part if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}', part) else '<field>'
            result += '.' + safe
    return result


def _issue(code, parts=(), message='Definition rejected by the static contract.'):
    return {'code': code, 'path': _path(parts), 'message': message}


def _failed(issues):
    return {'ok': False, 'candidate': None, 'issues': issues[:64]}


class DefinitionError(ValueError):
    pass


def _tree_check(value):
    """Bound API inputs too; allow shared subtrees but never ancestor cycles."""
    count = 0
    ancestors = set()

    def visit(node, depth):
        nonlocal count
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise DefinitionError('tree limit')
        kind = type(node)
        if kind in (dict, list):
            if id(node) in ancestors:
                raise DefinitionError('cyclic input')
            ancestors.add(id(node))
            if kind is dict:
                if any(type(k) is not str for k in node):
                    raise DefinitionError('non-string key')
                children = node.values()
            else:
                children = node
            for child in children:
                visit(child, depth + 1)
            ancestors.remove(id(node))
        elif kind is float:
            if not math.isfinite(node):
                raise DefinitionError('nonfinite number')
        elif kind not in (str, int, bool, type(None)):
            raise DefinitionError('non-json value')

    visit(value, 0)
    if len(_canonical(value)) > MAX_BYTES:
        raise DefinitionError('byte limit')


def _first_difference(expected, actual, parts):
    if type(expected) is dict and type(actual) is dict:
        for key in sorted(set(expected) | set(actual)):
            if key not in expected:
                return parts + ['<field>']
            if key not in actual:
                return parts + [key]
            if expected[key] != actual[key]:
                return _first_difference(expected[key], actual[key], parts + [key])
    if type(expected) is list and type(actual) is list:
        for index, (left, right) in enumerate(zip(expected, actual)):
            if left != right:
                return _first_difference(left, right, parts + [index])
    return parts


def _schema_issues(demand):
    issues = []
    for error in VALIDATOR.iter_errors(demand):
        parts = list(error.absolute_path)
        if error.validator == 'required':
            missing = [k for k in error.validator_value if k not in error.instance]
            issues.extend(_issue('MISSING_REQUIRED', parts + [k], 'Required definition is missing; no default was supplied.') for k in missing)
        elif error.validator == 'const':
            parts = _first_difference(error.validator_value, error.instance, parts)
            issues.append(_issue('FLOOR_CONFLICT', parts, 'Definition differs from a fixed safety obligation.'))
        else:
            issues.append(_issue('SCHEMA_INVALID', parts, 'Definition violates the registered structure, type or bound.'))
        if len(issues) >= 64:
            break
    return issues


def _literal_matches(value, spec):
    kinds = {'boolean': bool, 'integer': int, 'string': str}
    if type(value) is not kinds.get(spec['type']):
        return False
    if type(value) is str and len(value) > 256:
        return False
    return 'enum' not in spec or value in spec['enum']


def _condition_issues(node, phases, parts):
    issues = []
    count = 0

    def walk(item, path, depth):
        nonlocal count
        count += 1
        if depth > 16 or count > 1000 or type(item) is not dict:
            issues.append(_issue('CONDITION_INVALID', path))
            return
        op = item.get('op')
        keys = {'present': {'op', 'fact'}, 'eq': {'op', 'fact', 'value'}, 'in': {'op', 'fact', 'values'},
                'all': {'op', 'args'}, 'any': {'op', 'args'}}
        if type(op) is not str or op not in keys or set(item) != keys[op]:
            issues.append(_issue('CONDITION_INVALID', path))
            return
        if op in ('all', 'any'):
            args = item['args']
            if type(args) is not list or not 1 <= len(args) <= 256:
                issues.append(_issue('CONDITION_INVALID', path + ['args']))
                return
            for index, child in enumerate(args):
                walk(child, path + ['args', index], depth + 1)
            return
        fact = item['fact']
        spec = FACTS.get(fact) if type(fact) is str else None
        if not spec or not spec['condition_selectable'] or not set(phases) <= set(spec['phases']):
            issues.append(_issue('FACT_UNAVAILABLE', path + ['fact'], 'Fact is unregistered, unavailable in this phase, or only observed after commit.'))
            return
        if op == 'present':
            return
        values = [item['value']] if op == 'eq' else item['values']
        if type(values) is not list or not 1 <= len(values) <= 256 or any(not _literal_matches(value, spec) for value in values):
            issues.append(_issue('FACT_TYPE_MISMATCH', path))

    walk(node, parts, 0)
    return issues


def _semantic_issues(demand):
    issues = []
    q = demand['quality_gates']['effective_code_lines']
    if q['minimum'] > q['maximum']:
        issues.append(_issue('RANGE_CONFLICT', ['quality_gates', 'effective_code_lines']))
    extensions = demand['templates']['clinical']['extra_fields']
    if len({field['id'] for field in extensions}) != len(extensions):
        issues.append(_issue('DUPLICATE_ID', ['templates', 'clinical', 'extra_fields']))
    rules = demand['additional_rules']
    ids = [rule['id'] for rule in rules]
    if len(set(ids)) != len(ids):
        issues.append(_issue('DUPLICATE_ID', ['additional_rules']))
    known = set(ids) | {rule['id'] for rule in FLOOR['safety_redlines']}
    for index, rule in enumerate(rules):
        path = ['additional_rules', index]
        if not set(rule['phases']) <= set(CHECKERS[rule['checker']]['phases']):
            issues.append(_issue('CHECKER_PHASE_MISMATCH', path + ['phases']))
        if not set(rule['depends_on']) <= known:
            issues.append(_issue('UNRESOLVED_REFERENCE', path + ['depends_on']))
        issues.extend(_condition_issues(rule['when'], rule['phases'], path + ['when']))
    pending = {rule['id']: set(rule['depends_on']) & set(ids) for rule in rules}
    while pending:
        ready = {name for name, dependencies in pending.items() if not dependencies}
        if not ready:
            issues.append(_issue('REFERENCE_CYCLE', ['additional_rules']))
            break
        pending = {name: dependencies - ready for name, dependencies in pending.items() if name not in ready}
    return issues


def compile_demand(demand):
    """Return one deterministic draft or issues. Never fill absent safety sections."""
    try:
        _tree_check(demand)
        normalized = _normalize(demand)
    except (DefinitionError, RecursionError, UnicodeError, ValueError, OverflowError):
        return _failed([_issue('INPUT_INVALID', message='Input must be bounded, acyclic, finite UTF-8 JSON.')])
    issues = _schema_issues(normalized)
    if issues:
        return _failed(issues)
    issues = _semantic_issues(normalized)
    if issues:
        return _failed(issues)
    schedule = {}
    for rule in normalized['safety_redlines']:
        for phase in rule['phases']:
            schedule.setdefault(phase, []).append({
                'rule_id': rule['id'], 'checker': rule['checker'], 'failure_class': rule['failure_class'],
            })
    payload = {'demand': normalized, 'compiled': {
        'mandatory_schedule': schedule, 'conditional_rules': normalized['additional_rules'],
        'floor_digest': FLOOR_DIGEST,
    }}
    content_digest = _digest(payload)
    candidate = {
        'format_version': '0.2', 'package_id': 'CP-' + content_digest[:24], 'package_version': normalized['version'],
        'input_digest': _digest(normalized), 'content_digest': content_digest, 'payload': payload,
        'governance': {'state': 'draft_unsubmitted', 'submitted_actor': None, 'approval': None, 'activation': None},
        'validation': {'status': 'passed_static_checks', 'runtime_handlers': 'not_integrated', 'quality_results': 'not_measured'},
    }
    try:
        _tree_check(candidate)
        if len(_canonical(candidate)) + 1 > MAX_BYTES:
            raise DefinitionError('serialized candidate limit')
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        return _failed([_issue('CANDIDATE_LIMIT', message='Expanded draft exceeds verifier resource limits; no candidate was emitted.')])
    return {'ok': True, 'candidate': candidate, 'issues': []}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DefinitionError('duplicate property')
        result[key] = value
    return result


def _constant(_value):
    raise DefinitionError('nonfinite number')


def parse_text(text):
    """Strict shared parser for definitions and candidate verification CLI."""
    if type(text) is bytes:
        if len(text) > MAX_BYTES:
            raise DefinitionError('byte limit')
        text = text.decode('utf-8', errors='strict')
    if type(text) is not str or len(text) > MAX_BYTES or len(text.encode('utf-8')) > MAX_BYTES:
        raise DefinitionError('text limit')
    value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    _tree_check(value)
    return value


def compile_text(text):
    try:
        demand = parse_text(text)
    except (ValueError, RecursionError, UnicodeError, OverflowError):
        return _failed([_issue('JSON_INVALID', message='Input is not bounded strict UTF-8 JSON; duplicate properties are forbidden.')])
    return compile_demand(demand)


def verify_candidate(candidate):
    """Recompile against shipped independent floor; hashes are not approvals."""
    try:
        _tree_check(candidate)
    except (ValueError, RecursionError, UnicodeError, OverflowError):
        return _failed([_issue('CANDIDATE_INVALID')])
    if type(candidate) is not dict or type(candidate.get('payload')) is not dict or 'demand' not in candidate['payload']:
        return _failed([_issue('CANDIDATE_INVALID')])
    result = compile_demand(candidate['payload']['demand'])
    if not result['ok']:
        return result
    if _normalize(candidate) != _normalize(result['candidate']):
        return _failed([_issue('CANDIDATE_MISMATCH', message='Candidate differs from recompiled draft; metadata and hashes cannot grant approval.')])
    return result
