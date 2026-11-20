"""Comps engine: tiered location matching, district normalization, pit segmentation.

A fake portal (search pages + detail pages) is injected as the fetcher, so the whole
gather_comps pipeline runs offline and deterministically.
"""
import pytest

from comps import (
    ADJACENT,
    EXACT,
    Comp,
    apply_pit_adjustment,
    card_district,
    card_price,
    comps_from_text,
    gather_comps,
    parse_results_page,
    rent_estimate,
    search_url,
)
from districts import adjacent_districts, find_district, normalize_district
from scraper import Listing, ScrapeError, detect_pit

# ------------------------------------------------------------------ fake portal

_ID = iter(range(8_100_000, 8_200_000))


def _ad(district, rent, area, pit, street="Testo g."):
    """(card html, detail url, detail html) for one rental ad."""
    ad_id = next(_ID)
    url = f"https://www.skelbiu.lt/skelbimai/isnuomojamas-garazas-{ad_id}.html"
    card = (f'<article><a href="/skelbimai/isnuomojamas-garazas-{ad_id}.html">Išnuomojamas garažas</a> '
            f'{rent} € ({rent / area:.2f} €/m²) {area} m² Vilnius, {district}, {street}</article>')
    pit_txt = "Garaže yra apžiūros duobė, rūsys." if pit else "Garažas sausas, be rūsio ir duobės."
    detail = (f"<html><body><h1>Vilnius, {district}, {street}, mūrinis garažas nuomai</h1>"
              f"<p>Kaina: {rent} €</p><p>Plotas: {area} m²</p><p>Aprašymas:</p>"
              f"<p>Išnuomojamas mūrinis garažas. Elektra įvesta. {pit_txt}</p>"
              f"<div>Panašūs skelbimai: garažas su duobe Antakalnyje 150 €</div></body></html>")
    return card, url, detail


def _portal(ads_by_district: dict[str, list[tuple]]) -> dict[str, str]:
    pages = {}
    for district, ads in ads_by_district.items():
        cards = []
        for rent, area, pit in ads:
            card, url, detail = _ad(district, rent, area, pit)
            cards.append(card)
            pages[url] = detail
        pages[search_url("skelbiu", "Vilnius", district)] = f"<html><body>{''.join(cards)}</body></html>"
    return pages


def fake_fetcher(pages, calls=None):
    def fetch(urls, scroll=False):
        if calls is not None:
            calls.extend(urls)
        return {u: pages.get(u, ScrapeError(f"404 {u}")) for u in urls}
    return fetch


PAGES = _portal({
    "Žirmūnai": [(120, 18, True), (130, 18, True), (125, 17, True), (140, 20, True), (90, 18, False)],
    "Šeškinė": [(80, 18, False), (85, 18, False), (90, 19, False), (75, 16, False), (95, 18, False)],
    "Antakalnis": [(110, 18, False), (100, 17, False)],
})


def _subject(district, pit, url):
    return Listing(source="skelbiu", url=url, city="Vilnius", district=district,
                   amenities={"electricity": True, "heating": False, "security": False, "pit": pit})


# ------------------------------------------------------------------ 1. distinct comps per listing

def test_different_districts_and_pit_status_give_distinct_comps_and_rents():
    a = _subject("Žirmūnuose", True, "https://www.skelbiu.lt/skelbimai/a-1.html")   # locative on purpose
    b = _subject("Šeškinė", False, "https://www.skelbiu.lt/skelbimai/b-2.html")
    ra = gather_comps(a, fetcher=fake_fetcher(PAGES), sources=["skelbiu"])
    rb = gather_comps(b, fetcher=fake_fetcher(PAGES), sources=["skelbiu"])

    urls_a, urls_b = {c.url for c in ra.comps}, {c.url for c in rb.comps}
    assert urls_a and urls_b and urls_a.isdisjoint(urls_b)
    assert {c.district for c in ra.comps} == {"Žirmūnai"}
    assert {c.district for c in rb.comps} == {"Šeškinė"}
    assert all(c.tier == EXACT for c in ra.comps + rb.comps)
    assert ra.searches[0].url == "https://www.skelbiu.lt/paieska/garazas-nuoma-vilnius-zirmunai/"
    assert rb.searches[0].url == "https://www.skelbiu.lt/paieska/garazas-nuoma-vilnius-seskine/"

    est_a = rent_estimate(apply_pit_adjustment(ra.comps, subject_has_pit=True))
    est_b = rent_estimate(apply_pit_adjustment(rb.comps, subject_has_pit=False))
    assert est_a["median_rent"] == 127.5      # four pit comps used raw; the no-pit one excluded
    assert est_b["median_rent"] == 85
    assert est_a["median_rent"] != est_b["median_rent"]


