# Authoring EQuIS reports

## Creating one

`POST /api/reports` returns **201**. Model the body on an existing user report
read back from `/api/reports/{id}` — copy its `parameters` array wholesale and
override what you need.

```python
status, body = call('/api/reports/%d' % TEMPLATE_ID)     # an existing report
model = json.loads(body)
params = []
for p in model.get('parameters') or []:
    q = dict(p)
    if q.get('name') == '@facility_id':
        q['value'] = str(FID); q['displayValue'] = str(FID)
    params.append(q)

call('/api/reports', 'POST', {
    "baseReportId": BASE_ID,                # the base report to build on (see below)
    "name": "Descriptive name",
    "code": "Analytical Results II",        # the base report's code
    "type": "class",
    "facilityId": FID,
    "statusFlag": "A",
    "remark": "What this is for. Who made it.",
    "parameters": params,
})
```

The new id is **negative** in the listing. `DELETE /api/reports/{positive}`
removes it.

## Filtering — the part that actually matters

**Filters only take effect when saved on the report.** Query-string forms
(`cas_rns=`, `@cas_rns=`, `reportParameters.cas_rns=`) are all ignored and the
report runs unfiltered — usually straight into the 230 s timeout.

**Multi-value parameters are PIPE-separated.**

```python
q['value'] = '7429-90-5|7440-38-2|7439-89-6'      # correct
q['value'] = '7429-90-5,7440-38-2,7439-89-6'      # 200 OK, ZERO ROWS
```

The comma form is the dangerous one: it looks like it worked.

**Find the base id; do not assume it.** List `/api/reports`, find the
`.system`-owned report whose name is "Analytical Results II" (its `id`
equals its `baseReportId`), and use that. It was 508 on every tenant tested,
but nothing guarantees it.

### Useful parameters on Analytical Results II

| Parameter | Effect | Notes |
|---|---|---|
| `@facility_id` | scopes to a facility | always set this |
| `@cas_rns` | analytes, by CAS | **biggest runtime lever** |
| `@sys_loc_codes` | locations | pipe-separated |
| `@fractions` | `D` dissolved / `T` total | see below |
| `@sample_types` | `N` = normal only | drops EB/FD QC rows — often half the data |
| `@matrix_codes` | `WS` water, `SE` sediment | |
| `@start_date` / `@end_date` | date window | weaker lever than analyte |
| `@analyte_groups` | named groups | some groups are huge; see troubleshooting |

**Always consider `@fractions`.** Left unset, Analytical Results II returns
dissolved *and* total interleaved. Observed 50/50 split, and the values differ
by ~8x (total Al median 0.395 mg/L vs dissolved 0.05). A chart or aggregate
over both is meaningless.

**Always consider `@sample_types=N`** for anything analytical. Equipment blanks
(EB) and field duplicates (FD) were 8,300 of 15,858 rows in one report, and
they are not results you plot.

## Discovering valid values without running the report

```
GET /api/reports/{id}/parameters/{paramName}?facilityId=N
```

Fast (0.5–1.3 s) and does not execute the report:

| Parameter | Returns |
|---|---|
| `@cas_rns` | `[["cas_rn","chemical_name"], ...]` — 99 analytes in 1.3 s |
| `@sys_loc_codes` | `[["sys_loc_code","loc_name"], ...]` — 124 in 0.6 s |
| `@analyte_groups` | group codes |
| `@fractions` | `D`, `T`, `N`, `PRT`, `SPLP`, `TCLP`, `TR` |
| `@matrix_codes` | matrices present |

Rows 0 and 1 are a header and a blank; real values start at index 2. Values are
scoped to *that saved report's* filters, so a facility-scoped copy returns its
own analytes while the base report returns none.

Names must match exactly — `@cas_rn`, `@analytes`, `@chemical_name` all return
400 "Invalid parameter". `GET /api/reports/{id}/parameters` lists the real ones.

## Runtime

EQuIS Online allows about **230 seconds**. Past that: HTTP 500 with an HTML
timeout page. On-premises, the limit is your IIS configuration's.

Measured on one ~1.3M-row facility report:

| Filter | Rows | Time |
|---|---|---|
| unfiltered | — | **500 at 230 s** |
| one analyte, all years | 46,866 | 28.8 s |
| 3 analytes | 93,716 | 40.4 s |
| 3-year date window | 548,389 | 174 s |
| 2024→ cutoff | 197,991 | 74.5 s |

**Filtering by analyte beats filtering by date.** One analyte across nine years
took 29 s; three years of everything took 174 s.

Generation time tracks **row count, not date span** — adding two sparse years
to a window cost nothing. Size slices by rows (~250k ≈ 80 s is a comfortable
target), not by calendar.

## The report cache: stale rows that look current

**A report's cached output is keyed on its saved parameters and is not
invalidated when the data changes.** Verified 2026-10-02 on Analytical Results II:

1. A report with `@sample_types=N` ran unfiltered while two samples, S-01
   and S-02, were another sample type, so they were excluded. That result
   was cached.
2. Their `SAMPLE_TYPE_CODE` was then corrected to `N`.
3. Filtering to those two samples was a parameter set the report had not run
   before, so it ran fresh and returned them (128 rows).
4. Clearing the filter restored the step-1 parameter set. The report
   replayed the cached 1,206 rows **without S-01/S-02**, in the UI xlsx
   export and through `/api/reports/{id}/data` alike.

A verbatim clone of the same parameters on a **new report id** returned
1,334 rows, S-01/S-02 included. There is no cache indicator: the JSON
`stats` field is null, and the only symptom is a fast run (1.8 s).

What does NOT force a fresh run:
- Enterprise's **Refresh** button (per the user)
- `iCacheableReportForceExpire=true`, which returns 500

What does: **a parameter set the report has not run before**, or a **new
report id**. To prove staleness, clone the report's parameters verbatim
onto a `ZZ_` report and compare row counts.

Consequence for any tool that re-saves filters onto one report and runs it
(e.g. a per-user scratch report): a correction to the database can stay
invisible indefinitely behind a parameter set it has already served.

## Reading output

`report_csv`-style flow: request with `Accept: text/csv, application/zip, */*`
and `oType=csv`. Expect any of zip/CSV/JSON/xlsx back.

`oType=csv` on stored EIA output is **faster than the native workbook** despite
being ~3x the bytes — 8.7 s vs 38.6 s for 127,691 rows, because a workbook must
be decompressed and XML-walked.

`outFields` genuinely works here (unlike on feature queries) and cuts payloads
hard — 3 of 68 fields took a page from 1.4 MB/10.9 s to 0.2 MB/2.1 s. But a
report answered from EQuIS's cache ignores it and returns everything, so also
drop unwanted columns while parsing.

## Data quirks

- **Non-detects leave `RESULT_NUMERIC` empty.** The value is in
  `REPORT_RESULT_VALUE`, with `DETECT_FLAG='N'` and limits in
  `REPORT_METHOD_DETECTION_LIMIT` / `REPORTING_DETECTION_LIMIT`. Reading only
  `RESULT_NUMERIC` silently drops every non-detect.
- Dates come back in mixed formats — `2017-09-19T08:36:00` from xlsx,
  `2/11/2009 12:00:00 AM` from CSV. Parse both.
- `X_COORD`/`Y_COORD` are in the facility's CRS, so non-spatial report types
  can still be mapped.
