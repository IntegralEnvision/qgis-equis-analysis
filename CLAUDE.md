# Working on this repository

QGIS plugin that maps analytical chemistry from EQuIS Enterprise. See
[README.md](README.md) for what it does and how to install it. This file is
for whoever is changing it.

## Commands

```
python tools/test_parsing.py          # parsing, paging, aggregation, cache
python tools/test_tasks.py            # cache decisions
python tools/test_state.py            # the panel's state machine
python tools/test_store_parity.py     # ReportTable vs ReportStore (needs duckdb)
python tools/test_overlay_payload.py  # overlay data shaping
python tools/measure_pivot.py         # what a pivot must decide, on the real parquet
python tools/test_pivot.py            # one row per sample: the pivot's rules
python tools/test_scatter_sql.py       # the scatter's real SQL against real pivots (node)
python tools/test_bivariate.py         # Jenks breaks, classes and colours for the scatter's map
python tools/test_analysis.py          # the Analysis tab's chooser: catalogue, pairs, choices
python tools/test_ternary.py           # ternary shares/colours, one-unit rule, page SQL held to ternary.py
python tools/test_censored.py          # box plots' Kaplan-Meier quartiles vs an independent KM (node, duckdb)
python tools/analysis_preview.py       # the Analysis tab in a browser, no QGIS (--shots DIR: screenshots)

# The parquet export pipeline: plain Python + pyarrow, imports no plugin code
python pipeline/test_slice_pipeline.py  # pipeline bookkeeping (pyarrow)
python pipeline/test_unit_convert.py    # collect-time unit conversion
python pipeline/test_arcgis_export.py   # GeoParquet for ArcGIS Pro: files, CRS, refusals
python pipeline/test_run_nightly.py     # the unattended check: order, stops, upload
python tools/test_arcgis_parity.py      # that export's non-detect columns vs ReportStore (duckdb)
python pipeline/run_nightly.py --root data/<facility>   # one facility's check: arm on new data, rebuild what ran

python tools/vendor_deps.py --target win --python 312   # build a dep bundle
python tools/package.py                                 # plain plugin zip
python tools/package.py --bundle win_amd64-cp312        # zip with duckdb in it
python tools/install.py                                 # copy into the QGIS profile
```

Needing QGIS (run under `python-qgis.bat`, or paste into the QGIS console):

```
tools/test_offline.py   tools/test_storage.py   tools/test_roll.py
tools/test_phase0.py    tools/test_webengine.py
tools/test_interactive.py   tools/test_parquet_source_ui.py
tools/test_tickable_combos.py   tools/test_bivariate_layer.py
tools/test_analysis_task.py   (the Analysis chooser's worker, in the task manager)
tools/try_scatter.py   tools/try_analysis_tab.py   (console probes: US-24, the Analysis tab)
tools/try_overlay.py   (NOT a console probe: run by `qgis --code` in a throwaway
                        profile; drives the overlay shell end to end, 13 checks, quits)
tools/webengine_probes/run_wasm.ps1   run_gpu.ps1   (also `qgis --code`, from PowerShell:
                        WASM on a plugin page, and whether it can have a GPU)
tools/mosaic_poc/overlay.py   brush_scale.py   temporal_scale.py
```

**Scope: QGIS 4.2+ / Qt6 / PyQt6 only.** Decided 2026-09-18. Qt5 shims and
`qgisMinimumVersion` below 4.2 are dead code; `metadata.txt` still says 3.34
and should be raised when the substrate work lands (it currently gates the
shipped unbundled plugin, which does run on 3.34).

## The one structural rule

**`equis_api.py`, `report_data.py`, `sparkline.py`, `cache.py` and `store.py`
import no QGIS.** That is why 1,500+ lines of tests run under a plain
interpreter, and it is the reason most of this codebase can be verified
without a GUI. Anything needing a path takes it from the caller, as
`cache.py` does. Do not import `qgis.*` into these modules to save a few
lines elsewhere.

## Writing to the tenant

The plugin writes to an EQuIS site in exactly five ways - create, save and
delete its own report, add and remove a group membership - and the README's
"What it writes to your EQuIS site" section lists them for administrators.
Every write goes through `EquisClient._request`, which refuses all of them
when `read_only` is set; the panel reads that same flag rather than keeping
its own. A new write must go through `_request` and be added to that README
section, and the plugin calls only the REST API's documented endpoints.

## Domain rules that must not be broken

These are not style preferences. Each one exists because getting it wrong
produces a *plausible number* rather than an error.

- **A non-detect is not a small measurement.** `RESULT_NUMERIC` is empty and
  only the limit is reported, so the value falls back to
  `REPORT_RESULT_VALUE` then the detection-limit columns. Reading
  `RESULT_NUMERIC` alone silently loses them — 8 of 46 points in one real
  series. `detect` and `at_limit` are different facts; a value plotted at a
  limit is a non-detect whatever `DETECT_FLAG` says.
