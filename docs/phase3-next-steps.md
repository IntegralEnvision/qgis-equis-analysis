# Phase 3, sequenced — what to test and what to build

Written 2026-09-26, the day US-1 first ran end to end: a live Chromium drew
a histogram from the 3.45-million-row parquet store, a brush reached QGIS,
and the map responded. That changes what is worth doing next, because the
question is no longer "does any of this work" but "does it stay honest".

## Where the stories actually stand

Not what was built — what has been **seen working on real data**. Several
things below are implemented and unverified, and that gap is the plan.

**Updated 2026-10-08** from the commits since this was written. Steps 1–4
are done or nearly; the table below is current, and the original 2026-09-26
reading of each row is kept where it explains why the work was ordered as it
was.

| story | P | state | what is missing |
|---|---|---|---|
| US-1 brush a histogram | P0 | **redraws per analyte** | Wired to the panel (758d5d0). Two stale-chart bugs were found LIVE and fixed: identical SQL over a replaced view served the first analyte's rows (00afe8d, 59328f7). A live re-check after 59328f7 is not recorded |
| US-2 name the population | P0 | **caption correct** | The chart follows the panel's scope, so **Aggregate** changes now reach it; not separately seen live |
| US-3 what a brush does to the project | P0 | **decided and built** | Highlight over an untouched base, node hidden. Still needs the toggle US-3 asks for, or an explicit decision not to have one |
| US-5 non-detects | P1 | **built** | Drawn distinctly and counted in the caption. The **Non-detects** picker redrawing the chart is untested |
| US-6 select on the map, re-bin | P1 | **works** | 01305b1: QGIS's own selection tools drive it, and selection and brush compose. Round trip seen on real data: select 12, narrow to 5, clear |
| US-11 instant brushing | P0 | **measured in isolation** | 49.8 ms brush, 11 ms handler. Never measured end to end through the bridge |
| US-12 chart and map agree | P1 | **asserted** | `tools/test_chart_map_agree.py` (d6787ea) runs the chart's REAL SQL through node against the layer builder |
| US-15 charts refuse mixed units | P1 | **done** | 42e9d93: refuses with the units and counts named; the unit is always on the axis |
| US-16 fraction and method rules | P1 | **fraction done, method open** | D-beside-T refused in the chart (42e9d93). A method gate in the chart is not built |
| US-20 export what is on screen | P1 | **no** | Export ignores the brush entirely |
| — interactive mode | | **built** | c8df57c: single-select picks draw one preview layer, and the chart follows it |
| — opening it from the UI | | **no** | `open_linked_views()` is still console-only. See step 6 |

## The order, and why

### Step 1 — make one live run repeatable (before any new feature)

US-1 worked once. A demo that works once is not a feature, and every story
below assumes the chart can be redrawn.

- [x] **A second draw replaces the first.** It did not at first: the resize
      redraw was an infinite loop, 2-3 redraws a second, seen live and fixed
      in 9155f24. `web/test/resize-guard.mjs` holds it.
- [x] **Changing the analyte redraws.** Wired in 758d5d0. It then drew the
      FIRST analyte's bars under every later name, twice over: Mosaic caches
      on the SQL string and the chart's SQL is identical for every analyte
      (00afe8d for the stats, 59328f7 for the marks).
      `web/test/view-cache.mjs` holds it. A live re-check after 59328f7 is
      not recorded.
- [ ] **Changing matrix / fraction / non-detect redraws**, which is US-2's
      and US-5's untested criterion in one move. `_refresh_chart` follows the
      whole scope, so this should hold; nobody has watched it.
- [x] **A brush survives a redraw, or is cleared deliberately.** Cleared
      before every redraw (758d5d0): a highlight belongs to the population
      the chart was showing.

Acceptance: click three analytes in a row, each redraws, each brushes, and
the map never shows a highlight belonging to a previous analyte.

### Step 2 — US-12, the test that catches the whole class of drift

*The chart's `n` and the layer's feature count must be the same number.*

This is cheap, catches real bugs, and is the only story here that can be
asserted without a browser: both numbers come from the same store, so the
test is a Python one comparing `histogramSql` counts against
`build_joined_layer` output for the same filters.

- [x] Equal for: one analyte; a narrowed matrix; a fraction
      (`tools/test_chart_map_agree.py`, d6787ea, the chart's real SQL via
      node). Also: a filter with no data maps no features.
- [ ] Equal for a method. Not in the test's cases.
- [ ] Equal for the awkward cases the story names — locations present on the
      aggregated layer with NULL results (now EXCLUDED after the join fix of
      2026-09-26, so the chart and the map should agree where before they
      could not), and *every result* mode against the row cap.

### Step 3 — US-15 and US-16 in the chart

The map refuses to aggregate across units, fractions and methods. The chart
does not, and it is now the thing people will read first.

- [x] The chart refuses a mixed-unit selection, naming the units and their
      counts (42e9d93). 140 of 1,034 (chemical, matrix) selections at Facility A
      are mixed, and narrowing the fraction resolves most.
- [x] **The unit is on the axis label, always**, or "value (unit not
      reported)" when there is none.
- [x] The fraction gate reaches the chart: D beside T is refused.
- [ ] The method gate does not. The chart inherits the method filter but
      does not refuse a multi-method selection the way the build does.
- [x] Found on the way: the chart's `maximum` ordered by value alone and
      could pick a non-detect. Now `detected DESC` first, matching the
      store; chart and store agree on all 9 locations of the worst case.

