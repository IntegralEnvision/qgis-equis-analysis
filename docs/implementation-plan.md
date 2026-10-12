# Implementation plan

Ordered work for the linked-views vision, with acceptance criteria. Written
2026-09-20, at the point where every architectural question has been measured
and nothing has been wired up yet. **Updated 2026-10-08**: phases 1b and 2 are
built, the histogram runs live against the store, and the current sequence
lives in [phase3-next-steps.md](phase3-next-steps.md).

Read [CLAUDE.md](../CLAUDE.md) first — it carries the invariants that decide
whether a change is correct, and the measurements that decide whether it is
worth making.

## Where things stand

| | Status |
|---|---|
| WebEngine, QWebChannel, ES modules from a custom scheme on QGIS 4.2.1 | ✅ measured |
| vgplot bundles to 813 KB of ESM with esbuild, no native build | ✅ measured |
| DuckDB + arro3 bundled into an installable 24 MB plugin zip | ✅ measured |
| Overlay: translucent web view on the canvas, anchored, brushable, real data | ✅ measured |
| Two-layer brushing scales with the selection, not the dataset | ✅ measured |
| `ReportStore` matches `ReportTable` on every question asked | ✅ measured |
| The panel reads from DuckDB (`_analysis`), parquet sources included | ✅ built, verified under QGIS 4.2.1 |
| Histogram over the store, brushing the map within budget (US-1) | ✅ seen live 2026-09-26 |
| Chart follows the panel; refuses mixed units and fractions; map selection re-bins it (US-6) | ✅ built; see phase3-next-steps.md for what has been seen |
| Chart `n` equals the layer's feature count (US-12) | ✅ asserted |
| **A control in the UI that opens the linked views** | ❌ console only |
| **Pivoted result set for multivariate views** | 📐 designed: [pivot-design.md](pivot-design.md) |

The plugin as shipped draws maps and time series exactly as before. The
linked views exist, but nothing in the UI opens them yet.

---

## Phase 1b — put the store behind the panel

The only phase with no visible change. Its value is that it removes data cost
from every later design conversation.

**1b.1 Build the store when a report loads.** — **written 2026-09-23,
verified under QGIS 4.2.1 the same day** (one item still needs a human).
- [x] Built by `BuildStoreTask` off the UI thread. It writes a FILE and
      closes it; `_on_store_ready` opens it again on the main thread. A
      DuckDB connection should not be handed between threads, and a file
      handoff is unambiguously safe where passing a live connection is only
      usually fine. `tools/test_store_handoff.py` drives exactly that, from a
      real worker thread, and checks 51 questions answer identically after
      the reopen.
- [x] Failure is survivable: `duckdb` missing, disk full, anything raised —
      logged to the QGIS log, `_report_store` stays None, the Python path
      carries on. The user asked for a report and got one; losing the
      accelerator is not their problem.
- [x] **Verified under QGIS 4.2.1, 2026-09-23.** Ingest on real cached
      reports: 79,105 rows 3.4 s, 123,752 rows 3.4 s, 222,571 rows 6.7 s
      (~13.8 s extrapolated to 460,000). The FIRST measurement was 32.1 s
      for 222,571 - see phase1-substrate-parity.md; `parse_date` was raising
      16 exceptions per row over a date format Analytical Results II actually
      emits. Fixed before accepting the number.
- [ ] Still unverified: that the panel stays responsive during the build.
      That needs a human watching a real fetch, not a test.

**1b.2 Switch the panel's reads over.** — **written 2026-09-23,
verified under QGIS 4.2.1 the same day.**
- [x] One seam: the `_analysis` property returns the store when it is ready
      and the table otherwise, and it is the only place that decides. No
      caller asks which it has. The invariant that makes every existing
      `if self._report_table is None` guard still correct is written on the
      property: the store is set only while a table is set, and
      `_discard_store()` runs wherever the table is cleared.
- [x] `analytes`, `rows_for` (x2), `fractions_for`, `aggregate` and the
      click-to-plot `series` all read `_analysis`.
- [x] **Both sparkline paths converted to `series_many()`.** They looped
      `series()` per location — and the roll path per location *per analyte*,
      which is the worst case the store's fixed per-query cost punishes.
      `ReportTable` grew `series_many()` too, as a plain loop, so the call
      site is written once and the seam stays honest.
- [x] `filtered_rows` deliberately still reads the table; see 1b.5.
- [x] **Verified under QGIS 4.2.1, 2026-09-23:** `test_offline.py`,
      `test_storage.py`, `test_roll.py`, plus `test_store_parity.py` and
      `test_store_handoff.py` against QGIS's own duckdb 1.5.4. All pass; the
      roll path included.

