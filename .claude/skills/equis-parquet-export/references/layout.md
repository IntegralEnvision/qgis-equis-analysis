# Layout, manifests and units.json

```
data/<name>/
  manifest_local.json          pipeline bookkeeping       TRACKED, NEVER UPLOADED
  units.json                   target unit per cell       tracked, hand-edited
  manifest.json                reader-facing              derived, uploaded
  matrix=WS/fraction=D/part-0000.parquet                  uploaded
  matrix=WS/fraction=N/part-0000.parquet
  ...
  _staging/                    per-slice files mid-run    deleted by repack
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
   "needs_report": true
  }
 },
 "location_rows_by_cell": {"WS/D": {"MW-1": 4120}},
 "partitions": {"WS/D": {"as_of": "2026-10-05T03:41:00Z", "rows": 355440}},
 "runs": [{"when": "...", "finished": "...", "rows": 1000000}]
}
```

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
  "SE/N":    "mg/kg"
}}
```

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
   "rows": 355440, "bytes": 41234567, "unit": "ug/L", "as_of": "2026-10-05T03:41:00Z"}
 ]
}
```

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
