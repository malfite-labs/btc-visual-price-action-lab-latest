"""Opt-in PA-09/10 adapters for the existing paper dispatcher; no market client."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from time import monotonic

from backend.domain import CostModel
from backend.strategy_pool import StrategyPool
from scripts.pa09_pa10_candidates import STEP
from scripts.pa09_pa10_execution import CandidateLedger, timestamp

NAMES={'PA-09':'区间边界 Pinbar 反转','PA-10':'完整压力支撑互换'}


class PaperLedger(CandidateLedger):
    production=True
    provenance='APPROVED_PA_V1'

    def _bind(self,directory,sid):
        root=Path(directory).resolve(strict=True)
        if sid not in NAMES:
            raise ValueError('INVALID_PA_EXTENSION_ID')
        self.sid,self.path=sid,root/(sid+'.sqlite3')
        if self.path.resolve()!=self.path or (self.path.exists() and (not self.path.is_file() or self.path.stat().st_nlink!=1)):
            raise ValueError('UNSAFE_PA_EXTENSION_LEDGER')
        self.doc=dict(format='btc-lab-declarative-strategy/v2',version='1.0.0',
                      position=dict(max_hold_bars=96 if sid=='PA-09' else 192,target_r=2.5))

    def open_bar(self,*args,**kwargs):
        raise ValueError('PRODUCTION_REQUIRES_QUOTE_EVENT')


class PAExtension:
    def __init__(self,directory,*,register=False,enabled=(),cost=None):
        self.ledgers={}
        self.enabled=frozenset(enabled)  # Tests may explicitly exercise the legacy startup fixture.
        self.exit_checks={}
        if not self.enabled <= NAMES.keys():
            raise ValueError('UNKNOWN_PA_EXTENSION_ENABLE_ID')
        root=Path(directory)
        if register:
            root.mkdir(parents=True,exist_ok=True)
        costs=cost or CostModel()
        self.max_age=costs.max_quote_age_ms
        for sid in NAMES:
            if (root/(sid+'.sqlite3')).exists():
                ledger=PaperLedger.open_existing(root,sid)
            elif register:
                ledger=PaperLedger(root,sid)
            else:
                continue
            ledger.fee=Decimal(str(costs.fee_rate))
            ledger.slip=Decimal(str(costs.adverse_slippage_bps))/10000
            ledger.step_qty=Decimal(str(costs.quantity_step))
            ledger.min_qty=Decimal(str(costs.minimum_quantity))
            ledger.min_notional=Decimal(str(costs.minimum_notional))
            self.ledgers[sid]=ledger
        if not self.enabled <= self.ledgers.keys():
            raise ValueError('PA_EXTENSION_ENABLE_REQUIRES_LEDGER')

    def set_enabled(self,sid,enabled,now,can_enable):
        ledger=self.ledgers[sid]
        if now.utcoffset() is None:
            raise ValueError('INVALID_ACTIVATION_TIME')
        with ledger._transaction() as db:
            st=ledger._read(db)
            if (st.get('enabled') is True)==enabled:
                return 'UNCHANGED'
            if enabled:
                last=st.get('last')
                if db.execute('pragma quick_check').fetchone()[0]!='ok':
                    raise ValueError('LEDGER_INTEGRITY_FAILED')
                if not last or not 0<=(now-(timestamp(last)+STEP)).total_seconds()<2*STEP.total_seconds():
                    raise ValueError('NATURAL_CYCLE_NOT_VERIFIED')
                if not can_enable():
                    raise ValueError('EXIT_MONITOR_NOT_VERIFIED')
            st.update(enabled=enabled, activated_at=now.isoformat() if enabled else None,
                      pending=None, phase=None)
            ledger._save(db,st)
            return 'ENABLED' if enabled else 'DISABLED'

    def exit_recent(self,sid):
        return monotonic()-self.exit_checks.get(sid,float('-inf'))<=self.max_age/1000

    def ledger_cycle(self,sid,bars,close,allowed):
        selected={}
        for bar in bars:
            if bar.get('is_closed') is not True:
                continue
            start=timestamp(bar['open_at'])
            end=timestamp(bar.get('close_at') or (start+STEP).isoformat())
            if end>close or end<=close-61*STEP:
                continue
            if end!=start+STEP or end in selected:
                raise ValueError('INVALID_PA_EXTENSION_WINDOW')
            selected[end]={**bar, **{k:float(bar[k]) for k in ('open','high','low','close')}}
        if close not in selected:
            raise ValueError('MISSING_PA_EXTENSION_CLOSE')
        # Only the contiguous tail is usable after an archive gap.
        window=[]; point=close
        while point in selected:
            window.append(selected[point]); point-=STEP
        return self.ledgers[sid].close_bar(window[::-1],healthy=allowed,enforce_activation=sid not in self.enabled)

    def ledger_quote(self,sid,quote,allowed):
        if not StrategyPool._valid_quote(quote):
            return 'INVALID_QUOTE'
        try:
            result=self.ledgers[sid].quote(
                quote['event_at'],quote['bid'],quote['ask'],age_ms=int(quote.get('age_ms',10**9)),
                max_age_ms=self.max_age,entry_allowed=allowed,enforce_activation=sid not in self.enabled)
        except Exception:
            self.exit_checks.pop(sid,None)
            raise
        if allowed is not None and result not in ('INVALID_QUOTE','DUPLICATE_OR_STALE'):
            self.exit_checks[sid]=monotonic()
        return result

    def list(self):
        result=[]
        for sid,ledger in self.ledgers.items():
            s=ledger.snapshot(); p=s['position']
            pnl=Decimal(0)
            if p:
                pnl=(Decimal(p['mark'])-Decimal(p['entry']))*Decimal(p['qty'])*(1 if p['direction']=='LONG' else -1)
            cash=Decimal(s['cash'])
            fills=ledger.fills()
            enabled=sid in self.enabled or s.get("enabled") is True
            cycle=(timestamp(s['last'])+STEP) if s['last'] else None
            running=bool(cycle and 0<=(datetime.now(timezone.utc)-cycle).total_seconds()<=1800
                         and s.get('last_status') not in {'ENTRY_BLOCKED','GAP_NO_ENTRY'})
            result.append(dict(id=sid,name=NAMES[sid],active_version='1.0.0',enabled=enabled,
                               status=('RUNNING' if running else 'ENABLED_NOT_RUNNING') if enabled else 'REGISTERED',
                               position_direction=p['direction'] if p else 'FLAT',
                               position_amount=str(Decimal(p['entry'])*Decimal(p['qty'])) if p else '0',
                               fees=str(sum((Decimal(f['fee']) for f in fills),Decimal(0))),
                               realized=str(sum((Decimal(f['net']) for f in fills if f['net'] is not None),Decimal(0))),
                               last_action=s.get('last_status'),
                               cash=str(cash),equity=str(cash+pnl),unrealized=str(pnl),position=p,
                               phase=s['phase'],last_cycle=cycle.isoformat() if cycle else None,
                               last_status=s.get('last_status') or 'REGISTERED'))
        return result
