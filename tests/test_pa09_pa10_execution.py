import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from scripts.pa09_pa10_execution import CandidateLedger
from test_pa09_pa10_candidates import bar


def candles(sid, short):
    if sid == 'PA-09':
        seq = [bar(i) for i in range(60)] + [bar(60,98.5,98.8,98,98.7)]
        px = 98.7
    else:
        seq = [bar(i) for i in range(40)]
        for i, values in enumerate([(101,104,101,104),(104,106,104,105),
                                    (102,104,102,103),(103,105,103,105)],40):
            seq.append(bar(i,*values))
        px = 105
    if short:
        seq = [dict(b,open=200-b['open'],high=200-b['low'],low=200-b['high'],close=200-b['close']) for b in seq]
        px = 200-px
    return seq,px


class Execution(unittest.TestCase):
    def test_round_trips_and_restart(self):
        for sid in ('PA-09','PA-10'):
            for short in (False,True):
                for take_profit in (False,True):
                    with self.subTest(sid=sid,short=short,target=take_profit), tempfile.TemporaryDirectory() as path:
                        db=CandidateLedger(Path(path),sid)
                        seq,px=candles(sid,short)
                        for i,b in enumerate(seq):
                            db.close_bar(seq[max(0,i-60):i+1])
                            db=CandidateLedger.open_existing(Path(path),sid)
                            self.assertEqual(db.close_bar(seq[max(0,i-60):i+1]),'DUPLICATE_OR_STALE')
                        signal=db.snapshot()['pending']
                        start=bar(len(seq))['open_at']
                        self.assertEqual(db.open_bar(start,px),'OPEN')
                        self.assertNotEqual(db.open_bar(start,px),'OPEN')
                        pos=db.snapshot()['position']
                        if sid=='PA-09':
                            self.assertEqual(D(pos['target']),D(str(signal['target'])))
                        trigger=D(pos['target' if take_profit else 'stop'])
                        bid,ask=(trigger-D('.01'),trigger) if short else (trigger,trigger+D('.01'))
                        self.assertEqual(db.quote(start,bid,ask,age_ms=0),'TARGET_QUOTE' if take_profit else 'STOP_QUOTE')
                        db=CandidateLedger.open_existing(Path(path),sid)
                        self.assertNotIn(db.quote(start,bid,ask,age_ms=0),('TARGET_QUOTE','STOP_QUOTE'))
                        state=db.snapshot(); fills=db.fills()
                        self.assertEqual(len(fills),2)
                        self.assertEqual(D(state['cash']),D('10000')+D(fills[-1]['net']))
                        for fill in fills:
                            self.assertEqual(D(fill['fee']),D(fill['price'])*D(fill['qty'])*D('.0005'))
                        self.assertIsNone(state['position'])

    def test_gap_timeout_and_independent_ledgers(self):
        with tempfile.TemporaryDirectory() as path:
            root=Path(path); a=CandidateLedger(root,'PA-09'); b=CandidateLedger(root,'PA-10')
            for db in (a,b):
                seq,px=candles(db.sid,False)
                db.close_bar(seq if db.sid=='PA-09' else seq[:-3])
                if db.sid=='PA-10':
                    for i in range(len(seq)-3,len(seq)): db.close_bar(seq[:i+1])
                self.assertEqual(db.open_bar(bar(len(seq))['open_at'],px,healthy=False),'ENTRY_BLOCKED')
                self.assertEqual(db.open_bar(bar(len(seq)+1)['open_at'],px),'ENTRY_EXPIRED')
                self.assertEqual(db.fills(),[])
                shift=200
                new=[dict(k,open_at=bar(i+shift)['open_at']) for i,k in enumerate(seq)]
                for i in range(len(new)):
                    db.close_bar(new[max(0,i-60):i+1])
                self.assertEqual(db.open_bar(bar(shift+len(seq))['open_at'],px),'OPEN')
                self.assertEqual(db.quote(bar(shift+len(seq))['open_at'],1,2,age_ms=60001),'INVALID_QUOTE')
                for i in range(96 if db.sid=='PA-09' else 192):
                    k=bar(shift+len(seq)+i,px,px,px,px)
                    db.close_bar([k])
                self.assertEqual(db.fills()[-1]['reason'],'TIME')
            self.assertNotEqual(a.path,b.path)
            self.assertEqual(len(a.fills()),2)
            self.assertEqual(len(b.fills()),2)
    def test_gap_discards_pattern_and_cannot_replay_pending(self):
        with tempfile.TemporaryDirectory() as path:
            root=Path(path)
            db=CandidateLedger(root,'PA-10')
            seq,px=candles('PA-10',False)
            for i in (40,41):
                db.close_bar(seq[:i+1])
            self.assertEqual(db.snapshot()['phase']['phase'],'EXTENSION_CONFIRMED')
            db=CandidateLedger.open_existing(root,'PA-10')
            self.assertEqual(db.close_bar([seq[-1]]),'GAP_NO_ENTRY')
            self.assertIsNone(db.snapshot()['phase'])
            self.assertEqual(db.close_bar(seq[:-1]),'DUPLICATE_OR_STALE')
            self.assertEqual(db.fills(),[])

    def test_same_bar_stop_first_and_open_uses_actual_rr(self):
        for sid in ('PA-09','PA-10'):
            with self.subTest(sid=sid), tempfile.TemporaryDirectory() as path:
                db=CandidateLedger(Path(path),sid)
                seq,px=candles(sid,False)
                for i in range(len(seq)):
                    db.close_bar(seq[max(0,i-60):i+1])
                self.assertEqual(db.open_bar(bar(len(seq))['open_at'],px),'OPEN')
                pos=db.snapshot()['position']
                k=bar(len(seq),px,float(pos['target'])+1,float(pos['stop'])-1,px)
                self.assertEqual(db.close_bar([k]),'STOP')
                self.assertLess(D(db.fills()[-1]['net']),0)
        with tempfile.TemporaryDirectory() as path:
            db=CandidateLedger(Path(path),'PA-09')
            seq,_=candles('PA-09',False)
            db.close_bar(seq)
            s=db.snapshot()['pending']
            # Reference at the exact 2R boundary; adverse slippage lowers actual R.
            px=(D(str(s['target']))+2*D(str(s['stop'])))/3
            self.assertEqual(db.open_bar(bar(len(seq))['open_at'],px),'ENTRY_REJECTED')
            self.assertEqual(db.fills(),[])
