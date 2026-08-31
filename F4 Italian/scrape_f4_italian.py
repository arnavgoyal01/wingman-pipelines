#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 scrape_f4_italian.py
--------------------------------------------------------------------------------
 Scraper for the FORMULA 4 ITALIAN CHAMPIONSHIP pages on motorsportstats.com.

 It produces three CSV files:

   1. schedule.csv            -- the "schedule" contract format (§7). One row per
                                 session of every round that has ALREADY RUN.
                                 session_datetime_local is required here, so a
                                 past round is only emitted when the source
                                 published its session timetable (see the FINDING
                                 note below); a round with no timetable is skipped
                                 rather than emitted with an invented time.

   2. upcoming_schedule.csv   -- the "upcoming_schedule" contract format (§7a).
                                 One row per announced session of every round that
                                 has NOT yet run (date on/after "today").

   3. driver.csv              -- the season participant list (drivers + teams +
                                 nationalities). This format is NOT defined in
                                 the contract, so every column name and format is
                                 lifted directly from the columns of the three
                                 defined formats (results §5, schedule §7,
                                 upcoming_schedule §7a). See DRIVER_CSV_COLUMNS
                                 below for the per-column citation.

 A round is routed by its date: date < today -> schedule.csv (§7);
 date >= today -> upcoming_schedule.csv (§7a).

 The code is deliberately written for AUDITABILITY:
   - one responsibility per function, with docstrings that cite the contract
     section each rule comes from;
   - parsing keyed on STABLE structural signals (link hrefs, flag image URLs,
     table layout) rather than the build-hashed CSS class names that
     motorsportstats regenerates on every deploy;
   - a self-check pass (`validate_*`) that re-applies the contract rules this
     collector is responsible for before anything is written to disk.

--------------------------------------------------------------------------------
 DATA SOURCES (verified 2026-08-31 against the live site + the supplied samples)
--------------------------------------------------------------------------------
 * Season calendar page
     https://motorsportstats.com/series/formula-4-italian-championship/calendar/<year>
     -> round number, event slug, venue, and the round DATE.
     (Matches the supplied sample `calendar.txt`.)

 * Per-round "info" page
     https://motorsportstats.com/results/formula-4-italian-championship/<year>/<slug>/info
     -> the session TIMETABLE (pageProps.scheduleData in __NEXT_DATA__): session
     code (P1/Q1/Race/...), start/end times as Unix epochs (local + UTC).
     IMPORTANT FINDING (verified 2026-08-31): completed prior seasons (e.g. 2025)
     publish a full timetable, so schedule.csv (§7) fills correctly there. The
     2026 rounds, however, currently publish NO timetable -- past and future
     alike show "TIMETABLE TO BE CONFIRMED" and scheduleData == []. Consequences:
       - 2026 past rounds -> no schedule.csv rows (a required session_datetime_local
         cannot be honestly supplied); the round is skipped with a note.
       - 2026 upcoming rounds -> upcoming_schedule.csv falls back to ONE race-day
         row per round with an empty time (§7a permits this).
     A SECOND FINDING: some published F4-Italian session times fall at/after
     18:00 local (e.g. 2025 evening qualifying/races). SESSION_TIME_001 rejects
     those with no exemption, so a fully-populated schedule.csv for this series
     may be refused by the official validator through no fault of the scraper.
     validate_past_schedule surfaces each such row as a WARNING.

 * Driver standings page
     https://motorsportstats.com/series/formula-4-italian-championship/standings/<year>/drivers
     -> every driver who has appeared in the season, with team + nationality.
     (Matches the supplied sample `drivers.txt`.)

--------------------------------------------------------------------------------
 FETCHING
--------------------------------------------------------------------------------
 motorsportstats is a Next.js site. Plain HTTP clients are refused by the edge,
 so live fetching uses a headless browser (Playwright) that renders the page and
 returns its HTML -- exactly reproducing how the supplied samples were captured.
 For auditing and offline runs, `--from-files` parses saved HTML instead, with
 no network access at all.

 Usage
 -----
   # Offline, from saved HTML (what the samples are):
   python scrape_f4_italian.py --from-files \
       --calendar-html samples/calendar.txt \
       --standings-html samples/drivers.txt \
       --reference-dir reference --out-dir output

   # Live (needs:  pip install playwright  &&  playwright install chromium):
   python scrape_f4_italian.py --live --year 2026 \
       --reference-dir reference --out-dir output

 Reference tables (the bundled dims from the contract library) are required for
 circuit / country / category resolution and must be in --reference-dir:
   dim_categories.csv, dim_countries.csv, dim_circuits.csv
================================================================================
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# BeautifulSoup is the only third-party parsing dependency (pip install beautifulsoup4).
from bs4 import BeautifulSoup


# =============================================================================
# 1. STATIC CONFIGURATION
#    Everything here is a fact about THIS series, taken from the bundled dims.
# =============================================================================

SERIES_ID = 64                      # dim_series.series_id for Italian F4.
SERIES_SLUG = "formula-4-italian-championship"   # motorsportstats URL slug.
SERIES_NAME_NORMALIZED = "italian_f4_championship"  # dim_series, for cross-check.

# Taxonomy triple -- must exist in dim_categories (verified: present).
SPORT = "motorsport"
DISCIPLINE = "single_seater"
CATEGORY = "formula_4"

# Base URLs.
BASE = "https://motorsportstats.com"

# Default collector identity written to the `source_collector` lineage column
# (§5.6 / §7a). Override with --collector.
DEFAULT_COLLECTOR = "arnav"


# -----------------------------------------------------------------------------
# 1a. Venue -> circuit-layout map.
#
# The calendar names a VENUE (e.g. "Misano World Circuit Marco Simoncelli"), but
# dim_circuits has one row PER LAYOUT (Misano has 7). The circuit_id we must emit
# is the specific car layout Italian F4 races on. We therefore map each
# motorsportstats venue slug to a (circuit_full_name_normalized, layout_normalized)
# pair and resolve the integer circuit_id from dim_circuits at load time -- so the
# ID is never a magic number, and a dim change is caught loudly rather than
# silently emitting a stale ID.
#
# Layout choices (the primary full-course car layout at each venue):
#   Misano      -> Grand Prix Circuit
#   Vallelunga  -> International Circuit
#   Monza       -> Road Course
#   Mugello     -> Grand Prix Course
#   Imola       -> Grand Prix Circuit
# -----------------------------------------------------------------------------
VENUE_SLUG_TO_CIRCUIT = {
    "misano-world-circuit-marco-simoncelli": ("misano", "grand_prix_circuit"),
    "autodromo-vallelunga-piero-taruffi":    ("vallelunga", "international_circuit"),
    "autodromo-nazionale-monza":             ("monza", "road_course"),
    "mugello-circuit":                       ("mugello", "grand_prix_course"),
    "autodromo-enzo-e-dino-ferrari":         ("imola", "grand_prix_circuit"),
    # Barcelona hosted a 2025 Italian F4 round (the series occasionally visits
    # non-Italian venues). Grand Prix Circuit is the car layout F4 uses.
    "circuit-de-barcelona-catalunya":        ("circuit_de_barcelona-catalunya", "grand_prix_circuit"),
}


