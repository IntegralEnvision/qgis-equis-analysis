# EIAs (EQuIS Information Agents)

An EIA runs a report out of band and stores the output in the database, so a
report too slow for a web request can still be read. **This is the only way to
get data from a report that exceeds the ~230 s limit** — verified: a report
that 500s on a direct call had 117 MB of agent output that read back in 20.9 s.

The agent runner has a longer budget than the web request, but **not an
unlimited one** — a report that cannot finish will produce an agent run with
`lastExecution` set and no output.

## Creating one — the exact payload

`POST /api/eia` returns 201. The shape below is copied from what the Enterprise
UI sends (captured via HAR) and is the only form verified to attach a schedule.

```python
TODAY = datetime.date.today().isoformat()

ev = {"id": -1,                       # sentinel: create
      "isDisabled": False,
      "userReportId": -REPORT_ID,     # NEGATIVE
      "name": "Descriptive - once",
      "description": None,
      "remark": "Why this exists.",
      "outputFormatString_ReadOnly": "csv",
      "outputFormat": 0,              # 0 csv, 7 txt, 1 htm, 8/xls variants
      "isForAnyFacility": True,       # True on every agent on the tenant
      "isForOnlyNewData": False,
      "agentType": 0,                 # 0 schedule, 1 edd, 2 trigger
      "agentTypeString_ReadOnly": "schedule",
      "formatName": None,
      "supressNoDataNotice": False,   # MUST be False - see below
      "supressAllNotices": False,     # MUST be False - see below
      "ownerUserId": USER_ID,
      "scheduleId": 0,                # sentinel: create
      "noticeTemplateId": 0}          # sentinel: create

sc = {"id": 0,                        # sentinel: create
      "statusFlag": "A",
      "createdBy": USER_ID,
      "timeStart": 0, "timeEnd": 1439, "timeInterval": 0,
      "effective": TODAY + "T00:00:00.000Z",   # MIDNIGHT, Z-suffixed
      "expiration": None, "lastEvaluated": None,
      "recurrenceType": 1,            # 1 once, 2 daily
      "recurrenceDay": 0, "recurrenceInterval": None,
      "recurrenceOrdinal": 0,
      "title": "runOnce:"}            # or "daily:"

tm = {"id": 0,
      "templateCode": "eia-%d-%d-%d" % (USER_ID, REPORT_ID, int(time.time()*1000)),
      "templateName": "report.success",
      "subject": NAME, "body": NAME,
      "ietfLang": "en", "contentType": "text/plain; charset=utf-8"}

call('/api/eia', 'POST', {"ReportEvent": ev, "Schedule": sc,
                          "NoticeTemplate": tm,
                          "Recipients": [{"id": USER_ID, "emailAddress": EMAIL}],
                          "HookInfo": None})
```

### Why each detail matters

- **PascalCase wrapper.** camelCase keys 500. A flat ReportEvent 400s.
- **Sentinel ids (`0`, `-1`).** Omitting them makes the server drop the
  `Schedule` silently — 201 returned, `scheduleId: null` on read-back. This is
  the single most common way to create an agent that never runs.
- **Negative `userReportId`.**
- **`effective` at MIDNIGHT, Z-suffixed.** See the scheduling trap below.
- **Strip every `*_ReadOnly` field** plus `id`/`scheduleId`/`noticeTemplateId`
  when cloning an existing agent, or the POST 500s.

## The scheduling trap

**A `runOnce:` agent whose `effective` is the creation moment is NEVER
evaluated.** Three agents sat unevaluated for up to 28 hours. Setting
`effective` to `<today>T00:00:00.000Z` with `timeStart: 0` had all three
evaluated within minutes and executed within 40 minutes.

The Enterprise UI writes the creation timestamp when you ask for an immediate
run — which is exactly the case that silently never fires. **Always write
midnight.**

A background evaluator sweeps continuously (observed stamps scattered across
all minutes of the hour). Expect 20–90 minutes from `effective` to execution.

## Notices are the output

**Never set `supressAllNotices` or `supressNoDataNotice` on an agent whose
output you intend to read.** The stored output *is* the notice attachment
(`ST_NOTICE_ATTACHMENT`). Suppressing notices suppresses the data — the agent
runs, sets `lastExecution`, and stores nothing.

This looks like tidiness (no inbox noise from a machine-read agent) and
silently defeats the whole mechanism.

## Checking whether it worked

Four observable fields, and only one of them means success:

| Field | Meaning |
|---|---|
| `Schedule.lastEvaluated` | the evaluator has seen it |
| `lastExecutionDateUTC_ReadOnly` | **the agent fired** — not that it produced anything |
| `notice_id_ReadOnly` | a notice was created |
| `attachment_id_ReadOnly` | **output exists — this is the one that matters** |

`lastExecution` set with `attachment_id` null means the run produced nothing:
either the report timed out or it legitimately returned no rows. **There is no
way to tell which** — `/api/logs` holds nothing from the EIA runner and
`/api/eia/{id}/status|history|runs` are all 404.

Do not diagnose from `/api/reports/{id}/use/{fid}` — it is a stale cache.

## Reading stored output

```
GET /api/reports/{reportId}/events/{reportEventId}/data?oType=csv
```

Discovery: `GET /api/reports/{id}/eia` — **204 when a report has no agents**,
which is normal, not an error. Filter to agents with a non-null
`attachment_id_ReadOnly`; sort by `lastExecutionDateUTC_ReadOnly` descending.

The report listing's `hasEIA` flag tracks *stored output*, not schedule — it
stays false until an agent has actually produced something.

`where`, `outFields` and `resultRecordCount` are **ignored** on stored output
(byte-identical responses) — it is a frozen snapshot. Only `oType` is honoured.
Filter client-side.

## Agent types

| `agentType` | Fires on | Needs a schedule |
|---|---|---|
| 0 `schedule` | clock | **yes** |
| 1 `edd` | new data loaded | no |
| 2 `trigger` | action-level exceedance | probably not |

EDD agents are event-driven and keep working when scheduled ones are
misconfigured — useful as a control when debugging.

## Editing

`PUT /api/eia/{id}` applies `ReportEvent` changes (verified by flipping the
suppress flags) **and** `Schedule` changes when a schedule row already exists.
What it cannot do is create the first schedule row — that needs the POST above
or the EIA widget.

`userData` cannot be set back to null via PUT. It is not significant — working
agents have both `null` and `""`.
