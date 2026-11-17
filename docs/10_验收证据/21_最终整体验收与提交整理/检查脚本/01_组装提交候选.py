"""Build a review-only T22 bundle from a merged, clean Git revision."""
import hashlib
import argparse
import base64
import io
import xml.etree.ElementTree as ET
import json
import os
import posixpath
import re
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[4]
EXTRAS = [
    'README.md', '00_作业导航.md', '.github/workflows/ci.yml',
    '03_需求规格说明书.md', '03_需求规格说明书.docx',
    '04_设计与控制文件/00_设计导航与基线.md',
    '04_设计与控制文件/architecture.md',
    '04_设计与控制文件/02_权限矩阵与字段投影.md',
    '04_设计与控制文件/03_数据与接口设计.md',
    '04_设计与控制文件/04_关键流程与时序设计.md',
    '04_设计与控制文件/05_需求到设计追踪矩阵.md',
    '04_设计与控制文件/08_控制包机器契约与静态编译.md',
    '04_设计与控制文件/19_人机分工登记与复核接口.md',
    '04_设计与控制文件/21_AI可执行规格提交索引与说明.md',
    '04_设计与控制文件/21_AI可执行规格提交索引与说明.docx',
    '04_设计与控制文件/22_系统设计报告.md',
    '04_设计与控制文件/22_系统设计报告.docx',
    '04_设计与控制文件/23_报告插图/01_系统分层.png',
    '04_设计与控制文件/23_报告插图/02_数据与历史.png',
    '04_设计与控制文件/23_报告插图/03_临床提交时序.png',
    '06_测试与实测报告.md', '06_测试与实测报告.docx',
    '08_个人研究分析报告.md', '08_个人研究分析报告.docx',
    '09_运行与提交说明.md', '09_运行与提交说明.docx',
    '07_AI使用留痕/01_需求阶段协作记录.md',
    '07_AI使用留痕/02_子代理原始任务提示词.md',
    '07_AI使用留痕/03_需规结构修订与调研记录.md',
    '07_AI使用留痕/19_后端综合验收与联调边界记录.md',
    '10_验收证据/07_控制包编译与安全校验/08_交付证据与基线检查.md',
    '10_验收证据/08_治理与账号接入/03_证据与基线检查.md',
    '10_验收证据/12_处方用药追加与更正/03_本批结果.md',
    '10_验收证据/13_演示建议与人工决定/03_本批结果.md',
    '10_验收证据/14_前端界面与浏览器验证/02_本批结果.md',
    '10_验收证据/14_前端界面与浏览器验证/03_检查结果.json',
    '10_验收证据/14_前端界面与浏览器验证/05_前端最终验收与合并记录.md',
    '10_验收证据/19_后端综合验收/01_正式负载测量协议_v1.0.md',
    '10_验收证据/19_后端综合验收/05_正式负载第二轮实测.md',
    '10_验收证据/19_后端综合验收/08_隔离队列停用恢复实测.md',
    '10_验收证据/19_后端综合验收/11_共享演示库0009升级与冒烟.md',
    '10_验收证据/19_后端综合验收/12_维护性规则与模板变更试验.md',
    '10_验收证据/19_后端综合验收/14_280ms门槛与代码规模口径评估.md',
    '10_验收证据/19_后端综合验收/21_AC06原文口径与性能改进决策.md',
    '10_验收证据/19_后端综合验收/22_AC06课程口径与实施计划.md',
    '10_验收证据/19_后端综合验收/24_AC06课程口径v1.1完整验收.md',
    '10_验收证据/19_后端综合验收/原始结果/code_scale_07.json',
    '10_验收证据/19_后端综合验收/检查脚本/04_生成正式负载数据.py',
    '10_验收证据/19_后端综合验收/检查脚本/05_正式负载服务.py',
    '10_验收证据/19_后端综合验收/检查脚本/11_课程口径HTTP负载.py',
    '10_验收证据/19_后端综合验收/检查脚本/16_生成测试报告Word.js',
    '10_验收证据/21_最终整体验收与提交整理/02_最终源码前后端复验.md',
    '10_验收证据/21_最终整体验收与提交整理/04_个人AI留痕核对候选.md',
    '10_验收证据/21_最终整体验收与提交整理/原始结果/01_三角色HTTP冒烟.json',
    '10_验收证据/21_最终整体验收与提交整理/原始结果/02_最终源码后端覆盖率.txt',
    '10_验收证据/21_最终整体验收与提交整理/检查脚本/04_生成设计报告图.py',
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
REPORTS = [
    '03_需求规格说明书', '06_测试与实测报告', '08_个人研究分析报告',
    '09_运行与提交说明', '04_设计与控制文件/21_AI可执行规格提交索引与说明',
    '04_设计与控制文件/22_系统设计报告',
]



def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)


