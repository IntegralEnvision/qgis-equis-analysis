# A different vision: linked views over a column store the plugin owns

**Status:** vision + user stories, September 2026. Nothing here is scheduled,
and nothing here has been built. Written to be argued with.

Companion to the design sketch in
[Mosaic + Qt WebView architecture notes](mosaic-qgis-plugin-architecture.md).
That document answers *how*; this one answers *what for*, and in what order.

---

## 0. The shift, in one paragraph

Today the plugin is a **pipeline**: connect → facility → report → chemical →
a layer on the map. Each step narrows, commits, and hands off. You get one
picture per press of a button, and to get a different picture you go back up
the pipeline and press it again. The proposed shift is from a pipeline to a
**workspace**: one fetched dataset sitting in a local column store, several
views onto it — the map, a histogram, a time series, a depth profile — and a
shared filter that every view both reads and writes. Brushing the histogram
filters the map; selecting on the map re-bins the histogram; the numbers
agree because there is one table underneath rather than a Python row list
beside a layer's attribute table.

The pipeline does not go away. It is how the data gets in, and the five
facts in [selection-ui-guidance.md](selection-ui-guidance.md) still govern
it: EQuIS applies only saved filters, nothing cascades before a fetch, and
fetch cost is dominated by chemicals × years. What changes is everything
*after* the rows land. Today "after" means a dropdown and a rebuild; the
proposal is that "after" means direct manipulation, and is instant.

---

## 1. The story as given

> **US-1.** User loads data with existing plugin UI, pulling relevant dataset
> from server and creating a layer for all Nickel analyses in Soil matrix, and
> a default or configurable color map is utilized to display points on the map
> according to analytical result. User is presented with a histogram of the
> numeric values (analytical results) present in this layer. Rubber band
> selection in the histogram limits the points shown on the map.

This is the right vertical slice to lead with: it touches the existing fetch
path, the existing symbology, one new view, and one new link, and it can be
demonstrated in fifteen seconds to someone who has never seen the plugin.

It also hides four decisions that we should make on purpose rather than
discover in the implementation. Each is promoted to its own story below:

| Hidden in US-1 | Story | Why it is not obvious |
|---|---|---|
| Whose values? Every result, or one per location? | **US-2** | The plugin's existing layers are aggregated (most recent / maximum). A histogram of 5,312 per-location maxima and a histogram of 46,000 individual results are different populations that answer different questions. |
| What does "limits the points shown" mean to QGIS? | **US-3** | Hiding features (`setSubsetString`) and highlighting them (`selectByIds`) are different mechanisms with different consequences for the legend, the feature count, and the rest of the project. |
| Does the colour map move when the brush moves? | **US-4** | If class breaks recompute on the filtered subset, the map recolours under the user's hand and the brush appears to do nothing. If they do not, a narrow brush can leave every visible point one colour. |
| Where do non-detects go on an axis of concentration? | **US-5** | A non-detect is not a small measurement. Plotted at ½ DL it makes a spike that is an artefact of laboratory sensitivity, not of contamination — and this plugin already refuses to tell that kind of lie elsewhere. |

---

## 2. The enhanced set

Stories are grouped into seven epics. Each carries acceptance criteria worth
arguing about and, where relevant, a **⚑ hammer this out** note marking a
decision I do not think should be made by whoever implements it first.

Priority key: **P0** the slice that proves the idea · **P1** what makes it a
tool rather than a demo · **P2** what makes it the *good* tool · **P3** later.

---

### Epic A — The distribution is a control, not a picture

#### US-1 · Brush a histogram to filter the map · **P0**

*As an environmental scientist looking at Nickel in soil across a site, I
want to drag a range across a histogram of the results and see the map keep
only the points in that range, so that I can find where the high tail
actually is without guessing at class breaks.*

Acceptance:
- Fetch as today (facility → report → Nickel, matrix SO). A histogram of the
  layer's numeric results appears beside or beneath the map, populated
  without a second server round-trip.
- Dragging a range on the value axis filters the map to the points whose
  result falls in that range, and the map updates within ~100 ms of the
  drag ending on a 460,000-row report.
- The brush is reversible: drag to resize, click away to clear, and the map
  returns to the full layer.
- The current brush is stated in words somewhere readable — `2.4 – 88 mg/kg
  (312 of 5,312 locations)` — not only as a shaded rectangle.
- Bin count is adjustable, and the default is chosen from the data (Freedman–
  Diaconis or similar) rather than a hard-coded 20.

#### US-2 · Say which population is on the axis · **P0**

*As a user, I want the histogram to tell me whether I am looking at every
result or one number per location, so that I do not read a distribution of
maxima as a distribution of measurements.*

Acceptance:
- The histogram's caption names the population and the filters behind it:
  `Nickel · soil · every result · n = 46,113` versus `Nickel · soil ·
  maximum per location · n = 5,312`.
- Changing the **Aggregate** picker changes the histogram immediately, with
  no rebuild of the map layer.
- ✅ **DECIDED 2026-09-23: the histogram bins what is on the screen.**
  Whatever aggregation the layer is showing, the histogram shows the same
  population. An explicit *show every result* toggle is available but is not
  the default.

  The reason is the brush, not the chart. When the histogram bins the
  displayed values, "keep the points in this range" means exactly what it
  looks like and needs no explanation. Binning every result while the map
  shows maxima would make the brush mean "keep locations having ANY result
  in this range", which is a different filter from the one the picture
  implies - and this plugin's whole discipline is refusing to show a
  plausible number that answers a different question.

#### US-3 · Decide what "limits the points shown" does to the project · **P0**

*As a user, I want brushing to change what I see without quietly changing
what my project contains, so that a colleague opening the saved `.qgz` is not
handed a filtered layer they cannot explain.*

Two candidate mechanisms, and they are not interchangeable:

| | `selectByIds` (highlight) | `setSubsetString` (hide) |
|---|---|---|
| Non-matching points | still drawn, unhighlighted | gone from the canvas |
| Feature count / statistics | unchanged | changes |
| Survives project save | no | **yes — and silently** |
| Matches "limits the points shown" | not really | yes |
| Works on memory layers | yes | yes, but dialect differs from GPKG |
| Reversible by another plugin's action | easily clobbered | no |

Acceptance:
- One of the two is the default and the panel says which, in the same way
  the offline banner says what you are looking at.
- If `setSubsetString` is the default, the layer carries a visible marker of
  the active filter — in the layer name or a banner — that survives a project
  save, in the spirit of the existing `[cached]` suffix. A saved project that
  reopens showing 312 of 5,312 points with nothing to say why is exactly the
  failure the offline work was engineered against.
- ⚑ **Hammer this out:** I lean toward **both**, on a toggle — *Highlight*
  for exploring inside a larger context, *Filter* for making a figure —
  defaulting to Highlight because it cannot damage anything.
- **Superseded in part by US-24**, which supplies a third mechanism better
  than either: *dim, do not remove*. Everything stays drawn, non-matching
  features at low opacity. It preserves the whole site as context and cannot
  damage a saved project. Treat that as the default and this pair as the two
  explicit escapes from it.

#### US-4 · Hold the colour map still under the brush · **P1**

*As a user, I want the colours to mean the same thing before and after I
brush, so that I can compare what is inside the brush with what was outside
it a moment ago.*

Acceptance:
- Class breaks are computed once, over the unfiltered population, and do not
  recompute when a brush is applied. A point that was orange stays orange.
- A *Recompute breaks on current selection* action exists for when that is
  genuinely wanted, and it is a press, not a side effect.
