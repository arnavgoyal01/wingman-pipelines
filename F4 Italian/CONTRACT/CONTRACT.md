# Race Results Data Contract — v2.2.1

> **Status:** Active
> **Library that enforces it:** `race_validator` v0.2.5
> **Last revised:** 2026-05-29

---

## What this document is

This is the single source of truth for how race-results CSVs must be structured.
Every scraper output that lands in the warehouse must conform. The
`race-validator` desktop app runs every rule in this document against your file
and reports violations.

Each section ends with a "Validator" subsection naming the rule IDs that
enforce it. If a file fails validation, the error message shows the rule ID
— look up that ID here to understand what to fix.

---

## Table of contents

1. [File-level rules](#1-file-level-rules)
2. [Column naming](#2-column-naming)
3. [Value conventions](#3-value-conventions)
4. [Normalization library (§3a)](#3a-normalization-library)
5. [Master tables](#4-master-tables)
6. [Schema — results file (§5)](#5-canonical-schema--results-file)
7. [Gap conversion rules (§6)](#6-gap-conversion-rules)
8. [Schema — schedule file (§7)](#7-canonical-schema--schedule-file)
9. [Forbidden patterns (§8)](#8-forbidden-patterns)
10. [Validation behavior (§9)](#9-validation-behavior)
11. [Collector workflow (§10)](#10-collector-workflow)
12. [Versioning (§11)](#11-versioning)
13. [Master table extension rules (§12)](#12-master-table-extension-rules)

---

## §1. File-level rules

### 1.1 File naming

```
<series_id>__<season_label>__<file_type>__<scraped_date>.csv
```

| Component | Rule | Example |
|---|---|---|
| `series_id` | The integer ID from `dim_series` | `142` |
| `season_label` | `YYYY` or `YYYY-YY` | `2025`, `2025-26` |
| `file_type` | Exactly `results` or `schedule` | `results` |
| `scraped_date` | UTC date the scrape ran | `2026-05-19` |

Fields are separated by **double underscores** (`__`). Single underscores
within fields are allowed (none of the standard fields need them, but the
filename parser tolerates them).

**Valid filenames:**
- `142__2025__results__2026-05-19.csv`
- `87__2025-26__results__2026-05-19.csv`
- `1__2024__schedule__2024-01-01.csv`

**Invalid filenames (and why):**

| Filename | Problem |
|---|---|
| `results.csv` | Missing all required fields |
| `142_2025_results_2026-05-19.csv` | Single underscores; need double |
| `142__2025__results__2026-05-19.CSV` | Uppercase extension; must be `.csv` |
| `142__2025__qualifying__2026-05-19.csv` | `qualifying` is not a valid file type |
| `142__2025__results__26-05-19.csv` | Date must be `YYYY-MM-DD` |
| `abc__2025__results__2026-05-19.csv` | `series_id` must be a positive integer |
| `142__25__results__2026-05-19.csv` | Year must be 4 digits |

**Validator:** `FILE_NAMING_001`, `FILE_NAMING_002`

### 1.2 Encoding & line format

| Requirement | Detail |
|---|---|
| Encoding | UTF-8 |
| Byte-order mark (BOM) | Not allowed |
| Line endings | Unix (`\n`) only — no `\r\n`, no `\r` |
| Delimiter | Comma (`,`) |
| Quoting | Double-quote (`"`) — used only when a field contains a comma |
| Header row | Exactly one |

**Why it matters:** Windows editors (Notepad, Excel "Save As CSV") default to
CRLF line endings and sometimes add BOMs. Re-save with "Save As → UTF-8 (no BOM)"
and choose "Unix (LF)" line endings.

**Validator:** `ENCODING_001`, `ENCODING_002`, `ENCODING_003`

### 1.3 Structural rules

| Rule | Detail |
|---|---|
| No fully blank rows | A row where every cell is empty is invalid |
| No "Unnamed" column headers | Pandas-default `Unnamed: 5` headers indicate a trailing comma in the header row |
| No empty column headers | Empty-string column names are invalid |
| No merged cells | (Not a problem for true CSVs — only happens if you export from Excel incorrectly) |

**Validator:** `STRUCTURE_001`, `STRUCTURE_002`

---

## §2. Column naming

### 2.1 Naming convention

- **snake_case only:** lowercase ASCII letters, digits, underscores
- No spaces, no parentheses, no periods, no slashes, no hyphens
- `driver_full_name_raw`, not `Driver Name`, not `driver-full-name`, not `Pos.`

### 2.2 Unit suffixes

When a numeric column carries a physical quantity, name it with a unit suffix:

| Suffix | Meaning | Example |
|---|---|---|
| `_ms` | Milliseconds (integer) | `race_time_ms` |
| `_m` | Metres (integer) | `circuit_length_m` |
| `_km` | Kilometres (float) | `circuit_length_km` (not used; we use `_m`) |
| `_kph` | Kilometres per hour | `best_lap_speed_kph` |
| `_seconds` | Seconds (float) | (not currently used) |

### 2.3 Boolean naming

Boolean columns must start with `is_`: `is_pole`, `is_fastest_lap_overall`,
`is_fastest_lap_in_class`.

### 2.4 Foreign-key naming

Foreign-key columns end with `_id`: `country_id`, `series_id`, `circuit_id`,
`driver_id`, `team_id`. Numeric IDs are integers; `country_id` and `region_id`
are 3-character codes.

### 2.5 The three-role text column convention

Every text field used for entity matching has up to three companions:

| Suffix | Where it appears | What it holds |
|---|---|---|
| `*_raw` | Result/schedule rows | Exactly what the source page said. Preserves capitalization and diacritics. Used for audit. Never appears in dim tables. |
| `*_display` | Dim tables | The canonical, correct, human-readable form (with diacritics). Used for charts, reports, UIs. Never appears in result rows. |
| `*_normalized` | Both dim tables and result rows | Lowercase ASCII, no diacritics. Used for matching and joins. |

**Example: a driver row**

```
driver_full_name_raw         driver_full_name_normalized
Nicolás Varrone              nicolas varrone
```

The `dim_drivers` table also has `full_name_display` (with diacritics) but
this column does NOT appear on result rows.

### 2.6 Column order

The column order in your CSV file must match the schema **exactly**. Reordering
columns — even if names are correct — is a validation error.

**Validator:** `COLUMN_NAMES_001`, `COLUMN_NAMES_002`

---

## §3. Value conventions

### 3.1 Missing values

Missing or unknown values are represented by **empty string** between two
commas (`,,`).

**Forbidden:** `"N/A"`, `"null"`, `"None"`, `"-"`, `"#N/A"`, `"NaN"`, `"DNF"`
(use `race_status` for DNF), `"unknown"`.

### 3.2 Numbers

**Integers:**
- Plain digits, optional leading minus: `17`, `-3`
- No trailing decimal: `17.0` is invalid
- No unit text inside: `"17 laps"` is invalid (use `laps_completed = 17` and `gap_to_leader_display = "17 Laps"` if you need the text)
- No thousands separators: `1,800,000` is invalid; use `1800000`

**Floats:**
- Period as decimal separator: `5.891`
- No thousands separators: `5,891.0` is invalid; use `5891.0` or `5891`
- Scientific notation accepted: `1.5e3`

### 3.3 Booleans

Exactly `TRUE` or `FALSE` (uppercase). Never `Yes`/`No`, `True`/`False`,
`1`/`0`, `Y`/`N`, `T`/`F`, or empty (booleans have no NULL state).

### 3.4 Dates and timestamps

There are **two distinct timestamp shapes** in the contract; they are not
interchangeable.

#### Session timestamps (local time with offset)

Used for: `session_datetime_local` and other venue-anchored times.

Format: ISO 8601 with timezone offset, NOT `Z`.

**Valid:**
- `2025-12-13 13:00:00+08:00`
- `2025-12-13T13:00:00+08:00`
- `2025-06-08 14:30:00-04:00`

**Invalid:**
- `2025-12-13 13:00:00` (missing offset, naive datetime)
- `2025-12-13T13:00:00Z` (UTC `Z` used where local was expected)
- `12 Dec 2025 13:30` (human-readable form)
- `13:00 13/12/2025` (wrong order, wrong separators)

**Why local with offset?** Downstream services (weather, daylight, timezone)
need to know the actual local time AND the offset. UTC alone loses the
venue context.

#### Lineage timestamps (UTC with Z)

Used for: `scraped_at`, `ingested_at`, and other machine-event times.

Format: ISO 8601 with `Z` suffix, strict `T` separator.

**Valid:**
- `2026-05-19T08:00:00Z`
- `2026-05-19T08:00:00.123Z`

**Invalid:**
- `2026-05-19 08:00:00+00:00` (use `Z` here, not the offset form)
- `2026-05-19T08:00:00` (no offset, no Z)

#### Date-only fields

Format: `YYYY-MM-DD`.

**Valid:** `2025-06-08`, `2026-01-15`
**Invalid:** `8 June 2025`, `06/08/2025`, `2025-6-8`

### 3.5 Text values

- No leading whitespace, no trailing whitespace
- No double spaces inside the text
- Diacritics preserved in `*_raw` and `*_display` columns: `Nicolás`,
  `Sørensen`, `Müller`, `è` are correct and should NOT be stripped
- Diacritics stripped in `*_normalized` columns — but use the canonical
  normalizer (§3a), never roll your own

**Forbidden characters anywhere in text:**
- Control characters (anything below U+0020 except space)
- Tab (`\t`)
- Newline (`\n`), carriage return (`\r`)
- Double quote (`"`)
- Backslash (`\`)
- Forward slash (`/`) — except in `source_url`
- Pipe (`|`)
- NULL byte (`\0`)

If a source page contains any of these in a name, strip them in the
scraper before output.

### 3.6 String columns with embedded units

Some `_display` columns deliberately preserve scraper-visible text that may
contain units. These are exempt from the "no units in values" rule:

- `gap_to_leader_display`: `"1 Lap"`, `"+1:05.721"`, `"25 Laps"`
- `interval_to_ahead_display`: same
- `car_model_raw`: `"Oreca 07 - Gibson"`

The corresponding `_ms` or `_normalized` columns ARE strict about format.

**Validator:** `VALUE_TYPE_001/002/003`, `TIMESTAMP_001/002`, `FORBIDDEN_CHARS_001`,
`WHITESPACE_001`

---

## §3a. Normalization library

Every `_normalized` column in the contract is produced by one of two canonical
functions. Collectors must use these — ad-hoc normalization will fail validation.

### 3a.1 `normalize_name(s)` — for person names

**Used for:** `driver_full_name_normalized` and any future person-name column.

**Output:** lowercase ASCII, single-spaced, no diacritics.

**Algorithm:**
1. Apply pre-mapping for characters NFKD doesn't decompose:
   `ø→o`, `Ø→O`, `æ→ae`, `œ→oe`, `ß→ss`, `ð→d`, `þ→th`, `ł→l`, `đ→d`, `ı→i`, `ŋ→n`
2. Unicode NFKD decompose
3. Remove all combining marks (Unicode category `Mn`)
4. Replace any non-`[A-Za-z0-9 ]` character with a single space
5. Lowercase
6. Collapse consecutive whitespace to a single space
7. Strip leading/trailing whitespace

**Examples:**

| Input | Output |
|---|---|
| `Nicolás Varrone` | `nicolas varrone` |
| `José María López` | `jose maria lopez` |
| `Théo Pourchaire` | `theo pourchaire` |
| `Søren Sørensen` | `soren sorensen` |
| `Müller, Nico` | `muller nico` |
| `Jean-Éric Vergne` | `jean eric vergne` |
| `  Lewis   Hamilton  ` | `lewis hamilton` |
| `Tadasuke Makino (牧野 任祐)` | `tadasuke makino` |

### 3a.2 `normalize_identifier(s)` — for entity identifiers

**Used for:** `team_name_normalized`, `car_model_normalized`,
`series_name_normalized`, `circuit_full_name_normalized`, and `sport`,
`discipline`, `category` taxonomy values.

**Output:** lowercase ASCII with underscores in place of any non-alphanumeric run.

**Algorithm:**
1. Apply the same pre-mapping (`ø→o`, etc.)
2. Unicode NFKD decompose
3. Remove combining marks
4. Replace any non-`[A-Za-z0-9]` run with a single `_`
5. Lowercase
6. Strip leading/trailing `_`

**Examples:**

| Input | Output |
|---|---|
| `Formula 1` | `formula_1` |
| `Autódromo José Carlos Pace` | `autodromo_jose_carlos_pace` |
| `Brands Hatch (GP)` | `brands_hatch_gp` |
| `Prema Racing` | `prema_racing` |
| `Oreca 07 - Gibson` | `oreca_07_gibson` |
| `Ligier JS P320 / Nissan` | `ligier_js_p320_nissan` |
| `Porsche 911 GT3 R` | `porsche_911_gt3_r` |
| `BMW M Hybrid V8` | `bmw_m_hybrid_v8` |
| `2024-25 Asian Le Mans` | `2024_25_asian_le_mans` |
| `___Test___` | `test` |

### 3a.3 Determinism rules

- Both functions are **pure**: same input always produces the same output.
- Both are **idempotent**: `f(f(x)) == f(x)`.
- Empty/whitespace-only input raises `ValueError` — never silently produces an empty string.
- Inputs longer than 200 characters raise `ValueError` — names are shorter than this.
- Both functions live in `race_validator/normalize.py`. Import them; do not re-implement them.

### 3a.4 Which normalizer for which column

| Field | Function |
|---|---|
| `driver_full_name_normalized` | `normalize_name` |
| `team_name_normalized` | `normalize_identifier` |
| `series_name_normalized` | `normalize_identifier` |
| `circuit_full_name_normalized` | `normalize_identifier` |
| `car_model_normalized` | `normalize_identifier` |
| `discipline` (in dim_categories and on result rows) | `normalize_identifier` |
| `category` (in dim_categories and on result rows) | `normalize_identifier` |
| `sport` (in dim_categories and on result rows) | `normalize_identifier` |

**Validator:** `NORMALIZATION_001`

---

## §4. Master tables

These reference tables are bundled inside the validator library and updated
via library releases.

### 4.1 `dim_countries`

ISO 3166-1 alpha-3 country codes. ~249 rows.

```
country_id (CHAR(3), PK)    country_name
USA                          United States
GBR                          United Kingdom
ARG                          Argentina
```

Use `country_id` for `nationality_code`, `country_id` on dim tables, and the
country part of any address.

### 4.2 `dim_regions`

Custom 3-letter continental codes (ISO has no standard for regions). 9 rows.

| region_id | region_name | When to use |
|---|---|---|
| `EUR` | Europe | Series confined to European countries |
| `MID` | Middle East | Saudi, UAE, Bahrain, Qatar, etc. |
| `ASI` | Asia (excl. Middle East) | Japan, China, Singapore, etc. |
| `OCE` | Oceania | Australia, NZ, Pacific |
| `AFR` | Africa | |
| `NAM` | North America | USA, Canada, Mexico |
| `SAM` | South America | Argentina, Brazil, Chile, etc. |
| `AMR` | Americas (NAM + SAM) | When a series spans both, or context unspecified |
| `INT` | International / global | Multi-continent: WEC, F1, etc. |

Pick the **most specific applicable code**.

### 4.3 `dim_categories`

Discipline/category taxonomy. Composite key `(sport, discipline, category)`.

```
sport       discipline       category
motorsport  single_seater    formula_4
motorsport  single_seater    formula_3
motorsport  endurance        lmp2
motorsport  endurance        lmgt3
```

All three values are already in `_normalized` form (lowercase, underscored).
No display variants needed — these are stable taxonomy strings.

### 4.4 `dim_series`

One row per championship. Columns:

```
series_id, series_name_display, series_name_normalized,
scope, country_id, region_id
```

**Scope-consistency rule:**

| `scope` | `country_id` | `region_id` |
|---|---|---|
| `club` | populated | empty |
| `national` | populated | empty |
| `regional` | empty | populated |
| `international` | empty | populated |

### 4.5 `dim_circuits`

One row per circuit layout (Brands Hatch GP and Brands Hatch Indy are
separate rows).

```
circuit_id, circuit_full_name_display, circuit_full_name_normalized,
city, country_id, latitude_dd, longitude_dd, circuit_length_m, layout
```

Coordinates are required (used for weather enrichment). `circuit_length_m` is
in whole metres (`5891`, not `5.891`).

### 4.6 What collectors look up where

| ID in your CSV | Looked up against |
|---|---|
| `country_id`, `nationality_code` | `dim_countries.country_id` |
| `region_id` | `dim_regions.region_id` |
| `series_id` | `dim_series.series_id` |
| `circuit_id` | `dim_circuits.circuit_id` |
| `(sport, discipline, category)` triple | `dim_categories` |

If a series, circuit, or category you need to scrape isn't in the bundled
dim, **stop and tell Berkay** — adding it requires a library release.

**Deferred to v0.3.0 (not enforced today):** `driver_id`, `team_id`, and
`car_model_normalized` will be looked up against BigQuery dim tables when
v0.3.0 ships. For v0.2.x, the collector fills these in as best they can
and the validator only checks format.

**Validator:** `MASTER_REF_001`, `MASTER_REF_002`

---

## §5. Canonical schema — `results` file

All `*_id` columns are required; NULL values fail validation.

### 5.1 Identity & context

| Column | Type | Required | Notes |
|---|---|---|---|
| `result_id` | STRING | yes | Surrogate primary key (assigned at DB level, but populated by scraper) |
| `series_id` | INT | yes | FK to `dim_series` |
| `season_label` | STRING | yes | `"2025"` or `"2025-26"` |
| `round_number` | INT | yes | Sequential round within the season |
| `session_type` | STRING | yes | `practice` \| `qualifying` \| `race` |
| `session_number` | INT | yes | `1`, `2`, `3`, ... within the session_type |
| `circuit_id` | INT | yes | FK to `dim_circuits` |
| `session_datetime_local` | TIMESTAMP_LOCAL | yes | ISO 8601 with offset (§3.4) |

### 5.2 Taxonomy

| Column | Type | Required | Notes |
|---|---|---|---|
| `sport` | STRING | yes | `motorsport` (currently the only value) |
| `discipline` | STRING | yes | From `dim_categories.discipline` |
| `category` | STRING | yes | From `dim_categories.category` |

### 5.3 Entry (car)

| Column | Type | Required | Notes |
|---|---|---|---|
| `entry_id` | STRING | yes | Unique per car per session |
| `car_number` | INT | yes | The visible race number |
| `team_id` | INT | yes | FK to `dim_teams` (lookup deferred to v0.3.0) |
| `team_name_raw` | STRING | yes | Exactly what the source said |
| `team_name_normalized` | STRING | yes | `normalize_identifier(team_name_raw)` |
| `car_model_raw` | STRING | no | What the source said: `Oreca 07 - Gibson` |
| `car_model_normalized` | STRING | no | `normalize_identifier(car_model_raw)` if non-empty |

### 5.4 Driver

| Column | Type | Required | Notes |
|---|---|---|---|
| `driver_id` | INT | yes | FK to `dim_drivers` (lookup deferred to v0.3.0) |
| `driver_full_name_raw` | STRING | yes | Source spelling with diacritics |
| `driver_full_name_normalized` | STRING | yes | `normalize_name(driver_full_name_raw)` |
| `driver_slot` | INT | yes | `1` for sprint races; `1..N` for endurance |
| `nationality_code` | CHAR(3) | yes | ISO 3166-1 alpha-3, as registered for THIS event |
| `driver_classification` | STRING | no | FIA: `Bronze`, `Silver`, `Gold`, `Platinum` |

### 5.5 Result

| Column | Type | Required | Notes |
|---|---|---|---|
| `race_status` | STRING | conditional | Required for race; empty for practice/qualifying |
| `grid_position` | INT | no | Starting grid position |
| `position_overall` | INT | no | Empty for DNS/DNQ |
| `position_in_class` | INT | no | |
| `laps_completed` | INT | yes | |
| `laps_down` | INT | no | `0` for lead-lap finishers; empty for non-race |
| `race_time_ms` | INT64 | conditional | See §5.7 |
| `gap_to_leader_ms` | INT64 | conditional | See §6 and §5.7 |
| `gap_to_leader_display` | STRING | no | Original gap value as-scraped |
| `interval_to_ahead_ms` | INT64 | no | Gap to the car directly ahead |
| `interval_to_ahead_display` | STRING | no | Original interval value |
| `best_lap_time_ms` | INT64 | no | |
| `best_lap_number` | INT | no | Which lap was the best |
| `best_lap_speed_kph` | FLOAT | no | |
| `is_pole` | BOOL | yes | See §5.8 |
| `is_fastest_lap_overall` | BOOL | yes | See §5.8 |
| `is_fastest_lap_in_class` | BOOL | yes | See §5.8 |

### 5.6 Lineage

Required on every row.

| Column | Type | Notes |
|---|---|---|
| `source_url` | STRING | Exact page scraped |
| `source_collector` | STRING | Collector identifier (e.g. your username) |
| `scraped_at` | TIMESTAMP_UTC | When the scrape ran, ISO 8601 with `Z` |
| `ingested_at` | TIMESTAMP_UTC | Populated by the pipeline; leave empty |

### 5.7 Cross-field NULL rules

**`race_status`:**
- Race rows: must be one of `FINISHED | LAPPED | DNF | DNS | DSQ | DNQ`
- Practice/qualifying rows: must be empty

**`race_time_ms`:**

| `session_type` | `race_status` | `race_time_ms` |
|---|---|---|
| `race` | `FINISHED` or `LAPPED` | **required** |
| `race` | `DNF`, `DNS`, `DSQ`, `DNQ` | **must be empty** |
| `practice` or `qualifying` | (empty) | **must be empty** |

**`gap_to_leader_ms`:**
- When `position_overall = 1`: **must be empty** (the leader has no gap to themselves)
- Otherwise: allowed but not required

### 5.8 Pole and fastest-lap rules

These apply to **race sessions only**. Group rows by
`(series_id, season_label, round_number, session_type='race', session_number)`.

**Pole (`is_pole`):**
- **Exactly one** row per race session has `is_pole = TRUE`
- That row's `race_status` cannot be `DNS` — the pole is whoever physically
  started P1, not the qualifying-fastest driver who didn't take the start

**Fastest lap overall (`is_fastest_lap_overall`):**
- **At most one** row per race session has this `= TRUE`
- Zero is allowed (rare — when no valid race lap was set)

**Fastest lap in class (`is_fastest_lap_in_class`):**
- **At most one** row per race session **per class** has this `= TRUE`
- Group by `(series, season, round, session_number, discipline, category)`
- For single-class series (F4, F3, etc.), this is the same row as
  `is_fastest_lap_overall`. Set both to TRUE on that row.
- For multi-class racing (Le Mans), each class gets its own row with this TRUE.

**Validator:** `CROSS_FIELD_001/002/003`, `POLE_001`, `FASTEST_001/002`,
`VALUE_TYPE_001/002/003`

---

## §6. Gap conversion rules

Race websites report gaps in many formats. Collectors convert each gap to
**milliseconds** for the `_ms` column AND preserve the original verbatim
in the `_display` column.

| Source format | Example | `_ms` value | `_display` value |
|---|---|---|---|
| Decimal seconds | `+0.328` | `328` | `+0.328` |
| Minutes:seconds | `+1:05.721` | `65721` | `+1:05.721` |
| Hours:minutes:seconds | `+1:29'09.907` | `5349907` | `+1:29'09.907` |
| "N Lap(s)" behind | `1 Lap` | N × session-leader's `best_lap_time_ms` | `1 Lap` |
| "N Lap(s)" behind | `25 Laps` | 25 × session-leader's `best_lap_time_ms` | `25 Laps` |
| Status string | `DNF`, `DNS`, `DSQ`, `DNQ` | empty (status goes in `race_status`) | empty |
| Empty (leader) | `""` | empty | empty |

**For lap-based gaps:** use the session **leader's** best lap time as the
multiplier, not the driver's own. The conversion is approximate; that's
expected.

**If the session leader's best lap isn't available:** leave `_ms` empty,
populate `_display` only. The pipeline can backfill later.

---

## §7. Canonical schema — `schedule` file

A schedule row is a planned session — no driver, no team, no result.
One row per session.

| Column | Type | Required | Notes |
|---|---|---|---|
| `series_id` | INT | yes | |
| `season_label` | STRING | yes | |
| `round_number` | INT | yes | |
| `session_type` | STRING | yes | `practice` \| `qualifying` \| `race` |
| `session_number` | INT | yes | |
| `circuit_id` | INT | yes | |
| `session_datetime_local` | TIMESTAMP_LOCAL | yes | |
| `sport` | STRING | yes | |
| `discipline` | STRING | yes | |
| `category` | STRING | yes | |
| `planned_duration_minutes` | INT | no | |
| `source_url` | STRING | yes | |
| `source_collector` | STRING | yes | |
| `scraped_at` | TIMESTAMP_UTC | yes | |

---

## §8. Forbidden patterns

Quick reference of things that always cause validation to fail:

- Mixed types in one column (`"16"` and `"12 laps"` in the same field)
- Status strings in numeric columns (`"DNS"` in `gap_to_leader_ms`)
- Unit suffixes in numeric columns (`"17 laps"` where INT expected)
  - Allowed in `*_display` and `*_raw` STRING columns
- Same physical concept stored in different columns by session type
- Duplicate rows (exact match on every column)
- Unnamed columns (`""` or `"Unnamed: N"`)
- Free-text date ranges (`"12 Dec 2025 13:30 - 15:00"`)
- Multiple rows for one session split by category — categories belong on
  result rows, not on separate session rows
- Timestamps without timezone offset
- Control characters, quotes, backslashes, pipes in any text field
- Missing `_normalized` companion for any text field used in matching
- `_normalized` value that doesn't equal the canonical normalizer's output
- `*_raw` columns appearing in dim tables
- `*_display` text columns appearing in result/schedule rows
  - Exceptions: `gap_to_leader_display`, `interval_to_ahead_display`
- Any NULL or unresolved `*_id` column on a result/schedule row
- Non-integer values in numeric `*_id` columns
- `dim_series` row violating the scope-consistency rule (§4.4)
- Result row's `(sport, discipline, category)` triple not in `dim_categories`
- Result row's `nationality_code` not in `dim_countries`
- Result row's non-null `car_model_normalized` not in `dim_car_models`
  (deferred to v0.3.0)
- Multiple `is_pole=TRUE` rows in one race session
- Zero `is_pole=TRUE` rows in a race session
- `is_pole=TRUE` on a `DNS` driver

---

## §9. Validation behavior

The validator runs in two stages.

### Stage 1 — Local validation (collector's machine)

What you run via the `race-validator` app or CLI.

For v0.2.x: hard-fails on any structural, format, value-level, normalization,
or cross-field rule violation. Reports all findings at once with row numbers,
rule IDs, and fix hints.

For v0.3.0 (planned): adds interactive entity resolution against BigQuery
(`dim_drivers`, `dim_teams`, `dim_car_models`). Stage 1 will pause and
prompt the collector to resolve unmatched drivers/teams before proceeding.

### Stage 2 — Server validation (planned)

When the upload pipeline lands, the server re-runs the same schema checks
on incoming files as a safety net. Identical rule set to Stage 1.

### Hard-fail principle

A file either passes completely or is rejected. No partial uploads. No
quarantine queue. If a single row violates a rule, the entire file is
rejected — fix it and re-validate.

This is intentional: race metrics (gaps, positions, classifications) are
computed across the full field. A missing or corrupt row would distort the
metrics for every other row in that session.

---

## §10. Collector workflow

1. **Before scraping:**
   - Confirm `series_id`, `season_label`, target `circuit_id`s, and target
     `(sport, discipline, category)` triples exist in the bundled dims.
   - If any are missing, request additions from Berkay before scraping.

2. **Write the scraper.** Output a CSV with source-as-scraped values
   in the columns defined by §5.

3. **Run the validator locally:**
   ```
   race-validator
   ```
   Drag the CSV into the app. Review every error and warning.

4. **Fix and re-validate** until the file passes.

5. **Upload** to the GCS bucket at:
   ```
   gs://<bucket>/raw/series=<series_id>/scraped_date=YYYY-MM-DD/<filename>.csv
   ```

6. **Wait for the ingest report.** Server-side validation will re-run
   (planned). On pass, the file is ingested into BigQuery. On fail, the
   file is rejected with the same kind of report.

**Collectors never edit CSVs by hand.** If a row is wrong, fix the scraper
and re-run; don't patch the output.

---

## §11. Versioning

- **Contract version:** `2.2.1`
- **Library version:** `0.2.5`

The library enforces a specific contract version. When the contract changes,
both versions bump and Berkay distributes a new library release. Collectors
upgrade with one `pip install --upgrade` command and the new rules become
active.

When you're stuck on an old library version, your scrapes will validate
against the old contract — which means they may not match what the warehouse
expects. Always run the most recent library version.

To check your installed version:
```
race-validator --version
```

---

## §12. Master table extension rules

### 12.1 Adding a new series, circuit, or category

These live in the bundled CSV reference files inside the library:
- `dim_countries.csv`
- `dim_regions.csv`
- `dim_categories.csv`
- `dim_series.csv`
- `dim_circuits.csv`

To add an entry: ping Berkay with the new row's data. Berkay updates the
CSV, ships a new library version, and notifies collectors to upgrade.

Collectors **do not edit the bundled CSVs directly** — those changes won't
make it into anyone else's environment.

### 12.2 Adding a new driver, team, or car model

Deferred to v0.3.0. In v0.2.x, the validator does NOT check that
`driver_id`/`team_id`/`car_model_normalized` exist in any dim — it only
checks that the value is correctly formatted.

In v0.3.0 the validator will:
1. Look up the driver/team/car against BigQuery dim tables
2. If not found, run fuzzy matching against existing entries
3. Prompt the collector to either link to an existing entity, add a new one,
   or escalate to Berkay if ambiguous

For now, just produce correctly-formatted driver/team/car columns and rely
on Berkay's manual review at ingest time.

### 12.3 Series definition

**One series = one championship with its own standings, calendar, and entry list.**

Quick test: does this thing have its own standings table at the end of the
season? If yes, it's a series. If it shares standings with another "version"
of itself in a different region, those are separate series.

**Split** brands with regional editions:
- Porsche Carrera Cup → Deutschland, France, Italia, GB, Asia, North America,
  Brasil, Japan, Australia, Scandinavia, Suisse, Benelux (each is its own row)
- Ferrari Challenge → Europe, North America, UK, Asia Pacific, Japan
- Lamborghini Super Trofeo → Europe, North America, Asia, Middle East

**Keep as one row** even though it visits multiple continents:
- FIA World Endurance Championship
- Intercontinental GT Challenge
- 24H Series
- Porsche Mobil 1 Supercup
- F1 Academy

The difference: WEC has one points table across all its races. Porsche
Carrera Cup Deutschland and Porsche Carrera Cup France have completely
separate standings — so they're separate series.

When in doubt, ask Berkay before adding.

---

## Quick reference card

If you're working from this contract day-to-day, the 90% answer to most
questions is:

1. **Column names and order**: match §5 (results) or §7 (schedule) exactly
2. **Numbers**: integers as `17`, floats as `5.891`, booleans as `TRUE`/`FALSE`
3. **Empty values**: empty string only, never `N/A` or `null`
4. **Session times**: `2025-12-13 13:00:00+08:00` (local + offset)
5. **Lineage times**: `2026-05-19T08:00:00Z` (UTC + Z)
6. **Names**: keep diacritics in `_raw` and `_display`; use
   `normalize_name()` or `normalize_identifier()` to compute `_normalized`
7. **IDs**: every `country_id`, `region_id`, `series_id`, `circuit_id`,
   `(sport, discipline, category)` triple must exist in the bundled dim
8. **Pole**: exactly one per race session, never on a DNS row
9. **Validate locally before upload**: drag the file into `race-validator`

If unsure: run the validator and read the error message. The rule ID it
prints (e.g. `VALUE_TYPE_002`) maps directly to a section above.
