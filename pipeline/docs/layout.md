# Layout, manifests and units.json

```
data/<name>/
  manifest_local.json          pipeline bookkeeping       TRACKED, NEVER UPLOADED
  units.json                   target unit per cell       tracked, hand-edited
  arcgis.json                  ArcGIS coordinates + CRS   tracked, hand-edited, optional
  manifest.json                reader-facing              derived, uploaded
  matrix=WS/fraction=D/part-0000.parquet                  uploaded
  matrix=WS/fraction=N/part-0000.parquet
  ...
  pipeline.json                what run_nightly.py needs  written by create_slice_reports.py; add upload by hand
  _staging/                    per-slice files mid-run    deleted by repack
  _dropped/matrix=*/fraction=*/<slice>.csv
                               rows left out for a unit   replaced every collect, never uploaded
                               on the wrong basis
  arcgis/                      GeoParquet for ArcGIS Pro  derived, uploaded if wanted
```

One directory per facility. `create_slice_reports.py` records the
facility id in `manifest_local.json` and refuses a root that belongs to
another.

## Partitions

Partitioned on **matrix and fraction only**: they are in every query the
reader issues and their cardinality is tiny. Analyte is filtered as often
but has hundreds of values, so it is handled by sorting within the file and
letting row-group statistics prune. One file per cell - slice boundaries are
an artefact of fetching and would make a reader open several files.

Written zstd level 9, dictionary-encoded, sorted on
`CHEMICAL_NAME, SYS_LOC_CODE, SAMPLE_DATE`, 50,000-row groups, statistics
on, written beside and renamed over so an interrupted repack never leaves a
truncated partition.

## manifest_local.json

Illustrative values:

```json
{
 "version": 1,
 "facility_id": 1234567,
 "prefix": "parquet_1234567",
 "slices": {
  "parquet_1234567_WS_D_00": {
   "matrix": "WS", "fraction": "D", "index": 0,
   "locations": ["..."], "report_id": 7000001, "event_id": 8000001,
   "history": [{"when": "...", "rows": 91234, "seconds": 61.0, "status": "ok"}],
   "last_rows": 91234, "converted": {"mg/L->ug/L": 18397},
   "dropped": {"Mercury ug/kg": 100},
   "armed_at": "2026-10-23T15:00:00Z", "collected_run": "2026-10-23T15:09:12.4",
   "needs_report": true
  }
 },
 "load_signal": {"DT_RESULT": 5432701, "DT_TEST": 5432701, "DT_SAMPLE": 5432701, "DT_LOCATION": 5432701},
 "load_signal_at": "2026-10-23T15:00:00Z",
 "location_rows_by_cell": {"WS/D": {"MW-1": 4120}},
 "partitions": {"WS/D": {"as_of": "2026-10-05T03:41:00Z", "rows": 355440,
                         "dropped": {"Mercury ug/kg": 130}}},
 "runs": [{"when": "...", "finished": "...", "rows": 1000000}]
}
```

- `armed_at` is when the pipeline last asked the slice's agent to run;
  `collected_run` is the agent run whose output was last collected. A slice
  whose agent has not run since `armed_at` holds its cell (`waiting`).
- `load_signal` is the newest EBATCH per table at the last check; when it
  moves, every slice is armed.
- `dropped` (per slice, and summed per partition when it is rebuilt) counts
  rows left out for a unit on the wrong basis, by chemical and unit.

- `prefix` is fixed when a facility is first sliced; names live on in the
  tenant. A hand-made manifest without it falls back to `parquet`.
- `rows: null` in history = stored nothing (split it); `0` = genuinely
  empty (leave it).
- A facility-wide `location_rows` from before 2026-10-05 is ignored: it
  summed every cell together.

## units.json

```json
{"cells": {
  "WS/D":    {"unit": "ug/L",  "source": "proposed 2026-10-05 from a 41,210-row probe: 71% of rows; edit to change"},
  "SE/D":    {"unit": "mg/L",  "source": "a leachate"},
  "SE/N":    "mg/kg",
  "WS/PRT":  {"unit": null, "source": "both bases are legitimate here"}
}}
```

`"unit": null` means deliberately none: nothing converted, nothing dropped,
and no later proposal fills it in.

Proposed by the calibration probe as the cell's dominant concentration unit
(pH, percent and other dimensionless units are never proposed), never
overwritten once present, and read by both `nightly_slices.py` (collect-time
conversion) and `set_reporting_units.py` (server-side, when the tenant's
factor table allows). A cell with no entry is not converted.

## manifest.json (published)

