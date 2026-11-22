"""Research-only exposure and trade grouping."""
from statistics import mean

def c_exposure(t):
 notional=float(t['qty'])*float(t['entry']);equity=float(t['initial_cash'])
 return {'notional':notional,'effective_leverage':notional/equity,'initial_margin':notional/5,'maintenance_requirement_entry':notional*.01}
def group_metrics(trades):
 p=[float(t['net_pnl']) for t in trades];w=[v for v in p if v>0];l=[v for v in p if v<0]
 return {'count':len(p),'net_pnl':sum(p),'win_rate':len(w)/len(p) if p else None,'average_win':mean(w) if w else None,'average_loss':mean(l) if l else None,'avg_win_loss_ratio':mean(w)/abs(mean(l)) if w and l else None,'profit_factor':sum(w)/abs(sum(l)) if l else None}
