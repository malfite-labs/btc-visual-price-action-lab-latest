"""Read-only, bounded observation of local paper service; no recovery actions."""
import json
import msvcrt
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
OPENER = build_opener(ProxyHandler({}))


def api(path):
    with OPENER.open('http://127.0.0.1:8011/api/v1/' + path, timeout=10) as response:
        return json.load(response)


def query(path, sql):
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in db.execute(sql)]
    finally:
        db.close()


def sample():
    now = datetime.now(timezone.utc)
    result = {'time_utc': now.isoformat(), 'issues': []}
    issues = result['issues']
    try:
        result['health'] = api('health')
        s = api('system/status')
        expected = {'scheduler': 'ENABLED_INTERNAL_PAPER', 'paper_automation': 'ENABLED_INTERNAL_PAPER',
                    'paper_gate_reason': 'RULE_PROFILE_READY', 'feed_health': 'LIVE', 'quote_health': 'FRESH',
                    'websocket': 'CONNECTED', 'websocket_recovery_state': 'HEALTHY',
                    'formal_trading_mode': 'RULE_DRIVEN', 'LIVE_TRADING_DISABLED': True}
        result['status'] = {key: s.get(key) for key in (*expected, 'last_candle_at', 'decision_dispatch')}
        for key, value in expected.items():
            if s.get(key) != value:
                issues.append('STATUS_' + key.upper() + '=' + str(s.get(key)))
        if result['health'].get('status') != 'ok':
            issues.append('HEALTH_NOT_OK')
    except Exception as exc:
        issues.append('BACKEND_READ_ERROR:' + type(exc).__name__)
    try:
        with OPENER.open('http://127.0.0.1:5174/', timeout=10) as response:
            result['frontend_http'] = response.status
        if result['frontend_http'] != 200:
            issues.append('FRONTEND_NOT_200')
    except Exception as exc:
        issues.append('FRONTEND_READ_ERROR:' + type(exc).__name__)
    try:
        cmd = ('Get-NetTCPConnection -State Listen -LocalPort 8011,5174 | ForEach-Object { '
               '$p=Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)"; '
               '[pscustomobject]@{port=$_.LocalPort;pid=$p.ProcessId;exe=$p.ExecutablePath;command=$p.CommandLine} '
               '} | ConvertTo-Json -Compress')
        raw = subprocess.check_output(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', cmd],
                                      timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        listeners = json.loads(raw.decode('utf-8-sig'))
        listeners = listeners if isinstance(listeners, list) else [listeners]
        result['listeners'] = listeners
        if {item['port'] for item in listeners} != {8011, 5174}:
            issues.append('PORT_LISTENER_MISSING')
        for item in listeners:
            if item['port'] == 8011 and not str(item.get('exe', '')).lower().startswith(str(ROOT).lower() + '\\'):
                issues.append('BACKEND_EXE_OUTSIDE_REPO')
            if item['port'] == 5174 and str(ROOT / 'frontend').lower() not in str(item.get('command', '')).lower():
                issues.append('FRONTEND_SOURCE_OUTSIDE_REPO')
    except Exception as exc:
        issues.append('LISTENER_CHECK_ERROR:' + type(exc).__name__)
    try:
        result['accounts'] = {x['agent']: {k: x['account'].get(k) for k in ('state', 'version', 'cash')}
                              for x in api('agents')}
        e = api('e/accounts')
        result['e_accounts'] = {k: {'status': e[k].get('status'), 'state': (e[k].get('account') or {}).get('state'),
                                    'version': (e[k].get('account') or {}).get('version')} for k in ('formal', 'shadow')}
        result['e_runtime'] = e.get('runtime')
        if any(e[k].get('status') != 'READY' for k in ('formal', 'shadow')):
            issues.append('E_ACCOUNT_NOT_READY')
        if (e.get('runtime') or {}).get('mode') != 'AUTO_PAPER':
            issues.append('E_AUTO_NOT_ENABLED')
    except Exception as exc:
        issues.append('ACCOUNT_READ_ERROR:' + type(exc).__name__)
    try:
        formal = ROOT / 'data/paper.sqlite3'
        result['abc_cycles'] = query(formal, 'select cycle_close,status,updated_at from scheduler_cycles order by cycle_close desc limit 4')
        result['decisions'] = query(formal, 'select agent,candle_close,md_version,action,outcome from decisions order by rowid desc limit 12')
        for name in ('formal', 'shadow'):
            result['e_' + name + '_cycles'] = query(ROOT / 'data/e' / (name + '.sqlite3'),
                                                    'select close_at,status,result_json from e_quant_cycles order by close_at desc limit 4')
        for agent in 'ABC':
            stamps = [row['candle_close'] for row in result['decisions'] if row['agent'] == agent]
            if not stamps or (now - datetime.fromisoformat(stamps[0].replace('Z', '+00:00'))).total_seconds() > 2100:
                issues.append('ABC_CYCLE_STALE_' + agent)
        for name in ('formal', 'shadow'):
            cycles = result['e_' + name + '_cycles']
            if not cycles or (now - datetime.fromisoformat(cycles[0]['close_at'].replace('Z', '+00:00'))).total_seconds() > 2100:
                issues.append('E_CYCLE_STALE_' + name.upper())
            if cycles and cycles[0]['status'] in ('CYCLE_ERROR', 'ERROR'):
                issues.append('E_CYCLE_ERROR_' + name.upper())
    except Exception as exc:
        issues.append('LEDGER_READ_ERROR:' + type(exc).__name__)
    return result


def main():
    import os
    run, logs = ROOT / '.run', ROOT / 'logs'
    run.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    with (run / 'unattended-observation.lock').open('a+b') as lock:
        lock.seek(0, 2)
        if not lock.tell():
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise SystemExit('OBSERVATION_ALREADY_RUNNING')
        start = datetime.now(timezone.utc)
        stamp = start.strftime('%Y%m%dT%H%M%SZ')
        out = logs / ('unattended-observation-' + stamp + '.jsonl')
        report = logs / ('unattended-observation-' + stamp + '.md')
        latest = run / 'unattended-observation.json'
        meta = {'pid': os.getpid(), 'started_utc': start.isoformat(), 'ends_utc': (start + timedelta(hours=24)).isoformat(),
                'samples': str(out), 'report': str(report), 'interval_seconds': 60, 'state': 'RUNNING'}
        deadline = time.monotonic() + 24 * 3600
        count = issue_count = 0
        while True:
            try:
                record = sample()
            except Exception as exc:
                record = {'time_utc': datetime.now(timezone.utc).isoformat(), 'issues': ['MONITOR_ERROR:' + type(exc).__name__]}
            with out.open('a', encoding='utf8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            count += 1
            issue_count += bool(record['issues'])
            meta.update(last_sample_utc=record['time_utc'], sample_count=count, samples_with_issues=issue_count, last_issues=record['issues'])
            latest.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf8')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(60, remaining))
        meta.update(state='COMPLETED', completed_utc=datetime.now(timezone.utc).isoformat())
        latest.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf8')
        report.write_text('# 24 小时无人值守观测\n\n' + json.dumps(meta, ensure_ascii=False, indent=2) +
                          '\n\n原始记录见 samples；异常须逐条复核。本观察不修改交易状态，不测试主机重启或网络中断恢复。\n', encoding='utf8')


if __name__ == '__main__':
    main()
