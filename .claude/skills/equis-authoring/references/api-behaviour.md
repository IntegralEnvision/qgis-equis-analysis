# EQuIS REST API: verified behaviour

Everything here was tested against live EQuIS Online tenants (EQuIS 7.25 and
7.26, 2026). Where the Swagger document disagrees, the Swagger document is
wrong. Behaviour can change between builds: re-check anything load-bearing
on yours. On-premises installs were not tested, and their request timeout in
particular will differ from EQuIS Online's ~230 s.

## Silent failures — the defining hazard

This API returns success and discards what it does not understand. Every one of
these was observed:

| What you send | What you get | What actually happened |
|---|---|---|
| `POST /api/eia` without sentinel ids | **201** | Schedule block dropped; agent never runs |
| `where=` / `cas_rns=` on `/data` | **500 at 230 s** | Filter ignored; full report attempted |
| `A,B,C` in a multi-value parameter | **200, 0 rows** | Parsed as one literal value |
| `iCacheableReportForceExpire=true` | **500** | Not supported despite being documented |
| Re-run a report after editing its data | **200, fast** | Cached rows replayed; the edit is invisible (see reports.md, "The report cache") |
| `PUT` a Schedule with no existing row | **200** | Nothing attached |
| EIA with notices suppressed | runs, `lastExecution` set | No output stored |

**Therefore: read back and assert after every write.** A helper:

```python
def put_and_verify(call, path, body, check):
    """PUT then re-read; `check` receives the re-read object."""
    status, _ = call(path, 'PUT', body)
    time.sleep(2)                      # writes are not read-your-writes
    status2, raw = call(path)
    got = json.loads(raw)
    assert check(got), "write reported %s but did not take effect" % status
    return got
```

## Authentication

- `Authorization: Bearer <jwt>`. The Swagger says HTTP basic; that is wrong.
  `X-API-Key` is rejected 401.
- The key is a JWT: `jti` is the username, `exp` the expiry. Decode locally
  (`references/permissions.md`) so an expired key reports as expired rather
  than as a puzzling 401.
- Token lifetimes vary wildly — 14 days and 833 days both observed. Read `exp`,
  never assume.

## Accept header overrides oType

`Accept: application/json` makes `/api/reports/{id}/data` return a JSON
envelope even when `oType=csv` is requested:

```json
{"stats": null,
 "schema": {"title": "...", "properties": {"COL": {...}, ...}},
 "data": [[v1, v2, ...], ...]}
```

Rows are **positional arrays** indexed against `schema.properties` order, not
objects. For CSV send `Accept: text/csv, application/zip, */*`.

Output format is inconsistent regardless: zip-containing-CSV, plain CSV, the
JSON envelope, or xlsx. Handle all four. An `.xlsx` is itself a zip, so check
for `xl/workbook.xml` before treating an archive as a CSV container.

## Transient failures

The host emits sporadic DNS resolution failures and 5xx that succeed on retry.
Retry connection errors; do **not** retry an HTTP status, since the status is
usually the answer you want. Firing several report requests concurrently
provokes 500s — run them sequentially.

## Negative vs positive ids

Saved user reports have a negative id in listings (`-3000001`) and are
addressed positively in paths (`/api/reports/3000001`). Both forms appear in
payloads:

- `GET /api/reports/{positive}` — read
- `userReportId` in an EIA POST — **negative**
- widget series id report segment — **negative**
- `DELETE /api/reports/{positive}` — delete

When a call 404s or silently no-ops, try the other sign.

## Endpoints that do not exist

Despite appearing in the Swagger or seeming obvious: `/api/schedules`,
`/api/eia/{id}/status|history|runs`, `/api/roles`, `/api/permissions`,
`/api/version`, `/api/license`. The only `schedul*` paths are unrelated SPM
batch-task endpoints.

`/api/logs` exists but holds only security warnings and report-DLL publish
messages — **nothing from the EIA runner**. There is no way to learn why an
agent produced no output.

## Pagination and counts

- `maxRecordCount` advertises 1000 but is not enforced — read whole layers in
  one request. Paging the tail provokes 500s.
- `returnCountOnly` over-reports (10,636 claimed vs 10,349 served). Do not
  treat a slightly short read as incomplete.
- `/api/reports/{id}/use/{facilityId}` is a **stale cached stat**, not a live
  count. It did not update after a report was edited and re-run twice. Note
  the path segment is the facility id, not an index.

## Facility feature layers

- Layer 0 = facility polygon, **1 = location points (VW_LOCATION)**, 2 =
  location polygons. Layers 0 and 2 are frequently empty.
- `returnGeometry=true` must be sent explicitly *and* acts as a filter: a
  facility whose locations have null coordinates returns zero features with
  it and all of them without. Retry without it, then tell the user the
  locations are unmapped.
- `where` and `objectIds` are **ignored** on feature queries — every clause
  returns the full layer. The clause is parsed (an unknown column 500s); it
  just never filters. Filter client-side.
- `spatialReference.wkid` is often 0 with the real EPSG in `latestWkid`.