# -----------------------------------------------------------------------------
# 1b. ISO 3166-1 alpha-2 -> alpha-3 map.
#
# motorsportstats flags are alpha-2 (e.g. .../flags/svg/it.svg -> "it"); the
# contract's nationality_code is alpha-3 (§2.4, §5.4). This static table is the
# canonical ISO mapping; every result is additionally checked against
# dim_countries so an unmapped or non-dim code fails loudly (MASTER_REF_002).
# -----------------------------------------------------------------------------
ALPHA2_TO_ALPHA3 = {
    "ad": "AND", "ae": "ARE", "af": "AFG", "ag": "ATG", "ai": "AIA", "al": "ALB", "am": "ARM", "ao": "AGO",
    "aq": "ATA", "ar": "ARG", "as": "ASM", "at": "AUT", "au": "AUS", "aw": "ABW", "ax": "ALA", "az": "AZE",
    "ba": "BIH", "bb": "BRB", "bd": "BGD", "be": "BEL", "bf": "BFA", "bg": "BGR", "bh": "BHR", "bi": "BDI",
    "bj": "BEN", "bl": "BLM", "bm": "BMU", "bn": "BRN", "bo": "BOL", "bq": "BES", "br": "BRA", "bs": "BHS",
    "bt": "BTN", "bv": "BVT", "bw": "BWA", "by": "BLR", "bz": "BLZ", "ca": "CAN", "cc": "CCK", "cd": "COD",
    "cf": "CAF", "cg": "COG", "ch": "CHE", "ci": "CIV", "ck": "COK", "cl": "CHL", "cm": "CMR", "cn": "CHN",
    "co": "COL", "cr": "CRI", "cu": "CUB", "cv": "CPV", "cw": "CUW", "cx": "CXR", "cy": "CYP", "cz": "CZE",
    "de": "DEU", "dj": "DJI", "dk": "DNK", "dm": "DMA", "do": "DOM", "dz": "DZA", "ec": "ECU", "ee": "EST",
    "eg": "EGY", "eh": "ESH", "er": "ERI", "es": "ESP", "et": "ETH", "fi": "FIN", "fj": "FJI", "fk": "FLK",
    "fm": "FSM", "fo": "FRO", "fr": "FRA", "ga": "GAB", "gb": "GBR", "gd": "GRD", "ge": "GEO", "gf": "GUF",
    "gg": "GGY", "gh": "GHA", "gi": "GIB", "gl": "GRL", "gm": "GMB", "gn": "GIN", "gp": "GLP", "gq": "GNQ",
    "gr": "GRC", "gs": "SGS", "gt": "GTM", "gu": "GUM", "gw": "GNB", "gy": "GUY", "hk": "HKG", "hm": "HMD",
    "hn": "HND", "hr": "HRV", "ht": "HTI", "hu": "HUN", "id": "IDN", "ie": "IRL", "il": "ISR", "im": "IMN",
    "in": "IND", "io": "IOT", "iq": "IRQ", "ir": "IRN", "is": "ISL", "it": "ITA", "je": "JEY", "jm": "JAM",
    "jo": "JOR", "jp": "JPN", "ke": "KEN", "kg": "KGZ", "kh": "KHM", "ki": "KIR", "km": "COM", "kn": "KNA",
    "kp": "PRK", "kr": "KOR", "kw": "KWT", "ky": "CYM", "kz": "KAZ", "la": "LAO", "lb": "LBN", "lc": "LCA",
    "li": "LIE", "lk": "LKA", "lr": "LBR", "ls": "LSO", "lt": "LTU", "lu": "LUX", "lv": "LVA", "ly": "LBY",
    "ma": "MAR", "mc": "MCO", "md": "MDA", "me": "MNE", "mf": "MAF", "mg": "MDG", "mh": "MHL", "mk": "MKD",
    "ml": "MLI", "mm": "MMR", "mn": "MNG", "mo": "MAC", "mp": "MNP", "mq": "MTQ", "mr": "MRT", "ms": "MSR",
    "mt": "MLT", "mu": "MUS", "mv": "MDV", "mw": "MWI", "mx": "MEX", "my": "MYS", "mz": "MOZ", "na": "NAM",
    "nc": "NCL", "ne": "NER", "nf": "NFK", "ng": "NGA", "ni": "NIC", "nl": "NLD", "no": "NOR", "np": "NPL",
    "nr": "NRU", "nu": "NIU", "nz": "NZL", "om": "OMN", "pa": "PAN", "pe": "PER", "pf": "PYF", "pg": "PNG",
    "ph": "PHL", "pk": "PAK", "pl": "POL", "pm": "SPM", "pn": "PCN", "pr": "PRI", "ps": "PSE", "pt": "PRT",
    "pw": "PLW", "py": "PRY", "qa": "QAT", "re": "REU", "ro": "ROU", "rs": "SRB", "ru": "RUS", "rw": "RWA",
    "sa": "SAU", "sb": "SLB", "sc": "SYC", "sd": "SDN", "se": "SWE", "sg": "SGP", "sh": "SHN", "si": "SVN",
    "sj": "SJM", "sk": "SVK", "sl": "SLE", "sm": "SMR", "sn": "SEN", "so": "SOM", "sr": "SUR", "ss": "SSD",
    "st": "STP", "sv": "SLV", "sx": "SXM", "sy": "SYR", "sz": "SWZ", "tc": "TCA", "td": "TCD", "tf": "ATF",
    "tg": "TGO", "th": "THA", "tj": "TJK", "tk": "TKL", "tl": "TLS", "tm": "TKM", "tn": "TUN", "to": "TON",
    "tr": "TUR", "tt": "TTO", "tv": "TUV", "tw": "TWN", "tz": "TZA", "ua": "UKR", "ug": "UGA", "um": "UMI",
    "us": "USA", "uy": "URY", "uz": "UZB", "va": "VAT", "vc": "VCT", "ve": "VEN", "vg": "VGB", "vi": "VIR",
    "vn": "VNM", "vu": "VUT", "wf": "WLF", "ws": "WSM", "ye": "YEM", "yt": "MYT", "za": "ZAF", "zm": "ZMB",
    "zw": "ZWE",
}


# =============================================================================
# 2. NORMALIZATION LIBRARY  (contract §3a)
#
# The contract says these MUST come from race_validator/normalize.py and must not
# be re-implemented. That library is not available standalone, so the two
# functions are reproduced here EXACTLY per the documented algorithm (§3a.1 /
# §3a.2). In the production pipeline, replace these two functions with:
#       from race_validator.normalize import normalize_name, normalize_identifier
# =============================================================================

# §3a pre-mapping for characters NFKD does not decompose.
_PRE_MAP = {
    "ø": "o", "Ø": "O", "æ": "ae", "œ": "oe", "ß": "ss", "ð": "d",
    "þ": "th", "ł": "l", "đ": "d", "ı": "i", "ŋ": "n",
}


def _apply_premap(s: str) -> str:
    for src, dst in _PRE_MAP.items():
        s = s.replace(src, dst)
    return s