def sha(data):
    return hashlib.sha256(data).hexdigest().upper()


def portable_markdown(name, original, bundled_names):
    """Keep traceable references without leaving dead local links in the selected bundle."""
    count = {'repository': 0, 'machine': 0}

    def replace(match):
        image, label, raw_target = match.groups()
        target = raw_target.split('#', 1)[0].strip('<>')
        if not target or target.startswith(('https:', 'http:', 'mailto:')):
            return match.group(0)
        if image and '/10_验收证据/' in target:
            relocated = 'docs/10_验收证据/' + target.split('/10_验收证据/', 1)[1]
            if relocated in bundled_names:
                count['machine'] += 1
                return f'![{label}]({posixpath.relpath(relocated, posixpath.dirname(name))})'
        if re.match(r'^[A-Za-z]:[/\\]', target) or target.startswith('file:'):
            count['machine'] += 1
            source_name = Path(target.replace('\\', '/')).name
            if image:
                return f'图：{label}（见随附Word；原图文件{source_name}未随包）'
            return f'{label}（原始来源：{source_name}；未随包）'
        if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', target):
            return match.group(0)
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(target)))
        if resolved in bundled_names or any(
                item.startswith(resolved.rstrip('/') + '/') for item in bundled_names):
            return match.group(0)
        if image:
            count['repository'] += 1
            return f'图：{label}（完整仓库路径：{resolved}）'
        count['repository'] += 1
        return f'{label}（完整仓库路径：{resolved}）'

    text = original.decode('utf-8')
    rewritten = re.sub(r'(!?)\[([^\]]+)\]\(([^)]+)\)', replace, text)
    return rewritten.encode('utf-8'), count


def check_bundled_markdown_links(entries):
    """Fail if any local Markdown link would point outside the selected bundle."""
    names = set(entries)
    count = 0
    for name, content in entries.items():
        if not name.endswith('.md'):
            continue
        for match in re.finditer(r'!?\[[^\]]+\]\(([^)]+)\)', content.decode('utf-8')):
            target = match.group(1).split('#', 1)[0].strip('<>')
            if not target or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', target):
                continue
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(target)))
            if resolved not in names and not any(
                    item.startswith(resolved.rstrip('/') + '/') for item in names):
                raise ValueError(f'Broken selected-bundle link in {name}: {target}')
            count += 1
    return count


def add_report_cover(path, title, identity):
    """Apply reusable layout to the generated report."""
    import runpy
    runpy.run_path(str(Path(__file__).with_name('06_课程报告排版.py')))['format_report'](path, title, identity)