### Step 4 — US-6, select on the map and re-bin

The reverse direction, and the first genuinely NEW capability. Mosaic's
model is that every view is both a source and a target of selection; so far
the map only receives.

- [x] Selecting locations on the map re-bins the histogram (01305b1), driven
      by `layer.selectionChanged`, so every QGIS selection tool works.
- [x] Selection and brush compose: the chart bins the selected locations,
      and the brush highlights within them. The pre-fetch "Only locations
      selected on the map" checkbox keeps its own meaning, and a test asserts
      the two sides of the fetch seam do not leak into each other.

This is where the plugin stops being "a chart that filters a map" and
becomes linked views.

### Step 5 — US-20, export what is on screen

Once a brush means something, the export that ignores it is wrong.

- [ ] The CSV carries only the brushed rows when a brush is active.
- [ ] It states the filter, including the brush range, in a header comment.
- [ ] `filtered_rows` is materialising every row today — 16 s for one
      analyte, 140 s unfiltered — so this wants the same treatment
      `count_for` got, or exporting a large selection will freeze the panel.

### Step 6 — open it from the panel

Everything above runs from the QGIS console, because nothing in the UI calls
`open_linked_views()`. Until a button does, none of it exists for a user.

- [x] **Where the views live (decided 2026-10-09): two tabs in the panel.**
      Tab 1 "Data" is the existing steps, unchanged. Tab 2 "Analysis" holds
      ONE long-lived web view, warmed in the background when data loads
      (not at QGIS start-up, so map-only users pay no Chromium memory), and
      never recreated: a live redraw measured 0.05 s against 0.44 s for a
      new page and ~1.4 s for engine start. Nothing is drawn into the
      hidden tab (no animation frames there; Mosaic's Arrow queries would
      hang); a redraw is owed and done when the tab is shown. The banner
      and status line are shared. `tools/try_analysis_tab.py` times warm-up
      and tab -> drawn. The layout inside tab 2 is the next conversation.
- [ ] A control that opens the histogram beside the map.
- [ ] Then the dock decision below: the web dock replacing the time-series
      dock, behind a setting, matplotlib the default until the web plane
      earns it.
- [ ] A run from the installable zip on a stock QGIS 4.2. Every run so far
      has been from a development profile.

### Step 7 — the pivot, then US-24

The scatter is the first view that needs more than one variable per row,
and it will not be the last. [pivot-design.md](pivot-design.md) works out
the general shape: rows are normal samples, variables are (analyte,
fraction), method/lab splitting is an option, every cell resolves by a stated
rule with its count reported, and every analyte is `_value`, `_detect`,
`_qualifier`, `_detectlimit`. Six decisions were taken 2026-10-08, including
four detection symbols in a scatter and scatters always per sample.

- [x] The pivot's data layer (`equis_loader/pivot.py`, 822c6c7): 33 checks,
      6 deliberate rule breaks caught, Facility A classes exact.
- [x] The scatter, first increment: two analytes, one matrix, four
      symbols, brush to the map, map selection dims it. SQL proven against
      real pivots (`tools/test_scatter_sql.py`).
- [x] **Seen working in QGIS** (2026-10-09: draws, brushes, highlights; As x Cr in SO is the test pair). Run `tools/try_scatter.py` in the console:
      it should report 1,573 / 19,770 / 58 / 41,821 and draw on log axes.
- [x] **The Analysis tab's chooser** (2026-10-10, asked: "assume the pivot
      for all the analytes exists; an analyte chooser for both X and Y").
      Matrix, X and Y pickers above the square scatter. A matrix is pivoted
      once for every analyte it holds (`equis_loader/analysis.py`), and Y
      lists how many samples each analyte shares with X, with those that
      share none set apart and unselectable. A choice survives a change of
      matrix when the analyte is there. Analytes in two units are left out
      by name, with the reason. Choosing a pair draws it and builds its
      scatter map. Pivot per matrix: SO 0.3 s, TAILS 0.4 s (628 columns),
      SE 1.7 s, WS 3.3 s. Water's Mn x Pb (63,222 samples) drew in 1.1 s.
      Answered in a worker since 2026-10-11 (`tasks.AnalysisTask`): QGIS
      stays usable through water's 4 s build, worst main-thread gap 19 ms.
      **Seen in a browser only**: `tools/analysis_preview.py --shots DIR`
      serves the real page and the real host logic with a stand-in for
      QWebChannel, and screenshots a scripted session. The controller and
      worker have run in QGIS's task manager (`tools/test_analysis_task.py`,
      a recorder in place of the web view); the web view, the bridge's
      `chosen` slot and the map layer have not been seen together yet.
- [ ] See the chooser in QGIS: open the Analysis tab with the Facility A
      parquet loaded and the matrix set to SO. It should offer 13 analytes,
      and As x Cr should draw 112 samples and a 10-station scatter map.
- [ ] A control for the histogram in the panel (step 6). After them comes the first
increment: two variables, one matrix, the four detection classes, and a
brush in both directions.

## Deliberately not next

- **US-13 session restore, US-19 saved views.** Both serialise a state that
  is still changing shape.

## The standing risk

Everything above is verified by running it in QGIS by hand. The two tests
that would automate it (`test_web_roundtrip`, `test_webengine`) skip,
because WebEngine needs the application QGIS itself creates. That is not
fixable from here, so the honest mitigation is that each step above ends
with a named thing to look at on screen — not "it should work".