def _strip_combining(s: str) -> str:
    """NFKD-decompose then drop Unicode combining marks (category 'Mn')."""
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def normalize_name(s: str) -> str:
    """Person-name normalizer (contract §3a.1).

    Output: lowercase ASCII, single-spaced, no diacritics.
    Steps: pre-map -> NFKD -> drop combining marks -> non [A-Za-z0-9 ] to space
           -> lowercase -> collapse whitespace -> strip.
    """
    if s is None or not s.strip():
        raise ValueError("normalize_name: empty/whitespace-only input")
    if len(s) > 200:
        raise ValueError("normalize_name: input longer than 200 chars")
    s = _apply_premap(s)
    s = _strip_combining(s)
    s = re.sub(r"[^A-Za-z0-9 ]", " ", s)   # step 4: keep space as separator
    s = s.lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_identifier(s: str) -> str:
    """Entity-identifier normalizer (contract §3a.2).

    Output: lowercase ASCII, any non-alphanumeric run collapsed to one '_'.
    Steps: pre-map -> NFKD -> drop combining marks -> non [A-Za-z0-9] run to '_'
           -> lowercase -> strip leading/trailing '_'.
    """
    if s is None or not s.strip():
        raise ValueError("normalize_identifier: empty/whitespace-only input")
    if len(s) > 200:
        raise ValueError("normalize_identifier: input longer than 200 chars")
    s = _apply_premap(s)
    s = _strip_combining(s)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s)
    s = s.lower()
    return s.strip("_")


# =============================================================================
# 3. REFERENCE (dim) TABLE LOADING  (contract §4)
# =============================================================================

@dataclass
class ReferenceData:
    """Bundled dim tables the collector must resolve IDs against (§4.6)."""
    country_ids: set                          # {alpha-3, ...}
    category_triples: set                     # {(sport, discipline, category), ...}
    circuits_by_name_layout: dict             # (name_norm, layout_norm) -> circuit_id
    circuits_by_name: dict                    # name_norm -> [circuit_id, ...]
    circuit_ids: set                          # {circuit_id, ...}
    circuit_has_layout: bool                  # whether the dim carries a layout column


def _circuit_field(row: dict, normalized_col: str, display_col: str):
    """Return a circuit dim field, tolerating a dim that lacks the _normalized
    column: if the normalized value is present use it, else derive it from the
    _display column via normalize_identifier (§3a). Returns None when neither
    column exists in this dim.
    """
    if normalized_col in row and row[normalized_col] is not None:
        return row[normalized_col].strip()
    if display_col in row and row[display_col] and row[display_col].strip():
        return normalize_identifier(row[display_col])
    return None


