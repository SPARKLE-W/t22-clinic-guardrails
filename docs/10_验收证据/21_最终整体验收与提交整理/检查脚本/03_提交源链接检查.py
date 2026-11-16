"""Check relative Markdown links in the current submission entrypoints."""
import re
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[4]
DOCUMENTS = [
    'README.md', '00_作业导航.md',
    '03_需求规格说明书.md', '06_测试与实测报告.md',
    '08_个人研究分析报告.md', '09_运行与提交说明.md',
    '04_设计与控制文件/21_AI可执行规格提交索引与说明.md',
    '04_设计与控制文件/22_系统设计报告.md',
    '10_验收证据/21_最终整体验收与提交整理/01_计划与验收目标.md',
    '10_验收证据/21_最终整体验收与提交整理/02_最终源码前后端复验.md',
    '10_验收证据/21_最终整体验收与提交整理/03_三视角交叉审查与处置.md',
    '10_验收证据/21_最终整体验收与提交整理/04_个人AI留痕核对候选.md',
    '10_验收证据/21_最终整体验收与提交整理/05_提交规则符合性评审.md',
    '10_验收证据/21_最终整体验收与提交整理/06_PR45审查与提交材料补强记录.md',
    '11_最终提交候选/00_成果与文件清单.md',
    '11_最终提交候选/01_本人验收页.md',
]


def main():
    checked = 0
    missing = []
    historical_sources = []
    for name in DOCUMENTS:
        document = ROOT / (name if name == 'README.md' else 'docs/' + name)
        for match in re.finditer(r'\[[^\]]+\]\(([^)]+)\)', document.read_text(encoding='utf-8')):
            target = unquote(match.group(1).split('#', 1)[0]).strip('<>')
            if not target or target.startswith(('https://', 'http://', 'mailto:')):
                continue
            checked += 1
            resolved = document.parent / target
            if not resolved.exists():
                # Frozen SRS cites original course files moved after its approval.
                original_names = {'T22_门诊护栏规则_学生版任务书.docx', '大作业要求（完整汇总）.docx'}
                original = Path('D:/大三/课程/软件设计与开发/大作业/00_来源摘录/原始材料') / Path(target).name
                if name == '03_需求规格说明书.md' and Path(target).name in original_names and original.is_file():
                    historical_sources.append((name, target, str(original)))
                else:
                    missing.append((name, target))
    print(f'documents={len(DOCUMENTS)} local_links={checked} missing={len(missing)}')
    print(f'historical_source_references={len(historical_sources)} (original files verified at relocated source directory)')
    for source, target in missing:
        print(f'{source}: {target}')
    if missing:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
