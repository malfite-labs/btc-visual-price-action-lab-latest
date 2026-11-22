"""Offline post-entry excursion measurements and bounded candidate changes."""
def excursions(bars,i,direction,horizon):
 if i<0 or i+horizon>len(bars):return None
 seq=bars[i:i+horizon]
 if any(a['close_at']!=b['open_at'] for a,b in zip(seq,seq[1:])):return None
 p=float(seq[0]['open']);high=max(float(x['high']) for x in seq);low=min(float(x['low']) for x in seq)
 close=float(seq[-1]['close']);sign=1 if direction=='LONG' else -1
 return {'mfe_pct':(high-p)/p*100 if sign==1 else (p-low)/p*100,'mae_pct':(p-low)/p*100 if sign==1 else (high-p)/p*100,'close_return_pct':sign*(close-p)/p*100}
def one_mechanism(before,after):
 groups=[]
 if before['long']!=after['long'] or before['short']!=after['short']:groups.append('entry_conditions')
 for k in ('stop_bars','target_r','max_hold'):
  if before[k]!=after[k]:groups.append(k)
 if len(groups)!=1:raise ValueError('RULE_MUST_CHANGE_EXACTLY_ONE_MECHANISM')
 return groups[0]