def test_outlier_fence_is_per_pit_segment():
    from comps import remove_outliers
    pit = [_c(r, True) for r in (120, 125, 130, 140)]
    kept, dropped = remove_outliers(pit + [_c(90, False)] + [_c(400, True), _c(5, False)])
    assert 90 in [c.monthly_rent for c in kept]          # genuine no-pit comp survives
    assert {c.monthly_rent for c in dropped} == {400, 5}


def test_cache_key_fields_change_result_key():
    a = gather_comps(_subject("Žirmūnai", True, "u1"), fetcher=fake_fetcher(PAGES), sources=["skelbiu"])
    b = gather_comps(_subject("Žirmūnai", False, "u1"), fetcher=fake_fetcher(PAGES), sources=["skelbiu"])
    assert a.subject_key != b.subject_key
    assert a.subject_key == ("u1", "Vilnius", "Žirmūnai", True, "")


def test_pit_status_is_read_from_detail_description_not_widgets():
    res = gather_comps(_subject("Šeškinė", False, "u"), fetcher=fake_fetcher(PAGES), sources=["skelbiu"])
    # every detail page carries a "Panašūs skelbimai: garažas su duobe" widget; it must be ignored
    assert all(c.enriched for c in res.comps)
    assert not any(c.has_pit for c in res.comps)
    assert all(c.has_electricity and c.garage_type == "Mūrinis" for c in res.comps)


# ------------------------------------------------------------------ 2. tiered fallback

def test_tier2_expands_to_adjacent_districts_when_exact_is_thin():
    calls = []
    res = gather_comps(_subject("Antakalnyje", False, "u"), fetcher=fake_fetcher(PAGES, calls),
                       sources=["skelbiu"])
    tiers = {c.tier for c in res.comps}
    assert tiers == {EXACT, ADJACENT}
    assert {c.district for c in res.comps if c.tier == EXACT} == {"Antakalnis"}
    neighbours = adjacent_districts("Antakalnis", "Vilnius")
    assert {c.district for c in res.comps if c.tier == ADJACENT} <= set(neighbours)
    assert "Žirmūnai" in {c.district for c in res.comps if c.tier == ADJACENT}
    assert search_url("skelbiu", "Vilnius", "Žirmūnai") in calls
    assert search_url("skelbiu", "Vilnius", "Šeškinė") not in calls        # not adjacent to Antakalnis
    assert any("Tier 2" in n for n in res.notes)


def test_no_tier2_when_exact_district_has_enough():
    calls = []
    gather_comps(_subject("Šeškinė", False, "u"), fetcher=fake_fetcher(PAGES, calls), sources=["skelbiu"])
    searched = [u for u in calls if "/paieska/" in u]
    assert searched == [search_url("skelbiu", "Vilnius", "Šeškinė")]


def test_adjacency_map_examples():
    assert {"Šnipiškės", "Antakalnis", "Baltupiai"} <= set(adjacent_districts("Žirmūnai", "Vilnius"))
    assert {"Fabijoniškės", "Pašilaičiai", "Justiniškės"} <= set(adjacent_districts("Šeškinėje", "Vilnius"))
    assert {"Viršuliškės", "Lazdynai"} <= set(adjacent_districts("Karoliniškės", "Vilnius"))
    assert adjacent_districts("Dainava", "Kaunas") and adjacent_districts("Vėtrungė", "Klaipėda")


