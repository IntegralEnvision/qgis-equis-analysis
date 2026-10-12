# EQuIS Chemical Explorer (QGIS plugin)

Maps analytical chemistry from EQuIS Enterprise in QGIS 4.x.

Built and tested against QGIS 4.0.2 (PyQt6) and the EQuIS 7.25 REST API.

## The workflow

The plugin is a **dock panel** (right-hand side by default, draggable to the
left or floating). Each step is a collapsible section that folds away as it
completes and opens the next, and a splitter lets you give either the steps or
the plot more room.

1. **Connect** — enter a site URL and API key.
2. The plugin lists the facilities the key can see. Nothing is fetched yet: the
   combo opens on a placeholder so no site is loaded merely for sorting first.
3. **Pick a facility** — its location points go onto the map.
4. **Pick a report** — the first whose name contains "ARII" is preselected.
5. The plugin lists the unique chemicals (name + CAS RN) in that report.
6. **Pick one or more chemicals, an aggregation** (most recent, maximum, or
   every result)
   **and a non-detect convention**, then *Add layer to map*. The location
   points are joined to each chemical's aggregated value per location.
   Quantified results are graduated yellow→red by concentration; non-detects
   are drawn as hollow squares at one fixed size. Ctrl-click several chemicals and the
   button becomes *Add N layers to map*: one layer per chemical, and the one
   you clicked last is left active.
7. **Click any point** with the time series tool active to plot that chemical's
   history at that location, in the panel itself.

### The time series tool

A normal checkable QGIS map tool, on the toolbar beside the plugin's own button
and mirrored by *Time series tool* in the panel. It arms itself once when you
first add a chemical layer, so the advertised click works straight away.
Untoggle it — or pick Pan, Identify, or any other tool — to get the normal
cursor back; the plugin's buttons untoggle to match, so the two never disagree
about what the cursor is doing.

### Sparklines

**Draw as time-series sparklines** is a checkbox that chooses how the *next*
layer is drawn: unchecked, graduated circles with hollow squares for
non-detects; checked, a small
SVG sparkline of each location's history. It deliberately does not restyle what
is already mapped — add the other view instead and keep both (see **Two views
of one dataset**). (It used to be a button that restyled an existing
layer, which meant it was greyed out until one existed — unpressable exactly
when you wanted it, while sitting beside *Add layer to map* as though it were
an alternative.)

The **min pts** spinner (default 2) sets how many results a location needs
before it earns a sparkline — a line through one point is not a trend — and
locations below the threshold keep a plain grey dot. If no location clears the
threshold the layer falls back to circles and says so, rather than showing an
empty sparkline renderer.

Markers are sized as a real distance on the ground rather than a fixed number
of millimetres, so zooming out shrinks them instead of leaving a solid mat of
overlapping markers; millimetre clamps stop them vanishing when zoomed far out
or swamping the map when zoomed right in. The ground size is derived from the
layer's own extent, so it starts out proportionate whatever the site's scale.

Each sparkline is captioned with its `SYS_LOC_CODE`, so a cluster of markers is
readable without clicking through them. Codes longer than the marker are
trimmed from the front (`…N-CODE-0001`), since site codes usually share a
prefix and it is the tail that tells them apart. The chemical name is captioned
above it, trimmed from the *back* (`1,1,2-Trichloro-1…`) because chemical names
are told apart by how they start.

### Rolling between chemicals

Ctrl-click several chemicals in the list, then press **Roll sparklines**: the
markers cycle through them, one chemical every few seconds (the spinner beside
the button sets the interval). Every marker is captioned with the chemical it
is currently showing, and the panel names it too, since the map may be scrolled
away from any marker.

This is not animated SVG — Qt implements SVG 1.2 Tiny, which has no declarative
animation, so SMIL and CSS keyframes both render frozen on the first frame with
no error. Instead each chemical gets its own marker column and a timer changes
which column the renderer reads. Advancing costs about a millisecond and writes
no data, so a GeoPackage layer is never dirtied by the roll.