**1b.3 Extend the parity treatment to `test_parsing.py`.**
Its analysis scenarios — the Facility A two-method case, multi-fraction, depth
columns, the row cap — are built from real observations and are different
coverage from the parity fixture's synthetic combinations.
That suite exercises `ReportTable` directly. Its *parsing* assertions stay as
they are; its *analysis* assertions should run against both implementations.
- [ ] Analysis assertions parameterised over both.
- [ ] Any new rule added to either implementation gains a fixture row.

**1b.5 Where an export gets the report's own columns. — DECIDED
2026-09-23: nowhere new. `ReportTable` stays.**

`filtered_rows()` returns the *report's* columns in the report's order, which
the store's canonical 18 do not carry. The plan here was to build a
`with_raw=True` store on demand at export time, because `with_raw` was
measured at roughly double the ingest (7.35 s -> 14.73 s).

**That premise did not survive re-measurement.** The doubling was mostly
`parse_date` raising 16 exceptions per row (see 1b.1), and both paths were
paying it. With that fixed, on a real 222,571-row x 22-column report:

| | ingest | on disk |
|---|---|---|
| canonical only | 8.2 s | 4.3 MB |
| with raw columns | 10.2 s | 10.0 MB |

1.2x, not 2x. But the more useful observation is that **the question was
malformed**: `_analysis` guarantees the store is only ever set while a table
is set, so `ReportTable` is ALREADY resident whenever the store is. The
export path reads it directly and is correct today, at no cost. Nothing needs
building.

- [x] Export keeps reading `ReportTable`. The comment at the call site in
      `dialog.py` says why, and points here.
- [x] No on-demand store, no `with_raw` by default. Both would add code and
      cost to make an already-working path work differently.

**1b.4 Delete `_build_row_index` and the paths that only served it. — NOT
DOING, 2026-09-23.**

This only made sense as a consequence of 1b.5 going the other way. Measured
on the same report: the index costs **0.28 s to build and 19.5 MB**, against
**174 MB of rows that stay resident regardless** because `filtered_rows`
needs them. Deleting it would remove 11% of the memory the table occupies and
save a quarter-second, in exchange for touching `_candidate_rows`, `series`,
`aggregate` and `rows_for` - the paths the parity suite exists to protect.

The index is not dead code: `filtered_rows` uses `_candidate_rows`, so it
still earns its place. Revisit only if `ReportTable` can be dropped from
memory entirely after ingest, which needs a different answer to 1b.5 than the
one above.

**Done when:** the panel reads from DuckDB, nothing looks different, and the
README's "the report is fetched once and held in memory" paragraph is
rewritten to describe what actually happens now.

---

## Phase 2 — the web view plane

Route B of the vision doc's §4. The mechanics are proven by
`tools/mosaic_poc/overlay.py`; what is new is the JS toolchain.

**2.1 esbuild bundle of vgplot, checked in. — BUILT 2026-09-23.** `web/`.
- [x] One ESM file, **583 KB** (the 813 KB estimate was high), from
      `@uwdata/vgplot` 0.31.0 + `esbuild` 0.28.2, both pinned exactly.
- [x] **Reproducible, and it is checkable**: `npm run check` rebuilds to a
      temp directory and diffs sha256 against what is committed. That turns
      "reproducible from a lockfile" into a claim that can fail in CI rather
      than an aspiration that rots. Verified: identical hashes.
- [x] The repo carries the output; `node_modules` (180 MB, 97 packages) is
      gitignored. Node is needed to REBUILD the bundle, never to install or
      run the plugin.
- [x] `equis_loader/web/` ships in the zip - checked through package.py's own
      collector in `tools/test_web_bundle.py`, along with the hashes still
      describing the files, so a hand-edited bundle is caught before a
      rebuild silently reverts it.
- [ ] CI does not run `npm run check` yet; there is no CI config in the repo.

**2.4 `QtChannelConnector` and a coordinator in the page. — BUILT
2026-09-23, unexercised in a real view.**
- [x] `web/src/connector.js` satisfies Mosaic's `query({type, sql})` and
      unwraps the bridge's envelope into a resolved value or a real rejected
      promise. Decodes Arrow with flechette, already a vgplot dependency.
- [x] `web/src/app.js` builds a Coordinator, routes page errors to the QGIS
      log, and calls `bridge.ready()` LAST.
