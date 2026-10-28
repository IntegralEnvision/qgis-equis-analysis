# Adopting ChemDuck: the plan

**Written 2026-10-28**, after surveying ChemDuck at `278c29b` (package
0.2.0, shared rules D1–D7 implemented) and this plugin on `overlay-shell`.
Background: [chemduck-parity.md](chemduck-parity.md) (where the two
disagreed, by cause), [chemduck-spike.md](chemduck-spike.md) (ChemDuck
inside the plugin's DuckDB works),
[chemduck-changes-for-qgis.md](chemduck-changes-for-qgis.md) (what was asked
of ChemDuck), and ChemDuck's `docs/shared-rules-decisions*.md`.

## The goal, and what doesn't move

**ChemDuck's SQL becomes the one authority for chemistry rules** in this
plugin: which result a location shows, how a non-detect is valued, which
results may be combined, when units or matrices refuse. Today the plugin holds
three copies of those rules (`report_data.py`, `store.py`,
`pipeline/arcgis_export.py`), and ChemDuck a fourth. The end state has one.

What stays the plugin's:

- **Getting data**: the EQuIS API, EIA stored output, the cache and offline
  mode, published parquet. ChemDuck has no EQuIS API loader, and the cached
  and offline marking stays in the panel.
- **QGIS**: layers, symbology, sparklines, the map tool, the time-series dock,
  the overlay and its window manager.
- **The panel's state**: `panel_state.py` stays the authority on what the
  panel can do. ChemDuck is the authority on the numbers.
- **The structural rule**: the new adapter module imports no QGIS, like
  `store.py`, so its tests run under a plain interpreter.

## What ChemDuck gives us now

| Plugin need | ChemDuck 0.2.0 |
| --- | --- |
| One value per location (map) | `aggregate_points` (one row per measurement point) and `rollup_points` (one per location, by a named rule: `max_over_points`, `named_position`, `none`) |
| Most recent / maximum | `p_event_agg := 'most_recent' \| 'maximum'`; `'statistic'` for Kaplan–Meier mean or median |
| Non-detect handling | Named, versioned policies (`chemduck_default` and four others) with a display factor; `rank_across` / `rank_reruns` |
| Row scope | `p_start_date`, `p_end_date` (whole days, inclusive), `p_sample_types` (NULL means normal samples) |
| Fractions | Every fraction its own row; `p_fraction` to pick one |
| Mixed units | Converted at load or publish; `p_units := 'refuse'` by default, with an error naming units and counts |
| Combining matrices | `p_matrices`, guarded by matrix class, unit and measurement basis |
| Per-sample (scatter, ternary) | `sample_results`, `sample_cell_maps` (one map per sample, the approach this plugin measured fastest) |
| Provenance | `sample_id`, `method_code`, `selected_by`, `rerun_selected_by`, `limit_source`, `nd_policy`, `nd_rule`, `n_points`, `matrices`, `unit_source`, `n_unknown_detect` |
| Install | `open_consumer(con, folder, partitions, schema=...)`; refuses to replace objects it didn't create; macros called through `schema_cursor(con, schema)` |
| Dependencies | `duckdb>=1.5.4`, `pyyaml>=6.0`; no extensions. QGIS 4.2.1's Python has both (DuckDB 1.5.4, PyYAML 6.0.3) |

## What ChemDuck still needs from us (upstream requests)

These block a phase below, and each is a ChemDuck change, raised there rather
than worked around here.

1. **Bug: `sample_results` checks units across matrices.** Its
   units-conflict window (`10_sample_results.sql` line 113) partitions by
   analyte, fraction, basis and method class, but not matrix. Called without
   `p_matrix`, an analyte measured in water (ug/L) and soil (mg/kg) is treated
   as mixed and silently dropped. *Blocks phase 4.*
2. **A refused variable must be listed, not just dropped.** `sample_results`
   under `'refuse'` leaves a mixed variable out without saying so. The plugin
   shows what was left out, as its pivot does today. *Blocks phase 4.*
3. **A load path into a named schema for report rows.** `load_to_duckdb` has
   no `schema` parameter and writes a database file. The plugin's live, EIA
   and cached reports arrive as ARII rows in memory and need loading into the
   session database's `chemduck` schema. *Blocks phase 1 for non-parquet
   sources.*
4. **A per-point time-series macro**, policy-aware: one location, one
   variable, every event, each with its value, detect flag, limit, date,
   method and provenance. `v_timeseries_data` has no fraction, method class,
   position or policy, and isn't in the consumer bundle. *Blocks phase 3.*
5. **Kaplan–Meier quartiles.** The box plots need Q1/median/Q3 with the
   undetermined flag. The plugin's `web/src/censored.js` and its independent
   test (`tools/test_censored.py`) can move to ChemDuck as the starting
   point. *Blocks phase 4's box plots.*
6. **Catalogue views** for what the data holds, for the pickers: analytes by
   matrix, fraction **and method class**; the method classes and positions
   present. `v_analyte_summary` has no method class. *Blocks phase 2.*
7. **A display factor as a call parameter**, separate from the policy. The
   plugin offers "non-detects at the limit / at half the limit / hidden".
   Only display changes between these, not selection, but today the only
   0.5-factor policies also average. *Blocks phase 2, or decision 1 settles
   it another way.*
8. **`sample_results` takes `p_method_class` and `p_matrices`**, like the
   aggregates.
9. **A reportable-only scope**, and "dated after the load" in the load
   report (both promised in the response; neither exists).

## The design after adoption

**One DuckDB per session, two schemas.**

- `main` keeps what is the plugin's: the report's own columns for export
  (`raw`), location features, and the store's bookkeeping.
- `chemduck` holds ChemDuck: `locations`, `samples`, `results` (a view over
  the parquet, or a table loaded from report rows) and its macros.

The spike measured this arrangement working, with ChemDuck in its own schema
beside the plugin's.

**One adapter, `equis_loader/chem.py`.** Every chemistry question goes
through it; nothing else in the plugin writes chemistry SQL. It:

- opens ChemDuck on the session database (parquet: `open_consumer` on the
  published sidecars; report rows: upstream request 3);
- holds the row scope (dates, sample types) and passes it to every call, so
  `_apply_row_scope` keeps setting it in one place;
- maps the panel's choices to macro parameters (aggregation, policy and
  display, fraction, matrix or matrices, method class, rollup);
