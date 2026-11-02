import pytest
from decimal import Decimal as D
from backend.ledger import Ledger


def test_frozen_shared_ledger_does_not_advertise_e_execution(tmp_path):
    ledger = Ledger(tmp_path / 'isolated.sqlite')
    before = ledger.accounts()
    with pytest.raises(ValueError, match='invalid agent'):
        ledger.open_position(agent='E', direction='LONG', quantity=D('0.1'), price=D('100'),
            stop=D('90'), fee=D('0.01'), slippage=D('0'), idempotency_key='E', expected_version=0)
    assert ledger.accounts() == before
