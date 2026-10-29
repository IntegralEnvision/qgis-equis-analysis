# What ChemDuck would need to change to serve the QGIS plugin

**Written 2026-10-22** for whoever works on ChemDuck. It stands on its own;
the measurements behind it are in the QGIS plugin repository
(`equis_qgis_public`), in `docs/chemduck-parity.md` and
`docs/chemduck-spike.md`.

> **Status, 2026-10-28: all seven are done in ChemDuck**, and so is a second
> round of requests (one bug, eight gaps, six divergences) at `dca591a`. The
> detect flag is three-valued and a missing column refuses (`f9ce7b0`). No
> extension is needed; geometry is in `sql/optional/spatial.sql`
> (`ace3d90`). ChemDuck installs into a named schema and refuses to replace
> a host's objects. The shared rules, change 4, were decided in ChemDuck's
> `docs/shared-rules-decisions-response.md` and built as `aggregate_points`
> (D1–D7). The per-sample macros, the sample-type scope and the importable
> `chemduck` package (`68e95fc`) cover changes 5–7. What the plugin does with
> them is [chemduck-adoption-plan.md](chemduck-adoption-plan.md). This
> document is kept as the request it was.

## Context

The QGIS plugin maps EQuIS chemistry from the same ARII parquet exports
Chemrooms reads. Today it carries its own rules (Python and SQL in its
`store.py` and `report_data.py`). The goal is for ChemDuck's SQL to become
the one home of those rules for both projects, so a location can't show one
number in QGIS and another in Chemrooms.

Two measurements on one facility (3,452,146 results, 906 locations) shaped
this list:

- **Parity.** The plugin and ChemDuck were compared location by location. At
  both tools' default (most recent result, same-date duplicates averaged,
  non-detects at half the limit), **28% of location values differ**, 303 of
  them by more than tenfold. Every difference was traced to a cause.
- **Spike.** ChemDuck's hybrid ARII source was opened inside the plugin's own
  DuckDB, and Chemrooms' setup and query code ran unchanged on a plugin page
  through the plugin's bridge. It worked: 9 of 9 rows identical to the same
  SQL run directly, in QGIS 4.2.1.

ChemDuck has grown up beside a 3D globe frontend, and some of what follows is
that frontend's needs sitting in the shared core. The plugin is a 2D desktop
map, runs inside QGIS's own Python (DuckDB 1.5.4 in the QGIS 4.2.1 install
measured, or a DuckDB the plugin bundles), and must work with no network
access at run time.

## The changes, at a glance

| # | Change | Why the plugin needs it | Priority |
|---|---|---|---|
| 1 | Read a blank detect flag as unknown, and refuse a source with no flag | Today blank reads as non-detect, and no flag column reads as **all detected** | **Must**, before anyone relies on it |
| 2 | Make the `spatial` extension unnecessary | The plugin won't ship a 55.5 MB extension | **Must** |
| 3 | Install into a named schema without touching anything else | Opened beside the plugin, ChemDuck replaced the plugin's own `results` view | **Must** |
| 4 | Settle the event and duplicate rules shared with the plugin | The main source of the 28% | **Must**, decided with the plugin |
| 5 | Per-sample multivariate macros | Scatters and ternaries are per sample, never per location | Should |
| 6 | A sample-type scope on aggregates | QC samples in a map are a choice, not a default | Should |
| 7 | An importable Python API, and a tested DuckDB range | The plugin and its pipeline call ChemDuck from Python | Should |

What ChemDuck already does better than the plugin is listed at the end; those
parts shouldn't change.

## 1. Detection: a blank flag is unknown, a missing flag is a refusal

**What happens now.** The ARII mapping says `'Y' → true`, `'N' → false`,
`'' → null`. The loader and the hybrid `results` view build
`LOWER(CAST("DETECT_FLAG" AS VARCHAR)) IN ('y')` from it, and that gives:

| Flag | Mapping says | Expression gives |
|---|---|---|
| `Y` | true | true |
| `N` | false | false |
| `''` (blank) | null | **false** - a non-detect |
| NULL | — | null |

And when the detect column is absent or unmapped, the loader sets
`detect_expr = "TRUE"`: **every result reads as detected**, so every
non-detect's reporting limit is mapped as a measured concentration.

**Why it matters.** The plugin found the mirror image of this on
2026-10-08: with no detect column, every result read as a non-detect and was
halved, so arsenic at 10 and 40 µg/L mapped as 5 and 20, with no error. Its
rule since then: *detection comes from `DETECT_FLAG` and nothing else; a
source without it is refused; a blank flag is unknown, neither detected nor
not.* The facility measured here has only `Y` and `N`, so neither problem
showed in the numbers. Another export could hit both.

**What to change.**
- Build the expression from the whole transform map, as a `CASE`, so
  unmapped and blank values come out NULL rather than false.
