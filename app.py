"""Garage / parking-space underwriting dashboard.

Run:  .venv\\Scripts\\streamlit run app.py
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from comps import (
    DEFAULT_PIT_PREMIUM,
    MIN_PIT_MATCH,
    TIER_ORDER,
    Comp,
    CompsResult,
    apply_pit_adjustment,
    comps_from_text,
    gather_comps,
    rent_estimate,
)
from districts import normalize_district
from scraper import (
    AMENITIES,
    GARAGE_TYPE_LABELS,
    GARAGE_TYPES,
    PARKING_TYPE,
    Listing,
    ScrapeBlocked,
    ScrapeError,
    detect_source,
    fetch_listing,
    parse_listing,
)
from underwriting import VERDICT_COLORS, Assumptions, underwrite

st.set_page_config(page_title="Garage Yield Underwriter", page_icon="🅿️", layout="wide")

TYPE_OPTIONS = ["", *GARAGE_TYPES.keys(), PARKING_TYPE]
AMENITY_LABELS = {"electricity": "Electricity", "heating": "Heating", "security": "Security",
                  "pit": "Has pit / Duobė"}

ss = st.session_state
for k, v in {"subject": None, "comps_result": None, "comps_key": None, "comps_error": "",
             "manual_comps": [], "rent": 60.0, "rent_auto": None}.items():
    ss.setdefault(k, v)


def eur(x: float | None) -> str:
    return "—" if x is None else f"€{x:,.0f}".replace(",", " ")


def pct(x: float | None, d: int = 1) -> str:
    return "—" if x is None else f"{x * 100:.{d}f}%"


# ---------------------------------------------------------------- cached data access
# Both caches are keyed on everything that changes the answer, so two different
# listings can never share a result.

@st.cache_data(ttl=3600, show_spinner=False, max_entries=100)
def cached_listing(listing_url: str) -> Listing:
    return fetch_listing(listing_url)


@st.cache_data(ttl=3600, show_spinner=False, max_entries=100)
def cached_comps(listing_url: str, city: str, district: str, has_pit: bool, garage_type: str) -> CompsResult:
    subj = Listing(source=detect_source(listing_url) if listing_url else "text", url=listing_url, city=city,
                   district=district, garage_type=garage_type, amenities={**{k: False for k in AMENITIES},
                                                                          "pit": has_pit})
    return gather_comps(subj)


def set_subject(lst: Listing) -> None:
    ss.subject = lst
    ss.manual_comps = []
    ss["f_price"] = float(lst.asking_price or 0.0)
    ss["f_area"] = float(lst.area_m2 or 0.0)
    ss["f_city"], ss["f_district"], ss["f_street"] = lst.city, lst.district, lst.street
    ss["f_type"] = lst.garage_type if lst.garage_type in TYPE_OPTIONS else ""
    for k in AMENITIES:
        ss[f"f_am_{k}"] = bool(lst.amenities.get(k))
    ss.rent_auto = None  # let the new comps set the rent


def current_comps_key() -> tuple:
    city = ss.get("f_city", "")
    return (ss.subject.url if ss.subject else "", city, normalize_district(ss.get("f_district", ""), city),
            bool(ss.get("f_am_pit", False)), ss.get("f_type", ""))


def load_comps(key: tuple) -> None:
    ss.comps_key, ss.comps_error = key, ""
    with st.spinner(f"Searching rental comps for {key[2] or key[1] or 'subject'} "
                    "(exact district → adjacent districts, then reading each ad) …"):
        try:
            ss.comps_result = cached_comps(*key)
        except ScrapeError as e:
            ss.comps_result = None
            ss.comps_error = f"Comps search failed: {e}. Paste rental ads below instead."


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Assumptions")
    st.caption("OpEx (utilities, bendrija/HOA) borne by lessee; taxes excluded.")
    vacancy = st.number_input("Vacancy & bad debt (%)", 0.0, 50.0, 5.0, 0.5) / 100
    closing = st.number_input("Notary & Registrų centras fees (€)", 0.0, 10000.0, 350.0, 25.0)
    capex = st.number_input("Immediate refresh / lock capex (€)", 0.0, 20000.0, 200.0, 25.0)
    y_lo, y_hi = st.slider("Target yield range (%)", 3.0, 20.0, (9.0, 10.0), 0.25)
    neg_thr = st.slider("Max discount still worth negotiating (%)", 0.0, 50.0, 15.0, 1.0,
                        help="Verdict: BUY if asking meets the low-end yield, NEGOTIATE if the required "
                             "discount is within this threshold, otherwise PASS.") / 100
    st.header("Comps")
    pit_premium = st.slider("Inspection Pit Rent Premium (%)", 0, 100, int(DEFAULT_PIT_PREMIUM * 100), 5,
                            help="Used to normalize comps whose pit status differs from the subject: "
                                 "non-pit comp × (1+p) for a pit subject, pit comp ÷ (1+p) for a no-pit subject. "
                                 f"Applied only when fewer than {MIN_PIT_MATCH} pit-matched comps exist.") / 100
    only_exact_pit = st.toggle("Only match exact pit status", value=False,
                               help="On: show only comps with the subject's pit status, raw rents (no adjustment).")

# ---------------------------------------------------------------- input
st.title("🅿️ Garage Yield Underwriter")
st.caption("Reverse-valuation for Lithuanian garage & parking listings — Aruodas.lt · Skelbiu.lt · Domoplius.lt")

c1, c2 = st.columns([5, 1])
url = c1.text_input("Sale listing URL", placeholder="https://www.skelbiu.lt/skelbimai/...",
                    label_visibility="collapsed")
if c2.button("Analyze", type="primary", width="stretch") and url.strip():
    with st.spinner("Rendering listing with headless Chromium …"):
        try:
            set_subject(cached_listing(url.strip()))
        except ScrapeBlocked as e:
            st.error(f"The portal blocked automated access ({e}). Open the ad in your browser, "
                     "copy the page (Ctrl+A, Ctrl+C) and paste it below.")
        except ScrapeError as e:
            st.error(f"Could not fetch the listing: {e}")

with st.expander("Paste Ad Text / HTML (fallback if the portal shows a CAPTCHA)", expanded=ss.subject is None):
    pasted = st.text_area("Ad text or page HTML", height=160, key="pasted_ad")
    if st.button("Parse pasted ad") and pasted.strip():
        set_subject(parse_listing(pasted, url=url.strip()))

subject: Listing | None = ss.subject
if subject is None:
    st.info("Enter a listing URL or paste the ad text to begin.")
    st.stop()

# comps follow the (possibly hand-edited) subject: any change of the cache key refetches
key = current_comps_key()
if key != ss.comps_key:
    load_comps(key)
result: CompsResult | None = ss.comps_result

# ---------------------------------------------------------------- verification panel
with st.container(border=True):
    st.markdown("**Extraction check** — what the comps engine is searching for")
    v = st.columns(6)
    v[0].markdown(f"City  \n**{key[1] or '—'}**")
    v[1].markdown(f"District  \n**{key[2] or '—'}**")
    v[2].markdown(f"Street  \n**{ss.get('f_street') or '—'}**")
    v[3].markdown(f"Area m²  \n**{ss.get('f_area') or '—'}**")
    v[4].markdown(f"Garage type  \n**{GARAGE_TYPE_LABELS.get(key[4], key[4])}**")
    v[5].markdown(f"Has pit / Duobė  \n**{'Yes' if key[3] else 'No'}**")
    if result and result.searches:
        st.dataframe(pd.DataFrame([{"Tier": s.tier, "Portal": s.source, "Search URL": s.url, "Result": s.status}
                                   for s in result.searches]),
                     hide_index=True, width="stretch",
                     column_config={"Search URL": st.column_config.LinkColumn()})
    if ss.comps_error:
        st.warning(ss.comps_error)

# ---------------------------------------------------------------- subject editors
st.subheader("Subject property")
if subject.title:
    st.markdown(f"**{subject.title}**" + (f" · [open ad]({subject.url})" if subject.url else ""))
a, b, c, d = st.columns(4)
asking = a.number_input("Asking price (€)", 0.0, 5_000_000.0, step=500.0, key="f_price") or None
area = b.number_input("Area (m²)", 0.0, 2000.0, step=0.5, key="f_area") or None
c.text_input("City", key="f_city")
d.selectbox("Garage type", TYPE_OPTIONS, key="f_type", format_func=lambda x: GARAGE_TYPE_LABELS.get(x, x))
e, f = st.columns(2)
e.text_input("Micro-district (mikrorajonas) — any case form, e.g. Antakalnyje", key="f_district")
f.text_input("Street", key="f_street")
for col, k in zip(st.columns(len(AMENITIES)), AMENITIES):
    col.checkbox(AMENITY_LABELS.get(k, k.capitalize()), key=f"f_am_{k}")
if not subject.asking_price:
    st.warning("Asking price not detected — enter it above.")
st.caption("Editing city, district, garage type or pit status re-runs the comps search for the new attributes.")

# ---------------------------------------------------------------- comps
st.subheader("Rental comparables")
if result:
    for note in result.notes:
        st.caption(note)
if st.button("Clear cache & refetch comps"):
    cached_comps.clear()
    load_comps(key)
    st.rerun()

with st.expander("Paste rental ads manually (one ad per line / paragraph)"):
    pasted_comps = st.text_area("Rental ads text", height=120, key="pasted_comps")
    if st.button("Add pasted comps") and pasted_comps.strip():
        ss.manual_comps = comps_from_text(pasted_comps, key[1])

all_comps: list[Comp] = (result.comps if result else []) + ss.manual_comps
rows = apply_pit_adjustment(all_comps, subject_has_pit=key[3], subject_type=key[4],
                            pit_premium=pit_premium, only_exact_pit=only_exact_pit)

df = pd.DataFrame([{
    "include": r.include,
    "Location tier": r.comp.tier,
    "District": r.comp.district or "?",
    "Pit": "Yes" if r.comp.has_pit else "No",
    "Type": GARAGE_TYPE_LABELS.get(r.comp.garage_type, r.comp.garage_type),
    "Elec.": "Yes" if r.comp.has_electricity else "No",
    "Rent €/mo (raw)": r.comp.monthly_rent,
    "Adjusted €/mo": r.adjusted_rent,
    "Area m²": r.comp.area_m2,
    "Adj. €/m²": round(r.adjusted_rent / r.comp.area_m2, 2) if r.comp.area_m2 else None,
    "Note": r.note + ("" if r.comp.enriched or r.comp.tier == "Manual" else " (card only)"),
    "Title": r.comp.title,
    "url": r.comp.url,
} for r in rows])

if df.empty:
    st.info("No comps yet. Paste rental ads above, or set the rent manually below.")
    estimate = {"n": 0, "median_rent": None, "median_rent_per_m2": None}
else:
    editor_key = f"comps_{hash((key, pit_premium, only_exact_pit, len(all_comps)))}"
    edited = st.data_editor(
        df, width="stretch", hide_index=True, key=editor_key,
        disabled=[c for c in df.columns if c != "include"],
        column_config={"include": st.column_config.CheckboxColumn(),
                       "url": st.column_config.LinkColumn(display_text="open")},
    )
    for r, inc in zip(rows, edited["include"].tolist()):
        r.include = bool(inc)
    estimate = rent_estimate(rows)
    tiers = pd.Series([r.comp.tier for r in rows if r.include]).value_counts()
    st.caption("Included by tier: " + ", ".join(f"{t}: {tiers.get(t, 0)}"
                                                for t in sorted(TIER_ORDER, key=TIER_ORDER.get) if tiers.get(t, 0)))

med, med_m2 = estimate["median_rent"], estimate["median_rent_per_m2"]
m1, m2, m3, m4 = st.columns(4)
m1.metric("Comps used", estimate["n"])
m2.metric("Median adj. rent / mo", eur(med) if med else "—")
m3.metric("Median adj. €/m² / mo", f"€{med_m2:.2f}" if med_m2 else "—")
m4.metric("€/m² × area / mo", eur(med_m2 * area) if med_m2 and area else "—")

# rent follows the comps estimate until the user types their own value
if med and (ss.rent_auto is None or ss.rent == ss.rent_auto):
    ss.rent = ss.rent_auto = float(round(med))
r1, r2, r3 = st.columns([2, 1, 1])
if r2.button("Use median rent", disabled=not med, width="stretch"):
    ss.rent = ss.rent_auto = float(round(med))
if r3.button("Use €/m² × area", disabled=not (med_m2 and area), width="stretch"):
    ss.rent = float(round(med_m2 * area))
rent = r1.number_input("**Assumed monthly rent (€)** — override as needed", 0.0, 10000.0, step=5.0, key="rent")

# ---------------------------------------------------------------- underwriting
st.subheader("Underwriting")
try:
    val = underwrite(Assumptions(rent, vacancy, closing, capex, y_lo / 100, y_hi / 100, neg_thr), asking)
except ValueError as err:
    st.error(str(err))
    st.stop()

k1, k2, k3, k4 = st.columns(4)
k1.metric("Gross annual rent", eur(val.gross_annual_rent))
k2.metric("Effective annual rent", eur(val.effective_annual_income), f"-{pct(vacancy)} vacancy",
          delta_color="off")
k3.metric("Closing + capex", eur(val.acquisition_costs))
k4.metric("Yield at asking", pct(val.implied_yield_at_asking, 2))

st.dataframe(pd.DataFrame([{
    "Target yield": pct(s.target_yield, 2),
    "Max all-in budget": eur(s.max_all_in_budget),
    "Max offer to seller": eur(s.max_offer_price),
    "Required discount vs asking": pct(s.required_discount),
} for s in val.scenarios]), hide_index=True, width="stretch")

if val.verdict:
    color = VERDICT_COLORS[val.verdict]
    st.markdown(
        f"<div style='display:flex;align-items:center;gap:14px;margin:6px 0 4px'>"
        f"<span style='background:{color};color:#fff;font-weight:700;font-size:1.3rem;"
        f"padding:6px 18px;border-radius:8px;letter-spacing:.05em'>{val.verdict}</span>"
        f"<span>{val.verdict_reason}</span></div>", unsafe_allow_html=True)
else:
    st.info(val.verdict_reason)

with st.expander("Formulas"):
    st.markdown(f"""
- Effective annual rent = {eur(rent)} × 12 × (1 − {pct(vacancy)}) = **{eur(val.effective_annual_income)}**
- Acquisition costs = {eur(closing)} notary/registry + {eur(capex)} lock/refresh = **{eur(val.acquisition_costs)}**
- Max all-in budget = effective annual rent ÷ target yield
- Max offer = max all-in budget − acquisition costs
- Required discount = (asking − max offer) ÷ asking, floored at 0
- Pit adjustment: comps with a different pit status are scaled by ±{pit_premium:.0%} only when fewer than
  {MIN_PIT_MATCH} pit-matched comps exist (or toggle "Only match exact pit status" to see raw comps).
- OpEx and taxes are excluded by assumption.
""")
