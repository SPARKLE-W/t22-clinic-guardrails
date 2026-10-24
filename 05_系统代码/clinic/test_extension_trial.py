"""Separate data-only rule and template changes through governance lifecycle."""
import json
import uuid
from pathlib import Path

from governance.models import Activation, Candidate, PolicyHead, Review
from governance.services import mutate as govern

from .models import GuardHit
from .services import read
from .tests import ClinicFixture
from .writes import diagnosis


class MaintenanceTrialTests(ClinicFixture):
    def spec(self):
        return json.loads((Path(__file__).parents[1] / 'examples/02_维护性规则模板增量.json').read_text(encoding='utf-8'))

    def definition(self):
        value = json.loads((Path(__file__).parents[1] / 'examples/01_完整定义需求_v0.2.json').read_text(encoding='utf-8'))
        value['scope']['store_id'] = str(self.a.pk)
        return value

    def activate(self, definition):
        generated = govern(self.manager.pk, 'governance.candidate.generate', str(uuid.uuid4()),
                           {'definition': definition, 'reason': '合成维护性变更试验'})
        candidate = Candidate.objects.get(pk=generated['candidate_ref'])
        self.assertEqual('draft', candidate.state)
        govern(self.manager.pk, 'governance.candidate.generate', str(uuid.uuid4()),
               {'candidate_ref': str(candidate.pk), 'reason': '合成试验提交'})
        govern(self.manager.pk, 'governance.candidate.review', str(uuid.uuid4()),
               {'candidate_ref': str(candidate.pk), 'content_digest': candidate.digest,
                'decision': 'approve', 'reason': '仅合成试验的程序化批准'})
        self.assertEqual(self.template.pk, PolicyHead.objects.get(store=self.a).candidate_id)
        govern(self.manager.pk, 'governance.candidate.activate', str(uuid.uuid4()),
               {'candidate_ref': str(candidate.pk), 'expected_generation': 1, 'reason': '仅合成试验启用'})
        self.assertEqual(candidate.pk, PolicyHead.objects.get(store=self.a).candidate_id)
        self.assertEqual(self.manager.pk, Review.objects.get(candidate=candidate).actor_id)
        self.assertEqual(self.manager.pk, Activation.objects.get(candidate=candidate).actor_id)
        return candidate

    def test_new_rule_uses_registered_checker_without_product_code_change(self):
        spec = self.spec()
        definition = self.definition()
        definition['additional_rules'] = [spec['additional_rule']]
        self.activate(definition)
        ref = self.create()['encounter_ref']
        blocked = diagnosis(self.doctor.pk, 'record', str(uuid.uuid4()), {}, ref)
        self.assertEqual('GUARD_BLOCKED', blocked['error']['code'])
        self.assertEqual({'SR-02', spec['additional_rule']['id']},
                         set(GuardHit.objects.filter(evaluation__encounter_id=ref).values_list('rule_ref', flat=True)))
        saved = diagnosis(self.doctor.pk, 'record', str(uuid.uuid4()), {'diagnosis': '合成诊断'}, ref)
        self.assertEqual('template-0.2', saved['template_version_ref'])

    def test_new_template_preserves_old_and_new_versions_without_product_code_change(self):
        spec = self.spec()
        old_ref = self.create()['encounter_ref']
        definition = self.definition()
        definition['templates']['version'] = spec['template_version']
        definition['templates']['clinical']['extra_fields'] = [spec['extra_field']]
        self.activate(definition)
        new_ref = self.create()['encounter_ref']
        old = diagnosis(self.doctor.pk, 'record', str(uuid.uuid4()), {'diagnosis': '旧模板记录'}, old_ref)
        self.assertEqual('template-0.2', old['template_version_ref'])
        saved = diagnosis(self.doctor.pk, 'record', str(uuid.uuid4()),
                          {'diagnosis': '合成诊断', 'extra_fields': {'x_followup_note': '一周后复诊'}}, new_ref)
        self.assertEqual(spec['template_version'], saved['template_version_ref'])
        self.assertEqual({'x_followup_note': '一周后复诊'},
                         read(self.doctor.pk, clinical=True, ref=new_ref)['clinical']['extra_fields'])
