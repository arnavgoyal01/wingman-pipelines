#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
f4spain_scraper.py
==================

Fetches the FIA Spanish Formula 4 Championship (https://f4spain.org/) calendar and
entry list, then reports:

  1. The schedule of ALL rounds in the season (every round is included; each round
     also carries an `is_upcoming` flag telling you whether its race weekend has
     not yet finished, relative to today's date).
  2. The teams and drivers -- with their nationalities -- taking part in those
     rounds.

--------------------------------------------------------------------------------
HOW THE WEBSITE IS STRUCTURED (and why this script reads the pages it does)
--------------------------------------------------------------------------------
The website does NOT publish a separate entry list for each individual round.
It publishes exactly two relevant things:

  * A season CALENDAR -- shown on the home page ("/") -- listing every round with
    its round number, circuit, country flag, date range, and a link to a round
    detail page.
  * A single season ROSTER -- the "Teams & Drivers" page ("/f4-teams-2026/") --
    listing every team and, under each, its drivers with car number and a
    nationality flag.

Because the roster is season-wide, the same set of teams/drivers competes in
every round of the season. This script therefore attaches the full season roster
to every round. (This design was confirmed with the requester.)

Each round also has its own detail page (e.g. "/round/f4s-round-4-2026/"). Those
pages carry the confirmed season year and, for a round that has not happened yet,
a machine-readable countdown timestamp (the exact race-weekend start). This
script uses the round detail page only to ENRICH the calendar data with that
precise start timestamp; it is never required.

--------------------------------------------------------------------------------
DATA SOURCES (all under https://f4spain.org)
--------------------------------------------------------------------------------
  Home page      "/"                     -> season calendar (list of rounds)
  Round page     "/round/<slug>/"        -> confirmed year + precise start time
  Roster page    "/f4-teams-2026/"       -> teams, drivers, nationalities

--------------------------------------------------------------------------------
OUTPUT
--------------------------------------------------------------------------------
  CONTRACT-COMPLIANT (Race Results Data Contract v2.2.1, §7 schedule):
  <outdir>/<series_id>__<season>__schedule__<scraped_date>.csv
                                 One row per planned SESSION (practice /
                                 qualifying / race). Matches §7 exactly:
                                 column names + order, filename, UTF-8/no-BOM/LF,
                                 series_id 68, circuit_id from dim_circuits,
                                 session_datetime_local with venue offset.

  NON-CONTRACT (reference only -- will NOT pass race-validator / be ingested):
  <outdir>/f4spain_data.json             Full nested structure (schedule + roster).
  <outdir>/roster_NONCONTRACT.csv        One row per driver in the season roster.
  <outdir>/round_entries_NONCONTRACT.csv One row per (round x driver), all rounds.

  Note: the roster (teams/drivers/nationalities) has no valid home in the
  contract -- it is neither a `results` row (no session result exists for an
  un-raced round) nor a `schedule` row (no driver columns). It is kept here as
  a separate, clearly-labelled reference output.

--------------------------------------------------------------------------------
USAGE
--------------------------------------------------------------------------------
  # Fetch live from f4spain.org and write outputs into ./output
  python3 f4spain_scraper.py --outdir output

  # Parse a folder of previously-saved HTML files instead of the network.
  # (Expects files named: home-page, teams-drivers, and one file per round.)
  python3 f4spain_scraper.py --local-dir "./saved_html" --outdir output

  # Override the season (defaults to the year detected on the home page).
  python3 f4spain_scraper.py --season 2026

The script is deliberately written as small, single-purpose functions with
explicit selectors so that each extraction step can be read and audited on its
own. Nothing is hidden behind clever one-liners.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency: Python 3.9+ (zoneinfo). Also: pip install tzdata")

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency: pip install requests")

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency: pip install beautifulsoup4 lxml")


# =============================================================================
# 1. CONFIGURATION
# =============================================================================

BASE_URL = "https://f4spain.org"
HOME_URL = f"{BASE_URL}/"
ROSTER_URL = f"{BASE_URL}/f4-teams-2026/"

# A polite, identifiable User-Agent and a sensible timeout.
HTTP_HEADERS = {
    "User-Agent": (
        "F4SpainScheduleBot/1.0 (+https://f4spain.org; educational data collection)"
    )
}
HTTP_TIMEOUT = 30  # seconds
HTTP_RETRIES = 3

# Months, used when turning a human date range ("22-23 August") into real dates.
MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# ISO 3166-1 alpha-2 country codes -> English country names.
# The site encodes nationality as a CSS flag class such as "fi fi-es" (Spain).
# We translate the two-letter code to a readable name. A full table is embedded
# (rather than pulling in a country library) so the mapping is auditable inline.
COUNTRY_BY_CODE = {
    "ad": "Andorra", "ae": "United Arab Emirates", "af": "Afghanistan",
    "ag": "Antigua and Barbuda", "ai": "Anguilla", "al": "Albania",
    "am": "Armenia", "ao": "Angola", "aq": "Antarctica", "ar": "Argentina",
    "as": "American Samoa", "at": "Austria", "au": "Australia", "aw": "Aruba",
    "ax": "Aland Islands", "az": "Azerbaijan", "ba": "Bosnia and Herzegovina",
    "bb": "Barbados", "bd": "Bangladesh", "be": "Belgium", "bf": "Burkina Faso",
    "bg": "Bulgaria", "bh": "Bahrain", "bi": "Burundi", "bj": "Benin",
    "bl": "Saint Barthelemy", "bm": "Bermuda", "bn": "Brunei", "bo": "Bolivia",
    "bq": "Caribbean Netherlands", "br": "Brazil", "bs": "Bahamas",
    "bt": "Bhutan", "bv": "Bouvet Island", "bw": "Botswana", "by": "Belarus",
    "bz": "Belize", "ca": "Canada", "cc": "Cocos Islands",
    "cd": "DR Congo", "cf": "Central African Republic", "cg": "Congo",
    "ch": "Switzerland", "ci": "Cote d'Ivoire", "ck": "Cook Islands",
    "cl": "Chile", "cm": "Cameroon", "cn": "China", "co": "Colombia",
    "cr": "Costa Rica", "cu": "Cuba", "cv": "Cape Verde", "cw": "Curacao",
    "cx": "Christmas Island", "cy": "Cyprus", "cz": "Czechia", "de": "Germany",
    "dj": "Djibouti", "dk": "Denmark", "dm": "Dominica",
    "do": "Dominican Republic", "dz": "Algeria", "ec": "Ecuador",
    "ee": "Estonia", "eg": "Egypt", "eh": "Western Sahara", "er": "Eritrea",
    "es": "Spain", "et": "Ethiopia", "fi": "Finland", "fj": "Fiji",
    "fk": "Falkland Islands", "fm": "Micronesia", "fo": "Faroe Islands",
    "fr": "France", "ga": "Gabon", "gb": "United Kingdom", "gd": "Grenada",
    "ge": "Georgia", "gf": "French Guiana", "gg": "Guernsey", "gh": "Ghana",
    "gi": "Gibraltar", "gl": "Greenland", "gm": "Gambia", "gn": "Guinea",
    "gp": "Guadeloupe", "gq": "Equatorial Guinea", "gr": "Greece",
    "gs": "South Georgia", "gt": "Guatemala", "gu": "Guam", "gw": "Guinea-Bissau",
    "gy": "Guyana", "hk": "Hong Kong", "hm": "Heard Island and McDonald Islands",
    "hn": "Honduras", "hr": "Croatia", "ht": "Haiti", "hu": "Hungary",
    "id": "Indonesia", "ie": "Ireland", "il": "Israel", "im": "Isle of Man",
    "in": "India", "io": "British Indian Ocean Territory", "iq": "Iraq",
    "ir": "Iran", "is": "Iceland", "it": "Italy", "je": "Jersey",
    "jm": "Jamaica", "jo": "Jordan", "jp": "Japan", "ke": "Kenya",
    "kg": "Kyrgyzstan", "kh": "Cambodia", "ki": "Kiribati", "km": "Comoros",
    "kn": "Saint Kitts and Nevis", "kp": "North Korea", "kr": "South Korea",
    "kw": "Kuwait", "ky": "Cayman Islands", "kz": "Kazakhstan", "la": "Laos",
    "lb": "Lebanon", "lc": "Saint Lucia", "li": "Liechtenstein",
    "lk": "Sri Lanka", "lr": "Liberia", "ls": "Lesotho", "lt": "Lithuania",
    "lu": "Luxembourg", "lv": "Latvia", "ly": "Libya", "ma": "Morocco",
    "mc": "Monaco", "md": "Moldova", "me": "Montenegro", "mf": "Saint Martin",
    "mg": "Madagascar", "mh": "Marshall Islands", "mk": "North Macedonia",
    "ml": "Mali", "mm": "Myanmar", "mn": "Mongolia", "mo": "Macao",
    "mp": "Northern Mariana Islands", "mq": "Martinique", "mr": "Mauritania",
    "ms": "Montserrat", "mt": "Malta", "mu": "Mauritius", "mv": "Maldives",
    "mw": "Malawi", "mx": "Mexico", "my": "Malaysia", "mz": "Mozambique",
    "na": "Namibia", "nc": "New Caledonia", "ne": "Niger", "nf": "Norfolk Island",
    "ng": "Nigeria", "ni": "Nicaragua", "nl": "Netherlands", "no": "Norway",
    "np": "Nepal", "nr": "Nauru", "nu": "Niue", "nz": "New Zealand",
    "om": "Oman", "pa": "Panama", "pe": "Peru", "pf": "French Polynesia",
    "pg": "Papua New Guinea", "ph": "Philippines", "pk": "Pakistan",
    "pl": "Poland", "pm": "Saint Pierre and Miquelon", "pn": "Pitcairn Islands",
    "pr": "Puerto Rico", "ps": "Palestine", "pt": "Portugal", "pw": "Palau",
    "py": "Paraguay", "qa": "Qatar", "re": "Reunion", "ro": "Romania",
    "rs": "Serbia", "ru": "Russia", "rw": "Rwanda", "sa": "Saudi Arabia",
    "sb": "Solomon Islands", "sc": "Seychelles", "sd": "Sudan", "se": "Sweden",
    "sg": "Singapore", "sh": "Saint Helena", "si": "Slovenia",
    "sj": "Svalbard and Jan Mayen", "sk": "Slovakia", "sl": "Sierra Leone",
    "sm": "San Marino", "sn": "Senegal", "so": "Somalia", "sr": "Suriname",
    "ss": "South Sudan", "st": "Sao Tome and Principe", "sv": "El Salvador",
    "sx": "Sint Maarten", "sy": "Syria", "sz": "Eswatini",
    "tc": "Turks and Caicos Islands", "td": "Chad", "tf": "French Southern Territories",
    "tg": "Togo", "th": "Thailand", "tj": "Tajikistan", "tk": "Tokelau",
    "tl": "Timor-Leste", "tm": "Turkmenistan", "tn": "Tunisia", "to": "Tonga",
    "tr": "Turkey", "tt": "Trinidad and Tobago", "tv": "Tuvalu", "tw": "Taiwan",
    "tz": "Tanzania", "ua": "Ukraine", "ug": "Uganda", "us": "United States",
    "uy": "Uruguay", "uz": "Uzbekistan", "va": "Vatican City",
    "vc": "Saint Vincent and the Grenadines", "ve": "Venezuela",
    "vg": "British Virgin Islands", "vi": "U.S. Virgin Islands", "vn": "Vietnam",
    "vu": "Vanuatu", "wf": "Wallis and Futuna", "ws": "Samoa", "ye": "Yemen",
    "yt": "Mayotte", "za": "South Africa", "zm": "Zambia", "zw": "Zimbabwe",
}


def country_name(code: Optional[str]) -> Optional[str]:
    """Translate a two-letter flag code to a country name (or None if unknown)."""
    if not code:
        return None
    return COUNTRY_BY_CODE.get(code.lower(), code.upper())


# =============================================================================
# 1b. CONTRACT CONFIGURATION (Race Results Data Contract v2.2.1, §7 schedule)
# =============================================================================
# These values map the F4 Spain website onto the warehouse's master tables so
# the schedule CSV validates against the contract. They were resolved from the
# bundled dim_* reference files:
#   * dim_series      -> F4 Spanish Championship  => series_id 68
#   * dim_categories  -> motorsport/single_seater/formula_4 (valid triple)
#   * dim_circuits    -> each round's circuit + layout (see CIRCUIT_BY_NAME)

CONTRACT_SERIES_ID = 68            # dim_series.series_id for "F4 Spanish Championship"
CONTRACT_SPORT = "motorsport"      # dim_categories.sport
CONTRACT_DISCIPLINE = "single_seater"
CONTRACT_CATEGORY = "formula_4"
DEFAULT_COLLECTOR = "arnav"        # source_collector (override with --collector)

# Circuit resolution: map the short circuit name shown on the calendar card
# (h4, e.g. "Motorland", "Jarama") to the exact dim_circuits row (a specific
# layout) and the venue's IANA timezone (used to attach the correct local UTC
# offset, DST-aware, to session_datetime_local).
#
# Layout choices for the two circuits with several layouts were confirmed with
# the requester: Navarra -> "Speed Circuit Long" (764); Barcelona-Catalunya ->
# "Grand Prix Circuit" (254).
CIRCUIT_BY_NAME = {
    "ricardo tormo": {"circuit_id": 270, "tz": "Europe/Madrid"},   # Circuit Ricardo Tormo, GP
    "portimao":      {"circuit_id": 72,  "tz": "Europe/Lisbon"},   # Algarve, Car Circuit
    "motorland":     {"circuit_id": 734, "tz": "Europe/Madrid"},   # Motorland Aragón, GP
    "jarama":        {"circuit_id": 497, "tz": "Europe/Madrid"},   # Jarama, GP
    "jerez":         {"circuit_id": 501, "tz": "Europe/Madrid"},   # Jerez, GP
    "navarra":       {"circuit_id": 764, "tz": "Europe/Madrid"},   # Navarra, Speed Circuit Long
    "montmelo":      {"circuit_id": 254, "tz": "Europe/Madrid"},   # Barcelona-Catalunya, GP
    "barcelona":     {"circuit_id": 254, "tz": "Europe/Madrid"},   # alias for the same venue
}


def resolve_circuit(circuit_name: Optional[str]) -> Optional[dict]:
    """
    Map a calendar circuit name to its dim_circuits entry (circuit_id + tz).

    Matching is case-insensitive and tolerant of extra words: we look for any
    known circuit key contained in the name. Returns None if the circuit is not
    in CIRCUIT_BY_NAME -- the caller must then STOP and escalate (contract §10:
    a circuit missing from the bundled dim requires a library release).
    """
    if not circuit_name:
        return None
    needle = circuit_name.strip().lower()
    for key, value in CIRCUIT_BY_NAME.items():
        if key in needle:
            return value
    return None


def classify_session(name: Optional[str]) -> Optional[str]:
    """
    Map a website session label to the contract's session_type
    (`practice` | `qualifying` | `race`), or None if the session must be
    excluded / is unrecognized.

    Rules (confirmed with the requester):
      * "Race ..."                      -> race
      * "Qualification/Qualifying ..."  -> qualifying
      * "Free Practice", "Practice"     -> practice
      * "Official Test" / any "test"    -> EXCLUDED (returns None)
      * anything else                   -> None (skipped, with a warning logged)
    """
    if not name:
        return None
    text = name.strip().lower()
    if "test" in text:                       # "Official Test" -> excluded per request
        return None
    if "race" in text:
        return "race"
    if "qualif" in text or "qualy" in text or text.startswith("quali"):
        return "qualifying"
    if "practice" in text or "warm" in text:
        return "practice"
    return None


def local_timestamp_with_offset(iso_date: Optional[str], time_hhmm: Optional[str],
                                tz_name: Optional[str]) -> Optional[str]:
    """
    Build a contract session_datetime_local value: ISO 8601 local time WITH the
    venue's UTC offset (DST-aware), e.g. "2026-06-19T13:15:00+02:00".

    Returns None if any input is missing so the caller can decide what to do.
    """
    if not (iso_date and time_hhmm and tz_name):
        return None
    try:
        hour, minute = (int(x) for x in time_hhmm.split(":")[:2])
        year, month, day = (int(x) for x in iso_date.split("-"))
        local = dt.datetime(year, month, day, hour, minute, 0, tzinfo=ZoneInfo(tz_name))
    except (ValueError, KeyError):
        return None
    stamp = local.strftime("%Y-%m-%dT%H:%M:%S%z")   # offset like "+0200"
    return stamp[:-2] + ":" + stamp[-2:]            # -> "+02:00"


# =============================================================================
# 1c. BORROWED CONTRACT NORMALIZATION (for the non-contract roster / entries)
# =============================================================================
# The roster and round-entries files are NOT contract file types, so the
# contract does not dictate their shape. But several of their fields have direct
# counterparts in the results/schedule schemas, so -- at the requester's ask --
# we apply the contract's own conventions to those fields. See BORROWED_FIELDS
# below for the exact list.
#
# IMPORTANT: the contract (§3a.5) says the canonical normalizers live in
# `race_validator/normalize.py` and must be imported, not re-implemented. That
# library is not available in this scraper's environment, and these are
# non-contract files, so the two functions below are a FAITHFUL re-implementation
# of the §3a algorithm (verified against every worked example in the contract).
# For any *real* contract file, import the library's versions instead.

_NORMALIZE_PREMAP = {
    "ø": "o", "Ø": "O", "æ": "ae", "œ": "oe", "ß": "ss",
    "ð": "d", "þ": "th", "ł": "l", "đ": "d", "ı": "i", "ŋ": "n",
}


def _premap(text: str) -> str:
    for src, dst in _NORMALIZE_PREMAP.items():
        text = text.replace(src, dst)
    return text


def _strip_combining_marks(text: str) -> str:
    import unicodedata
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def normalize_name(value: str) -> str:
    """§3a.1 normalize_name -- person names -> lowercase ASCII, single-spaced."""
    import unicodedata
    if value is None or not value.strip():
        raise ValueError("normalize_name: empty input")
    if len(value) > 200:
        raise ValueError("normalize_name: input too long")
    value = _premap(value)
    value = unicodedata.normalize("NFKD", value)
    value = _strip_combining_marks(value)
    value = re.sub(r"[^A-Za-z0-9 ]", " ", value)   # non-alnum/space -> space
    value = value.lower()
    value = re.sub(r"\s+", " ", value).strip()
    return value


def normalize_identifier(value: str) -> str:
    """§3a.2 normalize_identifier -- entity ids -> lowercase ASCII with underscores."""
    import unicodedata
    if value is None or not value.strip():
        raise ValueError("normalize_identifier: empty input")
    if len(value) > 200:
        raise ValueError("normalize_identifier: input too long")
    value = _premap(value)
    value = unicodedata.normalize("NFKD", value)
    value = _strip_combining_marks(value)
    value = re.sub(r"[^A-Za-z0-9]+", "_", value)   # non-alnum run -> single _
    value = value.lower()
    value = value.strip("_")
    return value


# Forbidden characters in text values (contract §3.5). Stripped from *_raw values
# (diacritics are preserved -- only these control/structural chars are removed).
_FORBIDDEN_TEXT_CHARS = re.compile(r'[\x00-\x1f\t\r\n"\\|]')


def clean_raw_text(value: Optional[str]) -> Optional[str]:
    """
    Apply the contract's §3.5 text rules to a *_raw value: remove forbidden
    characters, collapse internal double spaces, trim ends -- while PRESERVING
    diacritics (Nicolás stays Nicolás). Forward slash is allowed only in URLs,
    so it is stripped here too (names never legitimately contain one).
    """
    if value is None:
        return None
    value = _FORBIDDEN_TEXT_CHARS.sub("", value)
    value = value.replace("/", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def bool_to_contract(value: Optional[bool]) -> str:
    """§3.3 booleans -> exactly 'TRUE' or 'FALSE' (uppercase). Empty if unknown."""
    if value is True:
        return "TRUE"
    if value is False:
        return "FALSE"
    return ""


def int_or_empty(value) -> str:
    """§3.2 integers -> plain digits, or empty string when missing/non-numeric."""
    if value is None or value == "":
        return ""
    match = re.search(r"-?\d+", str(value))
    return match.group() if match else ""


# ISO 3166-1 alpha-2 (site flag code) -> alpha-3 (contract nationality_code,
# CHAR(3), validated against dim_countries.country_id). Full table so any
# nationality that appears on the roster resolves. Codes not present here yield
# an empty nationality_code plus a warning, never a wrong guess.
ALPHA3_BY_ALPHA2 = {
    "ad": "AND", "ae": "ARE", "af": "AFG", "ag": "ATG", "ai": "AIA", "al": "ALB",
    "am": "ARM", "ao": "AGO", "aq": "ATA", "ar": "ARG", "as": "ASM", "at": "AUT",
    "au": "AUS", "aw": "ABW", "ax": "ALA", "az": "AZE", "ba": "BIH", "bb": "BRB",
    "bd": "BGD", "be": "BEL", "bf": "BFA", "bg": "BGR", "bh": "BHR", "bi": "BDI",
    "bj": "BEN", "bl": "BLM", "bm": "BMU", "bn": "BRN", "bo": "BOL", "bq": "BES",
    "br": "BRA", "bs": "BHS", "bt": "BTN", "bv": "BVT", "bw": "BWA", "by": "BLR",
    "bz": "BLZ", "ca": "CAN", "cc": "CCK", "cd": "COD", "cf": "CAF", "cg": "COG",
    "ch": "CHE", "ci": "CIV", "ck": "COK", "cl": "CHL", "cm": "CMR", "cn": "CHN",
    "co": "COL", "cr": "CRI", "cu": "CUB", "cv": "CPV", "cw": "CUW", "cx": "CXR",
    "cy": "CYP", "cz": "CZE", "de": "DEU", "dj": "DJI", "dk": "DNK", "dm": "DMA",
    "do": "DOM", "dz": "DZA", "ec": "ECU", "ee": "EST", "eg": "EGY", "eh": "ESH",
    "er": "ERI", "es": "ESP", "et": "ETH", "fi": "FIN", "fj": "FJI", "fk": "FLK",
    "fm": "FSM", "fo": "FRO", "fr": "FRA", "ga": "GAB", "gb": "GBR", "gd": "GRD",
    "ge": "GEO", "gf": "GUF", "gg": "GGY", "gh": "GHA", "gi": "GIB", "gl": "GRL",
    "gm": "GMB", "gn": "GIN", "gp": "GLP", "gq": "GNQ", "gr": "GRC", "gs": "SGS",
    "gt": "GTM", "gu": "GUM", "gw": "GNB", "gy": "GUY", "hk": "HKG", "hm": "HMD",
    "hn": "HND", "hr": "HRV", "ht": "HTI", "hu": "HUN", "id": "IDN", "ie": "IRL",
    "il": "ISR", "im": "IMN", "in": "IND", "io": "IOT", "iq": "IRQ", "ir": "IRN",
    "is": "ISL", "it": "ITA", "je": "JEY", "jm": "JAM", "jo": "JOR", "jp": "JPN",
    "ke": "KEN", "kg": "KGZ", "kh": "KHM", "ki": "KIR", "km": "COM", "kn": "KNA",
    "kp": "PRK", "kr": "KOR", "kw": "KWT", "ky": "CYM", "kz": "KAZ", "la": "LAO",
    "lb": "LBN", "lc": "LCA", "li": "LIE", "lk": "LKA", "lr": "LBR", "ls": "LSO",
    "lt": "LTU", "lu": "LUX", "lv": "LVA", "ly": "LBY", "ma": "MAR", "mc": "MCO",
    "md": "MDA", "me": "MNE", "mf": "MAF", "mg": "MDG", "mh": "MHL", "mk": "MKD",
    "ml": "MLI", "mm": "MMR", "mn": "MNG", "mo": "MAC", "mp": "MNP", "mq": "MTQ",
    "mr": "MRT", "ms": "MSR", "mt": "MLT", "mu": "MUS", "mv": "MDV", "mw": "MWI",
    "mx": "MEX", "my": "MYS", "mz": "MOZ", "na": "NAM", "nc": "NCL", "ne": "NER",
    "nf": "NFK", "ng": "NGA", "ni": "NIC", "nl": "NLD", "no": "NOR", "np": "NPL",
    "nr": "NRU", "nu": "NIU", "nz": "NZL", "om": "OMN", "pa": "PAN", "pe": "PER",
    "pf": "PYF", "pg": "PNG", "ph": "PHL", "pk": "PAK", "pl": "POL", "pm": "SPM",
    "pn": "PCN", "pr": "PRI", "ps": "PSE", "pt": "PRT", "pw": "PLW", "py": "PRY",
    "qa": "QAT", "re": "REU", "ro": "ROU", "rs": "SRB", "ru": "RUS", "rw": "RWA",
    "sa": "SAU", "sb": "SLB", "sc": "SYC", "sd": "SDN", "se": "SWE", "sg": "SGP",
    "sh": "SHN", "si": "SVN", "sj": "SJM", "sk": "SVK", "sl": "SLE", "sm": "SMR",
    "sn": "SEN", "so": "SOM", "sr": "SUR", "ss": "SSD", "st": "STP", "sv": "SLV",
    "sx": "SXM", "sy": "SYR", "sz": "SWZ", "tc": "TCA", "td": "TCD", "tf": "ATF",
    "tg": "TGO", "th": "THA", "tj": "TJK", "tk": "TKL", "tl": "TLS", "tm": "TKM",
    "tn": "TUN", "to": "TON", "tr": "TUR", "tt": "TTO", "tv": "TUV", "tw": "TWN",
    "tz": "TZA", "ua": "UKR", "ug": "UGA", "um": "UMI", "us": "USA", "uy": "URY",
    "uz": "UZB", "va": "VAT", "vc": "VCT", "ve": "VEN", "vg": "VGB", "vi": "VIR",
    "vn": "VNM", "vu": "VUT", "wf": "WLF", "ws": "WSM", "ye": "YEM", "yt": "MYT",
    "za": "ZAF", "zm": "ZMB", "zw": "ZWE",
}


def nationality_alpha3(alpha2: Optional[str]) -> str:
    """Map a site alpha-2 flag code to the contract's alpha-3 nationality_code."""
    if not alpha2:
        return ""
    return ALPHA3_BY_ALPHA2.get(alpha2.lower(), "")


# -----------------------------------------------------------------------------
# BORROWED_FIELDS -- the exact contract conventions applied to the NON-CONTRACT
# roster / round-entries files, and where each convention is defined.
#
#   Roster field -> contract counterpart / convention borrowed
#   -----------------------------------------------------------------------
#   team_name_raw                 <- team_name_raw            §2.5 (*_raw: keep
#                                                              diacritics; §3.5 text)
#   team_name_normalized          <- team_name_normalized     §3a.2 normalize_identifier
#   car_number                    <- car_number               §3.2 integer (§5.3)
#   driver_full_name_raw          <- driver_full_name_raw      §2.5 / §3.5
#   driver_full_name_normalized   <- driver_full_name_normalized §3a.1 normalize_name
#   nationality_code              <- nationality_code          §5.4 alpha-3 CHAR(3),
#                                                              validated vs dim_countries
#   source_url                    <- source_url               §5.6 lineage
#   source_collector              <- source_collector         §5.6 lineage
#   scraped_at                    <- scraped_at                §3.4 UTC "Z" timestamp
#
#   Additional in round_entries:
#   round_number                  <- round_number             §3.2 integer (§5.1)
#   circuit_id                    <- circuit_id               §5.1 FK to dim_circuits
#   round_start_date/round_end_date <- (date-only)            §3.4 YYYY-MM-DD
#   is_upcoming                   <- boolean naming/value      §2.3 / §3.3 TRUE|FALSE
#
#   NOT borrowed (no contract counterpart -- kept for human readability):
#   team_points, nationality_name, circuit_name_raw, driver_profile_url
# -----------------------------------------------------------------------------


# =============================================================================
# 2. DATA MODEL
# =============================================================================

@dataclass
class Driver:
    number: Optional[str]           # car number, e.g. "17"
    name: str                       # full name, e.g. "Nacho Tunon"
    nationality_code: Optional[str] # ISO alpha-2, e.g. "es"
    nationality: Optional[str]      # readable, e.g. "Spain"
    profile_url: Optional[str]      # link to the driver's page


@dataclass
class Team:
    name: str                       # display name, e.g. "MP Motorsport"
    points: Optional[int]           # championship points shown on the roster page
    profile_url: Optional[str]      # link to the team's page
    drivers: list = field(default_factory=list)  # list[Driver]


@dataclass
class Round:
    round_number: Optional[int]     # 1, 2, 3, ...
    circuit: Optional[str]          # short circuit name from the calendar card
    official_circuit_name: Optional[str]  # full name from the round page (if fetched)
    country_code: Optional[str]     # ISO alpha-2 of the host country
    country: Optional[str]          # readable host country
    date_text: Optional[str]        # raw text, e.g. "22-23 August"
    start_date: Optional[str]       # ISO date "2026-08-22"
    end_date: Optional[str]         # ISO date "2026-08-23"
    start_datetime: Optional[str]   # precise ISO timestamp from the round page, if any
    season: Optional[int]           # championship year
    url: Optional[str]              # round detail page URL
    is_upcoming: Optional[bool]     # True if the weekend has not finished yet
    sessions: list = field(default_factory=list)  # per-session timetable (see below)


@dataclass
class Session:
    """A single track session within a round weekend (FP / qualifying / race)."""
    day_label: Optional[str]        # "DAY 1"
    day_date_text: Optional[str]    # "Friday June 19"
    date: Optional[str]             # ISO date "2026-06-19"
    time: Optional[str]             # local start time "09:30"
    datetime_local: Optional[str]   # "2026-06-19T09:30" (local track time)
    name: Optional[str]             # "Free Practice", "Qualification 1", "Race 1"...


# =============================================================================
# 3. HTTP LAYER
# =============================================================================

def make_session() -> requests.Session:
    """Create a requests session with our standard headers."""
    session = requests.Session()
    session.headers.update(HTTP_HEADERS)
    return session


def fetch_html(session: requests.Session, url: str) -> str:
    """
    Download a page and return its HTML text.

    Retries a few times on transient network errors before giving up.
    """
    last_error = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            response = session.get(url, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
            return response.text
        except requests.RequestException as error:
            last_error = error
            print(f"  [warn] fetch failed ({attempt}/{HTTP_RETRIES}) for {url}: {error}",
                  file=sys.stderr)
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


# =============================================================================
# 4. SMALL PARSING HELPERS
# =============================================================================

def make_soup(html: str) -> BeautifulSoup:
    """Parse an HTML string into a BeautifulSoup tree (lxml backend)."""
    return BeautifulSoup(html, "lxml")


def flag_code_from(element) -> Optional[str]:
    """
    Given an element that carries flag CSS classes like 'flag fi fi-es fis',
    return the two-letter country code ('es'). Returns None if not present.
    """
    if element is None:
        return None
    for css_class in element.get("class", []):
        if css_class.startswith("fi-"):
            return css_class[3:]  # strip the "fi-" prefix
    return None


def clean_text(element) -> Optional[str]:
    """Collapse internal whitespace of an element's text; None if element missing."""
    if element is None:
        return None
    return re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()


def first_int(text: Optional[str]) -> Optional[int]:
    """Return the first integer found in a string, or None."""
    if not text:
        return None
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


# =============================================================================
# 5. PARSER: SEASON YEAR (from the home page)
# =============================================================================

def parse_season_year(home_soup: BeautifulSoup) -> Optional[int]:
    """
    Read the championship year from the home page calendar heading, which renders
    as e.g. "2026 CALENDAR". Falls back to None if it cannot be found.
    """
    super_title = home_soup.select_one(".super-title")
    if super_title:
        year = first_int(clean_text(super_title))
        # Sanity-check the value looks like a plausible season year.
        if year and 2000 <= year <= 2100:
            return year
    return None


# =============================================================================
# 5b. PARSER: ROSTER URL (auto-discovered from the site navigation menu)
# =============================================================================

def parse_roster_url(home_soup: BeautifulSoup, season: Optional[int] = None) -> Optional[str]:
    """
    Find the "Teams & Drivers" roster page URL from the site's navigation menu,
    so the script keeps working when the roster slug changes each season
    (e.g. /f4-teams-2026/ -> /f4-teams-2027/) without any code edit.

    Strategy, most reliable first:
      1. A menu link whose href points at a "f4-teams" page.
      2. A menu link whose visible text mentions both "team" and "driver".
      3. Fall back to building the URL from the season year, then to the
         module-level ROSTER_URL constant.
    """
    # 1. href-based match (the slug always contains "f4-teams").
    for anchor in home_soup.find_all("a", href=True):
        if "f4-teams" in anchor["href"].lower():
            return anchor["href"]

    # 2. link-text based match ("Teams & Drivers", "Teams and Drivers", ...).
    for anchor in home_soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True).lower()
        if "team" in text and "driver" in text:
            return anchor["href"]

    # 3. fall back to a season-derived URL, then the hard-coded default.
    if season:
        return f"{BASE_URL}/f4-teams-{season}/"
    return ROSTER_URL


# =============================================================================
# 6. PARSER: CALENDAR (list of rounds, from the home page)
# =============================================================================

def parse_calendar(home_soup: BeautifulSoup) -> list:
    """
    Extract every round from the home page's round-list block.

    Each round card contains:
      * a link to the round detail page,   -> a[href*="/round/"]
      * the round number,                  -> h3   ("Round 4")
      * the circuit name,                  -> .card-body h4   ("Jarama")
      * the date range,                    -> .date   ("22-23 August")
      * the host-country flag.             -> .flag  (class "fi-es" etc.)
    Returns a list of partially-filled Round objects (dates not yet computed).
    """
    block = home_soup.select_one('[class*="lazyblock-round-list"]')
    if block is None:
        return []

    rounds = []
    for card in block.select("li .round-card"):
        link = card.select_one('a[href*="/round/"]')
        number_el = card.select_one("h3")
        circuit_el = card.select_one(".card-body h4")
        date_el = card.select_one(".date")
        flag_el = card.select_one(".card-body .flag") or card.select_one(".flag")

        code = flag_code_from(flag_el)
        rounds.append(
            Round(
                round_number=first_int(clean_text(number_el)),
                circuit=clean_text(circuit_el),
                official_circuit_name=None,
                country_code=code,
                country=country_name(code),
                date_text=clean_text(date_el),
                start_date=None,
                end_date=None,
                start_datetime=None,
                season=None,
                url=link["href"] if link and link.has_attr("href") else None,
                is_upcoming=None,
            )
        )
    return rounds


# =============================================================================
# 7. PARSER: ROUND DETAIL PAGE (confirmed year + precise start time)
# =============================================================================

def parse_round_detail(round_soup: BeautifulSoup, season: Optional[int] = None) -> dict:
    """
    Extract enrichment fields from a single round detail page:
      * official_circuit_name  (the full circuit title in the <h1>)
      * season                 (the year shown next to the title)
      * start_datetime         (ISO timestamp from the countdown, upcoming rounds
                                only; past rounds have no countdown)
      * has_countdown          (True when the page shows a 'Round starting in' timer)
      * sessions               (the per-session timetable: free practice,
                                qualifying, races -- see parse_round_schedule)
    Any field that cannot be found is simply omitted.

    `season` (if known from the calendar) is used to turn each session's
    "Friday June 19" heading into a full ISO date.
    """
    detail = {}

    heading = round_soup.select_one(".round-titles h1")
    if heading:
        # The <h1> holds a flag <div>, the circuit text, and a <span> with the year.
        year_span = heading.select_one("span")
        if year_span:
            detail["season"] = first_int(clean_text(year_span))

        # Circuit name = the heading text with the year span and flag removed.
        heading_copy = BeautifulSoup(str(heading), "lxml")
        for stripped in heading_copy.select("span, .flag"):
            stripped.extract()
        name = clean_text(heading_copy)
        if name:
            # Some upcoming pages append " - RACE"; drop that decorative suffix.
            detail["official_circuit_name"] = re.sub(r"\s*-\s*RACE\s*$", "", name).strip()

    # The countdown widget stores the exact start moment in its attribute:
    #   uk-countdown="date: 2026-08-22T00:00:00+00:00"
    countdown = round_soup.select_one("[uk-countdown]")
    if countdown:
        match = re.search(r"date:\s*([0-9T:+\-]+)", countdown.get("uk-countdown", ""))
        if match:
            detail["start_datetime"] = match.group(1)
            detail["has_countdown"] = True

    # The per-session timetable. Use the caller's season when known, otherwise
    # the year parsed from this page's title, so session dates get the right year.
    season_for_dates = season or detail.get("season")
    detail["sessions"] = parse_round_schedule(round_soup, season_for_dates)

    return detail


def parse_round_schedule(round_soup: BeautifulSoup, season: Optional[int]) -> list:
    """
    Extract the session-by-session timetable from a round detail page.

    Structure on the page:
      section.round-schedule
        .sch-day                         -> one block per day
            p        -> "DAY 1"
            h3       -> "Friday June 19"  (weekday, month, day)
            ul li    -> one row per session:
                .time span   -> start time ("09:30")   [+ a chevron icon span]
                span         -> session name ("Free Practice", "Race 1", ...)

    Returns a list of Session objects (empty if the page has no published
    timetable yet -- some round pages are published before the schedule is set).
    """
    section = round_soup.select_one(".round-schedule")
    if section is None:
        return []

    sessions = []
    for day in section.select(".sch-day"):
        day_label = clean_text(day.find("p"))
        day_heading = clean_text(day.find("h3"))

        # Turn "Friday June 19" into an ISO date using the season year.
        iso_date = None
        if day_heading and season:
            match = re.search(r"([A-Za-z]+)\s+(\d{1,2})\s*$", day_heading)
            if match:
                month = MONTHS.get(match.group(1).lower())
                if month:
                    try:
                        iso_date = dt.date(season, month, int(match.group(2))).isoformat()
                    except ValueError:
                        iso_date = None

        for row in day.select("li"):
            # Start time = the first <span> inside the .time container.
            time_text = None
            time_container = row.select_one(".time")
            if time_container:
                time_span = time_container.find("span")
                time_text = clean_text(time_span)

            # Session name = the row's span(s) that are NOT the time and NOT the
            # decorative chevron icon; take the last such span.
            name = None
            name_spans = [
                span for span in row.find_all("span")
                if span.find_parent(class_="time") is None
                and "chevron-icon" not in span.get("class", [])
            ]
            if name_spans:
                name = clean_text(name_spans[-1])

            # Skip empty rows that carry neither a time nor a name.
            if not time_text and not name:
                continue

            datetime_local = (
                f"{iso_date}T{time_text}" if (iso_date and time_text) else None
            )
            sessions.append(
                Session(
                    day_label=day_label,
                    day_date_text=day_heading,
                    date=iso_date,
                    time=time_text,
                    datetime_local=datetime_local,
                    name=name,
                )
            )
    return sessions


# =============================================================================
# 8. PARSER: SEASON ROSTER (teams + drivers + nationalities)
# =============================================================================

def parse_roster(roster_soup: BeautifulSoup) -> list:
    """
    Extract every team and its drivers from the Teams & Drivers page.

    Structure on the page:
      .team-card
        .f4-team-details a[href*="/team/"]   -> team name + team URL
        .tscore                              -> championship points
        .driver-card-small-container         -> one per driver, each with:
            .number   -> car number
            .name p   -> driver full name
            .flag     -> nationality flag (class "fi-es" etc.)
            href      -> driver profile URL
    Returns a list of Team objects.
    """
    teams = []
    for card in roster_soup.select(".team-card"):
        # --- team name + url (strip out the logo image/markup inside the link) ---
        team_link = card.select_one('.f4-team-details a[href*="/team/"]')
        team_name = None
        team_url = None
        if team_link:
            team_url = team_link.get("href")
            link_copy = BeautifulSoup(str(team_link), "lxml")
            for stripped in link_copy.select("img, div"):
                stripped.extract()
            team_name = clean_text(link_copy)

        points = first_int(clean_text(card.select_one(".tscore")))

        # --- drivers ---
        drivers = []
        for dcard in card.select(".driver-card-small-container"):
            code = flag_code_from(dcard.select_one(".flag"))
            drivers.append(
                Driver(
                    number=clean_text(dcard.select_one(".number")),
                    name=clean_text(dcard.select_one(".name p")) or "",
                    nationality_code=code,
                    nationality=country_name(code),
                    profile_url=dcard.get("href"),
                )
            )

        teams.append(
            Team(name=team_name or "", points=points, profile_url=team_url, drivers=drivers)
        )
    return teams


# =============================================================================
# 9. DATE HANDLING (turn "22-23 August" + season year into real dates)
# =============================================================================

def parse_date_range(date_text: Optional[str], season: Optional[int]) -> tuple:
    """
    Convert a human date range plus the season year into (start_date, end_date)
    as ISO strings ("2026-08-22", "2026-08-23").

    Handles the two shapes the site uses:
      * "22-23 August"          (same month)
      * "30 May - 1 June"       (spanning two months; handled defensively)
    Returns (None, None) if it cannot be parsed or the year is unknown.
    """
    if not date_text or not season:
        return None, None

    text = date_text.strip()

    # Shape A: "D-D Month"  e.g. "22-23 August"
    match = re.match(r"^\s*(\d{1,2})\s*[-–]\s*(\d{1,2})\s+([A-Za-z]+)\s*$", text)
    if match:
        start_day, end_day, month_name = match.groups()
        month = MONTHS.get(month_name.lower())
        if month:
            try:
                start = dt.date(season, month, int(start_day))
                end = dt.date(season, month, int(end_day))
                return start.isoformat(), end.isoformat()
            except ValueError:
                return None, None

    # Shape B: "D Month - D Month"  e.g. "30 May - 1 June"
    match = re.match(
        r"^\s*(\d{1,2})\s+([A-Za-z]+)\s*[-–]\s*(\d{1,2})\s+([A-Za-z]+)\s*$", text
    )
    if match:
        start_day, start_month_name, end_day, end_month_name = match.groups()
        start_month = MONTHS.get(start_month_name.lower())
        end_month = MONTHS.get(end_month_name.lower())
        if start_month and end_month:
            try:
                start = dt.date(season, start_month, int(start_day))
                end = dt.date(season, end_month, int(end_day))
                return start.isoformat(), end.isoformat()
            except ValueError:
                return None, None

    # Shape C: single day "D Month"
    match = re.match(r"^\s*(\d{1,2})\s+([A-Za-z]+)\s*$", text)
    if match:
        day, month_name = match.groups()
        month = MONTHS.get(month_name.lower())
        if month:
            try:
                single = dt.date(season, month, int(day))
                return single.isoformat(), single.isoformat()
            except ValueError:
                return None, None

    return None, None


def is_round_upcoming(round_obj: Round, today: dt.date) -> Optional[bool]:
    """
    A round counts as "upcoming" if its race weekend has NOT finished yet, i.e.
    its end date is today or later. If we could not compute an end date, we fall
    back to None (unknown) so nothing is silently mislabeled.
    """
    if round_obj.end_date is None:
        return None
    end = dt.date.fromisoformat(round_obj.end_date)
    return end >= today


# =============================================================================
# 10. ASSEMBLY (tie the pieces together)
# =============================================================================

def build_dataset(
    home_html: str,
    roster_html: str,
    round_html_by_url: dict,
    today: dt.date,
    season_override: Optional[int] = None,
    collector: str = DEFAULT_COLLECTOR,
) -> dict:
    """
    Given the raw HTML of the home page, the roster page, and (optionally) each
    round detail page, produce the final structured dataset.

    `round_html_by_url` maps a round URL -> its HTML (may be empty; round detail
    enrichment is optional).
    """
    home_soup = make_soup(home_html)
    roster_soup = make_soup(roster_html)

    # --- season year ---
    season = season_override or parse_season_year(home_soup)

    # Roster page URL (for the borrowed source_url lineage field on roster rows).
    roster_source_url = parse_roster_url(home_soup, season) or ROSTER_URL

    # --- calendar ---
    rounds = parse_calendar(home_soup)
    for r in rounds:
        r.season = season
        r.start_date, r.end_date = parse_date_range(r.date_text, season)

        # Optional enrichment from the round's own page (adds the full circuit
        # name, the precise start timestamp, and the per-session timetable).
        detail_html = round_html_by_url.get(r.url or "")
        if detail_html:
            detail = parse_round_detail(make_soup(detail_html), season=r.season)
            r.official_circuit_name = detail.get("official_circuit_name")
            r.start_datetime = detail.get("start_datetime")
            r.sessions = [asdict(s) for s in detail.get("sessions", [])]
            # Prefer the year stated on the round page if the home page lacked one.
            if r.season is None and detail.get("season"):
                r.season = detail["season"]
                r.start_date, r.end_date = parse_date_range(r.date_text, r.season)

        # We still compute this flag for reference, but every round is reported.
        r.is_upcoming = is_round_upcoming(r, today)

    # --- season roster ---
    teams = parse_roster(roster_soup)
    roster_dict = {
        "team_count": len(teams),
        "driver_count": sum(len(t.drivers) for t in teams),
        "teams": [asdict(t) for t in teams],
    }

    # --- attach the season roster to EVERY round ---
    # The site publishes one season-wide roster, so the same set of teams/drivers
    # is attached to each round in the calendar.
    rounds_out = []
    for r in rounds:
        entry = asdict(r)
        entry["roster"] = roster_dict  # same season-wide roster for every round
        rounds_out.append(entry)

    upcoming_count = sum(1 for r in rounds if r.is_upcoming)

    # Lineage timestamp for the contract: UTC "now" in the Z form (§3.4).
    scraped_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "source": HOME_URL,
        "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "as_of_date": today.isoformat(),
        "season": season,
        # Contract lineage/context fields (used by the contract schedule writer):
        "season_label": str(season) if season else None,  # single-year season -> "2026"
        "series_id": CONTRACT_SERIES_ID,
        "scraped_at": scraped_at,
        "collector": collector,
        "roster_source_url": roster_source_url,
        "round_count": len(rounds_out),
        "upcoming_round_count": upcoming_count,  # how many of those are still ahead
        "rounds": rounds_out,                    # ALL rounds, each with the roster
        "season_roster": roster_dict,
    }


# =============================================================================
# 11. INPUT ACQUISITION (live network OR a folder of saved HTML)
# =============================================================================

def gather_live(session: requests.Session) -> tuple:
    """
    Fetch the home page, then every round detail page it links to, then the
    roster page. Returns (home_html, roster_html, {round_url: round_html}).
    """
    print(f"Fetching home page: {HOME_URL}")
    home_html = fetch_html(session, HOME_URL)

    # Discover round URLs from the calendar so we enrich exactly the real rounds.
    home_soup = make_soup(home_html)
    season = parse_season_year(home_soup)
    round_urls = [r.url for r in parse_calendar(home_soup) if r.url]

    round_html_by_url = {}
    for url in round_urls:
        print(f"Fetching round page: {url}")
        try:
            round_html_by_url[url] = fetch_html(session, url)
        except RuntimeError as error:
            # Enrichment is optional; keep going if one round page is unavailable.
            print(f"  [warn] skipping round enrichment for {url}: {error}",
                  file=sys.stderr)

    # Auto-discover the roster URL from the navigation menu so the script keeps
    # working across seasons (the roster slug changes each year).
    roster_url = parse_roster_url(home_soup, season) or ROSTER_URL
    print(f"Fetching roster page: {roster_url}")
    roster_html = fetch_html(session, roster_url)

    return home_html, roster_html, round_html_by_url


def read_file(path: str) -> str:
    """Read a local HTML/txt file as UTF-8 text."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def gather_local(local_dir: str) -> tuple:
    """
    Load HTML from a folder of saved files (useful for offline auditing/testing).

    Files are matched loosely by name:
      * home page   -> a file whose name contains "home"
      * roster page -> a file whose name contains "team"
      * round pages -> every other *.txt / *.html file
    Round pages are keyed by the canonical round URL found inside each file, so
    they line up with the URLs parsed from the home page.
    """
    files = [
        os.path.join(local_dir, name)
        for name in os.listdir(local_dir)
        if name.lower().endswith((".txt", ".html", ".htm"))
    ]

    home_html = None
    roster_html = None
    round_files = []

    for path in files:
        base = os.path.basename(path).lower()
        if "home" in base:
            home_html = read_file(path)
        elif "team" in base or "driver" in base:
            roster_html = read_file(path)
        else:
            round_files.append(path)

    if home_html is None:
        raise RuntimeError(f"No home page file (name containing 'home') found in {local_dir}")
    if roster_html is None:
        raise RuntimeError(f"No roster file (name containing 'team'/'driver') found in {local_dir}")

    # Key each round file by the canonical round URL it declares, so it matches
    # the URLs discovered on the home page.
    round_html_by_url = {}
    for path in round_files:
        html = read_file(path)
        match = re.search(r'rel="canonical" href="([^"]*?/round/[^"]*?)"', html)
        if match:
            round_html_by_url[match.group(1)] = html
        else:
            print(f"  [warn] no /round/ canonical URL in {path}; ignoring for enrichment",
                  file=sys.stderr)

    return home_html, roster_html, round_html_by_url


# =============================================================================
# 12. OUTPUT WRITERS
# =============================================================================

def write_json(dataset: dict, outdir: str) -> str:
    path = os.path.join(outdir, "f4spain_data.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(dataset, handle, ensure_ascii=False, indent=2)
    return path


def build_schedule_rows(dataset: dict) -> tuple:
    """
    Turn the scraped rounds into contract-compliant schedule rows (§7).

    Returns (rows, warnings):
      * rows      -- list of dicts, one per planned session, in the exact §7
                     column order.
      * warnings  -- human-readable notes about anything skipped (unresolved
                     circuits, unrecognized session labels, sessions missing a
                     usable local time). Surfaced to the collector, never silent.

    Contract rules applied here:
      * session_type is `practice` | `qualifying` | `race` (classify_session).
      * "Official Test" is excluded (per the requester).
      * session_number is the explicit number in the label ("Race 2" -> 2) or,
        when the label has none ("Free Practice"), a running count within the
        (round, session_type) in time order.
      * circuit_id comes from dim_circuits (resolve_circuit); a round whose
        circuit is not in the bundled dim is skipped with a warning (§10).
      * session_datetime_local is local time WITH the venue offset (§3.4).
      * A round with no published timetable contributes no rows (a schedule row
        cannot exist without a session).
    """
    season_label = dataset.get("season_label")
    scraped_at = dataset.get("scraped_at")
    collector = dataset.get("collector") or DEFAULT_COLLECTOR
    series_id = dataset.get("series_id", CONTRACT_SERIES_ID)

    rows = []
    warnings = []

    for r in dataset["rounds"]:
        sessions = r.get("sessions") or []
        if not sessions:
            continue  # no timetable published yet -> no schedule rows for this round

        circuit = resolve_circuit(r.get("circuit"))
        if circuit is None:
            warnings.append(
                f"Round {r.get('round_number')} ({r.get('circuit')!r}): circuit not "
                f"found in dim_circuits (CIRCUIT_BY_NAME) -- rows skipped. Escalate "
                f"to Berkay to add it (contract §10)."
            )
            continue

        type_counter = {"practice": 0, "qualifying": 0, "race": 0}
        for s in sessions:
            raw_name = s.get("name") or ""
            session_type = classify_session(raw_name)
            if session_type is None:
                # Intentionally-excluded "Official Test" is expected; warn only
                # for genuinely unrecognized labels so nothing slips through.
                if "test" not in raw_name.lower():
                    warnings.append(
                        f"Round {r.get('round_number')}: unrecognized session "
                        f"{raw_name!r} -- skipped (no contract session_type)."
                    )
                continue

            local_ts = local_timestamp_with_offset(
                s.get("date"), s.get("time"), circuit["tz"]
            )
            if local_ts is None:
                warnings.append(
                    f"Round {r.get('round_number')}: session {raw_name!r} has no "
                    f"usable date/time -- skipped (session_datetime_local required)."
                )
                continue

            # session_number: explicit trailing integer in the label, else the
            # running count within this (round, type).
            explicit = re.search(r"(\d+)\s*$", raw_name)
            type_counter[session_type] += 1
            session_number = int(explicit.group(1)) if explicit else type_counter[session_type]

            rows.append({
                "series_id": series_id,
                "season_label": season_label,
                "round_number": r.get("round_number"),
                "session_type": session_type,
                "session_number": session_number,
                "circuit_id": circuit["circuit_id"],
                "session_datetime_local": local_ts,
                "sport": CONTRACT_SPORT,
                "discipline": CONTRACT_DISCIPLINE,
                "category": CONTRACT_CATEGORY,
                "planned_duration_minutes": "",   # not published on the site (optional)
                "source_url": r.get("url"),
                "source_collector": collector,
                "scraped_at": scraped_at,
            })

    return rows, warnings


def write_schedule_csv(dataset: dict, outdir: str) -> str:
    """
    Write the CONTRACT-COMPLIANT schedule file (Race Results Data Contract v2.2.1,
    §7). One row per planned session.

    File conforms to §1-§3:
      * Filename: <series_id>__<season_label>__schedule__<scraped_date>.csv
      * UTF-8, no BOM, Unix (LF) line endings, comma delimiter, single header.
      * Exact §7 column names and order.
    """
    # §7 canonical column order -- do not reorder.
    columns = [
        "series_id", "season_label", "round_number", "session_type",
        "session_number", "circuit_id", "session_datetime_local", "sport",
        "discipline", "category", "planned_duration_minutes", "source_url",
        "source_collector", "scraped_at",
    ]

    rows, warnings = build_schedule_rows(dataset)

    # Contract filename (§1.1). scraped_date is the UTC date of the scrape.
    scraped_date = (dataset.get("scraped_at") or "")[:10] or dataset.get("as_of_date")
    season_label = dataset.get("season_label") or "unknown"
    series_id = dataset.get("series_id", CONTRACT_SERIES_ID)
    filename = f"{series_id}__{season_label}__schedule__{scraped_date}.csv"
    path = os.path.join(outdir, filename)

    # newline="" + lineterminator="\n" => Unix LF (§1.2); utf-8 default has no BOM.
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    if warnings:
        print("\n[schedule] contract notes (nothing written silently dropped):",
              file=sys.stderr)
        for note in warnings:
            print(f"  - {note}", file=sys.stderr)

    return path


def _safe_normalize(func, value):
    """Run a normalizer, but never crash the whole file on odd input."""
    try:
        return func(value)
    except (ValueError, TypeError):
        return ""


def write_roster_csv(dataset: dict, outdir: str) -> str:
    """
    One row per driver in the season roster.

    NON-CONTRACT FILE. The Race Results Data Contract defines only `results` and
    `schedule` file types; an entry-list/roster (teams + drivers + nationalities,
    with no session results) fits neither. This file is provided for reference
    only and will NOT pass race-validator or be ingested.

    HOWEVER, per the requester, every field that has a contract counterpart uses
    the contract's own convention (see BORROWED_FIELDS at the foot of this file):
      * team_name_raw / team_name_normalized     (normalize_identifier)
      * driver_full_name_raw / _normalized       (normalize_name)
      * car_number                               (INT)
      * nationality_code                         (alpha-3, §5.4)
      * source_url / source_collector / scraped_at (lineage, §5.6/§3.4)
      * text cleaned per §3.5; booleans per §3.3.
    """
    path = os.path.join(outdir, "roster_NONCONTRACT.csv")
    columns = [
        "team_name_raw", "team_name_normalized", "team_points",
        "car_number", "driver_full_name_raw", "driver_full_name_normalized",
        "nationality_code", "nationality_name", "driver_profile_url",
        "source_url", "source_collector", "scraped_at",
    ]
    source_url = dataset.get("roster_source_url") or ROSTER_URL
    collector = dataset.get("collector") or DEFAULT_COLLECTOR
    scraped_at = dataset.get("scraped_at")

    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for team in dataset["season_roster"]["teams"]:
            team_raw = clean_raw_text(team["name"]) or ""
            team_norm = _safe_normalize(normalize_identifier, team_raw)
            for driver in team["drivers"]:
                name_raw = clean_raw_text(driver["name"]) or ""
                writer.writerow({
                    "team_name_raw": team_raw,
                    "team_name_normalized": team_norm,
                    "team_points": int_or_empty(team["points"]),
                    "car_number": int_or_empty(driver["number"]),
                    "driver_full_name_raw": name_raw,
                    "driver_full_name_normalized": _safe_normalize(normalize_name, name_raw),
                    "nationality_code": nationality_alpha3(driver["nationality_code"]),
                    "nationality_name": driver["nationality"] or "",
                    "driver_profile_url": driver["profile_url"] or "",
                    "source_url": source_url,
                    "source_collector": collector,
                    "scraped_at": scraped_at,
                })
    return path


def write_round_entries_csv(dataset: dict, outdir: str) -> str:
    """
    One row per (round x driver) -- the flat 'who races where' view, all rounds.

    NON-CONTRACT FILE (see write_roster_csv). Reference only; not ingestible.
    Borrows the same contract conventions, plus:
      * round_number                 (INT)
      * circuit_id                   (FK to dim_circuits, §5.1)
      * round_start_date/round_end_date (date-only YYYY-MM-DD, §3.4)
      * is_upcoming                  (TRUE/FALSE, §3.3)
    """
    path = os.path.join(outdir, "round_entries_NONCONTRACT.csv")
    columns = [
        "round_number", "circuit_id", "circuit_name_raw",
        "round_start_date", "round_end_date", "is_upcoming",
        "team_name_raw", "team_name_normalized", "car_number",
        "driver_full_name_raw", "driver_full_name_normalized",
        "nationality_code", "nationality_name",
        "source_url", "source_collector", "scraped_at",
    ]
    collector = dataset.get("collector") or DEFAULT_COLLECTOR
    scraped_at = dataset.get("scraped_at")

    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for r in dataset["rounds"]:
            circuit = resolve_circuit(r.get("circuit"))
            circuit_id = circuit["circuit_id"] if circuit else ""
            circuit_raw = clean_raw_text(r.get("circuit")) or ""
            for team in r["roster"]["teams"]:
                team_raw = clean_raw_text(team["name"]) or ""
                team_norm = _safe_normalize(normalize_identifier, team_raw)
                for driver in team["drivers"]:
                    name_raw = clean_raw_text(driver["name"]) or ""
                    writer.writerow({
                        "round_number": int_or_empty(r["round_number"]),
                        "circuit_id": circuit_id,
                        "circuit_name_raw": circuit_raw,
                        "round_start_date": r["start_date"] or "",
                        "round_end_date": r["end_date"] or "",
                        "is_upcoming": bool_to_contract(r["is_upcoming"]),
                        "team_name_raw": team_raw,
                        "team_name_normalized": team_norm,
                        "car_number": int_or_empty(driver["number"]),
                        "driver_full_name_raw": name_raw,
                        "driver_full_name_normalized": _safe_normalize(normalize_name, name_raw),
                        "nationality_code": nationality_alpha3(driver["nationality_code"]),
                        "nationality_name": driver["nationality"] or "",
                        "source_url": r["url"] or "",
                        "source_collector": collector,
                        "scraped_at": scraped_at,
                    })
    return path


# =============================================================================
# 13. HUMAN-READABLE SUMMARY (printed to the console)
# =============================================================================

def print_summary(dataset: dict) -> None:
    season = dataset["season"]
    print("\n" + "=" * 70)
    print(f"F4 SPAIN {season or ''} - ALL ROUNDS "
          f"({dataset['upcoming_round_count']} still upcoming as of {dataset['as_of_date']})")
    print("=" * 70)

    if not dataset["rounds"]:
        print("No rounds found (the calendar may not be published yet).")
    for r in dataset["rounds"]:
        label = f"Round {r['round_number']}" if r["round_number"] else "Round"
        marker = "UPCOMING" if r["is_upcoming"] else "done" if r["is_upcoming"] is False else "?"
        print(f"\n{label}: {r['circuit']} ({r['country']})  [{marker}]")
        print(f"  Dates: {r['date_text']}  [{r['start_date']} -> {r['end_date']}]")
        if r.get("start_datetime"):
            print(f"  Starts: {r['start_datetime']}")
        print(f"  Page:  {r['url']}")
        sessions = r.get("sessions") or []
        if sessions:
            print("  Schedule:")
            current_day = None
            for s in sessions:
                if s.get("day_date_text") != current_day:
                    current_day = s.get("day_date_text")
                    print(f"    {current_day}")
                print(f"      {s.get('time') or '--:--':>5}  {s.get('name') or ''}")
        else:
            print("  Schedule: (not published yet)")

    roster = dataset["season_roster"]
    print("\n" + "-" * 70)
    print(f"SEASON ROSTER: {roster['team_count']} teams, "
          f"{roster['driver_count']} drivers (applies to every round above)")
    print("-" * 70)
    for team in roster["teams"]:
        pts = f" [{team['points']} pts]" if team["points"] is not None else ""
        print(f"\n{team['name']}{pts}")
        for d in team["drivers"]:
            num = f"#{d['number']}" if d["number"] else "  "
            print(f"   {num:>4}  {d['name']}  ({d['nationality']})")
    print()


# =============================================================================
# 14. COMMAND-LINE ENTRY POINT
# =============================================================================

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Scrape the F4 Spain schedule of all rounds and the "
                    "teams/drivers (with nationalities) taking part."
    )
    parser.add_argument(
        "--outdir", default="output",
        help="Directory for JSON/CSV output (default: ./output).",
    )
    parser.add_argument(
        "--local-dir", default=None,
        help="Parse saved HTML files from this folder instead of the network.",
    )
    parser.add_argument(
        "--season", type=int, default=None,
        help="Override the season year (default: detected from the home page).",
    )
    parser.add_argument(
        "--today", default=None,
        help="Override 'today' as YYYY-MM-DD (default: the real current date). "
             "Useful for testing the upcoming-round logic.",
    )
    parser.add_argument(
        "--collector", default=DEFAULT_COLLECTOR,
        help=f"source_collector identifier for the contract schedule file "
             f"(default: {DEFAULT_COLLECTOR}).",
    )
    parser.add_argument(
        "--no-files", action="store_true",
        help="Print the summary only; do not write JSON/CSV files.",
    )
    args = parser.parse_args(argv)

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()

    # --- acquire the raw HTML ---
    if args.local_dir:
        print(f"Reading saved HTML from: {args.local_dir}")
        home_html, roster_html, round_html_by_url = gather_local(args.local_dir)
    else:
        session = make_session()
        home_html, roster_html, round_html_by_url = gather_live(session)

    # --- build the structured dataset ---
    dataset = build_dataset(
        home_html=home_html,
        roster_html=roster_html,
        round_html_by_url=round_html_by_url,
        today=today,
        season_override=args.season,
        collector=args.collector,
    )

    # --- report ---
    print_summary(dataset)

    if not args.no_files:
        os.makedirs(args.outdir, exist_ok=True)
        written = [
            write_json(dataset, args.outdir),
            write_schedule_csv(dataset, args.outdir),
            write_roster_csv(dataset, args.outdir),
            write_round_entries_csv(dataset, args.outdir),
        ]
        print("Wrote:")
        for path in written:
            print(f"  {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
