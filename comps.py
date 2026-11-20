"""Rental comparable engine: tiered location matching + inspection-pit segmentation.

Pipeline (gather_comps):
  1. Tier 1 - Exact District: search every portal's rental results for the subject's
     *mikrorajonas* (district-specific URLs), keep cards whose location normalizes to
     that same district (up to MAX_PER_TIER).
  2. Tier 2 - Adjacent District: if fewer than MIN_EXACT relevant exact comps, search the
     neighbouring districts from districts.py's adjacency map.
  3. City-wide: last resort only if both tiers together are still too thin.
  4. Enrichment: open each selected comp's detail page (one batched browser session)
     and extract has_pit / garage_type / has_electricity from title + attributes +
     description, so pit status is not guessed from a one-line card.
  5. Sanity bounds + IQR outlier removal.

Pit segmentation (apply_pit_adjustment) is a pure function run on every UI change:
comps matching the subject's pit status are used raw; if fewer than MIN_PIT_MATCH match,
mismatched comps are added and normalized by the Inspection Pit Rent Premium
(x(1+p) when the subject has a pit and the comp doesn't, /(1+p) the other way round).
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field, replace
from typing import Callable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from districts import adjacent_districts, district_locative, district_slug, find_district, normalize_district
from scraper import (
    CITIES,
    Listing,
    ScrapeBlocked,
    ScrapeError,
    _AREA_RE,
    classify_garage,
    detect_amenities,
    detect_source,
    fold,
    parse_listing,
    parse_number,
    parse_prices,
)

EXACT, ADJACENT, CITYWIDE, MANUAL = "Exact District", "Adjacent District", "City-wide", "Manual"
TIER_ORDER = {EXACT: 0, ADJACENT: 1, CITYWIDE: 2, MANUAL: 3}

MAX_PER_TIER = 15
MIN_EXACT = 4                          # below this, expand to adjacent districts
MIN_PIT_MATCH = 3                      # below this, mix in pit-adjusted mismatched comps
DEFAULT_PIT_PREMIUM = 0.30
RENT_MIN, RENT_MAX = 15.0, 800.0       # €/month sanity bounds for a garage / parking spot
MIN_AREA_M2 = 6.0                      # smaller = storage locker, not a garage
SOURCES = ("skelbiu", "domoplius", "aruodas")

Fetcher = Callable[..., dict]          # (urls, scroll=bool) -> {url: html | ScrapeError}

DETAIL_LINK_RE = {
    "aruodas": re.compile(r"aruodas\.lt/(?:[a-z0-9\-]+-)?\d-\d{4,}/?(?:$|\?)"),
    "skelbiu": re.compile(r"/skelbimai/.+-\d{6,}\.html"),
    "domoplius": re.compile(r"domoplius\.lt/skelbimai/.+-\d{5,}\.html"),
}
CITY_LOCATIVE = {
    "Vilnius": "vilniuje", "Kaunas": "kaune", "Klaipėda": "klaipedoje", "Šiauliai": "siauliuose",
    "Panevėžys": "panevezyje", "Alytus": "alytuje",
}
IRRELEVANT = ("parduod", "pardavim", "kambari", "butas", "buto ", "konteiner", "namas ", "sodyb", "sandeliuk")


# --------------------------------------------------------------------- model

@dataclass
class Comp:
    title: str
    monthly_rent: float
    area_m2: float | None
    url: str
    source: str = ""
    city: str = ""
    district: str = ""
    tier: str = CITYWIDE
    has_pit: bool = False
    garage_type: str = ""
    has_electricity: bool = False
    snippet: str = ""
    enriched: bool = False          # features read from the full ad, not just the card

    @property
    def rent_per_m2(self) -> float | None:
        return self.monthly_rent / self.area_m2 if self.area_m2 else None


@dataclass
class SearchLog:
    tier: str
    source: str
    url: str
    status: str


@dataclass
class CompsResult:
    subject_key: tuple
    comps: list[Comp] = field(default_factory=list)
    excluded: list[Comp] = field(default_factory=list)
    searches: list[SearchLog] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class AdjustedComp:
    comp: Comp
    include: bool
    adjusted_rent: float
    pit_match: bool
    type_match: bool
    note: str = ""


# --------------------------------------------------------------------- stats

def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def remove_outliers(comps: list[Comp]) -> tuple[list[Comp], list[Comp]]:
    """
    Drop rents outside sanity bounds, then outside the 1.5*IQR Tukey fence (n >= 4).
    The fence is applied within each pit segment: pit garages legitimately rent for more,
    so a pooled fence would discard genuine no-pit comps in a pit-heavy district.
    """
    in_bounds = [c for c in comps if RENT_MIN <= c.monthly_rent <= RENT_MAX]
    dropped = [c for c in comps if c not in in_bounds]
    kept: list[Comp] = []
    for segment in (True, False):
        seg = [c for c in in_bounds if c.has_pit == segment]
        if len(seg) >= 4:
            q1, _, q3 = statistics.quantiles([c.monthly_rent for c in seg], n=4, method="inclusive")
            lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
            dropped += [c for c in seg if not lo <= c.monthly_rent <= hi]
            seg = [c for c in seg if lo <= c.monthly_rent <= hi]
        kept += seg
    order = {id(c): i for i, c in enumerate(comps)}
    return sorted(kept, key=lambda c: order[id(c)]), dropped


# --------------------------------------------------------------------- URLs

def fallback_sources(subject: Listing) -> list[str]:
    first = subject.source if subject.source in SOURCES else "skelbiu"
    return [first] + [s for s in SOURCES if s != first]


def search_url(source: str, city: str, district: str = "") -> str:
    """Rental-search URL for a city, or for one micro-district of it."""
    c, d = district_slug(city), district_slug(district) if district else ""
    if source == "skelbiu":
        return f"https://www.skelbiu.lt/paieska/garazas-nuoma{'-' + c if c else ''}{'-' + d if d else ''}/"
    if source == "domoplius":
        return f"https://domoplius.lt/skelbimai/garazai/nuoma{'/' + c if c else ''}{'/' + d if d else ''}"
    loc = CITY_LOCATIVE.get(city, "")
    return (f"https://www.aruodas.lt/garazai-nuomai/{loc + '/' if loc else ''}"
            f"{district_locative(district) + '/' if district and loc else ''}")


# --------------------------------------------------------------------- results-page parsing

_UNIT_RE = re.compile(r"(\d[\d  ]*(?:[.,]\d+)?)\s*€\s*/\s*(?:m²|m2|kv)", re.I)
_PRICE_RAW_RE = re.compile(r"(\d{1,3}(?:[   ]\d{3})+|\d+)(?:[.,]\d{1,2})?\s*(?:€|eur)", re.I)


def card_price(text: str, area: float | None) -> float | None:
    """
    First € amount in a results card, disambiguated against the card's €/m² figure.
    Cards often glue a house number or index onto the price ("g.44 110 €" -> 44110),
    so every suffix of a space-grouped number is tried and the best price/area fit wins.
    """
    prices = parse_prices(text)
    if not prices:
        return None
    first_end, value = prices[0][1], prices[0][0]
    raw_m = next((m for m in _PRICE_RAW_RE.finditer(text) if m.end() == first_end), None)
    unit_m = _UNIT_RE.search(text)
    if raw_m is None or not area or not unit_m:
        return value
    unit = parse_number(unit_m.group(1).replace(" ", "").replace(" ", ""))
    if not unit:
        return value
    groups = re.split(r"[   ]", raw_m.group(1))
    variants = [float("".join(groups[i:])) for i in range(len(groups))]
    return min(variants, key=lambda v: abs(v / area - unit))


def _is_relevant(text: str) -> bool:
    f = fold(text)
    return not any(k in f for k in IRRELEVANT)


def _mentions_city(text: str, city: str) -> bool:
    if not city or city not in CITIES:
        return True
    return bool(set(re.findall(r"[a-z]+", fold(text))) & set(CITIES[city]))


def card_district(text: str, city: str) -> str:
    """District of a results card: 'Vilnius, Žirmūnai, Lakūnų g.' first, then any mention."""
    if city:
        m = re.search(rf"{re.escape(city)}\s*,\s*([^,|]{{3,40}})", text)
        if m:
            d = normalize_district(m.group(1).strip(), city)
            if find_district(d, city):
                return d
    return find_district(text, city)


def _card_for(a, max_len: int = 700):
    node = a
    for _ in range(8):
        node = node.parent
        if node is None or node.name in ("body", "html"):
            return None
        txt = node.get_text(" ", strip=True)
        if len(txt) > max_len:
            return None
        if "€" in txt or "eur" in txt.lower():
            return node
    return None


def parse_results_page(html: str, base_url: str, city: str = "") -> list[Comp]:
    """Rental cards on a portal search page (portal-agnostic: link -> nearest card with a € price)."""
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    source = detect_source(base_url)
    link_re = DETAIL_LINK_RE.get(source)
    seen_cards: set[int] = set()
    out: dict[str, Comp] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"]).split("#")[0].split("?")[0]
        if urlparse(href).netloc != urlparse(base_url).netloc or (link_re and not link_re.search(href)):
            continue
        card = _card_for(a)
        if card is None or id(card) in seen_cards:
            continue
        seen_cards.add(id(card))
        text = card.get_text(" ", strip=True)
        slug = urlparse(href).path.replace("-", " ").replace("/", " ")
        if not _is_relevant(text + " " + slug) or not _mentions_city(text + " " + slug, city):
            continue
        m = _AREA_RE.search(text)
        area = parse_number(m.group(1)) if m else None
        if area is not None and area < MIN_AREA_M2:
            continue
        price = card_price(text, area)
        if not price:
            continue
        feats = detect_amenities(text + " " + slug)
        out.setdefault(href, Comp(
            title=(a.get_text(" ", strip=True) or text[:90])[:120],
            monthly_rent=price,
            area_m2=area if area and area <= 2000 else None,
            url=href,
            source=source,
            city=city,
            district=card_district(text + " | " + slug, city),
            has_pit=feats["pit"],
            garage_type=classify_garage(text),
            has_electricity=feats["electricity"],
            snippet=text[:160],
        ))
    return list(out.values())


# --------------------------------------------------------------------- enrichment

def enrich(comps: list[Comp], fetcher: Fetcher) -> list[str]:
    """Read pit / type / electricity (and missing area/district) from each comp's full ad."""
    targets = [c for c in comps if c.url and not c.enriched]
    if not targets:
        return []
    pages = fetcher([c.url for c in targets], scroll=False)
    failed = []
    for c in targets:
        html = pages.get(c.url)
        if not isinstance(html, str) or not html:
            failed.append(c.url)
            continue
        d = parse_listing(html, url=c.url, rental=True)
        c.has_pit = d.has_pit
        c.has_electricity = d.has_electricity
        c.garage_type = d.garage_type or c.garage_type
        c.area_m2 = c.area_m2 or d.area_m2
        if d.district and d.city == c.city:
            c.district = d.district
        c.enriched = True
    return failed