Redrawing is now the cost that matters (preparing the markers was, until the
report gained a row index): roughly 200 ms for 300 markers and 2.4 s
for 3,000, so a whole facility can take longer to draw than the interval. The
plugin measures this and warns when the roll will lag — zooming in fixes it,
since QGIS only draws what is in the viewport, and raising **min pts** also
thins the markers. Each embedded marker is about 1.2 KB, so three chemicals
across 10,000 locations is roughly 37 MB in the layer; a handful of chemicals
is practical, dozens are not.

### One session at a time

One facility and one report are active at a time. The panel keeps that session
while hidden, so reopening shows what the map is currently showing — the active
facility, report, and mapped chemical appear in a banner at the top — rather
than making you re-run a report that takes minutes.

Adding a layer replaces only the *same view* — the same chemical, aggregation
and symbology. Anything else on the map stays, so adding Toluene never destroys
your Benzene layer. Switching facility still clears the outgoing facility's
layers, and reconnecting discards the loaded report, so it asks first.

Layers go into a QGIS layer group named for the facility and report, and
**Remove EQuIS layers** clears the lot when the map gets crowded.

When several chemicals are mapped at once, the last one added is the **active
layer**: the time series tool hit-tests against it, and the sparkline buttons
restyle it. All the layers share the same location geometry, so a click lands
in the same place whichever is active; what the plot shows is governed by the
chemical selected in the list. Selecting more than eight chemicals asks for
confirmation first, since each one is a separate join over the report and its
own table in the GeoPackage.

The report is fetched once and held in memory as `ReportTable.rows`, so
choosing chemicals, switching aggregation, and clicking points are instant
after the initial run.

Row lookups go through an index built on first use, grouping row positions by
chemical and by chemical+location. Without it every aggregate and time series
scans the whole report, which is affordable once but not per location: drawing
sparklines for 300 locations on a 460,000-row report meant 300 full scans and
took about 45 seconds. The index costs a few seconds and ~35 MB once, then
answers in microseconds — the same 300 lookups take 0.007 s. It is lazy, so a
report that is loaded and discarded never pays for it.

## Working offline

Everything pulled from EQuIS is cached on disk, under the active QGIS profile
(`…/profiles/default/equis_cache`). A 460,000-row report keeps about 4 MB there
and reloads in under a second, against the minutes a live run costs.

**Reports load from the cache by default.** *Run report* becomes *Load cached
report* once a copy exists, and the combo marks which reports are already
stored (`● cached 14 Aug`). The **↻** button beside it re-runs against EQuIS
and replaces the stored copy — cached data is never silently refreshed, and
never silently stale either.

There are two ways into offline mode:

- **Work offline** in the Connect section, which needs no API key at all. Only
  facilities and reports that were loaded before are listed, since anything
  else would be a dead end.
- **Automatically**, when a connection attempt fails and the site has cached
  data. This is the case that actually happens — nobody decides in advance that
  the network is down — so the panel drops into offline mode and says so rather
  than dead-ending on an error.

Offline data is signalled four ways, because a stale map that looks live is the
one outcome worth engineering against:

| Where | What it shows |
|---|---|
| The banner | Turns amber, headed `⚠ Offline — showing cached data`, with each item's age |
| Layer names | Suffixed `[cached]` — the only marker that survives into a saved project |
| The message bar | A one-time warning with a **Retry connection** button |
| The report combo | `● cached <date>` per report, before you commit to a run |

A report truncated by a row limit stays flagged as partial everywhere it
appears, and an uncapped request will not be satisfied by a truncated copy —
otherwise the cache would quietly turn a known-incomplete report into an
authoritative one.

The cache is capped at 2 GB and evicts least-recently-used reports past that;
facility and location metadata is never evicted, since a report cannot be
mapped without the locations to join against. *Clear cache* in the Connect
section deletes the current site's copy.

The cache and layer storage are independent: you can work offline and still
write persistent layers. See **Layer storage** below.

### Two views of one dataset

