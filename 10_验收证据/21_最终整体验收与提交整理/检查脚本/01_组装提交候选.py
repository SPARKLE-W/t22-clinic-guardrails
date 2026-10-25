"""Build a review-only T22 bundle from a merged, clean Git revision."""
import hashlib
import io
import json
import os
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from lxml import etree


ROOT = Path(__file__).resolve().parents[3]
PRIVATE = Path(os.environ.get('T22_FINAL_RAW_DIR', Path.home() / '.cache/t22-runtime/private'))
OUTPUT = ROOT / '11_最终提交候选' / '包体'
EXTRAS = [
    'README.md', 'AGENTS.md', '00_作业导航.md',
    '03_需求规格说明书.md', '03_需求规格说明书.docx',
    '04_设计与控制文件/architecture.md',
    '04_设计与控制文件/02_权限矩阵与字段投影.md',
    '04_设计与控制文件/03_数据与接口设计.md',
    '04_设计与控制文件/04_关键流程与时序设计.md',
    '04_设计与控制文件/05_需求到设计追踪矩阵.md',
    '04_设计与控制文件/08_控制包机器契约与静态编译.md',
    '04_设计与控制文件/21_AI可执行规格提交索引与说明.md',
    '04_设计与控制文件/21_AI可执行规格提交索引与说明.docx',
    '04_设计与控制文件/22_系统设计报告.md',
    '04_设计与控制文件/22_系统设计报告.docx',
    '06_测试与实测报告.md', '06_测试与实测报告.docx',
    '08_个人研究分析报告.md', '08_个人研究分析报告.docx',
    '09_运行与提交说明.md', '09_运行与提交说明.docx',
    '07_AI使用留痕/01_需求阶段协作记录.md',
    '07_AI使用留痕/02_子代理原始任务提示词.md',
    '07_AI使用留痕/03_需规结构修订与调研记录.md',
    '07_AI使用留痕/19_后端综合验收与联调边界记录.md',
    '10_验收证据/07_控制包编译与安全校验/08_交付证据与基线检查.md',
    '10_验收证据/14_前端界面与浏览器验证/02_本批结果.md',
    '10_验收证据/14_前端界面与浏览器验证/03_检查结果.json',
    '10_验收证据/14_前端界面与浏览器验证/05_前端最终验收与合并记录.md',
    '10_验收证据/19_后端综合验收/05_正式负载第二轮实测.md',
    '10_验收证据/19_后端综合验收/08_隔离队列停用恢复实测.md',
    '10_验收证据/19_后端综合验收/12_维护性规则与模板变更试验.md',
    '10_验收证据/19_后端综合验收/14_280ms门槛与代码规模口径评估.md',
    '10_验收证据/19_后端综合验收/22_AC06课程口径与实施计划.md',
    '10_验收证据/19_后端综合验收/24_AC06课程口径v1.1完整验收.md',
    '10_验收证据/19_后端综合验收/原始结果/code_scale_07.json',
    '10_验收证据/19_后端综合验收/检查脚本/04_生成正式负载数据.py',
    '10_验收证据/19_后端综合验收/检查脚本/05_正式负载服务.py',
    '10_验收证据/19_后端综合验收/检查脚本/11_课程口径HTTP负载.py',
    '10_验收证据/21_最终整体验收与提交整理/02_最终源码前后端复验.md',
    '10_验收证据/21_最终整体验收与提交整理/04_个人AI留痕核对候选.md',
    '10_验收证据/21_最终整体验收与提交整理/原始结果/01_三角色HTTP冒烟.json',
    '10_验收证据/21_最终整体验收与提交整理/原始结果/02_最终源码后端覆盖率.txt',
    '11_最终提交候选/00_成果与文件清单.md',
    '11_最终提交候选/01_本人验收页.md',
]
EXTRA_PREFIXES = [
    '04_设计与控制文件/控制文件/03_',
    '04_设计与控制文件/控制文件/04_',
    '04_设计与控制文件/控制文件/05_',
    '04_设计与控制文件/控制文件/06_',
    '04_设计与控制文件/控制文件/07_',
    '10_验收证据/14_前端界面与浏览器验证/03_流程截图/',
]
RAW = {
    'course-formal-20261025-02.requests.jsonl': '52479A6E5581F1948E755B99BEE8B80E5C7A948CB2A1011CA41A26C16035C802',
    'course-formal-20261025-02.summary.json': '0D5079DCE4E3B5517BB248276CC56E53C445EC6C33A6724927173F193487368D',
    'course-formal-20261025-02.inflight.jsonl': 'BA42B7A8929C6555FF43C89D19542A780015BF5DB38D5DC67B8787475D58A4FA',
    'course-formal-20261025-02.resources.jsonl': '45460B80D6914C1D5183A843C23AA8EFF0DB56FA9FA225D62E1609A3134934C1',
}


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)