- The existing rules survive the new path: non-detects out of the
  classification and drawn as hollow squares at one fixed size, symbol
  levels so the higher result paints on top.
- The ramp is configurable (QGIS colour ramp picker) and the choice is part
  of the saved view, per US-19.

#### US-5 · Put non-detects somewhere honest · **P1**

*As a user, I want to see how much of the distribution is non-detect, so that
I am not shown a peak that is really the laboratory's reporting limit.*

Acceptance:
- Non-detects are counted and shown distinctly — a separate bar, a hatched
  segment, or a stacked component per bin — never silently merged into the
  detections at ½ DL.
- The **Non-detects** picker (½ DL / full DL / exclude) redraws the histogram
  immediately, as it already redraws the time series.
- The caption states the split: `46,113 results · 8,204 non-detect (17.8%)`.
- ✅ **DECIDED 2026-09-23: linear by default, log as a toggle.**
  Log is arguably the better view of a log-normal distribution, but it
  cannot show zero, it complicates the brush label, and it asks the user to
  understand the axis before they can read the chart. Linear is what the
  numbers on the map already are. The toggle is there for anyone who wants
  the log view, and it can become the default later if use argues for it -
  that is a cheap change, where a log axis that quietly drops zeros is not.

---

### Epic B — Everything is linked, and says what it is filtered by

#### US-6 · Select on the map, re-bin the chart · **P1**

*(US-24 is this story's fully specified form. Read them together; where they
differ, US-24 wins.)*

*As a user, I want to rubber-band a cluster of wells on the map and see the
histogram redraw for just those wells, so that I can ask whether that corner
of the site is different from the rest.*

Acceptance:
- QGIS's own selection tools (rectangle, polygon, freehand, select by
  expression) all drive it — the link is to `layer.selectionChanged`, not to
  a tool the plugin invents.
- A selection of 10,000 features does not stall the UI.
- Round-tripping is stable: brushing the chart, then selecting on the map,
  then clearing the map selection returns to the brushed state and not to
  some third thing. Re-entrancy is guarded, and a test asserts the round trip.
- The existing *Only locations selected on the map (N)* checkbox is
  reconciled with this. Today it is a **pre-fetch** filter — the map
  selection narrows what is asked of EQuIS. Under linked views the same
  gesture also means a **post-fetch** filter. Those are the two sides of the
  seam that [selection-ui-guidance.md](selection-ui-guidance.md) R1 says to
  show, and one checkbox cannot be both.

#### US-7 · One filter bar that lists every active predicate · **P1**

*As a user, I want a single readable statement of everything currently
narrowing what I see, so that I never wonder why the map looks empty.*

Acceptance:
- A chip row above the views: `Nickel ✕` `soil ✕` `2.4–88 mg/kg ✕`
  `312 locations selected on map ✕` `2014–2025 ✕`, each individually
  removable, plus *Clear all*.
- Chips are visually grouped by which side of the seam they sit on — sent to
  EQuIS versus applied here — and removing an EQuIS-side chip is understood
  to invalidate the rows in hand, exactly as the panel already treats a
  changed pre-fetch filter as `stale`.
- Filtering to zero rows says so in the chip row and names the chip most
  likely responsible, rather than showing an empty map and an empty chart.

#### US-8 · Time series inherits the filter · **P1**

*As a user, I want the time-series plot to respect the same filters as
everything else, so that clicking a point after brushing shows me the
brushed series rather than the whole history.*

Acceptance:
- The existing click-to-plot behaviour is unchanged when nothing is brushed.
- With a date or value brush active, the plot shades or dims the excluded
  region rather than cropping it — context is the point of a time series.
- ⚑ **Hammer this out:** does a value brush filter the series at all? Arguably
  a time series should always show every point at that location, with the
  brushed range marked, because the question it answers is "what happened
  here over time", not "what is in range".

#### US-9 · Brush a date axis · **P2**

*As a user, I want a timeline strip showing sample counts over time that I
can brush, so that "the last three years" is a drag rather than two date
pickers and a refetch.*

Acceptance:
- Post-fetch, so instant; distinct from the `@start_date` / `@end_date`
  parameters that shape the request.
- The strip shows sampling density, which is itself informative — gaps in
  monitoring are visible.

#### US-10 · Brush depth · **P2**

*As a user working a soil boring, I want to filter by sampled interval, so
that I can look at the 0–2 m zone across the whole site at once.*

Acceptance:
- Works on `start_depth` / `end_depth`, which the layer already carries in
  *every result* mode.
- Intervals are treated as intervals: a 0–5 m core matches a 0–2 m brush by
  overlap, and the rule chosen is stated.
- Composes with the existing depth-profile rendering rather than replacing
  it.

#### US-24 · Linked scatter and map for sample screening · **P1**

Contributed in full, kept verbatim because it is specified more tightly than
anything above it. It is US-6 made concrete and considerably more demanding.

> *As an environmental chemist reviewing sediment results for a site, I want to
> brush a scatter of two analyte concentrations and see the matching stations
> highlight on the map, and select stations on the map and see their points
> highlight in the scatter, so that I can spot where co-occurring exceedances
> cluster spatially without building a filtered layer by hand.*
>
> **Context.** Both views show the same set of samples. Each sample has a
> location, two concentrations of interest (say total PCBs and total PAHs), a
> depth, and a date. The scatter plots one concentration against the other on
> log axes; the map shows stations as points.
>
> **Acceptance criteria**
>
> 1. **Brush scatter → map.** Dragging a rectangle on the scatter dims every
>    station outside the brush and shows those inside at full opacity, within
>    100 ms. A status line shows how many samples match.
> 2. **Live update.** Dragging or resizing the brush updates the map
>    continuously, not only on release.
> 3. **Clear.** Clicking outside the brush returns the map to all stations at
>    full opacity.
> 4. **Map → scatter.** Dragging a rectangle on the map dims points outside the
>    geographic selection. The scatter's axes do not rescale.
> 5. **Both active.** With a scatter brush and a map selection both present,
>    each view highlights the intersection.
> 6. **Context preserved.** Unselected points and stations stay visible but
>    dimmed in every state, so the full distribution and the full site are
>    always in view.
> 7. **Depth filter applies to both.** With a depth slider at 0–30 cm, only
>    surface samples participate in either brush or in the highlight.
> 8. **Table follows.** A table beneath the views lists the highlighted
>    samples, sortable by any column.
> 9. **Selection is inspectable.** The current selection reads as a plain
>    expression (concentration ranges, map extent, depth) and can be copied.
> 10. **Scale.** With 250,000 samples loaded, criteria 1 and 4 still meet the
>     100 ms target.
>
> **Mosaic constructs.** One shared `Selection.intersect()` holds the state;
> each interactor contributes a clause. Crossfilter resolution is deliberately
> not used, since each view should respond to its own brush. `intervalXY` on
> the scatter and `intervalXY` on the map plot both write to that selection.
> Dim-not-remove is the two-layer idiom: an unfiltered faint mark under a
> `filterBy`-filtered solid mark, in both views. The depth `slider` writes a
> third clause. The count, the readable expression, and the table are clients
> subscribed to that one selection. How the selection gets evaluated and where
> the data lives is somebody else's story.

##### What this story settles

**It decides the Route A / B question in §4.** This is Mosaic-shaped in its
bones — `Selection.intersect()`, two `intervalXY` interactors, dim-not-remove,
four clients on one selection. Hand-rolling it in Qt is precisely the
"re-implement the coordinator" cost Route A defers to its phase 3. If this
story is in scope, **Route B wins**, and §4 is updated to say so.