- turns ChemDuck's refusals into the panel's status-line messages;
- returns the shapes the callers use today, plus the provenance.

**`ReportStore` shrinks** to the source and the plugin's own tables: open a
manifest or files, the raw columns, location features, export, changed
files. Its `v_results` rules, `aggregate`, `series`, `units_for`, `count_for`
and `rows_for` move behind the adapter.

**DuckDB becomes mandatory.** The live path already builds a store in the
background; the `ReportTable`-only mode (before the store lands, or if
DuckDB is missing) can't hold ChemDuck's rules and retires. That reverses
"A DuckDB-less fallback path" in `implementation-plan.md`'s "deliberately
not planned" list into a decision: decision 4 below.

**ChemDuck ships inside the plugin**, vendored and pinned like the DuckDB
bundle, at a recorded ChemDuck version. The DuckDB the plugin runs must be in
ChemDuck's `SUPPORTED_DUCKDB` (1.5.4–1.5.5 today). The new QGIS beta's DuckDB
has to be checked against it.

## Phases

Each phase ends with something run and seen, per this project's habit, and
leaves the plugin releasable.

### Phase 0 — fix what's wrong today, and vendor ChemDuck

The survey found defects in the current product, independent of ChemDuck.
They're cheap, and they'd otherwise be carried into the comparison:

1. **Export breaks on the live path once the store lands.** The background
   store is built without the report's columns (`BuildStoreTask(table, path)`
   at `dialog.py:2520`, `with_raw` defaulting to False), and export then asks
   that store for them (`dialog.py:5235`). Seen in the code, not yet run.
2. **The mixed-unit guard is skipped on a `ReportTable`** (`dialog.py:4370`
   returns early), so a live or cached report is mapped unchecked until its
   store lands.
3. **Units differ by source.** The parquet path coalesces `RESULT_UNIT` and
   `REPORT_RESULT_UNIT`; the live path takes the first column present, so a
   live non-detect is unitless where the same row from parquet reads ug/L.
4. **`count_for` and `units_for` have no tests**, though `count_for`'s
   docstring says the parity gate covers it.
5. **The histogram filters differently from the store**: case-sensitive
   matrix, fraction and method comparisons (`histogram.js:83-85`), and only
   the first matrix of a multi-matrix layer.

Then: `tools/vendor_deps.py` vendors ChemDuck at a pinned commit into the
plugin, and a test checks the pinned version, `SUPPORTED_DUCKDB` against the
running DuckDB, and the bundle's checksum.

*Done when:* each defect has a failing test first, then a fix;
`tools/test_offline.py` and `tools/test_parquet_source_ui.py` pass under
QGIS; the plugin zip carries ChemDuck and the version test passes.

