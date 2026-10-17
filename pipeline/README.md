# Exporting a facility to parquet

One Analytical Results II run cannot return a large facility: EQuIS allows
about **230 s** per request and an unfiltered run 500s. So the facility is
cut into many narrow **slice reports**, each with a **daily EIA** whose
stored output is collected, converted, and **repacked** into one parquet
file per `(matrix, fraction)`, described by a **published manifest** a
reader fetches over HTTP. The plugin's parquet source reads that manifest.

This is plain Python with pyarrow. No language model is involved at any
step. Setting a facility up needs a person's judgement (which cells, which
units, and permission to create objects on a shared tenant). After that,
`run_nightly.py` runs every night unattended.

## Setup

Run the scripts from the directory that holds the facility's data and
credentials:

```
python pipeline/survey_facility.py --facility <id>
```

- **Python 3.8+ and `pyarrow`** (`pip install pyarrow`). Nothing else: the
  scripts use the standard library for HTTP.
- **Credentials**: `EQUIS_SITE` and `EQUIS_API_KEY` environment variables,
  or a `.env` in the working directory or any directory above it:
  ```
  equis_site: https://<tenant>.equisonline.com
  api_key: <EQuIS API key>
  ```
  Keep `.env` out of version control.
- **Rights**: the key's user needs **Creator on Reports** (not implied by
  Owner or Editor) to create slice reports, and must be able to create
  agents. A read-only key can run the survey and every `--dry-run`.
- **No template report is needed.** Slices clone their parameters from the
  tenant's Analytical Results II base report, found by what it is (owned by
  `.system`, its own base) rather than by id, since ids differ between
  tenants. Pass `--sample-types N` to keep QC out of the export; without it
  every sample type is exported (the plugin can still filter QC on read).
  `--template <rid>` remains for a hand-tuned report whose saved filters
  every slice should share.
- **Check it works**: `python pipeline/test_slice_pipeline.py`,
  `python pipeline/test_unit_convert.py` and
  `python pipeline/test_run_nightly.py`, offline, in a few seconds.

Each facility gets its own data directory (`--root data/<name>`): the
manifests, `units.json`, `pipeline.json` and the parquet all live there.

## Setting a facility up

```
survey_facility.py            which (matrix, fraction) cells exist     read-only
create_slice_reports.py       per cell: bins -> reports + agents       WRITES to the tenant
  (wait for the agents' first run - they fire daily, 03:00 UTC default)
pipeline.json                 what run_nightly.py needs to know        written by hand
run_nightly.py                every night from then on                 see below
```

1. **Survey.** `python pipeline/survey_facility.py --facility <id>`. Prints
   matrices (samples, locations, date span, sample types), fractions, and a
   dry-run command per candidate cell. Read-only.
2. **Dry-run one cell**, then create it:
   `create_slice_reports.py --facility <id> --root data/<name> --sample-types N --cell WS/D --dry-run`.
   It weights locations, runs **one throwaway probe slice** to calibrate
   rows-per-location for that cell, refuses an empty cell, proposes the
   cell's target unit into `data/<name>/units.json`, and prints the bins.
   **Start with one cell.** Each slice is a permanent report plus agent in a
   shared tenant; agree it with whoever administers the tenant before the
   first real create.
3. **Review `units.json`.** Each cell's proposal is the dominant
   concentration unit in its probe. Water usually wants `ug/L` (trace metals
   in mg/L read as zeros); a dissolved fraction on a solid is a leachate and
   stays `mg/L`. A cell with no entry is not converted.
4. **Write `data/<name>/pipeline.json`**, stating the same sample types the
   cells were created with:
   ```json
   {"facility": 12345, "sample_types": "N"}
   ```
   `sample_types` is required, even as `null` (every type). The manifest
   does not record what the first slices were made with, and new slices
   made with a different value would mix QC into some of a cell's slices
   and not others. Optional keys: `"template"`, `"provision_pending"`
   (default true), `"arcgis"` (default: whether `arcgis.json` exists), and
   `"upload"` (below).
5. **After the agents have run once**, schedule `run_nightly.py`.

## Every night: `run_nightly.py`

```
python pipeline/run_nightly.py --root data/<name>
python pipeline/run_nightly.py --root data/<name> --no-upload
```

| Step | Script | If it fails |
|---|---|---|
| 1. collect, re-bin, repack | `nightly_slices.py` | the night stops; nothing is published |
| 2. reports for slices re-binning added | `create_slice_reports.py --pending` | reported; the night goes on, and those cells stay KEPT |
| 3. GeoParquet for ArcGIS Pro, if configured | `arcgis_export.py` | reported; the night goes on |
| 4. the published manifest | `publish_manifest.py` | the night stops; nothing is uploaded |
| 5. upload, if configured | (built in) | the night stops |

