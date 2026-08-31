# Race Results Data Contract — v2.7.0

> **Status:** Active
> **Library that enforces it:** `race_validator` v0.5.0
> **Last revised:** 2026-08-26

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
| `142__2025__upcoming__2026-05-19.csv` | `upcoming` is not a valid file type; write `upcoming_schedule` |
| `142__2025__results__26-05-19.csv` | Date must be `YYYY-MM-DD` |
| `abc__2025__results__2026-05-19.csv` | `series_id` must be a positive integer |
| `142__25__results__2026-05-19.csv` | Year must be 4 digits |

**Validator:** `FILE_NAMING_001`, `FILE_NAMING_002`, `FILE_NAMING_003`

#### The filename must agree with the contents

The `series_id` in the filename must equal the `series_id` column on **every
row**. Both values can be individually valid and still disagree, and when they
do the file is not merely misnamed — it is filed as one series and read as
another:

- the filename decides **where the file is stored** (the raw bucket partitions
  on it) and what `uploads.series_id` records in the ledger;
- the column decides **what the data is loaded as** downstream.

A real example that passed every other rule: `69__2024__results__2026-05-19.csv`
carrying `series_id = 68` on all 967 rows. 68 and 69 are both real series
(Spanish F4 and French F4), and `series_name_normalized` agreed with the
*column*, so `MASTER_REF_003` was satisfied too. The file was French F4 by name
and Spanish F4 by content, permanently.

Fix whichever is wrong: rename the file, or correct `series_id` **and**
`series_name_normalized` together, copying both from `dim_series`.

**Validator:** `FILE_NAMING_003`

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
| `_m` | Metres (integer) | `layout_length_m` |
| `_km` | Kilometres (float) | `layout_length_km` (not used; we use `_m`) |
| `_kph` | Kilometres per hour | `best_lap_speed_kph` |
| `_seconds` | Seconds (float) | (not currently used) |

### 2.3 Boolean naming

Boolean columns must start with `is_`: `is_pole`, `is_fastest_lap_overall`,
`is_fastest_lap_in_class`.

### 2.4 Foreign-key naming

Foreign-key columns end with `_id`: `country_id`, `series_id`, `circuit_id`,
`region_id`. Numeric IDs are integers; `country_id` and `region_id`
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

A `_display` variant of the name may exist downstream, but it does NOT appear
on result rows — send the `_raw` and `_normalized` pair only.

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

**No column in either file uses this shape as of 2.5.0.** `ingested_at` was
removed and `scraped_at` became a date — and in 2.6.0 `scraped_at` was removed
too. Nothing for a collector to produce here.

The format is documented because the validator still enforces it on any column
declared UTC, so it applies immediately if a machine-event column is
reintroduced: ISO 8601 with a `Z` suffix and a strict `T` separator, e.g.
`2026-05-19T08:00:00Z`. The offset form `+00:00` is not accepted in its place.

#### Date-only fields

**No column in either file uses this shape as of 2.6.0.** `scraped_at` was
removed; the scrape date now lives only in the filename's `scraped_date`
component (§1.1), which `FILE_NAMING_001` validates. The rule below is retained
because it is schema-driven and applies the moment a DATE column is introduced.

Used for: the `scraped_date` component of the filename (§1.1).

Format: `YYYY-MM-DD`.

**Valid:** `2025-06-08`, `2026-01-15`
**Invalid:** `8 June 2025`, `06/08/2025`, `2025-6-8`, `2026-05-19T08:00:00Z`

A time component is rejected outright — a date-only field records *which day*
something happened, nothing finer.

#### Session start-time window

**No session may start at or after 18:00 local time.** `session_datetime_local`
is tested on its **wall-clock** time — the value as written, before the UTC
offset. `2024-05-11T18:10:00+02:00` starts at 18:10 at the venue and is
rejected, regardless of what that instant is in UTC.

The boundary is inclusive: `18:00:00` fails, `17:59:59` passes.

There are **no exemptions** — the rule applies to every series and every
session type, practice included. Endurance series that run at night, and
individual evening races in sprint series, cannot pass while this rule stands.
That is deliberate, not an oversight.

When a time is wrong rather than genuinely late, the usual cause is a timezone
offset applied twice: check the wall-clock time against the venue's published
timetable, and that the offset is the venue's own rather than the collecting
machine's.

