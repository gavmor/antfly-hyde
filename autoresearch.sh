#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export UV_OFFLINE=1 UV_LOCKED=1 NO_COLOR=1

uv run python - <<'PY'
import json
import subprocess
import sys

files = ['app/hyde.py', 'ebooks/monitor.py', 'ebooks/prepare.py', 'reverse/generate.py']

def run(args):
    result = subprocess.run(args, text=True, capture_output=True)
    if result.returncode not in (0, 1):
        sys.stderr.write(result.stderr + result.stdout)
        raise SystemExit(f'command failed: {args!r} (exit {result.returncode})')
    return result

ruff = run(['uv', 'run', 'ruff', 'check', '--no-cache', '--output-format', 'json', *files])
lint = len(json.loads(ruff.stdout))
formatted = 0
for path in files:
    result = run(['uv', 'run', 'ruff', 'format', '--check', path])
    formatted += result.returncode

ty = run(['uv', 'run', 'ty', 'check', '--output-format', 'sarif', '--color', 'never'])
type_issues = sum(1 for run_result in json.loads(ty.stdout)['runs'] for item in run_result['results'] if item.get('level') in ('error', 'warning'))
print(f'METRIC issues={lint + formatted + type_issues}')
print(f'METRIC ruff_diagnostics={lint}')
print(f'METRIC unformatted_files={formatted}')
print(f'METRIC ty_diagnostics={type_issues}')
PY
