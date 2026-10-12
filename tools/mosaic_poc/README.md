# Overlay proof of concept

Stage 1 of the linked-views work in [docs/vision-linked-views.md](../../docs/vision-linked-views.md).
A translucent `QWebEngineView` sitting on the QGIS map canvas, drawing one
small live time-series chart per location, anchored to the map and sharing one
brush.

**QGIS 4.2+ only.** See [docs/webengine-support.md](../../docs/webengine-support.md).

## Run it

From the QGIS Python console:

```python
exec(open(r"C:\path\to\equis_qgis\tools\mosaic_poc\overlay.py").read())

poc = OverlayPoc(iface)              # demo data - no EQuIS, no plugin needed
# or, with the panel open and a report loaded:
poc = OverlayPoc.from_plugin(iface)  # the real series, from rows already in memory

poc.set_interactive(True)    # charts take the mouse
poc.set_interactive(False)   # map takes the mouse (the default)
poc.report()                 # what happened, formatted to paste back
poc.close()
```

`from_plugin` reads the panel's loaded `ReportTable` rather than fetching
anything: the rows are already in memory and re-running a report can take
minutes.

## What it is testing

Four questions, all specific to putting a web view *over* the canvas and all
independent of Mosaic:

1. **Compositing.** Is the map visible through the overlay, or does Chromium
   paint an opaque rectangle over it? Three things have to agree —
   `WA_TranslucentBackground` on the view, a transparent page background
   colour, and `background: transparent` in the CSS. Any one of them missing
   and the map disappears.
2. **Anchoring.** Pan, zoom and resize the window. Do the charts follow their
   locations, or drift and lag behind the map?
3. **Mouse.** With `set_interactive(False)`, can you drag the map by dragging
   across a chart? With `True`, does the same drag brush the chart instead?
   The attribute has to go on `view.focusProxy()` — Chromium's native child —
   and that child does not exist until the page has loaded.
4. **Shared brush.** Drag across one chart. Do the other charts dim their
   out-of-range points too?

`poc.report()` prints these as a checklist. Answer them by looking at the map;
they are not things the code can assert about itself.

### Results so far

Measured 2026-09-19. **All four pass**, and the real-data path works.

| | QGIS 4.2.1 / Windows |
|---|---|
| 1. Compositing | ✅ map renders through the overlay; only the cards are opaque |
| 2. Anchoring | ✅ charts follow the map through drags |
| 3. Mouse toggle | ✅ `True` takes the mouse off the map, `False` gives it back |
| 4. Shared brush | ✅ after the double-gate fix below |
| `from_plugin` | ✅ draws the panel's loaded report, no refetch |

**What that settles.** A web view can be composited over the QGIS canvas
without hiding it, kept registered with the map through pan and zoom, handed
the mouse and given it back, and driven from the plugin's own in-memory rows.
Those are the mechanics every later linked view needs, and none of them
depended on Mosaic — which is why they were worth isolating.

**Why 4 failed at first: two gates on one mouse.** Mouse access was decided in two
independent places — Qt's `WA_TransparentForMouseEvents` on the view's
`focusProxy`, and CSS `pointer-events` on `body`. They desynced in precisely
the way that produces the reported symptom: Qt's gate opened, so the map
stopped panning, while the CSS gate stayed shut, so the page ignored every
click. The overlay swallowed the mouse and did nothing with it.

The fix is one authority, not tighter synchronisation. Qt decides whether the
page sees an event at all; if the page sees one, it was meant for it. The
`interactive` class is now presentation only — cursor and HUD wording.

Two more input hazards were fixed while in there, either of which could
produce the same silence on its own: an SVG with no painted background is not
hit-testable, so a drag starting between two markers landed on nothing; and
without `touch-action: none` the browser may claim a drag as a pan gesture and
withhold `pointermove`. `setPointerCapture` now falls back to window-level
listeners if it is refused, rather than losing the drag.

### If the brush still does nothing

```python
poc.brush_test()   # sets a brush from Python, no mouse involved
poc.diagnose()     # what the page thinks its own state is
```