**Validator:** `SESSION_TIME_001`

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

**Validator:** `VALUE_TYPE_001/002/003`, `TIMESTAMP_001/002`, `SESSION_TIME_001`,
`FORBIDDEN_CHARS_001`, `WHITESPACE_001`

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
series_id, parent_organization, series_name_display, series_name_normalized,
scope, country_id, region_id
```

`parent_organization` is the umbrella organization a series belongs to (e.g.
`Porsche Carrera Cup` for the national/regional Carrera Cup series, `Ferrari
Challenge`, `Formula Middle East`). Empty for standalone series.
`series_name_normalized` is the `series_name_display` run through
`normalize_identifier` (§3a).

**Scope-consistency rule:**

| `scope` | `country_id` | `region_id` |
|---|---|---|
| `club` | populated | empty |
| `national` | populated | empty |
| `regional` | empty | populated |
| `international` | empty | populated |

### 4.5 `dim_circuits`

One row per circuit layout (Brands Hatch GP and Brands Hatch Indy are
separate rows). Columns:

```
circuit_id, circuit_full_name_display, layout_display,
circuit_full_name_normalized, layout_normalized, local_address,
latitude_dd, longitude_dd, country_id, layout_length_m,
alt_circuit_full_name_display
```

- `circuit_full_name_display` / `layout_display` are the human-readable circuit
  name and its specific layout, kept in separate columns.
- `circuit_full_name_normalized` / `layout_normalized` are the respective
  `_display` values run through `normalize_identifier` (§3a). `layout_normalized`
  is empty when no layout is given.
- `local_address` is the circuit's location/address (replaces the former
  `city`).
- `alt_circuit_full_name_display` holds an alternate/historical display name;
  may be empty.
- Coordinates (`latitude_dd`, `longitude_dd`) are required (used for weather
  enrichment).
- `layout_length_m` (formerly `circuit_length_m`) is **optional**: many obscure
  layout variants have no officially published length, so this field may be
  empty. When present it is in whole metres (`5891`, not `5.891`).

### 4.6 What collectors look up where

| ID in your CSV | Looked up against |
|---|---|
| `country_id`, `nationality_code` | `dim_countries.country_id` |
| `region_id` | `dim_regions.region_id` |
| `series_id` | `dim_series.series_id` |
| `series_name_normalized` (results) | `dim_series.series_name_normalized` (for the row's `series_id`) |
| `circuit_id` | `dim_circuits.circuit_id` |
| `(sport, discipline, category)` triple | `dim_categories` |

If a series, circuit, or category you need to scrape isn't in the bundled
dim, **stop and tell Berkay** — adding it requires a library release.

**Entity resolution is not the collector's job.** Drivers, teams, and car
models are identified by their `_raw` / `_normalized` name columns only.
Matching those names to warehouse IDs happens downstream at load time, so
there are no `driver_id` or `team_id` columns to fill in — send the names
correctly normalized (§3a) and the resolution layer does the rest.

**Validator:** `MASTER_REF_001`, `MASTER_REF_002`, `MASTER_REF_003`

---

## §5. Canonical schema — `results` file

All `*_id` columns are required; NULL values fail validation.

### 5.1 Identity & context

| Column | Type | Required | Notes |
|---|---|---|---|
| `series_id` | INT | yes | FK to `dim_series` |
| `series_name_normalized` | STRING | yes | Denormalized from `dim_series`; must equal `dim_series.series_name_normalized` for this `series_id` |
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
| `car_number` | INT | yes | The visible race number |
| `team_name_raw` | STRING | yes | Exactly what the source said |
| `team_name_normalized` | STRING | yes | `normalize_identifier(team_name_raw)` |
| `car_model_raw` | STRING | no | What the source said: `Oreca 07 - Gibson` |
| `car_model_normalized` | STRING | no | `normalize_identifier(car_model_raw)` if non-empty |

### 5.4 Driver

| Column | Type | Required | Notes |
|---|---|---|---|
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
| `laps_completed` | INT | conditional | Required for race `FINISHED`/`LAPPED`; optional for `DNF`/`DNS`/`DSQ`/`DNQ` and for practice/qualifying. See §5.7 |
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

**`laps_completed`:**

| `session_type` | `race_status` | `laps_completed` |
|---|---|---|
| `race` | `FINISHED` or `LAPPED` | **required** |
| `race` | `DNF`, `DNS`, `DSQ`, `DNQ` | **optional** |
| `practice` or `qualifying` | (empty) | **optional** |

Note the difference from `race_time_ms`: a non-finisher's `race_time_ms` must
be *empty*, but their `laps_completed` is merely *optional*. A driver who
retired on lap 30 did complete 30 laps, and that number is worth keeping when
the source reports it. Send it when you have it; leave it blank when you don't.

The same applies to practice and qualifying, which became optional in 2.6.1.
A lap count is a race metric: for a timed session, sources publish the lap
*times* rather than a count, so requiring one asked for data that does not
exist. Optional means optional, not forbidden — send it where your source
does report it.

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

**Validator:** `CROSS_FIELD_001/002/003/004`, `POLE_001`, `FASTEST_001/002`,
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

---

## §7a. Canonical schema — `upcoming_schedule` file

A third scraped format. Where `schedule` records the sessions of an event that
has **already run**, `upcoming_schedule` records what a series has **announced**
— sessions that have not happened yet. One row per announced session.

| Column | Type | Required | Notes |
|---|---|---|---|
| `series_id` | INT | yes | |
| `season_label` | STRING | yes | |
| `round_number` | INT | yes | |
| `session_type` | STRING | yes | `practice` \| `qualifying` \| `race` |
| `session_number` | INT | yes | |
| `circuit_id` | INT | yes | |
| `session_date` | DATE | **yes** | The announced day, `YYYY-MM-DD` |
| `session_datetime_local` | TIMESTAMP_LOCAL | **no** | The running time, once announced |
| `sport` | STRING | yes | |
| `discipline` | STRING | yes | |
| `category` | STRING | yes | |
| `source_url` | STRING | yes | |
| `source_collector` | STRING | yes | |

### Why the date and the time are separate columns

A published calendar reliably gives a **day** for each session. The time of day
is often not fixed until much closer to the event, and sometimes not until the
weekend itself. So the day is required and the time is optional.

That split is why this is its own file type rather than a nullable column on
`schedule`. On `schedule` the time is a fact of record — a session that has run
happened at a specific moment, and a blank there means data is missing. Here a
blank is the honest answer, and inventing a plausible time would be worse than
leaving it out.

**Leave `session_datetime_local` empty rather than guessing.** A guessed time
propagates: the weather and track-condition steps evaluate the physics at the
session time, so a wrong time yields wrong conditions, with nothing to indicate
it.

### The two must agree

When `session_datetime_local` is present, its **local** date must equal
`session_date`. Two columns describing one moment drift otherwise, and nothing
else compares them.

The comparison is on the wall-clock date as written, before the offset:
`2026-03-14T23:30:00+01:00` is the 14th at the circuit and `session_date` should
say the 14th, even though that instant is the 15th in UTC.

**Validator:** `SESSION_DATE_001`

### Not carried over from `schedule`

`planned_duration_minutes` only. It is rarely published in advance, and a
guessed duration is worth less than a blank.

`source_url` and `source_collector` **are** required, as everywhere else. Row
level provenance matters more here than elsewhere, not less: an announced
calendar is a claim about the future that the series can revise, so knowing
which page a row came from is what makes a later revision traceable.

---

## §8. Forbidden patterns

Quick reference of things that always cause validation to fail:

- Mixed types in one column (`"16"` and `"12 laps"` in the same field)
- Status strings in numeric columns (`"DNS"` in `gap_to_leader_ms`)
- Unit suffixes in numeric columns (`"17 laps"` where INT expected)
  - Allowed in `*_display` and `*_raw` STRING columns
- Same physical concept stored in different columns by session type
- Duplicate rows (exact match on every column) — `DUPLICATE_001`
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
- Multiple `is_pole=TRUE` rows in one race session
- Zero `is_pole=TRUE` rows in a race session
- `is_pole=TRUE` on a `DNS` driver

---

## §9. Validation behavior

Data reaches the warehouse in three stages.

### Stage 1 — Validation (hosted app, or locally)

What you run via the `race-validator` app or CLI.

Hard-fails on any structural, format, value-level, normalization, or
cross-field rule violation. Reports all findings at once with row numbers,
rule IDs, and fix hints.

There is no entity-resolution step here, and there will not be one. Earlier
drafts planned to have collectors resolve drivers and teams to IDs during
validation; 2.5.0 moved that work to load time instead, which is why the
`_id` columns are gone. Collectors validate format and normalization only.

### Stage 2 — Submission

Passing files are submitted through the validator app, which stores the CSV
and records the validation report against the submitting collector. Files that
fail are not uploaded.

### Stage 3 — Load-time resolution

Names are matched to warehouse entities when the file is loaded, creating new
drivers, teams, and car models as needed. This is the layer that assigns the
surrogate and foreign keys removed in 2.5.0.

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

3. **Validate the file.** Either route runs the identical rule set:

   - **Hosted** (normal route) — sign in at
     <https://wingmandatavalidator.streamlit.app/> and drop the CSV in.
     Accounts are invite-only; ask Berkay for one.
   - **Local** — `race-validator`, which opens the same app at
     `localhost:8501`. Validate-only: nothing leaves your machine, and there
     is no submit step.

   Review every finding. **Errors block submission; warnings do not** — a
   warning means the file is acceptable but you should check the value.

4. **Fix and re-validate** until the file passes. Fix the scraper, not the CSV
   (see below).

5. **Submit.** On a passing file the hosted app shows **Submit To Wingman**.
   That button is the whole upload step — there is no bucket to write to by
   hand, and no path to construct. The app stores the CSV in the private `raw`
   bucket under

   ```
   <series_id>__<series_name>/<season_label>/<file_type>/<scraped_date>/<filename>.csv
   ```

   and records one row per submission — including the full validation report —
   against your account.

   A file that fails is never uploaded. This is enforced twice: the button does
   not appear, and the database's insert policy independently refuses any row
   not marked as passing. Bypassing the UI does not get a failing file in.

6. **Confirmation is immediate.** A passing submission is accepted or refused
   on the spot, not by a later report. The refusal you are most likely to meet:

   - **Already submitted** — this exact file content was submitted before.
     Deduplication is on the file's content hash, so renaming a file does not
     make it new. Change the data, or leave it alone.

   The submission also records which `contract_version` and `library_version`
   judged the file, so a file that passed under an older rule set stays
   distinguishable from one judged by a stricter later one. Note that the app
   does **not** refuse an older library: keeping current is a matter of taking
   Berkay's releases, not something the submission gate enforces.

   You can see your own submission history in the app. You cannot browse the
   bucket, and you cannot see other collectors' files.

**Collectors never edit CSVs by hand.** If a row is wrong, fix the scraper
and re-run; don't patch the output.

---

## §11. Versioning

- **Contract version:** `2.7.0`
- **Library version:** `0.5.0`

### Changes in 2.5.0 — breaking

Four ID columns are **removed** from the `results` schema:

| Removed | Was |
|---|---|
| `result_id` | Surrogate key invented by the scraper |
| `entry_id` | Unique per car per session |
| `team_id` | FK to `dim_teams` |
| `driver_id` | FK to `dim_drivers` |

All four are now assigned downstream, by a resolution layer that runs when data
is loaded into the warehouse tables. Collectors no longer produce any of them.

`result_id` and `entry_id` were surrogate keys with no source in the scraped
page — the scraper had to invent them. `driver_id` and `team_id` were foreign
keys the collector could never resolve correctly anyway: the dim tables they
pointed at were never bundled, so the validator only ever checked that the value
looked like an integer.

What this means for you:

- Delete all four columns from your scraper output. Column order must match §5
  exactly, so a file that still carries them fails `COLUMN_NAMES_001` — a hard
  rejection, not a warning.
- Rule `DUPLICATE_002` (duplicate `result_id`) is retired. Exact-duplicate row
  detection (`DUPLICATE_001`) is unaffected and still runs.
- Drivers, teams and car models are now identified **by name only**. This makes
  §3a normalization the load-bearing part of your output: two spellings of one
  driver resolve to two different people. Get `normalize_name` right.
- Files validated under 2.4.0 or earlier will not pass 2.5.0 without this edit.

The `results` file was **41 columns** at 2.5.0, down from 46. 2.6.0 takes it to **40** — see below.

Two further changes in 2.5.0:

**`scraped_at` is now a date, not a timestamp.** Send `2026-05-19`, not
`2026-05-19T08:00:00Z`. A time component is rejected. This matches the
`scraped_date` field already in the filename, so the two should agree.
Applies to both `results` and `schedule` files.

**`ingested_at` is removed.** It was always left blank by collectors and
populated by the pipeline, so carrying an empty column through every file
bought nothing. The load step records ingest time itself. Delete the column —
like the ID columns above, leaving it in place fails `COLUMN_NAMES_001`.

**`laps_completed` is no longer required for non-finishers.** It may be left
empty on `DNF`, `DNS`, `DSQ` and `DNQ` rows in a race session. It is still
required for `FINISHED` and `LAPPED` rows, and for every practice/qualifying
row. New rule `CROSS_FIELD_004` enforces this. Populating it for a
non-finisher remains valid and is preferred where the source reports it.

## What changed in 2.7.0

**New file type: `upcoming_schedule`.** Announced sessions that have not
happened yet, described in §7a. Filenames use it exactly as written:

```
64__2025__upcoming_schedule__2026-08-26.csv
```

Thirteen columns. The difference that matters is `session_date` (DATE, required)
alongside `session_datetime_local` (optional): a calendar gives the day
reliably, the running time often not until much later. New rule
`SESSION_DATE_001` requires the two to agree on the day when both are present.

Purely additive — `results` and `schedule` are untouched, and nothing that
passed under 2.6.1 fails under 2.7.0.

## What changed in 2.6.1

**`laps_completed` is no longer required for practice or qualifying.**
`CROSS_FIELD_004` now asks for it only on classified race finishers
(`FINISHED`, `LAPPED`). See §5.7.

A lap count is a race metric. For a timed session the sources publish lap
*times*, not a count, so the rule was demanding data that does not exist: a full
Italian F4 2025 season had it blank on all 864 practice and qualifying rows
while every `FINISHED` and `LAPPED` race row carried it. That is the source being
consistent, not a collector cutting corners.

Relaxation only — nothing that passed under 2.6.0 fails under 2.6.1.

## What changed in 2.6.0

**The filename's `series_id` must match the `series_id` column.** New rule
`FILE_NAMING_003`, described in §1.1. Nothing previously compared the two, so a
file could be stored under one series and read as another while passing every
rule. No collector action is needed for files that were already consistent —
this rule only rejects a file that contradicts itself.

**No session may start at or after 18:00 local time.** New rule
`SESSION_TIME_001`, described in §3.4. Tested on the wall-clock time in
`session_datetime_local`, with no exemption for any series or session type.

This one is not backward-compatible with existing data: files carrying a
legitimate evening session no longer pass. Of the files on hand when the rule
was introduced, 238 of 9,860 rows fell after the cutoff — a night practice in
series 3 (`24h_series`) and an 18:10 race in series 68
(`f4_spanish_championship`). Both were valid; both are now rejected. Collectors
with such files cannot submit them under 2.6.0.

**`scraped_at` is removed.** The scrape date is already carried by the
filename's `scraped_date` component, so repeating it on every row bought
nothing — and because nothing compared the two, a file could disagree with its
own name indefinitely. One value, one place: the filename.

`results` goes from 41 columns to **40**, `schedule` from 14 to **13**. Delete
the column — like `ingested_at` before it, leaving it in place fails
`COLUMN_NAMES_001`. No DATE column remains in either file; the format rule stays
documented in §3.4 because it is schema-driven and applies the moment one is
added.

Every file produced before 2.6.0 carries this column and will therefore fail
until the scraper drops it and the file is re-exported.

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

### 12.2 New drivers, teams, and car models

Nothing to do. Unlike series, circuits, and categories — which must already
exist in a bundled dim before you can reference their ID — drivers, teams and
car models are carried purely as names.

A driver appearing for the first time needs no registration and no new library
release. Send `driver_full_name_raw` as the source spelled it, plus the
`normalize_name` output alongside (§3a), and the load-time resolution layer
either matches an existing entity or creates one.

That places the whole burden on normalization being right. Two spellings of the
same driver resolve to two people, so §3a determinism is what makes this work —
it is the only thing tying your rows to an identity.

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
