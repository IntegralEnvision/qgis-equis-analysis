---
name: equis-parquet-export
description: Extract a whole EQuIS facility's analytical results (Analytical Results II) into Hive-partitioned parquet with a published manifest, when the facility is too large for one report run. Covers surveying the facility, splitting it into (matrix, fraction, location-set) slice reports with nightly EIAs, collecting their stored output, unit conversion, repacking into matrix=/fraction= partitions, and publishing. Use when asked to export, extract, mirror or "pull the whole database" for a facility, to add a facility to the parquet source, or when a nightly slice run fails, stores nothing, or produces a partition with missing locations.
---

# Exporting a facility to parquet

One Analytical Results II run cannot return a large facility: EQuIS allows
about **230 s** per request and an unfiltered run 500s. So the facility is
cut into many narrow **slice reports**, each with a **daily EIA** whose
stored output is collected, converted, and **repacked** into one parquet
file per `(matrix, fraction)`, described by a **published manifest** a
reader fetches over HTTP.

## Setup

The scripts are in this skill's own `scripts/` folder and are run from the
user's project directory, where the data and credentials belong:

```
python <skill dir>/scripts/survey_facility.py --facility <id>
```

`<skill dir>` is the base directory this skill was loaded from. Below,
`scripts/x.py` means that path.

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
- **Check it works**: `python scripts/test_slice_pipeline.py` and
  `python scripts/test_unit_convert.py`, offline, in a few seconds.

Each facility gets its own data directory (`--root data/<name>`): the
manifests, `units.json` and the parquet all live there.

If the **equis-authoring** skill is installed, load it before touching
reports or agents by hand: it holds the API behaviour behind these scripts
(cloning parameter descriptors, pipe-separated lists, EIA payloads, reading
stored output). The scripts already encode what they need of it.

## The procedure

```
survey_facility.py            which (matrix, fraction) cells exist     read-only
create_slice_reports.py       per cell: bins -> reports + agents       WRITES to the tenant
  (wait for the agents' first run - they fire daily, 03:00 UTC default)
nightly_slices.py             collect -> re-bin -> repack              edits @sys_loc_codes
create_slice_reports.py --pending   reports for slices re-binning added
publish_manifest.py           manifest.json for readers                local
upload partitions + manifest.json                                       never manifest_local.json
```

1. **Survey.** `python scripts/survey_facility.py --facility <id>`. Prints
   matrices (samples, locations, date span, sample types), fractions, and a
   dry-run command per candidate cell. Read-only.
2. **Dry-run one cell**, then create it:
   `create_slice_reports.py --facility <id> --root data/<name> --sample-types N --cell WS/D --dry-run`.
   It weights locations, runs **one throwaway probe slice** to calibrate
   rows-per-location for that cell, refuses an empty cell, proposes the
   cell's target unit into `data/<name>/units.json`, and prints the bins.
   **Start with one cell.** Each slice is a permanent report plus agent in a
   shared tenant; confirm with the user before the first real create.
3. **Review `units.json`.** Each cell's proposal is the dominant
   concentration unit in its probe. Water usually wants `ug/L` (trace metals
   in mg/L read as zeros); a dissolved fraction on a solid is a leachate and
   stays `mg/L`. A cell with no entry is not converted.
4. **After the agents have run**, `nightly_slices.py --facility <id> --root data/<name>`
   collects, re-bins from measured rows, and repacks. Schedule it daily.
5. If re-binning added slices, `create_slice_reports.py ... --pending`.
6. `publish_manifest.py --root data/<name>`, then upload the
   `matrix=*/fraction=*/part-0000.parquet` tree and `manifest.json`.

## What "done" means - check every one

- Every slice in `manifest_local.json` has a `report_id` and an `event_id`,
  and its last run has `rows` that is a number (`null` = stored nothing).
- `nightly_slices.py` printed no `KEPT ... produced nothing` line, or you
  know why each did. A kept partition is yesterday's, dated by `as_of`.
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

## References

| Read | When |
|---|---|
| `references/splitting.md` | Choosing cells and bins; why weights, probes and re-binning work as they do |
| `references/failures.md` | Something stored nothing, timed out, double-counted, or converted wrongly |
| `references/layout.md` | The parquet layout, both manifests, `units.json`, and reading it back |