# --------------------------------------------------------------------- tiered search

def _search(tier: str, urls: dict[str, str], city: str, fetcher: Fetcher,
            log: list[SearchLog]) -> list[Comp]:
    """Fetch {url: source} search pages; log each; return all cards."""
    pages = fetcher(list(urls), scroll=True)
    cards: list[Comp] = []
    for url, src in urls.items():
        res = pages.get(url)
        if isinstance(res, ScrapeBlocked):
            log.append(SearchLog(tier, src, url, f"blocked ({res})"))
        elif isinstance(res, Exception) or res is None:
            log.append(SearchLog(tier, src, url, f"error ({res})"))
        else:
            found = parse_results_page(res, url, city)
            log.append(SearchLog(tier, src, url, f"{len(found)} rental cards"))
            cards += found
    return cards


def _dedupe(cards: list[Comp], taken: set[str]) -> list[Comp]:
    out = []
    for c in cards:
        if c.url not in taken:
            taken.add(c.url)
            out.append(c)
    return out


def gather_comps(subject: Listing, fetcher: Fetcher | None = None, sources: list[str] | None = None,
                 max_per_tier: int = MAX_PER_TIER, min_exact: int = MIN_EXACT,
                 enrich_details: bool = True) -> CompsResult:
    import scraper  # resolved at call time so tests can substitute scraper.fetch_many

    fetcher = fetcher or scraper.fetch_many
    city = subject.city
    district = normalize_district(subject.district, city)
    sources = sources or fallback_sources(subject)
    res = CompsResult(subject_key=(subject.url, city, district, subject.has_pit, subject.garage_type))
    taken: set[str] = set()
    live_sources = list(sources)

    def usable(cards: list[Comp]) -> list[Comp]:
        kept, _ = remove_outliers(cards)
        return kept

    selected: list[Comp] = []
    if district:
        pool1 = _dedupe(_search(EXACT, {search_url(s, city, district): s for s in sources}, city, fetcher,
                                res.searches), set())
        live_sources = [l.source for l in res.searches if l.tier == EXACT and "rental cards" in l.status] or sources
        exact = [replace(c, tier=EXACT) for c in pool1 if c.district == district][:max_per_tier]
        taken.update(c.url for c in exact)
        selected += exact
        n_exact = len(usable(exact))
        res.notes.append(f"Tier 1: {n_exact} relevant comps in {district}.")

        neighbours = adjacent_districts(district, city)
        if n_exact < min_exact and neighbours:
            # aruodas is skipped for the fan-out when another portal works (it 403s headless browsers)
            srcs = [s for s in live_sources if s != "aruodas"] or live_sources
            urls = {search_url(s, city, d): s for d in neighbours for s in srcs}
            cards = _search(ADJACENT, urls, city, fetcher, res.searches)
            # keyword search for the exact district also returns some neighbouring-district ads
            adj_pool = cards + [c for c in pool1 if c.url not in taken]
            adjacent = [replace(c, tier=ADJACENT)
                        for c in _dedupe([c for c in adj_pool if c.district in neighbours], taken)]
            adjacent.sort(key=lambda c: neighbours.index(c.district))
            selected += adjacent[:max_per_tier]
            res.notes.append(f"Tier 2: only {n_exact} exact comps (< {min_exact}) - added "
                             f"{len(adjacent[:max_per_tier])} from adjacent: {', '.join(neighbours)}.")
        elif not neighbours and n_exact < min_exact:
            res.notes.append(f"No adjacency map for '{district}' in {city or 'this city'}.")
    else:
        res.notes.append("Subject district unknown - using city-wide search.")

    if len(usable(selected)) < min_exact:
        cards = _search(CITYWIDE, {search_url(s, city): s for s in live_sources}, city, fetcher, res.searches)
        extra = [replace(c, tier=CITYWIDE) for c in _dedupe(cards, taken)][:max_per_tier]
        selected += extra
        if district:
            res.notes.append(f"Still under {min_exact} - added {len(extra)} city-wide comps.")

    if enrich_details and selected:
        failed = enrich(selected, fetcher)
        if failed:
            res.notes.append(f"{len(failed)} comp detail page(s) could not be opened; their pit/type "
                             "come from the results card only.")
    res.comps, res.excluded = remove_outliers(selected)
    if res.excluded:
        res.notes.append(f"{len(res.excluded)} outlier(s) removed.")
    return res