The same chemical can be on the map twice — once as graduated circles, once as
sparklines — because they answer different questions. Add it with the checkbox
off, tick the checkbox, and add it again: both layers stay, named
`… (most recent)` and `… (most recent, sparklines)` so the panel tells them
apart.

Under GeoPackage storage the two share **one table**: same rows, two styles,
which is how QGIS normally works and avoids duplicating identical geometry and
attributes in the file. The table is named for the data alone, so symbology
never multiplies what is stored.

## Layer storage

**Store as** in the Chemical section decides where the result layer lives.

**GeoPackage (default)** writes it to `<project>_equis.gpkg` beside the `.qgz`,
so the project and its data travel together — zip the folder and it opens
working on another machine. One layer per mapped chemical shares the file, and
re-mapping a chemical overwrites its own table rather than piling up copies.
Writing 10,636 features costs about 0.23 s and 1.4 MB.

**Memory** is the old behaviour: fast, leaves nothing on disk, and discarded
when QGIS closes. A saved project reopens with the layer *empty* — QGIS stores
the reference but not the data — so this suits throwaway exploration rather
than work you intend to keep.

Until the project has been saved there is nowhere to put the GeoPackage, so
layers stay in memory and the panel says so. Save the project, then add the
layer again to write it alongside. If the write fails — full disk, read-only
folder — the layer is still added as a memory layer and the reason is
reported, since losing the join would be worse than losing persistence.

Sparkline markers are embedded in the features themselves as base64 data URIs
rather than written to temp files. A path into the temp directory dangles as
soon as the machine sweeps it, which would leave a persistent layer with
missing symbols; embedding costs roughly 450–600 bytes per location that
qualifies and survives both reopening and being copied to another machine.

## Non-detects

Non-detects are kept rather than dropped. These reports leave `RESULT_NUMERIC`
empty for a non-detect and report only the limit the result fell below, so the
plugin falls back to `REPORT_RESULT_VALUE` and then the detection-limit
columns, and marks the point as a limit value. Reading `RESULT_NUMERIC` alone
silently loses them — at Facility B that hid 8 of 46 points in one series.

The **Non-detects** dropdown picks the convention, and applies to both the map
aggregation and the plot:

| Option | Effect |
|---|---|
| 1/2 detection limit (default) | Plot/aggregate at half the limit — usual for statistics and trends |
| Full detection limit | Plot/aggregate at the limit — conservative for screening |
| Exclude non-detects | Drop them entirely |

Changing it redraws the open plot immediately; rebuild the layer to apply it to
the map. The plot draws detections as **solid filled circles** and non-detects
as **open circles**, and the map follows the same convention: detections are
graduated circles, non-detects **hollow squares**.

The layer is drawn with **symbol levels**: markers are painted in ascending
order of concentration, so where locations overlap the higher result sits on
top and non-detects sit underneath everything. Without this the draw order is
feature order, which is arbitrary — a hot spot can be hidden beneath a
neighbouring low result purely because of where it fell in the report.

**Matrix is multi-select.** Tick as many as apply: sediment and tailings are
both solids and often belong on one map, while water never belongs beside
either. Ticking nothing means every matrix. The layer name lists what was
chosen (`SE+TAILS`), and two different combinations are two different layers
rather than one replacing the other.

A non-detect is the absence of a measurement, not a small one, so it is not
given a place on the colour ramp. It is drawn at one fixed size — the number
behind it is the laboratory's detection limit, and graduating on that would
read as a concentration gradient where there is none. Non-detects are also
left out of the classification, so the class breaks reflect the contamination
actually measured rather than being pulled down toward the laboratory's
sensitivity. The layer carries `result_detect` (the winning row's
DETECT_FLAG) and `result_at_limit` (whether the plotted number is a limit
rather than a measurement); the renderer prefers the latter and falls back to
the former on layers built before that column existed.

Sparkline symbology is unaffected: a sparkline already shows each point's
detect status within the trend.

## Every result (unaggregated)

The **Aggregate** picker has a third option, *Every result (one point per
sample)*. The other two collapse a location's whole history into one number;
this one does not collapse at all — **one map feature is one result row**, so a
well sampled twelve times becomes twelve features.

