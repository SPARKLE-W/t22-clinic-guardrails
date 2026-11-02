"""Author shipped contracts from reviewed definitions; not the production compiler."""
import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
DATA = PROJECT / '05_系统代码/vetcontrols/data'
STAGE = Path(__file__).resolve().parent.parent


def normalized(value, key=''):
    if isinstance(value, dict):
        return {k: normalized(v, k) for k, v in sorted(value.items())}
    if isinstance(value, list):
        items = [normalized(v) for v in value]
        return items if key == 'minimum_screen_resolution' else sorted(items, key=lambda v: json.dumps(v, sort_keys=True))
    return value


def write(name, data):
    (DATA / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


floor = normalized(json.loads((DATA / 'base_floor.json').read_text(encoding='utf-8')))
auth = normalized(json.loads((DATA / 'permission_floor.json').read_text(encoding='utf-8'))['authorization'])
proposal = json.loads((STAGE / '04_检查器与事实登记提案.json').read_text(encoding='utf-8'))
registry = {
    'version': '0.2', 'status': 'static_contract_runtime_not_integrated',
    'srs_sha256': floor['srs']['sha256'], 'phases': proposal['phase_registry'],
    'checkers': [{k: item[k] for k in ('id', 'checker', 'phases', 'failure_class')} for item in floor['safety_redlines']],
    'facts': {name: {k: spec[k] for k in ('type', 'provider', 'phases', 'condition_selectable', 'availability', 'enum', 'items') if k in spec}
              for name, spec in proposal['facts'].items()},
}
for name in ['audit.facility_ready', 'audit.required_plan_complete', 'audit.same_transaction_planned', 'outcome.business_audit_atomic']:
    registry['facts'][name]['phases'] = sorted(set(registry['facts'][name]['phases']) | {'decision_change', 'reception_change'})
for name, enum in [('principal.role', ['frontdesk', 'doctor', 'manager']), ('operation.mode', sorted({v['mode'] for v in proposal['operations'].values() if 'mode' in v}))]:
    registry['facts'][name] = {'type': 'string', 'provider': 'authorization_entry', 'phases': registry['phases'],
                              'condition_selectable': True, 'availability': 'trusted_service_input', 'enum': enum}
write('registry.json', registry)


def exact(value):
    return {'const': value}


def shaped(value):
    if isinstance(value, dict):
        return {'type': 'object', 'required': list(value), 'additionalProperties': False,
                'properties': {k: shaped(v) for k, v in value.items()}}
    return exact(value)


def obj(properties):
    return {'type': 'object', 'required': list(properties), 'additionalProperties': False, 'properties': properties}


identifier = {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$'}
q = shaped(floor['quality_gates'])


def bound(section, name, minimum, maximum=None, integer=False):
    spec = {'type': 'integer' if integer else 'number', 'minimum': minimum}
    if maximum is not None:
        spec['maximum'] = maximum
    q['properties'][section]['properties'][name] = spec


bound('coverage', 'minimum_percent', 80, 100)
bound('effective_code_lines', 'minimum', 3200, 4800, True)
bound('effective_code_lines', 'maximum', 3200, 4800, True)
for key, minimum in [('stores', 5), ('pets_approx', 6000), ('records_approx', 20000)]:
    bound('data', key, minimum, integer=True)
for key, minimum in [('concurrency', 360), ('duration_seconds', 600), ('throughput_per_minute_min', 800)]:
    bound('load', key, minimum, integer=True)
bound('load', 'p95_ms_max', 0.001, 280)
bound('load', 'authorized_failures_max', 0, 0, True)
for key in ['rule_files_max', 'modules_max', 'person_days_max']:
    bound('extension_per_example', key, 0, 1)
q['properties']['browser']['properties']['minimum_screen_resolution'] = {
    'type': 'array', 'prefixItems': [{'type': 'integer', 'minimum': 1, 'maximum': x} for x in [1366, 768]],
    'minItems': 2, 'maxItems': 2,
}
t = shaped(floor['templates'])
t['properties']['version'] = identifier
t['properties']['clinical']['properties']['extra_fields'] = {
    'type': 'array', 'maxItems': 64, 'items': obj({
        'id': {'type': 'string', 'pattern': '^x_[a-z][a-z0-9_]{0,47}$'},
        'type': {'enum': ['text', 'integer', 'boolean']}, 'required': {'const': False}, 'visible_role': {'const': 'doctor'},
    }),
}
rule = obj({
    'id': {'type': 'string', 'pattern': '^EXT-[A-Za-z0-9_-]{1,48}$'}, 'version': identifier,
    'checker': {'enum': [item['checker'] for item in registry['checkers']]},
    'phases': {'type': 'array', 'minItems': 1, 'maxItems': 11, 'uniqueItems': True, 'items': {'enum': registry['phases']}},
    'source': {'type': 'array', 'minItems': 1, 'maxItems': 10, 'uniqueItems': True, 'items': {'enum': sorted({s for r in floor['safety_redlines'] for s in r['source']})}},
    'depends_on': {'type': 'array', 'maxItems': 128, 'uniqueItems': True, 'items': identifier},
    'when': {'type': 'object'},
})
schema = obj({
    'format_version': {'const': '0.2'}, 'demand_id': identifier, 'version': identifier,
    'scope': obj({'store_id': {'type': 'string', 'format': 'uuid'}, 'synthetic': {'type': 'boolean'}}),
    'srs': exact(floor['srs']), 'safety_redlines': exact(floor['safety_redlines']),
    'coding_standards': exact(floor['coding_standards']), 'quality_gates': q, 'templates': t,
    'collaboration': exact(floor['collaboration']), 'authorization': exact(auth),
    'additional_rules': {'type': 'array', 'maxItems': 128, 'items': rule},
})
schema['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
schema['$id'] = 'urn:t22:control-demand:0.2'
write('demand.schema.json', schema)
print('Authored registry.json and demand.schema.json; no candidate generated.')