- **Detection comes from `DETECT_FLAG`, never from anything else.** A
  source without the column is refused (`REQUIRED_FOR_ANALYSIS` in
  `report_data.py`, read by both implementations). Without it every result
  read as a non-detect and was halved under ½ DL: Arsenic 10 and 40 µg/L
  mapped as 5 and 20, with no error (found 2026-10-08). Qualifier text is a
  lab's vocabulary, not a substitute. The scope is EQuIS and EQuIS-derived
  extractions, which carry the flag; a source without it is fixed upstream.
- **A non-detect can never be the maximum** while a real detection exists at
  that location. It carries only the limit it fell below, so `<50`
  substituted at half its limit is 25 — a number about laboratory
  sensitivity, not about the site. Found 2026-09-28: 759 (chemical, matrix,
  location) cells across 47 analytes were reporting one, the worst
  overstating a location's real maximum by **1,250x**. A location with
  *only* non-detects still reports one, marked `at_limit` — 9,660 cells are
  that, and "we looked and found nothing" is a result.
- **Never aggregate across fractions.** Dissolved and total come from the
  same water on the same day and differ by up to ~29x. The build refuses.
- **Matrices aggregate together only when their units agree; the unit guard
  is the one gate.** A soil maximum beside groundwater criteria is
  meaningless, and bulk solids (mg/kg) differ in unit from water (ug/L,
  mg/L), so that is refused. WS+WG and SE+TAILS are legitimate. Matrix
  families are deliberately NOT modelled, and units are NOT translated at the
  presentation level: conversion belongs to the pipeline, with EQuIS's own
  factors. Known gaps, accepted rather than modelled (decided 2026-10-01):
  leachate and dissolved-on-solid results share water's units (IDCU
  LEACHATE/D and WG/D are both ug/L), and pH (SU), ORP (mV) and percent
  appear on both sides. Combining those is the analyst's call, and a
  multi-matrix aggregate says so on the status line.
- **A multivariate point is one sample, never a location summary.** At 174
  of 216 multi-sample WS/T stations at Facility A, the maximum Mn and the
  maximum Pb come from different samples, so a per-location scatter plots
  combinations no sample ever had. Scatters (and every pivoted view) are
  per sample; see `docs/pivot-design.md`. A per-location scatter is
  allowed only as ONE REAL SAMPLE per location by a stated rank - most
  recent, or highest on one chosen axis with detected ranked first
  (`bivariate.picks_sql`, decided 2026-10-12) - never X's maximum beside
  Y's from another sample.
- **Two methods on one sample are real data, not duplicates.** At Facility A, 162
  of 172 intervals carry exactly two, disagreeing about detection.
- **A filter on a column the report lacks matches everything.** Dropping
  every row is a worse failure than ignoring a filter the data cannot express.
- **Every filter in the panel applies whatever the source.** Dates and the QC
  checkbox were prefetch-only, so on a parquet source (every row the facility
  has) they did nothing: a map with Dates ticked for 2015–2026 drew a 2003
  result (found 2026-10-03). They are now a row scope, set in one place
  (`_apply_row_scope`) on `ReportTable` and `ReportStore` alike. A date bound
  excludes undated rows and is whole days, inclusive.
- **Cached/offline data is marked everywhere it appears** — banner, layer
  name, message bar, report combo. A stale map that looks live is the one
  outcome worth engineering against.
- **Location codes join case-insensitively**; the report and the feature
  service do not agree on case.

## What measurement taught us

Numbers from `docs/phase0-substrate.md`, `docs/phase1-substrate-parity.md`
and `tools/mosaic_poc/README.md`. Do not re-derive them; do re-measure if you
change something they depend on.

- **Pixels are scarce; rows are not.** In 100 ms QGIS draws ~6,500 plain
  circles or 125 SVG sparkline markers; DuckDB scans ~7,400,000 rows. A drawn
  circle costs ~1,100x a scanned row. **Anything that reduces marks beats
  anything that reduces rows.** (R9)
- **Ask once for many, never many times for one.** A query costs a few ms of
  parse-plan-execute regardless of size. Looping a tiny query 300 times cost
  1.44 s where one grouped query cost 52 ms. `ReportStore.series_many()`
  exists for this reason.
- **`executemany` is unusable for bulk loading** — 6.5 minutes at report
  scale. Write a CSV and `COPY` it: ~1.1 s.
- **The 25,000-feature cap is a drawing limit, not a data limit.** It lands
  at ~400 ms of pan at 15.5 µs/point. DuckDB does not raise it.