def test_search_urls_are_district_specific():
    assert search_url("skelbiu", "Vilnius", "Šiaurės miestelis") == \
        "https://www.skelbiu.lt/paieska/garazas-nuoma-vilnius-siaures-miestelis/"
    assert search_url("domoplius", "Kaunas", "Šilainiai") == \
        "https://domoplius.lt/skelbimai/garazai/nuoma/kaunas/silainiai"
    assert search_url("aruodas", "Vilnius", "Žirmūnai") == "https://www.aruodas.lt/garazai-nuomai/vilniuje/zirmunuose/"
    assert search_url("skelbiu", "Vilnius") == "https://www.skelbiu.lt/paieska/garazas-nuoma-vilnius/"


# ------------------------------------------------------------------ 3. normalization & pit keywords

@pytest.mark.parametrize("raw,city,expected", [
    ("Antakalnyje", "Vilnius", "Antakalnis"), ("Antakalnio", "Vilnius", "Antakalnis"),
    ("Žirmūnuose", "Vilnius", "Žirmūnai"), ("Žirmūnų", "Vilnius", "Žirmūnai"),
    ("Šeškinėje", "Vilnius", "Šeškinė"), ("Šeškinės", "Vilnius", "Šeškinė"),
    ("Baltupiuose", "Vilnius", "Baltupiai"), ("Fabijoniškėse", "Vilnius", "Fabijoniškės"),
    ("Šiaurės miestelyje", "Vilnius", "Šiaurės miestelis"), ("zirmunai", "Vilnius", "Žirmūnai"),
    ("Dainavoje", "Kaunas", "Dainava"), ("Centre", "Kaunas", "Centras"), ("Vėtrungėje", "Klaipėda", "Vėtrungė"),
    ("Kažkuriuose", "Vilnius", "Kažkuriai"),   # unknown name: generic locative -> nominative
])
def test_district_normalization(raw, city, expected):
    assert normalize_district(raw, city) == expected


def test_find_district_ignores_street_names():
    assert find_district("Vilnius, Šnipiškės, Verkių g. 5", "Vilnius") == "Šnipiškės"
    assert find_district("Išnuomoju požeminį garažą Justiniškėse", "Vilnius") == "Justiniškės"
    assert card_district("110 € 18 m² Vilnius, Viršuliškės, Viršuliškių g.", "Vilnius") == "Viršuliškės"


@pytest.mark.parametrize("text,expected", [
    ("Mūrinis garažas su duobe", True),
    ("Yra apžiūros duobė ir rūsys", True),
    ("Garažas su apžiūros duobe, elektra", True),
    ("Garažas be duobės", False),
    ("Sausas, be rūsio ir duobės.", False),
    ("Duobės nėra, bet yra elektra", False),
    ("Nėra elektros, yra duobė", True),
    ("Požeminė stovėjimo vieta", False),
    ("Dėl apžiūros skambinkite tel. +370", False),     # "apžiūra" = viewing, not a pit
    ("Apžiūros laikas suderinamas", False),
])
def test_pit_keyword_detection(text, expected):
    assert detect_pit(text) is expected


# ------------------------------------------------------------------ 4. pit segmentation

def _c(rent, pit, tier=EXACT, gtype="Mūrinis"):
    return Comp(title="x", monthly_rent=rent, area_m2=18, url=f"u{rent}{pit}", tier=tier, has_pit=pit,
                garage_type=gtype, district="Žirmūnai")


def test_pit_subject_with_few_pit_comps_uses_premium_on_non_pit():
    rows = apply_pit_adjustment([_c(130, True), _c(100, False), _c(90, False)], subject_has_pit=True,
                                pit_premium=0.30)
    by_rent = {r.comp.monthly_rent: r for r in rows}
    assert by_rent[100].include and by_rent[100].adjusted_rent == 130.0 and "pit-adjusted +30%" in by_rent[100].note
    assert by_rent[130].adjusted_rent == 130 and by_rent[130].note == ""
    assert rent_estimate(rows)["median_rent"] == pytest.approx(130.0)


