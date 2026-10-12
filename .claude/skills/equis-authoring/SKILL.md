---
name: equis-authoring
description: Create or edit EQuIS Enterprise reports, EIAs (information agents), dashboards and widgets through the REST API. Use when asked to build/modify an EQuIS report, filter a report by analyte/location/date, schedule or debug an EIA, read an EIA's stored output, or add a dashboard, time-series chart or map widget. Also use when an EQuIS report returns HTTP 500, a chart renders blank, or an agent runs but stores nothing.
---

# Authoring EQuIS reports, agents and dashboards

The EQuIS Enterprise REST API contradicts its own Swagger document in ways
that cost hours to rediscover. This skill is the verified behaviour, learned
by testing against live EQuIS Online tenants (builds 7.25 and 7.26, 2026).

**Read `references/api-behaviour.md` before the first write of a session.**
It is the difference between a 201 and a silent no-op.

## Finding the spec

The Swagger UI is at **`<EQUIS_SITE>/swagger`**. The raw JSON it loads is at
`<EQUIS_SITE>/swagger/docs/<build-version>` — the version is embedded in the
path, so read it out of the UI page rather than guessing:

```python
html = get('/swagger').decode('utf-8', 'replace')
path = '/' + re.search(r"'(swagger/docs/[^']+)'", html).group(1).split('|')[0]
spec = json.loads(get(path))          # 294 paths, 209 definitions on 7_25_1_25129
```

The usual Swashbuckle locations (`/swagger/v1/swagger.json`, `/swagger/docs/v1`)
all 404. Grepping the JSON is far faster than clicking through the UI.

Treat the spec as a map of what endpoints *exist*, not as a description of how
they behave — it is wrong about auth, output formats, and which parameters are
honoured. Every discrepancy in these references was found by testing.

## The one rule that matters most

**A 200 does not mean it worked.** This API accepts malformed input and
discards the parts it does not understand, without error:

- `POST /api/eia` returns 201 but drops the `Schedule` block if ids are omitted
- Report parameters on the query string are ignored; the report runs unfiltered
- A comma-separated multi-value parameter returns 200 and zero rows
- An EIA can set `lastExecution` and store nothing
- A report re-run after its data was edited can replay **cached, stale rows**
  (the cache is keyed on parameters; Refresh does not bypass it)

After every write, **read the object back and assert the field you set is
there.** Never report success from the status code alone.

## Working method

1. **Model on something that works.** Read an existing report/agent/widget of
   the same kind and copy its shape. Do not construct payloads from the
   Swagger schema — it is wrong in places.
2. **Survey before inventing.** If unsure whether a structure is valid, count
   how many existing objects use it. A shape used by 924 objects is safe; a
   shape used by none is a guess. `references/survey-snippets.py` has the
   loops for this.
3. **Change one thing at a time**, then verify. Compounded edits make a
   failure impossible to localise.
4. **Clean up test objects.** Name throwaways `ZZ_<purpose>` and delete them
   in the same session. This is a shared tenant.

## Before writing to a shared tenant

Creating a report, agent or dashboard adds a permanent object other people
see. Confirm with the user before the first write unless they have already
asked for the object. Deleting or editing anything you did not create needs
explicit permission — say which object and why.

Check the caller's rights first (`references/permissions.md`): report
creation needs Creator on the Reports object type, which is not implied by
Owner or Editor, and **`CreatorOf` does not tell you** — users who own
hundreds of reports have `CreatorOf: 0`. The only reliable test is a write.

## Quick orientation

| Task | Start at |
|---|---|
| Build or filter a report | `references/reports.md` |
| Create/schedule/debug an EIA | `references/eia.md` |
| Read an EIA's stored output | `references/eia.md` — "Reading output" |
| Dashboard, chart or map widget | `references/dashboards.md` |
| EQuIS Live (logger/sensor) chart widget | `references/dashboards.md` — "EQuIS Live historical chart"; `references/live_widget.py` |
| A report 500s / a chart is blank | `references/troubleshooting.md` |
| Rows missing that the database has | `references/troubleshooting.md` — report cache |
| Auth, permissions, tokens | `references/permissions.md` |

## Non-negotiables

- **Multi-value parameters are PIPE-separated.** `A|B|C`. Comma returns 200
  and no rows — a silent wrong answer.
- **Report filters only work when SAVED on the report.** `where=`,
  `cas_rns=`, `reportParameters.x=` on the query string are all ignored.
- **Report ids are negative in listings** (`-3000001`) and positive in most
  paths (`/api/reports/3000001`). Widget series ids use the NEGATIVE form.
- **~230 s request limit on EQuIS Online.** This is the Azure-hosted
  service's front end; an on-premises install's limit is whatever its IIS
  and app-pool timeouts are set to, so measure yours. A report that cannot
  generate in that time
  returns HTTP 500 with a timeout page. Narrowing by analyte cuts far more
  than narrowing by date.
- **Never suppress notices on an EIA whose output you intend to read** — the
  stored output *is* the notice attachment.
