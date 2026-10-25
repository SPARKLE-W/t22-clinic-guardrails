"""Check relative Markdown links in the current submission entrypoints."""
import re
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[3]
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
    '11_最终提交候选/00_成果与文件清单.md',
    '11_最终提交候选/01_本人验收页.md',
]


def main():
    checked = 0
    missing = []
    for name in DOCUMENTS:
        document = ROOT / name
        for match in re.finditer(r'\[[^\]]+\]\(([^)]+)\)', document.read_text(encoding='utf-8')):
            target = unquote(match.group(1).split('#', 1)[0]).strip('<>')
            if not target or target.startswith(('https://', 'http://', 'mailto:')):
                continue
            checked += 1
            resolved = document.parent / target
            if not resolved.exists():
                missing.append((name, target))
    print(f'documents={len(DOCUMENTS)} local_links={checked} missing={len(missing)}')
    for source, target in missing:
        print(f'{source}: {target}')
    if missing:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
