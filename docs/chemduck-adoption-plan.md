# Adopting ChemDuck: the plan

**Written 2026-10-28**, after surveying ChemDuck at `278c29b` (package
0.2.0, shared rules D1–D7 implemented) and this plugin on `overlay-shell`.
**Updated the same day** for ChemDuck `dca591a`, which answered every
request this plan raised (one bug, eight gaps, six divergences). ChemDuck's
`docs/qgis-plugin-requests-changelog.md` has the item-by-item record, and
the item numbers below (B1, G1–G9, D1–D6) are its numbers. **Updated
2026-10-29** for `da0d3b5` (G9, `km_quartiles_of`): nothing in this plan now
waits on ChemDuck.
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

ChemDuck at `da0d3b5` (still package 0.2.0; `SUPPORTED_DUCKDB` 1.5.4 and
1.5.5):

| Plugin need | ChemDuck |
| --- | --- |
| One value per location (map) | `aggregate_points` (one row per measurement point) and `rollup_points` (one per location, by a named rule: `max_over_points`, `named_position`, `none`); `p_locations` narrows either |
| Most recent / maximum | `p_event_agg := 'most_recent' \| 'maximum'`; `'statistic'` for Kaplan–Meier mean or median |
| Non-detect handling | Named, versioned policies (`chemduck_default` and four others); `rank_across` / `rank_reruns`; `p_display_factor` draws a selected non-detect at another factor of its limit for one call, never changing which result is selected (G7) |
| Row scope | `p_start_date`, `p_end_date` (whole days, inclusive), `p_sample_types` (NULL means normal samples, typed `N` or untyped), `p_reportable_only` (NULL means the dataset's choice: on where the source carries `REPORTABLE_RESULT`) (G8) |
| A filter the source can't express | Matches everything, and every row's `filters_ignored` names it (D6), the plugin's own rule |
| Fractions | Every fraction its own row; `p_fraction` to pick one |
| Mixed units | Converted at load or publish; `p_units := 'refuse'` by default, with an error naming units and counts; per variable, matrix included, in the per-sample macros too (B1) |
| Combining matrices | `p_matrices`, guarded by matrix class, unit and measurement basis, on the aggregates and the per-sample macros alike, through one shared check (`matrix_combine_class`) (G2) |
| Per-sample (scatter, ternary) | `sample_results`, `sample_cell_maps` (one map per sample, the approach this plugin measured fastest), both taking `p_method_class` and `p_matrices` (G2); `sample_results_refused` lists what the same call left out, with units, counts and reason (G1) |
| Time series | `point_series`: one row per sample of each measurement point, re-runs resolved by `aggregate_points`' own code, ordered by point then event, for a list of locations in one call (G4) |
| Box plots | `km_quartiles(analytes, p_group := 'none' \| 'location' \| 'year', ...)`: Q1, median, Q3, each with its "below the lowest detect" flag, plus F0, counts, min/max detect, max limit and `n_left_out`. The plugin's `censored.js`, ported, with `test_censored`'s cases (G5). `km_quartiles_of(p_rows)`: the same figures over rows the caller names, through `query_table` (a table, a view or the caller's CTE of `grp, value, detected`), for the brushed box plot; `km_quartiles` runs on it, so the arithmetic is written once (G9) |
| Catalogue | `v_analyte_summary` per method class with `n_samples`; `v_method_classes`; `v_positions`; all three in the consumer bundle and published as sidecars (G6) |
| Provenance | `sample_id`, `method_code`, `selected_by` (ending in the tie-break when one date has no time of day, D4), `rerun_selected_by`, `limit_source`, `limit_type` (D3), `display_factor`, `nd_policy`, `nd_rule`, `n_points`, `matrices`, `unit_source`, `n_unknown_detect`, `filters_ignored` |
| Install | `open_consumer(con, folder, partitions, schema=...)` for published sidecars; `load_source(con, source, mapping, schema='chemduck')` for rows the host holds, as a path or in memory (a DuckDB relation from any connection, a DataFrame, an Arrow table), by the same rules and refusals as `load_to_duckdb` (G3). Both refuse to replace objects they didn't create; macros are called through `schema_cursor(con, schema)` |
| Load report | `load_report` adds results not reportable, and results dated after the load with the latest date (G8) |
| Dependencies | `duckdb>=1.5.4`, `pyyaml>=6.0`; no extensions. QGIS 4.2.1's Python has both (DuckDB 1.5.4, PyYAML 6.0.3) |

## The upstream requests, answered

Every request the first version of this plan raised is in ChemDuck, test
first, each fix reverted once to see its test fail (29 reversions). Two
divergences were settled the other way, with ChemDuck's code kept and its
response amended:

- **D1, `p_nd_method`** still runs ChemDuck 0.1's aggregate unchanged,
  because Chemrooms' maps were drawn with it. The plugin never calls it: the
  adapter names a policy (`p_nd_policy`), always. Rows from the old path now
  say so in `nd_rule`.
- **D2, no refusal on a double halving**: no rule values a non-detect from
  its reported value, so no query exists to refuse. The export's multiplier
  is recorded (`nd_value_multiplier`, 1.0 on every non-detect with a limit
  at ChemDuck's test facility).

And one behaviour change to plan for: **`v_analyte_summary` and its sidecar
now have one row per method class**, so a picker reading it offers field and
lab pH as two variables. That is what phase 2 wants.

## The brushed box plot, settled

The linked box plot computes over the rows the other charts' brush leaves
(`kmBoxSqlOver` on `lk_<n>_shown`), which no scope parameter expresses, so
without a table-taking form the page would have kept its own Kaplan–Meier
arithmetic. Asked of ChemDuck 2026-10-28
(`docs/qgis-plugin-request-km-over-rows.md` there), built as G9.

**Measured 2026-10-29** (DuckDB 1.5.5, Windows 11, this machine; scratch
probe, not yet a repository test). The page's `kmBoxSqlOver` and
`chemduck.km_quartiles_of` were run over the same rows: 5,559 synthetic
samples in 400 locations, log-normal and gridded values (so ties),
censoring from none to 95%, and 156 unknown-detect rows. Every column of
every group was identical (n, counts, min and max detect, max limit, F0, the
three quartiles and their undetermined flags), for each of 3 groupings x 3
brushes (up to 398 groups, 127 with Q1 undetermined). The call had the shape
the page will send: the brushed rows as a CTE, the macro, by its qualified
name, in a sibling CTE of the same statement, from a cursor in `main`. Both
took tens of milliseconds. The page's figure includes the whiskers, so the
two times aren't the same work and aren't compared. DuckDB 1.5.4 wasn't
re-run: QGIS 4.2.1 had been uninstalled for the beta. ChemDuck's own suite
covers 1.5.4 and WASM 1.5.4.

Whiskers and Tukey fences stay the page's. They are drawing, not a chemistry
rule, computed from ChemDuck's quartiles and the detected values in the same
statement.

## What is still open

One is a ChemDuck defect, found by the parity test: item 5.

1. **The QGIS's DuckDB.** *Checked 2026-10-29:* QGIS 4.2.3, which replaced
   4.2.1 here, has DuckDB 1.5.5, in ChemDuck's `SUPPORTED_DUCKDB` (1.5.4,
   1.5.5). `tools/test_chemduck_vendor.py` is the check, to run under every
   QGIS the plugin supports.
2. **Interactive speed: too slow for a click on 2026-10-29; within budget
   at 4 threads on 2026-11-05, open included** (re-measured below; one
   location's series is just over). *Measured
   2026-10-29* by `tools/measure_chemduck_calls.py` on Facility A, ChemDuck
   opened on its sidecars beside the plugin's store, in QGIS 4.2.3's Python
   (DuckDB 1.5.5). Medians of three warm runs. The machine was noisy, so
   read the ratios:

   | Question | Plugin today | ChemDuck |
   | --- | --- | --- |
   | Map, one analyte (3 cases, both aggregations) | 0.17–0.33 s | 4.2–8.1 s |
   | Footer count | 0.07–0.18 s | 5.3–8.4 s |
   | One location's time series | 0.07–0.11 s | 2.7–5.3 s |
   | Every location's series (sparklines) | 0.20–0.62 s | 5.7–17.3 s |
   | Box plot by location (`km_quartiles`) | on the page | 1.7–2.7 s |
   | Pivot, two analytes, WS | 1.2 s | 1.5 s |
   | Pivot, every analyte, TAILS | 1.5 s | 6.5 s |
   | Catalogue (sidecar `analyte_summary`) | 0.17–0.22 s | under 0.01 s |
   | Load a 557,691-row report | 38 s | 89 s |
   | Open the source | 0.27 s | 40.5 s (5.4–8.1 s in plain Python, same day) |

   **The time is in the macros, not the data.** ChemDuck's `results` view
   filters one analyte in 0.06 s, and `v_results_denormalized` in 0.26 s.
   `aggregate_points` over the same rows takes 3.65 s. DuckDB's profiler
   puts most of it in four `GROUP BY`s over at most 63,000 rows, at 1.7–4.8 s
   each, and a 1.27 s moisture lookup over every result on every call. The
   same aggregate mix over synthetic rows takes 0.03–0.24 s. **Raised
   2026-10-29** with these numbers and budgets: map ≤ 0.5 s, count ≤ 0.2 s,
   one location's series ≤ 0.2 s, sparklines ≤ 1 s, open ≤ 2 s. It is in
   ChemDuck's `docs/qgis-plugin-request-interactive-speed.md`.

   **A cap on DuckDB's threads helps both sides.** ChemDuck's
   `docs/duckdb-thread-cap.md` found its macros fastest at `threads = 4`:
   about thirty pipeline breaks per call, each paying per-thread setup and
   waiting on the slowest thread. The setting belongs to the whole
   database, so ChemDuck in a schema of the plugin's connection can't be
   capped alone. So the plugin's own calls were timed under the cap too.
   `measure_chemduck_calls.py --threads`, Facility A, plain Python 3.14,
   DuckDB 1.5.5, 2026-10-29, machine busy at about 58% CPU. Runs at 4,
   then 14, then 4 threads, each the median of three warm runs:

   | Question | Plugin, 4 / 14 / 4 | ChemDuck, 4 / 14 / 4 |
   | --- | --- | --- |
   | Map, one analyte (3 cases, both aggregations) | 0.05–0.12 / 0.09–0.25 s | 0.67–1.56 / 2.94–4.78 s |
   | Footer count | 0.02–0.06 / 0.04–0.10 s | 1.00–1.54 / 3.66–5.28 s |
   | One location's series | 0.03–0.05 / 0.05–0.08 s | 0.62–1.26 / 2.74–3.25 s |
   | Sparklines | 0.10–0.32 / 0.14–0.37 s | 1.90–6.30 / 5.30–12.30 s |
   | Pivot, two analytes, WS | 0.36, 0.51 / 0.83 s | 0.43, 0.50 / 1.12 s |
   | Pivot, every analyte, TAILS | 0.53, 0.66 / 1.14 s | 1.63, 1.75 / 4.94 s |

   Taking 14 threads' time over 4 threads' time, call by call:
   - **First 4-thread run:** the plugin's 17 calls were 1.06–3.64× faster
     at 4 threads (median 2.05), none slower. ChemDuck's 23 were 2.35–5.34×
     faster (median 4.0).
   - **Second 4-thread run, which overlapped another test:** the plugin's
     median was 1.63×. Three of its calls were slower than at 14 threads, by
     at most 0.022 s. ChemDuck's were 1.95–5.07× faster.

   At 4 threads ChemDuck is still over the budgets: a map in 0.7–1.6 s
   against 0.5, and a count in 1.0–1.5 s against 0.2. On the parity
   fixture, 36 results as it then was, one `aggregate_points` call took
   0.11 s on one thread and 0.41–0.85 s on fourteen. That's a per-call
   floor with no rows to speak of, sent to ChemDuck for the speed work.

   *Decision 8 below* takes the cap: one setting, in one place, because
   it governs every query on the connection.

   **Re-measured 2026-11-05, after ChemDuck's speed work: within budget
   at 4 threads.** The open, first measured here as the one miss, is
   covered at the end of this block. The vendored copy moved from
   `da0d3b5` to `daddfb6`, which merges ChemDuck's PRs #12 and #13:
   - #12: lean engine scans off the moisture lookup, `point_counts`, one
     matrix-class read, and cached parquet footers.
   - #13: collapse stages that skip events of one result, and a grouped
     unit check.

   The parity test is green on it, and old and new return the same row
   counts on every call. Same laptop, plain Python 3.14.6, DuckDB 1.5.5,
   `measure_chemduck_calls.py --skip-load`. The script now also records
   process CPU beside wall time. Other jobs held the CPU at 0–75% and
   moved it within runs. So the two pins ran alternately, three pairs at
   4 threads, one pair in reverse order. In the reversed pair the new pin
   ran under 52–75% load and the old under 4–30%, and the new was still
   1.3–2.3× faster on wall time and 1.3–2.1× on CPU. Medians of the three
   runs' warm medians, at 4 threads:

   | Question | Budget | Plugin | ChemDuck `da0d3b5` | ChemDuck `daddfb6` |
   | --- | --- | --- | --- | --- |
   | Map, one analyte (3 cases, both aggregations) | 0.5 s | 0.03–0.04 s | 0.45–0.73 s | 0.26–0.35 s |
   | Footer, through `point_counts` (new) | 0.2 s | 0.02–0.03 s | (no macro) | 0.08–0.09 s |
   | Footer, a map call counted | | | 0.50–0.67 s | 0.27–0.33 s |
   | One location's series | 0.2 s | 0.02–0.03 s | 0.40–0.50 s | 0.21–0.22 s |
   | Sparklines | 1 s | 0.05–0.07 s | 0.69–1.20 s | 0.44–0.62 s |
   | Box plot by location (`km_quartiles`) | | on the page | 0.23–0.30 s | 0.14–0.18 s |
   | Pivot, two analytes, WS | | 0.24 s | 0.30 s | 0.15 s |
   | Pivot, every analyte, TAILS | | 0.40 s | 1.26 s | 0.53 s |
   | Open the source (plain Python) | 2 s | 0.06–0.10 s | 1.81–3.16 s | 1.71–1.74 s |

   - **Against the budgets at 4 threads:** a map, a footer through
     `point_counts` and sparklines are within. One location's series is
     just over, at 0.21–0.22 s against 0.2.
   - **The plugin is still faster:** about 7–10× on a map and on one
     series, 3–5× on a footer.
   - **Gains per call:** 1.3–2.4× on wall time, 1.3–1.9× on CPU.
   - **ChemDuck's own CPU is still over its wall time:** 0.58–0.86 s of
     CPU for a 0.26–0.35 s map at 4 threads.
   - **Default threads (14), one pair:** the new pin's maps took
     1.26–1.65 s, over budget. One call, the most-recent Arsenic map, was
     slower than the old pin's (1.38 against 1.16 s), within this
     machine's noise. The cap is still what brings calls within budget.
   - **In QGIS 4.2.3's Python** (3.12.15, DuckDB 1.5.5; the new pin at 4
     threads, load 35–68%), calls take about what they take in plain
     Python: maps 0.25–0.41 s, `point_counts` 0.08–0.09 s, one series
     0.20–0.23 s, sparklines 0.39–0.62 s.
   - **Opening there took 18.6 s** at `daddfb6`, against 1.7 s in plain
     Python and 40.5 s on 2026-10-29.

   **The open, taken apart the same day** with `tools/profile_chemduck.py`,
   which times each install statement:
   - **The cause.** 17.9 of the 18.9 s was one statement, the open's
     first with a bound parameter. ChemDuck registered each object it made
     with `INSERT OR IGNORE INTO chemduck_objects VALUES (?, ?)`. DuckDB's
     Python client imports numpy, pandas and pyarrow on the first
     parameter bound on a connection: `SELECT ?` alone took 1.8 s in plain
     Python and 13.5 s in `python-qgis.bat` with them unimported, and 1 ms
     after. ChemDuck's measurement put the rest on a cold read of those
     packages' files, once per boot.
   - **Fixed in ChemDuck PR #14** (`2986b3f`, merged as `e7139bd`, now
     vendored). The open binds no parameter and writes the registry in two
     statements; 207 statements became 115. Through the harness in
     `python-qgis.bat`, 4 threads: the open takes 0.84–0.96 s, and a second
     open in the same process 0.84–0.91 s.
   - **That moves the import; it doesn't remove it.** The session's first
     bound parameter pays it, and both the store's map and the adapter's
     `points` bind. In `python-qgis.bat` the first `chem.points` took
     2.86 s and the second 0.35 s, with file caches warm. Until this was
     found, the harness hid the cost: it bound parameters itself between
     the open and the session. It now names the step that paid.
   - **Real QGIS doesn't pay it.** QGIS 4.2.3 started with a fresh profile
     holding only its core plugins (MetaSearch, db_manager, grassprovider,
     processing) had all three imported by the time the GUI was up. A
     first `SELECT ?` there took 0.8 ms. So the 18.6 s, and probably the
     40.5 s, belonged to the bare interpreter, not to a user of the plugin.
     Not checked: a profile with Processing disabled.

   **What this changes.** Phases 2 and 3 wait on that request; they are
   what puts these calls on a click. What moves without waiting:
   - the catalogue, read from the published sidecars, never the live views;
   - the two-analyte pivot;
   - the brushed box plot, whose macro reads rows the page already holds;
   - the parity work, which is about the rules, not speed.

   **Found by the same run, for the adapter:**
   - **Fractions are named in ChemDuck, coded in the plugin.**
     `Total`/`Dissolved`/`N` against `T`/`D`. The adapter translates.
   - **Most of Facility A's methods are unclassified.** Arsenic in WS Total
     is two variables: method class `UNK` (51,906 results at 96 locations)
     and `lab` (11,061 at 123). A map call without `p_method_class` returns
     a point per class. Phase 2 must choose the class, or ask. The data
     owner's method classes, or ChemDuck's seed, decide which codes are
     `lab` and which `field`.
   - **Publishing Facility A's sidecars took 57.7 s,** against 8.8 s on
     2026-10-22 before the catalogue sidecars and the newer rules. That is a
     pipeline step (phase 6), so it isn't a click. It is noted for that
     phase.
   - **`load_source` from a relation needs pyarrow** (`relation.arrow()`).
     QGIS 4.2.3's Python has pyarrow 25.0.1 and pandas 3.0.6. The plugin's
     own DuckDB bundle carries arro3, not pyarrow. So a QGIS that relies on
     that bundle can't load report rows this way unless pyarrow is bundled
     too, or the rows go through a file.
3. **`spike_chemduck/host.py` still imports ChemDuck's old `tools/` path**,
   not the `chemduck` package. *2026-10-29:* `measure_chemduck_parity.py`,
   the other one, is gone, replaced by `tools/test_chemduck_parity.py` on
   the vendored package. The spike is a record, so it stays as it was.
4. **`load_schema` prints a line per SQL file** ("Loading 03_views.sql...").
   In QGIS that goes to the Python console's stdout. The adapter
   (`chem._quiet`) silences it until ChemDuck logs through `logging`.
   That's a small request, not a blocker.
5. **A sample with no date, or no matrix, stops ChemDuck's load.** *Found
   2026-10-29* by the parity fixture. `insert_samples` drops such a sample,
   but `insert_results` still inserts its results, so the foreign key
   refuses the whole load with a message naming only a sample id. The
   plugin maps both rows. Facility A has neither, so the case is the live
   report path's. It has been raised in ChemDuck's
   `docs/qgis-plugin-request-load-undated.md`, and the parity test records
   it as a known refusal that fails once the load succeeds.
6. **No depth or unit filter on `aggregate_points`.** *Found 2026-11-05*,
   when the plugin's row scope gained both (Facility A's sediment: depths
   in cm beside m, and 3,401 leachate results in mg/L beside 635,904 in
   mg/kg; CLAUDE.md has the rules). ChemDuck takes dates and sample types
   but neither of these. The adapter holds the five-part scope and
   refuses both by name (`Chem.points`), and the parity test checks that
   it refuses. It can't filter its answer afterwards:
   - **Units.** A point's winning row is chosen across every unit, so
     dropping a unit afterwards leaves a winner the store never had.
   - **Depth.** `depth_units` is kept but never converted, so a bound in
     metres can't be read against points written in cm.

   To raise with ChemDuck: a whole-interval depth bound in one unit,
   converted by exact length factors, with no-depth rows out, and a list
   of result units to keep. Both apply before selection. Needed before
   phase 2, when the panel's map comes from ChemDuck.

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
  published sidecars; report rows: `load_source` given a relation over the
  store's own table, so the rows aren't copied through Python);
