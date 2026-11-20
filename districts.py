"""Lithuanian micro-district (mikrorajonas) normalization and adjacency maps.

Listings write district names in different grammatical cases:
  nominative  Žirmūnai, Antakalnis, Šeškinė      (labels: "Mikrorajonas: Žirmūnai")
  locative    Žirmūnuose, Antakalnyje, Šeškinėje ("garažas Žirmūnuose")
  genitive    Žirmūnų, Antakalnio, Šeškinės      ("Žirmūnų g.", "Antakalnio mikrorajone")
`stem()` strips these endings so every form of a name maps to the same key;
`normalize_district()` returns the canonical nominative name.

Adjacency maps are a practical approximation of which micro-districts border
each other (used for Tier-2 comps). Edit `_EDGES` to tune them.
"""

from __future__ import annotations

import re
import unicodedata

# Longest first. Covers -ai/-uose/-ų, -is/-yje/-io, -ė/-ėje/-ės/-ėse, -as/-e/-o, -a/-oje/-os.
_ENDINGS = (
    "iuose", "uose", "iuje", "yje", "eje", "oje", "ese", "ose", "uje",
    "iai", "ai", "iu", "ys", "is", "io", "ies", "es", "as", "os", "us",
    "e", "a", "u", "o", "y", "i",
)

DISTRICTS: dict[str, list[str]] = {
    "Vilnius": [
        "Antakalnis", "Bajorai", "Baltupiai", "Fabijoniškės", "Grigiškės", "Jeruzalė", "Justiniškės",
        "Karoliniškės", "Lazdynai", "Lazdynėliai", "Naujamiestis", "Naujininkai", "Naujoji Vilnia",
        "Pašilaičiai", "Paupys", "Pavilnys", "Pilaitė", "Rasos", "Santariškės", "Senamiestis", "Šeškinė",
        "Šiaurės miestelis", "Šnipiškės", "Tarandė", "Užupis", "Valakampiai", "Verkiai", "Vilkpėdė",
        "Viršuliškės", "Visoriai", "Žirmūnai", "Žvėrynas",
    ],
    "Kaunas": [
        "Aleksotas", "Aukštieji Šančiai", "Centras", "Dainava", "Eiguliai", "Gričiupis", "Kalniečiai",
        "Kleboniškis", "Naujamiestis", "Palemonas", "Panemunė", "Petrašiūnai", "Romainiai", "Senamiestis",
        "Šančiai", "Šilainiai", "Vilijampolė", "Žaliakalnis",
    ],
    "Klaipėda": [
        "Bandužiai", "Baltija", "Centras", "Debrecenas", "Dragūnai", "Giruliai", "Kauno", "Laukininkai",
        "Melnragė", "Miško", "Paupiai", "Poilsis", "Rimkai", "Senamiestis", "Smeltė", "Vingis", "Vėtrungė",
        "Žardininkai",
    ],
}