- When the mapping names no detect column, or the source lacks it, refuse to
  load with `error()`, naming the column. Never default to `TRUE`.
- Every aggregate treats a NULL `detected` as unknown: counted, reported, and
  never ranked as either detect or non-detect.

**Fixture rows.** One location, one analyte: flags `Y` (10), `N` (limit 5),
`''` (limit 5). `detected` is true, false, NULL. A load whose mapping omits
`detected` fails with a message naming `DETECT_FLAG`.

## 2. No `spatial` extension required

**What happens now.** ChemDuck never calls a spatial *operation* - no
distance, buffer or transform. It uses the extension for three things:

- `01_tables.sql` declares `locations.geom GEOMETRY`;
- `tools/load_data.py` builds it with `ST_Point(x, y)`, and
  `tools/arii_hybrid.py` writes it to the sidecar with `ST_AsWKB`;
- every view and macro that touches locations passes `geom` along, and
  several put it in a `GROUP BY` (`v_results_denormalized`,
  `v_results_for_mapping`, `v_location_summary`, `aggregate_results`,
  `pivot_results_by_analyte`, `analyte_pair_data`, `analyte_composition`,
  `classify_concentration`, and three extended views).

Every one of them also carries `x` and `y` as plain doubles. Chemrooms draws
from `x` and `y` and never reads ChemDuck's `geom`.

**Measured.** In the DuckDB 1.5.4 inside QGIS's Python, with `spatial` neither installed nor
auto-loaded, the hybrid consumer opened and `aggregate_results` answered:
the sidecar's `geom` arrives as a WKB `BLOB` and is only passed through. So
the *consumer* already works without the extension. The full loader, the
publisher and every test (`INSTALL spatial; LOAD spatial;` in four test
files) still require it.

**What to change.**
- Take `geom` out of the core. `locations` keeps `x`, `y` and a recorded
  CRS. Views and macros stop passing `geom` and stop grouping by it.
- Move geometry to an optional file (say `sql/90_spatial.sql`) that a
  consumer with `spatial` loaded runs to add, for example,
  `v_locations_geom`. Nothing in the core depends on it.
- The loader and publisher build geometry only when asked.
- Run the test suite in two modes: core with `spatial` absent and
  auto-loading off, which must pass; and core plus the spatial file.

**Acceptance.** The whole test suite and `tools/check_arii_parity.py` pass
under `duckdb.connect(config={"autoload_known_extensions": False,
"autoinstall_known_extensions": False})` with no `spatial` installed.

## 3. Install into a named schema, and never replace a host's objects

**What happened.** In the spike, ChemDuck's consumer was first opened in the
plugin's `main` schema. Its `CREATE OR REPLACE VIEW results` silently
replaced the plugin's own `results` view, and the plugin's next query failed
(`Referenced column "detect" not found`). It failed loudly only because the
columns happened to differ. Opened in a schema of its own, `chemduck`, from a
cursor whose default schema was `chemduck`, everything worked side by side.

**What to change.**
- Every entry point that creates objects (`load_schema`, `open_consumer`,
  the bootstrap) takes a target schema and creates only there.
- No ChemDuck SQL references `main.` or another schema by name.
- Creating an object that already exists in the target schema and wasn't
  made by ChemDuck is refused, not replaced. ChemDuck can record what it
  created in a small table of its own.
- Document how a host calls it: a cursor with `USE <schema>`, or
  schema-qualified names.

**Acceptance.** A test opens ChemDuck into schema `chemduck` of a database
that already has a `main.results` view with different columns.
`main.results` is unchanged afterwards, and `chemduck.aggregate_results`
answers.

## 4. The rules shared with the plugin

The parity measurement found these differences. Each needs **one decision
for both projects**; the recommendation and the ChemDuck-side change follow
each.

1. **An event is a sample, not a day.** ChemDuck's `sample_date` is a date,
   with the time kept as an attribute, and its duplicate stage averages all
   of a day's samples at one depth. The plugin's "most recent" is the latest
   sample by time. At the measured facility, 400,287 water groups (location,
   fraction, analyte, date, depth) hold several samples: 299,932 taken at
   different times of day, 93,724 at one timestamp. This is most of the 28%.
   *Change:* key events on `sample_id` with the full timestamp. Combining a
   day's samples becomes an explicit recipe, never the default.
2. **Never average a detect with a substituted non-detect and call the
   result detected.** The duplicate stage does
   `avg(adj_result) ... bool_or(detected)`: one dissolved-iron location
   mapped 6.10 "detected", the mean of 12 samples, some of them half-limit
   substitutes, where the latest sample was 2.03. This is
   `domain-rules-remediation.md` §2, still open. *Change:* a combination
   containing a non-detect is not detected (or carries a third state).
   Selecting one real result is always allowed.
