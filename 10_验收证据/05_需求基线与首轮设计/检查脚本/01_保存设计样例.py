"""保存人工拟定的设计样例；不是控制文件生成器，也不运行规则或审批。"""
import copy
import hashlib
import json
from pathlib import Path

project = Path(__file__).resolve().parents[3]
folder = project / '04_设计与控制文件' / '控制文件'
folder.mkdir(exist_ok=True)
baseline = hashlib.sha256((project / '03_需求规格说明书.md').read_bytes()).hexdigest().upper()
store = '11111111-1111-4111-8111-111111111111'

bindings = [
 ('UC-01.1','encounter.create','frontdesk','F+',['W-CREATE','Q-CREATE-AUX'],['R-RECEPTION','R-CREATE-AUX'],['create','doctor_options','verify_pet_ref']),
 ('UC-01.2','encounter.read','frontdesk','F','Q-OWN',['R-RECEPTION'],['list','detail']),
 ('UC-01.3','encounter.correct','frontdesk','F','W-RECEPTION',['R-RECEPTION'],['correct']),
 ('UC-01.4','encounter.mark_error','frontdesk','F','W-ERROR',['R-RECEPTION'],['mark']),
 ('UC-02.1','clinical.read','doctor','D','Q-OWN',['R-CLINICAL','R-RECEPTION','R-SUGGESTION'],['list','detail']),
 ('UC-02.2','clinical.diagnosis.write','doctor','D','W-DIAGNOSIS',['R-CLINICAL'],['record','correct']),
 ('UC-03','medication.record','doctor','D','W-MEDICATION',['R-WRITE-RESULT'],['append','correct']),
 ('UC-04.1','clinical.suggestion.adopt','doctor','D','W-ADOPT',['R-WRITE-RESULT'],['adopt']),
 ('UC-04.2','clinical.suggestion.decline','doctor','D','W-DECLINE',['R-WRITE-RESULT'],['decline']),
 ('UC-05','clinical.alert.handle','doctor','D',['Q-ALERT','W-ALERT'],['R-ALERT'],['read','resolve']),
 ('UC-06.1','governance.candidate.review','manager','M',['Q-GOVERNANCE','W-REVIEW'],['R-GOVERNANCE'],['read','review']),
 ('UC-06.2','governance.candidate.activate','manager','M','W-ACTIVATE',['R-GOVERNANCE'],['activate']),
 ('UC-07.1','report.store_summary.read','manager','MG','Q-SUMMARY',['R-SUMMARY'],['read']),
 ('UC-07.2','audit.metadata.read','manager','MA','Q-AUDIT',['R-AUDIT'],['read']),
 ('UC-08','export.clinical','doctor','D','Q-EXPORT-CLINICAL',['CSV-CLINICAL'],['export']),
 ('UC-09.1','export.guard_detail','doctor','D','Q-EXPORT-HIT',['CSV-GUARD-DETAIL'],['export']),
 ('UC-09.2','export.guard_summary','manager','MG','Q-EXPORT-HIT',['CSV-GUARD-SUMMARY'],['export']),
 ('UC-10','governance.candidate.generate','manager','M','W-GENERATE',['R-GOVERNANCE'],['generate']),
 ('UC-11.1','collaboration.assignment.write','manager','M',['Q-ASSIGNMENT','W-ASSIGNMENT'],['R-ASSIGNMENT'],['read','write']),
 ('UC-11.2','collaboration.assignment.review','manager','M','W-ASSIGNMENT-REVIEW',['R-ASSIGNMENT'],['review']),
]

