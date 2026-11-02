"""检查定稿产物与设计文档引用；不运行生成器、产品接口或负载。"""
import hashlib
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
import fitz

project = Path(__file__).resolve().parents[3]
stage = Path(__file__).resolve().parents[1]
design = project / '04_设计与控制文件'
checks = []

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()

def check(name, passed, detail=''):
    checks.append({'check':name,'passed':bool(passed),'detail':detail})

srs = project / '03_需求规格说明书.md'
baseline = 'FE95C5BF2495B763EA2C7075C46CBE5D367F7F82401E2E1818EF81BF9243DAE8'
check('需求v1.0指纹未变',sha(srs)==baseline,sha(srs))
check('冻结快照逐字节一致',srs.read_bytes()==(stage/'02_需规v1.0_基线快照.md').read_bytes())
old = stage.parent / '04_需规v0.8完整审查' / '04_需规v0.8_完整复核快照.md'
check('v0.8历史快照指纹保持',sha(old)=='7AA60AB61F2521876E65F31EA94A087F9A7A4A08B8F6F02E390A29FD595CA5C8')
export = stage/'定稿导出'
stats = json.loads((export/'05_导出结构检查.json').read_text(encoding='utf-8-sig'))
actual = json.loads((export/'06_Word排版实查.json').read_text(encoding='utf-8-sig'))
check('Word导出源关联v1.0',stats['sourceSHA256']==baseline)
word = project/'03_需求规格说明书.docx'
pdf = export/'03_需求规格说明书_v1.0_预览.pdf'
check('Word与实际排版记录指纹一致',sha(word)==actual['docxSHA256'])
check('PDF与实际排版记录指纹一致',sha(pdf)==actual['pdfSHA256'])
with zipfile.ZipFile(word) as z:
    check('Word压缩包完整',z.testzip() is None)
    roots = [ET.fromstring(z.read(n)) for n in z.namelist() if n.endswith('.xml') or n.endswith('.rels')]
    check('Word全部XML可解析',len(roots)>0,f'{len(roots)} XML/relationship parts')
    doc = ET.fromstring(z.read('word/document.xml'))
    ns = {'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    check('Word52表及8图',len(doc.findall('.//w:tbl',ns))==52 and len([n for n in z.namelist() if n.startswith('word/media/')])==8)
with fitz.open(pdf) as p:
    check('PDF40页且无空白文本页',len(p)==40 and all(page.get_text().strip() for page in p))
    bad = []
    for i,page in enumerate(p):
        for b in page.get_text('blocks'):
            if b[4].strip() and (b[0]<-1 or b[1]<-1 or b[2]>page.rect.width+1 or b[3]>page.rect.height+1):
                bad.append(i+1)
    check('PDF文本块无页面越界',not bad,str(bad))

markdowns = sorted(design.glob('*.md'))
check('首轮设计8份Markdown',len(markdowns)==8)
texts = {p.name:p.read_text(encoding='utf-8-sig') for p in markdowns}
check('设计文档围栏闭合',all(t.count('```')%2==0 for t in texts.values()))
matrix = texts['02_权限矩阵与字段投影.md']
keys = re.findall(r'^\| UC-[^|]+\| `([^`]+)`',matrix,re.M)
check('矩阵20个唯一权限键',len(keys)==20 and len(set(keys))==20)
inp_path = design/'控制文件'/'01_演示需求输入_v0.1.json'
out_path = design/'控制文件'/'02_控制包候选_v0.1.json'
inp = json.loads(inp_path.read_text(encoding='utf-8-sig'))
out = json.loads(out_path.read_text(encoding='utf-8-sig'))
check('两个JSON可解析',isinstance(inp,dict) and isinstance(out,dict))
check('输入引用摘要对应实际文件',out['input']['sha256']==sha(inp_path))
check('两个样例需求基线正确',inp['srs']['sha256']==out['srs']['sha256']==baseline)
check('三必需部分完整且输入输出一致',all(k in inp and inp[k] and inp[k]==out[k] for k in ['safety_redlines','coding_standards','quality_gates']))
check('SR01—10各一项及独立版本',sorted(r['id'] for r in out['safety_redlines'])==[f'SR-{i:02}' for i in range(1,11)] and all(r.get('version') for r in out['safety_redlines']))
bindings = out['authorization']['bindings']
check('JSON/矩阵/输入20键一致',len(bindings)==20 and set(b['permission'] for b in bindings)==set(keys)==set(inp['permission_keys']))
check('JSON默认拒绝且仅三角色',out['authorization']['default']=='deny' and {b['role'] for b in bindings}=={'frontdesk','doctor','manager'})
registry = set(out['authorization']['projection_registry']['names'])
used = {s for b in bindings for s in b['input_projections']+b['output_projections']}
check('投影引用全部登记并有D2定义',used<=registry and all(s in matrix for s in used),f'{len(used)} projection names')
apis = texts['03_数据与接口设计.md']
api_keys = set(re.findall(r'^\| (?:GET|POST) [^|]+\| ([a-z_.]+)',apis,re.M))
check('API权限键与矩阵一致',api_keys==set(keys))
check('辅助模式与只读投影已登记',all(s in matrix and s in out_path.read_text(encoding='utf-8') for s in ['Q-CREATE-AUX','R-CREATE-AUX','Q-GOVERNANCE','Q-ASSIGNMENT','doctor_options','verify_pet_ref']))
check('用药及决定响应为无正文结果投影',all(b['output_projections']==['R-WRITE-RESULT'] for b in bindings if b['goal'] in ['UC-03','UC-04.1','UC-04.2']))
check('无虚构审批/启用或生成成功',out['status']=='design_draft' and out['validation']['status']=='not_run' and all(out[k] is None for k in ['submitted_actor','approval','activation','content_digest']))
check('屏幕基准与视口记录分开',all(x['quality_gates']['browser']=={'os':'Windows','families':['Edge','Chrome'],'minimum_screen_resolution':[1366,768],'window':'maximized','zoom_percent':100,'actual_viewport':'record_during_test'} for x in [inp,out]))
check('题定质量数字为目标',out['quality_gates']['status']=='targets_not_results' and out['quality_gates']['load']=={'concurrency':360,'duration_seconds':600,'p95_ms_max':280,'throughput_per_minute_min':800,'authorized_failures_max':0,'timing_end':'complete_response_received','report_separately':['reception','query','clinical_write','guard_check','export']} and out['quality_gates']['coverage']['minimum_percent']==80)
trace = texts['05_需求到设计追踪矩阵.md']
required = [f'{prefix}-{i:02}' for prefix,n in [('FR',8),('NFR',9),('AC',8)] for i in range(1,n+1)]
check('追踪表含全部FR/NFR/AC行',all(re.search(r'^\| '+r+ r' \|',trace,re.M) for r in required),f'{len(required)} rows')
check('rule_id加入同轮去重',all('attempt_id, rule_id, rule_version, field' in texts[n] or 'attempt_id,rule_id,rule_version,field' in texts[n] for n in ['03_数据与接口设计.md','04_关键流程与时序设计.md']))
check('并发与FloorSpec修正已写入',all(s in apis for s in ['共享读锁','排他锁','再次比较','固定模式']) and all(s in texts['01_控制文件格式与生成校验设计.md'] for s in ['FloorSpec','无条件调度','非底线扩展字段','O(RF＋G＋I)']))
check('未生成产品实现目录',not (project/'05_系统代码').exists())

navs = [project/'00_作业导航.md',project/'01_T22要求核对与验收清单.md',project/'02_T22完成流程与人机任务分工.md',project/'07_AI使用留痕'/'04_设计阶段协作与决策记录.md',project.parent/'README.md',stage/'07_首轮设计综合处置.md',stage/'09_本机预研条件盘点.md']
broken = []
for path in markdowns+navs:
    txt = path.read_text(encoding='utf-8-sig')
    for target in re.findall(r'(?<!!)\[[^\]]*\]\(([^)]+)\)',txt):
        target = target.strip('<>').split('#')[0]
        if not target or re.match(r'(?:https?|app|codex|mailto):',target):
            continue
        dest = Path(target) if re.match(r'^[A-Za-z]:[/\\]',target) else path.parent/target
        if not dest.exists(): broken.append(f'{path.name}: {target}')
check('设计及当前导航的本地文档链接可达',not broken,broken)
check('三份实际审查报告存在',all((stage/n).exists() for n in ['04_定稿与设计交付覆盖审查.md','05_角色与控制设计一致性审查.md','06_事务规则与架构设计审查.md']))

inventory = {str(p.relative_to(project)).replace('\\','/'):sha(p) for p in markdowns+[inp_path,out_path]}
result = {'scope':'document_integrity_and_design_reference_checks_only','sourceSHA256':sha(srs),'passed':sum(c['passed'] for c in checks),'total':len(checks),'checks':checks,'designSHA256':inventory,'runtime_tests_executed':False,'generator_executed':False,'mermaid_rendered':False,'full_OOXML_schema_validation':False}
(stage/'08_基线与设计检查结果.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
rows = ['# 需求基线与设计文档检查','', '> 2026-10-21。检查对象是定稿文件、设计文本和样例引用；不验证业务实现、生成器或性能。','',f'实际检查：{result["passed"]}/{result["total"]}项通过。','', '| 检查项 | 结果 |','| --- | --- |']
rows += [f'| {c["check"]} | {"通过" if c["passed"] else "失败"} |' for c in checks]
rows += ['', '原始结果及首轮设计逐文件SHA256见[08检查结果](08_基线与设计检查结果.json)；检查脚本留在本目录检查脚本子目录。','', 'Word已在本轮实际通过Microsoft Word 16.0打开、更新目录并导PDF（见定稿导出日志）；本次再次核对文件指纹、ZIP/XML结构、表图和40页PDF文本范围。未运行全量OOXML schema工具，不能冒称完整schema验证。首末页实际查看及v0.8全40页视觉检查沿用定稿记录中的真实范围。','', '设计Mermaid图保留源码，未正式渲染导图；JSON投影引用尚未编译机器字段模式。三份代理完整初审和修后定点意见见04—06报告，主代理逐项处置另见07。以上检查不表示全部设计已冻结或系统已满足任何AC实测条款。','']
(stage/'03_需求基线与设计文档检查.md').write_text('\n'.join(rows),encoding='utf-8')
print(json.dumps({'passed':result['passed'],'total':result['total'],'failures':[c for c in checks if not c['passed']]},ensure_ascii=False))
raise SystemExit(0 if result['passed']==result['total'] else 1)
