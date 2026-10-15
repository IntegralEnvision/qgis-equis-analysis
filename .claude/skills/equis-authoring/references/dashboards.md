# Dashboards and widgets

Full CRUD works, contrary to expectation. Verified: `POST /api/dashboards`
(201), `PUT`/`DELETE /api/dashboards/{id}`, and the same on
`/api/dashboards/{id}/widgets[/{wid}]`.

## Structure

- The dashboard id field is **`euid`**, not `id`.
- Widgets live in **`sections[].columns[].widgets[]`**. A section's own
  `widgets` field reads `None` — looking there makes a populated dashboard
  appear empty.
- `GET /api/dashboards/{id}` returns the nested layout;
  `GET /api/dashboards/{id}/widgets` returns a flat list. Both are useful.

## Creating a dashboard

`POST /api/dashboards` **copies a source dashboard, including its widgets** —
it does not build one from nothing. The cheapest route to a working dashboard:

1. Copy one that already has the widget types you want
2. `PUT` those widgets' configs
3. `DELETE` the widgets you do not need

```python
status, body = call('/api/dashboards/%d' % SOURCE_ID)
new = json.loads(body)
new['title'] = "New title"
for f in ('id', 'euid', 'createdDate'):
    new.pop(f, None)
call('/api/dashboards', 'POST', new)
```

**Set `facilityId` and `url` after copying, and fix the title.** The copy
drops `facilityId` and `url` (both read null) and inherits the source's
`userSettings.subtitle`, which will name the wrong project. The server also
rewrites the title to `"<title> - <owner> - <date>"` (seen 2026-10-07). One
`PUT /api/dashboards/{id}` fixes all three. Match a working dashboard:
`facilityId: <facility id>`, `url: "f=<facility id>"`.

A new widget can also be `POST`ed to `/api/dashboards/{id}/widgets` (201):
clone an existing widget's JSON, drop `id`, set `dashboardId`/`sectionId`.
It appears in both the nested layout and the flat list.

To look at a dashboard in a browser, open `<site>/Default.aspx?d=<id>`.
`/?d=<id>` redirects to the dashboard you last opened, not the one you asked for.

Add sections by appending to `sections[]` and PUTting the dashboard; each needs
`title`, `rowOrder`, `dashboardId`, and a `columns` list with at least one
`{"sectionId": 0, "order": 0, "background": "#FFFFFF", "size": 100.0,
"widgets": []}`.

## TimeSeries widget (widgetTypeId 383)

The config lives in `userSettings.chartParams`.

### Series binding

```json
{"name": "Aluminum",
 "report": "Exact report NAME, not id",
 "tab": "MW-01",
 "yAxis": 0,
 "type": "line",
 "color": "#2f7ed8",
 "zIndex": null, "showInLegend": true, "marker": {"enabled": true},
 "id": "SYS_LOC_CODE==MW-01::CHEMICAL_NAME==Aluminum::-3000014::SAMPLE_DATE::REPORT_RESULT_VALUE::REPORT_RESULT_UNIT::::"}
```

The `id` is `::`-delimited:

```
<COLUMN==VALUE filters> :: <NEGATIVE report id> :: <x col> :: <y col> :: <unit col> :: :: 
```

Note the four trailing colons.

### Three structural rules — break any and it renders blank

1. **Exactly TWO `COLUMN==VALUE` filters.** All 924 pre-existing series on the
   tenant surveyed use two. A third filter produces *"This chart contains reports that
   failed to load"*. To split by a third dimension (e.g. fraction), make one
   report per value and vary which report the series names.
2. **ONE `yAxis` entry per series**, each series indexing its own
   (`yAxis: 0,1,2...`). Verified across every working widget: 45 series/45
   axes, 140/140, 2524/2524. Four series sharing one axis renders nothing.
3. **One report per widget.** No working widget on the tenant surveyed spans two.

### Axis definition

```json
{"gridLineWidth": 1, "lineWidth": 1, "max": null, "min": null,
 "reversed": false, "opposite": false, "showEmpty": false,
 "showLastLabel": true, "type": "linear",
 "labels": {"enabled": true, "rotation": 0,
            "format": "{value} {$unit}",
            "align": "right", "x": -3, "y": 0, "style": {}},
 "position": "primary", "visible": true,
 "title": {"enabled": true, "text": "Concentration"}}
```

- **`{$unit}` shows units on the axis**, substituted from the series id's unit
  column. All 20,734 axes on the tenant surveyed use exactly `"{value} {$unit}"`.
- Two variants exist: visible (17,824 axes) and hidden — `visible: false` with
  `labels.enabled: false` (2,910). If you copy a template, check which you got.
- With one axis per series, make only the **first axis on each tab** visible or
  every series stacks its own axis along the chart edge.
- `type`: `linear` is the overwhelming default (20,734 axes on one tenant).
  `logarithmic` works and renders — verified — and is worth it when a series
  spans orders of magnitude (single series spanning 5,400x were common in
  river monitoring data). **Only safe when no value is ≤0**, which Highcharts
  drops silently. Check the data first:
  `min(v for v in values) > 0`.

### Tabs

Group series by `tab` to get one chart tab per location. **Tab order follows
series order** — emit locations in the sequence you want (e.g. upstream to
downstream).

## Map widget (widgetTypeId 388)

`Widgets/EQuIS/GIS/WebMap/widgetContainer.html`. Draws the facility's location
layer directly — **no report binding needed**, just `facilityId`.

