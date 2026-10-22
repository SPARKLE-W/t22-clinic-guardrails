import copy
import json
import math
import unittest
from pathlib import Path
from vetcontrols import compile_demand, compile_text, verify_candidate

ROOT = Path(__file__).resolve().parent.parent


def demand():
    return json.loads((ROOT / 'examples/01_完整定义需求_v0.2.json').read_text(encoding='utf-8'))


class CompilerTests(unittest.TestCase):
    def reject(self, data, path=None):
        result = compile_demand(data)
        self.assertFalse(result['ok'])
        self.assertIsNone(result['candidate'])
        self.assertTrue(result['issues'])
        if path:
            self.assertTrue(any(issue['path'].startswith(path) for issue in result['issues']), result['issues'])
        return result

    def test_complete_input_produces_unapproved_static_candidate(self):
        result = compile_demand(demand())
        self.assertTrue(result['ok'], result['issues'])
        candidate = result['candidate']
        self.assertEqual('draft_unsubmitted', candidate['governance']['state'])
        self.assertIsNone(candidate['governance']['submitted_actor'])
        self.assertIsNone(candidate['governance']['approval'])
        self.assertIsNone(candidate['governance']['activation'])
        self.assertEqual('not_integrated', candidate['validation']['runtime_handlers'])
        self.assertEqual('targets_not_results', candidate['payload']['demand']['quality_gates']['status'])
        self.assertEqual(64, len(candidate['content_digest']))
        self.assertTrue(verify_candidate(candidate)['ok'])

    def test_missing_quality_is_located_and_never_filled(self):
        data = demand()
        del data['quality_gates']
        result = self.reject(data, '$.quality_gates')
        self.assertTrue(any(issue['code'] == 'MISSING_REQUIRED' for issue in result['issues']))

    def test_mandatory_checker_cannot_be_replaced_even_when_self_claims_safe(self):
        data = demand()
        data['safety_redlines'][3]['checker'] = 'always_allow'
        self.reject(data, '$.safety_redlines')

    def test_all_ten_redlines_and_three_sections_are_required(self):
        for section in ['safety_redlines', 'coding_standards', 'quality_gates']:
            with self.subTest(section=section):
                data = demand(); del data[section]
                self.reject(data, '$.' + section)
        for index in range(10):
            with self.subTest(redline=index):
                data = demand(); data['safety_redlines'].pop(index)
                self.reject(data, '$.safety_redlines')

    def test_removing_phase_changing_failure_or_wrapping_floor_fails(self):
        for field, value in [('phases',['candidate']), ('failure_class','allow'), ('may_weaken',True), ('when',{'op':'eq','fact':'principal.role','value':'nobody'})]:
            with self.subTest(field=field):
                data = demand(); data['safety_redlines'][0][field] = value
                self.reject(data, '$.safety_redlines')

    def test_twenty_permissions_and_manager_projection_cannot_widen(self):
        for mutation in ['role', 'predicate', 'field', 'mode', 'missing', 'unknown']:
            with self.subTest(mutation=mutation):
                data = demand(); auth = data['authorization']
                if mutation == 'role': auth['bindings'][0]['role'] = 'manager'
                if mutation == 'predicate': auth['predicates']['D'] = 'all_stores'
                if mutation == 'field': auth['projections']['R-AUDIT']['fields']['diagnosis'] = {'type':'text'}
                if mutation == 'mode': auth['bindings'][0]['server_selected_modes'].append('delete')
                if mutation == 'missing': auth['bindings'].pop()
                if mutation == 'unknown': auth['bindings'].append({**auth['bindings'][0], 'permission':'root.all'})
                self.reject(data, '$.authorization')

    def test_coding_obligations_cannot_be_replaced_by_same_ids(self):
        data = demand(); data['coding_standards'][1]['obligations'] = ['allow_sql_concatenation']
        self.reject(data, '$.coding_standards')

    def test_quality_limits_and_targets_identity_are_enforced(self):
        changes = [('coverage','minimum_percent',79), ('load','p95_ms_max',281), ('load','concurrency',359),
                   ('load','duration_seconds',599), ('load','throughput_per_minute_min',799), ('load','authorized_failures_max',1),
                   ('effective_code_lines','minimum',3199), ('effective_code_lines','maximum',4801)]
        for section, key, value in changes:
            with self.subTest(section=section,key=key):
                data = demand(); data['quality_gates'][section][key] = value
                self.reject(data, '$.quality_gates')
        data = demand(); data['quality_gates']['status'] = 'passed'
        self.reject(data, '$.quality_gates')
        data = demand(); data['quality_gates']['all_tests_pass'] = False
        self.reject(data, '$.quality_gates')

    def test_stronger_targets_and_smaller_supported_screen_are_allowed(self):
        data = demand(); data['quality_gates']['coverage']['minimum_percent'] = 85
        data['quality_gates']['load']['p95_ms_max'] = 250
        data['quality_gates']['browser']['minimum_screen_resolution'] = [1280,720]
        self.assertTrue(compile_demand(data)['ok'])
        data['quality_gates']['browser']['minimum_screen_resolution'] = [1920,1080]
        self.reject(data, '$.quality_gates')

    def test_diagnosis_medication_and_human_adoption_floor(self):
        for mutation in ['diagnosis','group','append','adopt','source','old']:
            with self.subTest(mutation=mutation):
                data = demand(); clinical = data['templates']['clinical']
                if mutation == 'diagnosis': clinical['required'] = []
                if mutation == 'group': clinical['medication']['all_or_none'] = ['drug']
                if mutation == 'append': clinical['medication']['storage'] = 'overwrite'
                if mutation == 'adopt': clinical['adoption']['direct_AI_write'] = True
                if mutation == 'source': clinical['adoption']['source_preserved'] = False
                if mutation == 'old': clinical['historic_interpretation'] = 'latest_template'
                self.reject(data, '$.templates')

    def test_extensions_use_registered_types_without_new_roles(self):
        data = demand()
        data['templates']['clinical']['extra_fields'] = [{'id':'x_followup_note','type':'text','required':False,'visible_role':'doctor'}]
        self.assertTrue(compile_demand(data)['ok'])
        for field, value in [('type','python_code'), ('visible_role','manager')]:
            changed = copy.deepcopy(data); changed['templates']['clinical']['extra_fields'][0][field] = value
            self.reject(changed, '$.templates')

    def extra(self, name='EXT-01'):
        return {'id':name,'version':'1','checker':'scope_and_projection','phases':['authorization'],
                'source':['SR-01'],'depends_on':[], 'when':{'op':'eq','fact':'principal.role','value':'doctor'}}

    def test_registered_extra_rule_is_separate_from_unconditional_floor(self):
        data = demand(); data['additional_rules'] = [self.extra()]
        result = compile_demand(data)
        self.assertTrue(result['ok'], result['issues'])
        schedule = result['candidate']['payload']['compiled']['mandatory_schedule']
        self.assertTrue(any(item['rule_id']=='SR-01' for item in schedule['authorization']))
        self.assertNotIn('when', next(item for item in schedule['authorization'] if item['rule_id']=='SR-01'))

    def test_nonclinical_write_phases_keep_audit_contract_without_clinical_rules(self):
        result = compile_demand(demand())
        self.assertTrue(result['ok'])
        schedule = result['candidate']['payload']['compiled']['mandatory_schedule']
        for phase in ['decision_change', 'reception_change']:
            ids = {item['rule_id'] for item in schedule[phase]}
            self.assertIn('SR-06', ids)
            self.assertNotIn('SR-02', ids)
            self.assertNotIn('SR-03', ids)

    def test_after_commit_facts_and_cross_phase_facts_cannot_be_preconditions(self):
        for fact in ['outcome.commit_state', 'template.required_fields_valid']:
            data = demand()
            data['additional_rules'] = [{**self.extra(), 'when': {'op': 'present', 'fact': fact}}]
            self.reject(data, '$.additional_rules')

    def test_conditions_reject_unknown_facts_operators_wrong_types_and_extra_keys(self):
        cases = [{'op':'eval','fact':'principal.role'}, {'op':'eq','fact':'user.__class__','value':'doctor'},
                 {'op':'eq','fact':'principal.role','value':1}, {'op':'any','args':[]},
                 {'op':'present','fact':'principal.role','allow':True}, {'op':'in','fact':'principal.role','values':['doctor',1]}]
        for when in cases:
            with self.subTest(when=when):
                data = demand(); data['additional_rules'] = [{**self.extra(), 'when':when}]
                self.reject(data, '$.additional_rules')

    def test_graph_cycles_missing_references_and_duplicate_ids_fail(self):
        for rules in [[{**self.extra(),'depends_on':['unknown']}],
                      [{**self.extra(),'depends_on':['EXT-02']},{**self.extra('EXT-02'),'depends_on':['EXT-01']}],
                      [self.extra(),self.extra()]]:
            data = demand(); data['additional_rules'] = rules
            self.reject(data, '$.additional_rules')

    def test_bool_is_not_numeric_target_and_nan_is_never_json_number(self):
        data = demand(); data['quality_gates']['coverage']['minimum_percent'] = True
        self.reject(data, '$.quality_gates')
        data = demand(); data['quality_gates']['coverage']['minimum_percent'] = math.nan
        self.reject(data)
        self.assertFalse(compile_text('{"value":NaN}')['ok'])

    def test_strict_json_duplicates_executable_payloads_and_input_limits(self):
        self.assertFalse(compile_text('{"format_version":"0.2","format_version":"0.1"}')['ok'])
        self.assertFalse(compile_text(b'\xff')['ok'])
        self.assertFalse(compile_text('['*2000+']'*2000)['ok'])
        self.assertFalse(compile_text(' '*1048577)['ok'])
        data = demand(); data['sql'] = 'DROP TABLE clinical'
        self.reject(data)
        data = demand(); data['clinical_payload'] = {'diagnosis':'existing clinical content'}
        self.reject(data)
        data = demand(); data['self'] = data
        self.reject(data)

    def test_order_determinism_and_no_input_mutation(self):
        data = demand(); untouched = copy.deepcopy(data)
        first = compile_demand(data)
        reordered = dict(reversed(list(data.items())))
        reordered['safety_redlines'] = list(reversed(data['safety_redlines']))
        second = compile_demand(reordered)
        self.assertTrue(first['ok']); self.assertTrue(second['ok'])
        self.assertEqual(first['candidate'], second['candidate'])
        self.assertEqual(untouched, data)

    def test_tamper_rehashed_candidate_and_fake_approval_are_not_trusted(self):
        result = compile_demand(demand()); self.assertTrue(result['ok'])
        candidate = result['candidate']
        changed = copy.deepcopy(candidate); changed['governance']['state'] = 'active'
        self.assertFalse(verify_candidate(changed)['ok'])
        changed = copy.deepcopy(candidate); changed['payload']['demand']['quality_gates']['load']['p95_ms_max'] = 999
        import hashlib
        changed['content_digest'] = hashlib.sha256(json.dumps(changed['payload'],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest().upper()
        self.assertFalse(verify_candidate(changed)['ok'])

    def test_generated_candidate_always_fits_its_verifier_limits(self):
        data = demand()
        # A bounded input can expand because compiled rules also carry definitions.
        condition = {'op': 'all', 'args': [self.extra()['when'] for _ in range(100)]}
        data['additional_rules'] = [{**self.extra('EXT-' + str(i)), 'when': condition} for i in range(40)]
        result = compile_demand(data)
        if result['ok']:
            self.assertTrue(verify_candidate(result['candidate'])['ok'])
        else:
            self.assertIsNone(result['candidate'])
            self.assertTrue(result['issues'])


if __name__ == '__main__':
    unittest.main()
