"""Durable C follower in dry-run mode. No exchange client is imported."""
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
import argparse
import json
import sqlite3
import time
import os

from scripts.live_copy_env import MAX_COPY_LEVERAGE
from scripts.live_copy_v2 import paper_fills


class DryFollower:
    def __init__(self, source, ledger, fixed, budget, *, strategy='C'):
        self.source = Path(source).resolve(strict=True)
        self.ledger = Path(ledger).resolve()
        if self.ledger == self.source or not 0 < fixed <= budget:
            raise ValueError('INVALID_DRY_RUN_CONFIGURATION')
        self.fixed, self.budget = fixed, budget
        if strategy not in ('A','B','C','G') and not strategy.startswith('pa-'):
            raise ValueError('FILL_ADAPTER_UNVERIFIED')
        self.strategy = strategy
        with self.connect() as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS subscription (id INTEGER PRIMARY KEY CHECK(id=1),
                    source TEXT, cursor INTEGER, fixed TEXT, budget TEXT);
                CREATE TABLE IF NOT EXISTS inbox (cursor INTEGER PRIMARY KEY, event_id TEXT UNIQUE,
                    payload TEXT, status TEXT NOT NULL, result TEXT);
                CREATE TABLE IF NOT EXISTS trades (id TEXT PRIMARY KEY, direction TEXT,
                    remaining TEXT, reserved TEXT, state TEXT);
            """)
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT source,fixed,budget FROM subscription WHERE id=1').fetchone()
            expected = (str(self.source) if strategy=='C' else f'{self.source}#{strategy}',
                        fixed.to_eng_string(), budget.to_eng_string())
            if row is None:
                db.execute('INSERT INTO subscription VALUES (1,?,?,?,?)',
                           (expected[0], paper_fills(self.source,strategy=strategy), fixed.to_eng_string(), budget.to_eng_string()))
            elif row != expected:
                raise ValueError('SUBSCRIPTION_CHANGED')

    def connect(self):
        return closing(sqlite3.connect(self.ledger, timeout=5))

    def cursor(self):
        with self.connect() as db:
            return db.execute('SELECT cursor FROM subscription WHERE id=1').fetchone()[0]

    def discover(self, *, pause_reason=None):
        with self.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            cursor = db.execute('SELECT cursor FROM subscription WHERE id=1').fetchone()[0]
            if paper_fills(self.source,strategy=self.strategy) < cursor:
                raise RuntimeError('SOURCE_CURSOR_REGRESSED')
            for fill in paper_fills(self.source, cursor,strategy=self.strategy):
                payload = asdict(fill)
                payload['discovered_at'] = datetime.now(UTC).isoformat()
                if pause_reason and fill.action in ('OPEN','ADD'):
                    payload['discovery_pause_reason'] = pause_reason
                db.execute('INSERT INTO inbox VALUES (?,?,?,\'PENDING\',NULL)',
                           (fill.cursor, fill.event_id, json.dumps(payload, default=str)))
                db.execute('UPDATE subscription SET cursor=? WHERE id=1', (fill.cursor,))

    def poll(self, *, paused=False):
        self.discover()
        processed = []
        while True:
            with self.connect() as db, db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute("SELECT cursor,event_id,payload FROM inbox WHERE status='PENDING' ORDER BY cursor LIMIT 1").fetchone()
                if row is None:
                    return processed
                cursor, event_id, payload = row
                fill = json.loads(payload)
                discovery_pause = fill.get('discovery_pause_reason')
                result = self.plan(db, fill, paused=paused or bool(discovery_pause))
                if result.get('reason') == 'PAUSED' and discovery_pause:
                    result['reason'] = discovery_pause
                db.execute("UPDATE inbox SET status='PLANNED',result=? WHERE cursor=?",
                           (json.dumps(result, default=str), cursor))
                processed.append((event_id,result))

    def plan(self, db, fill, *, paused=False):
        trade = fill['trade_id']
        action, direction = fill['action'], fill['direction']
        qty = Decimal(fill['quantity'])
        row = db.execute('SELECT direction,remaining,reserved,state FROM trades WHERE id=?',(trade,)).fetchone()
        if row and row[0] != direction:
            raise RuntimeError('TRADE_DIRECTION_MISMATCH')
        if action in ('OPEN','ADD'):
            if action == 'ADD' and (not row or row[3] != 'OPEN'):
                return {'action':'SKIPPED','reason':'NO_ASSOCIATED_POSITION'}
            if action == 'OPEN' and row:
                raise RuntimeError('TRADE_ALREADY_SEEN')
            reserved = sum((Decimal(x[0]) for x in db.execute("SELECT reserved FROM trades WHERE state='OPEN'")), Decimal(0))
            if paused or reserved + self.fixed > self.budget:
                if row:
                    db.execute('UPDATE trades SET remaining=? WHERE id=?',(str(Decimal(row[1])+qty), trade))
                else:
                    db.execute('INSERT INTO trades VALUES (?,?,?,?,?)',(trade,direction,str(qty),'0','SKIPPED'))
                return {'action':'SKIPPED','reason':'PAUSED' if paused else 'BUDGET_LIMIT'}
            remaining = qty + (Decimal(row[1]) if row else Decimal(0))
            margin = self.fixed + (Decimal(row[2]) if row else Decimal(0))
            db.execute('INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?)',
                       (trade,direction,str(remaining),str(margin),'OPEN'))
            return {'action':action,'direction':direction,'planned_margin':self.fixed}
        if not row or row[3] != 'OPEN':
            return {'action':'SKIPPED','reason':'NO_ASSOCIATED_POSITION'}
        before = Decimal(row[1])
        if action in ('FULL_CLOSE', 'PARTIAL_CLOSE') and 0 < qty == before:
            fraction = Decimal(1)
            effective_action = 'FULL_CLOSE'
        elif action == 'PARTIAL_CLOSE' and 0 < qty < before:
            fraction = qty / before
            effective_action = 'PARTIAL_CLOSE'
        else:
            raise RuntimeError('UNVERIFIED_EXIT_QUANTITY')
        reserved = Decimal(row[2]) * (1 - fraction)
        db.execute('UPDATE trades SET remaining=?,reserved=?,state=? WHERE id=?',
                   (str(before - qty), str(reserved), 'CLOSED' if fraction == 1 else 'OPEN', trade))
        return {'action': effective_action, 'direction': direction, 'fraction': fraction}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('ledger')
    parser.add_argument('--fixed',type=Decimal,required=True)
    parser.add_argument('--budget',type=Decimal,required=True)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--runtime-dir', type=Path)
    parser.add_argument('--strategy',default='C')
    parser.add_argument('--config-id')
    parser.add_argument('--legacy-demo-ledger', action='store_true')
    parser.add_argument('--environment', choices=('BINANCE_DEMO', 'BINANCE_PRODUCTION'), default='BINANCE_DEMO')
    parser.add_argument('--account-id')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--demo-check',action='store_true')
    parser.add_argument('--production-check',action='store_true')
    parser.add_argument('--authorized-demo-orders',action='store_true')
    parser.add_argument('--authorized-production-orders',action='store_true')
    parser.add_argument('--leverage',type=int,choices=range(1,MAX_COPY_LEVERAGE+1),default=5)
    parser.add_argument('--stop-fraction',type=Decimal)
    parser.add_argument('--target-fraction',type=Decimal)
    parser.add_argument('--risk-cap',type=Decimal)
    args = parser.parse_args()
    if args.demo_check or args.production_check:
        if args.production_check:
            args.environment = 'BINANCE_PRODUCTION'
        return demo_run(args,parser)
    import msvcrt
    lock_path = args.runtime_dir/'active.lock' if args.runtime_dir else Path(str(Path(args.ledger).resolve()) + '.lock')
    with lock_path.open('a+b') as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise SystemExit('FOLLOWER_ALREADY_RUNNING') from None
        if args.authorized_demo_orders or args.authorized_production_orders:
            if args.authorized_production_orders:
                args.environment = 'BINANCE_PRODUCTION'
            stopped = demo_run(args,parser)
            if stopped is not None:
                lock.close()
                stopped('STOPPED')
            return
        follower = DryFollower(args.source,args.ledger,args.fixed,args.budget,strategy=args.strategy)
        print('FOLLOW_NEW_FILLS DRY_RUN',flush=True)
        runtime = args.runtime_dir
        state = 'MONITORING'
        def publish():
            if runtime:
                target = runtime / 'status.json'
                pending = target.with_suffix('.tmp')
                pending.write_text(json.dumps({'state':state, 'time':time.time(),'strategy':args.strategy,
                    'mode':'DRY_RUN','environment':args.environment,'pid':os.getpid(),'copy_config_id':args.config_id,
                    'cursor':follower.cursor()}), encoding='utf8')
                pending.replace(target)
        try:
            while True:
                command = (runtime / 'command.txt').read_text(encoding='utf8').strip() if runtime else ''
                if command == 'stop':
                    break
                state = 'PAUSED' if command == 'pause' else 'MONITORING'
                publish()
                for event_id, result in follower.poll(paused=state=='PAUSED'):
                    print(event_id,json.dumps(result,default=str),flush=True)
                if args.once:
                    break
                time.sleep(3)
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            state = 'RECONCILE_REQUIRED'
            publish()
            raise
        else:
            state = 'STOPPED'
            publish()


def demo_read_retry(operation, publish, *, sleep=time.sleep, jitter=None):
    """Retry only transient Demo GET failures, including before account ownership."""
    from random import uniform
    from scripts.live_c_demo_exchange import DemoApiError
    labels = {'/fapi/v1/time':'Binance 时间同步','/fapi/v3/balance':'账户身份及余额',
              '/fapi/v1/openAlgoOrders':'Algo 挂单','/fapi/v1/openOrders':'普通挂单',
              '/fapi/v3/positionRisk':'BTCUSDT 持仓','/fapi/v3/account':'账户余额',
              '/fapi/v1/order':'普通委托','/fapi/v1/algoOrder':'Algo 委托'}
    delays = (3,5,10,20,30,60)
    failures = 0
    jitter = jitter or (lambda:uniform(-.5,.5))
    while True:
        try:
            if failures:
                publish('DEMO_RECONCILING','正在重新核对仓位、挂单及本地委托',retries=failures)
            return operation(failures)
        except DemoApiError as exc:
            endpoint = labels.get(exc.operation,exc.operation)
            reason = f'{endpoint}查询失败（HTTP {exc.status}，代码 {exc.code}）'
            if exc.method!='GET' or (exc.status not in (0,408,429) and exc.status<500 and exc.code!=-1021):
                publish('RECONCILE_REQUIRED',reason)
                raise
            base = delays[min(failures,len(delays)-1)]
            failures += 1
            delay = max(1,min(60,base+jitter()))
            publish('DEMO_RECONNECTING' if failures==1 else 'DEMO_NETWORK_RECOVERY',
                    reason+f'，{delay:.1f} 秒后重连',retries=failures)
            sleep(delay)


def read_demo_command(runtime):
    if runtime is None:
        return 'run'
    try:
        action = (runtime/'command.txt').read_text(encoding='utf8').lstrip('\ufeff').strip()
    except (OSError, UnicodeError):
        return 'pause'
    return action if action in ('run', 'pause', 'stop') else 'pause'


def demo_poll(engine, command, publish, *, sleep=time.sleep, jitter=None, startup=False):
    def current_command():
        return command() if callable(command) else command

    def discover():
        action = current_command()
        engine.follower.discover(pause_reason={'pause':'MANUAL_PAUSE','stop':'STOP_PENDING'}.get(action))

    def wait_with_discovery(delay):
        while delay > 0:
            discover()
            interval = min(1, delay)
            sleep(interval)
            delay -= interval

    def poll(failures):
        discover()
        action = current_command()
        status = {'stop':'DEMO_STOP_PENDING','pause':'DEMO_PAUSED'}.get(action,'DEMO_RUNNING')
        if action=='stop' and engine.can_stop():
            return True,[]
        pause_reason = {'pause':'MANUAL_PAUSE','stop':'STOP_PENDING'}.get(action)
        if startup:
            publish('DEMO_RECONCILING','正在核对仓位、挂单及本地委托')
        events = engine.tick(paused=pause_reason is not None,pause_reason=pause_reason,
                             command=current_command)
        publish(status,'已核对成功，已恢复原授权状态' if failures else '',success=True,retries=failures)
        return False,events
    return demo_read_retry(poll,publish,sleep=wait_with_discovery,jitter=jitter)


def demo_run(args, parser):
    from scripts.live_copy_env import DEMO_ENV, PROD_ENV, alias_fingerprint, env_paths, make_exchange
    from scripts.live_copy_follow import FollowExecutor

    environment = getattr(args, 'environment', DEMO_ENV)
    is_prod = environment == PROD_ENV
    paths = env_paths(environment, base_root=getattr(args, 'data_dir', None))
    allow_writes = bool(getattr(args, 'authorized_production_orders', False)) if is_prod else bool(getattr(args, 'authorized_demo_orders', False))
    exchange, _, resolved_account_id = make_exchange(
        environment, account_id=getattr(args, 'account_id', None),
        allow_writes=allow_writes, base_root=getattr(args, 'data_dir', None)
    )
    mode_code = 'PRODUCTION' if is_prod else 'DEMO'
    runtime = args.runtime_dir or paths.runtime
    last_success = None
    last_reason = ''
    reconnect_count = 0
    def publish(state, reason='', *, success=False,retries=0):
        nonlocal last_success,last_reason,reconnect_count
        if retries:
            reconnect_count=retries
        if success:
            last_success=time.time()
        if reason or state not in ('DEMO_RUNNING','DEMO_PAUSED'):
            last_reason=reason
        if runtime:
            runtime.mkdir(parents=True, exist_ok=True)
            target = runtime/'status.json'
            pending = runtime/f'status.{mode_code.lower()}.tmp'
            pending.write_text(json.dumps(dict(state=state,strategy=args.strategy,
                mode=mode_code,environment=environment,account_id=resolved_account_id,
                time=time.time(),pid=os.getpid(),copy_config_id=args.config_id,
                cursor=follower.cursor() if follower is not None else None,reason=last_reason,last_reconciled_at=last_success,
                reconnect_count=reconnect_count,last_connected_at=last_success)),encoding='utf8')
            for attempt in range(5):
                try:
                    pending.replace(target)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(.05)
    follower = None

    def initialize(_failures):
        exchange.synchronize()
        mode = exchange._request('GET','/fapi/v1/positionSide/dual').get('dualSidePosition')
        if type(mode) is not bool:
            raise RuntimeError(f'{mode_code}_POSITION_MODE_UNVERIFIED')
        exchange.hedge = mode
        snapshot = exchange.risk_snapshot()
        settings = snapshot['symbol'][0]
        if (settings['marginType'] != 'ISOLATED' or int(settings['leverage']) > args.leverage
                or not snapshot['permissions']['enableFutures']):
            raise RuntimeError(f'{mode_code}_CONFIGURATION_NOT_COMPATIBLE')
        return mode, snapshot

    check_only = bool(getattr(args, 'demo_check', False) or getattr(args, 'production_check', False))
    if check_only:
        report = exchange.preflight_report(planned_leverage=args.leverage, fixed_margin=args.fixed, budget=args.budget)
        print(f'{mode_code}_READ_ONLY', json.dumps(report, ensure_ascii=False), flush=True)
        return
    else:
        publish('DEMO_RECONCILING','正在进行 Binance 时间同步及账户初始化')
        try:
            mode, snapshot = demo_read_retry(initialize,publish)
        except (OSError, ValueError, RuntimeError, KeyError, IndexError) as exc:
            publish('RECONCILE_REQUIRED',f'{mode_code} 启动检查失败（{exc}）')
            raise
    if args.once or args.risk_cap is None:
        parser.error(f'{mode_code} execution requires configured risk budget and a managed service')
    args.stop_fraction = args.stop_fraction or None
    args.target_fraction = args.target_fraction or None
    if (any(value is not None and (not value.is_finite() or not 0 < value < 1)
            for value in (args.stop_fraction,args.target_fraction))
            or not args.risk_cap.is_finite() or args.risk_cap <= 0):
        parser.error(f'Invalid {mode_code} stop or risk settings')
    from uuid import UUID
    if args.config_id:
        config_id = UUID(args.config_id).hex
    else:
        config_id = None
    directory = paths.ledgers
    if config_id and (is_prod or not args.legacy_demo_ledger):
        directory = directory/config_id
    directory.mkdir(parents=True,exist_ok=True)
    ledger = directory/'demo-execution-inbox.sqlite3'
    from contextlib import ExitStack
    with ExitStack() as ownership:
        alias = demo_read_retry(lambda _: ownership.enter_context(exchange.order_ownership()),publish)
        if (args.strategy.startswith('pa-') and not paper_fills(args.source,strategy=args.strategy)
                and args.strategy!='pa-trend-false-break-reclaim'):
            raise RuntimeError('PA_ADAPTER_HAS_NO_VERIFIED_FILLS')
        if not ledger.exists() and any(demo_read_retry(lambda _: exchange.exposure(),publish)):
            raise RuntimeError('UNASSOCIATED_EXPOSURE')
        follower = DryFollower(args.source,ledger,args.fixed,args.budget,strategy=args.strategy)
        policy = json.dumps([args.leverage,str(args.stop_fraction),str(args.risk_cap),mode,
                             'FOLLOW_PAPER',str(args.target_fraction)])
        with follower.connect() as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS demo_config_binding(id INTEGER PRIMARY KEY, value TEXT)')
            if config_id:
                db.execute('INSERT OR IGNORE INTO demo_config_binding VALUES (1,?)',(config_id,))
                if db.execute('SELECT value FROM demo_config_binding WHERE id=1').fetchone()[0] != config_id:
                    raise RuntimeError('DEMO_CONFIG_BINDING_MISMATCH')
            db.execute('CREATE TABLE IF NOT EXISTS demo_policy(id INTEGER PRIMARY KEY, value TEXT)')
            db.execute('INSERT OR IGNORE INTO demo_policy VALUES (1,?)',(policy,))
            prior = db.execute('SELECT value FROM demo_policy WHERE id=1').fetchone()[0]
            if prior != policy:
                if any(exchange.exposure()):
                    raise RuntimeError('DEMO_EXECUTION_POLICY_CHANGED_WITH_EXPOSURE')
                old = json.loads(prior)
                if len(old) not in (3,4) or old[0] != args.leverage:
                    raise RuntimeError('DEMO_EXECUTION_POLICY_CHANGED')
                if db.execute('SELECT 1 FROM inbox LIMIT 1').fetchone():
                    raise RuntimeError('EXISTING_DEMO_EVENTS_REQUIRE_POLICY_RECONCILIATION')
                db.execute('UPDATE demo_policy SET value=? WHERE id=1',(policy,))
        account_binding = f'PROD-{alias_fingerprint(alias)}' if is_prod else 'DEMO-CONTINUOUS'
        engine = FollowExecutor(follower,exchange,directory,account_binding,
            leverage=args.leverage,stop_fraction=args.stop_fraction,risk_cap=args.risk_cap,
            target_fraction=args.target_fraction,exit_mode='FOLLOW_PAPER')
        print(f'{mode_code}_CONTINUOUS',args.strategy,flush=True)
        startup = True
        reconcile_paused = False
        try:
            while True:
                def command():
                    return read_demo_command(runtime)
                try:
                    if reconcile_paused:
                        # Process stays alive with new openings paused while retrying reconciliation
                        if command() == 'stop':
                            try:
                                if engine.can_stop():
                                    return publish
                            except Exception:
                                pass
                        stopped, events = demo_poll(engine, lambda: 'pause' if command() == 'run' else command(), publish, startup=True)
                        reconcile_paused = False
                    else:
                        stopped, events = demo_poll(engine, command, publish, startup=startup)
                    startup = False
                    if stopped:
                        return publish
                    for event, result in events:
                        print(event, result, flush=True)
                    time.sleep(1)
                except Exception as exc:
                    from scripts.live_c_demo_exchange import DemoApiError
                    code = str(exc)
                    print(f'{mode_code}_RECONCILE', type(exc).__name__, code, flush=True)
                    if isinstance(exc, DemoApiError):
                        # Non-transient auth/permission errors cannot self-heal; exit cleanly
                        raise
                    if code in ('PROTECTION_INCOMPLETE', 'PROTECTION_REQUIRED'):
                        publish('PROTECTION_INCOMPLETE', '已配置的交易所保护单缺失或与成交不符，已暂停新开仓并保持对账')
                    elif code == 'EXCHANGE_UNKNOWN':
                        publish('EXCHANGE_UNKNOWN', '委托结果未确认，已暂停新开仓并等待对账恢复')
                    else:
                        publish('RECONCILE_REQUIRED', f'本地委托与交易所尚未核对一致（{code}），已暂停新开仓并持续对账')
                    reconcile_paused = True
                    time.sleep(5)
        except Exception as exc:
            from scripts.live_c_demo_exchange import DemoApiError
            print(f'{mode_code}_FATAL', type(exc).__name__, str(exc), flush=True)
            if not isinstance(exc, DemoApiError):
                publish('RECONCILE_REQUIRED', f'本地委托与交易所尚未核对一致（{exc}）')
            raise


if __name__ == '__main__':
    main()
