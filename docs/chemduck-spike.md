# Spike: Chemrooms' data handling on a plugin page, without SQLRooms' UI

**Run 2026-10-22**, QGIS 4.2.1 on Windows 11 (QtWebEngine, DuckDB 1.5.4 in
QGIS's Python), Facility A. Code: `tools/spike_chemduck/`. Read
[chemduck-parity.md](chemduck-parity.md) first: it explains why the values
below differ from the plugin's.

> **Status, 2026-10-28.** The spike reached ChemDuck through its old
> `tools/` path. ChemDuck is now the importable `chemduck` package, and has a
> second way in that the spike didn't have: `load_source`, which loads rows
> the host already holds into a named schema. The arrangement measured here
> (its own `chemduck` schema, a cursor whose default schema is `chemduck`,
> no `spatial`)
> is the one the adoption plan builds on.

## The question

Can the plugin take Chemrooms' data handling - ChemDuck's SQL, and
Chemrooms' own setup and query code - without taking SQLRooms' layout, React
or DuckDB-WASM?

## What was built

- **Python side.** ChemDuck's hybrid ARII source (sidecars published once,
  then ChemDuck's SQL and a `results` view over the same partitions), opened
  in the plugin's own DuckDB in a schema of its own, `chemduck`, beside the
  plugin's `ReportStore`. The page gets a cursor whose default schema is
  `chemduck`.
- **Page side.** Two functions imported from Chemrooms' source,
  **unchanged**: `initEntityLayers` (what Chemrooms runs at startup) and
  `buildSamplesLayerSql` (what it runs to colour a map by an analyte). They
  talk to SQLRooms' `createBaseDuckDbConnector`, given a four-line
  implementation that sends SQL over the plugin's bridge and decodes the
  Arrow that comes back. Bundled with the plugin's esbuild.
- **Run twice:** in Chromium through a stand-in for the bridge, and in QGIS
  through the plugin's real `WebBridge` and `QWebChannel`, with the plugin's
  own `LinkedViews` class pointed at the spike's page.

## Results

| | Chromium | QGIS |
|---|---|---|
| Chemrooms' `initEntityLayers`, through the bridge | 152 ms, 8 queries | 110 ms, 8 queries |
| `buildSamplesLayerSql` (Arsenic, soil, total) | 182 ms | 176 ms |
| Rows identical to the same SQL run directly in Python | 9 of 9 | 9 of 9 |
| Opening ChemDuck on the plugin's database | 0.5 s | 0.6 s (plus 6.3 s for a `spatial` download the spike didn't need; see finding 2) |
| Re-run in QGIS with no `spatial` at all | — | opened in 0.38 s; setup 66 ms; 9 of 9 rows identical |

**It works.** Chemrooms' code ran unchanged on a plugin page, through the
plugin's own bridge, against the plugin's own DuckDB. The plugin's
`ReportStore` kept answering beside it.

**What the page weighs:** 529 KB minified, of which most is `apache-arrow`
and `zod`; Chemrooms' own code is 48 KB, plus 31 KB of DuckDB-WASM glue that
never runs. No React, no SQLRooms UI. For comparison, the full SQLRooms stack
with Mosaic measured 3.16 MB.

**The values are ChemDuck's.** At the maximum recipe with duplicates
maximised, all 9 locations agree with the plugin's `ReportStore`. At
Chemrooms' default (most recent, duplicates averaged) none of the 9 do:
every soil (date, depth) group at this facility holds several samples at one
timestamp, which ChemDuck averages and the plugin doesn't (cause A in the
parity document). The tenth location has no coordinates, and Chemrooms'
layer query leaves out what it can't draw.

## Findings that shape the design

1. **ChemDuck needs its own schema.** Opened in the plugin's `main` schema,
   ChemDuck's `CREATE OR REPLACE VIEW results` silently replaced the plugin's
   own `results` view; the plugin's next query failed with `Referenced column
   "detect" not found`. It failed loudly here only because the columns happen
   to differ. In a schema of its own, with a cursor that defaults to it, both
   work side by side.
2. **The plugin does NOT need DuckDB's `spatial` extension** (corrected
   2026-10-22; the first version of this note said it did). The spike loaded
   it on an assumption, which cost a 6.3 s download of a 55.5 MB extension.
   Re-run under QGIS's own DuckDB 1.5.4 with `spatial` neither installed nor
   auto-loaded, ChemDuck's consumer opened and `aggregate_results` answered:
   `geom` arrives as a WKB `BLOB` and is only passed through. ChemDuck needs
   `spatial` where geometry is BUILT - its full loader and its sidecar
   publisher - which is pipeline-side. What it would take to drop it there
   too is in [chemduck-changes-for-qgis.md](chemduck-changes-for-qgis.md).
   (Installing the extension into a deep folder also failed on Windows'
   260-character path limit.)
3. **The sidecars are a pipeline step.** Publishing them took 8.8 s and
   645 KB for Facility A. They belong in `pipeline/run_nightly.py`, beside
   the manifest, as Chemrooms already expects.
4. **Chemrooms' query builder is tied to its 3D view.** The SQL
   `buildSamplesLayerSql` emits joins `geoid_offset()`,
   `location_elevations_sampled` (terrain heights sampled from Cesium) and
   `depth_interval_units`, all created by `initEntityLayers`. They worked
   here as empty or zero, but a 2D map pays for and ignores them. A shared
   core would separate the aggregation query from the altitude decoration.
   The helpers in between aren't exported, so today it's all of
   `initEntityLayers` or none.
5. **Two Vite-isms needed shims in the build:** `import.meta.env` (defined
   as empty, meaning "no project configured", so Chemrooms leaves the source
   to Python) and `?raw` SQL imports. Two lines each.

## What this means for step 2

Taking Chemrooms' data handling without its UI is feasible and light. Before
the plugin depends on it:

- the seven rule decisions in [chemduck-parity.md](chemduck-parity.md) have
  to land, or the plugin inherits day-averaging;
- ChemDuck's changes in [chemduck-changes-for-qgis.md](chemduck-changes-for-qgis.md),
  chief among them a detection rule that reads blank flags as non-detects
  and a missing flag column as all detected;
- Chemrooms would export a small 2D-clean query core (aggregation separate
  from altitude) that both apps import.

None of that requires either app to adopt the other's layout.

## Reproducing

```
node tools/spike_chemduck/build.mjs <work>/page            # from ../chemrooms
python tools/spike_chemduck/host.py --manifest <facility>/manifest.json \
    --chemduck ../chemduck --work <work> [--recipe '{"eventAgg":"maximum","dupAgg":"max"}']
# QGIS: see the header of tools/spike_chemduck/qgis_run.py
```