**It supplies a third answer to US-3, better than either of mine.** US-3
agonised over `selectByIds` (highlight) versus `setSubsetString` (hide).
Criterion 6 says *neither*: keep everything visible, dim what does not match.
That preserves the full site as context, which is what a chemist actually
needs, and it cannot damage a saved project the way a subset string can.

**It confirms R6 twice over.** Criterion 9 — read the selection as a plain
expression and copy it — is only cheap if a selection *is* a predicate.
Criterion 5's intersection is predicate composition. A row-set model gives
neither for free.

**It notes a deliberate deviation worth recording:** `intersect()` rather than
`crossfilter()`. Under crossfilter a view is not filtered by its own brush;
under intersect it is, which is what makes criterion 6's dimming visible in
the scatter itself and not only on the map.

##### What this story opens

**⚑ Whose map is it?** "`intervalXY` on the map plot" is Mosaic vocabulary,
which implies the map is a **Mosaic plot inside the web view** — not the QGIS
canvas. That is the single largest unstated decision in the story, and the two
readings build different products:

| | Map is the QGIS canvas | Map is a Mosaic plot in the web view |
|---|---|---|
| Rectangle select | a QGIS map tool | `intervalXY`, as written |
| Dimming | renderer opacity, or two layers | the two-layer idiom, as written |
| Criterion 10 at 250k | the hard problem, below | Mosaic's home ground |
| Basemap, CRS, projection, layer tree, print layouts | free, they are QGIS | reimplemented or absent |
| What the user has | a QGIS plugin | a dashboard that happens to live in QGIS |

My lean: **the QGIS canvas stays the map.** Everything the plugin is for —
basemaps, CRS handling, the layer tree, print layouts, other people's layers
alongside — lives there, and reimplementing it in vgplot to gain a brush would
be a bad trade. But that makes criterion 10 the load-bearing risk rather than
a footnote.

##### Measured, 2026-09-20 — criterion 10 needs rewriting

The spike ran (`tools/mosaic_poc/brush_scale.py`, full results in its
[README](../tools/mosaic_poc/README.md)). The two-layer proposal below is
**confirmed**, and the criterion it was meant to satisfy is **wrong**.

**The proposal holds.** At 250,000 points with a 2% brush, the two-layer
arrangement renders in **49.8 ms** (memory) and **58.5 ms** (GeoPackage) —
inside the budget. Redraw scales with the selection, exactly as argued. The
alternatives do not come close: `selectByIds` takes 2.1–3.9 s because QGIS
repaints every feature, and `subsetString` takes 0.36 s because the provider
re-filters every row on each render pass.

**The criterion's premise does not hold.** Brushing was never the binding
constraint. Drawing the base layer is, and it is paid on **every pan and
zoom**:

| Points | Base draw (GeoPackage) |
|---|---|
| 25,000 | 392 ms |
| 100,000 | 1,695 ms |
| 250,000 | **3,826 ms** |

Linear at ~15.5 µs per point, and no brush arrangement touches it, because
when the extent changes the base layer genuinely has changed. A map that
takes 3.8 seconds to pan is unusable whatever its brush does.

That also validates something already in the plugin: **the existing 25,000
cap sits almost exactly at a 400 ms pan.** It was set empirically, and it
turns out to be at the edge of tolerable rather than arbitrary.

**So criterion 10 should be replaced by three separate ones:**

1. *Brushing* scales with the selection, not the dataset — **met**, and the
   two-layer arrangement is how.
2. *Panning* has its own budget, and it caps how many features may be vector
   features on the canvas at roughly 25,000–30,000. Criterion 10's 250,000
   cannot be met this way at all.
3. *Wide brushes are out of scope by design.* At 20% of 250,000 — 50,000
   highlighted features — case C costs 397 ms. Nothing rescues that, and
   nothing should: 50,000 solid markers is not a picture anyone reads.

**⚑ The recommendation that follows: the base layer must stop being 250,000
vector features.** It is static, faint, and exists to give context — which is
a picture, not a feature collection. Have DuckDB compute a density grid (it
is a `GROUP BY` on two rounded coordinates, milliseconds at this scale) and
draw it as a **single raster**. Cost becomes O(pixels), constant in the number
of samples, and panning stops scaling with the dataset. The highlight stays
vector, small, and interactive — which is the half already proven.

That is also what Mosaic itself does with large scatterplots: it rasterizes
density rather than emitting a mark per row. The two-layer idiom survives
intact; only the base's *representation* changes.

**Canvas caching confirmed, 2026-09-20.** On the live canvas at 250,000
points, dirtying only the highlight cost 167 ms against 764.5 ms for both
layers — the base was not redrawn, and the cache saved 78% of the frame. The
assumption every offscreen number rests on holds, so the two-layer split is
load-bearing in a real canvas and not only in a renderer job.

(That run also reported a highlight-only repaint missing the budget. It was
measuring 50,000 features rather than 5,000, because the spike left the
highlight holding the previous 20% brush; fixed, and the narrow-brush canvas
figure is still to be measured.) And GeoPackage reported a
refused subset string at 100k and 250k but not 25k, which is more likely the
spike's `featureCount()` guard misfiring than a real refusal.

**⚑ Criterion 10 versus a measurement already in the README.** Unaggregated
layers are **capped at 25,000 features today**, and the README says why:
"QGIS redraws every one on every pan." Criterion 10 asks for 250,000 at
100 ms, continuously during a drag (criterion 2) — ten times the existing cap,
at interactive frame rates.

The way out is to take the story's own two-layer idiom literally in QGIS:

- **Base layer** — all 250,000 samples, faint, *static*. It never changes
  during a brush, so it is drawn once and QGIS can cache it.
- **Highlight layer** — only the matching features, solid. This is what
  redraws on every brush frame, and it is small in exactly the case that
  matters, because a brush that selects most of the data is not a brush
  anyone is reading.

Redraw cost then scales with the **selection**, not the dataset, which is what
makes 100 ms plausible at all. It needs proving, not assuming: a spike that
brushes 250,000 synthetic points on the QGIS canvas is the cheapest way to
find out, and it should happen before this story is scheduled.

**⚑ The data model has to pivot, and pivoting is where the traps are.** Every
story above this one works on long data — one row per result. A scatter of
PCBs against PAHs needs one row per **sample** with two value columns. That
is a DuckDB `PIVOT` or a self-join on
`(location, sample_date, depth, matrix, fraction)`, and three things bite:

- **Unpaired samples.** A sample with PCBs but no PAHs has no place on the
  scatter. Silently dropping it is wrong — that sample was collected and
  analysed. Say how many were dropped, as the story's status line already
  establishes the habit of saying.
- **The Facility A case fans the join out.** US-16 records that one sample is
  routinely analysed by two methods, 162 of 172 intervals at one site. Two
  methods for PCBs joined against two for PAHs is **four points for one
  sample** — a scatter that quietly quadruples its own n. The method filter
  must be resolved to one method per analyte *before* the pivot, not after.
- **"Total PCBs" may not be an analyte.** It is often a sum of congeners
  rather than a reported row. Whether the report provides the total or the
  plugin must compute it changes the pivot and is worth confirming against a
  real ARII output early.

**⚑ Log axes meet non-detects, and a scatter makes it obvious.** US-5 worried
about a spike at ½ DL in a histogram. On a log-log scatter the same convention
draws **crosshair artefacts**: a vertical line of points at the PCB detection
limit, a horizontal line at the PAH one, and a dense corner where both are
non-detect that reads as a cluster of co-occurrence when it is the opposite —
the samples where neither was found. Non-detects need a visual convention of
their own here (open marks, or a margin rug outside the axes), not just a
substituted value.