`brush_test` splits the remaining unknown in two: if the charts dim, drawing
works and only input is at fault; if they do not, input was never the problem.
`diagnose` reports the fields that distinguish the causes — `pointerdowns: 0`
means no click reached a chart at all, downs without moves means something
claimed the drag after it started, and `bodyPointerEvents: "none"` would mean
the DOM is still refusing the mouse.

## Why there is no Mosaic in it yet

Mosaic's value is a coordinator issuing SQL to a database. The plugin has no
DuckDB yet, so a "Mosaic PoC" today would be Mosaic over a fake connector —
which would exercise none of the thing we actually want and would fail for
reasons we could not separate from the overlay's own.

So the chart layer is hand-written SVG with no build step, and it is isolated
behind the small page contract documented at the top of `overlay.html`:

```
in   bridge.dataset(json)    [{id, name, chemical, unit, points:[{t,v,detected}]}]
in   bridge.positions(json)  [{id, x, y, visible}]
in   window.setInteractive(bool)
out  bridge.ready()          once state is built AND signals connected
out  bridge.brushed(json)    {from, to, count}, or "" when cleared
out  bridge.log(string)
```

A vgplot implementation satisfies the same contract. The order that follows
from this is: overlay (here) → DuckDB packaged into the plugin → Mosaic over
the bridge to it.

## The bundling question is already answered

Measured 2026-09-18 in this repo's toolchain, since it decides whether
stage 3 is viable:

| | |
|---|---|
| `@uwdata/vgplot` | 0.31.0 |
| Install | 89 packages, npm registry, no native build |
| Bundle | `esbuild --bundle --format=esm --minify` |
| Output | **813 KB** single ESM file, built in 148 ms |

813 KB is comfortably inside the size that can be checked into the plugin and
served from a custom scheme — which
[docs/webengine-support.md](../../docs/webengine-support.md) records as
working from inside a plugin on QGIS 4.2.1. Nothing about the JS toolchain is
blocking; it just should not land in the same change as the overlay.

## Known limits of this stage

- **A chart per location does not scale.** Only locations inside the viewport
  are drawn, capped at `MAX_CHARTS` (12). Hundreds of anchored cards would be
  unreadable long before they were slow. US-24's two-layer idea is the answer
  at real scale, and this is not a test of it.
- **The brush is on time only.** One axis, one clause. It is enough to show
  that several charts share one selection, which is the idea; it is not
  `Selection.intersect()`.
- **Nothing is written back to the map.** The brush does not yet filter or dim
  the QGIS layer. That is the next thing to add here, and it is the cheapest
  place to try the static-base-plus-small-highlight arrangement that US-24
  criterion 10 depends on.

## Tests

`series_payload` and the demo generator carry no QGIS imports, so the part
that decides what a point looks like on screen runs anywhere:

```
python tools\test_overlay_payload.py
```

It pins the rule that is easy to get wrong and impossible to see: a value
plotted at a detection limit is a non-detect whatever `DETECT_FLAG` says. The
dock plot encodes that in `timeseries._is_detected`; if the overlay disagreed,
one point would be solid in one view and hollow in the other.


---

# Phase 1.5: brush repaint cost

`brush_scale.py` answers the one number that can still invalidate US-24 after
everything else has passed: **criterion 10 asks for 250,000 points
re-highlighted within 100 ms, continuously during a drag.** The plugin caps
unaggregated layers at 25,000 today, and the README says why — "QGIS redraws
every one on every pan."

```python
exec(open(r"C:\path\to\equis_qgis\tools\mosaic_poc\brush_scale.py").read())

spike = BrushScale(iface, sizes=[25000], storages=["memory"])   # quick look
spike = BrushScale(iface)     # full: 25k/100k/250k, memory and GeoPackage
spike.run()
spike.canvas_check()
spike.close()
```

It compares three ways of answering one brush frame, at each size, for a
narrow brush (2% selected) and a wide one (20%):

