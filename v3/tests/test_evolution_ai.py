from scripts.evolution_ai import validate_proposal, reservation
from decimal import Decimal
import pytest

def test_proposal_requires_executable_setup_and_reason():
    assert validate_proposal('A', {'setup':'A_PULLBACK','analysis':'losses after costs','reason':'test pullbacks'}) == 'A_PULLBACK'
    with pytest.raises(ValueError): validate_proposal('A', {'setup':'T2','analysis':'x','reason':'x'})
    with pytest.raises(ValueError): validate_proposal('A', {'setup':'A_PULLBACK','analysis':'','reason':'x'})

def test_reservation_is_conservative():
    assert reservation('abc') == (Decimal(1283)*Decimal('.30')+Decimal(1000)*Decimal('1.20'))/Decimal(1000000)
