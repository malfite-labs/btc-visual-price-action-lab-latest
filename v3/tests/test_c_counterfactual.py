from datetime import datetime,timezone
from scripts.c_counterfactual import second_signal_status

def test_single_position_at_second_signal_close():
 d=lambda s:datetime.fromisoformat(s).astimezone(timezone.utc)
 assert second_signal_status(d('2026-10-28T12:00:59+00:00'),d('2026-10-28T12:15:10+00:00'),d('2026-10-28T12:15:00+00:00'))=='NOT_OPENED_SINGLE_POSITION'
 assert second_signal_status(d('2026-10-28T12:00:59+00:00'),d('2026-10-28T12:14:59+00:00'),d('2026-10-28T12:15:00+00:00'))=='ELIGIBLE_FOR_PREFLIGHT'