# --------------------------------------------------------------------- pit segmentation

def apply_pit_adjustment(comps: list[Comp], subject_has_pit: bool, subject_type: str = "",
                         pit_premium: float = DEFAULT_PIT_PREMIUM, only_exact_pit: bool = False,
                         min_match: int = MIN_PIT_MATCH) -> list[AdjustedComp]:
    """
    Pit-matched comps are used at face value. With fewer than `min_match` of them,
    mismatched comps are included and normalized by the pit premium; otherwise they
    are listed but unticked. `only_exact_pit` drops mismatched comps entirely (raw view).
    """
    if pit_premium < 0:
        raise ValueError("pit_premium must be >= 0")
    n_match = sum(1 for c in comps if c.has_pit == subject_has_pit)
    rows = []
    for c in comps:
        pit_match = c.has_pit == subject_has_pit
        type_match = bool(subject_type) and c.garage_type == subject_type
        if pit_match:
            rows.append(AdjustedComp(c, True, c.monthly_rent, True, type_match))
            continue
        if only_exact_pit:
            continue
        factor = (1 + pit_premium) if subject_has_pit else 1 / (1 + pit_premium)
        sign = f"+{pit_premium:.0%}" if subject_has_pit else f"-{1 - 1 / (1 + pit_premium):.0%}"
        if n_match < min_match:
            rows.append(AdjustedComp(c, True, round(c.monthly_rent * factor, 2), False, type_match,
                                     f"pit-adjusted {sign}"))
        else:
            rows.append(AdjustedComp(c, False, round(c.monthly_rent * factor, 2), False, type_match,
                                     f"excluded: {n_match} pit-matched comps available"))
    rows.sort(key=lambda r: (TIER_ORDER.get(r.comp.tier, 9), not r.pit_match, not r.type_match))
    return rows


def rent_estimate(rows: list[AdjustedComp]) -> dict:
    inc = [r for r in rows if r.include]
    per_m2 = [r.adjusted_rent / r.comp.area_m2 for r in inc if r.comp.area_m2]
    return {"n": len(inc), "median_rent": median([r.adjusted_rent for r in inc]),
            "median_rent_per_m2": median(per_m2)}


# --------------------------------------------------------------------- manual comps

def comps_from_text(text: str, city: str = "") -> list[Comp]:
    """Pasted comps: one listing per line/paragraph containing a € price."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n|\n(?=\S)", text or "") if b.strip()]
    out = []
    for b in blocks:
        prices = [v for v, _ in parse_prices(b) if v > 0]
        if not prices:
            continue
        m = _AREA_RE.search(b)
        feats = detect_amenities(b)
        out.append(Comp(title=b[:120], monthly_rent=prices[0], area_m2=parse_number(m.group(1)) if m else None,
                        url="", city=city, district=card_district(b, city), tier=MANUAL,
                        has_pit=feats["pit"], garage_type=classify_garage(b),
                        has_electricity=feats["electricity"], snippet=b[:160]))
    return out