### Phase 1 — ChemDuck alongside, answering nothing yet (shadow)

- The adapter opens ChemDuck on both paths. Parquet: sidecars published next
  to the manifest (phase 6), or built locally in a worker if absent
  (8.8 s for Facility A). Reports: upstream request 3.
- Nothing in the UI changes. A debug setting logs, for each map built, the
  adapter's answer beside `ReportStore.aggregate`'s.
- `tools/measure_chemduck_parity.py` becomes a test that compares the
  adapter to the store and holds every difference to an **intended
  differences list**: positions and depths kept apart, events by timestamp,
  methods selected and reported, the quantitation limit reached, ties to the
  later date. A difference with no entry fails.
- A **shared fixture facility**, synthetic and in this repository, exercises
  every rule; the real facility stays a pre-release smoke test.

*Done when:* the parity test is green on the fixture with every difference
listed, and on Facility A each remaining difference falls in a listed
category.

### Phase 2 — maps

- `aggregate` → `aggregate_points`, then `rollup_points` where a location has
  several points. **The panel asks how to roll up** when it must (decision 2);
  a location with one point is unaffected.
- Layer fields gain the provenance: `method_code`, `selected_by`,
  `limit_source`, `n_points`, `matrices`, `nd_policy`. The layer's stamped
  scope (`_stamp_scope`) adds the policy and the rollup, so a saved project
  says how its numbers were made.
- **Guards become ChemDuck's.** The unit guard calls the macro and shows its
  refusal; the fraction guard stays (a layer is one fraction) but asks
  ChemDuck; multi-matrix layers use `p_matrices`, so ChemDuck's class guard
  replaces this plugin's unit-only one. CLAUDE.md's "matrix families are
  deliberately NOT modelled" changes with it, since ChemDuck models them.
- The non-detect picker maps to a policy and display (decision 1).
- `at_limit` is derived in the adapter: a selected non-detect (`detected` is
  false, with a limit) is at its limit, as today.
- Pickers read ChemDuck's catalogue (upstream request 6): **variables split by
  method class**, so "pH (field)" and "pH (lab)" are two choices.

*Done when:* the parity test is green; on Facility A a map built for a fixed
list of analytes and both aggregations matches the adapter's answer cell for
cell; `test_offline` and `test_parquet_source_ui` pass under QGIS with the
new fields; a refusal for mixed units and one for mixed matrix classes are
seen on the status line.

### Phase 3 — time series, sparklines, roll, location click

- `series`, `series_many` → the time-series macro (upstream request 4), one
  query for many locations, as `series_many` already is.
- The dock plot and sparklines keep `timeseries._is_detected`'s rule (at
  limit beats the flag), now fed from ChemDuck's detect and limit.
- A location with several points plots one series per point, labelled by
  position or depth, instead of interleaving them.
- On the parquet path, changing the non-detect choice re-plots the picked
  location (today it doesn't, `dialog.py:6148`).

*Done when:* the time series for a fixed set of locations equals the macro's
answer; a profile station plots separate series per depth; sparklines and the
roll draw from the adapter.

### Phase 4 — the Analysis tab

- `build_pivot` → `sample_cell_maps` (after upstream requests 1, 2, 8). Cell
  keys gain method class; the pivot's column names follow
  (`<analyte>_<fraction>_<methodclass>`), and the page's SQL builders read the
  same four columns per variable they read today.
- The plugin's `QC_SUFFIX` stopgap goes when the source mapping's `qc_rules`
  are active, which waits on the data owner's answers. Until then the
  plugin's mapping declares the same rule, so there's still one place.
- The histogram reads ChemDuck's per-sample results instead of `v_results`,
  with case-insensitive filters.
- Box plots: Kaplan–Meier quartiles from ChemDuck (upstream request 5); the
  page's `censored.js` retires, and its test moves to ChemDuck.
- The scatter map's per-location picks (`bivariate.picks_sql`) become a
  `rank_across` pick in ChemDuck, so the scatter and the main map rank
  samples the same way.

*Done when:* `test_scatter_sql`, `test_ternary`, `test_analysis`,
`test_bivariate` and the web tests pass against ChemDuck-built pivots;
`tools/try_overlay.py` passes in QGIS.

### Phase 5 — retire the copies

Following CLAUDE.md's rule for deleting old code:

- `ReportTable`'s rules (`row_value`, `aggregate`, `series`, `rows_for`)
  come out once no caller uses them, and `ReportTable` stays only as the
  parser of report CSV into rows.
- `tools/test_store_parity.py` is retargeted: the adapter against ChemDuck's
  fixture answers, with the same mutation discipline (deliberate rule breaks
  must fail it).
- `pipeline/arcgis_export.py` stops holding its own non-detect rules: it
  reads ChemDuck's `results` view once the pipeline has DuckDB (phase 6), and
  `tools/test_arcgis_parity.py` compares it to ChemDuck.
- CLAUDE.md's domain-rules section is rewritten to point at ChemDuck as the
  authority, keeping each rule's measured reason.

*Done when:* one copy of the non-detect rules exists, and the parity gates
that replaced the old ones have each been mutation-checked.

### Phase 6 — the pipeline publishes ChemDuck's sidecars

- `pipeline/run_nightly.py` gains a step after repacking and before upload:
  ChemDuck `publish` writes the sidecars, the ChemDuck SQL bundle and its
  `SOURCE.json` beside `manifest.json`. The manifest lists them.
- The pipeline gains `duckdb` and `pyyaml` as dependencies, and ChemDuck,
  vendored at the plugin's pinned version so a published facility and the
  plugin reading it agree.
- Units: the pipeline's collect-time conversion (`unit_convert.py`, EQuIS's
  factors) and ChemDuck's load-time conversion must not both convert. The
  pipeline's conversion stays the source (it uses EQuIS's own factors), and
  ChemDuck records the result `as_reported`. A test holds them to that.