- [x] Loaded in a real QWebEngineView: `tools/test_web_roundtrip.py`
      passes in the QGIS 4.2.1 console (see 2.5a).

**2.2 Custom URL scheme handler in the plugin. — BUILT 2026-09-23,
verified under QGIS 4.2.1.** `equis_loader/web_assets.py`.
- [x] `ensure_scheme()` is idempotent; a scheme cannot be registered twice
      and a plugin reload will try.
- [x] A reload REPOINTS the existing handler rather than installing a rival.
      Qt keeps the first handler and ignores the newcomer, so the naive
      version would serve stale files for the rest of the session with no
      error anywhere. Checked live: same object, new root.
- [x] Path containment is a separate pure function (`resolve`) so it tests
      without Qt. Traversal, URL-encoded traversal and a sibling directory
      sharing the root's prefix all fail closed - `commonpath`, not
      `startswith`.
- [x] The reply QBuffer is parented to the job, per the probe's finding.
- [x] **New hazard found while building this:**
      `QWebEngineProfile.defaultProfile()` ABORTS THE PROCESS when no
      QApplication exists - a hard exit with no traceback, which first
      appeared as a test run producing no output at all. `install()` now
      checks and raises instead.

**2.3 The bridge: `query(sql, type) -> Arrow IPC`. — BUILT 2026-09-23,
verified under QGIS 4.2.1.**
Split in two so the serialization is testable without Qt:
`bridge_payload.py` decides what crosses the wire, `web_bridge.py` is the
QObject around it.
- [x] Prefers QGIS's own **pyarrow**, falls back to **arro3**. Finding 1
      said to avoid `.arrow()` because it drags in ~40 MB - but QGIS 4.2.1
      already ships pyarrow, numpy AND duckdb, so avoiding it saves nothing
      and vendoring arro3 would add a second Arrow runtime. **A stock QGIS
      4.2 install needs nothing vendored for this.** Both paths are forced
      in `tools/test_bridge_payload.py`; see phase0-substrate.md finding 1a.
- [x] A `ready()` handshake, not `loadFinished`.
- [x] Slots with return values work. `tools/test_web_bridge.py` invokes
      `query` through `QMetaObject.invokeMethod` with `Q_RETURN_ARG` - the
      path QWebChannel itself uses - so a mistyped `@pyqtSlot` that is
      callable from Python but invisible to the page cannot pass.
- [x] Errors travel as a JSON envelope (`{ok, kind, data|error}`), because a
      slot cannot raise across the channel: PyQt logs it and JS receives
      null, which a connector cannot tell from an empty result.
- [x] The bridge holds a CALLABLE returning the connection, not a
      connection. The panel rebuilds its store on every fetch; a bridge
      pinning the old one would answer from a report the map no longer shows.
- [x] Wired into the panel through `LinkedViewsController`, which
      `open_linked_views()` builds on demand (2.5).

**2.5a The view itself. — BUILT 2026-09-23, needs a console run.**
`equis_loader/web_view.py` constructs the QWebEngineView, registers the
scheme, installs the handler, wires the channel and loads the bundle - in
that order, each step for a documented reason.
- [x] Construction order: scheme, handler, channel, THEN load.
- [x] A ready timeout, so a page that never connects says so instead of
      sitting blank.
- [x] **`tools/test_web_roundtrip.py` PASSES** in the QGIS 4.2.1 console,
      2026-09-23. All five: the page reports ready, a JSON query round-trips
      to DuckDB, an Arrow query decodes in the page, the connector answers
      Arrow directly, and a bad query rejects rather than resolving empty.
- [x] The view must be SHOWN. Mosaic defers only Arrow requests to
      `requestAnimationFrame`, and Chromium fires no frames for an invisible
      surface, so a hidden view answers JSON and hangs Arrow forever with no
      error anywhere. See webengine-support.md.

**2.5 US-1: a vgplot histogram over the loaded report. — RENDERS
2026-09-23**, verified in the QGIS 4.2.1 console: `svg=yes rects=40`,
5 bins from Freedman-Diaconis, over a live DuckDB query.
- [x] Caption names the population and filters (US-2). Measured output:
      `Nickel · SO · every result · n = 120 · 24 non-detect (20.0%) · mg/kg`
- [x] Non-detects shown distinctly, not merged at 1/2 DL (US-5) -
      `fill: 'detected'` stacks them per bin.
