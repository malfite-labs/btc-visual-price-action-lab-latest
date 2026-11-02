"""One-shot local health inspection. No trading, recovery or external connections."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / 'logs'
OPENER = build_opener(ProxyHandler({}))


def assess(status, stats, now):
    problems = []
    if status.get('websocket') != 'CONNECTED': problems.append('ALPACA_WS_NOT_CONNECTED')
    if status.get('websocket_quote_health') != 'FRESH': problems.append('ALPACA_QUOTE_NOT_FRESH')
    if status.get('scheduler') != 'ENABLED_INTERNAL_PAPER': problems.append('FORMAL_SCHEDULER_OFF')
    if status.get('shadow_evolution', {}).get('state') != 'RUNNING': problems.append('SHADOW_NOT_RUNNING')
    if status.get('scheduler') == 'ENABLED_INTERNAL_PAPER':
        for agent in 'ABC':
            stamp = (stats.get('agents', {}).get(agent, {}).get('last_decision') or {}).get('candle_close')
            try:
                age = (now - datetime.fromisoformat(stamp.replace('Z', '+00:00'))).total_seconds()
                if not 0 <= age <= 2100: problems.append('FORMAL_CYCLE_STALE_' + agent)
            except (AttributeError, TypeError, ValueError): problems.append('FORMAL_CYCLE_UNKNOWN_' + agent)
    return problems


def fetch(url):
    with OPENER.open(url, timeout=8) as response:
        return response.status, response.read()


def main():
    import msvcrt
    LOGS.mkdir(exist_ok=True)
    with (LOGS / 'health-monitor.lock').open('a+b') as lock:
        lock.seek(0, os.SEEK_END)
        if not lock.tell(): lock.write(b'0'); lock.flush()
        lock.seek(0)
        try: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError: return 0
        now = datetime.now(timezone.utc)
        result = {'time_utc': now.isoformat(), 'backend': 'UNKNOWN', 'frontend': 'UNKNOWN', 'book': 'UNKNOWN', 'issues': []}
        try:
            _, payload = fetch('http://127.0.0.1:8011/api/v1/system/status')
            status = json.loads(payload)
            _, payload = fetch('http://127.0.0.1:8011/api/v1/statistics/abc')
            stats = json.loads(payload)
            result.update(backend='ONLINE', websocket=status.get('websocket'), quote=status.get('websocket_quote_health'), formal=status.get('scheduler'), shadow=status.get('shadow_evolution', {}).get('state'))
            result['issues'].extend(assess(status, stats, now))
        except (OSError, ValueError, KeyError) as exc:
            result['backend'] = 'UNAVAILABLE'
            result['issues'].append('BACKEND_OR_STATUS_UNAVAILABLE:' + type(exc).__name__)
        try:
            code, _ = fetch('http://127.0.0.1:5174/')
            result['frontend'] = 'ONLINE' if code == 200 else 'UNAVAILABLE'
            _, payload = fetch('http://127.0.0.1:5174/api/v1/health')
            if json.loads(payload).get('status') != 'ok': result['issues'].append('FRONTEND_API_UNAVAILABLE')
        except (OSError, ValueError) as exc:
            result['frontend'] = 'UNAVAILABLE'
            result['issues'].append('FRONTEND_OR_PROXY_UNAVAILABLE:' + type(exc).__name__)
        try:
            _, payload = fetch('http://127.0.0.1:8011/api/v1/research/book-pa/latest')
            result['book'] = json.loads(payload).get('status', 'UNKNOWN')
            if result['book'] not in ('READY', 'NO_RESEARCH_RESULTS'): result['issues'].append('BOOK_UNAVAILABLE')
        except (OSError, ValueError) as exc:
            result['issues'].append('BOOK_UNAVAILABLE:' + type(exc).__name__)
        cutoff = now.timestamp() - 1200
        for path in LOGS.glob('*err*.log'):
            if path.stat().st_mtime < cutoff: continue
            with path.open('rb') as stream:
                stream.seek(max(0, path.stat().st_size - 131072))
                text = stream.read().decode('utf8', errors='replace')
            for marker in ('MemoryError', 'StackOverflow', '0xc00000fd'):
                if marker.lower() in text.lower(): result['issues'].append('RECENT_BACKEND_LOG:' + marker + ':' + path.name)
        result['state'] = 'HEALTHY' if not result['issues'] else 'ISSUES'
        with (LOGS / ('health-monitor-' + now.astimezone().strftime('%Y-%m-%d') + '.jsonl')).open('a', encoding='utf8') as out:
            out.write(json.dumps(result, ensure_ascii=False) + '\n')
        print(json.dumps(result, ensure_ascii=False))
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