def test_pit_subject_with_enough_pit_comps_excludes_non_pit():
    rows = apply_pit_adjustment([_c(130, True), _c(140, True), _c(120, True), _c(80, False)], subject_has_pit=True)
    non_pit = next(r for r in rows if not r.pit_match)
    assert not non_pit.include and "excluded" in non_pit.note
    assert rent_estimate(rows)["median_rent"] == 130


def test_no_pit_subject_discounts_pit_comps():
    rows = apply_pit_adjustment([_c(130, True), _c(90, False)], subject_has_pit=False, pit_premium=0.30)
    pit_row = next(r for r in rows if r.comp.has_pit)
    assert pit_row.include and pit_row.adjusted_rent == pytest.approx(100.0)
    assert "pit-adjusted -23%" in pit_row.note


def test_only_exact_pit_toggle_shows_raw_matching_comps():
    rows = apply_pit_adjustment([_c(130, True), _c(100, False)], subject_has_pit=True, only_exact_pit=True)
    assert [(r.comp.monthly_rent, r.adjusted_rent) for r in rows] == [(130, 130)]


def test_premium_slider_changes_estimate():
    comps = [_c(130, True), _c(100, False), _c(100, False)]
    lo = rent_estimate(apply_pit_adjustment(comps, True, pit_premium=0.10))["median_rent"]
    hi = rent_estimate(apply_pit_adjustment(comps, True, pit_premium=0.50))["median_rent"]
    assert lo == pytest.approx(110) and hi == pytest.approx(150)   # [130,110,110] vs [130,150,150]


def test_sort_prefers_exact_tier_then_pit_then_type():
    rows = apply_pit_adjustment([_c(90, False, ADJACENT), _c(100, False, EXACT, "Metalinis"),
                                 _c(110, True, EXACT, "Metalinis"), _c(120, True, EXACT)],
                                subject_has_pit=True, subject_type="Mūrinis")
    assert [r.comp.monthly_rent for r in rows] == [120, 110, 100, 90]


def test_negative_premium_rejected():
    with pytest.raises(ValueError):
        apply_pit_adjustment([_c(100, True)], True, pit_premium=-0.1)


# ------------------------------------------------------------------ parsing helpers

def test_card_price_strips_glued_house_number():
    assert card_price("Nuomoju garažą S. Grunau g.44 110 € (6,88 €/m²)", 16) == 110
    assert card_price("1 110 € 7 €/m² 15 m²", 15) == 110
    assert card_price("1 968 € 15 €/m² 131.2 m²", 131.2) == 1968


def test_results_page_filters_sales_other_cities_and_lockers():
    html = """<html><body>
    <article><a href="/skelbimai/garazo-nuoma-vilniuje-a-8000001.html">x</a> 90 € 5 €/m² 18 m² Vilnius, Žirmūnai</article>
    <article><a href="/skelbimai/garazo-nuoma-kaune-b-8000002.html">x</a> 70 € 4 €/m² 18 m² Kaunas, Centras</article>
    <article><a href="/skelbimai/parduodamas-garazas-c-8000003.html">x</a> 15 000 € 833 €/m² 18 m² Vilnius</article>
    <article><a href="/skelbimai/sandeliukas-d-8000004.html">x</a> 24 € 12 €/m² 2 m² Vilnius, Žirmūnai</article>
    </body></html>"""
    comps = parse_results_page(html, "https://domoplius.lt/skelbimai/garazai/nuoma/vilnius", "Vilnius")
    assert [(c.monthly_rent, c.district) for c in comps] == [(90, "Žirmūnai")]


def test_comps_from_pasted_text_extracts_pit_and_district():
    comps = comps_from_text("Garažas Žirmūnuose su duobe 18 m² 120 €\nPožeminė vieta Šeškinėje 12 m2 60 eur",
                            "Vilnius")
    assert [(c.monthly_rent, c.district, c.has_pit) for c in comps] == [(120, "Žirmūnai", True),
                                                                       (60, "Šeškinė", False)]
