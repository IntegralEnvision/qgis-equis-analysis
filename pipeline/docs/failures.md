# Failures, and what each one looked like

Every entry here produced a plausible result rather than an error, which is
why it is written down.

## Data that went missing without a word

| Symptom | Cause | Now |
|---|---|---|
| A cell's partition lost some locations after a bad night | `repack` rebuilt it from whichever slices produced output | A cell is rebuilt only when every slice staged; otherwise yesterday's is KEPT and named |
| Results before 2014 absent | a plan started at a guessed year (4,388 rows) | Location bins have no date axis; the survey prints each matrix's first sample |
| A whole cell looked empty | the template's 18-analyte `@cas_rns` was inherited; one solid cell had none of them | `@cas_rns` is cleared on every slice |
| A filter on a chemical literally named "None" | EQuIS stores an empty parameter as the string `"None"` | Scrubbed when cloning parameters |
| Locations fell out of every slice after `--replace` | a reused report kept its old `@sys_loc_codes` while the manifest recorded new ones | Reused reports have the new list saved and verified |
| A crash on slice 3 lost slices 1-2's accounting | the manifest was saved once at the end | Saved per slice |
| A slice's locations could vanish from a rebuilt cell | a failed run stores text, not nothing: `"Errors"` / "Report did not generate output" (64 bytes, read 2026-10-23); collect took it as a one-row slice, and the cell as complete | Output without a `SYS_LOC_CODE` column is recorded as `error output` and holds the cell |

## Data counted twice

`data/<name>/**/*.parquet` matched the repacked partitions **and** the
per-slice staging files: about 6.9M rows for a facility of about 3.45M, every row twice.
`repack` deletes `_staging/`, and a reader should take its explicit file
list from `manifest.json` - the first reader built on this also refuses a
glob that matches anything outside `matrix=*/fraction=*/`. If you must
glob, name the levels: `matrix=*/fraction=*/*.parquet`.

## An agent that never runs, or runs and stores nothing

- `POST /api/eia` without the sentinel ids drops the Schedule silently -
  201, `scheduleId` null. `create_agent` reads back and asserts it.
- A one-time schedule needs `effective` at **midnight** `Z`: one effective
  at the moment it was made is never evaluated. Slice agents are one-time
  and re-armed by editing that row (`slice_store.arm_agent`): `effective`
  back to today and `lastEvaluated` cleared, which made a fired agent fire
  again 6 minutes later (2026-10-23). A PUT can edit a schedule row but not
  create one.
- **An armed slice that never runs** reads as `waiting`, then `OVERDUE`
  after 6 hours. Check the agent in EQuIS; `--trigger` re-arms it.
- **Notices: the evidence disagrees, so check.** `create_agent` sets
  `supressAllNotices: true` to avoid emailing a large attachment each run,
  and the first facility's slices stored output that way. Other EIA work
  found that suppressing notices is how an agent ends up storing nothing,
  because the stored output is the notice attachment.
  Which holds may depend on the tenant or build: after a new facility's
  first night, confirm every slice's `rows` is a number before trusting it.
- A slice whose stored output is empty records `rows: null`, distinct from
  `rows: 0` (genuinely no data, correct, never re-planned).

## Conversion that made things worse

- **Reading only `RESULT_UNIT`** left 630,021 WS rows looking unitless -
  a non-detect has no result unit, only the limit's unit one column over -
  and mixed (chemical, matrix) pairs went from 237 to 292. The unit columns
  are coalesced, first non-empty wins.
- **One unit per matrix** killed a dissolved slice on a solid: a leachate is
  mg/L, and EQuIS answered "MISSING UNIT CONVERSION FACTOR - cannot convert
  'mg/L' to 'mg/kg'" for the whole report, 78 bytes. Units are per cell.
- **`@reporting_unit` server-side** dies on `percent` -> `mg/kg` (no row in
  `RT_UNIT_CONVERSION_FACTOR`), so conversion happens at collect time with
  EQuIS's own factors, keeping `RESULT_UNIT_ORIGINAL` / `RESULT_VALUE_ORIGINAL`.
  A pair with no factor, or a `FORMULA` factor, is left alone - never guessed.

- **A unit on the wrong basis.** The first facility's surface water had 260
  dissolved and total Mercury non-detects at "4 ug/kg" (2026-10-23): the
  particulate fraction's unit, applied to all three when two EDDs were
  prepared. No factor converts mg/kg to ug/L, so they sat in a ug/L layer
  as numbers nobody could compare. Now dropped with a warning, written to
  `_dropped/`, and counted on the published partition (`unit_convert.
  split_mismatched`). Commit mode matters for the fix: **Merge does not
  overwrite** a non-empty column, so reloading the corrected EDDs with the
  mode they were loaded with changes nothing; UpdateOnly does.

## Types

pyarrow infers a column's type from the first block and dies later:
`START_DEPTH` was whole numbers for 16 MB, inferred int64, then hit `0.5`.
Twenty numeric columns are declared in `FORCE_DOUBLE`, and codes that look
numeric (`CAS_RN`, `COLUMN_NUMBER`) in `FORCE_STRING`. A new report column
that matters should be added to one or the other.

## Reads that were slow for a structural reason

- **Sorting without small row groups made reads worse** (1.27 -> 1.93 MiB
  for one analyte). Sorted on `CHEMICAL_NAME, SYS_LOC_CODE, SAMPLE_DATE`
  with 50,000-row groups: 0.34 MiB. The two go together.
- **A window function in the reader's view** (`row_number() OVER ()`)
  blocked pushdown, so the first query read the whole facility (31-60 s
  from Azure). Row ids are `list_position(files, filename) * 2^40 +
  file_row_number`.

## Publishing

- A **SAS URL's query string is its credential**: a path appended after it
  goes after the signature and Azure answers 403 `AuthenticationFailed`,
  which reads exactly like an expired token. Paths go before the `?`.
- Over HTTP, DuckDB rejects globs, which is why the manifest lists files.
- The two manifests once shared a name; an upload went out with all ten
  partitions and no manifest, and the name people would have sent held the
  report ids. They are `manifest.json` (public) and `manifest_local.json`
  (never uploaded) for that reason.
