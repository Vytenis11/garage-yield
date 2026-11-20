"""Fetching and parsing of Lithuanian garage / parking listings.

Supported portals: aruodas.lt, skelbiu.lt, domoplius.lt (plus any pasted text/HTML).

Fetching runs Playwright (headless Chromium) in a *child process* so it never
collides with Streamlit's own event loop (a known problem on Windows).
Parsing is portal-agnostic: it combines label/value pairs found in the DOM
(dt/dd, th/td, "Label: value" lines) with regex fallbacks over the page text,
so the same logic also handles pasted ad text.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from districts import find_district, normalize_district

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/159.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1440, "height": 900}

PORTALS = ("aruodas", "skelbiu", "domoplius")

GARAGE_TYPES = {  # checked in this order
    "Požeminis": ("pozemin", "underground", "rusyje"),            # underground
    "Daugiaaukštis": ("daugiaauksc", "multi-stor", "multistor", "parking house"),  # multi-story
    "Metalinis": ("metalin", "metal"),                             # metal
    "Mūrinis": ("murin", "brick", "blokeli", "gelzbeton"),         # brick
}
PARKING_TYPE = "Stovėjimo vieta"                                   # open parking space
GARAGE_TYPE_LABELS = {
    "Požeminis": "Požeminis (underground)", "Daugiaaukštis": "Daugiaaukštis (multi-story)",
    "Metalinis": "Metalinis (metal)", "Mūrinis": "Mūrinis (brick)", PARKING_TYPE: "Stovėjimo vieta (parking)",
    "": "Unknown",
}
PARKING_HINTS = ("stovejimo viet", "parkavimo viet", "parking spot", "parking space", "aikstel")

AMENITIES = {
    "electricity": ("elektr", "trifaz", "apsvietim", "electric", "380 v", "220 v"),
    "heating": ("sildym", "sildom", "heating", "heated"),
    "security": ("apsaug", "saugum", "signalizac", "videostebej", "vaizdo steb", "security", "uzkardas",
                 "uztvar", "aptverta", "rakinam"),
    # "apžiūros duobė" is matched by "duob"; bare "apžiūra/apžiūros" means a *viewing* ("dėl apžiūros skambinkite")
    "pit": ("duob", "inspection pit"),
}

CITIES = {
    "Vilnius": ("vilnius", "vilniuje", "vilniaus"),
    "Kaunas": ("kaunas", "kaune", "kauno"),
    "Klaipėda": ("klaipeda", "klaipedoje", "klaipedos"),
    "Šiauliai": ("siauliai", "siauliuose", "siauliu"),
    "Panevėžys": ("panevezys", "panevezyje", "panevezio"),
    "Alytus": ("alytus", "alytuje", "alytaus"),
    "Marijampolė": ("marijampole", "marijampoleje"),
    "Mažeikiai": ("mazeikiai", "mazeikiuose"),
    "Jonava": ("jonava", "jonavoje"),
    "Utena": ("utena", "utenoje"),
    "Kėdainiai": ("kedainiai", "kedainiuose"),
    "Telšiai": ("telsiai", "telsiuose"),
    "Tauragė": ("taurage", "taurageje"),
    "Ukmergė": ("ukmerge", "ukmergeje"),
    "Visaginas": ("visaginas", "visagine"),
    "Palanga": ("palanga", "palangoje"),
    "Druskininkai": ("druskininkai", "druskininkuose"),
    "Trakai": ("trakai", "trakuose"),
}

BLOCK_MARKERS = (
    "captcha", "just a moment", "cf-challenge", "attention required", "access denied",
    "are you a robot", "ar esate zmogus", "verify you are human", "patvirtinkite, kad esate",
)

_STREET_RE = re.compile(
    r"([A-ZĄČĘĖĮŠŲŪŽ][\wąčęėįšųūžĄČĘĖĮŠŲŪŽ\.\- ]{1,40}?\s(?:g|gatvė|al|pr|pl|skg|kel|tak)\.?)(?=[\s,]|$)"
)
_AREA_RE = re.compile(r"(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:m²|m2|kv\.?\s*m\b|kvm\b)", re.I)
_PRICE_RE = re.compile(
    r"(\d{1,3}(?:[   .]\d{3})+|\d+)(?:[.,](\d{1,2}))?\s*(?:€|eur\b|eurų|euro)", re.I
)


class ScrapeError(RuntimeError):
    pass


class ScrapeBlocked(ScrapeError):
    """Portal returned a CAPTCHA / bot wall - use the paste fallback."""


@dataclass
class Listing:
    source: str = "text"
    url: str = ""
    title: str = ""
    asking_price: float | None = None
    city: str = ""
    district: str = ""
    street: str = ""
    area_m2: float | None = None
    garage_type: str = ""
    amenities: dict[str, bool] = field(default_factory=lambda: {k: False for k in AMENITIES})
    is_rental: bool = False

    @property
    def has_pit(self) -> bool:
        return bool(self.amenities.get("pit"))

    @property
    def has_electricity(self) -> bool:
        return bool(self.amenities.get("electricity"))

    def to_dict(self) -> dict:
        return asdict(self)


# --- text helpers ---------------------------------------------------------

def fold(s: str) -> str:
    """Lowercase and strip Lithuanian diacritics for keyword matching."""
    nfkd = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def parse_number(s: str) -> float | None:
    if s is None:
        return None
    s = s.replace(" ", " ").replace(" ", " ")
    m = re.search(r"\d[\d \.]*(?:,\d+)?|\d+(?:\.\d+)?", s)
    if not m:
        return None
    raw = m.group(0).strip()
    # "15 000", "15.000" (thousands) vs "18.5", "18,5" (decimals)
    if re.fullmatch(r"\d{1,3}(?:[ .]\d{3})+", raw):
        raw = re.sub(r"[ .]", "", raw)
    else:
        raw = raw.replace(" ", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_prices(text: str) -> list[tuple[float, int]]:
    """All € amounts in text as (value, end_index), skipping €/m² unit prices."""
    out = []
    for m in _PRICE_RE.finditer(text or ""):
        tail = text[m.end(): m.end() + 6].replace(" ", "")
        if tail.lower().startswith(("/m²", "/m2", "/kv", "/m ", "/m)")) or tail.lower() == "/m":
            continue
        whole = re.sub(r"[   .]", "", m.group(1))
        val = float(whole) + (float("0." + m.group(2)) if m.group(2) else 0.0)
        out.append((val, m.end()))
    return out


def detect_source(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for p in PORTALS:
        if p in host:
            return p
    return "other"


def looks_blocked(html: str) -> bool:
    f = fold(BeautifulSoup(html, "lxml").get_text(" ", strip=True)[:5000])
    return any(m in f for m in BLOCK_MARKERS) and len(f) < 4000


# --- fetching -------------------------------------------------------------

def system_chromium() -> str | None:
    """A distro-installed Chromium (Streamlit Cloud gets one from packages.txt)."""
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        path = shutil.which(name)
        if path:
            return path
    return None


async def _launch_chromium(p):
    """
    Launch Playwright's bundled Chromium; if it isn't installed (e.g. on Streamlit Community
    Cloud, where `playwright install` never runs), fall back to the system Chromium.
    CHROMIUM_PATH forces a specific executable.
    """
    kwargs = {"headless": True,
              "args": ["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"]}
    forced = os.environ.get("CHROMIUM_PATH")
    if forced:
        return await p.chromium.launch(executable_path=forced, **kwargs)
    try:
        return await p.chromium.launch(**kwargs)
    except Exception as e:  # noqa: BLE001
        fallback = system_chromium()
        if "Executable doesn't exist" not in str(e) or not fallback:
            raise
        return await p.chromium.launch(executable_path=fallback, **kwargs)


async def _playwright_fetch_many(urls: list[str], scroll: bool = False, concurrency: int | None = None,
                                 timeout_ms: int = 40000) -> dict[str, dict]:
    """Fetch several pages in one headless browser, `concurrency` tabs at a time."""
    import asyncio

    # fewer tabs on Linux hosts (Streamlit Cloud free tier has ~1 GB RAM); FETCH_CONCURRENCY overrides
    concurrency = concurrency or int(os.environ.get("FETCH_CONCURRENCY",
                                                    "2" if sys.platform.startswith("linux") else "4"))

    from playwright.async_api import async_playwright

    results: dict[str, dict] = {}
    async with async_playwright() as p:
        browser = await _launch_chromium(p)
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            viewport=VIEWPORT,
            locale="lt-LT",
            timezone_id="Europe/Vilnius",
            extra_http_headers={"Accept-Language": "lt-LT,lt;q=0.9,en-US;q=0.8,en;q=0.7"},
        )
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        sem = asyncio.Semaphore(concurrency)

        async def one(url: str) -> None:
            async with sem:
                page = await ctx.new_page()
                try:
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=6000)
                    except Exception:
                        pass
                    if scroll:
                        for _ in range(4):
                            await page.mouse.wheel(0, 2500)
                            await page.wait_for_timeout(350)
                    results[url] = {"status": resp.status if resp else 0, "html": await page.content()}
                except Exception as e:  # noqa: BLE001
                    results[url] = {"status": 0, "html": "", "error": f"{type(e).__name__}: {e}"}
                finally:
                    await page.close()

        await asyncio.gather(*(one(u) for u in dict.fromkeys(urls)))
        await browser.close()
    return results


def _clean_err(stderr: str, limit: int = 600) -> str:
    """Playwright wraps messages in box-drawing frames; keep only the readable lines."""
    stderr = re.sub(r"\\u25[0-7][0-9a-fA-F]", " ", stderr.replace("\\n", "\n"))  # escaped box chars
    lines = []
    for line in stderr.splitlines():
        line = re.sub(r"[─-╿]+", " ", line).strip()
        if line and line != "|":
            lines.append(line)
    return " | ".join(lines)[:limit]


def fetch_many(urls: list[str], scroll: bool = False, timeout_s: int = 240) -> dict[str, str | ScrapeError]:
    """
    Fetch rendered HTML for several URLs via Playwright in a subprocess.
    Returns {url: html} with a ScrapeBlocked / ScrapeError instance for pages that failed.
    """
    urls = [u for u in dict.fromkeys(urls) if u]
    bad = [u for u in urls if not urlparse(u).scheme.startswith("http")]
    if bad:
        raise ScrapeError(f"URL must start with http:// or https:// ({bad[0]})")
    if not urls:
        return {}
    cmd = [sys.executable, __file__, "--fetch-many"] + (["--scroll"] if scroll else [])
    try:
        proc = subprocess.run(cmd, input=json.dumps(urls).encode("utf-8"), capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        raise ScrapeError(f"Timed out fetching {len(urls)} page(s)") from e
    if proc.returncode != 0:
        raise ScrapeError(_clean_err(proc.stderr.decode("utf-8", "replace")) or f"fetch failed ({proc.returncode})")
    raw = json.loads(proc.stdout.decode("utf-8", "replace"))
    out: dict[str, str | ScrapeError] = {}
    for u in urls:
        r = raw.get(u, {"status": 0, "html": "", "error": "no result"})
        if r.get("error"):
            out[u] = ScrapeError(_clean_err(r["error"]))
        elif r["status"] >= 400 and r["status"] != 404:
            out[u] = ScrapeBlocked(f"HTTP {r['status']} from {u}")
        elif looks_blocked(r["html"]):
            out[u] = ScrapeBlocked("Portal served a CAPTCHA / bot-check page.")
        else:
            out[u] = r["html"]
    return out


def fetch_html(url: str, timeout_s: int = 90, scroll: bool = False) -> str:
    """Fetch one page. Raises ScrapeBlocked / ScrapeError."""
    res = fetch_many([url], scroll=scroll, timeout_s=timeout_s)[url]
    if isinstance(res, ScrapeError):
        raise res
    return res


# --- parsing --------------------------------------------------------------

def _label_pairs(soup: BeautifulSoup, text: str) -> dict[str, str]:
    """Collect {folded label: value} from dt/dd, th/td and 'Label: value' lines."""
    pairs: dict[str, str] = {}
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            pairs.setdefault(fold(dt.get_text(" ", strip=True)).rstrip(":"), dd.get_text(" ", strip=True))
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) == 2:
            pairs.setdefault(fold(cells[0].get_text(" ", strip=True)).rstrip(":"),
                             cells[1].get_text(" ", strip=True))
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        m = re.match(r"^([^:]{2,30}):\s*(.*)$", line)
        if m:
            val = m.group(2).strip() or (lines[i + 1] if i + 1 < len(lines) else "")
            pairs.setdefault(fold(m.group(1)).strip(), val)
    return pairs


def _pick(pairs: dict[str, str], *keys: str) -> str:
    for k in keys:
        for label, val in pairs.items():
            if label == k or label.startswith(k):
                return val
    return ""


def _find_city(*texts: str) -> str:
    for t in texts:
        words = set(re.findall(r"[a-z]+", fold(t)))
        for city, forms in CITIES.items():
            if words & set(forms):
                return city
    return ""


def classify_garage(text: str) -> str:
    f = fold(text)
    for gtype, keys in GARAGE_TYPES.items():
        if any(k in f for k in keys):
            return gtype
    if any(k in f for k in PARKING_HINTS):
        return PARKING_TYPE
    return ""


# "be X", "be X ir Y", "nėra X, Y ir Z" - stops at affirmatives like ", yra duobė"
_NEG_SCOPE_RE = re.compile(
    r"\b(?:be|nera|neturi|no|without)\s+[a-z]+"
    r"(?:(?:\s+ir|\s+and|,)\s+(?!yra\b|turi\b|su\b|is\b|has\b|with\b)[a-z]+){0,3}"
)
# postposed negation: "duobės nėra", "elektros neturi"
_NEG_AFTER_RE = re.compile(r"\b[a-z]+\s+(?:nera|neturi|nebus)\b")


def detect_amenities(text: str) -> dict[str, bool]:
    """Keyword match, ignoring negated phrases like "be rūsio ir duobės" / "duobės nėra"."""
    f = _NEG_AFTER_RE.sub(" ", _NEG_SCOPE_RE.sub(" ", fold(text)))
    return {k: any(w in f for w in words) for k, words in AMENITIES.items()}


def detect_pit(text: str) -> bool:
    """True for 'duobė', 'su duobe', 'apžiūros duobė'; False for 'be duobės', 'duobės nėra'."""
    return detect_amenities(text)["pit"]


_DESC_START = ("aprasymas", "skelbimo aprasymas", "papildoma informacija", "description")
_DESC_END = ("panasus skelbim", "panasus objekt", "komentarai", "kiti pardavejo", "jus taip pat",
             "kiti skelbimai", "informavimas apie netinkam", "rekomenduojam")


def feature_text(title: str, pairs: dict[str, str], text: str, limit: int = 4000) -> str:
    """
    Text used for pit / type / electricity detection: title + attribute fields +
    the description block only (not nav, ads, or 'similar listings' widgets,
    which on portal pages often mention other garages' features).
    """
    f = fold(text)
    starts = [f.find(k) for k in _DESC_START if f.find(k) >= 0]
    start = min(starts) if starts else 0
    ends = [f.find(k, start + 1) for k in _DESC_END if f.find(k, start + 1) > start]
    end = min(ends) if ends else len(text)
    body = text[start:end][:limit] if starts else text[:end][:limit * 2]
    attrs = " ".join(_pick(pairs, k) for k in ("tipas", "garazo tipas", "ypatybes", "papildomos patalpos",
                                               "patogumai", "apsauga", "features", "irengimas"))
    return " ".join([title, attrs, body])


def _json_ld(soup: BeautifulSoup) -> dict:
    out: dict = {}
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (TypeError, ValueError):
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            offers = item.get("offers")
            if isinstance(offers, dict) and offers.get("price") and "price" not in out:
                out["price"] = parse_number(str(offers["price"]))
            if item.get("name") and "name" not in out:
                out["name"] = str(item["name"])
    return out


def parse_listing(content: str, url: str = "", rental: bool | None = None) -> Listing:
    """Parse a listing page (HTML) or pasted plain text into a Listing."""
    is_html = bool(re.search(r"<(html|body|div|span|p|dl|table)\b", content or "", re.I))
    soup = BeautifulSoup(content if is_html else "", "lxml")
    ld = _json_ld(soup) if is_html else {}
    if is_html:
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        text = soup.get_text("\n", strip=True)
    else:
        text = content or ""
    pairs = _label_pairs(soup, text)
    f_all = fold(text)

    title = ""
    if is_html:
        h1 = soup.find("h1")
        og = soup.find("meta", property="og:title")
        title = (h1.get_text(" ", strip=True) if h1 else "") or (og.get("content", "") if og else "")
    title = title or ld.get("name", "") or next((l.strip() for l in text.splitlines() if l.strip()), "")[:160]

    if rental is None:
        rental = bool(re.search(r"nuom|/men|per men|rent", fold(title + " " + url)))

    lst = Listing(source=detect_source(url) if url else "text", url=url, title=title, is_rental=rental)

    # Price
    price = ld.get("price")
    if not price:
        price_label = _pick(pairs, "kaina", "price", "nuomos kaina")
        if price_label:
            pp = parse_prices(price_label) or [(parse_number(price_label) or 0, 0)]
            price = pp[0][0] or None
    if not price:
        m = re.search(r"kaina", f_all)
        window = text[m.start(): m.start() + 120] if m else ""
        pp = parse_prices(window) or parse_prices(text)
        lo, hi = (5, 2000) if rental else (500, 2_000_000)
        cand = [v for v, _ in pp if lo <= v <= hi]
        price = cand[0] if cand else None
    lst.asking_price = price

    # Area
    area = parse_number(_pick(pairs, "plotas", "area", "bendras plotas"))
    if not area:
        m = _AREA_RE.search(text)
        area = parse_number(m.group(1)) if m else None
    lst.area_m2 = area if area and 1 <= area <= 2000 else None

    # Location: labels first, then "City, District, Street" title pattern
    title_parts = [p.strip() for p in re.split(r",", title) if p.strip()]
    lst.city = _find_city(_pick(pairs, "gyvenviete", "miestas", "savivaldybe", "city")) \
        or _find_city(title) or _find_city(url.replace("-", " ")) or _find_city(text[:3000])
    lst.district = _pick(pairs, "mikrorajonas", "rajonas", "district", "seniunija")
    lst.street = _pick(pairs, "gatve", "adresas", "street", "address")
    if title_parts and lst.city and fold(title_parts[0]).startswith(fold(lst.city)[:5]):
        if not lst.district and len(title_parts) >= 3:
            lst.district = title_parts[1]
        if not lst.street:
            for p in title_parts[1:]:
                if _STREET_RE.search(p):
                    lst.street = p
                    break
    if not lst.district and lst.city:
        # "Garažas Vilniuje Baltupiuose, ..." -> district word follows the locative city form
        # (fold() keeps string length for Lithuanian letters, so indices map back to `title`)
        forms = "|".join(CITIES[lst.city])
        m = re.search(rf"\b(?:{forms})\s+([a-z][a-z\-]{{3,}})", fold(title))
        if m and not re.match(r"(g|gatve|raj|sav|m)\b", m.group(1)):
            lst.district = title[m.start(1): m.end(1)]
    if not lst.street:
        m = _STREET_RE.search(title + "\n" + text[:4000])
        lst.street = m.group(1).strip() if m else ""
    if lst.district and _STREET_RE.fullmatch(lst.district):
        lst.district, lst.street = "", lst.street or lst.district
    if not lst.district and lst.city:
        lst.district = find_district(title + " " + text[:3000], lst.city)
    lst.district = normalize_district(lst.district, lst.city)

    # Type & amenities: labelled fields, title and description block only
    feats = feature_text(title, pairs, text)
    type_text = " ".join([_pick(pairs, "tipas", "garazo tipas", "type", "pastato tipas"), title])
    lst.garage_type = classify_garage(type_text) or classify_garage(feats)
    lst.amenities = detect_amenities(feats)
    return lst


def fetch_listing(url: str) -> Listing:
    return parse_listing(fetch_html(url), url=url)


# --- child-process entry point -------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] in ("--fetch-many", "--fetch"):
        import asyncio

        targets = sys.argv[2:3] if sys.argv[1] == "--fetch" else json.loads(sys.stdin.buffer.read().decode("utf-8"))
        try:
            res = asyncio.run(_playwright_fetch_many(targets, scroll="--scroll" in sys.argv))
        except Exception as e:  # noqa: BLE001
            print(f"{type(e).__name__}: {e} [python={sys.executable}]", file=sys.stderr)
            sys.exit(2)
        if sys.argv[1] == "--fetch":  # debugging convenience: raw HTML of one page
            sys.stdout.buffer.write(next(iter(res.values())).get("html", "").encode("utf-8"))
        else:
            sys.stdout.buffer.write(json.dumps(res).encode("utf-8"))
        sys.exit(0)
    print("usage: python scraper.py --fetch URL | --fetch-many [--scroll] < urls.json", file=sys.stderr)
    sys.exit(1)