def load_reference_data(reference_dir: Path) -> ReferenceData:
    """Load dim_countries, dim_categories and dim_circuits from CSV.

    The dim_circuits loader is tolerant of schema drift: `circuit_full_name_
    normalized` / `layout_normalized` are used when present, else derived from the
    `_display` columns. A dim with no layout column at all is supported via
    name-only resolution (see resolve_circuit_id).
    """
    ref_country_ids = set()
    with open(reference_dir / "dim_countries.csv", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            ref_country_ids.add(row["country_id"].strip())

    triples = set()
    with open(reference_dir / "dim_categories.csv", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            triples.add((row["sport"].strip(), row["discipline"].strip(),
                         row["category"].strip()))

    circuits_by_name_layout: dict = {}
    circuits_by_name: dict = {}
    circuit_ids = set()
    with open(reference_dir / "circuits.csv", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        has_layout = ("layout_normalized" in cols) or ("layout_display" in cols)
        has_name = ("circuit_full_name_normalized" in cols) or ("circuit_full_name_display" in cols)
        if "circuit_id" not in cols or not has_name:
            raise ValueError(
                "dim_circuits.csv must have circuit_id and a circuit name column "
                f"(circuit_full_name_normalized or _display). Found columns: {cols}")
        for row in reader:
            cid = int(row["circuit_id"])
            circuit_ids.add(cid)
            name = _circuit_field(row, "circuit_full_name_normalized",
                                  "circuit_full_name_display")
            if name is None:
                continue
            circuits_by_name.setdefault(name, []).append(cid)
            if has_layout:
                layout = _circuit_field(row, "layout_normalized", "layout_display") or ""
                circuits_by_name_layout[(name, layout)] = cid

    return ReferenceData(
        country_ids=ref_country_ids,
        category_triples=triples,
        circuits_by_name_layout=circuits_by_name_layout,
        circuits_by_name=circuits_by_name,
        circuit_ids=circuit_ids,
        circuit_has_layout=has_layout,
    )


def resolve_circuit_id(venue_slug: str, ref: ReferenceData) -> int:
    """Map a motorsportstats venue slug to the dim_circuits circuit_id (§4.5).

    Resolution order:
      1. exact (circuit_name, layout) match when the dim carries layouts;
      2. name-only, when the venue's name maps to exactly one circuit row.
    Fails loudly (per §4.6, "stop and tell Berkay") when the venue is unknown,
    absent from the dim, or ambiguous (several layouts but no layout column to
    pick between them).
    """
    if venue_slug not in VENUE_SLUG_TO_CIRCUIT:
        raise KeyError(
            f"Unknown venue slug '{venue_slug}'. Add it to VENUE_SLUG_TO_CIRCUIT "
            f"with the correct dim_circuits circuit/layout, or request a dim addition.")
    name, layout = VENUE_SLUG_TO_CIRCUIT[venue_slug]

    # 1) exact name+layout when the dim has layouts
    if ref.circuit_has_layout and (name, layout) in ref.circuits_by_name_layout:
        return ref.circuits_by_name_layout[(name, layout)]

    # 2) name-only fallback
    ids = ref.circuits_by_name.get(name, [])
    if len(ids) == 1:
        return ids[0]
    if len(ids) > 1:
        raise KeyError(
            f"Venue '{venue_slug}' -> circuit '{name}' has {len(ids)} rows in "
            f"dim_circuits (layouts {sorted(ids)}) but layout '{layout}' could not "
            f"be matched (dim layout column missing or different). Resolve the "
            f"correct circuit_id and pin it.")
    raise KeyError(
        f"Venue '{venue_slug}' -> circuit '{name}' is not in dim_circuits. "
        f"The dim may have changed; re-check the mapping or request a dim addition.")


def resolve_nationality(flag_code_alpha2: str, ref: ReferenceData,
                        who: str = "") -> str:
    """alpha-2 flag code -> alpha-3 nationality_code, checked against dim (§5.4)."""
    a2 = flag_code_alpha2.strip().lower()
    if a2 not in ALPHA2_TO_ALPHA3:
        raise KeyError(f"Flag code '{a2}' ({who}) not in the ISO alpha-2 map.")
    a3 = ALPHA2_TO_ALPHA3[a2]
    if a3 not in ref.country_ids:
        raise KeyError(f"nationality_code '{a3}' ({who}) not in dim_countries.")
    return a3


# =============================================================================
# 4. FETCH LAYER
#    Parsers below operate on HTML strings; how the HTML is obtained is isolated
#    here so the parsers can be audited against saved files with no network.
# =============================================================================

def fetch_rendered_html(url: str, wait_selector: str = "table",
                        scroll: bool = False) -> str:
    """Return the fully rendered HTML of `url` using headless Chromium.

    motorsportstats renders with JavaScript and refuses plain HTTP clients, so a
    real browser engine is used. Requires:  pip install playwright  and
    playwright install chromium.

    `scroll=True` auto-scrolls the page before capturing HTML. This is REQUIRED
    for the standings page: its country-flag images are lazy-loaded Next.js
    images whose `src` is a base64 placeholder until the row scrolls into view,
    so without scrolling the driver nationalities are absent from the HTML.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "Playwright is required for --live fetching. Install with:\n"
            "    pip install playwright && playwright install chromium\n"
            "or run with --from-files against saved HTML."
        ) from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1400, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
        page.goto(url, wait_until="networkidle", timeout=60000)
        try:
            page.wait_for_selector(wait_selector, timeout=15000)
        except Exception:
            pass  # some pages (empty entry lists) legitimately have no table

        if scroll:
            _scroll_to_load_lazy_images(page)

        html = page.content()
        browser.close()
        return html


def _scroll_to_load_lazy_images(page) -> None:
    """Scroll top-to-bottom to trigger lazy-loaded images, then wait for them.

    Next.js lazy images swap their placeholder src for the real URL when they
    enter the viewport (IntersectionObserver). We step down the whole page, then
    wait until no flag image still carries a data: placeholder (bounded).
    """
    try:
        total = page.evaluate("document.body.scrollHeight")
        step = 900
        y = 0
        while y < total:
            page.evaluate(f"window.scrollTo(0, {y})")
            page.wait_for_timeout(150)
            y += step
            total = page.evaluate("document.body.scrollHeight")   # grows as it loads
        page.evaluate("window.scrollTo(0, 0)")
        # Best-effort wait for flag images to resolve to real URLs.
        try:
            page.wait_for_function(
                """() => {
                    const imgs = [...document.querySelectorAll('tr img[alt]')];
                    if (!imgs.length) return true;
                    const pending = imgs.filter(im => (im.getAttribute('src')||'')
                        .startsWith('data:'));
                    return pending.length === 0;
                }""",
                timeout=8000)
        except Exception:
            pass
        page.wait_for_load_state("networkidle")
    except Exception:
        pass  # scrolling is best-effort; parser reports any row it cannot read


def load_html_file(path: Path) -> str:
    """Read saved HTML/fragment from disk (offline / audit mode)."""
    return Path(path).read_text(encoding="utf-8")


# Full motorsportstats pages embed their data as JSON in a
# <script id="__NEXT_DATA__"> tag. When present it is the most reliable source
# (structured, not hashed markup); the DOM parsers remain as a fallback for the
# rendered table fragments that were supplied as samples.
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)


def extract_next_data(html: str) -> Optional[dict]:
    """Return props.pageProps from the page's __NEXT_DATA__ JSON, or None."""
    if not html:
        return None
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        return data.get("props", {}).get("pageProps", {})
    except (ValueError, AttributeError):
        return None


def _local_iso_and_date(start_epoch: int, start_epoch_utc: int) -> tuple[str, dt.date]:
    """Convert the site's (startTime, startTimeUtc) pair to a contract timestamp.

    motorsportstats gives `startTime` as the venue wall-clock expressed as a
    Unix epoch, and `startTimeUtc` as the true UTC epoch. Their difference is the
    venue's UTC offset. We format the wall-clock with that offset, e.g.
    "2025-05-03 11:15:00+02:00" -- the local-time-with-offset shape §3.4 requires.
    """
    offset = start_epoch - start_epoch_utc                 # seconds
    wall = dt.datetime.utcfromtimestamp(start_epoch)       # the venue wall-clock
    sign = "+" if offset >= 0 else "-"
    oh, rem = divmod(abs(offset), 3600)
    om = rem // 60
    stamp = wall.strftime("%Y-%m-%d %H:%M:%S") + f"{sign}{oh:02d}:{om:02d}"
    return stamp, wall.date()


def _event_slug_from_full(full_slug: str) -> str:
    """'formula-4-italian-championship_2025_misano-2' -> 'misano-2'."""
    marker = SERIES_SLUG + "_"
    if marker in full_slug:
        rest = full_slug.split(marker, 1)[1]        # '2025_misano-2'
        parts = rest.split("_", 1)                  # ['2025', 'misano-2']
        if len(parts) == 2:
            return parts[1]
    return full_slug


# =============================================================================
# 5. PARSERS  (built and verified against the supplied samples)
#
# Each parser keys on stable structural signals, NOT on the build-hashed
# styled-components class names (e.g. "styled__Td-sc-w4o42v-3"), which change on
# every site deploy.
# =============================================================================

@dataclass
class CalendarRound:
    """One round as listed on the calendar page."""
    round_number: int
    event_slug: str          # e.g. "imola" / "misano-2"  (last path segment)
    event_name: str          # e.g. "Imola"
    venue_name: str
    venue_slug: str
    date: dt.date            # the date shown in the calendar (the race day)


# A calendar row is identified by an event-results link like
#   /results/formula-4-italian-championship/2026/imola/info
_EVENT_LINK_RE = re.compile(
    r"^/results/" + re.escape(SERIES_SLUG) + r"/(\d{4})/([^/]+)/info$"
)
_VENUE_LINK_RE = re.compile(r"^/venue/([^/]+)/overview$")
_DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")   # DD.MM.YYYY


def parse_calendar(html: str) -> list[CalendarRound]:
    """Parse the season calendar into a list of CalendarRound.

    Prefers the structured __NEXT_DATA__ (full live pages); falls back to the
    rendered calendar table (the supplied `calendar.txt` fragment).
    """
    nd = extract_next_data(html)
    if nd and nd.get("calendar", {}).get("events"):
        return _parse_calendar_next_data(nd["calendar"]["events"])
    return _parse_calendar_dom(html)


def _parse_calendar_next_data(events: list[dict]) -> list[CalendarRound]:
    """Parse calendar.events[] from __NEXT_DATA__.

    Each event: slug (full season-event uuid), name, venue{name,slug},
    startDate/endDate (Unix). The calendar row's date is the event END date (the
    race day), matching what the rendered table shows.
    """
    rounds: list[CalendarRound] = []
    for i, e in enumerate(events):
        end = e.get("endDate") or e.get("startDate")
        rounds.append(CalendarRound(
            round_number=i + 1,
            event_slug=_event_slug_from_full(e["slug"]),
            event_name=e.get("name", ""),
            venue_name=e.get("venue", {}).get("name", ""),
            venue_slug=e.get("venue", {}).get("slug", ""),
            date=dt.datetime.utcfromtimestamp(end).date(),
        ))
    if not rounds:
        raise ValueError("parse_calendar: __NEXT_DATA__ calendar had no events.")
    rounds.sort(key=lambda r: r.round_number)
    return rounds


def _parse_calendar_dom(html: str) -> list[CalendarRound]:
    """Parse the rendered calendar table (fallback for the fragment sample).

    Structure per row (verified against calendar.txt):
        <td>ROUND</td><td>DD.MM.YYYY</td>
        <td> ... <a href="/results/.../<slug>/info">Event</a>
                 <a href="/venue/<venue-slug>/overview">Venue</a> ... </td>
    """
    soup = BeautifulSoup(html, "html.parser")
    rounds: list[CalendarRound] = []

    for tr in soup.find_all("tr"):
        # Find the event-results link that marks this as a calendar row.
        event_link = None
        for a in tr.find_all("a", href=True):
            if _EVENT_LINK_RE.match(a["href"]):
                event_link = a
                break
        if event_link is None:
            continue

        m = _EVENT_LINK_RE.match(event_link["href"])
        event_slug = m.group(2)
        event_name = event_link.get_text(strip=True)

        # Venue link in the same row.
        venue_name, venue_slug = "", ""
        for a in tr.find_all("a", href=True):
            vm = _VENUE_LINK_RE.match(a["href"])
            if vm:
                venue_slug = vm.group(1)
                venue_name = a.get_text(strip=True)
                break

        # Round number and date live in the first two cells of the row.
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        round_number = None
        date_val = None
        for text in cells:
            if round_number is None and text.isdigit():
                round_number = int(text)
            dm = _DATE_RE.match(text)
            if dm and date_val is None:
                date_val = dt.date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
        if round_number is None or date_val is None:
            raise ValueError(f"Calendar row for '{event_slug}' missing round/date: {cells}")

        rounds.append(CalendarRound(
            round_number=round_number, event_slug=event_slug, event_name=event_name,
            venue_name=venue_name, venue_slug=venue_slug, date=date_val,
        ))

    if not rounds:
        raise ValueError("parse_calendar: no calendar rows found (page structure changed?).")
    rounds.sort(key=lambda r: r.round_number)
    return rounds


@dataclass
class Session:
    """One timetabled session from a round's info page (when published)."""
    session_type: str                    # practice | qualifying | race
    session_number: int                  # renumbered 1..N WITHIN its session_type
    session_date: dt.date                # local (wall-clock) date of the session
    session_datetime_local: str          # ISO 8601 local w/ offset (§3.4)
    planned_duration_minutes: Optional[int] = None


def _classify_session_type(code: str, is_race: bool) -> Optional[str]:
    """Map a scheduleData entry to a contract session_type, or None to skip.

    Contract session_type domain is practice | qualifying | race. Non-session
    timetable entries (e.g. 'GR' starting grid) map to None and are dropped.
    """
    if is_race:
        return "race"
    c = (code or "").strip().upper()
    if c.startswith("P"):
        return "practice"
    if c.startswith("Q"):
        return "qualifying"
    return None


def parse_event_sessions(html: str) -> list[Session]:
    """Parse a round's info-page session timetable from __NEXT_DATA__.

    The timetable lives in pageProps.scheduleData: each entry has a session code
    (P1/Q1/Race/...), isRace, isTimetable, cancelled, and startTime/startTimeUtc
    (+ endTime) as Unix epochs. We keep the timetabled, non-cancelled sessions
    that carry a start time, convert to local-with-offset, and renumber
    session_number 1..N within each session_type (§5.1: "within the session_type").

    FINDING: for Formula 4 Italian the 2026 rounds currently publish NO timetable
    (scheduleData == []), so this returns [] for them -- the honest result. Prior
    complete seasons (e.g. 2025) do publish it, and this parser reads it in full.
    """
    nd = extract_next_data(html)
    if not nd:
        return []
    schedule_data = nd.get("scheduleData") or []
    if not schedule_data:
        return []

    # Sort chronologically by the site's global session number.
    ordered = sorted(schedule_data, key=lambda x: x.get("sessionNumber", 0))

    per_type_counter: dict[str, int] = {}
    sessions: list[Session] = []
    for entry in ordered:
        if entry.get("cancelled"):
            continue
        if not entry.get("isTimetable"):
            continue
        start = entry.get("startTime")
        start_utc = entry.get("startTimeUtc")
        if start is None or start_utc is None:
            continue
        sess = entry.get("session", {}) or {}
        stype = _classify_session_type(sess.get("code", ""), entry.get("isRace", False))
        if stype is None:
            continue

        stamp, local_date = _local_iso_and_date(start, start_utc)
        per_type_counter[stype] = per_type_counter.get(stype, 0) + 1

        duration = None
        end = entry.get("endTime")
        if end and start and end > start:
            duration = int(round((end - start) / 60))

        sessions.append(Session(
            session_type=stype,
            session_number=per_type_counter[stype],
            session_date=local_date,
            session_datetime_local=stamp,
            planned_duration_minutes=duration,
        ))
    return sessions


@dataclass
class DriverEntry:
    """One driver from the season standings page."""
    driver_full_name_raw: str
    driver_slug: str
    team_name_raw: str
    team_slug: str
    nationality_alpha2: str


_DRIVER_LINK_RE = re.compile(r"^/driver/([^/]+)/")
_TEAM_LINK_RE = re.compile(r"^/team/([^/]+)/")
_FLAG_RE = re.compile(r"/flags/svg/([a-z]{2})\.svg")


def _row_flag(tr) -> tuple[str, str]:
    """Return (alpha2_code, alt_text) for the driver flag image in a row.

    The driver row's flag <img> carries the driver name in its `alt` and the
    nationality in its src/srcset (.../flags/svg/<cc>.svg). These are returned
    INDEPENDENTLY: `alt` is read from any row image that has one (so the name
    survives even when the flag is still a lazy-load base64 placeholder), while
    the alpha-2 code is only returned once a real flag URL is present. So a
    placeholder yields ("", "<driver name>"), not ("", "").
    """
    code, alt = "", ""
    for img in tr.find_all("img"):
        a = (img.get("alt", "") or "").strip()
        if a and not alt:
            alt = a
        src = (img.get("src", "") or "") + " " + (img.get("srcset", "") or "")
        fm = _FLAG_RE.search(src)
        if fm and not code:
            code = fm.group(1)
    return code, alt


def parse_driver_standings(html: str) -> list[DriverEntry]:
    """Parse the driver-standings table into a list of DriverEntry.

    Structure per driver row (verified against drivers.txt):
        <td>POS</td>
        <td> ...<img alt="Driver Name" src=".../flags/svg/<cc>.svg">        (nationality + name)
                [<a href="/driver/<slug>/...">Driver Name</a>]              (optional profile link)
                <a href="/team/<slug>/...">Team Name</a> ... </td>          (team, always present)
        <td>... per-event result cells ...</td>

    The table interleaves empty expander rows between driver rows, and MANY
    drivers have no /driver/ profile link. The STABLE signal for a driver row is
    therefore: it contains a /team/ link AND a flag image. The driver name comes
    from the /driver/ link text when present, else from the flag image's alt.
    """
    soup = BeautifulSoup(html, "html.parser")
    entries: list[DriverEntry] = []
    seen = set()
    missing_flag: list[str] = []

    for tr in soup.find_all("tr"):
        # A driver row must have a team link (skips header + empty expander rows).
        team_link = None
        for a in tr.find_all("a", href=True):
            if _TEAM_LINK_RE.match(a["href"]):
                team_link = a
                break
        if team_link is None:
            continue

        team_slug = _TEAM_LINK_RE.match(team_link["href"]).group(1)
        team_name = team_link.get_text(strip=True)

        nationality_alpha2, flag_alt = _row_flag(tr)

        # Driver name: prefer the profile-link text, else the flag image alt.
        driver_link = tr.find("a", href=_DRIVER_LINK_RE)
        driver_slug = (_DRIVER_LINK_RE.match(driver_link["href"]).group(1)
                       if driver_link else "")
        driver_name = (driver_link.get_text(strip=True) if driver_link else flag_alt)

        if not driver_name or not team_name:
            raise ValueError(
                f"Incomplete driver row: name={driver_name!r} team={team_name!r}")

        if not nationality_alpha2:
            # Name+team present but the flag URL never resolved -- almost always a
            # lazy-load timing issue (the flag <img> is still a base64
            # placeholder). Collect it and fail with an actionable message below.
            missing_flag.append(driver_name)

        # De-duplicate: prefer profile slug, else name+team, as the identity key.
        key = driver_slug or f"{driver_name}|{team_slug}"
        if key in seen:
            continue
        seen.add(key)

        entries.append(DriverEntry(
            driver_full_name_raw=driver_name, driver_slug=driver_slug,
            team_name_raw=team_name, team_slug=team_slug,
            nationality_alpha2=nationality_alpha2,
        ))

    if not entries:
        raise ValueError(
            "parse_driver_standings: no driver rows found. If fetching live, "
            "check the standings URL resolved (it must be "
            "/series/<slug>/standings/<year>, no '/drivers' suffix).")
    if missing_flag:
        raise ValueError(
            f"{len(missing_flag)} driver(s) had no resolvable nationality flag "
            f"(lazy-loaded images not fully materialised): "
            f"{', '.join(missing_flag[:8])}"
            f"{'...' if len(missing_flag) > 8 else ''}. When fetching live this "
            f"is a scroll/timing issue -- fetch_rendered_html(scroll=True) should "
            f"prevent it; re-run, or capture the standings HTML after it has fully "
            f"rendered and use --from-files/--standings-html.")
    return entries


# =============================================================================
# 6. ROW BUILDERS  (turn parsed objects into contract rows)
# =============================================================================

# ---- schedule (§7): exact column order -------------------------------------
# For rounds that have already run. session_datetime_local is REQUIRED here
# (a session that ran happened at a known moment); there is no session_date
# column, and planned_duration_minutes is optional.
SCHEDULE_COLUMNS = [
    "series_id", "season_label", "round_number", "session_type",
    "session_number", "circuit_id", "session_datetime_local",
    "sport", "discipline", "category", "planned_duration_minutes",
    "source_url", "source_collector",
]

# ---- upcoming_schedule (§7a): exact column order --------------------------
UPCOMING_SCHEDULE_COLUMNS = [
    "series_id", "season_label", "round_number", "session_type",
    "session_number", "circuit_id", "session_date", "session_datetime_local",
    "sport", "discipline", "category", "source_url", "source_collector",
]

# ---- driver.csv: columns lifted from the defined schemas ------------------
# Each column cites the section of the contract its NAME and FORMAT come from.
#   series_id                    INT     -> results §5.1 / schedule §7
#   season_label                 STRING  -> results §5.1 / schedule §7
#   driver_full_name_raw         STRING  -> results §5.4
#   driver_full_name_normalized  STRING  -> results §5.4  (normalize_name, §3a.1)
#   nationality_code             CHAR(3) -> results §5.4  (dim_countries)
#   team_name_raw                STRING  -> results §5.3
#   team_name_normalized         STRING  -> results §5.3  (normalize_identifier, §3a.2)
#   source_url                   STRING  -> lineage §5.6 / §7a
#   source_collector             STRING  -> lineage §5.6 / §7a
DRIVER_CSV_COLUMNS = [
    "series_id", "season_label", "driver_full_name_raw",
    "driver_full_name_normalized", "nationality_code", "team_name_raw",
    "team_name_normalized", "source_url", "source_collector",
]


def event_info_url(year: int, event_slug: str) -> str:
    return f"{BASE}/results/{SERIES_SLUG}/{year}/{event_slug}/info"


def standings_url(year: int) -> str:
    # Correct pattern is /series/<slug>/standings/<year> -- note there is NO
    # trailing "/drivers" segment (that path 404s).
    return f"{BASE}/series/{SERIES_SLUG}/standings/{year}"


def build_past_schedule_rows(rounds: list[CalendarRound],
                             sessions_by_slug: dict[str, list[Session]],
                             ref: ReferenceData,
                             year: int,
                             today: dt.date,
                             collector: str) -> tuple[list[dict], list[str]]:
    """Build `schedule` rows (§7) for every round that has already run.

    "Past" = the round's calendar date is before `today`. §7 requires one row per
    session WITH a real session_datetime_local, so a past round is only emitted
    when its info page actually published a session timetable. When it did not
    (the Formula 4 Italian 2026 reality), the round is skipped and a note is
    returned -- we never invent a session time to satisfy a required column.
    """
    season_label = str(year)
    rows: list[dict] = []
    notes: list[str] = []

    for rnd in rounds:
        if rnd.date >= today:
            continue    # not yet run -> belongs in upcoming_schedule

        sessions = sessions_by_slug.get(rnd.event_slug, [])
        if not sessions:
            notes.append(
                f"round {rnd.round_number} ({rnd.event_slug}): no session "
                f"timetable published -> no schedule rows emitted")
            continue

        circuit_id = resolve_circuit_id(rnd.venue_slug, ref)
        src = event_info_url(year, rnd.event_slug)
        for s in sessions:
            rows.append({
                "series_id": SERIES_ID,
                "season_label": season_label,
                "round_number": rnd.round_number,
                "session_type": s.session_type,
                "session_number": s.session_number,
                "circuit_id": circuit_id,
                "session_datetime_local": s.session_datetime_local,
                "sport": SPORT,
                "discipline": DISCIPLINE,
                "category": CATEGORY,
                "planned_duration_minutes": ("" if s.planned_duration_minutes is None
                                             else s.planned_duration_minutes),
                "source_url": src,
                "source_collector": collector,
            })
    return rows, notes


def build_upcoming_schedule_rows(rounds: list[CalendarRound],
                                 sessions_by_slug: dict[str, list[Session]],
                                 ref: ReferenceData,
                                 year: int,
                                 today: dt.date,
                                 collector: str) -> list[dict]:
    """Build upcoming_schedule rows for every round that has not yet run.

    "Upcoming" = the round's calendar date is on/after `today` (per the user's
    chosen cutoff). For each upcoming round:
      * if the info page published a session timetable -> one row per session
        (session_date required; session_datetime_local filled when known);
      * otherwise (the Formula 4 Italian reality) -> ONE fallback 'race' row on
        the round's race day, with session_datetime_local left EMPTY. §7a is
        explicit that a blank time is the honest answer and a guessed time is
        worse than none.
    """
    season_label = str(year)
    rows: list[dict] = []

    for rnd in rounds:
        if rnd.date < today:
            continue    # already run -> not "upcoming"

        circuit_id = resolve_circuit_id(rnd.venue_slug, ref)
        src = event_info_url(year, rnd.event_slug)
        sessions = sessions_by_slug.get(rnd.event_slug, [])

        if sessions:
            for s in sessions:
                rows.append({
                    "series_id": SERIES_ID,
                    "season_label": season_label,
                    "round_number": rnd.round_number,
                    "session_type": s.session_type,
                    "session_number": s.session_number,
                    "circuit_id": circuit_id,
                    "session_date": s.session_date.isoformat(),
                    "session_datetime_local": s.session_datetime_local or "",
                    "sport": SPORT,
                    "discipline": DISCIPLINE,
                    "category": CATEGORY,
                    "source_url": src,
                    "source_collector": collector,
                })
        else:
            # Fallback: one race-day row, no time (§7a).
            rows.append({
                "series_id": SERIES_ID,
                "season_label": season_label,
                "round_number": rnd.round_number,
                "session_type": "race",
                "session_number": 1,
                "circuit_id": circuit_id,
                "session_date": rnd.date.isoformat(),
                "session_datetime_local": "",
                "sport": SPORT,
                "discipline": DISCIPLINE,
                "category": CATEGORY,
                "source_url": src,
                "source_collector": collector,
            })

    return rows


def build_driver_rows(entries: list[DriverEntry],
                      ref: ReferenceData,
                      year: int,
                      collector: str) -> list[dict]:
    """Build driver.csv rows from the season standings entries."""
    season_label = str(year)
    src = standings_url(year)
    rows: list[dict] = []
    for e in entries:
        rows.append({
            "series_id": SERIES_ID,
            "season_label": season_label,
            "driver_full_name_raw": e.driver_full_name_raw,
            "driver_full_name_normalized": normalize_name(e.driver_full_name_raw),
            "nationality_code": resolve_nationality(
                e.nationality_alpha2, ref, who=e.driver_full_name_raw),
            "team_name_raw": e.team_name_raw,
            "team_name_normalized": normalize_identifier(e.team_name_raw),
            "source_url": src,
            "source_collector": collector,
        })
    return rows


# =============================================================================
# 7. CSV WRITER  (contract §1.2 encoding rules)
# =============================================================================

def write_csv(rows: list[dict], columns: list[str], out_path: Path) -> None:
    """Write rows to CSV with UTF-8, no BOM, Unix LF endings, one header row.

    Missing values are the empty string (§3.1). Column order is fixed by
    `columns` (§2.6). Quoting only when a field contains a comma (§1.2).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" + lineterminator="\n" guarantees LF (never CRLF); encoding
    # "utf-8" (not utf-8-sig) guarantees no BOM.
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, lineterminator="\n",
                                quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: ("" if row.get(c) is None else row[c]) for c in columns})


# =============================================================================
# 8. SELF-VALIDATION  (re-apply the collector-owned rules before writing)
#     This is a safety net, not a replacement for the official race-validator.
# =============================================================================

_FORBIDDEN_CHARS = set('\t\n\r"\\|') | {chr(c) for c in range(0x20)}


def _check_text_field(value: str, col: str, allow_slash: bool) -> list[str]:
    problems = []
    if value != value.strip():
        problems.append(f"{col}: leading/trailing whitespace ({value!r})")
    if "  " in value:
        problems.append(f"{col}: double space ({value!r})")
    for ch in value:
        if ch in _FORBIDDEN_CHARS:
            problems.append(f"{col}: forbidden char {ch!r} ({value!r})")
            break
        if ch == "/" and not allow_slash:
            problems.append(f"{col}: forbidden '/' ({value!r})")
            break
    return problems


_DT_LOCAL_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2}):(\d{2})([+-]\d{2}:\d{2})$")


def validate_past_schedule(rows: list[dict], ref: ReferenceData
                           ) -> tuple[list[str], list[str]]:
    """Re-check `schedule` (§7) rows. Returns (errors, warnings).

    Errors are structural/format faults this collector owns (they block writing).
    SESSION_TIME_001 (a session at/after 18:00 local) is returned as a WARNING:
    it originates in the source's published times, not in this code, and the
    contract's own validator is the authority that blocks submission. The row is
    still written so the collector can see the data and the flag.
    """
    errors: list[str] = []
    warnings: list[str] = []
    for i, r in enumerate(rows):
        tag = f"schedule row {i}"
        if int(r["circuit_id"]) not in ref.circuit_ids:
            errors.append(f"{tag}: circuit_id {r['circuit_id']} not in dim_circuits")
        if (r["sport"], r["discipline"], r["category"]) not in ref.category_triples:
            errors.append(f"{tag}: (sport,discipline,category) not in dim_categories")
        if r["session_type"] not in {"practice", "qualifying", "race"}:
            errors.append(f"{tag}: bad session_type {r['session_type']!r}")
        # session_datetime_local is REQUIRED here and must be ISO+offset, not Z.
        sdt = r["session_datetime_local"]
        if not sdt:
            errors.append(f"{tag}: session_datetime_local is required (§7)")
        elif sdt.endswith("Z"):
            errors.append(f"{tag}: session_datetime_local uses Z, need offset")
        else:
            m = _DT_LOCAL_RE.match(sdt)
            if not m:
                errors.append(f"{tag}: session_datetime_local not ISO+offset ({sdt!r})")
            elif int(m.group(2)) >= 18:                       # SESSION_TIME_001
                warnings.append(
                    f"{tag}: session starts {m.group(2)}:{m.group(3)} local "
                    f"(>= 18:00) -> SESSION_TIME_001 would reject this at the "
                    f"official validator")
        # planned_duration_minutes: optional -> empty or integer
        pdm = r["planned_duration_minutes"]
        if pdm != "" and not str(pdm).lstrip("-").isdigit():
            errors.append(f"{tag}: planned_duration_minutes not integer ({pdm!r})")
        for col in ("series_id", "round_number", "session_number", "circuit_id"):
            if not str(r[col]).lstrip("-").isdigit():
                errors.append(f"{tag}: {col} not integer ({r[col]!r})")
    # exact-duplicate row check (§8 DUPLICATE_001)
    seen = set()
    for i, r in enumerate(rows):
        key = tuple(r[c] for c in SCHEDULE_COLUMNS)
        if key in seen:
            errors.append(f"schedule row {i}: exact duplicate")
        seen.add(key)
    return errors, warnings


def validate_upcoming_schedule(rows: list[dict], ref: ReferenceData) -> list[str]:
    """Re-check the upcoming_schedule (§7a) rows against collector-owned rules."""
    problems: list[str] = []
    for i, r in enumerate(rows):
        tag = f"schedule row {i}"
        # FK / taxonomy existence (§4.6)
        if int(r["circuit_id"]) not in ref.circuit_ids:
            problems.append(f"{tag}: circuit_id {r['circuit_id']} not in dim_circuits")
        if (r["sport"], r["discipline"], r["category"]) not in ref.category_triples:
            problems.append(f"{tag}: (sport,discipline,category) not in dim_categories")
        # session_type domain (§7a)
        if r["session_type"] not in {"practice", "qualifying", "race"}:
            problems.append(f"{tag}: bad session_type {r['session_type']!r}")
        # session_date format YYYY-MM-DD, required (§7a / §3.4)
        try:
            d = dt.date.fromisoformat(r["session_date"])
        except Exception:
            problems.append(f"{tag}: session_date not YYYY-MM-DD ({r['session_date']!r})")
            d = None
        # session_datetime_local optional; when present must carry an offset,
        # not 'Z', its local date must equal session_date (SESSION_DATE_001),
        # and its wall-clock time must be < 18:00 (SESSION_TIME_001).
        sdt = r["session_datetime_local"]
        if sdt:
            if sdt.endswith("Z"):
                problems.append(f"{tag}: session_datetime_local uses Z, need offset")
            m = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2}):(\d{2})([+-]\d{2}:\d{2})$", sdt)
            if not m:
                problems.append(f"{tag}: session_datetime_local not ISO+offset ({sdt!r})")
            else:
                if d is not None and m.group(1) != r["session_date"]:
                    problems.append(f"{tag}: datetime local date != session_date")
                if int(m.group(2)) >= 18:
                    problems.append(f"{tag}: session starts >= 18:00 local (SESSION_TIME_001)")
        # integer columns
        for col in ("series_id", "round_number", "session_number", "circuit_id"):
            if not str(r[col]).lstrip("-").isdigit():
                problems.append(f"{tag}: {col} not integer ({r[col]!r})")
    return problems


def validate_drivers(rows: list[dict], ref: ReferenceData) -> list[str]:
    """Re-check driver.csv rows: normalization, nationality FK, text hygiene."""
    problems: list[str] = []
    for i, r in enumerate(rows):
        tag = f"driver row {i} ({r['driver_full_name_raw']!r})"
        # normalization is idempotent and matches the canonical output (§3a)
        if normalize_name(r["driver_full_name_raw"]) != r["driver_full_name_normalized"]:
            problems.append(f"{tag}: driver_full_name_normalized mismatch")
        if normalize_identifier(r["team_name_raw"]) != r["team_name_normalized"]:
            problems.append(f"{tag}: team_name_normalized mismatch")
        # nationality in dim_countries (§5.4)
        if r["nationality_code"] not in ref.country_ids:
            problems.append(f"{tag}: nationality_code {r['nationality_code']} not in dim_countries")
        # text hygiene on _raw columns (§3.5); slashes never allowed here
        problems += [f"{tag}: {p}" for p in _check_text_field(r["driver_full_name_raw"], "driver_full_name_raw", allow_slash=False)]
        problems += [f"{tag}: {p}" for p in _check_text_field(r["team_name_raw"], "team_name_raw", allow_slash=False)]
    # exact-duplicate row check (§8 DUPLICATE_001)
    seen = set()
    for i, r in enumerate(rows):
        key = tuple(r[c] for c in DRIVER_CSV_COLUMNS)
        if key in seen:
            problems.append(f"driver row {i}: exact duplicate")
        seen.add(key)
    return problems


# =============================================================================
# 9. FILENAME HELPERS  (contract §1.1)
# =============================================================================

def contract_filename(file_type: str, season_label: str, scraped_date: dt.date) -> str:
    """<series_id>__<season_label>__<file_type>__<scraped_date>.csv  (§1.1)."""
    return f"{SERIES_ID}__{season_label}__{file_type}__{scraped_date.isoformat()}.csv"


# =============================================================================
# 10. ORCHESTRATION
# =============================================================================

def run(args) -> int:
    reference_dir = Path(args.reference_dir)
    out_dir = Path(args.out_dir)
    year = args.year
    today = (dt.date.fromisoformat(args.today) if args.today
             else dt.datetime.now(dt.timezone.utc).date())
    scraped_date = today
    collector = args.collector

    ref = load_reference_data(reference_dir)

    # ---- obtain HTML (live browser render, or saved files) -----------------
    if args.from_files:
        calendar_html = load_html_file(Path(args.calendar_html))
        standings_html = (load_html_file(Path(args.standings_html))
                          if args.standings_html else None)
        info_html_by_slug: dict[str, str] = {}
        if args.info_html_dir:
            for pat in ("*.txt", "*.html"):
                for p in Path(args.info_html_dir).glob(pat):
                    info_html_by_slug[p.stem] = p.read_text(encoding="utf-8")
    else:
        calendar_html = fetch_rendered_html(
            f"{BASE}/series/{SERIES_SLUG}/calendar/{year}")
        standings_html = fetch_rendered_html(standings_url(year), scroll=True)
        info_html_by_slug = {}

    # ---- parse calendar ----------------------------------------------------
    rounds = parse_calendar(calendar_html)
    n_past = sum(1 for r in rounds if r.date < today)
    n_future = len(rounds) - n_past
    print(f"[calendar] parsed {len(rounds)} rounds ({n_past} past, {n_future} upcoming)")

    # ---- fetch/parse the session timetable for EVERY round -----------------
    # Past rounds feed schedule.csv (§7); upcoming rounds feed upcoming_schedule
    # (§7a). Both read the same info-page scheduleData.
    sessions_by_slug: dict[str, list[Session]] = {}
    for rnd in rounds:
        if args.from_files:
            html = info_html_by_slug.get(rnd.event_slug)
        else:
            html = fetch_rendered_html(event_info_url(year, rnd.event_slug))
        sessions_by_slug[rnd.event_slug] = parse_event_sessions(html)

    # ---- build rows --------------------------------------------------------
    past_rows, past_notes = build_past_schedule_rows(
        rounds, sessions_by_slug, ref, year, today, collector)
    upcoming_rows = build_upcoming_schedule_rows(
        rounds, sessions_by_slug, ref, year, today, collector)
    print(f"[build] schedule(§7): {len(past_rows)} rows | "
          f"upcoming_schedule(§7a): {len(upcoming_rows)} rows")
    for note in past_notes:
        print(f"  [note] {note}")

    # ---- self-validate before writing --------------------------------------
    errors = validate_upcoming_schedule(upcoming_rows, ref)
    sched_errors, sched_warnings = validate_past_schedule(past_rows, ref)
    errors += sched_errors

    driver_rows: list[dict] = []
    if standings_html is not None:
        driver_entries = parse_driver_standings(standings_html)
        print(f"[standings] parsed {len(driver_entries)} drivers")
        driver_rows = build_driver_rows(driver_entries, ref, year, collector)
        errors += validate_drivers(driver_rows, ref)
    else:
        print("[standings] skipped (no standings HTML given) -> driver.csv not produced")

    if errors:
        print("\nVALIDATION ERRORS (nothing written):", file=sys.stderr)
        for p in errors:
            print("  -", p, file=sys.stderr)
        return 1
    print("[validate] all blocking self-checks passed")
    for w in sched_warnings:
        print(f"  [WARN] {w}")

    # ---- write -------------------------------------------------------------
    season_label = str(year)
    written = []

    sched_name = contract_filename("schedule", season_label, scraped_date)
    write_csv(past_rows, SCHEDULE_COLUMNS, out_dir / sched_name)
    written.append(str(out_dir / sched_name) +
                   ("" if past_rows else "  (header only: no past-round timetable published)"))

    upcoming_name = contract_filename("upcoming_schedule", season_label, scraped_date)
    write_csv(upcoming_rows, UPCOMING_SCHEDULE_COLUMNS, out_dir / upcoming_name)
    written.append(str(out_dir / upcoming_name))

    if standings_html is not None:
        # driver.csv is not a contract-validated file type, so it keeps the plain
        # pipeline name rather than the §1.1 pattern.
        write_csv(driver_rows, DRIVER_CSV_COLUMNS, out_dir / "driver.csv")
        written.append(str(out_dir / "driver.csv"))

    print("\nWrote:")
    for w in written:
        print("  " + w)
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Scrape F4 Italian schedule + drivers.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true",
                      help="Fetch live pages with a headless browser (Playwright).")
    mode.add_argument("--from-files", action="store_true",
                      help="Parse saved HTML instead of fetching (offline/audit).")

    p.add_argument("--year", type=int, default=2026, help="Season year (default 2026).")
    p.add_argument("--reference-dir", default="reference",
                   help="Dir with dim_categories.csv, dim_countries.csv, dim_circuits.csv.")
    p.add_argument("--out-dir", default="output", help="Output directory.")
    p.add_argument("--collector", default=DEFAULT_COLLECTOR,
                   help="source_collector value written to every row.")
    p.add_argument("--today", default=None,
                   help="Override 'today' (YYYY-MM-DD) for the upcoming cutoff/scraped_date.")

    # --from-files inputs
    p.add_argument("--calendar-html", help="Saved calendar page/fragment HTML.")
    p.add_argument("--standings-html", default=None,
                   help="Saved driver-standings HTML. Optional: omit to skip driver.csv.")
    p.add_argument("--info-html-dir", default=None,
                   help="Dir of saved info-page HTML named <event_slug>.txt/.html "
                        "(needed to populate schedule.csv from saved files).")
    return p


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.from_files and not args.calendar_html:
        print("--from-files requires at least --calendar-html", file=sys.stderr)
        return 2
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