- **A wide pivot is a map per row, never thousands of filtered aggregates.**
  Every analyte in tailings as `max(..) FILTER (WHERE var = k)` (2,576 of
  them) ran past five minutes and 18 GB; one map per sample read out by
  key took 0.26 s (2026-10-10). Build the maps in their own statement,
  because the same maps inside the read-out took 89 s. DuckDB's `PIVOT` is
  fast with ONE aggregate (2,512 columns in 1.2 s) and degrades
  superlinearly with several: 628 values x 1-4 aggregates took 0.4 / 3.7 /
  8.6 / 23.5 s synthetic, and 92.7 s on tailings, with operators reporting
  only 4.0 s of CPU and the planner 0.02 s (2026-10-11). Filtered
  aggregates ignored `memory_limit` and the interrupt: 21 GB at 628 of them.
  `docs/pivot-design.md` has the rest.
- **A pending result hides other connections' tables.** A result left
  unfetched or partly fetched (`execute(..).fetchone()` on many rows) holds
  its connection's transaction open, and tables another connection makes
  since are invisible to it: "Table with name pp_3 does not exist! Did you
  mean memory.pp_3?". `.sql()` does not end a pending result, `commit()`
  does not help, `execute()` does. `bridge_payload.answer` runs a no-op
  first for that reason (2026-10-11). Anything reading tables a worker's
  cursor made needs the same.
- **Temporal filtering works inside an envelope**: ~100,000–150,000 results
  per layer with a few thousand visible per window. The scan is paid over the
  whole layer every frame.

## Two habits this work kept needing

**One authority per decision.** Mouse access was gated twice — Qt's
`WA_TransparentForMouseEvents` and CSS `pointer-events` — and they desynced,
so the overlay swallowed clicks and did nothing with them. `panel_state.py`
exists because the panel once held six opinions about its own state. When you
find two places deciding one thing, delete one.

**Never average or sum results in a summary.** It destroyed a finding three
times in one week of work: summing update and render time reported a passing
design as failing; averaging an index's effect across selectivities reported
"no change" over a −39%/+54% split; asserting a global condition reported a
healthy install as blocked. **Decompose, and report the parts.** A check that
never ran is not a check that failed — say so separately.

## Before deleting old code

There is a parity gate: `tools/test_store_parity.py` holds `ReportTable` and
`ReportStore` to identical answers over thousands of filter combinations, and
it has been mutation-tested (8 of 8 deliberate rule breaks caught). Nothing
comes out of `report_data.py` until a caller has been switched over, that
gate is green, and `tools/test_offline.py` still passes under QGIS.

If you add a rule to one implementation, add the case to the parity fixture.
One mutation survived the first round purely because no fixture row exercised
it.

A third copy of the non-detect rules lives in `pipeline/arcgis_export.py`,
which does not import the plugin: the pipeline runs on its own, with Python
and pyarrow, wherever a facility's data lives. `tools/test_arcgis_parity.py` holds it to the store,
row for row, including on the real facility. A change to the value ladder,
the detect values, the unit columns or the date formats fails that gate until
the exporter follows.

## Documents

| | |
|---|---|
| [docs/implementation-plan.md](docs/implementation-plan.md) | **What to do next**, in order, with acceptance criteria |
| [docs/overlay-shell.md](docs/overlay-shell.md) | Branch `overlay-shell`: the plugin as panels on the map - plan, rules, status |
| [docs/phase3-next-steps.md](docs/phase3-next-steps.md) | The linked views' current sequence: what has been seen working, and what is next |
| [docs/pivot-design.md](docs/pivot-design.md) | One row per sample for multivariate views: the rules every cell needs, measured |
| [docs/vision-linked-views.md](docs/vision-linked-views.md) | The user stories (US-1…US-24) and recommendations (R1…R10) |
| [docs/mosaic-qgis-plugin-architecture.md](docs/mosaic-qgis-plugin-architecture.md) | The Mosaic + WebEngine design sketch |
| [docs/phase0-substrate.md](docs/phase0-substrate.md) | Bundling DuckDB into the plugin — measured |
| [docs/phase1-substrate-parity.md](docs/phase1-substrate-parity.md) | The DuckDB store and its parity gate — measured |
| [docs/webengine-support.md](docs/webengine-support.md) | Which QGIS builds can run a web view, and what WASM can do there — measured |
| [tools/mosaic_poc/README.md](tools/mosaic_poc/README.md) | Overlay, brush-scale and temporal spikes — measured |
| [docs/selection-ui-guidance.md](docs/selection-ui-guidance.md) | Why the panel is shaped the way it is |
| [pipeline/README.md](pipeline/README.md) | Exporting a facility to parquet: survey, slice, collect, repack, publish, and the unattended night |
| [docs/arcgis-pro-geoparquet-brief.md](docs/arcgis-pro-geoparquet-brief.md) | What ArcGIS Pro needs of a GeoParquet file, sourced; read before touching `arcgis_export.py` |

## Style

Comments say **why**, not what. The codebase explains decisions where they
were made — what was tried, what broke, what the alternative cost. Match that
density; it is the reason the plugin's odd corners are maintainable. Commit
messages do the same at a larger scale: what changed, what it was before, and
what the measurement or failure was that justified it.

When a claim is measurable, measure it and record the number with its date
and the machine it came from. Several confident claims in this project's own
source documents turned out to be wrong.