_EDGES: dict[str, list[tuple[str, str]]] = {
    "Vilnius": [
        ("Žirmūnai", "Šnipiškės"), ("Žirmūnai", "Antakalnis"), ("Žirmūnai", "Baltupiai"),
        ("Žirmūnai", "Šiaurės miestelis"), ("Žirmūnai", "Verkiai"), ("Šnipiškės", "Antakalnis"),
        ("Antakalnis", "Baltupiai"), ("Šiaurės miestelis", "Baltupiai"), ("Baltupiai", "Jeruzalė"),
        ("Baltupiai", "Santariškės"), ("Baltupiai", "Verkiai"), ("Baltupiai", "Fabijoniškės"),
        ("Šnipiškės", "Senamiestis"), ("Šnipiškės", "Naujamiestis"), ("Šnipiškės", "Žvėrynas"),
        ("Šnipiškės", "Šeškinė"), ("Antakalnis", "Senamiestis"), ("Antakalnis", "Užupis"),
        ("Antakalnis", "Valakampiai"), ("Senamiestis", "Užupis"), ("Senamiestis", "Naujamiestis"),
        ("Senamiestis", "Naujininkai"), ("Užupis", "Paupys"), ("Užupis", "Rasos"), ("Paupys", "Senamiestis"),
        ("Naujamiestis", "Žvėrynas"), ("Naujamiestis", "Vilkpėdė"), ("Naujamiestis", "Naujininkai"),
        ("Žvėrynas", "Karoliniškės"), ("Žvėrynas", "Šeškinė"), ("Šeškinė", "Fabijoniškės"),
        ("Šeškinė", "Pašilaičiai"), ("Šeškinė", "Justiniškės"), ("Šeškinė", "Viršuliškės"),
        ("Fabijoniškės", "Pašilaičiai"), ("Fabijoniškės", "Justiniškės"), ("Fabijoniškės", "Bajorai"),
        ("Fabijoniškės", "Jeruzalė"), ("Pašilaičiai", "Justiniškės"), ("Pašilaičiai", "Pilaitė"),
        ("Justiniškės", "Viršuliškės"), ("Justiniškės", "Pilaitė"), ("Karoliniškės", "Viršuliškės"),
        ("Viršuliškės", "Lazdynai"), ("Karoliniškės", "Lazdynai"), ("Lazdynai", "Lazdynėliai"),
        ("Lazdynai", "Pilaitė"), ("Lazdynai", "Vilkpėdė"), ("Karoliniškės", "Vilkpėdė"),
        ("Vilkpėdė", "Naujininkai"), ("Naujininkai", "Rasos"), ("Rasos", "Naujoji Vilnia"),
        ("Naujoji Vilnia", "Pavilnys"), ("Pavilnys", "Antakalnis"), ("Verkiai", "Santariškės"),
        ("Verkiai", "Visoriai"), ("Santariškės", "Visoriai"), ("Jeruzalė", "Santariškės"),
        ("Jeruzalė", "Visoriai"), ("Bajorai", "Jeruzalė"), ("Bajorai", "Pašilaičiai"),
        ("Pilaitė", "Grigiškės"), ("Verkiai", "Valakampiai"), ("Visoriai", "Tarandė"),
    ],
    "Kaunas": [
        ("Centras", "Naujamiestis"), ("Centras", "Senamiestis"), ("Centras", "Žaliakalnis"),
        ("Senamiestis", "Vilijampolė"), ("Senamiestis", "Aleksotas"), ("Naujamiestis", "Žaliakalnis"),
        ("Naujamiestis", "Šančiai"), ("Naujamiestis", "Aleksotas"), ("Žaliakalnis", "Kalniečiai"),
        ("Žaliakalnis", "Dainava"), ("Žaliakalnis", "Eiguliai"), ("Žaliakalnis", "Šančiai"),
        ("Kalniečiai", "Eiguliai"), ("Kalniečiai", "Dainava"), ("Kalniečiai", "Šilainiai"),
        ("Dainava", "Gričiupis"), ("Dainava", "Šančiai"), ("Gričiupis", "Petrašiūnai"),
        ("Gričiupis", "Šančiai"), ("Šančiai", "Aukštieji Šančiai"), ("Aukštieji Šančiai", "Panemunė"),
        ("Šančiai", "Panemunė"), ("Šančiai", "Petrašiūnai"), ("Petrašiūnai", "Palemonas"),
        ("Eiguliai", "Šilainiai"), ("Eiguliai", "Vilijampolė"), ("Šilainiai", "Vilijampolė"),
        ("Šilainiai", "Romainiai"), ("Vilijampolė", "Kleboniškis"),
    ],
    "Klaipėda": [
        ("Centras", "Senamiestis"), ("Centras", "Vėtrungė"), ("Centras", "Paupiai"), ("Vėtrungė", "Paupiai"),
        ("Vėtrungė", "Melnragė"), ("Melnragė", "Giruliai"), ("Senamiestis", "Smeltė"), ("Smeltė", "Baltija"),
        ("Baltija", "Debrecenas"), ("Baltija", "Kauno"), ("Kauno", "Debrecenas"), ("Debrecenas", "Dragūnai"),
        ("Debrecenas", "Bandužiai"), ("Bandužiai", "Laukininkai"), ("Dragūnai", "Bandužiai"),
        ("Laukininkai", "Žardininkai"), ("Žardininkai", "Vingis"), ("Vingis", "Poilsis"),
        ("Poilsis", "Miško"), ("Miško", "Kauno"), ("Rimkai", "Laukininkai"), ("Paupiai", "Kauno"),
    ],
}


