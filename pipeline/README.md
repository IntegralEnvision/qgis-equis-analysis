# Exporting a facility to parquet

One Analytical Results II run cannot return a large facility: EQuIS allows
about **230 s** per request and an unfiltered run 500s. So the facility is
cut into many narrow **slice reports**, each with a **one-time EIA** whose
stored output is collected, converted, and **repacked** into one parquet
file per `(matrix, fraction)`, described by a **published manifest** a
reader fetches over HTTP. The plugin's parquet source reads that manifest.

The agents run only when asked. `run_nightly.py` checks whether the
facility has new data; if so it **arms** every slice (sets its agent to run
once more today), and on a later check it rebuilds each cell whose slices
have all run. Until 2026-10-23 every agent ran every night whether anything
had changed or not, which loaded a shared tenant for nothing.

This is plain Python with pyarrow. No language model is involved at any
step. Setting a facility up needs a person's judgement (which cells, which
units, and permission to create objects on a shared tenant). After that,
`run_nightly.py` runs unattended, as often as it is scheduled.

## Setup

Run the scripts from a directory at or below the `.env` that holds the
tenant's credentials (this repo's root, usually), and point `--root` at
the facility's data directory, which can be anywhere:

```
python pipeline/survey_facility.py --facility <id>
python pipeline/create_slice_reports.py --facility <id> --root C:\data\<name> ...
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
  `python pipeline/test_unit_convert.py`,
  `python pipeline/test_run_nightly.py` and
  `python pipeline/test_arcgis_export.py`, offline, in a few seconds.

Each facility gets its own data directory (`--root data/<name>`): the
manifests, `units.json`, `pipeline.json` and the parquet all live there.

## Setting a facility up

```
survey_facility.py            which (matrix, fraction) cells exist     read-only
create_slice_reports.py       per cell: bins -> reports + agents       WRITES to the tenant
  (each agent runs once, minutes after it is created)
pipeline.json                 what run_nightly.py needs to know        written by the create; add upload
run_nightly.py                scheduled from then on, e.g. hourly      see below
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
   stays `mg/L`. A cell with no entry is not converted, and nothing in it
   is dropped (below).
4. **Check `data/<name>/pipeline.json`.** The first real create writes it,
   stating what the slices were made with:
   ```json
   {"facility": 12345, "sample_types": "N", "template": null}
   ```
   A later cell created with different values is refused: slices made with
   another `sample_types` would mix QC into some of a cell's slices and not
   others. Add the optional keys by hand; later creates keep them:
   `"name"` (the facility name published in `manifest.json`; default: the
   data directory's name), `"provision_pending"` (default true), `"arcgis"` (default: whether
   `arcgis.json` exists), and `"upload"` (below).
5. **Schedule `run_nightly.py`.** The create recorded the facility's load
   signal and armed the new slices, so the first check after their agents
   have run builds the cells. Until then they read `waiting`.

## Every check: `run_nightly.py`

```
python pipeline/run_nightly.py --root data/<name>
python pipeline/run_nightly.py --root data/<name> --no-upload
python pipeline/run_nightly.py --root data/<name> --trigger
```

**What it asks of the tenant.** Each check reads the facility's **load
signal**: the newest `EBATCH` (the id of the EDD that wrote a row) in
`DT_RESULT`, `DT_TEST`, `DT_SAMPLE` and `DT_LOCATION`, about 1 s each,
plus one listing of the agents. If the signal moved, every slice is
**armed**: its agent's schedule is edited to run once, today. A cell is
collected and rebuilt once every one of its slices has run since it was
armed; until then it says `waiting`. When nothing changed, nothing runs
and nothing is published.

**Arming is tested, and fast.** A `ZZ_` test agent on 2026-10-23
(the first facility, one location, 1,142 rows) ran 9 minutes after it was
created; re-armed after that run, it was evaluated 45 s later and ran
again 6 minutes after, with a new attachment of the same rows. Earlier
one-time agents took 20-90 minutes, so a slice is not `OVERDUE` until 6
hours. There is no "run now" endpoint; a one-time schedule due today is
the only way to ask.

**Slices made before 2026-10-23 have daily agents.** They keep running
every night until a slice is first armed, which edits its schedule to
one-time; `--trigger` converts the lot at once.

The signal sees any EDD loaded into the facility, and the newest load
being rolled back. It does **not** see edits made in the EQuIS UI or an
older load rolled back: `--trigger` arms every slice by hand for those.

Why not EQuIS's own new-data agents: the tenant's only one fired after a
load into a *different* facility, stored "Report did not generate output",
and missed four later loads (read 2026-10-23).

| Step | Script | If it fails |
|---|---|---|
| 1. arm on new data; collect and repack finished cells | `nightly_slices.py` | the run stops; nothing is published. If no cell was rebuilt, steps 3-5 are skipped |
| 2. reports for slices re-binning added | `create_slice_reports.py --pending` | reported; the run goes on, and those cells wait |
| 3. GeoParquet for ArcGIS Pro, if configured | `arcgis_export.py` | reported; the run goes on |
| 4. the published manifest | `publish_manifest.py` | the run stops; nothing is uploaded |
| 5. upload, if configured | (built in) | the run stops |

