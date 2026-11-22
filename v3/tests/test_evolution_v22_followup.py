from scripts.evolution_v22_followup import c_exposure, group_metrics
import pytest

def test_c_exposure_uses_actual_notional_not_max_leverage():
 t={'qty':'0.1','entry':'10000','initial_cash':'10000'}
 d=c_exposure(t)
 assert d['notional']==1000 and d['effective_leverage']==.1 and d['initial_margin']==200 and d['maintenance_requirement_entry']==10

def test_group_metrics_separates_losses():
 r=group_metrics([{'net_pnl':'10'},{'net_pnl':'-5'}])
 assert r['profit_factor']==2 and r['win_rate']==.5 and r['avg_win_loss_ratio']==2