- [x] **Two vgplot facts that cost a run each:**
      `plot()` calls `connect(this, ...marks)` with `this` as the API
      CONTEXT, so directives imported from the module connect their marks to
      nothing - the element appears, stays empty, and reports no error. And
      `plot()` returns the container BEFORE the chart is drawn: vgplot fills
      it asynchronously once its bin-extent queries settle, so a check that
      looks once sees nothing.
- [x] **Brushing filters the map within 100 ms**, measured end to end on
      `linked_views.py` at 250,000 features with 5,000 brushed: first brush
      36.7 ms, every brush after 11.0 ms. With the POC's 49.8 ms render that
      is ~87 ms of the 100 ms budget.
      The naive version was 858 ms, because scanning the layer per brush
      costs O(dataset) where the whole arrangement exists to cost
      O(selection). A provider-side `IN (...)` expression was far worse
      still - 151 SECONDS - so the answer is a plain dict built once per
      layer, and `prepare()` builds it at layer-build time rather than on
      the user's first drag.
- [x] Wired into `dialog.py` 2026-09-23: `open_linked_views()` builds the
      controller on demand, `prepare()` runs when a layer is built, and both
      `closeEvent` and `shutdown` tear the view down - shutdown before the
      store, because a page mid-query against a closed connection crashes
      rather than erroring. `tools/test_import_smoke.py` asserts that
      ordering.
- [ ] No BUTTON opens it yet. `open_linked_views()` is callable from the
      console; putting it in the UI is a deliberate next step, not an
      oversight - the panel's layout deserves its own thought. Now step 6 of
      [phase3-next-steps.md](phase3-next-steps.md).

**2.6 Brush → map, as two layers. — MAP HALF BUILT 2026-09-23.**
`equis_loader/brush_layer.py`, `tools/test_brush_layer.py` (16 checks).
- [x] Static base, small highlight layer above it.
- [x] Dim-not-remove: the base keeps every feature, unfiltered and
      unselected. The suite asserts that explicitly, because it is what
      makes the canvas cache work - a highlight that touched the base would
      dirty it and cost 597 ms a frame at 250,000 points.
- [x] Cost scales with the SELECTION, re-measured on this implementation:
      500 brushed of 25,000 -> 3.7 ms; 5,000 of 250,000 -> 15.0 ms. With the
      POC's 49.8 ms render that is ~65 ms of the 100 ms budget.
- [x] A brush too wide to read says so rather than rendering a slow,
      unreadable frame: past ~20,000 highlighted markers the picture is a
      solid mass and the advice is to narrow the range.
- [x] Connected to the histogram's brush (4de5281), inside the 100 ms
      budget.

**Done when:** a chemist can brush a histogram and watch the map respond, on
a stock QGIS 4.2 install, from a zip.

---

## Phase 3 — linked views

The sequence actually followed is in [phase3-next-steps.md](phase3-next-steps.md);
it ordered the histogram's honesty (repeatable redraws, US-12, the unit and
fraction gates, US-6) ahead of new views. The items below are the original
outline.

**3.1 US-24, the linked scatter.** The fully specified story; read it before
anything else here. **The general shape it needs is worked out in
[pivot-design.md](pivot-design.md)**, measured on Facility A: rows are normal
samples (QC excluded), variables are (analyte, fraction) with method/lab
splitting only as an option, every cell resolves by a stated rule with its
count reported, and every analyte is four columns, `_value`, `_detect`,
`_qualifier` and `_detectlimit`, with detection taken from the database's
`DETECT_FLAG` and never inferred.
- [x] The pivot builder and its column dictionary (pivot-design.md,
      Engineering). Each cell resolves to ONE result by D3 even across
      methods, which is what keeps the Facility A two-method case from fanning
      out to four points per sample; splitting by method or lab is an
      option (D2).
- [x] The cell rule (D3) asserted against the store's `aggregate`, so a
      pivot cell and a mapped maximum cannot pick different rows.
- [x] Unpaired samples counted and stated, never silently dropped (D6).
- [x] Four symbols for the four detection classes, non-detects at their
      limits, stacking accepted (decision 5). Of the Mn×Pb pairs in WS/T,
      97.5% involve at least one non-detect, so this decides the picture.
- [x] Per sample only: no location-aggregated scatter (decision 6).
- [x] Built and seen in QGIS 4.2.1, 2026-10-08/09: pivot.py (822c6c7), the
      scatter (4d40f7d), its bivariate map after biscale (fafbd75), open-time
      feedback (882dbe7), log axes that name what they leave out (2a38762),
      field duplicates and replicates not plotted (c4dfba3, 8d85fe0).