def fold(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def stem(word: str) -> str:
    """Case-insensitive, diacritic-free stem of one word with Lithuanian case endings removed."""
    w = re.sub(r"[^a-z]", "", fold(word))
    for end in _ENDINGS:
        if w.endswith(end) and len(w) - len(end) >= 4:
            return w[: -len(end)]
    return w


def name_key(name: str) -> str:
    """Stem every word of a (possibly multi-word) district name."""
    return " ".join(stem(w) for w in re.findall(r"[^\W\d_]+", name or ""))


def _index(city: str) -> dict[str, str]:
    return {name_key(d): d for d in DISTRICTS.get(city, [])}


def normalize_district(name: str, city: str = "") -> str:
    """'Antakalnyje' -> 'Antakalnis', 'Žirmūnuose' -> 'Žirmūnai', 'Šeškinės' -> 'Šeškinė'."""
    if not name or not name.strip():
        return ""
    key = name_key(name)
    cities = [city] if city in DISTRICTS else list(DISTRICTS)
    for c in cities:
        hit = _index(c).get(key)
        if hit:
            return hit
    # not in the built-in list: best-effort nominative from common locative endings
    word = name.strip()
    for loc, nom in (("iuose", "iai"), ("uose", "ai"), ("yje", "is"), ("ėje", "ė"), ("ėse", "ės"), ("oje", "a")):
        if word.endswith(loc):
            return word[: -len(loc)] + nom
    return word


def find_district(text: str, city: str) -> str:
    """First canonical district of `city` mentioned in free text (any grammatical case)."""
    index = _index(city)
    if not index or not text:
        return ""
    matches = list(re.finditer(r"[^\W\d_]+", text))
    words = [stem(m.group(0)) for m in matches]
    # a word directly followed by a street suffix is a street name ("Verkių g."), not a district
    street = [bool(_STREET_SUFFIX.match(text, m.end())) for m in matches]
    multi = {k: v for k, v in index.items() if " " in k}
    for i, w in enumerate(words):
        for k, v in multi.items():
            parts = k.split()
            if words[i: i + len(parts)] == parts and not street[i + len(parts) - 1]:
                return v
        if w in index and not street[i]:
            return index[w]
    return ""


_STREET_SUFFIX = re.compile(r"\s*(?:g\.|g\b|gatv|al\.|pr\.|pl\.|skg\.|kel\.|tak\.)", re.I)


def adjacent_districts(district: str, city: str) -> list[str]:
    d = normalize_district(district, city)
    out: list[str] = []
    for a, b in _EDGES.get(city, []):
        if a == d and b not in out:
            out.append(b)
        elif b == d and a not in out:
            out.append(a)
    return out


def district_slug(district: str) -> str:
    """'Šiaurės miestelis' -> 'siaures-miestelis' (Skelbiu / Domoplius URL form)."""
    return re.sub(r"[^a-z0-9]+", "-", fold(district)).strip("-")


def district_locative(district: str) -> str:
    """Nominative -> locative slug used by Aruodas URLs ('Žirmūnai' -> 'zirmunuose')."""
    f = fold(district).split()[0] if district else ""
    for nom, loc in (("iai", "iuose"), ("ai", "uose"), ("is", "yje"), ("e", "eje"), ("es", "ese"),
                     ("as", "e"), ("a", "oje"), ("ys", "yje")):
        if f.endswith(nom):
            return f[: -len(nom)] + loc
    return f