- holds the row scope (dates, sample types, reportable only) and passes it
  to every call, so `_apply_row_scope` keeps setting it in one place;
- maps the panel's choices to macro parameters (aggregation, policy,
  display factor, fraction, matrix or matrices, method class, rollup);
- turns ChemDuck's refusals, `sample_results_refused`'s rows and
  `filters_ignored` into the panel's status-line messages;
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

Then: `tools/vendor_chemduck.py` vendors ChemDuck at a pinned commit (`da0d3b5`
or later) into the plugin, and a test checks the pinned version, `SUPPORTED_DUCKDB` against the
running DuckDB, and the bundle's checksum.

> *Done 2026-10-29:* ChemDuck 0.2.0 at `da0d3b5` is committed as the
> subpackage `equis_loader.chemduck`. It's a subpackage so that another ChemDuck
> a user has installed can't answer for it. The copy has 17 files, 372,921 bytes,
> about 100 KB in the zip. The files are the commit's own bytes, read with
> `git cat-file`, kept unconverted by `.gitattributes`, and hashed in
> `VENDORED.json`. `tools/test_chemduck_vendor.py` checks the hashes and that
> no file was added. It also checks the version, the absence of QGIS imports, the
> running DuckDB against `SUPPORTED_DUCKDB`, an install into schema `chemduck`
> with extensions off and nothing left in `main`, a `km_quartiles_of` answer,
> packaging, and provenance against the checkout. A hand-edited SQL file
> and an added file each fail it. It passes under plain Python (DuckDB
> 1.5.5) and under QGIS 4.2.3 (Python 3.12.15, DuckDB 1.5.5, PyYAML 6.0.3,
> Qt 6.11.0), 2026-10-29. Under QGIS the provenance step skips, because git
> isn't on `python-qgis.bat`'s PATH.
>
> The five defects' QGIS tests ran in 4.2.3 the same day, with a scratch
> settings folder (`QGIS_CUSTOM_CONFIG_PATH`; the real profile's
> `QGIS4.ini` was unchanged): `test_offline` 248/248, `test_import_smoke`
> 19/19, `test_parquet_source_ui` 18/18. With the export reading
> `_analysis` again, `test_offline`'s new step fails with the StoreError.