**⚑ Station versus sample, a third granularity.** US-2 distinguished *every
result* from *per-location aggregate*. This story adds **sample** in between:
the scatter is per sample, the map is per station, and a station has many
samples across depth and date. So "matching stations highlight" means a
station lights up when *any* of its samples match. Whether one match in forty
should look the same as forty out of forty is a real display question —
full opacity either way, or opacity graduated by the matching fraction.

##### Acceptance criteria to add to the ten

- The count in criterion 1 states both granularities: `312 samples at 47
  stations`. They are different numbers and both matter.
- Unpaired and non-detect samples are counted and stated, never silently
  dropped.
- Criterion 10 is measured on the QGIS canvas with the two-layer arrangement
  above, and the result recorded the way the WebEngine results are.

---

### Epic C — The substrate, told from the user's side

These are infrastructure stories written as outcomes, because infrastructure
with no outcome attached is how a rewrite gets justified and then does not
pay for itself.

#### US-11 · Brushing is instant on a real report · **P0**

*As a user of a 460,000-row report, I want every post-fetch interaction to
feel immediate, so that exploring is not rationed.*

Acceptance:
- Re-binning a histogram, applying a brush, and re-aggregating on a
  460,000-row report each complete in under 100 ms warm.
- No interaction that only re-filters rows in hand touches the network.
- The first fetch may pay a one-off cost to load the rows into the store; it
  is measured and stated, and must not exceed today's ~seconds-scale index
  build. Today's `ReportTable` index costs a few seconds and ~35 MB and
  answers in microseconds — that is the bar, not the memory layout.

#### US-12 · The chart and the map cannot disagree · **P1**

*As a user, I want the count in the chart caption and the feature count of
the layer to be the same number, so that I can trust either one.*

Acceptance:
- Both derive from one table. There is no second copy of the rows to drift.
- A test asserts that for a given filter, the chart's `n` equals the map's
  visible feature count — including the awkward cases: locations with no
  result (present on the aggregated layer as NULLs, absent from the chart),
  and the 25,000-result cap in *every result* mode.

#### US-13 · The session survives a restart · **P2**

*As a user who spent four minutes running a report, I want to close QGIS and
reopen the project into the same workspace, so that a restart does not cost
me the afternoon.*

Acceptance:
- The store lives beside the project, as `<project>_equis.duckdb`, the way
  `<project>_equis.gpkg` already does, and zips up with it.
- Reopening restores the rows; restoring the *view* is US-19.
- The existing disk cache is not replaced by this. The cache is keyed by
  site and report and shared across projects; the store is one project's
  working set. ⚑ **Hammer this out:** or they *should* be one thing, and the
  cache becomes a DuckDB file per site. That is a larger change and a better
  end state.

#### US-14 · Ask a question the UI has no button for · **P3**

*As a power user, I want to run SQL against the loaded data, so that a
question nobody anticipated does not require a plugin release.*

Acceptance:
- A console over the loaded tables, read-only by default.
- The result can be sent to the map as a layer, or to a chart.
- Every chart can show the SQL behind it — which is also the best debugging
  affordance the linked-view design has.

---

### Epic D — The honesty the plugin already has, carried into the new views

The current plugin's distinguishing quality is that it refuses to show a
number it cannot stand behind: mixed fractions stop a build, non-detects are
kept and marked, offline data is signalled four ways, a truncated report
stays flagged everywhere it appears. Every one of those guarantees has to be
re-earned in a chart, because a chart is a more persuasive liar than a map.

#### US-15 · Charts refuse mixed units · **P1**

*As a user, I want a histogram that would mix mg/kg with µg/L to refuse and
tell me why, so that I do not brush a range that means two different things.*

Acceptance:
- Unit mixing within one chart is detected and stopped, offering the ways
  out, in the manner of the existing fraction check.
- The unit is on the axis label, always.

#### US-16 · Charts inherit the fraction and method rules · **P1**

*As a user, I want dissolved and total kept apart in a chart exactly as they
are kept apart on the map.*

Acceptance:
- The existing per-chemical, per-matrix mixed-fraction check gates chart
  construction as it gates layer construction.
- Where two analytic methods disagree on one sample — the Facility A case, 162 of
  172 intervals carrying two results — the chart can separate by method, and
  by default does not silently pick one.

#### US-17 · Charts work offline, and say so · **P1**

*As a user on a site with no connection, I want the full linked-view
workspace over cached data.*

Acceptance:
- Every post-fetch view works from cache with no client.
- The amber offline banner extends over the charts; a chart built from cached
  data is marked, and the mark survives into an exported image.
- A truncated report is flagged on the chart too — a histogram of a report cut
  off mid-way is a distribution with a missing tail, and nothing about the
  picture reveals it.

#### US-18 · Changing the non-detect convention changes everything at once · **P2**

*As a user, I want to flip between ½ DL and full DL and watch every view move
together, so that I can see how much of a conclusion rests on that
convention.*

Acceptance:
- Map, histogram, time series and any summary statistic all follow one
  setting, updating together.
- This is a genuinely useful analytical move and worth demonstrating: the
  same site can look materially different under the two conventions, and the
  plugin is well placed to show that in one gesture.

---

### Epic E — Leaving with something

#### US-19 · Save and restore a view · **P2**

*As a user, I want my workspace — which charts, which filters, which ramp —
to be part of the project, so that reopening puts me back where I was and a
colleague opening the same project sees the same thing.*

Acceptance:
- View state is written into the `.qgz` (custom project properties) and
  restored on load.
- A view can also be exported as a small file to hand to someone else.
- ⚑ **Hammer this out:** Mosaic has a declarative spec format
  (`mosaic-spec`, JSON/YAML). If the view plane ends up being Mosaic, that
  spec *is* the saved view and is worth adopting early, even for native Qt
  charts, so the two view planes save the same thing.

#### US-20 · Export exactly what is on screen · **P1**

*As a user, I want to export the brushed subset — the 312 locations, not the
5,312 — to CSV or GeoPackage, so that what I hand over matches what I was
looking at.*

Acceptance:
- Builds on the existing CSV export of the rows behind mapped layers.
- The export carries the filter statement from US-7 as a header comment or a
  sidecar, so the recipient knows what they have.

#### US-21 · A chart in a print layout · **P3**

*As a user producing a figure, I want the histogram in a QGIS print layout
beside the map.*

Acceptance:
- Renders at layout resolution, not as a screenshot of a widget.
- Note: DataPlotly does this and its Qt6 migration is the reference for the
  offscreen-render pitfalls.

---

### Epic F — It has to install

#### US-22 · Install from ZIP on Windows, no compiler, no network · **P0**

*As a user in an environmental consultancy, I want to install this the way I
install every other QGIS plugin, so that I do not need IT to touch my
machine.*

Acceptance:
- `Install from ZIP` works on a stock OSGeo4W QGIS install with no network
  access at install time and no build tools.
- The bundled dependency lives under the plugin directory and is only on
  `sys.path` while the plugin is active — QDuckDB's `embedded_external_libs`
  pattern, built in CI.
- The ZIP stays under a size we agree is acceptable. DuckDB's wheel is
  ~13 MB on Windows and ~15 MB on macOS arm64, and it publishes **no abi3
  wheel**, so in general it is one wheel per (platform × Python minor).
  `arro3-io` is ~9 MB but **is** abi3 from cp311, so one wheel per platform
  covers every Python regardless.
