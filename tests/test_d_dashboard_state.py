import importlib.util
from pathlib import Path

path = Path(__file__).resolve().parents[1] / 'scripts' / 'local_dashboard.py'
spec = importlib.util.spec_from_file_location('dashboard_for_d_state', path)
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


def test_d_control_state_requires_explicit_execution_readiness():
    assert dashboard.d_control_state({'status': 'AUTO_PAPER', 'execution_ready': True}) == 'RUNNING'
    assert dashboard.d_control_state({'status': 'AUTO_PAPER', 'execution_ready': False}) == 'PROTECTION_UNCERTAIN'
    assert dashboard.d_control_state({'status': 'AUTO_PAPER'}) == 'PROTECTION_UNCERTAIN'
    assert dashboard.d_control_state({'status': 'PROTECTION_UNCERTAIN'}) == 'PROTECTION_UNCERTAIN'
    assert dashboard.d_control_state({'status': 'OBSERVE_ONLY'}) == 'OBSERVE_ONLY'
    assert dashboard.d_control_state({'status': 'UNAVAILABLE'}) == 'UNKNOWN'
    assert dashboard.d_control_state({'status': 'PAUSED', 'enabled': False}) == 'PAUSED'