| | What changes | Why it might not scale |
|---|---|---|
| **A** `subsetString` on the big layer | the layer itself narrows | the provider re-filters every row |
| **B** `selectByIds` on the big layer | selection only | QGIS repaints all 250,000 features |
| **C** two layers, rebuild the highlight | a small separate layer | **the proposal** — base never changes, so it is cacheable |

If C holds, redraw cost scales with the **selection** rather than the
dataset, which is the only thing that makes criterion 10 plausible.

Rendering is timed with `QgsMapRendererParallelJob` at a fixed output size —
the idiom `tools/test_roll.py` already uses — because the canvas renders
asynchronously, caches and coalesces refreshes, so timing it directly measures
the scheduler as much as the work. `canvas_check()` then does the end-to-end
canvas version, to test the one assumption the offscreen numbers rest on: that
an unchanged base layer is served from the canvas cache rather than redrawn.

Three things the script guards against flattering itself:

- **A refused subset string is reported, not measured.** The memory provider
  only gained `setSubsetString` support recently; an ignored filter leaves the
  layer whole, so the case would time a full-dataset render while looking like
  a narrow brush.
- **C's filtering is inside its own timer.** A and B pay for filtering within
  their update cost, and C must be measured the same way or the comparison is
  rigged. In production that step is the DuckDB query, measured at ~6 ms on
  460,000 rows in phase 0.
- **GeoPackage is tested as well as memory**, because GeoPackage is what the
  plugin actually writes.

## Results

Measured 2026-09-20, QGIS 4.2.1 / Windows, 1400x900 output, EPSG:3857.

**The two-layer arrangement works. Criterion 10's premise does not.**

### Case C render cost, narrow brush (2% selected)

| Points | Highlighted | Render, memory | Render, GeoPackage |
|---|---|---|---|
| 25,000 | 500 | 10.5 ms | 14.3 ms |
| 100,000 | 2,000 | 16.0 ms | 29.7 ms |
| 250,000 | 5,000 | **49.8 ms** | **58.5 ms** |

**Every one is inside the 100 ms budget.** Redraw scales with the
*selection*, not the dataset, which is exactly what the two-layer idiom
claimed. The alternatives do not: at 250,000 points `selectByIds` renders in
2.1 s (memory) and 3.9 s (GeoPackage), because QGIS repaints all 250,000
features; `subsetString` renders in 0.36 s because the provider re-filters
every row on every render pass.

### Where case C went over, and why it does not count

The run reported case C as OVER at 250,000 because the summary added the
*update* half — 135 ms — to the render. That update is this spike building
5,000 `QgsFeature` objects in Python at ~27 µs each, plus a Python list
comprehension over 250,000 tuples. **Production does neither:** the filter is
a DuckDB query (~6 ms on 460,000 rows, measured in phase 0) and the result
goes to QGIS in bulk rather than one feature at a time. The summary now
reports render and update separately, because summing them buried the finding.

### The wall nobody budgeted for

| Points | Base layer draw, memory | Base layer draw, GeoPackage |
|---|---|---|
| 25,000 | 63 ms | 392 ms |
| 100,000 | 190 ms | 1,695 ms |
| 250,000 | 2,070 ms | **3,826 ms** |

GeoPackage is **linear at ~15.5 µs per point**, and this cost is paid on every
**pan and zoom** — not per brush frame, and not avoidable by any arrangement
tested here, because the base layer genuinely changes when the extent does.

That gives a pan budget:

| Tolerable pan | Points |
|---|---|
| 100 ms | ~6,500 |
| 250 ms | ~16,000 |
| 500 ms | ~32,000 |
| 1,000 ms | ~65,000 |

**The plugin's existing 25,000 cap lands almost exactly on a 400 ms pan.**
That number was set empirically, long before this spike, and it turns out to
sit right at the edge of tolerable — independent confirmation that the cap is
in the right place and was not arbitrary.

### Wide brushes are a different problem