# 投影名只作登记引用；其确切字段/语义由同目录02的白名单定义，运行编译前须固化机器格式。
projection_names = sorted({p for *_, inp, outs, modes in bindings for p in ([inp] if isinstance(inp,str) else inp) + outs})
rules = [
 ('SR-01','scope_and_projection',['candidate','authorization'],'FR-05','security_refusal'),
 ('SR-02','referenced_template',['candidate','clinical_change'],'FR-02','guard_hit_when_evaluated'),
 ('SR-03','append_only_medication',['candidate','clinical_change'],'FR-04','guard_hit_when_evaluated'),
 ('SR-04','human_adoption_and_retention',['candidate','clinical_change'],'FR-06','guard_hit_when_evaluated'),
 ('SR-05','manual_approval_content_binding',['candidate','activation'],'FR-01','governance_refusal'),
 ('SR-06','audit_and_authorized_export',['candidate','read','clinical_change','export'],'FR-08','audit_or_export_fault'),
 ('SR-07','registered_permission_entry',['candidate','release','authorization'],'FR-05.4','security_refusal'),
 ('SR-08','credential_and_parameterized_query',['candidate','release','persistence'],'NFR-06','security_refusal'),
 ('SR-09','durable_alert_and_idempotency',['candidate','clinical_change','delivery'],'NFR-05','recovery_fault'),
 ('SR-10','mandatory_control_sections',['candidate'],'FR-01','candidate_validation_failure'),
]
redlines = [{'id':rid,'version':'0.1','checker':checker,'phases':phases,'source':[src,rid],
             'failure_class':kind,'may_weaken':False} for rid,checker,phases,src,kind in rules]
standards = [
 {'id':'CS-01','requirement':'All protected endpoints use the registered permission entry and server projections.'},
 {'id':'CS-02','requirement':'Parameterized queries; password hashing; never persist raw passwords or request bodies in audit.'},
 {'id':'CS-03','requirement':'Explicit commit of medical mutation and required audit before success response; no required audit in after-response callbacks.'},
 {'id':'CS-04','requirement':'Medication append/correction only; immutable actor/time from trusted context.'},
 {'id':'CS-05','requirement':'No eval, executable SQL, file selectors or arbitrary object traversal in controls.'},
 {'id':'CS-06','requirement':'Outbox and receipt deduplication use stable IDs; publish confirmation is not doctor handling.'},
]
gates = {
 'status':'targets_not_results','required_sections':['safety_redlines','coding_standards','quality_gates'],
 'protected_endpoint_scan':{'missing_permission':'block_release','negative_sample_required':True},
 'coverage':{'minimum_percent':80,'measurement_kind':'to_be_declared_before_measurement'},
 'effective_code_lines':{'minimum':3200,'maximum':4800,'counting_scope':'to_be_declared'},
 'all_tests_pass':True,'all_error_codes_and_negative_scenarios_accounted':True,
 'data':{'stores':5,'pets_approx':6000,'records_approx':20000},
 'load':{'concurrency':360,'duration_seconds':600,'p95_ms_max':280,'throughput_per_minute_min':800,
         'authorized_failures_max':0,'timing_end':'complete_response_received',
         'report_separately':['reception','query','clinical_write','guard_check','export']},
 'extension_per_example':{'rule_files_max':1,'modules_max':1,'person_days_max':1,
                          'both_rule_and_template_examples_required':True},
 'browser':{'os':'Windows','families':['Edge','Chrome'],'minimum_screen_resolution':[1366,768],
            'window':'maximized','zoom_percent':100,'actual_viewport':'record_during_test'},
 'evidence_required':['transaction_audit_faults','queue_recovery','retry_and_redelivery','restart',
                      'clinical_csv_completeness','authorization_and_projection','real_AI_process'],
}
templates = {
 'version':'demo-template-v0.1','source':['FR-02','C-06','C-07'],
 'reception':{
   'required':['pet_id','species','complaint','doctor_id'],'optional':['pet_name'],
   'server_owned':['encounter_id','store_id','receptionist_id','created_at'],
   'owner_fixed_after_creation':True,'pet_id_immutable_after_creation':True,
 },
 'clinical':{
   'required':['diagnosis'],'optional':['examination'],'type':'versioned_text_fields',
   'medication':{'optional_when_no_medication':True,'all_or_none':['drug','amount','unit'],
                 'value_type':'nonempty_text','storage':'append_only',
                 'correction_requires':['original_entry_ref','reason'],'pharmacology_thresholds':None},
   'adoption':{'requires':['saved_suggestion_ref','reason','trusted_doctor'],
               'state_from':'pending','state_to':'adopted','decline_reason_required':False,
               'source_preserved':True,'direct_AI_write':False},
   'first_record_still_requires_diagnosis':True,'historic_interpretation':'referenced_version',
 },
}

