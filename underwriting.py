"""Reverse-valuation underwriting model for garage / parking-space acquisitions.

Scope (by design):
  * OpEx excluded - the lessee covers utilities and bendrija/HOA fees.
  * Taxes excluded.

Formulas:
  Gross Annual Rent       = Monthly Rent * 12
  Effective Annual Income = Gross Annual Rent * (1 - Vacancy Rate)
  Acquisition Costs       = Notary/Registru centras fees + immediate capex
  Max All-In Budget(y)    = Effective Annual Income / y
  Max Offer Price(y)      = max(0, Max All-In Budget(y) - Acquisition Costs)
  Required Discount(y)    = max(0, (Asking - Max Offer(y)) / Asking)
  Implied Yield @ Asking  = Effective Annual Income / (Asking + Acquisition Costs)
"""

from __future__ import annotations

from dataclasses import dataclass, field

BUY = "BUY"
NEGOTIATE = "NEGOTIATE"
PASS = "PASS"

VERDICT_COLORS = {BUY: "#1a7f37", NEGOTIATE: "#b7791f", PASS: "#c53030"}


@dataclass(frozen=True)
class Assumptions:
    monthly_rent: float
    vacancy_rate: float = 0.05          # 5.0 %
    closing_fees: float = 350.0         # notary + Registru centras
    capex: float = 200.0                # refresh / lock upgrade
    yield_low: float = 0.09             # 9.0 %
    yield_high: float = 0.10            # 10.0 %
    negotiation_threshold: float = 0.15  # max discount still worth negotiating

    def __post_init__(self) -> None:
        if self.monthly_rent < 0:
            raise ValueError("monthly_rent must be >= 0")
        if not 0 <= self.vacancy_rate < 1:
            raise ValueError("vacancy_rate must be in [0, 1)")
        if self.closing_fees < 0 or self.capex < 0:
            raise ValueError("closing_fees and capex must be >= 0")
        if self.yield_low <= 0 or self.yield_high <= 0:
            raise ValueError("target yields must be > 0")
        if self.yield_low > self.yield_high:
            raise ValueError("yield_low must be <= yield_high")
        if self.negotiation_threshold < 0:
            raise ValueError("negotiation_threshold must be >= 0")


# --- building blocks -------------------------------------------------------

def gross_annual_rent(monthly_rent: float) -> float:
    return monthly_rent * 12.0


def effective_annual_income(monthly_rent: float, vacancy_rate: float) -> float:
    return gross_annual_rent(monthly_rent) * (1.0 - vacancy_rate)


def acquisition_costs(closing_fees: float, capex: float) -> float:
    return closing_fees + capex


def max_all_in_budget(effective_income: float, target_yield: float) -> float:
    if target_yield <= 0:
        raise ValueError("target_yield must be > 0")
    return effective_income / target_yield


def max_offer_price(all_in_budget: float, acq_costs: float) -> float:
    """Price payable to the seller; never negative."""
    return max(0.0, all_in_budget - acq_costs)


def required_discount(asking_price: float | None, max_offer: float) -> float | None:
    """Fraction the asking price must drop to reach max_offer (0 if already below)."""
    if not asking_price or asking_price <= 0:
        return None
    return max(0.0, (asking_price - max_offer) / asking_price)


def implied_yield(effective_income: float, asking_price: float | None, acq_costs: float) -> float | None:
    if not asking_price or asking_price <= 0:
        return None
    return effective_income / (asking_price + acq_costs)


def yield_points(yield_low: float, yield_high: float) -> list[float]:
    """Low, midpoint and high of the target range (deduplicated, ascending)."""
    pts = [yield_low, (yield_low + yield_high) / 2.0, yield_high]
    out: list[float] = []
    for p in pts:
        p = round(p, 6)
        if p not in out:
            out.append(p)
    return out


# --- full model -----------------------------------------------------------

@dataclass
class ScenarioRow:
    target_yield: float
    max_all_in_budget: float
    max_offer_price: float
    required_discount: float | None


@dataclass
class Valuation:
    gross_annual_rent: float
    effective_annual_income: float
    acquisition_costs: float
    asking_price: float | None
    implied_yield_at_asking: float | None
    scenarios: list[ScenarioRow] = field(default_factory=list)
    verdict: str | None = None
    verdict_reason: str = ""


def verdict(asking_price: float | None, offer_at_low_yield: float, negotiation_threshold: float) -> tuple[str | None, str]:
    """
    BUY       - asking price already delivers >= the minimum target yield.
    NEGOTIATE - needs a discount of at most `negotiation_threshold` to hit it.
    PASS      - needs a deeper discount than that (or rent supports no price).
    """
    if not asking_price or asking_price <= 0:
        return None, "No asking price - verdict unavailable."
    if offer_at_low_yield <= 0:
        return PASS, "Rent does not cover acquisition costs at the target yield."
    disc = required_discount(asking_price, offer_at_low_yield) or 0.0
    if disc == 0:
        return BUY, "Asking price meets the minimum target yield as-is."
    if disc <= negotiation_threshold:
        return NEGOTIATE, f"Needs a {disc:.1%} discount (threshold {negotiation_threshold:.0%})."
    return PASS, f"Needs a {disc:.1%} discount, above the {negotiation_threshold:.0%} threshold."


def underwrite(a: Assumptions, asking_price: float | None) -> Valuation:
    gross = gross_annual_rent(a.monthly_rent)
    eff = effective_annual_income(a.monthly_rent, a.vacancy_rate)
    costs = acquisition_costs(a.closing_fees, a.capex)

    rows = []
    for y in yield_points(a.yield_low, a.yield_high):
        budget = max_all_in_budget(eff, y)
        offer = max_offer_price(budget, costs)
        rows.append(ScenarioRow(y, budget, offer, required_discount(asking_price, offer)))

    offer_low = rows[0].max_offer_price  # lowest yield -> highest affordable price
    v, reason = verdict(asking_price, offer_low, a.negotiation_threshold)

    return Valuation(
        gross_annual_rent=gross,
        effective_annual_income=eff,
        acquisition_costs=costs,
        asking_price=asking_price,
        implied_yield_at_asking=implied_yield(eff, asking_price, costs),
        scenarios=rows,
        verdict=v,
        verdict_reason=reason,
    )