def sha(data):
    return hashlib.sha256(data).hexdigest().upper()


def portable_srs_word(original):
    """Keep frozen SRS content/images; unlink machine-local D: hyperlinks in bundle copy."""
    source = io.BytesIO(original)
    output = io.BytesIO()
    with zipfile.ZipFile(source) as old:
        rels_name = 'word/_rels/document.xml.rels'
        document_name = 'word/document.xml'
        rels = etree.fromstring(old.read(rels_name))
        document = etree.fromstring(old.read(document_name))
        ids = set()
        for relationship in list(rels):
            target = relationship.get('Target', '')
            if relationship.get('TargetMode') == 'External' and target.lower().startswith('file:'):
                ids.add(relationship.get('Id'))
                rels.remove(relationship)
        r_id = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
        link_name = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}hyperlink'
        removed_links = 0
        for link in document.iter(link_name):
            if link.get(r_id) not in ids:
                continue
            parent = link.getparent()
            position = parent.index(link)
            for child in list(link):
                parent.insert(position, child)
                position += 1
            parent.remove(link)
            removed_links += 1
        if not ids or removed_links == 0:
            raise ValueError('Expected machine-local hyperlinks were not found in frozen SRS Word.')
        with zipfile.ZipFile(output, 'w') as new:
            for entry in old.infolist():
                content = old.read(entry.filename)
                if entry.filename == rels_name:
                    content = etree.tostring(rels, xml_declaration=True, encoding='UTF-8', standalone=True)
                elif entry.filename == document_name:
                    content = etree.tostring(document, xml_declaration=True, encoding='UTF-8', standalone=True)
                new.writestr(entry, content)
    return output.getvalue(), removed_links


def main():
    if git('status', '--porcelain').strip():
        raise SystemExit('Commit and merge changes before creating the candidate bundle.')
    commit = git('rev-parse', 'HEAD').decode().strip()
    branch = git('branch', '--show-current').decode().strip()
    if branch != 'main':
        raise SystemExit('Build only from the merged main branch.')
    tracked = set(git('ls-files', '-z').decode().rstrip('\0').split('\0'))
    files = set(EXTRAS)
    files.update(name for name in tracked if name.startswith('05_系统代码/'))
    files.update(name for name in tracked if any(name.startswith(prefix) for prefix in EXTRA_PREFIXES)
                 and '历史_修正前/' not in name)
    if not files <= tracked:
        raise SystemExit(f'Untracked or missing selected files: {sorted(files - tracked)}')
    entries = {}
    for name in sorted(files):
        path = ROOT / name
        if not path.is_file():
            raise SystemExit(f'Missing file: {name}')
        entries[name] = path.read_bytes()
    srs_source_hash = sha(entries['03_需求规格说明书.docx'])
    entries['03_需求规格说明书.docx'], local_links = portable_srs_word(
        entries['03_需求规格说明书.docx'])
    for name, expected in RAW.items():
        data = (PRIVATE / name).read_bytes()
        if sha(data) != expected:
            raise SystemExit(f'Raw load result changed: {name}')
        entries[f'10_验收证据/21_最终整体验收与提交整理/负载原始结果/{name}'] = data
    manifest = {
        'purpose': 'T22 review candidate; personal report requires the student final sign-off',
        'git_commit': commit,
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'srs_word_portability': {'source_sha256': srs_source_hash,
                                 'machine_local_links_changed_to_plain_text': local_links,
                                 'source_baseline_unchanged': True},
        'files': [{'path': name, 'bytes': len(data), 'sha256': sha(data)}
                  for name, data in sorted(entries.items())],
    }
    entries['manifest.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    target = OUTPUT / f'T22_提交候选_{commit[:7]}_请本人审定.zip'
    if target.exists():
        raise SystemExit(f'Candidate bundle already exists: {target}')
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in sorted(entries.items()):
            archive.writestr(f'T22_提交候选/{name}', data)
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) == len(entries)
    print(json.dumps({'path': str(target), 'git_commit': commit, 'files': len(entries),
                      'bytes': target.stat().st_size, 'sha256': sha(target.read_bytes())}, ensure_ascii=False))


if __name__ == '__main__':
    main()
