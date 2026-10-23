"""Report source line counts with explicit, non-passing measurement scopes."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CODE = ROOT / '05_系统代码'


def useful_lines(path):
    marker = '#' if path.suffix == '.py' else '//' if path.suffix == '.js' else None
    return sum(bool(line.strip()) and (marker is None or not line.lstrip().startswith(marker))
               for line in path.read_text(encoding='utf-8').splitlines())


def total(paths):
    paths = list(paths)
    return {'files': len(paths), 'lines': sum(useful_lines(path) for path in paths)}


python = [path for path in CODE.rglob('*.py')
          if 'migrations' not in path.parts and 'tests' not in path.parts and path.name != 'tests.py'
          and not path.name.startswith('test_') and path.name != 'mq_test_tools.py']
javascript = list((CODE / 'frontend').rglob('*.js'))
javascript = [path for path in javascript if 'tests' not in path.parts]
html = list((CODE / 'frontend').rglob('*.html'))
css = list((CODE / 'frontend').rglob('*.css'))
report = {'method': 'physical nonblank lines, excluding whole-line Python # and JS // comments',
          'excluded': 'tests, migrations, mq_test_tools.py, generated/binary files',
          'backend_python': total(python), 'frontend_javascript': total(javascript),
          'frontend_html': total(html), 'frontend_css': total(css)}
report['backend_plus_javascript'] = report['backend_python']['lines'] + report['frontend_javascript']['lines']
report['backend_plus_all_frontend'] = report['backend_plus_javascript'] + report['frontend_html']['lines'] + report['frontend_css']['lines']
print(json.dumps(report, ensure_ascii=False, indent=2))
