"""Small, deterministic research proposal and evaluation rules; no model calls."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

def choose_setup(trades, agent, minimum=40):
    groups=defaultdict(list)
    for t in trades:
        if t.get('agent')==agent and t.get('setup'):
            groups[t['setup']].append(Decimal(str(t['net_pnl'])))
    eligible=[(sum(values,Decimal(0))/len(values),name) for name,values in groups.items() if len(values)>=minimum]
    return max(eligible)[1] if eligible else None

def acceptance(base,candidate,minimum=40):
    if candidate['closed_trades']<minimum:return False,'INSUFFICIENT_TRADES'
    if not base.get('closed_trades') or base.get('profit_factor') is None or candidate.get('profit_factor') is None:return False,'INSUFFICIENT_BASELINE_OR_PROFIT_FACTOR'
    bnet=Decimal(str(base['net_pnl'])); cnet=Decimal(str(candidate['net_pnl']))
    base_mean=bnet/base['closed_trades']; candidate_mean=cnet/candidate['closed_trades']
    # Fixed minimum effect sizes prevent promoting a few cents of improvement
    # merely because the filter traded less. They are research gates, not proof.
    minimum_mean_gain=max(Decimal('1'),abs(base_mean)*Decimal('.10'))
    checks=(cnet>bnet, candidate_mean-base_mean>=minimum_mean_gain, Decimal(str(candidate['profit_factor']))-Decimal(str(base['profit_factor']))>=Decimal('.05'), Decimal(str(candidate['max_drawdown_percent']))<=Decimal(str(base['max_drawdown_percent'])))
    return (True,'ALL_RESEARCH_GATES_MET') if all(checks) else (False,'NO_CONSISTENT_IMPROVEMENT')

def walk_forward_windows(start,end,rounds):
    a=date.fromisoformat(start);b=date.fromisoformat(end)
    if not 1<=rounds<=10 or (b-a).days<60*(rounds+1):raise ValueError('range too short for requested rounds')
    total=(b-a).days;first=a+timedelta(days=total//2);remain=(b-first).days
    output=[]
    for i in range(rounds):
        t=first+timedelta(days=remain*i//rounds);e=first+timedelta(days=remain*(i+1)//rounds)
        output.append({'start':a.isoformat(),'train_end':t.isoformat(),'validation_end':e.isoformat()})
    return output

def eligible_coverage(bars,start,end):
    """Count 288-closed-bar windows with 12 subsequent available bars only."""
    segments=[];run=[]
    for bar in bars:
        if run and run[-1]['close_at']!=bar['open_at']:
            segments.append(run);run=[]
        run.append(bar)
    if run:segments.append(run)
    eligible=[seg[i]['close_at'] for seg in segments for i in range(287,len(seg)-12) if start<=seg[i]['close_at']<end]
    return {'eligible_bars':len(eligible),'valid_days':len({x[:10] for x in eligible}),'method':'UTC day with at least one eligible bar; 288 prior/including current and 12 future data availability only'}