- **The 4.2 floor collapses that matrix, if one thing holds.** QGIS 4.2.1
  ships Python 3.12.13, so a cp312 DuckDB wheel per platform is the whole
  set — three wheels, not a grid. That is contingent on *every* 4.2 build
  shipping 3.12, which is an assumption, not a fact: OSGeo4W, the macOS
  installer and Linux distro packages do not always agree. The probe already
  prints the Python version, and
  [webengine-support.md](webengine-support.md) already has a column for it,
  so **the survey that answers this is the one already being run** — one
  more row per platform settles it.
- ⚑ **Hammer this out:** if 4.2 really is cp312 everywhere, ship one
  platform-specific ZIP per platform (what QDuckDB effectively does). If it
  is not, the choice is a fat ZIP carrying every wheel or a thin ZIP that
  pip-installs on first run and fails gracefully behind a corporate proxy.

#### US-23 · Degrade rather than break · **P1**

*As a user whose plugin install is incomplete, I want the plugin to keep
working minus the new views, so that a packaging problem costs me a feature
and not the tool.*

Scope halved by the 4.2 floor (R5): WebEngine is present on every supported
build, so there is no view-plane fallback to write. What remains is the
substrate, where the failure is a packaging one rather than an environment
one:

- Missing or unloadable `duckdb` → today's in-memory path still runs, and the
  panel says the charts are unavailable and why. This mirrors the existing
  matplotlib guard in `timeseries.py`.
- The wrong wheel for the running Python is the realistic form of this: it
  fails at import, not at install, and the message should say which Python
  the plugin was built for and which one is running.
- ⚑ **Hammer this out:** is a DuckDB-less fallback worth maintaining at all,
  or should a bad install simply refuse to load with a clear message? Keeping
  two data paths alive forever to cover a packaging bug is a real tax on R2.

---

### Epic G — Summaries, statistics and the table