3. **Select across methods, never average across them** (§2, Layer 0).
   Two methods on one sample are averaged today; at one tailings station
   that gave 2.95 "detected" where one method detected and the other didn't.
   *Change:* the duplicate key includes method; cross-method selection
   returns the winning method with the value.
4. **One limit chain, applied per row** (§5). ChemDuck reaches the
   quantitation limit; the plugin doesn't, and can't value 51,844 field-
   parameter non-detects that ChemDuck can. Both should use one chain:
   `REPORT_RESULT_LIMIT`, then the individual limits in a documented order
   including the quantitation limit. Record the export's Non-Detect
   Multiplier and refuse to halve a value it has already halved.
5. **Mixed units: refuse by default.** `aggregate_results` computes the
   aggregate and sets `units_conflict`; the plugin refuses to map a
   mixed-unit selection, because a maximum across ug/L and mg/L ranks by unit
   (38 analyte/matrix/fraction combinations here hold more than one unit).
   *Change:* a parameter, defaulting to refusal, so a caller can't forget to
   check the flag.
6. **Combining matrices only when units agree.** `aggregate_results` always
   keeps matrices apart. The plugin lets an analyst combine matrices that
   share units (surface water with groundwater, sediment with tailings), and
   refuses otherwise. Either ChemDuck accepts a list of matrices behind the
   same unit guard, or the plugin drops the feature; decide which.

## 5. Per-sample multivariate macros

`pivot_results_by_analyte` returns, per location, the **average** of each
analyte over every event; `analyte_pair_data` groups by location and date.
The plugin's rule, from measurement: at 174 of 216 multi-sample water
stations, the maximum manganese and the maximum lead come from different
samples, so a per-location scatter plots combinations no sample ever had.
Every point in a scatter or ternary is **one sample**, with each analyte's
value, detect flag, qualifier and limit in its own columns.

The plugin also measured how to build that table fast: one map per sample,
read out by key, built in its own statement, took 0.26 s for every analyte in
tailings, where `max(...) FILTER (WHERE ...)` per analyte ran past five
minutes and 18 GB. A ChemDuck macro for this should take the same approach.

## 6. A sample-type scope on aggregates

`aggregate_results` has no sample-type parameter; the views exclude only
`AVG`, `REAN`, `DNU`, `IDW` and `NR`, so field duplicates, blanks and trip
blanks enter a map by default. The plugin scopes every query by sample type
in one place, so a map and a chart can't disagree. *Change:* a scope
parameter (remediation §6), applied before every aggregate. (The facility
measured holds only normal samples, so this wasn't exercised.)

## 7. An importable Python API, and a tested DuckDB range

- `arii_hybrid.publish` and `open_consumer` are reached today by putting
  `tools/` on `sys.path`. The plugin and its nightly pipeline need an
  importable package with a stable, versioned API.
- Publishing the sidecars took 8.8 s and produced 645 KB for this facility.
  In the plugin's world it becomes a step of the nightly pipeline, which
  today needs only Python and pyarrow; it would gain `duckdb` and `pyyaml`,
  but not `spatial` once change 2 lands.
- The QGIS 4.2.1 install measured has DuckDB **1.5.4** in its own Python; the
  plugin can also bundle one. ChemDuck checks DuckDB-WASM compatibility; it
  should also test against the Python DuckDB versions the plugin runs on.
- Keep shipping the SQL beside the data with `SOURCE.json`, as now. A
  consumer then always runs the SQL its data was written for.

## Not ChemDuck's to change, but adjacent

Chemrooms' `buildSamplesLayerSql` wraps the `aggregate_results` call in its
3D apparatus: `geoid_offset()`, terrain heights sampled from Cesium,
depth-unit tables for drawing depth below the surface. A 2D map pays for
those joins and ignores them. If Chemrooms exported the aggregation query
separately from its altitude decoration, both apps could share that query.
That's a Chemrooms change, noted here because it is where the shared code
would live.

## What ChemDuck already does right

Keep these. On each, the plugin is the one that should move:

- **Depth intervals are distinct measurement points.** The plugin merges a
  profile's depths and its "most recent" takes an arbitrary depth (one water
  station has 105).
- **Ties go to the later date.** The plugin keeps the first row.
- **The limit chain reaches the quantitation limit.**
- **Fractions are never combined**, and unknown fraction codes pass through
  rather than becoming NULL.
- **A non-detect never wins a maximum over a detection.** With duplicates
  maximised, the two tools' maxima differ in only 24 of 28,187 location
  cells.

## How to know it's done

The plugin's `tools/measure_chemduck_parity.py` compares the two on a real
facility and names the cause of every difference. Once the decisions in §4
are made and implemented on both sides, it becomes a gate: at the shared
default recipe, the only differences left should be ones both projects have
written down as intended. ChemDuck's own `tests/test_domain_rules.py` gains
a fixture for each change above, mutation-checked as its existing rules are.
