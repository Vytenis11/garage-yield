"""Headless dry-run of app.py via Streamlit's AppTest, with the portal fetcher faked (no network)."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import scraper
from test_comps import PAGES, fake_fetcher

APP = str(Path(__file__).resolve().parents[1] / "app.py")

AD_PIT = """Vilnius, Žirmūnai, Kalvarijų g., mūrinis garažas
Kaina: 10 500 €
Plotas: 18 m²
Aprašymas: Elektra, apžiūros duobė, apsauga
"""
AD_NOPIT = """Vilnius, Šeškinė, Ukmergės g., mūrinis garažas
Kaina: 10 500 €
Plotas: 18 m²
Aprašymas: Elektra, be duobės
"""


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    calls = []
    monkeypatch.setattr(scraper, "fetch_many", fake_fetcher(PAGES, calls))
    return calls


def _run_paste(ad):
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.text_area(key="pasted_ad").input(ad)
    next(b for b in at.button if b.label == "Parse pasted ad").click()
    return at.run()


def _comps_table(at):
    return at.data_editor[0].value if at.data_editor else None


def test_app_loads_empty():
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert any("Enter a listing URL" in i.value for i in at.info)


def test_two_listings_get_different_comps_and_rent():
    a = _run_paste(AD_PIT)
    b = _run_paste(AD_NOPIT)
    assert not a.exception and not b.exception
    ta, tb = _comps_table(a), _comps_table(b)
    assert set(ta["District"]) == {"Žirmūnai"} and set(tb["District"]) == {"Šeškinė"}
    assert set(ta["url"]).isdisjoint(set(tb["url"]))
    assert a.number_input(key="rent").value == 128      # 127.5 rounded: pit comps, Žirmūnai
    assert b.number_input(key="rent").value == 85
    assert "Exact District" in set(ta["Location tier"])
    md = " ".join(m.value for m in a.markdown)
    assert "Žirmūnai" in md and "Has pit / Duobė" in md


def test_search_urls_shown_at_top():
    at = _run_paste(AD_PIT)
    searches = at.dataframe[0].value
    assert "https://www.skelbiu.lt/paieska/garazas-nuoma-vilnius-zirmunai/" in set(searches["Search URL"])


def test_pit_toggle_and_verdict():
    at = _run_paste(AD_PIT)
    at.toggle[0].set_value(True).run()
    assert not at.exception
    assert set(_comps_table(at)["Pit"]) == {"Yes"}
    html = " ".join(m.value for m in at.markdown)
    assert "PASS" in html or "NEGOTIATE" in html or "BUY" in html


def test_editing_pit_status_refetches_with_new_key(offline):
    at = _run_paste(AD_PIT)
    n = len(offline)
    at.checkbox(key="f_am_pit").uncheck().run()
    assert not at.exception
    assert len(offline) > n                              # new cache key -> new search
    assert set(_comps_table(at)["Pit"]) >= {"No"}
