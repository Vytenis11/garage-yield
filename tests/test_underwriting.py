import pytest

import underwriting as uw
from underwriting import Assumptions, underwrite


# --- vacancy ------------------------------------------------------------

def test_gross_annual_rent():
    assert uw.gross_annual_rent(80) == 960


def test_vacancy_reduces_income():
    assert uw.effective_annual_income(80, 0.05) == pytest.approx(912.0)


def test_zero_vacancy_equals_gross():
    assert uw.effective_annual_income(100, 0.0) == 1200


@pytest.mark.parametrize("bad", [-0.01, 1.0, 1.5])
def test_invalid_vacancy_rejected(bad):
    with pytest.raises(ValueError):
        Assumptions(monthly_rent=80, vacancy_rate=bad)


# --- closing / capex deductions ------------------------------------------

def test_acquisition_costs_default():
    a = Assumptions(monthly_rent=80)
    assert uw.acquisition_costs(a.closing_fees, a.capex) == 550


def test_offer_is_budget_minus_costs():
    budget = uw.max_all_in_budget(912, 0.09)
    assert budget == pytest.approx(10133.333, rel=1e-6)
    assert uw.max_offer_price(budget, 550) == pytest.approx(9583.333, rel=1e-6)


def test_offer_floored_at_zero_when_costs_exceed_budget():
    assert uw.max_offer_price(400, 550) == 0.0


def test_negative_costs_rejected():
    with pytest.raises(ValueError):
        Assumptions(monthly_rent=80, closing_fees=-1)


# --- reverse valuation ---------------------------------------------------

def test_full_model_default_scenarios():
    v = underwrite(Assumptions(monthly_rent=80), asking_price=12000)
    assert [s.target_yield for s in v.scenarios] == [0.09, 0.095, 0.10]
    budgets = [s.max_all_in_budget for s in v.scenarios]
    assert budgets == pytest.approx([912 / 0.09, 912 / 0.095, 912 / 0.10])
    offers = [s.max_offer_price for s in v.scenarios]
    assert offers == pytest.approx([b - 550 for b in budgets])
    # higher yield target -> lower affordable price
    assert offers[0] > offers[1] > offers[2]
    assert v.scenarios[2].required_discount == pytest.approx((12000 - 8570) / 12000)


def test_implied_yield_at_asking_includes_costs():
    v = underwrite(Assumptions(monthly_rent=80), asking_price=9450)
    assert v.implied_yield_at_asking == pytest.approx(912 / 10000)


def test_discount_zero_when_asking_below_offer():
    assert uw.required_discount(5000, 9000) == 0.0


def test_discount_none_without_asking():
    assert uw.required_discount(None, 9000) is None
    assert uw.required_discount(0, 9000) is None


def test_zero_rent_gives_zero_offer_and_pass():
    v = underwrite(Assumptions(monthly_rent=0), asking_price=10000)
    assert all(s.max_offer_price == 0 for s in v.scenarios)
    assert v.verdict == uw.PASS


def test_equal_yield_bounds_dedupe():
    v = underwrite(Assumptions(monthly_rent=80, yield_low=0.1, yield_high=0.1), 9000)
    assert len(v.scenarios) == 1


def test_inverted_yield_range_rejected():
    with pytest.raises(ValueError):
        Assumptions(monthly_rent=80, yield_low=0.11, yield_high=0.10)


def test_zero_yield_rejected():
    with pytest.raises(ValueError):
        uw.max_all_in_budget(1000, 0)


def test_spec_formula_chain_no_opex_no_tax():
    """Effective = rent*12*(1-vac); budget = eff/yield; offer = budget - (350 + 200)."""
    v = underwrite(Assumptions(monthly_rent=127.5), asking_price=17500)
    eff = 127.5 * 12 * 0.95
    assert v.effective_annual_income == pytest.approx(eff)
    for s, y in zip(v.scenarios, (0.09, 0.095, 0.10)):
        assert s.max_all_in_budget == pytest.approx(eff / y)
        assert s.max_offer_price == pytest.approx(eff / y - 550)


def test_comps_rent_estimate_feeds_model_monotonically():
    low = underwrite(Assumptions(monthly_rent=85), 12000).scenarios[0].max_offer_price
    high = underwrite(Assumptions(monthly_rent=127.5), 12000).scenarios[0].max_offer_price
    assert high > low


# --- verdict ------------------------------------------------------------

def test_verdict_buy():
    # offer @9% = 9583.33
    v = underwrite(Assumptions(monthly_rent=80), asking_price=9500)
    assert v.verdict == uw.BUY


def test_verdict_buy_exactly_at_threshold_price():
    offer = 912 / 0.09 - 550
    v = underwrite(Assumptions(monthly_rent=80), asking_price=offer)
    assert v.verdict == uw.BUY


def test_verdict_negotiate():
    v = underwrite(Assumptions(monthly_rent=80), asking_price=10500)  # ~8.7% discount
    assert v.verdict == uw.NEGOTIATE


def test_verdict_pass():
    v = underwrite(Assumptions(monthly_rent=80), asking_price=15000)  # ~36% discount
    assert v.verdict == uw.PASS


def test_verdict_none_without_price():
    v = underwrite(Assumptions(monthly_rent=80), asking_price=None)
    assert v.verdict is None
    assert v.implied_yield_at_asking is None