```json
{
 "version": 1, "facility": "example", "facility_id": 1234567,
 "generated": "2026-10-05T04:02:11Z", "rows": 1000000,
 "partitions": [
  {"matrix": "WS", "fraction": "D", "path": "matrix=WS/fraction=D/part-0000.parquet",
   "rows": 355440, "bytes": 41234567, "unit": "ug/L", "as_of": "2026-10-05T03:41:00Z",
   "dropped_rows": 130, "dropped": {"Mercury ug/kg": 130}}
 ]
}
```

`dropped_rows` and `dropped` appear only on a partition that left rows out
for a unit on the other basis from its `unit` (README, "Rows dropped").

Built from the files on disk, not from the pipeline's intentions, so a
partition that was never written is never advertised. Row counts come from
the parquet footers and let a reader catch a truncated upload. `as_of`
differs between partitions when a cell was KEPT; a reader must show the age
rather than assume one night. Contains no report ids, agent ids or location
lists - it is readable by anyone holding the URL.

## Reading it back

Fetch `manifest.json`, then read the listed files by name. With DuckDB:

```python
import duckdb, json, urllib.request
base = "https://<account>.blob.core.windows.net/<container>/<name>"   # no query string
sas = "?sv=..."                       # the SAS token, if any - appended LAST
m = json.load(urllib.request.urlopen(base + "/manifest.json" + sas))
files = [base + "/" + p["path"] + sas for p in m["partitions"]]
con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs")
rel = con.read_parquet(files, hive_partitioning=True)  # matrix, fraction come off the paths
assert rel.count("*").fetchone()[0] == m["rows"]       # catches a truncated upload
```

- **An explicit list, never a glob**: DuckDB rejects globs over plain
  HTTPS, which would force `az://` and an account key onto what can be a
  read-only signed URL.
- **The path goes before the `?`** (see failures.md).
- **Do not put a window function over the scan** (`row_number() OVER ()`)
  in a view: it blocks predicate pushdown and turns every first query into
  a full read. Use `filename` and `file_row_number` if rows need ids.
- Show each partition's `as_of` somewhere a user will see it.

Measured over a plain SAS URL from the first reader built on this: count
3.45M rows 0.89 s, one analyte 0.76 s; opening a 41 MiB partition fetched
0.25 MiB in 5 range requests.

## The ArcGIS output

`arcgis_export.py` rewrites the partitions for ArcGIS Pro 3.6+, which reads
one file as one layer, ignores Hive paths, and refuses or mangles several
things the partitions carry (the requirements, with Esri's sources, are in
the plugin repository's `docs/arcgis-pro-geoparquet-brief.md`):

```
arcgis/
  <MATRIX>_<FRACTION>.parquet           one point per result      GeoParquet 1.1, WKB, zstd
  <MATRIX>_<FRACTION>_unplaced.parquet  rows with no coordinates  a table; only if any
  locations.parquet                     one point per location    most common coordinate
  manifest.json                         files, rows, settings, as_of
  README.txt                            for the Pro user
```

What changes on the way, and why:

| Partition | ArcGIS file | Because |
|---|---|---|
| in-file `matrix`, `fraction` | dropped | dictionary-typed duplicates; `fraction` collides with `FRACTION` once Pro folds case |
| dates as text | `timestamp[us]` | Pro time-enables only a date field; a column with any value no format reads stays text, and the run says so |
| `TIME`, null-typed columns | text | not documented as readable; a column null-typed in one cell takes its type from another so every file has ONE schema |
| X/Y as numbers | one WKB `geometry`, explicit PROJJSON `crs` | Pro does not treat coordinate columns as geometry, and its handling of a missing CRS is undocumented |
| rows without coordinates | `_unplaced` table | tested 2026-10-14: Pro's persistent cache (>= 500k rows) refuses a file with ANY null or empty (NaN) point, wherever it sits; small files load, so it fails only once a cell grows. Never dropped, never given a stand-in coordinate |
| `RESULT_NUMERIC` only | plus `IS_DETECT`, `VALUE_OR_LIMIT`, `VALUE_IS_LIMIT`, `VALUE_UNIT` | RESULT_NUMERIC is empty for a non-detect; the four state the facts with the plugin's own rules and halve nothing |

**Coordinates are a setting.** `arcgis.json` (or `--x`, `--y`, `--crs`)
names the columns and their CRS: `EPSG:<code>`, or a PROJJSON file path
relative to the root. EPSG:4326 is built in; other codes need pyproj or the
file. Coordinates that cannot be in the named CRS - metres called degrees,
or degrees called projected - are refused.

**Pro caches every file it opens**, on disk at 500,000 rows and up, and
rebuilds when the file's date changes. A cell is rewritten only when its
partition, the settings, the schema or the exporter's `FORMAT_VERSION`
changed; `--force` rewrites all. Measured 2026-10-14 on about 3.45M rows:
22.5 s for all ten cells from scratch, and `tools/test_arcgis_parity.py`
(plugin repository) found every row's detection, value, limit flag, unit
and date identical to the plugin's store.