**3.2 Rasterise the static base.** The finding that matters most for scale:
a DuckDB density grid drawn as one raster makes the base cost O(pixels)
instead of O(points), so panning stops scaling with the dataset.
- [ ] Panning a 250,000-result view costs about what a 25,000-result view
      costs today.

**3.3 The filter statement (US-7).** Chips for every active predicate,
grouped by which side of the fetch seam they sit on, individually removable.

**3.4 Revisit sparklines (R9, open question 4).** An embedded SVG marker
costs ~52x a plain circle to draw. The overlay already draws live, brushable
series with nothing in the layer.

---

## Phase 4 — summaries that follow the selection (Epic G)

Added 2026-09-22 from [`mapdata`](https://pypi.org/project/mapdata/), which
arrived at the same linked-view architecture independently and had to decide
what to put in it. None of this needs new native dependencies: `numpy` and
`matplotlib` ship with QGIS, `pymannkendall` and `jenkspy` are small and pure
Python, and Jenks classification is in QGIS already.

Order by what a chemist asks for most, not by what is easiest:

**4.1 US-28, summary statistics that follow the selection.** n, detects,
%ND, min/median/max/mean/sd, grouped. It is a `GROUP BY` and costs
milliseconds. State the non-detect convention with every number. Decide
explicitly whether the 95% UCL is in scope — offering a naive one would be
worse than offering none, because it would be used.

**4.2 US-29, Mann-Kendall trend per location.** The strongest domain fit in
the whole of Epic G: it is what regulators expect for groundwater monitoring
and the plugin already holds the data. Sen's slope on the map, "insufficient
data" kept distinct from "no trend", a minimum point count before a trend is
claimed, and the non-detect convention stated.

**4.3 US-27, the ECDF first.** A screening level drawn as a vertical line
turns "what fraction exceeds the criterion" into a reading off an axis, which
is the question environmental work actually asks and nothing currently
answers. Grouped box plots and strip charts next.

**4.4 US-30, classification methods.** Mostly exposing what QGIS ships;
`layer_builder._graduated_ranges` hard-codes quantiles today. Manual breaks
at regulatory thresholds are the ones that matter.

**4.5 US-25 and US-26, the table and a writable filter statement.** Both are
small once the predicate model exists, and US-26 is close to free under R6.

**4.6 US-31, duplicate detection.** Would have diagnosed the Facility A
two-methods case in one query rather than by inspection.

US-32 (correlation, PCA, source apportionment) stays P3, and the honest
question there is whether it belongs in a QGIS plugin at all or whether a
good export is the right answer.

---

## Housekeeping, any time

- [x] **A report without `DETECT_FLAG` was valued as all non-detects** and
      halved under the ½ DL default (found 2026-10-08). Now refused in both
      implementations from one list, `REQUIRED_FOR_ANALYSIS`; see
      pivot-design.md, "The detect flag was assumed".
- [ ] **The report → EIA → parquet pipeline must REQUIRE `DETECT_FLAG`**,
      plus `INTERPRETED_QUALIFIERS` and `LAB_QUALIFIERS`, which the pivot's
      `_qualifier` reads. Today it carries them when a slice's stored output
      has them (`FORCE_STRING` in `nightly_slices.py` keeps them strings),
      but nothing checks they are THERE. A slice whose template report does
      not output them would be collected and published, and the plugin
      would then refuse the whole source at open (`from_parquet`), a night
      too late. Check the template in `create_slice_reports.py` before any
      report is made, and each slice's output at collect, and fail the slice
      by name. Facility A's partitions carry all three today.
- [ ] `metadata.txt` → `qgisMinimumVersion=4.2` when the substrate lands.
- [ ] The 25,000 cap becomes **two** numbers — features in the layer, and
      features visible at once (R10).
- [ ] Run `tools/test_phase0.py` on macOS. If every 4.2 build ships Python
      3.12, the dependency matrix is one bundle per platform rather than a
      grid.
- [ ] CI: build the three bundles and produce one zip per platform.
- [ ] Re-run `spike.canvas_check()` on the fixed `brush_scale.py` for the
      narrow-brush canvas figure; only the 20% case was ever measured live.

## Things deliberately not planned

- **Mosaic before DuckDB is behind the panel.** It would be a coordinator
  talking to a throwaway adapter.
- **Raising the feature cap because DuckDB is fast.** The cap is about
  drawing. It does not move.
- **A DuckDB-less fallback path**, until someone decides whether it is worth
  maintaining two data paths forever to cover a packaging bug (US-23).
