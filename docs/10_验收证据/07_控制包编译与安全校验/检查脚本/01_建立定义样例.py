"""Author explicit v0.2 definition fixtures from reviewed design; not the compiler."""
import copy
import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[4]
CODE = PROJECT / 'backend'
old = json.loads((PROJECT / '04_设计与控制文件/控制文件/01_演示需求输入_v0.1.json').read_text(encoding='utf-8-sig'))
candidate = json.loads((PROJECT / '04_设计与控制文件/控制文件/02_控制包候选_v0.1.json').read_text(encoding='utf-8-sig'))
rules = copy.deepcopy(old['safety_redlines'])
for rule in rules:
    if rule['id'] == 'SR-04':
        rule['phases'].append('decision_change')
    if rule['id'] in ('SR-06', 'SR-09'):
        rule['phases'].extend(['decision_change', 'reception_change'])
obligations = [
    ['registered_permission_entry', 'server_projections'],
    ['parameterized_queries', 'password_hashing', 'no_raw_credentials_or_request_bodies_in_audit'],
    ['clinical_audit_atomic_transaction', 'required_commit_before_success', 'no_required_audit_after_response'],
    ['append_only_medication', 'trusted_actor_and_time'],
    ['no_executable_controls', 'no_arbitrary_object_or_file_selectors'],
    ['stable_event_ids', 'outbox_receipt_deduplication', 'publish_confirmation_not_handling'],
]
standards = [{'id': f'CS-{i+1:02}', 'obligations': items} for i, items in enumerate(obligations)]
templates = copy.deepcopy(old['templates'])
templates['version'] = 'template-0.2'
templates['reception']['field_types'] = {'pet_id':'reference', 'species':'text', 'complaint':'text', 'doctor_id':'reference', 'pet_name':'text'}
templates['clinical']['field_types'] = {'diagnosis':'text', 'examination':'text'}
templates['clinical']['extra_fields'] = []
floor = {'version':'0.2', 'srs':old['srs'], 'safety_redlines':rules, 'coding_standards':standards,
         'quality_gates':old['quality_gates'], 'templates':templates, 'collaboration':candidate['collaboration']}
(CODE / 'vetcontrols/data').mkdir(parents=True, exist_ok=True)
(CODE / 'examples').mkdir(exist_ok=True)
(CODE / 'vetcontrols/data/base_floor.json').write_text(json.dumps(floor, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
permissions = json.loads((CODE / 'vetcontrols/data/permission_floor.json').read_text(encoding='utf-8-sig'))
demand = {'format_version':'0.2', 'demand_id':'DEMO-DEMAND-002', 'version':'0.2', 'scope':old['scope'], 'srs':old['srs'],
          **{key:copy.deepcopy(floor[key]) for key in ['safety_redlines','coding_standards','quality_gates','templates','collaboration']},
          'authorization':permissions['authorization'], 'additional_rules':[]}
(CODE / 'examples/01_完整定义需求_v0.2.json').write_text(json.dumps(demand, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
print('Definition fixture and independent base floor authored; no candidate generated or activated.')
