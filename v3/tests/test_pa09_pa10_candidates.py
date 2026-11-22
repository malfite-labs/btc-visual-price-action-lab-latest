import json
import unittest
from datetime import datetime, timezone
from scripts.pa09_pa10_candidates import STEP, entry, pa09, pa10, time


def bar(i,o=100,h=102,l=98,c=100):
    return dict(open_at=(datetime(2026,1,1,tzinfo=timezone.utc)+i*STEP).isoformat(),open=o,high=h,low=l,close=c,is_closed=True)


class Candidates(unittest.TestCase):
    def test_pinbar_both_sides_and_next_open(self):
        base=[bar(i) for i in range(60)]
        for b, side in [(bar(60,98.5,98.8,98,98.7),'LONG'),(bar(60,101.5,102,101.2,101.3),'SHORT')]:
            signal=pa09(base+[b]); self.assertEqual(signal['side'],side)
            self.assertEqual(entry(signal,time(b)+STEP,b['close']),signal['target'])
            self.assertIsNone(entry(signal,time(b)+STEP,100))
            self.assertIsNone(entry(signal,time(b)+STEP,b['close'],validating=True))
            self.assertIsNone(entry(signal,time(b)+2*STEP,b['close']))
        self.assertIsNone(pa09(base+[bar(60,98.6,99,98,98.6)]))
        with self.assertRaises(ValueError): pa09(base+[{**bar(60),'is_closed':False}])

    def test_order_and_serialization_in_both_directions(self):
        for side,d in [('LONG',1),('SHORT',-1)]:
            base=[bar(i) for i in range(40)]
            samples=[(101,104,101,104),(102,103,102,103),(104,106,104,105),(102,104,102,103),(103,105,103,105)]
            for i,(o,h,l,c) in enumerate(samples,40):
                if d<0:o,h,l,c=200-o,200-l,200-h,200-c
                base.append(bar(i,o,h,l,c))
            state=None
            for i,phase in enumerate(['BREAKOUT_CANDIDATE','BREAKOUT_CANDIDATE','EXTENSION_CONFIRMED','RETEST_OBSERVED','CONFIRMED'],41):
                state,signal=pa10(base[:i],state)
                self.assertEqual(state['phase'],phase)
                state=json.loads(json.dumps(state))
                self.assertEqual(pa10(base[:i],state),(state,None))
                self.assertEqual(state['level'],100+2*d)
                if phase!='CONFIRMED': self.assertIsNone(signal)
            self.assertEqual(signal['side'],side)
            self.assertIsNotNone(entry(signal,time(base[-1])+STEP,base[-1]['close']))

    def test_expiry_and_gap(self):
        base=[bar(i) for i in range(40)]+[bar(40,101,104,101,104)]
        state,_=pa10(base)
        for i in range(41,58):
            base.append(bar(i,102,103,102,103))
            state,signal=pa10(base,state)
            self.assertIsNone(signal)
        self.assertIsNone(state)
        with self.assertRaises(ValueError): pa10(base+[bar(59)])