At 20% selected — 50,000 highlighted features — case C costs 397 ms to render
at 250,000 points. Nothing rescues that, and nothing should: 50,000 solid
markers is not a picture anyone reads. This is a constraint on the
*interaction*, not a performance bug, and criterion 10 should say so.

### Two things this run did not settle

### Canvas caching is real — confirmed

Run 2026-09-20 on the live canvas, memory, 250,000 points:

| | |
|---|---|
| base + highlight dirty | 764.5 ms |
| highlight dirty only | 167.0 ms |
| **saved by the cache** | **597.5 ms — 78% of the frame** |

**The assumption every offscreen number rests on holds.** When only the
highlight is dirtied, QGIS serves the unchanged base from its layer cache
instead of redrawing it. The two-layer split is load-bearing on a live
canvas, not just in a renderer job.

That run also printed *"even a highlight-only repaint misses the budget"*.
**Ignore that line** — it came from the pre-fix version, and it was measuring
**50,000 highlighted features, not 5,000**: `run()` ends on the 20% brush and
the old `canvas_check()` inherited the layer as it was left. The comparable
offscreen figure is the 20% row (450.7 ms), not the 2% one (49.8 ms). The
narrow-brush canvas number is still unmeasured; both defects are fixed, so a
re-run answers it.

Note also that canvas figures are not directly comparable with the offscreen
ones above — the canvas viewport is whatever size the QGIS window is, where
the offscreen job is fixed at 1400x900. Within a single canvas run the
comparison is sound, which is all the caching question needs.

  Two defects in that method were found and fixed before it was first run,
  either of which would have produced a confidently wrong answer. Both
  storages named their layers `base_<size>` / `hl_<size>`, so the lookup
  returned whichever came first and **silently measured memory** while
  appearing to reflect the whole run. And `run()` leaves the highlight
  holding the *last* brush it applied — 20%, the wide case that is out of
  scope by design — so `canvas_check()` inherited ~50,000 features and would
  have reported the worst case as the headline. Layers are now named per
  storage, and the highlight is refilled to the chosen fraction first, with
  the count printed so the number is interpretable.
- **GeoPackage reported "provider refused a subset string" at 100k and 250k
  but not at 25k.** That is suspicious: OGR supports subset strings. The
  guard compares `featureCount()` before and after, and OGR may return an
  estimate or not refresh it for larger layers, so this is more likely my
  guard misfiring than a real refusal. Unresolved, and it does not affect the
  case-C conclusion.


---

# Temporal filtering (`temporal_scale.py`)

Measured 2026-09-20, QGIS 4.2.1 / Windows, GeoPackage, 1400x900.

| Layer | Window | Visible | Unindexed | Indexed | Drawing floor |
|---|---|---|---|---|---|
| 50,000 | 2% | 1,001 | 48.3 ms | **29.3 ms** | 11.6 ms |
| 50,000 | 10% | 5,114 | 118.2 ms | 121.3 ms | 27.5 ms |
| 250,000 | 2% | 5,079 | 169.7 ms | **145.2 ms** | 32.8 ms |
| 250,000 | 10% | 25,553 | 481.1 ms | 739.7 ms | 125.2 ms |

Unfiltered, for comparison: 237.8 ms at 50,000 and 1,125 ms at 250,000.
Animation at 250,000 with a 2% window: median 159 ms, **6.3 fps**.

Fitted from those four points:

```
ms  =  0.0005 x layer_rows  +  0.0153 x visible_marks
```

Full interpretation and the resulting size envelope are in **R10** of
[docs/vision-linked-views.md](../../docs/vision-linked-views.md). The short
version: filtering is ~32x cheaper per row than drawing is per mark, so a
temporal window pays — but the scan runs over the whole layer every frame, so
the design is comfortable to ~100,000–150,000 results and not beyond.

The first version of this script's summary **averaged the index's effect
across selectivities and reported "no change"**, hiding both the cost model
and a textbook index result (−39% on a narrow window, +54% on a wide one).
That is the third time in this work that aggregating a result destroyed it;
the summary now decomposes rather than averages.