Drawn from [`mapdata`](https://pypi.org/project/mapdata/) 3.23.0, a standalone
map-and-table explorer for coordinate data (read 2026-09-22). It is the same
shape as this vision — a map, a table, plots and statistics all wired to one
selection, everything recomputing when the selection changes — built for
general spatial data rather than for chemistry. Worth mining precisely
because someone else arrived at the linked-view architecture independently
and then had to decide *what to put in it*.

These stories extend earlier epics rather than replacing them; each names
which.

#### What not to build, because QGIS already has it

Listed so nobody spends a sprint on it after reading mapdata's feature list:

| mapdata feature | Already in QGIS |
|---|---|
| Alternative basemap tile servers | XYZ tiles, the browser panel, QuickMapServices |
| Export the map to PNG/JPEG/PostScript | Print layouts (US-21), *Save Map as Image* |
| Select rows by query expression | *Select by Expression* on any layer |
| Coordinate reference system conversion | The whole of QGIS |
| Read CSV/spreadsheet/database | Data source manager, and our own EQuIS path |

The value is not those features. It is **which summaries are worth wiring to
a selection**, and mapdata has thought about that harder than we have.

---

#### US-25 · The attribute table is a linked view · **P1**

*(Extends US-24 criterion 8, which asked for "a table beneath the views".)*

*As a chemist, I want the attribute table to be one of the linked views —
selecting on the map or brushing a chart highlights rows, and selecting rows
highlights the map — so that I can read the actual numbers behind a cluster
without leaving the workspace.*

Acceptance:
- QGIS's own attribute table participates in the shared selection, in both
  directions. It already links to `layer.selectedFeatureIds()`, so the work
  is connecting that to the predicate, not building a table.
- With a brush active, the table can show *only* the selection or the whole
  layer, and says which.
- ⚑ **Hammer this out:** QGIS's table is per layer. A workspace filtered
  across several layers has no single table to show. Either the table is
  scoped to the active layer and says so, or the web view grows its own
  table over the DuckDB query — which duplicates something QGIS does well.

#### US-26 · Write the selection, not only read it · **P2**

*(Extends US-7 and US-24 criterion 9, which only asked to read and copy it.)*

*As a user, I want to edit the filter statement as text and apply it, so that
I can reproduce a colleague's selection exactly, or make one no combination
of brushes can express.*

Acceptance:
- The chip row's statement is editable. Applying it updates every view.
- Invalid input is refused with the reason, and the previous selection
  stands — a filter that silently becomes "everything" is the failure mode
  worth engineering against.
- A statement round-trips: read it, apply it unchanged, get the same
  selection.
- This is nearly free under R6. A selection that is already a SQL predicate
  can be printed and parsed; a selection that were a row set could not.

#### US-27 · The right distribution plot for the question · **P2**

*(Extends US-1, which assumed a histogram was the only view of a
distribution.)*

*As a chemist, I want box plots, strip charts, ECDFs and kernel density
plots over the same selection, because a histogram answers only one of the
questions I have.*

Acceptance:
- Box plot **grouped by a categorical column** — matrix, location, method,
  sampling year. Grouped box plots are the standard site-characterisation
  figure and the plugin has no way to draw one today.
- **Strip chart** for small n, where a box plot's quartiles are noise: a
  location with five results should show five dots, not a box.
- **ECDF**, which is the one worth building first: drawing a screening level
  as a vertical line turns "what fraction of results exceed the criterion"
  into a reading off the axis. That is the question environmental work
  actually asks, and no current view answers it.
- Non-detects keep their convention in all of them (US-5): an ECDF built on
  ½ DL substitutions has a visible step at the limit, and should say so.
- ⚑ **Hammer this out:** Q-Q and kernel density are for checking
  distributional assumptions before a statistical test. They earn their place
  only if US-28 and US-29 land; otherwise they are ornaments.

#### US-28 · Summary statistics that follow the selection · **P1**

*As a chemist, I want a statistics panel that recomputes whenever the
selection changes, so that "what is the summary for these 312 samples" stops
being an export to Excel.*

Acceptance:
- Per selected group: **n, detections, % non-detect, minimum, median,
  maximum, mean, standard deviation**, and the unit.
- **The non-detect convention is stated with every number**, because a mean
  over ½ DL substitutions and a mean over full DL are different numbers and
  neither is wrong.
- Grouping by matrix, location, chemical or year; it is a `GROUP BY` and
  costs milliseconds (R9).
- Copyable as a table, because it will end up in a report.
- ⚑ **Hammer this out — the 95% UCL.** It is *the* statistic environmental
  risk assessment asks for, and doing it properly means distribution fitting
  and censored-data methods (what EPA's ProUCL exists for). Offering a naive
  UCL would be worse than offering none: it would be used. Either scope it
  seriously or state plainly that it is out of scope and why.

#### US-29 · Mann-Kendall trend per location · **P1**

*The strongest single idea in mapdata for this plugin.* It depends on
`pymannkendall`; the test is what regulators expect for groundwater
monitoring, and the plugin already holds exactly the data it needs.

*As a hydrogeologist, I want each monitoring well classified as increasing,
decreasing, stable or no-trend, with Sen's slope, and drawn on the map — so
that I can see where a plume is attenuating and where it is not, without
exporting every well to a spreadsheet.*

Acceptance:
- Mann-Kendall statistic, p-value and **Sen's slope estimator** per location
  per chemical, over the current selection.
- The map shows the outcome directly: arrow markers or a diverging ramp on
  slope, with "insufficient data" distinct from "no trend" — they mean
  opposite things and conflating them is the sort of plausible-looking error
  this codebase exists to avoid.
- **A minimum number of results before a trend is claimed**, in the spirit of
  the sparkline `min pts` spinner. Four points is not a trend.
- **Non-detects are the hard part and must be stated.** Mann-Kendall is
  rank-based, so it tolerates censored data better than a regression does,
  but ties at the detection limit still affect it and substituting ½ DL
  invents ordering that was never measured. Say which convention was used,
  every time.
- ⚑ **Hammer this out:** whether to offer Regional Kendall across wells, and
  seasonal Mann-Kendall where sampling is quarterly. Both are standard; both
  are more assumptions to explain.

#### US-30 · Choose how the colour ramp classifies · **P2**

*(Extends US-4, which held class breaks still under a brush but never asked
where they come from.)*

*As a user, I want to pick the classification method, because quantile breaks
on a log-normal contaminant distribution put most of the site in one class
and hide the gradient I am looking for.*

Acceptance:
- Quantile (today's behaviour, and the default), **natural breaks (Jenks)**,
  equal interval, standard deviation, and pretty breaks.
- Mostly exposing what QGIS already ships — `QgsClassificationJenks` and
  friends exist and are unused. `layer_builder._graduated_ranges` currently
  hard-codes quantiles.
- **Manual breaks entered as text**, because screening levels are the breaks
  that matter: a map classed at the regulatory thresholds reads directly as
  exceedance.
- Non-detects stay out of the classification whichever method is chosen — the
  existing rule, which nothing here may weaken.

#### US-31 · Find duplicates, and tell them from real replicates · **P2**

*(Belongs with Epic D, the honesty stories.)*

mapdata offers "rows with identical values in one or more columns" and
cardinality evaluation as data-management tools. For this data that is not
housekeeping — it is the Facility A diagnosis.

*As an analyst, I want to find rows that are identical except in one column,
so that I can tell a genuine second analysis from a duplicated export.*

Acceptance:
- Report rows sharing (location, date, chemical, depth, fraction) and name
  **which column differs**: `ANALYTIC_METHOD` means two real analyses;
  nothing differing means a duplicated row.
- Report distinct values and counts for any column — the cheapest way to
  find a matrix code that appears twice under different spellings.
- This would have explained the Facility A case in one query instead of by
  inspection, and the README's account of it is the argument for building it.

#### US-32 · Analyte-to-analyte structure · **P3**

*As an environmental forensics analyst, I want to see which analytes
co-occur across samples, so that I can reason about how many sources are
present.*

Acceptance:
- **Correlation matrix across analytes** over the current selection. This is
  US-24's question — do PCBs and PAHs co-occur — asked for every pair at
  once, and it needs the same pivot, with the same traps.
- PCA, or non-negative matrix factorisation as source apportionment. Marked
  P3 deliberately: EPA PMF is the tool people actually use for this, it is a
  specialist workflow, and half-doing it would be worse than pointing at it.
- ⚑ **Hammer this out:** whether this belongs in a QGIS plugin at all, or
  whether the honest answer is to make the *export* good enough that PMF or
  R is one step away. US-20 may be the better home for this story.

---

#### What mapdata's dependency list says about ours

`numpy` and `matplotlib` ship with QGIS, so US-27 and most of US-28 cost
nothing to bundle. `pymannkendall` and `jenkspy` are small and pure Python,
and Jenks is in QGIS already. Nothing in Epic G requires the ~90 MB of native
code the substrate needed — which is worth knowing before the statistics work
gets postponed on a guess about packaging.

---

## 3. Architectural recommendation

### R1. Split the data plane from the view plane, and make the seam the connector

The attached notes bundle two independent bets: **DuckDB + Arrow** as the
compute substrate, and **Mosaic in a `QWebEngineView`** as the view layer.
They have very different risk profiles. The substrate is boring, portable,
and pays for itself on day one (US-11, US-12). The web view is blocked on the
platform most of these users run — see R5.

Keep them apart, and let Mosaic's own connector contract be the seam:

```
query({ type: 'arrow' | 'json' | 'exec', sql }) -> result
```

Anything that can satisfy that contract is a view plane. A native Qt
histogram calls it directly in-process. A `QWebEngineView` calls it over
`QWebChannel`. A browser tab calls it over HTTP against Mosaic's own
`duckdb-server` protocol. Same SQL, same predicates, same saved view.

Adopting the contract before adopting Mosaic costs almost nothing and means
the web view, when it becomes available, is an addition rather than a
rewrite.

### R2. DuckDB replaces `ReportTable`'s storage — not its knowledge

`report_data.py` is 823 lines and most of it is not data structure, it is
**domain judgement**: that a non-detect leaves `RESULT_NUMERIC` empty and the
value must fall back to `REPORT_RESULT_VALUE` and then the detection-limit
columns; that `DETECT_FLAG` and "the plotted number is a limit" are different
facts; that fractions must not be aggregated across; that a chemical is
identified by name *and* CAS RN. Losing any of that would be a regression no
amount of query speed pays for.

The migration is therefore: **the row list and the hand-built index become a
DuckDB table; the rules become SQL views over it.** A `v_results` view that
resolves the non-detect fallback and exposes `value`, `at_limit`, `detect`,
`matrix`, `fraction`, `method`, `depth_from`, `depth_to`, `sample_time` is the
single object every chart and every layer queries. Then:

- `analytes()` → `SELECT DISTINCT chemical, cas_rn, count(*) FROM v_results`
- `aggregate(MOST_RECENT)` → a window function
- `series()` → a filtered ordered scan
- `_build_row_index()` → **deleted**; this is what the database is for

The existing `tools/test_parsing.py` suite (1,569 lines, no QGIS, no network)
is the safety net that makes this tractable: port it to run against both
implementations and require identical answers before the old path is removed.
That is the single most important sequencing constraint in this document.

### R3. Arrow for the wire, and prefer `arro3-io` to `pyarrow`

`boro` — which is structurally this design with a Jupyter comm where we have
a `QWebChannel` — depends on `anywidget`, `duckdb>=1.5.2` and **`arro3-io`**,
not `pyarrow`. That choice is worth copying for the reason that matters to a
QGIS plugin: `arro3-io` publishes **abi3** wheels (`cp311-abi3`) at ~9 MB, so
one wheel per platform serves every Python QGIS might ship. `pyarrow` is
several times larger, drags in `numpy`, and would be a second copy of an
Arrow runtime beside whatever QGIS already links.

Read `boro`'s Python side for the serialization pattern specifically — it is
the closest published thing to what the bridge needs.

Note that `boro`'s own metadata requires Python ≥3.12 while DuckDB's wheels
go back to 3.10; if we borrow the pattern rather than the package, we are not
bound by that floor. Worth checking what QGIS 4.0.2 actually ships before
pinning anything.

### R4. Bundle like QDuckDB does

QDuckDB (Oslandia) is the precedent to follow because it solved exactly the
packaging problem: the DuckDB Python client is not distributed with QGIS, so
QDuckDB embeds it in an `embedded_external_libs/` subdirectory of the plugin
during CI, activated only when the plugin is. Its source is also the reference
for loading the `spatial` extension inside QGIS's Python.

The wrinkle is that DuckDB ships no abi3 wheel, so the embedded copy is
specific to a Python minor version as well as a platform — which turns US-22's
open question into a build-matrix question that should be settled before any
of this is written, not after.

### R5. QGIS 4.2 is the floor, so the web view plane is simply available

**Scope decision, 2026-09-18: nothing below QGIS 4.2 is supported.** That
turns this section from a risk into a constraint, and most of what it used to
argue evaporates.

Measured on 4.2.1; full record and per-column meanings in
[webengine-support.md](webengine-support.md):

```
Windows | QGIS 4.2.1-Belem do Para | Qt 6.11.0 / PyQt 6.11.0 |
Python 3.12.13 | Chromium 140.0.0.0 | webengine=yes channel=yes modules=yes
```

`QWebEngineView` renders, `QWebChannel` carries values both ways including a
slot with a return value (the Mosaic connector's shape), and a custom URL
scheme registered from inside a plugin serves a bundled ES-module app. The
architecture notes can be implemented as written, with the Vite bundle shipped
inside the plugin and no local HTTP server.

Three consequences follow from the floor, and they are worth stating because
they are savings rather than caveats:

- **One Qt.** Everything is PyQt6 / Qt 6.11+. The `qgis.PyQt` compatibility
  dances — scoped-versus-unscoped enums, `QtWebEngineWidgets` versus
  `QtWebEngineCore` class locations, the `QgsRangeSlider` import guard in
  `dialog.py` — are all dead code against this floor.
- **One Python.** QGIS 4.2.1 ships Python 3.12.13, which collapses the
  packaging matrix (see US-22) and, incidentally, clears `boro`'s declared
  `requires_python >= 3.12` — so its pattern can be borrowed or its package
  used directly.
- **No WebEngine fallback to build.** US-23 loses half its scope.

What does *not* follow is that Mosaic is automatically the right thing to
build first. That question is now purely about cost, and it is re-opened
honestly in §4.

### R10. An unaggregated layer plus the Temporal Controller works, inside an envelope

Measured 2026-09-20 (`tools/mosaic_poc/temporal_scale.py`), because R9 reads
as though it forbids the unaggregated-plus-temporal design the plugin already
supports. It does not. Two windows at each of two sizes separate the cost that
scales with the **layer** from the cost that scales with the **window**:

```
ms  =  0.0005 x layer_rows  +  0.0153 x visible_marks
       ^ filter, every frame   ^ drawing, only what shows
```

The draw coefficient — 15.2 to 17.0 µs per mark — independently reproduces
phase 1.5's 15.3 µs per point from an unrelated experiment, which is good
reason to trust the model.

**Filtering is ~32x cheaper per row than drawing is per mark**, so a temporal
window genuinely pays: it converts expensive marks into cheap scanned rows.
That is the reassuring half, and it is why the design is sound.

The catch is that the scan is paid over the **whole layer on every frame**,
so it sets a ceiling regardless of how narrow the window is:

| Layer rows | Filter floor | Marks left inside a 100 ms budget |
|---|---|---|
| 25,000 | 12 ms | 5,400 |
| 50,000 | 25 ms | 4,700 |
| 100,000 | 50 ms | 3,100 |
| 150,000 | 75 ms | 1,600 |
| 250,000 | **124 ms** | **none — the scan alone exceeds the budget** |

Measured end to end: 50,000 results with a 2% window renders in **29 ms**
(smooth, ~34 fps if animated); 250,000 results with a 2% window takes
**145 ms** — about **6 fps**, steppable by hand but not smooth.

**So the guidance is a size envelope, not a prohibition.** Up to roughly
100,000–150,000 results in one layer, with a few thousand visible per window,
an unaggregated layer driven by the Temporal Controller is comfortable. Past
that the per-frame scan eats the budget and the answer is fewer features in
the layer — narrow the fetch, split by matrix or chemical, or aggregate —
rather than a cleverer filter.

Note what this does to the existing cap: **25,000 was set for unfiltered
panning, where every feature is drawn.** With a temporal window active only
the visible marks are drawn, so the same budget supports a far larger layer.
The cap should be expressed as two numbers — features in the layer, and
features visible at once — rather than one.

**⚑ Index the time column only if windows are narrow.** The index is not one
answer, and averaging across selectivities hides that:

| | Unindexed | Indexed | |
|---|---|---|---|
| 50,000 rows, 2% window | 48.3 ms | 29.3 ms | **−39%** |
| 50,000 rows, 10% window | 118.2 ms | 121.3 ms | +3% |
| 250,000 rows, 2% window | 169.7 ms | 145.2 ms | **−14%** |
| 250,000 rows, 10% window | 481.1 ms | 739.7 ms | **+54%** |

Textbook behaviour: a win at high selectivity, a real loss at low, because
past some fraction a sequential scan beats many index seeks. It also answers
the push-down question the spike was built for — the index *does* reach the
provider, so QGIS is not evaluating the expression per feature.

### R9. Pixels are the scarce resource, not rows

Measured across phases 0 and 1.5, and the most consequential thing either of
them found. Same 100 ms frame:

| | Affordable in 100 ms |
|---|---|
| DuckDB rows scanned | **7,400,000** |
| QGIS plain circles drawn | **6,500** |
| QGIS SVG sparkline markers drawn | **125** |

Per unit that is ~13 ns per row against ~15,300 ns per point: **a drawn circle
costs about 1,100x what a scanned row costs**, and one of the plugin's
existing sparkline markers costs ~800 us — 52x a circle, ~59,000x a row.

This is not QGIS being slow. It is structural: a database does set-at-a-time
work in columnar loops; a renderer does per-object work with styling, symbol
levels and blending, then produces pixels. The two jobs do not have comparable
costs and never will.

**It inverts the premise this plugin was built on.** The current architecture
spends its effort on the data side — a row index costing seconds and ~35 MB,
a 25,000-feature cap, sparklines pre-rendered into each feature — because with
rows in Python lists, data genuinely was the bottleneck. Underneath DuckDB it
is not. Four things follow:

1. **The 25,000 cap is a drawing limit, not a data limit**, and should say so.
   It lands at ~400 ms of pan at 15.5 us/point, which is why it was set there
   empirically. Nothing about DuckDB raises it, because it was never about
   the rows.
2. **Aggregation stops being a performance measure and becomes purely an
   analytical choice.** Map the most recent result because "most recent" is
   the question, not because a machine cannot afford 460,000 rows. US-2's
   ⚑ becomes easier to answer honestly once that is true.
3. **Anything that reduces marks beats anything that reduces rows.** Rasterise
   the static base (US-24). Draw only the viewport. Bin before drawing. A
   query that is 10x more complex costs microseconds; a hundred more markers
   costs milliseconds.
4. **The sparkline design is now the wrong shape and worth revisiting.**
   Embedding an SVG per feature makes each mark ~52x more expensive to draw —
   spending the scarce resource to save the abundant one — and costs ~1.2 KB
   per location in the layer besides. It was a good answer to "how do I show a
   trend per location without a second query" when queries were expensive.
   Queries are no longer expensive. Open question 4 in §5 asked whether
   sparklines stay as they are; this is the argument that they should not.

The general rule for everything after the substrate lands: **spend queries
freely, spend marks carefully.**

### R6. Model a selection as a predicate, not a row set

This is the idea worth taking from Mosaic whether or not we take Mosaic. A
selection is a **SQL predicate** — `"value" BETWEEN 2.4 AND 88` — that views
compose, rather than a list of feature IDs that views intersect. Predicates
compose cleanly, survive re-aggregation, are trivially renderable as the
US-7 chip row, and serialize into the saved view of US-19 for free. Row sets
do none of that.

The one place row IDs are unavoidable is the hand-off to QGIS, which speaks
`QgsFeatureId`. Two cautions there:

- **`fid` discipline.** The DuckDB table must carry a key that is stable
  against the QGIS layer's feature IDs. GeoPackage fids are stable;
  **memory-layer fids are assigned on insert and are not**, so a rebuild
  renumbers them and a saved brush points at the wrong features. Either
  GeoPackage storage becomes a precondition of linked views, or the plugin
  mints its own key and joins on it. The second is safer and is a small
  change to `layer_builder`.
- **Large selections.** Past roughly a thousand ids, `fid IN (…)` becomes a
  pathological SQL string; write the ids to a temp table and join. The
  attached notes flag the same threshold.

### R7. Extend the state machine; do not add flags to the panel

`panel_state.py` exists because the panel once answered "may I do this?" from
six scattered variables and produced three bugs in a week. Linked views add a
new axis — *a filter predicate is active, and views are mid-update* — and the
temptation will be a `_brushing` boolean beside the others. That is the exact
mistake the module's docstring documents. Whatever re-entrancy guard the
map↔chart round trip needs belongs in `PanelState` with its transitions
written down and exercised by `tools/test_state.py`, which already does this
with a plain interpreter and no QGIS.

### R8. Keep the QGIS-free modules QGIS-free

`equis_api.py`, `report_data.py`, `sparkline.py` and `cache.py` import no
QGIS, which is why 1,569 lines of parsing tests run under a plain
interpreter. A DuckDB layer belongs on the same side of that line: a
`store.py` that takes its path from the caller, exactly as `cache.py` does,
and knows nothing about layers. Then the substrate migration of R2 is
testable without QGIS at all, which is what makes the dual-implementation
comparison affordable.

---

## 4. Suggested sequencing

Phases 0 and 1 are not in question. Phases 2 onward are, because the QGIS 4.2
floor removed the reason this document originally gave for building a native
chart first.

| Phase | Contains | Proves |
|---|---|---|
| **0. Prove the packaging** | US-22 alone. A do-nothing plugin build that imports `duckdb` and `arro3-io` from `embedded_external_libs` on Windows, macOS and Linux — and records each one's Python version, settling the cp312 question. | The riskiest and least interesting part is solved before anything is built on it. If this cannot be made to work, everything downstream changes. |
| **1. Substrate, invisible** | US-11, US-12, R2, R8. Rows land in DuckDB; `report_data` becomes SQL views; `test_parsing.py` passes against both implementations. No UI change except speed. | The domain knowledge survived the move. |

**Phase 2's view-plane mechanics are already proven.**
[`tools/mosaic_poc`](../tools/mosaic_poc/README.md) put a translucent
`QWebEngineView` over the map canvas on QGIS 4.2.1 and confirmed, 2026-09-19,
that it composites without hiding the map, stays anchored through pan and
zoom, hands the mouse back and forth on demand, shares one brush across
several charts with dim-not-remove, and draws the panel's real loaded report.
None of that needed Mosaic, which is why it was worth doing first: whatever
the view plane turns out to be, it sits on those mechanics.

Then one of two routes.

### Route A — native chart first (the original plan)

| Phase | Contains |
|---|---|
| **2.** US-1 – US-5, US-20, R6, R7: matplotlib histogram, `SpanSelector`, predicate → map |
| **3.** Epic B linked views, hand-rolled over the connector contract |
| **4.** Mosaic, replacing the native charts behind the same contract |

Cheap to start: matplotlib is already a dependency and already used by
`timeseries.py`, `SpanSelector` is a rubber-band brush out of the box, and
US-1's whole view layer is a `FigureCanvasQTAgg`, a `hist()` call and a
callback. No npm, no Vite, no second language in a repo whose test culture is
"runs under a plain interpreter". It also keeps the JS toolchain out of the
way while the substrate migration — the genuinely hard part — is still
settling.

The cost is that phase 3 is where it hurts. Hand-rolling a shared predicate
across five linked views, with cross-filter exclusion and cached
re-aggregation, is re-implementing Mosaic's coordinator by hand. That is the
part of Mosaic worth having, and Route A writes it twice.

### Route B — Mosaic as soon as the substrate lands

| Phase | Contains |
|---|---|
| **2.** Vite build in CI, bundle served from a custom scheme, the `QtChannelConnector` and bridge of the architecture notes, US-1 as a vgplot histogram |
| **3.** Epic B, US-24 at its centre — largely configuration, since cross-filtering is what Mosaic *is* |
| **4.** US-19's `mosaic-spec` saved views, US-21 layouts |

More to stand up before anything is visible: a JS build in a Python repo, a
bundle checked in, a scheme handler, and a debugging story that now spans two
runtimes. In exchange, Epic B — which is the actual product, not US-1 — mostly
stops being code.

### Which

This turned on one question — is US-1 the deliverable, or is Epic B? —
and **US-24 answered it.** That story is specified in Mosaic's own vocabulary
(`Selection.intersect()`, two `intervalXY` interactors, dim-not-remove, four
clients on one selection) and is not a chart but a workspace. Building it
Route A means writing Mosaic's coordinator by hand and then deleting it.

**Route B**, with two amendments:

- **Keep the native histogram as a phase-1 spike, not as phase 2.** A day's
  work, thrown away on purpose. It validates the R6 predicate model against
  real chemistry while the bundle work happens, and gives something to show.
  What it must not do is acquire users, because then it has to be maintained.
- **Phase 1.5 has run, and it changed a story rather than a schedule.**
  The two-layer arrangement is confirmed; criterion 10's 250,000-point
  premise is not, because panning rather than brushing is the binding
  constraint. See the measurement under US-24. The follow-on work is a
  rasterized base layer, not a faster brush.
- **Originally: add a phase 1.5 to prove criterion 10 on the QGIS canvas.** US-24 asks for
  250,000 points highlighted within 100 ms, continuously during a drag. The
  plugin caps unaggregated layers at 25,000 today for a measured reason. The
  two-layer arrangement in US-24 should make redraw scale with the selection
  rather than the dataset, but that is an argument, not a measurement, and
  everything visual in Epic B rests on it. Spike it with synthetic points
  before scheduling US-24; it is a day, and it can invalidate a quarter.

Phase 0 before phase 1 is the one ordering I would defend hardest. Everything
else can be argued.

---

## 5. Open questions

1. **Does the pipeline survive intact?** This document assumes the EQuIS fetch
   path is unchanged and only "after the rows land" is re-imagined. Is that
   right, or does a local store change what we should be fetching — e.g.
   fetching wider and filtering locally, now that local filtering is free?
   The five API facts say fetch cost is chemicals × years, which argues for
   fetching *more chemicals, fewer years* and exploring locally. That would
   be a real change to the pipeline and it is not in these stories.
2. **One store or two?** US-13's question: does the on-disk cache become
   DuckDB, making cache and working set the same object?
3. **Geometry in DuckDB?** The attached notes say start attribute-only and add
   `ST_*` when a chart needs it. Agreed — but "brush a map extent to filter
   charts" (US-6's mirror image) is the first thing anyone will ask for, and
   it needs either spatial predicates in the store or QGIS doing the spatial
   work and handing over ids. The second is fine and cheaper.
4. **What happens to sparklines and the roll?** They are an aggregation
   rendered into a marker, and they predate linked views. Under a workspace
   model a sparkline is arguably a tiny linked chart per feature. Do they stay
   as they are, or become the first thing built on the new substrate?
   **Partly answered by R9:** an embedded SVG marker costs ~52x a plain
   circle to draw, which is spending the scarce resource to save the abundant
   one. The overlay in `tools/mosaic_poc` already draws live, brushable
   series without putting anything in the layer at all, which is the shape
   this should take.
5. **How many chemicals at once?** Everything above is written for one
   chemical on one axis. The multi-chemical cases the plugin already supports
   — several layers, rolling markers — do not have an obvious histogram.
   A chemical × chemical scatter is the natural linked view for that question
   and is not yet a story here.
6. **Who is this for?** The stories assume an environmental scientist who
   knows QGIS. If the audience includes people who do not, the workspace model
   is a bigger jump than the pipeline and the sequencing should reflect that.
