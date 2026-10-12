# Troubleshooting

Ordered by what to check first. Each ordering was learned by getting it wrong.

## A report returns HTTP 500

Read the body. A request-timeout page (`"The request timed out"`, `"failed to
respond within"`) after ~230 s means the report cannot generate in time — not a
server fault.

Fix, in order of effectiveness:

1. **Filter by analyte** (`@cas_rns`) — one analyte across nine years took
   29 s where three years of everything took 174 s
2. `@sample_types=N` — drops QC blanks and duplicates, often ~half the rows
3. `@fractions` — halves an Analytical Results II report
4. `@sys_loc_codes` — fewer locations
5. `@start_date` — weakest lever; generation tracks rows, not span

Filters must be **saved on the report**. Query-string filters are ignored and
you will keep hitting the same 500.

If it still cannot finish: use an EIA (`references/eia.md`). That is the only
route for a report that exceeds the limit.

## A chart renders blank

**The failure is silent** — the widget's data request returns 200 with correct
rows. Do not conclude from the network trace that it worked.

Check in this order. The first three are structural and cost nothing:

1. **`yAxis` count == series count?** Four series on one shared axis renders
   nothing. Each series must index its own axis.
2. **Exactly two `COLUMN==VALUE` filters in each series id?** A third gives
   *"This chart contains reports that failed to load"*.
3. **One report across all series?** No working widget spans two.
4. **Report row count.** Under ~4,000 renders; 7,558 did not.
5. **Do the (filter, filter) pairs exist in the data?** Fetch the report and
   count rows per pair — a typo in a location code gives an empty series.

Only after all five: ask for a browser console capture. The guard is
client-side Angular; the API cannot tell you.

**A HAR of a *working* dashboard is worth more than any amount of API
probing.** Comparing the working widget's request and config against yours
localises the difference immediately. Ask for one early.

## An EIA runs but stores nothing

`lastExecution` set, `attachment_id_ReadOnly` null.

1. **`supressAllNotices` / `supressNoDataNotice` are False?** The output *is*
   the notice attachment.
2. **Can the report generate at all?** Time it directly. If it 500s at 230 s,
   the agent likely exceeds its budget too.
3. **Does the report return rows?** An empty report legitimately stores
   nothing.

There is **no way to see the runner's error** — `/api/logs` holds nothing from
it and the status endpoints are 404. Do not claim a cause you cannot observe;
say "the run produced nothing" and list the candidates.

## An EIA never runs

`Schedule.lastEvaluated` is null.

1. **Does a schedule row exist?** `scheduleId: null` means the POST dropped the
   Schedule block — you omitted the sentinel ids. Recreate it.
2. **Is `effective` at midnight?** An `effective` set to the creation moment is
   never evaluated. Set `<today>T00:00:00.000Z` with `timeStart: 0`.
3. **Has it been long enough?** 20–90 minutes from `effective` is normal.
4. **`isDisabled: False`?**

## A filter returns no rows

**Check for commas.** `A,B,C` in a multi-value parameter returns 200 and zero
rows. It must be `A|B|C`.

Then check the value exists: `GET /api/reports/{id}/parameters/{name}` lists
valid values. Location codes are often inconsistent — `MW 01A` (space) and `MW-01`
(hyphen) can coexist, and `MW-05A` may not exist where `MW 05A` does.

## Rows are missing that the database plainly has

Suspect the **report cache** before the data or the filter, especially if
records were edited recently (sample type, matrix, validation) and the run
was fast. The cache is keyed on parameters and is not invalidated by data
edits, and Enterprise's Refresh does not bypass it.

Proof: clone the report's parameters **verbatim** onto a `ZZ_` report (new
id = no cache), run both, and compare counts per location. If the clone has
more rows, the original is stale. Fix it by changing any parameter and
saving, or by recreating the report. See `reports.md`, "The report cache".

## 401 / 403

Decode the JWT `exp` locally before assuming anything — an expired key gives a
401 that looks like a server problem. `references/permissions.md` has the
helper.

A 403 with a valid key is a permissions problem, not an expiry. Do not send the
user to regenerate a working key.

## Everything looks right and it still fails

You are probably verifying the wrong thing. Two errors from this session worth
avoiding:

- **"The data exists" ≠ "the consumer can read it."** Confirming a CSV contains
  matching rows proves nothing about whether a widget can resolve them.
- **"The agent fired" ≠ "output was stored."** `lastExecution` and
  `attachment_id` are different claims.

State what you actually observed. If a step is unverified, say so — this API
punishes confident inference.
