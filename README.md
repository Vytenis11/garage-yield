# Garage Yield Underwriter

Streamlit dashboard that takes a garage / parking-space sale listing (Aruodas.lt, Skelbiu.lt,
Domoplius.lt, or pasted text/HTML), pulls rental comps from the same portal, and reverse-values
the maximum offer price that delivers a 9–10 % yield.

## Run

```powershell
.\.venv\Scripts\streamlit run app.py
```

Tests: `.\.venv\Scripts\python -m pytest`

Fresh setup:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m playwright install chromium
```

## Deploy free on Streamlit Community Cloud

1. On github.com create a new repository (e.g. `garage-underwriter`).
2. Click **uploading an existing file** and drag in: `app.py`, `comps.py`, `districts.py`, `scraper.py`,
   `underwriting.py`, `requirements.txt`, `packages.txt`, `README.md`, `.gitignore` and the `tests` folder.
   Do **not** upload `.venv`, `.claude` or `__pycache__`. Commit.
3. Go to https://share.streamlit.io, sign in with GitHub, **Create app → Deploy a public app from GitHub**.
   Repository = your repo, branch `main`, main file `app.py`.
   Under **Advanced settings** pick Python **3.12**. Deploy (first build ≈ 5 min: it apt-installs Chromium).
4. Share the `https://<name>.streamlit.app` link.

`packages.txt` installs the system Chromium; `scraper.py` falls back to it automatically because
`playwright install` never runs on Streamlit Cloud. Optional env vars: `CHROMIUM_PATH` (force a browser),
`FETCH_CONCURRENCY` (tabs at once; default 2 on Linux, 4 elsewhere).

Cloud caveats: portals block data-centre IPs more readily than home connections, so some scrapes may
fail there (the paste-ad / paste-comps fallbacks still work). Free apps sleep after inactivity and wake on the
next visit. To restrict access, set the app to private in its Streamlit settings and invite testers by email.

## Files

| File | Role |
|---|---|
| `scraper.py` | Batched Playwright fetch (child process, 4 tabs at a time) + portal-agnostic parser: price, location, area, garage type, pit / electricity (from title + attributes + description only, negation-aware) |
| `districts.py` | Lithuanian district normalization (*Antakalnyje → Antakalnis*, *Žirmūnų → Žirmūnai*) and adjacency maps for Vilnius, Kaunas, Klaipėda — edit `_EDGES` to tune neighbours |
| `comps.py` | Tiered comps: Tier 1 exact *mikrorajonas* search on every portal (≤15), Tier 2 adjacent districts if < 4, city-wide last resort; reads each comp's full ad; per-pit-segment IQR outliers; pit-premium adjustment |
| `underwriting.py` | Pure reverse-valuation math and BUY / NEGOTIATE / PASS verdict |
| `app.py` | Streamlit UI; comps cached on `(listing_url, city, district, has_pit, garage_type)` |

## Comps logic

- Search URLs are built from the subject's **city + micro-district**, e.g.
  `skelbiu.lt/paieska/garazas-nuoma-vilnius-zirmunai/`, `domoplius.lt/skelbimai/garazai/nuoma/vilnius/zirmunai`.
  Aruodas is tried too but blocks headless browsers (HTTP 403).
- Each comp is tagged **Exact District** / **Adjacent District** / City-wide / Manual.
- **Pit segmentation:** comps with the subject's pit status are used raw. With fewer than 3 of them,
  mismatched comps are added and normalized by the *Inspection Pit Rent Premium* (default +30 %:
  ×1.3 for a pit subject, ÷1.3 for a no-pit subject); otherwise they're listed unticked.
  *Only match exact pit status* shows the raw matching comps only.
- Editing the subject's city / district / type / pit box re-runs the search for the new cache key.

## Model

- Effective income = monthly rent × 12 × (1 − vacancy)
- Max all-in budget = effective income ÷ target yield
- Max offer = all-in budget − (notary/Registrų centras fees + capex), floored at 0
- **BUY** – asking already meets the low-end yield; **NEGOTIATE** – required discount ≤ threshold
  (default 15 %); **PASS** – deeper discount needed.
- OpEx (utilities, bendrija) and taxes are excluded by assumption.

## If a portal blocks you

Open the ad in your normal browser, Ctrl+A / Ctrl+C, and paste into **Paste Ad Text / HTML**.
For comps, paste rental ads into the manual comps box (district and pit status are parsed from the text);
untick any comp in the table to drop it from the estimate.