def refresh_word_fields(path):
    """Finalize only this generated report in a hidden Word instance."""
    target = str(path.resolve()).replace("'", "''")
    script = """$ErrorActionPreference='Stop';
$word=New-Object -ComObject Word.Application;
$word.Visible=$false; $word.DisplayAlerts=0; $word.AutomationSecurity=3;
try {
 $doc=$word.Documents.Open('""" + target + """',$false,$false);
 try {
  for($i=1;$i -le $doc.Sections.Count;$i++) {
   $section=$doc.Sections.Item($i);
   $section.PageSetup.DifferentFirstPageHeaderFooter=0;
   $section.Footers.Item(1).LinkToPrevious=$false;
   $footer=$section.Footers.Item(1).Range;
   $footer.Text=''; $footer.ParagraphFormat.Alignment=1;
   if($i -ge 2){
    $section.Footers.Item(1).PageNumbers.RestartNumberingAtSection=$true;
    $section.Footers.Item(1).PageNumbers.StartingNumber=1;
    if($i -eq 2){$section.Footers.Item(1).PageNumbers.NumberStyle=1}else{$section.Footers.Item(1).PageNumbers.NumberStyle=0};
    $null=$footer.Fields.Add($footer,33);
   }
  }
  $doc.Repaginate();
  foreach($toc in $doc.TablesOfContents){$toc.Update()};
  $null=$doc.Fields.Update(); $doc.Save();
  $doc.ExportAsFixedFormat([IO.Path]::ChangeExtension($doc.FullName,'.pdf'),17);
 } finally {$doc.Close(0)}
} finally {$word.Quit(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($word)}
"""
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    subprocess.run(['powershell', '-NoProfile', '-EncodedCommand', encoded],
                   check=True, timeout=180, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def selected_files(tracked):
    """Keep canonical repository paths; generated Word never serves as source."""
    files = {name if name in {'README.md', '.github/workflows/ci.yml'} else 'docs/' + name
             for name in EXTRAS if not name.endswith('.docx')}
    files.update(name for name in tracked if name.startswith(('backend/', 'frontend/')))
    files.update(name for name in tracked if any(name.startswith('docs/' + prefix)
                 for prefix in EXTRA_PREFIXES) and '历史_修正前/' not in name)
    files.add('AGENTS.md')
    files.add('docs/T22_课程实验报告.md')
    files.update(name for name in tracked if name.startswith('docs/04_设计与控制文件/23_报告插图/'))
    files.update(name for name in tracked if name.startswith('docs/07_AI使用留痕/') and name.endswith('.md'))
    files.update('docs/10_验收证据/19_后端综合验收/' + name for name in ('16_端点专有错误码与拒绝审计增量.md', '18_安全红线负例与凭据持久化修复.md'))
    files.add('docs/10_验收证据/21_最终整体验收与提交整理/原始结果/09_最终课程活跃会话20261117.json')
    files.add('docs/10_验收证据/21_最终整体验收与提交整理/检查脚本/05_课程窗口独立复算.py')
    files.add('docs/10_验收证据/21_最终整体验收与提交整理/原始结果/08_最终版本代码规模20261116.json')
    files.add('docs/10_验收证据/19_后端综合验收/检查脚本/01_代码规模.py')
    files.update('docs/10_验收证据/05_需求基线与首轮设计/图源/' + name for name in ('02_三角色目标总览_v1.0.png', '03_共用与条件关系_v1.0.png', '07_业务上下文_v1.0.png', '08_审批启用状态_v1.0.png', '09_AI建议状态_v1.0.png', '10_告警两维状态_v1.0.png'))
    files.add(Path(__file__).relative_to(ROOT).as_posix())
    files.add(Path(__file__).with_name('06_课程报告排版.py').relative_to(ROOT).as_posix())
    files.add('docs/10_验收证据/21_最终整体验收与提交整理/检查脚本/03_提交源链接检查.py')
    files.add('docs/10_验收证据/21_最终整体验收与提交整理/原始结果/07_最终版本本地回归20261116.json')
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Validate selection without generating files')
    parser.add_argument('--preview', action='store_true', help='Build a clearly marked branch preview')
    parser.add_argument('--refresh-word-fields', action='store_true', help='Use installed Windows Word to cache TOC and page numbers')
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--identity-file', type=Path, help='External JSON with student-supplied cover fields')
    parser.add_argument('--evidence-dir', type=Path, help='Verified final course raw evidence directory')
    args = parser.parse_args()
    identity = json.loads(args.identity_file.read_text(encoding='utf-8-sig')) if args.identity_file else {}
    if not isinstance(identity, dict) or any(not isinstance(v, str) or not v.strip() for v in identity.values()):
        raise SystemExit('Cover identity must be a JSON object of nonempty text fields.')
    commit = git('rev-parse', 'HEAD').decode().strip()
    branch = git('branch', '--show-current').decode().strip()
    tracked = set(git('ls-files', '-z').decode().rstrip('\0').split('\0'))
    files = selected_files(tracked)
    missing = sorted(name for name in files if name not in tracked or not (ROOT / name).is_file())
    if missing:
        raise SystemExit(f'Missing tracked sources: {missing}')
    entries = {name: (ROOT / name).read_bytes() for name in sorted(files)}
    evidence_index = json.loads(entries['docs/10_验收证据/21_最终整体验收与提交整理/原始结果/09_最终课程活跃会话20261117.json'])
    if args.evidence_dir:
        evidence_root = args.evidence_dir.resolve()
        for item in evidence_index['files']:
            path = (evidence_root / item['path']).resolve()
            if not path.is_relative_to(evidence_root):
                raise SystemExit('Raw evidence path escapes its directory.')
            data = path.read_bytes()
            if sha(data).lower() != item['sha256'].lower():
                raise SystemExit('Raw evidence hash mismatch: ' + item['path'])
            entries['docs/10_验收证据/21_最终整体验收与提交整理/负载原始结果/final20261117/' + item['path']] = data
    elif not args.check:
        raise SystemExit('Final measured evidence is required: supply --evidence-dir.')
    changes = []
    for name in sorted(entries):
        if name.endswith('.md'):
            original = entries[name]
            entries[name], counts = portable_markdown(name, original, set(entries))
            if any(counts.values()):
                changes.append({'path': name, 'source_sha256': sha(original), **counts})
    working_links = check_bundled_markdown_links(entries)
    if args.check:
        print(json.dumps({'files': len(files), 'working_local_links': working_links,
                          'report_sources': len(REPORTS), 'word_reports': 1, 'git_commit': commit}, ensure_ascii=False))
        return
    if git('status', '--porcelain', '--', *sorted(files)).strip():
        raise SystemExit('Commit selected sources before building.')
    if branch != 'main' and not args.preview:
        raise SystemExit('Final candidates require merged main; use --preview for a branch draft.')
    if args.output_dir is None or args.output_dir.resolve().drive.lower() not in ('d:', 'e:'):
        raise SystemExit('Specify --output-dir on D: or E:, outside the source repository.')
    output = args.output_dir.resolve()
    if output.is_relative_to(ROOT):
        raise SystemExit('Generated bundles must stay outside the source repository.')
    tag = ('预览' if args.preview else '提交候选') + '_' + commit[:7]
    stage = output / tag
    if stage.exists() or (output / (tag + '.zip')).exists():
        raise SystemExit('Output already exists; evidence is never overwritten.')
    stage.mkdir(parents=True)
    for name, data in entries.items():
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    combined_name = 'docs/T22_课程实验报告.md'
    source = stage / combined_name
    source.write_bytes(entries[combined_name])
    target = source.with_suffix('.docx')
    subprocess.run(['pandoc', str(source), '-f', 'gfm', '-t', 'docx', '--standalone',
                    '--resource-path=' + str(source.parent), '-o', str(target)], check=True, cwd=stage)
    add_report_cover(target, '社区宠物医院门诊病历与护栏规则系统的设计与实现', identity)
    if args.refresh_word_fields:
        refresh_word_fields(target)
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() or 'word/document.xml' not in archive.namelist():
            raise RuntimeError('Invalid integrated Word report')
    entries[target.relative_to(stage).as_posix()] = target.read_bytes()
    if args.refresh_word_fields:
        entries[target.with_suffix('.pdf').relative_to(stage).as_posix()] = target.with_suffix('.pdf').read_bytes()
    working_links = check_bundled_markdown_links(entries)
    manifest = {'purpose': 'Review preview' if args.preview else 'Student acceptance candidate',
                'git_commit': commit, 'branch': branch, 'preview': args.preview,
                'created_at_utc': datetime.now(timezone.utc).isoformat(),
                'markdown_portability': changes, 'working_local_links': working_links,
                'cover_fields_supplied': bool(identity),
                'word_fields_refreshed': args.refresh_word_fields,
                'word_source': 'Single authored course report with seven chapters and two supporting appendices',
                'files': [{'path': name, 'bytes': len(data), 'sha256': sha(data)}
                          for name, data in sorted(entries.items())]}
    data = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    entries['manifest.json'] = data
    (stage / 'manifest.json').write_bytes(data)
    target = output / (tag + '.zip')
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in sorted(entries.items()):
            archive.writestr('T22/' + name, data)
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        for name, data in entries.items():
            assert sha(archive.read('T22/' + name)) == sha(data)
    print(json.dumps({'path': str(target), 'git_commit': commit, 'files': len(entries),
                      'bytes': target.stat().st_size, 'sha256': sha(target.read_bytes())}, ensure_ascii=False))


if __name__ == '__main__':
    main()