*Done when:* each defect has a failing test first, then a fix;
`tools/test_offline.py` and `tools/test_parquet_source_ui.py` pass under
QGIS; the plugin zip carries ChemDuck and the version test passes.

### Phase 1 — ChemDuck alongside, answering nothing yet (shadow)

- The adapter opens ChemDuck on both paths. Parquet: sidecars published next
  to the manifest (phase 6), or built locally in a worker if absent
  (8.8 s for Facility A). Reports: `load_source`, in the worker that builds
  the store today.
- Nothing in the UI changes. A debug setting logs, for each map built, the
  adapter's answer beside `ReportStore.aggregate`'s.
- `tools/measure_chemduck_parity.py` moves to the `chemduck` package and
  the shared-rules path (`aggregate_points`, not 0.1's `aggregate_results`),
  and becomes a test that compares the adapter to the store. It holds every
  difference to an **intended differences list**: positions and depths kept
  apart, events by timestamp, methods selected and reported, the
  quantitation limit reached, ties to the later date, non-reportable results
  left out. A difference with no entry fails.
- **Measure the interactive calls** (open item 2): `aggregate_points` for a
  map, `point_series` for one location and for a layer's sparklines,
  `sample_cell_maps` for a pivot, `km_quartiles`, the catalogue views, and
  `load_source` at live-report scale. Each is set beside the plugin's
  number for the same answer today, on Facility A in QGIS's Python, with the
  date and machine. *Done 2026-10-29* (open item 2): 10–50× slower on every
  click-time call, raised with ChemDuck.
- A **shared fixture facility**, synthetic and in this repository, exercises
  every rule; the real facility stays a pre-release smoke test.

*Done when:* the parity test is green on the fixture with every difference
listed; on Facility A each remaining difference falls in a listed
category; and the timings are recorded, with any call too slow for a click
raised with ChemDuck.

> *Parity, done 2026-10-29.* The pieces:
> - **The adapter**, `equis_loader/chem.py` (no QGIS). It opens ChemDuck in
>   schema `chemduck` of the store's connection, by `load_source` or by
>   `open_consumer`. It holds the row scope as the store does, and answers
>   the map's question as `aggregate_points` points carrying the store's
>   keys.
> - **The fixture**, `tools/chem_fixture.py`: 26 locations, each built to
>   show one rule and declaring what each aggregation should then do.
> - **The test**, `tools/test_chemduck_parity.py`. It replaces
>   `measure_chemduck_parity.py`, and holds every difference to 13 intended
>   causes. Each cause is checked against the rows, not inferred from the
>   answer's shape.
>
> On the fixture, both ways of opening ChemDuck give the same points, and
> every location differs exactly as it declares. A date window and a
> sample-type scope reach both sides. Nine deliberate rule breaks each fail
> it: five in the adapter, three in the store, and the adapter passing
> `p_units := 'flag'`. It passes under plain Python and under QGIS 4.2.3,
> in 27–37 s on one thread.
>
> **Facility A**, the 12 most-measured analytes, 14,737 cells per recipe:
> every difference falls in a listed cause (888 s at 4 threads). Most
> recent, at both non-detect choices:
> - agree: 7,352;
> - points (depths and positions kept apart): 3,459;
> - same date: 2,604;
> - method classes: 1,052;
> - units refused by both guards: 477;
> - units converted by ChemDuck: 98;
> - units by method class: 40.
>
> Maximum differs from that in three places: agree 9,486; tie to the later
> date 439; no same-date difference.
>
> Rules Facility A doesn't exercise appear only in the fixture: no
> non-reportable result, no QC sample type, no quantitation-limit-only
> non-detect, no undated sample.
>
> **Found by it:**
> - ChemDuck's load refuses a sample with no date or no matrix (open item 5);
> - ChemDuck converts units at load (decision 9);
> - ChemDuck refuses a solid's unit on water even when it's the variable's
>   only unit;
> - a call costs 0.11 s on one thread and 0.4–0.85 s on fourteen, even on 40
>   rows (decision 8).
>
> *Still to do in this phase:* the debug log of adapter beside store for
> each map built, and the report-rows path (`Chem.load` in the worker that
> builds the store).

### Phase 2 — maps

- `aggregate` → `aggregate_points`, then `rollup_points` where a location has
  several points. **The panel asks how to roll up** when it must (decision 2);
  a location with one point is unaffected.
- Layer fields gain the provenance: `method_code`, `selected_by`,
  `limit_source`, `limit_type`, `display_factor`, `n_points`, `matrices`,
  `nd_policy`. A non-empty `filters_ignored` goes on the status line, as the
  plugin's "a filter on a column the report lacks" rule says. The layer's stamped
  scope (`_stamp_scope`) adds the policy and the rollup, so a saved project
  says how its numbers were made.
- **Guards become ChemDuck's.** The unit guard calls the macro and shows its
  refusal; the fraction guard stays (a layer is one fraction) but asks
  ChemDuck; multi-matrix layers use `p_matrices`, so ChemDuck's class guard
  replaces this plugin's unit-only one. CLAUDE.md's "matrix families are
  deliberately NOT modelled" changes with it, since ChemDuck models them.
- The non-detect picker maps to a policy and `p_display_factor`
  (decision 1).
- `at_limit` is derived in the adapter: a selected non-detect (`detected` is
  false, with a limit) is at its limit, as today.
- Pickers read ChemDuck's catalogue (`v_analyte_summary`,
  `v_method_classes`, `v_positions`): **variables split by method class**, so
  "pH (field)" and "pH (lab)" are two choices.

