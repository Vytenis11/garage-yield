from scraper import detect_amenities, feature_text, looks_blocked, parse_listing, parse_number

AD_TEXT = """Vilnius, Žirmūnai, Kalvarijų g., mūrinis garažas
Kaina: 14 500 €
(805 €/m²)
Plotas: 18 m²
Tipas: Mūrinis
Ypatybės: Elektra, Apžiūros duobė, Apsauga
Mikrorajonas: Žirmūnai
"""

AD_HTML = """<html><body><h1>Kaunas, Šilainiai, Baltų pr., požeminė stovėjimo vieta</h1>
<dl><dt>Kaina:</dt><dd>9 900 €</dd><dt>Plotas:</dt><dd>13,5 m²</dd>
<dt>Ypatybės:</dt><dd>Šildymas, Vaizdo stebėjimas</dd></dl></body></html>"""


def test_parse_number_formats():
    assert parse_number("15 000 €") == 15000
    assert parse_number("18,5 m²") == 18.5
    assert parse_number("1.250") == 1250


def test_parse_plain_text_ad():
    lst = parse_listing(AD_TEXT)
    assert lst.asking_price == 14500
    assert lst.area_m2 == 18
    assert lst.city == "Vilnius"
    assert lst.district == "Žirmūnai"
    assert lst.street.startswith("Kalvarijų")
    assert lst.garage_type == "Mūrinis"
    assert lst.has_pit and lst.has_electricity
    assert lst.amenities == {"electricity": True, "heating": False, "security": True, "pit": True}


def test_parse_html_ad():
    lst = parse_listing(AD_HTML, url="https://www.aruodas.lt/garazai-kaune-x-6-123456/")
    assert lst.source == "aruodas"
    assert lst.asking_price == 9900
    assert lst.area_m2 == 13.5
    assert lst.city == "Kaunas"
    assert lst.district == "Šilainiai"
    assert lst.garage_type == "Požeminis"
    assert lst.amenities["heating"] and lst.amenities["security"]


def test_district_from_locative_title_is_normalized():
    html = "<html><body><h1>Garažas Vilniuje Baltupiuose, Kalvarijų g.</h1><p>Kaina 25 000 €</p></body></html>"
    lst = parse_listing(html, url="https://domoplius.lt/skelbimai/parduodamas-garazas-8817322.html")
    assert lst.city == "Vilnius"
    assert lst.district == "Baltupiai"
    assert lst.street == "Kalvarijų g."
    assert lst.asking_price == 25000


def test_district_label_in_locative_is_normalized():
    lst = parse_listing("Garažas\nKaina: 9 000 €\nMikrorajonas: Antakalnyje\nVilnius")
    assert lst.district == "Antakalnis"


def test_electricity_trifazis():
    assert detect_amenities("Įvesta trifazė srovė")["electricity"]


def test_feature_text_ignores_similar_listings_widget():
    text = "Meniu\nAprašymas:\nSausas garažas be duobės.\nPanašūs skelbimai\nGaražas su duobe 150 €"
    assert "su duobe" not in feature_text("Garažas", {}, text)
    assert parse_listing(text).has_pit is False


def test_amenity_negation_in_lists():
    am = detect_amenities("Garažas sausas, švarus, be rūsio ir duobės. Elektra: įvesta elektra. "
                          "Saugumas: teritorijoje įrengti nauji vartai.")
    assert am == {"electricity": True, "heating": False, "security": True, "pit": False}
    assert detect_amenities("Nėra elektros, yra duobė") == {"electricity": False, "heating": False,
                                                            "security": False, "pit": True}


def test_blocked_detection():
    assert looks_blocked("<html><title>Just a moment...</title><body>Checking your browser</body></html>")
    assert not looks_blocked(AD_HTML)
