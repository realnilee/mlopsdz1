"""Намеренная поломка в копии проекта: быстрый прогон обязан завершиться с ненулевым кодом."""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ['release.py', 'lab.py', 'service.py', 'api_contract.py', 'examples_fixture.py']
FAST = 'public and integration'


def run(project):
    code = 0
    for args in (['-s', 'tests'], ['-s', 'tests_integration', '-t', '.']):
        done = subprocess.run([sys.executable, '-m', 'unittest', 'discover', *args], cwd=project,
                              capture_output=True, text=True, encoding='utf-8', errors='replace')
        print(f'unittest {" ".join(args)} -> exit {done.returncode}: {done.stderr.strip().splitlines()[-1]}')
        code = code or done.returncode
    return code


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    copy = Path(tmp) / 'project'
    copy.mkdir()
    for name in FILES:
        shutil.copy(ROOT / name, copy / name)
    for name in ('tests', 'tests_integration', 'examples'):
        shutil.copytree(ROOT / name, copy / name, ignore=shutil.ignore_patterns('__pycache__'))
    print('--- без поломки')
    clean = run(copy)
    source = (copy / 'release.py').read_text(encoding='utf-8')
    broken = source.replace("report.get('passed') is True", "report.get('passed')")
    assert broken != source
    (copy / 'release.py').write_text(broken, encoding='utf-8')
    print("--- поломка: report.get('passed') is True -> report.get('passed')")
    failed = run(copy)
print(f'итог: чистая копия exit={clean}, сломанная exit={failed}')
sys.exit(0 if clean == 0 and failed != 0 else 1)