*Done when:* the parity test is green; on Facility A a map built for a fixed
list of analytes and both aggregations matches the adapter's answer cell for
cell; `test_offline` and `test_parquet_source_ui` pass under QGIS with the
new fields; a refusal for mixed units and one for mixed matrix classes are
seen on the status line.

### Phase 3 — time series, sparklines, roll, location click

- `series`, `series_many` → `point_series` with `p_locations`, one query
  for many locations, as `series_many` already is. Phase 1's timing decides
  whether a location click can call it directly.
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

- `build_pivot` → `sample_cell_maps`, with `p_method_class` or `p_matrices`
  as the panel chose. Cell keys gain method class; the pivot's column names
  follow (`<analyte>_<fraction>_<methodclass>`), and the page's SQL builders
  read the same four columns per variable they read today. What the pivot
  lists as left out comes from `sample_results_refused` with the same
  parameters.
- The plugin's `QC_SUFFIX` stopgap goes when the source mapping's `qc_rules`
  are active, which waits on the data owner's answers. Until then the
  plugin's mapping declares the same rule, so there's still one place.
- The histogram reads ChemDuck's per-sample results instead of `v_results`,
  with case-insensitive filters.
- Box plots: Kaplan–Meier quartiles from ChemDuck, both kinds. The page's
  box SQL becomes a `km_rows` CTE over the linked table (`grp`, `value`,
  `detected`, the brush's predicates), `chemduck.km_quartiles_of('km_rows')`,
  and the whisker step joined to it. `censored.js` keeps `GROUPS` (labels
  identical to `km_quartiles`' `p_group`), the fences and `boxSummary`, and
  loses the estimator.
- `tools/test_censored.py` keeps its independent estimate, which is a test,
  not a copy of the rule. It is retargeted: ChemDuck's quartiles, through
  the page's SQL, against that estimate, plus the hand cases and Facility A.
  The 2026-10-29 probe above becomes its comparison of the page against
  `km_quartiles_of`, kept until the old estimator is deleted.
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
   response's rule; "half limit" stays a display option
   (`p_display_factor := 0.5`, which changes the drawn value and never the
   selection); "hidden" filters non-detects out of the layer. Statistics
   are Kaplan–Meier, said on the layer.
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
6. **Reportable only, by default.** The plugin reads `REPORTABLE_RESULT`
   nowhere today. ChemDuck leaves non-reportable results out by default
   where the source carries the column. That is the response's rule:
   EQuIS's own judgement of which re-run to report. Recommended: accept. A
   QC review that wants every result unticks it, and the layer's stamped
   scope says which. ChemDuck found no non-reportable results at its test
   facility, so the default changes nothing there. Facility A's count goes in
   phase 1's intended-differences list either way.

7. **Whether ChemDuck may ship with this plugin at all.** *Settled
   2026-10-29: ChemDuck is to be opened, licence to be chosen, and vendoring
   proceeds on that assumption.* Found 2026-10-29,
   at phase 0's vendoring step: ChemDuck is a private repository
   (`IntegralEnvision/chemduck`, no licence), and this plugin's is public,
   with a zip meant for distribution. Vendoring it into either publishes it.
   That decision is the owner's, not this plan's: open ChemDuck under a
   licence, or ship the plugin with ChemDuck only where it is licensed (an
   internal build that vendors it at build time, never committed here).
   Phase 0's vendoring and everything after it wait on the answer.

8. **DuckDB's thread count.** `threads` is the database's setting: `SET
   threads` on any cursor changes it for all of them, and DuckDB refuses
   `SET SESSION threads`. So it can't be ChemDuck's alone or the store's
   alone. Recommended: `ReportStore` sets `threads = min(4, the default)`
   where it opens the session's connection, and nothing else sets it. That
   is one authority, and it caps both sides, which the measurement above
   says helps both. Background builds (`BuildStoreTask`, a `load_source`
   worker) open their own databases, so this cap doesn't reach them, and
   they can keep every core. *Not yet taken:* ChemDuck's note says to
   measure again on an idle machine before settling on 4, and every figure
   so far is from a busy one. The parity fixture runs on one thread for its
   own speed: 12 s against 1 min 49 s at 14, at 21 locations.

9. **Who converts units.** CLAUDE.md says conversion belongs to the
   pipeline, with EQuIS's own factors, and never to presentation. The
   plugin's guard stops a variable in two units and asks. *Found
   2026-10-29* by the parity fixture: ChemDuck converts at load, by its own
   factors. A variable in ug/L and mg/L becomes one in mg/L, each row
   marked `unit_source = 'chemduck'`, and the map is drawn without a
   question. On Facility A the pipeline has already converted, so
   ChemDuck finds nothing to convert there (`as_reported`). On a live
   report it would. ChemDuck also refuses a unit that contradicts its
   matrix (ug/kg on water) even when it's the variable's only unit, where
   the plugin maps it. Recommended: keep the pipeline's conversion as the
   one source for parquet (phase 6 already says so), and for live reports
   accept ChemDuck's conversion, with its factor and `unit_source` on the
   layer, since a live report has no pipeline in front of it. A layer
   whose rows ChemDuck converted says so on the status line, as a
   multi-matrix aggregate does now. The parity test lists both as
   intended (`units_converted`, `unit_contradicts_matrix`) until this is
   decided.

## Risks

- **ChemDuck is 0.x**, "stable within a minor version". Vendor at a pinned
  commit; upgrade deliberately, with the parity test.
- **DuckDB version coupling.** ChemDuck's `SUPPORTED_DUCKDB` must include
  the DuckDB the plugin runs on, which now depends on QGIS's Python as well
  as the plugin's bundle. The new QGIS beta may move it.
- **Interactive speed.** The footer count and the pickers answer on every
  click today in tens of milliseconds. ChemDuck's macros do more (reruns,
  same-day, events, provenance). Its own numbers so far are seconds, not
  milliseconds (open item 2). Phase 1 measures every call on Facility A,
  before phase 2 commits to one. Ask ChemDuck for a cheaper form where one is
  needed, as its changelog shows it will: a first `sample_results` cost
  2.1x until the scope moved after the scan.
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
| 0 Fix today's defects, vendor ChemDuck | — (`da0d3b5` has everything); the beta's DuckDB checked | — |
| 1 Shadow, parity as a gate, timings | — | 6 |
| 2 Maps | — (possibly a cheaper call, from phase 1's timings) | 6 |
| 3 Time series and sparklines | — (as phase 2) | 4, 6 |
| 4 Analysis tab | — (`km_quartiles_of` landed, G9) | 3, 6 |
| 5 Retire the copies | — | — |
| 6 Pipeline sidecars | — | 1–4 |