That is the only way the matrix and the fraction can reach the map. An
aggregate keeps a single winning row per location and discards everything
else about the others, the matrix and fraction included; an unaggregated
layer carries them per feature:

| Field | Meaning |
|---|---|
| `result_matrix` | `MATRIX_CODE` of this result |
| `result_fraction` | `FRACTION` — dissolved, total, or not applicable |
| `sample_type` | `SAMPLE_TYPE_CODE` — normal, blank, duplicate |
| `result_seq` | Which result this is at its location, oldest first |
| `sample_time` | The sample date as a real datetime (see below) |
| `start_depth` / `end_depth` | The sampled interval, as numbers |
| `depth_unit` | Its unit (`m`, `ft`) |
| `custom_1` | `DT_SAMPLE.CUSTOM_FIELD_1` - site-specific sample text |
| `method` | `ANALYTIC_METHOD` - which laboratory method produced the result |

### Two methods, one sample

**A sample is routinely analysed by more than one method, and they can
disagree.** At Facility A, 162 of 172 tailings intervals carry exactly two
results - same location, same depth, same day, same fraction - differing only
by `ANALYTIC_METHOD`:

```
10-15 m   ICM14B   < 0.01   non-detect
10-15 m   ICM40B     0.02   detection
```

Both are real. Neither is a duplicate, and the schema is right to hold them.
Before the method reached the plugin they looked like duplicated rows, and a
depth profile drew two dots at one depth with nothing to explain why.

The **Method** picker (multi-select, beside Matrix) filters on it. Ticking
nothing keeps every method; the row hides itself when a report only has one,
since there is then nothing to choose. Pick a single method and a profile
shows exactly one point per depth.

With several methods selected the profile draws **one line per method**,
colour-coded with a legend, so the two analyses can be compared rather than
mistaken for scatter. Detections stay filled and non-detects hollow within
each series.

**Depth matters most where the map cannot help.** A soil boring is sampled
every few metres and every one of those results shares the borehole's single
coordinate, so the stack is not merely crowded - without depth nothing
distinguishes a result at 2 m from one at 40 m. Filter or graduate on
`start_depth` to read a profile down the hole.

`START_DEPTH`, `END_DEPTH` and `DEPTH_UNIT` are already in Analytical Results
II's default output; the plugin simply was not keeping them. `CUSTOM_FIELD_1`
is requested through the report's `@extra_fields` parameter, which the plugin
**adds to** rather than overwrites - the report carries columns of its own
(`RT_ANALYTE.ANALYTE_TYPE`, `RT_MATRIX.MATRIX_DESC` on this tenant) and
clobbering them would change what everyone else gets out of that report.

Note that the report also has `@min_start_depth` / `@max_end_depth`
parameters. Those *filter* by depth without adding a column, so before this
change you could narrow a report to 0-2 m with nothing in the output to
confirm it.

`result_value`, `result_date`, `result_detect` and the rest keep the names
they have on the aggregated layer, so one symbology and one saved style serve
both.

**Points are not moved.** Every result sits on its location's true surveyed
coordinate, so results at one location stack exactly. Stacked markers would
hide each other — measured at 500 painted pixels for six results against 476
for the top marker alone, near enough to be indistinguishable from a maximum
— so the layer is drawn with QGIS's **point displacement** renderer, which
fans coincident points into a ring around a centre dot marking the real
position. That is a drawing decision, not a data one: the geometry still
holds the surveyed coordinate, and switching the renderer off restores the
stack. The graduated ramp, the hollow-square non-detects and the symbol
levels all survive inside it.

**The Temporal Controller works on these layers.** `sample_time` is a real
datetime field, not the ISO string in `result_date` — QGIS can only animate a
date/datetime column, and a string that merely looks like a date is invisible
to it. The layer arrives with its temporal properties configured (mode
*instant from field*, pointed at `sample_time`) but **switched off**, since an
active temporal layer is filtered by the canvas's current time range and would
otherwise appear empty the moment it was added. Turn it on in *Layer
Properties → Temporal*, or from the Temporal Controller panel, and the map
animates through the sampling history. Results with no date stay visible in
every window rather than disappearing, which is QGIS's own convention.