**Exit code** 0 means every step ran and every slice produced output. 1
means the night completed but something needs a look: a slice stored
nothing, a cell was KEPT, or step 2 or 3 failed. 2 means a step that stops
the night failed, or the run was refused. The summary reports each step on
its own line, and a step that did not run says *not run*, never *failed*.
Output is mirrored to `data/<name>/_logs/` (the last 30 nights), and a lock
file stops two nights overlapping.

**Upload** goes to Azure Blob storage over a SAS URL:

```json
"upload": {"base": "https://<account>.blob.core.windows.net/<container>/<name>",
           "sas_env": "PARQUET_SAS"}
```

The SAS token is read from the environment variable `sas_env` names, or
from that key, lower-cased, in the `.env`. It never goes in
`pipeline.json`, which is tracked, and a `base` with a query string is
refused. Partitions go up first, then `arcgis/`, then `manifest.json`
**last**, so a reader never follows a manifest to a file that is not there
yet. A file whose MD5 matches the blob already there is skipped.

**Scheduling.** Run it after the agents' 03:00 UTC run has had time to
finish. On Windows, a Task Scheduler action of
`python pipeline\run_nightly.py --root data\<name>`, starting in the
project directory. With cron:
`30 5 * * * cd /path/to/project && python pipeline/run_nightly.py --root data/<name>`.

## Columns beyond the default output

Every slice asks for `slice_store.EXTRA_FIELDS` through ARII's
`@extra_fields` parameter: today `DT_TEST.IDENTIFIER`, which arrives as a
column named `IDENTIFIER`. `LAB_SDG` is already in the default output, so
it is not asked for again (that only adds a duplicate). New slices get the
fields when they are created. Slices made earlier are brought level with:

```
python pipeline/set_extra_fields.py --root data/<name> --dry-run
python pipeline/set_extra_fields.py --root data/<name>
```

The column reaches the parquet with each slice's next stored output.

## What "done" means - check every one

- Every slice in `manifest_local.json` has a `report_id` and an `event_id`,
  and its last run has `rows` that is a number (`null` = stored nothing).
- The night printed no `KEPT ... produced nothing` line, or you know why
  each did. A kept partition is yesterday's, dated by `as_of`.
- The published row total is plausible against `survey_facility.py`'s
  result count - lower is expected (saved filters, QC excluded), higher
  is a double count and is never right.
- `units.json` names a unit for every cell that should convert.

## Non-negotiables

- **A missing slice looks exactly like data that does not exist.** That is
  the failure this whole design is organised against. `repack` only
  rebuilds a cell when *every* slice in it produced output; never work
  around that by repacking from partial staging.
- **Location is the splitting axis**, saved as a pipe-separated
  `@sys_loc_codes`. Report runs ignore `where=` on the query string, so a
  date or analyte cut through `where=` silently fetches the whole facility.
- **Weights are per cell.** A location's rows in WS/D say nothing about its
  rows in WS/N.
- **`manifest_local.json` never leaves the machine.** It holds report and
  agent ids and location lists. `manifest.json` is the public one.
- **Name the glob levels.** `matrix=*/fraction=*/*.parquet`, never
  `**/*.parquet`: a staging copy once doubled a facility to 6.9M rows.
- **The report cache can replay stale rows.** Rewriting a slice back to a
  location list it has run before may return cached output from before a
  database correction (verified on a hand-run report 2026-10-02; untested
  on EIA runs). Suspect it when a slice's rows do not move after an edit.

## Optional: GeoParquet for ArcGIS Pro 3.6+

`arcgis_export.py --root data/<name>` writes `data/<name>/arcgis/`, one
GeoParquet layer per cell; Pro cannot use the partitions themselves.
Coordinates default to LONGITUDE/LATITUDE as WGS84. EQuIS stores whatever
datum it was given, so set the real one in `data/<name>/arcgis.json`
(`{"x":..., "y":..., "crs": "EPSG:<code>"}`). Its existence turns the step
on in `run_nightly.py`. Unchanged cells are left alone, so Pro's caches
stay valid.

## Further reading

| Read | When |
|---|---|
| [docs/splitting.md](docs/splitting.md) | Choosing cells and bins; why weights, probes and re-binning work as they do |
| [docs/failures.md](docs/failures.md) | Something stored nothing, timed out, double-counted, or converted wrongly |
| [docs/layout.md](docs/layout.md) | The parquet layout, both manifests, `units.json`, reading it back, and the ArcGIS output |
