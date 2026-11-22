"""V1.0 IMPLEMENTATION_PROPOSAL: isolated simulation only, no runtime wiring."""
import json
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

from backend.strategy_pool import FORMAT_V2, StrategyPool, StrategyValidationError
from scripts.pa09_pa10_candidates import STEP, entry, pa09, pa10, time, validate

FEE, SLIP, STEP_QTY = D('.0005'), D('.0002'), D('.00000001')


def timestamp(value):
    t = datetime.fromisoformat(value)
    if t.utcoffset() is None:
        raise ValueError('NAIVE_EVENT_TIME')
    return t.astimezone(timezone.utc)


class CandidateLedger:
    production = False
    provenance = "IMPLEMENTATION_PROPOSAL"
    fee, slip, step_qty, min_qty, min_notional = FEE, SLIP, STEP_QTY, STEP_QTY, D("1")

    def __init__(self, directory, sid):
        self._bind(directory, sid)
        self.path.touch(exist_ok=False)
        with self._transaction() as db:
            db.execute('create table snapshot (value text not null)')
            db.execute('create table fills (id text primary key, value text not null)')
            db.execute('insert into snapshot values (?)', (json.dumps(dict(
                strategy=sid, version='1.0.0', provenance=self.provenance,
                cash='10000', position=None, phase=None, pending=None,
                last=None, quote_at=None)),))

    def _bind(self, directory, sid):
        root = Path(directory).resolve(strict=True)
        if sid not in ('PA-09','PA-10') or not root.is_relative_to(Path(tempfile.gettempdir()).resolve()) or root == Path(tempfile.gettempdir()).resolve():
            raise ValueError('ISOLATED_TEMP_DIRECTORY_REQUIRED')
        self.sid, self.path = sid, root / (sid+'.sqlite3')
        if self.path.resolve() != self.path or (self.path.exists() and self.path.stat().st_nlink != 1):
            raise ValueError('UNSAFE_LEDGER_PATH')
        self.doc = dict(format=FORMAT_V2, version='1.0.0', position=dict(
            max_hold_bars=96 if sid=='PA-09' else 192, target_r=2.5))

    @classmethod
    def open_existing(cls, directory, sid):
        obj=cls.__new__(cls)
        obj._bind(directory,sid)
        state=obj.snapshot()
        if state['strategy']!=sid or state['provenance']!=obj.provenance or state['version']!='1.0.0':
            raise ValueError('LEDGER_IDENTITY_MISMATCH')
        return obj

    @contextmanager
    def _transaction(self):
        db=sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True, timeout=2)
        try:
            with db:
                db.execute('begin immediate')
                yield db
        finally:
            db.close()

    @staticmethod
    def _read(db):
        return json.loads(db.execute('select value from snapshot').fetchone()[0])

    @staticmethod
    def _save(db,state):
        db.execute('update snapshot set value=?',(json.dumps(state),))

    def snapshot(self):
        with self._transaction() as db:
            return self._read(db)

    def fills(self):
        with self._transaction() as db:
            return [json.loads(x) for x, in db.execute('select value from fills order by rowid')]

    def _fill(self, db, state, event, kind, price, fee, qty, reason, net=None):
        record=dict(strategy=self.sid,at=event.isoformat(),kind=kind,reason=reason,
                    price=str(price),fee=str(fee),qty=str(qty),net=None if net is None else str(net))
        db.execute('insert into fills values (?,?)',(kind+':'+event.isoformat(),json.dumps(record)))

    def _settle(self, db, state, event, outcome, reason):
        pos=state['position']
        self._fill(db,state,event,'CLOSE',outcome['price'],outcome['fee'],pos['qty'],reason,outcome['net'])
        state['cash']=str(D(state['cash'])+outcome['gross']-outcome['fee'])
        state['position']=None

    def cancel_market_pending(self):
        with self._transaction() as db:
            st=self._read(db)
            if st['pending'] or st['phase']:
                st.update(pending=None,phase=None,last_status='CANCEL_PENDING_MARKET_RECOVERY')
                self._save(db,st)

    def close_bar(self, bars, *, healthy=True, enforce_activation=False):
        validate(bars)
        stamp=time(bars[-1]).astimezone(timezone.utc)
        with self._transaction() as db:
            st=self._read(db)
            last=timestamp(st['last']) if st['last'] else None
            if last and stamp<=last:
                return 'DUPLICATE_OR_STALE'
            gap=last is not None and stamp-last!=STEP
            healthy=healthy() if callable(healthy) else healthy
            if enforce_activation:
                activated=st.get('activated_at')
                healthy=healthy and st.get('enabled') is True and activated and stamp+STEP>timestamp(activated)
            previous_pending=st['pending']
            st.update(last=stamp.isoformat(),pending=None)
            status='WAIT'
            pos=st['position']
            if gap or not healthy:
                st['phase']=None
                status='GAP_NO_ENTRY' if gap else 'ENTRY_BLOCKED'
            if pos:
                opened=timestamp(pos['opened_cycle'])
                if stamp>=opened and not (self.production and gap):
                    pos['bars_held']=int((stamp-opened)//STEP)
                    result=StrategyPool._close_position(pos,self.doc,bars,D(pos['fee_rate']),D(pos['slippage_rate']))
                    if result:
                        self._settle(db,st,stamp+STEP,result,result['reason'])
                        status=result['reason']
                    else:
                        status='HOLD'
            elif healthy and not gap:
                if self.sid=='PA-09':
                    st['pending']=pa09(bars)
                else:
                    prior_phase=st['phase']
                    st['phase'],st['pending']=pa10(bars,prior_phase)
                    if prior_phase and not st['phase'] and prior_phase['phase']!='CONFIRMED':
                        status='PATTERN_EXPIRED' if stamp-timestamp(prior_phase['breakout'])>16*STEP else 'PATTERN_CANCELED'
                if st['pending']:
                    status='PENDING_NEXT_OPEN'
                elif previous_pending:
                    status='ENTRY_EXPIRED'
            st['last_status']=status
            self._save(db,st)
            return status

    def open_bar(self, at, price, *, healthy=True):
        stamp=timestamp(at)
        with self._transaction() as db:
            st=self._read(db); signal=st['pending']
            if not signal or st['position']:
                return 'NO_PENDING_ENTRY'
            expected=timestamp(signal['signal'])+STEP
            if stamp<expected:
                return 'EARLY_OPEN'
            st['pending']=None
            status='ENTRY_EXPIRED' if stamp>expected else 'ENTRY_BLOCKED'
            if stamp==expected and not healthy:
                # Preserve expiry evidence but never allow this signal later.
                st['pending']={**signal,'blocked':True}
            if stamp==expected and healthy and not signal.get('blocked'):
                status=self._enter(db,st,signal,stamp,expected,price,lambda: True)
            self._save(db,st)
            return status

    def _enter(self, db, st, signal, stamp, expected, price, allowed):
        ref=D(str(price)); direction=signal['side']
        side=1 if direction=='LONG' else -1
        fill=ref*(1+side*self.slip)
        if not ref.is_finite() or ref<=0:
            return 'ENTRY_REJECTED'
        if self.sid=='PA-09':
            stop,fixed_target=D(str(signal['stop'])),D(str(signal['target']))
            risk=side*(fill-stop)
            valid=stop>0 and fixed_target>0 and risk>0 and side*(fixed_target-fill)>=2*risk
        else:
            valid=entry(signal,expected,float(fill)) is not None
        if not valid:
            return 'ENTRY_REJECTED'
        try:
            pos,fee,_=StrategyPool._new_position(
                self.sid,self.doc,[{'close':ref}],stamp.isoformat(),self.fee,self.slip,
                self.step_qty,self.min_qty,self.min_notional,D(st['cash']),entry_reference=ref,
                direction=direction,stop_price=D(str(signal['stop'])))
            if self.sid=='PA-09':
                pos['target']=str(signal['target'])
                exit_px=D(pos['target'])*(1-side*self.slip)
                if side*(exit_px-fill)<=(exit_px+fill)*self.fee:
                    raise StrategyValidationError('TARGET_CANNOT_COVER_COSTS')
        except StrategyValidationError:
            return 'ENTRY_REJECTED'
        if not allowed():
            return 'ENTRY_BLOCKED'
        self._fill(db,st,stamp,'OPEN',pos['entry'],fee,pos['qty'],'ENTRY_NEXT_OPEN')
        st.update(position=pos,cash=str(D(st['cash'])-fee))
        return 'OPEN'

    def quote(self, at, bid, ask, *, age_ms, max_age_ms=60000, entry_allowed=None, enforce_activation=False):
        stamp=timestamp(at); bid,ask=D(str(bid)),D(str(ask))
        if not (bid.is_finite() and ask.is_finite() and 0<bid<=ask and 0<=age_ms<=max_age_ms):
            return 'INVALID_QUOTE'
        with self._transaction() as db:
            st=self._read(db); pos=st['position']
            if ((self.production and st['last'] and stamp<timestamp(st['last'])+STEP)
                    or (st['quote_at'] and stamp<=timestamp(st['quote_at']))
                    or (pos and stamp<timestamp(pos['opened_cycle']))):
                return 'DUPLICATE_OR_STALE'
            st['quote_at']=stamp.isoformat()
            reason='NO_POSITION'
            if (not pos and entry_allowed is not None and st['pending']
                    and (not enforce_activation or st.get('enabled') is True)):
                signal=st['pending']
                expected=timestamp(signal['signal'])+STEP
                if stamp>=expected:
                    st['pending']=None
                    if stamp>=expected+STEP:
                        reason='ENTRY_EXPIRED'
                    elif not entry_allowed():
                        reason='ENTRY_BLOCKED'
                    else:
                        reason=self._enter(db,st,signal,stamp,expected,
                                           ask if signal['side']=='LONG' else bid,entry_allowed)
            if pos:
                long=pos['direction']=='LONG'; px=bid if long else ask
                pos['mark']=str(px)
                stop,target=D(pos['stop']),D(pos['target'])
                hit_stop=px<=stop if long else px>=stop
                hit_target=px>=target if long else px<=target
                reason='HOLD'
                if hit_stop or hit_target:
                    reason='STOP_QUOTE' if hit_stop else 'TARGET_QUOTE'
                    # Reuse settlement arithmetic with the executable quote as its reference.
                    trigger_pos=pos if hit_stop else {**pos,'target':str(px)}
                    point={k:px for k in ('open','high','low','close')}
                    outcome=StrategyPool._close_position(trigger_pos,self.doc,[point],D(pos['fee_rate']),D(pos['slippage_rate']))
                    self._settle(db,st,stamp,outcome,reason)
            if reason not in ('NO_POSITION','HOLD'):
                st['last_status']=reason
            self._save(db,st)
            return reason