This can run alongside phases 2–4. Phase 1's local fallback covers facilities
published before it.

*Done when:* a nightly run publishes sidecars that `open_consumer` accepts,
and `test_run_nightly.py` checks the step and its failure stops.

## Decisions to take before phase 2

1. **What the non-detect picker means.** ChemDuck's default shows a selected
   non-detect at its limit and uses Kaplan–Meier for statistics; the plugin
   today offers ½ DL, full limit, or excluded. Recommended: the map shows
   non-detects **at the limit, marked as non-detects**, which is the
   response's rule; "half limit" stays a display option (upstream request
   7); "hidden" filters non-detects out of the layer. Statistics are
   Kaplan–Meier, said on the layer.
2. **The rollup for locations with several points.** ChemDuck has no default,
   by design. Recommended for the map: ask, once per layer, only when the
   selection has multi-point locations, offering "highest (screening)", a
   named depth or position, or "each point"; remember the answer per
   project.
3. **Policy per project.** Recommended: `chemduck_default`, recorded on every
   layer; a project may name another in its settings.
4. **DuckDB becomes required.** Recommended: yes. The `ReportTable`-only mode
   can't carry ChemDuck's rules, and every supported QGIS has DuckDB either
   in its Python or in the plugin's bundle.
5. **Field replicates in maps.** Today the map's maximum counts them and the
   pivot drops them (pinned by `test_pivot.py:170-177`). With ChemDuck's
   `['N']` scope and the source's QC rules, both drop them. Recommended:
   accept, and say so in the release notes.

## Risks

- **ChemDuck is 0.x**, "stable within a minor version". Vendor at a pinned
  commit; upgrade deliberately, with the parity test.
- **DuckDB version coupling.** ChemDuck's `SUPPORTED_DUCKDB` must include
  the DuckDB the plugin runs on, which now depends on QGIS's Python as well
  as the plugin's bundle. The new QGIS beta may move it.
- **Interactive speed.** The footer count and the pickers answer on every
  click today in tens of milliseconds. ChemDuck's macros do more (reruns,
  same-day, events, provenance). Measure `aggregate_points` and the
  catalogue views on Facility A before phase 2, against the numbers in
  CLAUDE.md; ask ChemDuck for a cheaper count if needed.
- **Cursors.** Macros bind tables in the caller's schema, so every call goes
  through `schema_cursor(con, "chemduck")`; and CLAUDE.md's pending-result
  rule (a pending result hides other connections' tables) applies to that
  cursor too.
- **Position and QC rules wait on the data owner** for Facility A. Until
  then, positions in that facility stay unknown and are treated as one point,
  as today, and the rollup question doesn't arise there.

## Order, at a glance

| Phase | Needs from ChemDuck | Parallel with |
| --- | --- | --- |
| 0 Fix today's defects, vendor ChemDuck | — | — |
| 1 Shadow, parity as a gate | 3 (report rows) | 6 |
| 2 Maps | 6, 7 | 6 |
| 3 Time series and sparklines | 4 | 4, 6 |
| 4 Analysis tab | 1, 2, 5, 8 | 3, 6 |
| 5 Retire the copies | — | — |
| 6 Pipeline sidecars | — | 1–4 |