Two things to know about this mode:

- **It is not a left join.** The aggregated layer keeps every location on the
  map and gives it NULLs where there is no result, so gaps stay visible. Here
  a location with no result for the chemical produces no feature at all —
  there is no result to hang one on. Add the aggregated layer alongside if you
  need to see which locations were sampled and found nothing.
- **It is capped at 25,000 results** per layer. One chemical over a decade at
  a few hundred locations runs to tens of thousands of features and QGIS
  redraws every one on every pan. When the cap bites, whole locations are
  dropped — never part of a location's history, which would misstate how many
  results a location has — and the panel says how many.

### Depth profiles

**Draw depth profiles where sampled at several depths** (on by default for
*Every result*) replaces the stack at each location with a small profile:
depth down the side with 0 at the top, concentration across. Detections are
filled circles, non-detects hollow squares, and the sampled interval is drawn
as a short bar so a 5 m core does not masquerade as a reading at one depth.

That is the right picture for a boring: every result there shares one
coordinate, so the map alone cannot tell 2 m from 40 m. A location sampled at
a single depth keeps its ordinary graduated marker - a profile through one
point would imply a vertical structure the data does not have.

The value axis always starts at zero, so a uniformly clean hole cannot be made
to look contaminated by a floating scale, and the depth caption at the bottom
is the base of the deepest interval rather than its midpoint.

Point displacement is skipped when profiles are drawn: a profile is already
the whole stack in one picture, so fanning the features apart would scatter
identical copies of it around the point.

Sparklines and the rolling animation are unavailable in this mode, and the
checkbox disables itself to say so. A sparkline *is* an aggregation: it draws
a location's whole series into one marker, so there is nothing left for it to
do when every result is already its own point.

## One matrix, one fraction

Dissolved and total are drawn from the same water on the same day and can
differ by more than an order of magnitude. Aggregating across them reports
whichever happened to be higher — a number that looks entirely plausible on
the map and is wrong.

So the aggregated modes refuse to do it. If the chemical you are mapping
carries more than one fraction and **Fraction** is still on *Any*, the build
stops and offers the ways out: map dissolved, map total, or map every result
separately and keep them apart on the map. Choosing one re-runs the build.

The check is per chemical and per matrix, not per report — a report can mix
fractions overall while the chemical you are mapping carries only one, and
stopping that build would be a false alarm.

The **Fraction** picker now also filters cached and agent data. It previously
shaped only the live request, so on those paths it was ignored: the rows were
already in hand and nothing applied the filter to them.

## Install

**From a ZIP** (the usual way to hand it to someone else):

```
python tools\package.py
```

That writes `dist/equis_loader-<version>.zip`. In QGIS, go to *Plugins →
Manage and Install Plugins → Install from ZIP*, choose the file, then enable
**EQuIS Chemical Explorer**. The panel and its map tool appear on the toolbar
and under *Web → EQuIS*.

`package.py` takes `--output DIR`, `--name NAME`, and `--no-version`. It skips
`__pycache__`, build artefacts, and dotfiles — including any `.env` sitting in
the plugin folder, so a packaged archive never carries an API key. It refuses
to build if `metadata.txt`, `__init__.py`, or `plugin.py` is missing.

**For local development**, copy straight into the profile instead:

```
python tools\install.py
```

which writes to
`%APPDATA%\QGIS\QGIS4\profiles\default\python\plugins\equis_loader`.

## Connecting

The panel needs a site URL and an EQuIS API key (a JWT). On first run it seeds
both from a `.env` file next to the plugin or one directory above it:

```
api_key: eyJhbGciOi...
equis_site: https://yoursite.equisonline.com
```

Credentials entered in the panel are saved to QSettings, so `.env` is only a
convenience for the first launch. Keep `.env` out of version control.