Copy an existing one; the useful fields are `facilityId`, `basemap` (`topo`),
`mapExtent`/`customExtent`, `showLocationLabels`, `labelColumn` (`code`),
`definitionColumn`, `selectedGroup`, and `widgets` (the Esri controls:
Legend, LayerList, BasemapGallery, Fullscreen, Search, measurement).

### Filtering which locations show

- `definitionColumn` filters on a location attribute — **but check the
  attribute varies.** On one facility every location was `type: 'SW'` and
  ~70 columns were null; only `code`/`name`/`x`/`y` differed. There was
  nothing to filter on.
- `selectedGroup` uses a **location group**:
  `GET /api/facilities/{id}/locationGroups` (204 when none exist). Groups are
  per-facility and often absent — one tenant had 23 for one facility, 2 for
  another, none for a third.
- Failing both, code prefixes are the only structure (`MW-*`, `SW-*`). That
  is convention, not data — brittle but often all there is.

## EQuIS Live historical chart (widgetTypeId 366)

`Widgets/EQuIS/Live/AdvancedTimeSeries/`, titled "EQuIS Live - Historical
Chart". It plots logger (sensor) series straight from the Live tables. It has
**no report, no facility parameter and no 1,000-point ceiling**: one
widget drew 14 series of 39k–97k points each (~1M points). Verified
2026-10-07 on EQuIS Online 7.26.1, by reading a working widget, building two
from scratch, and drawing them in a browser. Only one working widget was
available to read, so the model is n=1. `references/live_widget.py` builds
the config.

It goes through the documented widget endpoints above. The logger and series
records it embeds are copied from an existing 366 widget's `userSettings`,
or from one built in the dashboard editor, rather than looked up.

### Other Live widgets

`Live/LiveTable/`, `Live/FileProcessor/`, and a DeviceBrowser (widgetTypeId
363) were seen, along with gauge, live chart, heat map and wind rose widgets.
Only the historical chart (366) has been built and verified.

### Config: three structures that must agree

Everything is in `userSettings` (a JSON string). Each series has one key,
`"<logger_series_id>.<frequency>.<calculation>.<type>"`, e.g.
`1001.none.none.line`, and the widget stores three views of it:

- `legendItemIds`: the ordered keys. **Plot order and colour order follow
  this list.**
- `legendObjs`: key → `{facility, logger, series, frequency, calculation,
  type, lineType, axis, seriesColor, startDate, days, series_unit, tooltip,
  ...}`. **The chart is drawn from these two**. `logger` is the whole
  logger record, as an existing widget holds it, and `series` is `{logger_series_id, series_desc}`.
- `legendItems`: a flat row per series for the editor's grid (`Series`,
  `LoggerSeriesId`, `Logger`, `StartDate` as `MM/DD/YYYY`, `Id` = the key).
  The editor rebuilds `legendItemIds` from it on reorder, so it must hold the
  same keys in the same order.

The editor refuses a duplicate key. Two series can come from one logger only
if their frequency, calculation or type differs.

### Time window: the WIDGET's `days`, not the series'

The widget computes each request's `startDate` from the series' `startDate`
and the widget-level `userSettings.days`. With `days: 0`, the chart starts at
the series' fixed `startDate`. With `days: N`, every series starts N days
before now, which gives a rolling window. **The per-series
`legendObjs[k].days` does not set the window.** Setting it to 90 still drew
from the fixed start date. Widget-level `days: 200` drew a rolling 200-day
window.

**A rolling window can be legitimately empty.** A "live" feed may have
stopped: on the tenant tested, no series had data in the four months before
the test, and two had stopped years earlier. Before choosing a window, draw
it with `days: 0` from an early `startDate` to see where each series ends. An
empty chart is not a broken config.

### Opening the dashboard as its owner writes to it

When someone who can edit the dashboard opens it, the widget refreshes
logger and series details and then **`PUT`s its own `userSettings` back**,
without the viewer doing anything. It overwrites each series' `series_desc`
with the server's description minus `" (unit)"` (which can leave a trailing
space), and the `logger` objects with fresh records. It is
idempotent: a working dashboard was byte-identical after a view. A config you
wrote will read back slightly different after anyone with edit rights views
it, so compare the keys and ids, not the whole JSON.

### Verified write paths

Both write paths were checked by reading the config back and drawing it in
headless Chromium (2026-10-07, on a throwaway `ZZ_` dashboard, since deleted):

- `PUT` a copied 366 widget with a fresh `userSettings` (3 series) → draws
- `POST` a new 366 widget cloned from the template (1 series) → draws
- `PUT` widget-level `days: 200` → the chart starts 200 days back

**Drawing it in a browser.** Playwright with `page.route("**/*")` can add
`Authorization: Bearer <key>` to requests for the EQuIS host *only*. The page
also loads `js.arcgis.com`, so attaching the header to every request would
send the key to Esri. The SPA then loads as the key's user. Open
`<site>/Default.aspx?d=<id>`, wait for network idle plus ~15 s, then count
`.highcharts-container` legends.

## Rendering limits

`lang_en.json` key `NoDataForMorePoints`: *"Reports with more than 1000 data
points will not render visual output."*

**This is advisory, not a hard cutoff.** Observed:

| Report rows | Series | Renders |
|---|---|---|
| 741 | 4 | yes |
| 1,287 | 15 | yes |
| 3,963 | 45 | yes (the reference widget) |
| 4,325 | 45 | yes |
| 7,558 | 4 | **no** |

Something between 4,325 and 7,558 is the real ceiling. Keep reports well under
~4,000 rows and prefer more widgets over bigger ones — each widget has its own
report and its own budget.

When a chart is blank, **the blank is silent**: the data request returns 200
with correct rows. Work through `references/troubleshooting.md` rather than
guessing.
