import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_second_backend_exits_before_ledger_initialization_and_recovers_after_exit(tmp_path):
    env = os.environ.copy()
    env.update(BTC_LAB_TEST_MODE='true', BTC_LAB_DATA_DIR=str(tmp_path),
               BTC_LAB_DB=str(tmp_path / 'paper.sqlite3'),
               BTC_LAB_CANDLE_DB=str(tmp_path / 'market-candles.sqlite3'),
               BTC_LAB_STRATEGY_POOL_DB=str(tmp_path / 'strategy-pool.sqlite3'),
               PAPER_AUTOMATION_ENABLED='false', PYTHONUNBUFFERED='1')
    cmd = [sys.executable, '-c', "import backend.app; print('READY', flush=True); input()"]
    first = subprocess.Popen(cmd, cwd=ROOT, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert first.stdout.readline().strip() == 'READY'
        probe = ("import backend.ledger; "
                 "backend.ledger.Ledger.__init__ = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('LEDGER_INITIALIZED')); "
                 "import backend.app")
        second = subprocess.run([sys.executable, '-c', probe], cwd=ROOT,
                                env=env, capture_output=True, text=True, timeout=30)
        assert second.returncode != 0
        assert 'V3_BACKEND_ALREADY_RUNNING' in second.stderr
        assert 'LEDGER_INITIALIZED' not in second.stderr
    finally:
        first.kill()
        first.wait(timeout=30)
    third = subprocess.run([sys.executable, '-c', 'import backend.app'], cwd=ROOT,
                           env=env, capture_output=True, text=True, timeout=30)
    assert third.returncode == 0, third.stderr


def test_repeated_backend_and_frontend_start_are_idempotent(tmp_path):
    script = tmp_path / 'idempotence.ps1'
    lifecycle = str(ROOT / 'scripts' / 'service_lifecycle.ps1').replace("'", "''")
    script.write_text(f"""
$ErrorActionPreference='Stop'
. '{lifecycle}'
function Get-V3ListenerPids([int]$Port) {{ if ($Port -eq 8012) {{ 401 }} else {{ 402 }} }}
function Test-V3OwnedListener([string]$Service, [int]$ProcessId) {{ return $true }}
function Test-V3Health([string]$Service) {{ return $true }}
function Start-Process {{ throw 'DUPLICATE_PROCESS_STARTED' }}
Start-V3Service 'backend'
Start-V3Service 'backend'
Start-V3Service 'frontend'
Start-V3Service 'frontend'
""", encoding='utf-8-sig')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File', str(script)],
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count('already running') == 4


@pytest.mark.parametrize('service', ['backend', 'frontend'])
def test_concurrent_starters_create_only_one_isolated_process(tmp_path, service):
    fake_root = tmp_path / 'fake-v3'
    (fake_root / '.venv' / 'Scripts').mkdir(parents=True)
    (fake_root / '.venv' / 'Scripts' / 'python.exe').touch()
    vite = fake_root / 'frontend' / 'node_modules' / 'vite' / 'bin'
    vite.mkdir(parents=True)
    (vite / 'vite.js').touch()
    marker = tmp_path / 'started.txt'
    script = tmp_path / 'concurrent.ps1'
    lifecycle = str(ROOT / 'scripts' / 'service_lifecycle.ps1').replace("'", "''")
    root = str(fake_root).replace("'", "''")
    marker_path = str(marker).replace("'", "''")
    script.write_text(f"""
$ErrorActionPreference='Stop'
. '{lifecycle}'
$script:V3Root='{root}'
$script:V3Run=Join-Path $script:V3Root '.run'
function Get-V3ListenerPids([int]$Port) {{ if (Test-Path -LiteralPath '{marker_path}') {{ 777 }} }}
function Test-V3OwnedListener([string]$Service,[int]$ProcessId) {{ return $true }}
function Test-V3Health([string]$Service) {{ return (Test-Path -LiteralPath '{marker_path}') }}
function Start-Process {{ Start-Sleep -Seconds 1; Add-Content -LiteralPath '{marker_path}' -Value 'started'; return [pscustomobject]@{{Id=777}} }}
Start-V3Service '{service}'
""", encoding='utf-8-sig')
    command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-File', str(script)]
    first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out1, err1 = first.communicate(timeout=45)
    out2, err2 = second.communicate(timeout=45)
    assert first.returncode == 0, (out1, err1)
    assert second.returncode == 0, (out2, err2)
    assert marker.read_text().splitlines() == ['started']


@pytest.mark.parametrize('name', ['binance-stream.lock', 'paper-scheduler.lock'])
def test_market_and_scheduler_leases_exclude_second_process_and_recover(tmp_path, name):
    path = tmp_path / name
    code = ("from backend.market import _StreamLease; import sys; "
            "lease=_StreamLease(sys.argv[1]); print('ACQUIRED' if lease.acquire() else 'BLOCKED', flush=True); "
            "sys.stdin.read()")
    command = [sys.executable, '-u', '-c', code, str(path)]
    first = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert first.stdout.readline().strip() == 'ACQUIRED'
        second = subprocess.run(command, cwd=ROOT, input='', capture_output=True, text=True, timeout=30)
        assert second.stdout.strip() == 'BLOCKED'
    finally:
        first.kill()
        first.wait(timeout=30)
    recovered = subprocess.run(command, cwd=ROOT, input='', capture_output=True, text=True, timeout=30)
    assert recovered.stdout.strip() == 'ACQUIRED', recovered.stderr


def test_nonofficial_v3_copy_refuses_to_initialize(tmp_path):
    copy = tmp_path / 'copy'
    shutil.copytree(ROOT / 'backend', copy / 'backend', ignore=shutil.ignore_patterns('__pycache__'))
    env = os.environ.copy()
    env.update(BTC_LAB_TEST_MODE='false', BTC_LAB_DATA_DIR=str(tmp_path / 'data'),
               PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(ROOT))
    result = subprocess.run([sys.executable, '-c', 'import backend.app'], cwd=copy, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert 'V3_OFFICIAL_DEPLOYMENT_REQUIRED' in result.stderr
    assert not (tmp_path / 'data' / 'paper.sqlite3').exists()
