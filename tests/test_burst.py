from bitgn_contest_agent.bench.burst import (
    LADDER, pick_operating_point, InsufficientHeadroomError
)


def test_ladder_is_fixed():
    assert LADDER == [4, 8, 16, 32, 48, 64, 96]


def test_pick_operating_point_below_eight_raises():
    import pytest
    with pytest.raises(InsufficientHeadroomError):
        pick_operating_point(first_break_level=4, errors_at_break=5)


def test_pick_operating_point_normal_case():
    # First break at N=32, formula: floor(0.6 * 32) = 19
    assert pick_operating_point(first_break_level=32, errors_at_break=5) == 19


def test_pick_operating_point_cleared_through_ceiling():
    # None means we cleared every level without breaking
    assert pick_operating_point(first_break_level=None, errors_at_break=0) == 48


def test_pick_operating_point_at_exactly_eight():
    # Boundary: N=8 is accepted, floor(0.6 * 8) = 4
    assert pick_operating_point(first_break_level=8, errors_at_break=3) == 4