**Exit code** 0 means every step ran and every slice produced output, or
there was nothing to do. 1 means the run completed but something needs a
look: a slice stored nothing or stored an error, a cell was KEPT, a slice
was armed over 6 hours ago and has not run (`OVERDUE`) or could not be
armed, or step 2 or 3 failed. 2 means a step that stops the run failed, or
the run was refused. The summary reports each step on its own line, and a
step that did not run says *not run*, never *failed*. Output is mirrored to
`data/<name>/_logs/` (30 days), and a lock file stops two runs overlapping.

**Re-binning is by hand now**, after a load large enough to move the bins:
`nightly_slices.py --facility <id> --root data/<name> --rewrite`. It saves
the new location lists and arms only the slices whose lists changed. A
cell is never rebuilt with some slices run on new lists and some on old:
either set alone covers each location once, a mix can double or drop one.
If it adds a slice, the next check gives it a report (step 2).

## By hand: `nightly_slices.py`

`run_nightly.py` runs this with no flags (the check). The flags are for a
person:

| | |
|---|---|
| `--trigger` | arm every slice now, new data or not |
| `--rewrite` | re-bin from measured rows; arms the slices that changed (`--dry-run` to see the bins first) |
| `--collect` | download every slice's stored output, whether or not it has run |
| `--repack` | rebuild the partitions from what is staged |
| `--weight walk` | exact per-location counts from `DT_RESULT`, to seed a new facility before `--rewrite`: about 94 min on a 4M-result facility |

**Upload** goes to Azure Blob storage over a container SAS URL with write
rights, kept in the `.env` (or the environment) beside the read-only one
readers are given:

```
parquet_cache:    https://<account>.blob.core.windows.net/<container>?sv=...&sp=r...
parquet_cache_rw: https://<account>.blob.core.windows.net/<container>?sv=...&sp=racwdl...
```

```json
"upload": {"sas_url_env": "parquet_cache_rw", "prefix": ""}
```

`prefix` is an optional folder in the container, for more than one facility
per container. The URL never goes in `pipeline.json`, which is tracked; a
config carrying a token is refused. The older split form, `{"base":
<https URL>, "sas_env": <variable holding only the token>}`, still works. Partitions go up first, then `arcgis/`, then `manifest.json`
**last**, so a reader never follows a manifest to a file that is not there
yet. A file whose MD5 matches the blob already there is skipped.

**Scheduling.** Hourly is reasonable: an idle check costs the tenant five
small reads. A load is then armed within the hour and, if the agents run
as fast as the test one did, published at the next check. On Windows, a Task Scheduler action of
`python pipeline\run_nightly.py --root data\<name>`, starting in the
project directory, repeating every hour. With cron:
`0 * * * * cd /path/to/project && python pipeline/run_nightly.py --root data/<name>`.

## Rows dropped for a unit on the wrong basis

When a cell has a unit in `units.json`, a row whose unit is a concentration
on the **other basis** - per mass (`mg/kg`, `ug/kg`) in a cell converted per
volume (`ug/L`), or the reverse - is left out of the partition, with a
warning. No factor converts between the two, so such a row is a load error,
not a measurement of the cell's medium: the first facility's surface water
carried 260 dissolved and total Mercury non-detects at "4 ug/kg", the
particulate unit applied to every fraction (2026-10-23).

- Units with no mass basis are never dropped: `SU`, `%`, `mV`, `pCi/L`,
  `NTU`, `ppm`. They belong in either kind of cell.
- The collect prints `DROPPED <n> row(s) not on ug/L's basis: Mercury
  ug/kg x130` per slice, and the check exits 1 while any are dropped.
- The rows are written as EQuIS stored them to
  `data/<name>/_dropped/matrix=*/fraction=*/<slice>.csv`, replaced every
  collect, for whoever corrects the source. Not uploaded.
- The published manifest gives the partition `dropped_rows` and `dropped`
  (`{"Mercury ug/kg": 130}`), so a reader can see it is short, and of what.

Fix the source and reload (with UpdateOnly, which restamps the rows' batch
and so arms the slices); the next rebuild drops nothing and says so.

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

The column reaches the parquet with each slice's next stored output; run
`run_nightly.py --trigger` to have that happen now.

## What "done" means - check every one

- Every slice in `manifest_local.json` has a `report_id` and an `event_id`,
  and its last run has `rows` that is a number (`null` = stored nothing).
- The last check printed no `KEPT`, `FAILED`, `OVERDUE`, `ERROR STORED` or
  `DROPPED` line, or you know why each did. A kept partition is the last
  complete one, dated by `as_of`; dropped rows are in `_dropped/`.
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
