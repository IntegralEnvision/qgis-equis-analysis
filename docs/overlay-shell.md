# The overlay shell: the plugin as panels on the map

Branch `overlay-shell`, started 2026-10-20. Asked for in these words: "what
if the data selection tab was part of the html overlay too? As in no plugin
chrome at all - you launch the plugin from its button which triggers the
appearance of the overlay? Each panel element would be magnetically
attracted to any edge of the map window. They could be minimized against
the edge or expanded."

This note is the plan, the rules the plan must not break, and where it has
got to. It is written so that a decision to merge, or to abandon, can be
made from it.

## The two risks, and how the plan meets them

**A QGIS without a working web view would have no plugin at all.** Today
only the Analysis tab needs QtWebEngine. Measured: Windows 4.2.1 works
([webengine-support.md](webengine-support.md)); macOS and Linux on 4.2 are
not measured, and 4.0 on Windows shipped broken (qgis/QGIS#65398). So the
dock stays, behind a setting (`equis_loader/ui`: `overlay` or `panel`),
until the probe has run on every platform the plugin is used on. **Asked of
a Mac user 2026-10-20; not yet answered.**

**The Data tab's rules live in its widgets.** `dialog.py` is 6,700 lines;
`panel_state.py` is 310. The cached/offline marking, read-only mode, the
five writes to the tenant and the replace-data confirmation are all in the
dialog. An HTML Data panel that re-implemented any of them would be the
"two authorities" CLAUDE.md warns against. So:

- **Python stays the only authority.** The page renders state the host
  sends and sends back intents ("open this parquet", "map this analyte").
  It decides nothing a rule depends on.
- **The rules move out of the widgets, not into JavaScript.** Step 2 pulls
  each one the Data panel needs into a controller the page and the dock
  both call - which also makes them testable without a GUI, like
  `report_data.py`.
- **A parity gate before the dock goes**, as for `ReportTable`: the same
  intents must leave both front ends in the same `panel_state`.

## Steps

1. **The shell** - done, below.
2. **The Data panel, parquet path**: URL, open, matrix, fraction, analytes,
   filters, build the map. The smaller path, and no writes to the tenant.
3. **The Data panel, live EQuIS path**: site, key, read-only, report,
   facility, fetch, cache and offline - with the banner's rule carried over
   unchanged.
4. **The rest**: the time-series plot (a matplotlib dock today), export.

## Step 1: what exists

- The toolbar button brings up the overlay; the dock stays hidden. Pressed
  again it hides and shows the panels (`dialog.toggle_overlay`).
- Five panels (`web/src/shell.js`): **Data**, **Choose**, **Chart**,
  **Histograms**, **Box plot**. Dragged by the header, a panel snaps to the
  nearest edge where it was let go; panels never overlap while there is
  room, on one edge or across a corner; minimised (– or double-click) it is
  a tab on its edge, upright on top and bottom, turned on the sides; a click
  on the tab expands it. The layout is kept in QSettings
  (`equis_loader/overlay_layout`) through the bridge.
- **QGIS's message bar is kept clear of.** It lies over the top of the
  canvas; the host reports where (`MapOverlay.keep_out`) and the page moves
  that edge in. Found when a drag on a header went to the bar instead.
- **The Data panel** shows what is loaded and repeats the dock's banner -
  cached, offline, cut short - word for word, from `_update_active_label`,
  which stays the one authority (CLAUDE.md: cached data is marked
  everywhere it appears). Its buttons ask the host: **Change data…** shows
  the dock's Data tab, **Into the panel** puts everything back in the
  dock's Analysis tab, **Hide** hides the overlay.
- With data loaded while over the map, the chooser is offered at once -
  there is no tab click to wait for.

Seen working 2026-10-20 in QGIS 4.2.1 on Windows 11, Facility A, by
`tools/try_overlay.py` (13 checks, all passing): the button, the hidden
dock, the Data panel naming the facility, the mouse gate three ways, a
brush, a panel dragged to another edge, a minimised tab, the saved layout,
a pan on open map, both Data panel buttons. Screenshots of that run were of
a locked screen; the layout was seen in the browser preview instead.

## Open questions

- Whether the wheel over a panel should zoom the map (today it scrolls the
  panel).
- Panel sizes are fixed per panel; resizing by an edge is not built.
- On a small canvas the default layout overlaps; `place` lets panels
  overlap rather than leave the canvas, which is the right failure but not
  a good layout.
- Two QGIS map views, or the 3D view: the overlay knows one canvas.
- A floating plugin panel can be left mostly off-screen (seen at y = 878,
  721 px tall, on a 960 px screen). "Change data…" should bring a floating
  panel back on-screen when it shows it.

## Known problem: docks drawn in the main window's corner

**Seen twice, on the user's own QGIS 4.2.1 (Windows 11, 125% scaling,
1536×960), with the overlay on; not reproduced.** Recorded 2026-10-27, to be
re-checked after updating to the newest QGIS beta.

**What it looks like.** A dock shown after some point in the session -
Identify Results the first time, the plugin's own panel and the Python
Console later - is drawn at the main window's top-left corner over the menus
and toolbars, with no background (what's behind shows through) and no way
to close it.

**What is known** (from `tools/diagnose_windows.py`, run with the problem on
screen):

- **The main window's dock layout stopped placing docks.** Qt reported the
  Python Console as docked (bottom area, not floating) at `(0,0)` with a
  default size of 100×203: it was never laid out. Not a painting fault.
- **The overlay itself was where it should be:** the web view a child of the
  map canvas, filling it.
- **Once broken, it stays broken.** Un-maximising and re-maximising QGIS
  doesn't fix it; nor does turning the overlay off ("⇲ Into the panel").
  Nothing appears in the Log Messages panel, so no Python exception was
  seen.
- The plugin's own panel was floating at the time.

**What was ruled out** in a scratch profile, each run in a fresh QGIS 4.2.1,
recording every dock's geometry (`docks1`/`docks2` runs, 2026-10-27): overlay
with the panel docked; overlay with the panel floating; the classic panel;
and both overlay variants again with the user's three other enabled plugins
(`basemaps`, `db_manager_community`, `slyr`). In all five, the Python Console
and Identify Results docked normally. So the trigger is something earlier in
a real session, not the setup.

**Leading guess, untested:** a dock drag (docking or undocking a panel)
interrupted over the overlay, leaving Qt's dock layout frozen mid-drag so
that every dock shown afterwards lands at `(0,0)`. Floating-panel drags on
Windows are native window moves, which synthetic input can't reproduce.

**Next time:** note what was done just before it broke, especially any panel
drag with the overlay on; and re-test on the newer QGIS before investing
further, since a Qt fix may already cover it.