demand = {
 'artifact_kind':'manually_authored_design_input','format_version':'0.1','demand_id':'DEMO-DEMAND-001',
 'version':'0.1','scope':{'store_id':store,'synthetic':True},
 'srs':{'version':'1.0','sha256':baseline},'submitted_actor':None,
 'safety_redlines':copy.deepcopy(redlines),'coding_standards':copy.deepcopy(standards),
 'quality_gates':copy.deepcopy(gates),'templates':copy.deepcopy(templates),
 'permission_keys':[b[1] for b in bindings],
 'note':'Synthetic definition requirements, not existing clinical records. No generator has been executed.',
}
input_path = folder / '01_演示需求输入_v0.1.json'
input_path.write_text(json.dumps(demand,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

candidate = {
 'artifact_kind':'manually_authored_expected_output_candidate','status':'design_draft',
 'format_version':'0.1','package_id':'DEMO-CONTROL-001','package_version':'0.1',
 'input':{'id':demand['demand_id'],'version':'0.1','file':input_path.name,
          'sha256':hashlib.sha256(input_path.read_bytes()).hexdigest().upper()},
 'srs':{'version':'1.0','sha256':baseline},'scope':copy.deepcopy(demand['scope']),
 'submitted_actor':None,'approval':None,'activation':None,'content_digest':None,
 'validation':{'status':'not_run','report_ref':None},
 'safety_redlines':redlines,'coding_standards':standards,'quality_gates':gates,
 'templates':templates,
 'authorization':{
   'default':'deny','trusted_identity':'server_session','unknown_fields':'reject',
   'source':'02_权限矩阵与字段投影.md',
   'predicates':{
     'F+':'server principal owns new encounter; verify assigned doctor belongs to same store',
     'F':'Encounter.store == Principal.store AND Encounter.receptionist == Principal.id',
     'D':'Encounter.store == Principal.store AND Encounter.doctor == Principal.id',
     'M':'Object.store == Principal.store AND object is governance or collaboration metadata',
     'MA':'AuditMeta.event_store == Principal.store; select C-06 metadata only',
     'MG':'restrict to Principal.store before aggregation; no clinical or identity detail',
   },
   'projection_registry':{'document':'02_权限矩阵与字段投影.md','section':4,'names':projection_names,
                          'machine_field_schema_status':'not_yet_compiled'},
   'bindings':[{'goal':goal,'permission':key,'role':role,'row_predicate':scope,
                'input_projections':[inp] if isinstance(inp,str) else inp,
                'output_projections':outs,'server_selected_modes':modes}
                for goal,key,role,scope,inp,outs,modes in bindings],
   'cross_store_clinical_refusal':{'source':'C-11','event_store':'trusted_requester_store_only',
                                   'manager_projection':'R-AUDIT','target_ref_opens_clinical_record':False},
   'other_undecided_event_visibility':'retain_security_evidence_without_inventing_store_access',
 },
 'rule_language':{
   'allowed_operators':['present','eq','in','all','any'],
   'registered_checkers':[r['checker'] for r in redlines],
   'selectors':'registered immutable facts only; no arbitrary traversal',
   'executable_content':False,'unregistered_checker':'reject',
   'mandatory_safety_floor':'SRS-v1.0-SR-01-to-SR-10-not-overridable',
   'runtime_alert_classification':'Only evaluated runtime guard hits create clinical alerts; validation/auth/facility faults remain distinct.',
 },
 'collaboration':{'source':['FR-03','C-04'],'owner_role':'manager','scope':'own_store',
                  'dimensions':['content_generation','data_entry','human_decision'],
                  'missing_evidence':'not_comparable','changed_version':'compare_again',
                  'manual_review_required':True,'clinical_text_visible':False},
 'differences':{'status':'design_mapping_only','prior_active_package':None,
                'actual_generator_diff_ref':None},
 'note':'Not executable configuration: projection schemas and checker implementation are future work. No approval, activation, generator success or test result is asserted.',
}
(folder/'02_控制包候选_v0.1.json').write_text(json.dumps(candidate,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'saved_design_examples':2,'permission_bindings':len(bindings),'rules':len(redlines),
                  'generator_executed':False,'runtime_tests_executed':False},ensure_ascii=False))