## A whole facility as parquet

A large facility's Analytical Results II cannot be generated inside the time
EQuIS allows one report request (~230 s), so it cannot be fetched live in one
piece. Instead it can be exported ahead of time, as Hive-partitioned parquet
(`matrix=<code>/fraction=<code>/part-*.parquet`) with a `manifest.json`, and
opened in the panel by choosing **Published parquet** as the source and
giving the manifest's folder or URL. Every filter in the panel applies to it
as to a live fetch; the rows are read with DuckDB, which QGIS 4.2 on Windows
ships.

**Opening one** needs only that path or URL. A source without `DETECT_FLAG`
is refused: without it every result would read as a non-detect.

**Producing one** is what
[`.claude/skills/equis-parquet-export/`](.claude/skills/equis-parquet-export/SKILL.md)
does, against a tenant you hold an API key for:

1. survey the facility (rows per matrix, fraction and location);
2. split it into slices small enough to generate, each a saved ARII report
   with its own information agent (EIA) run nightly by EQuIS;
3. collect each agent's stored output, converting units with EQuIS's own
   factors so a column never mixes them;
4. repack by matrix and fraction, and publish the manifest.

It is written as a [Claude Code skill](https://docs.claude.com/en/docs/claude-code/skills)
(`SKILL.md` is the procedure, `references/` the measured failure modes), and
its scripts run on their own with Python and pyarrow:

```
S=.claude/skills/equis-parquet-export/scripts
python $S/survey_facility.py --help
python $S/test_slice_pipeline.py     # the pipeline's bookkeeping
python $S/test_unit_convert.py       # unit conversion by EQuIS's factors
```

## What it loads

**Locations** come from the facility's feature service. That service publishes
three layers; the plugin uses layer 1:

| Layer | Contents |
|---|---|
| 0 | Facility geometry (polygon, from `DT_GEOGRAPHY`) |
| 1 | Location points (from `VW_LOCATION`) — what the plugin maps |
| 2 | Location geometry (polygon) |

Layers 0 and 2 are frequently empty on real facilities, so empty layers are
detected and skipped rather than added blank. Points load in the facility's own
CRS.

**Reports** are generated on demand and can take minutes; output of several
hundred thousand rows is normal. Only reports carrying `SYS_LOC_CODE`,
`CHEMICAL_NAME`, `RESULT_NUMERIC` and `SAMPLE_DATE` can drive the map —
anything else is reported as such, naming the missing columns, rather than
failing obscurely.

The **row limit is off by default**, because truncating a report cuts it
mid-way: later sample dates vanish and a chemical's time series appears to stop
early. A 461,936-row report loads fine — the constraint is memory, not the API.
The plugin keeps only the 12 columns the workflow reads, discarding the rest as
the response is parsed, which cut that report from 706 MB (1,450 MB peak) to
185 MB (332 MB peak). That client-side pruning matters because a cached report
ignores `outFields` and returns all 31 columns regardless. Turn the limit on if
a report is too large for the machine; the status line says so explicitly when
output was truncated.

**The joined layer** carries the location's own fields plus:

| Field | Meaning |
|---|---|
| `result_value` | Aggregated result (most recent or maximum); one result's own value in *every result* mode |
| `result_unit` | Unit of that result |
| `result_date` | Sample date of the winning result |
| `result_detect` | `DETECT_FLAG` of the winning result |
| `result_at_limit` | 1 when `result_value` is a detection limit, not a measurement |
| `result_count` | Results for this chemical at this location |
| `detect_count` | How many of those were detections |
| `analyte` | Chemical the layer was built for |
| `spark_svg` | Path to this location's sparkline (only after *Sparklines on map*) |

Locations with no result for the selected chemical stay on the map with a null
value, so gaps in coverage remain visible. Location codes are matched
case-insensitively between the report and the feature service, since the two do
not always agree on case.

## API behaviour worth knowing

These were established against the live service and drive the implementation.
The Swagger document is unreliable — several documented parameters do not
behave as described, and the failures are silent.

**Authentication and formats**

- **Auth is `Authorization: Bearer <jwt>`.** The Swagger document advertises
  HTTP basic; bearer is what the deployment accepts.
- **`f=geojson` is ignored** on feature queries — the response is always Esri
  JSON, so the plugin converts geometry itself.
- **`spatialReference.wkid` is often `0`** with the real EPSG code in
  `latestWkid`; the plugin prefers whichever is non-zero and falls back to the
  facility's `wkid`.
- **The `Accept` header overrides `oType` on report runs.** Sending
  `Accept: application/json` — the obvious default for a REST client — makes
  `/api/reports/{id}/data?oType=csv` return a JSON envelope instead of CSV:
  `{"stats":…, "schema":…, "<report name>":[ …rows… ]}`, where the row key is
  the report's own name rather than a fixed field. The plugin sends
  `Accept: text/csv, application/zip, */*`, and still handles the envelope
  (falling back to the longest list of objects) in case a report returns it
  anyway.
- **Report output format varies even so**: `application/zip` containing one CSV
  for some reports, `text/plain` CSV for others.

**Geometry**

- **`returnGeometry=true` must be sent explicitly.** The documented default is
  `true`, but the service omits geometry unless asked, yielding attribute-only
  features that silently fail to draw.
- **…and it is a filter, not just a projection.** When a facility's locations
  have null coordinates the service returns *zero* features for
  `returnGeometry=true`, even though `returnCountOnly` reports them and the
  same query without the flag returns them all. Observed at Facility B: 28
  locations, all with null `x`/`y`, so the layer looked empty and nothing
  downstream worked. The plugin retries without the flag when a layer with a
  positive count comes back empty, then says the locations have no coordinates
  instead of showing an empty map.

**Paging**

- **`maxRecordCount` advertises 1000 but is not enforced.** Asking for the
  whole layer in one request works and is by far the most reliable route: one
  request returned 10,349 features in 4 s, where paging the same layer took 11+
  requests and failed partway. The plugin fetches the layer count, then reads
  the layer in a single request when it is under 50,000 features.
- **Paged reads become unreliable near the end of a layer.** Pages at
  `resultOffset` 0–9000 succeeded consistently, but `resultOffset=10000` failed
  repeatedly with HTTP 500 — while `resultOffset=0` with a 10,636-row page size
  succeeded. Paging remains as a fallback, stepping the page size down when one
  is refused.
- **`returnCountOnly` over-reports.** The same layer reports 10,636 features but
  serves 10,349, so a read that comes up slightly short is not necessarily
  incomplete and must not be retried forever.
- The host emits sporadic HTTP 500s and DNS failures; the client retries 5xx
  (not 4xx) and runs report requests sequentially, since firing several
  concurrently provokes 500s.

**Filtering and column selection**

- **`where` and `objectIds` are ignored on facility feature queries.** Every
  clause returns the entire layer: `1=1`, `code = '<one code>'` and
  `type = 'DOMW'` all returned the same 10,636 features and the same rows. The
  clause is still parsed — an unknown column raises HTTP 500 — it just never
  filters. Location filtering on these layers has to happen client-side.
- **`where` does work on report runs**, filtering on report output columns.
  `IN (...)`, `LIKE` and compound `AND` clauses all behave correctly. It is the
  only server-side row filter available: one report went from 461,936 rows in
  123 s to 460 rows in 14 s when narrowed to two locations.
- **The report's own `@sys_loc_codes` parameter is ignored** when passed as a
  query parameter, as is `filters.sys_loc_code` — both returned all 461,936
  rows across 5,764 locations. Use `where` instead.
- **`outFields` is honoured on both feature queries and report runs**, and is
  one of the few documented parameters that behaves as advertised. Asking for 3
  of a layer's 68 fields cut a 1,000-feature page from 1.4 MB to 0.2 MB and
  10.9 s to 2.1 s. Field order is respected and geometry is still returned when
  the coordinate fields are omitted. Unknown field names are dropped silently —
  a typo yields a missing column, not an error.
- **`outFields` and `resultRecordCount` are ignored when a report is answered
  from cache**: the stored output is replayed whole. The same request that
  returned 38 columns / 21 KB live returned an identical 6.5 MB / 32-column zip
  when cached, so the plugin enforces its row cap and column pruning locally.
  `iCacheableReportForceExpire=true` returns HTTP 500 and cannot work around
  this.

**Report data**

- **Non-detects leave `RESULT_NUMERIC` empty**, carrying the limit in
  `REPORT_RESULT_VALUE` plus the detection-limit columns, with
  `DETECT_FLAG='N'`. See [Non-detects](#non-detects).
- Report CSVs carry `X_COORD`/`Y_COORD` in the facility's CRS, so even
  non-spatial report types can be mapped from those columns.

## Layout

```
equis_loader/
  __init__.py        plugin entry point
  plugin.py          menu/toolbar wiring, dock registration
  dialog.py          the dock panel, one collapsible section per step
  equis_api.py       REST client (no QGIS imports; testable standalone)
  report_data.py     analytes, aggregation, time series (no QGIS imports)
  sparkline.py       small SVG sparklines (no QGIS imports)
  layer_builder.py   Esri JSON / report rows -> QgsVectorLayer, styling
  timeseries.py      the embedded matplotlib plot widget
  maptool.py         click a point -> location code
  tasks.py           QgsTask wrappers so the UI never blocks; cache read/write
  cache.py           on-disk cache of features and reports (no QGIS imports)
  settings.py        QSettings + .env fallback, cache location
tools/
  selftest.py        end-to-end check against the live API
  test_parsing.py    offline tests: parsing, paging, analysis, cache (no network)
  test_tasks.py      offline tests: cache decisions in tasks.py (no network)
  test_offline.py    the offline workflow end to end (needs QGIS, no network)
  test_storage.py    geopackage persistence and embedded sparklines (needs QGIS)
  test_roll.py       rolling markers: captions, cycling, teardown (needs QGIS)
  package.py         build a ZIP for QGIS's "Install from ZIP"
  install.py         copy the plugin into the QGIS profile
.claude/skills/
  equis-parquet-export/   a whole facility to parquet: survey, slice, collect, publish
  equis-authoring/        EQuIS reports, agents and dashboards through the REST API
```

`equis_api.py`, `report_data.py`, `sparkline.py` and `cache.py` deliberately
import no QGIS modules, so the API handling, the chemistry logic, the SVG
rendering and the cache are all testable with a plain interpreter. `cache.py`
takes its root path from the caller for the same reason.

## Testing

Offline tests — parsing, paging, non-detect handling, aggregation, and the
cache — need no network and no QGIS:

```
python tools\test_parsing.py
python tools\test_tasks.py
```

The offline workflow test drives the panel itself, so it needs the QGIS Python
but still no network. It seeds a scratch cache and walks the whole path: work
offline → cached facility → cached report → joined layer → time series.

```
"C:\Program Files\QGIS 4.0.2\bin\python-qgis.bat" tools\test_offline.py
"C:\Program Files\QGIS 4.0.2\bin\python-qgis.bat" tools\test_storage.py
```

`test_storage.py` saves a project, clears it and reads it back cold, so it
proves the layers really survive rather than only that a file appeared — and
it asserts that a memory layer still reopens empty, so the GeoPackage result
cannot pass vacuously.

The end-to-end check runs against the live API and needs the QGIS Python:

```
"C:\Program Files\QGIS 4.0.2\bin\python-qgis.bat" tools\selftest.py
"C:\Program Files\QGIS 4.0.2\bin\python-qgis.bat" tools\selftest.py --report
```

`--report` additionally executes a real report, which is slow (minutes).

Last verified against the live site: 26 facilities listed; 10,349 location
points read in a single request; a 461,936-row report parsed to 12 columns in
~40 s; 786 chemicals listed in 0.3 s; PFOA aggregated across 5,312 locations in
2.2 s; a 126-point time series spanning 2007–2025.
