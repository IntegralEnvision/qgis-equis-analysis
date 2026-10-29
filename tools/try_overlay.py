r"""The overlay shell, driven end to end in a real QGIS.

Not a console probe: QGIS runs this at startup, in a THROWAWAY profile,
and it QUITS QGIS when it is done (hard - a save prompt must not hang an
unattended run). Everything goes through the plugin as a user would reach
it, with Qt's synthetic input - the real mouse is never touched:

  1. the toolbar button brings up the overlay, with the dock hidden and the
     Data panel saying nothing is loaded;
  2. a parquet facility loads (through the panel's own code, dock hidden)
     and the Data panel names it; the chooser is offered with no tab click;
  3. hovering a panel gives the page the mouse, open map gives it to the
     map, Ctrl over a panel gives it to the map;
  4. a drag on the Chart panel brushes ("N of M samples selected");
  5. a panel dragged by its header snaps to another edge; one minimised
     becomes a tab; the layout is kept in QSettings;
  6. a drag on open map, with the Pan tool, moves the extent;
  6b. a QGIS selection - on Locations, then on the scatter's map - with the
     dock still hidden dims the charts to those stations IN PLACE (same
     chart, no spinner), is drawn in QGIS's selection colour on the map,
     and clearing it makes every sample solid again;
  7. "Change data…" brings up the dock; "Into the panel" puts the view
     back in the Analysis tab.

Each step is screenshotted. This exists because the two tests that would
cover the web view skip outside QGIS's own application object
(docs/phase3-next-steps.md, "The standing risk"); this one IS that
application.

    $env:OVERLAY_DATA = "C:\path\to\data\facility_a"     # default: data\facility_a
    $env:OVERLAY_OUT  = "C:\somewhere\shots"             # default: a temp folder
    $env:OVERLAY_PAIR = "SO,Arsenic,Chromium"            # matrix, X, Y by name
    python tools\install.py --dest <root>\profiles\try\python\plugins\equis_loader
    & "C:\Program Files\QGIS 4.2.1\bin\qgis.bat" --profiles-path <root> `
        --profile try --nologo --noversioncheck --code tools\try_overlay.py

Then read <OUT>\log.txt; its last lines are the SUMMARY.
"""
import json
import os
import tempfile
import time
import traceback

from qgis.core import (QgsMapRendererCustomPainterJob, QgsMapSettings,
                       QgsProject, QgsRasterLayer, QgsVectorLayer)
from qgis.PyQt.QtCore import QEvent, QPoint, QPointF, QSettings, Qt, QTimer
from qgis.PyQt.QtGui import QGuiApplication, QImage, QMouseEvent, QPainter
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QApplication
from qgis.utils import iface, loadPlugin, plugins, startPlugin

# --code runs this with no __file__ (found 2026-10-20: the NameError killed
# the run before its log existed), so the default data path is relative to
# the working directory - the repository root, as for every other tool.
DATA = os.environ.get("OVERLAY_DATA") or os.path.abspath(os.path.join("data", "facility_a"))
OUT = os.environ.get("OVERLAY_OUT") or tempfile.mkdtemp(prefix="try_overlay_")
os.makedirs(OUT, exist_ok=True)
MATRIX, X_NAME, Y_NAME = (os.environ.get("OVERLAY_PAIR")
                          or "SO,Arsenic,Chromium").split(",")
LOG = open(os.path.join(OUT, "log.txt"), "w", encoding="utf-8")
T0 = time.monotonic()
STATE = {}
CHECKS = []


def say(message):
    LOG.write("%7.1f  %s\n" % (time.monotonic() - T0, message))
    LOG.flush()


def check(label, ok, detail=""):
    CHECKS.append((label, bool(ok)))
    say("[%s] %s%s" % ("PASS" if ok else "FAIL", label, (" - %s" % detail) if detail else ""))


def shot(name):
    """From the screen, not QWidget.grab: what Chromium composited."""
    win = iface.mainWindow()
    QApplication.processEvents()
    screen = win.screen() or QGuiApplication.primaryScreen()
    g = win.frameGeometry()
    screen.grabWindow(0, g.x(), g.y(), g.width(), g.height()).save(
        os.path.join(OUT, name))
    say("shot %s" % name)


def page(js, then):
    STATE["panel"]._linked.views.view.page().runJavaScript(js, then)


def ask_page(js, key):
    """Ask the page something; the answer lands in STATE[key] (JSON)."""
    def got(result):
        try:
            STATE[key] = json.loads(result) if result else None
        except (TypeError, ValueError):
            STATE[key] = result
    page("JSON.stringify(%s)" % js, got)


def wait(cond, then, label, limit=120.0, every=250):
    began = time.monotonic()

    def poll():
        try:
            ok = cond()
        except Exception as exc:  # noqa: BLE001 - keep polling, say why at the end
            ok = False
            STATE["last_error"] = repr(exc)
        if ok:
            say("ready: %s (%.1f s)" % (label, time.monotonic() - began))
            QTimer.singleShot(300, guard(then))
        elif time.monotonic() - began > limit:
            say("TIMEOUT: %s (%s)" % (label, STATE.get("last_error")))
            STATE["timed_out"] = label
            shot("timeout.png")
            summary()
        else:
            QTimer.singleShot(every, poll)
    poll()


def after(ms, fn):
    QTimer.singleShot(ms, guard(fn))


def guard(fn):
    def run():
        try:
            fn()
        except Exception:  # noqa: BLE001
            say("ERROR\n" + traceback.format_exc())
            summary()
    return run


def to_window(viewport_point):
    win = iface.mainWindow()
    g = iface.mapCanvas().viewport().mapToGlobal(viewport_point)
    return win.windowHandle(), win.mapFromGlobal(g)


def rect_of(panel_id, then):
    """A panel's rectangle in viewport pixels (the page fills the viewport)."""
    ask_page("(() => { const r = document.getElementById(%s).getBoundingClientRect();"
             " return {x: r.left, y: r.top, w: r.width, h: r.height}; })()"
             % json.dumps(panel_id), "rect")
    after(300, lambda: then(STATE["rect"]))


def drag(start, end, steps=12, modifier=Qt.KeyboardModifier.NoModifier):
    window, a = to_window(start)
    _, b = to_window(end)
    QTest.mouseMove(window, a)
    QApplication.processEvents()
    QTest.mousePress(window, Qt.MouseButton.LeftButton, modifier, a)
    for i in range(1, steps + 1):
        QTest.mouseMove(window, QPoint(a.x() + (b.x() - a.x()) * i // steps,
                                       a.y() + (b.y() - a.y()) * i // steps), 15)
        QApplication.processEvents()
    QTest.mouseRelease(window, Qt.MouseButton.LeftButton, modifier, b)
    QApplication.processEvents()


# ------------------------------------------------------------------ steps

def start():
    win = iface.mainWindow()
    win.showNormal()
    win.setGeometry(40, 40, 1500, 900)
    win.raise_()
    win.activateWindow()
    QSettings().remove("equis_loader/overlay_layout")     # start from the default
    if "equis_loader" not in plugins:
        loadPlugin("equis_loader")
        startPlugin("equis_loader")
    QgsProject.instance().addMapLayer(QgsRasterLayer(
        "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19&zmin=0",
        "OpenStreetMap", "wms"))
    plugin = plugins["equis_loader"]
    plugin.run()                                   # the toolbar button
    panel = plugin.dialog
    STATE["panel"] = panel
    check("the button brings up the overlay", panel._linked is not None
          and panel._linked.overlay is not None)
    check("the dock stays out of the way", not panel.isVisible())
    wait(lambda: panel._linked.views.ready_seen, launched, "page ready")


def launched():
    after(800, lambda: (shot("0_launch.png"), load()))


def load():
    panel = STATE["panel"]
    panel.source_combo.setCurrentIndex(1)          # published parquet
    panel.parquet_edit.setText(DATA)
    panel._open_parquet_source()
    wait(lambda: panel._report_store is not None, loaded, "store open")


def loaded():
    layers = [lyr for lyr in QgsProject.instance().mapLayers().values()
              if isinstance(lyr, QgsVectorLayer)]
    if layers:
        iface.setActiveLayer(layers[0])
        iface.zoomToActiveLayer()
    linked = STATE["panel"]._linked
    # Offered with no tab click: the page's matrix list arrives by itself.
    wait(lambda: linked.session is not None and linked._task is None
         and linked.session.matrices(), offered, "chooser offered", limit=60)


def offered():
    ask_page("document.querySelector('#data-body .data-what').textContent", "data_what")
    idle = {"n": 0}
    linked = STATE["panel"]._linked

    def quiet():
        idle["n"] = idle["n"] + 1 if linked._task is None else 0
        return idle["n"] >= 12
    wait(quiet, choose, "controller quiet", limit=60)


def choose():
    check("the Data panel names what loaded", "Nothing loaded" not in (STATE.get("data_what") or "Nothing loaded"),
          STATE.get("data_what"))
    linked = STATE["panel"]._linked
    # By NAME: a prefix depends on the data (arsenic, or arsenic_t where
    # fractions split it), and a prefix the catalogue lacks is dropped.
    catalogue = linked.session.catalogue(MATRIX)
    prefix = {v["chemical"].lower(): v["prefix"] for v in catalogue["variables"]}
    choice = {"matrix": MATRIX, "x": prefix.get(X_NAME.lower()),
              "y": prefix.get(Y_NAME.lower()), "chart": "scatter", "mode": "all"}
    say("choosing %s" % choice)
    page("window.equis.bridge.chosen(%s); 'sent'" % json.dumps(json.dumps(choice)),
         lambda r: None)
    drawn_then(lambda: after(1500, lambda: (shot("1_over_map.png"), gate())))


def drawn_then(then):
    STATE["drawn"] = False

    def ask():
        page("JSON.stringify(!!(window.equis && window.equis.linked && "
             "window.equis.linked.box && window.equis.linked.box.solid))",
             lambda r: STATE.__setitem__("drawn", r == "true"))
        return STATE["drawn"]
    wait(ask, then, "chart and linked views drawn", limit=90)


def gate():
    rect_of("p-chart", gate_with)


def gate_with(r):
    ov = STATE["panel"]._linked.overlay
    vp = iface.mapCanvas().viewport().rect()
    say("viewport %dx%d, %d cards" % (vp.width(), vp.height(), len(ov.cards)))
    chart = QPoint(int(r["x"] + r["w"] / 2), int(r["y"] + r["h"] / 2))
    STATE["chart"] = r
    empty = QPoint(vp.width() // 2, vp.height() // 2)
    while ov.over_card(empty) and empty.y() < vp.height() - 40:
        empty += QPoint(0, 40)
    STATE["empty"] = empty
    window, p = to_window(chart)
    QTest.mouseMove(window, p)
    QApplication.processEvents()
    on_card = ov.open
    window, q = to_window(empty)
    QTest.mouseMove(window, q)
    QApplication.processEvents()
    on_map = ov.open
    QTest.mouseMove(window, p)
    QApplication.processEvents()
    # QTest cannot hold a key across a move, so: a move carrying Ctrl, sent
    # to whichever widget has the mouse there now.
    ev = QMouseEvent(QEvent.Type.MouseMove, QPointF(p), QPointF(p),
                     QPointF(window.mapToGlobal(p)), Qt.MouseButton.NoButton,
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier)
    QApplication.sendEvent(ov.view.focusProxy() if ov.open
                           else iface.mapCanvas().viewport(), ev)
    QApplication.processEvents()
    check("over a panel, the page has the mouse", on_card is True)
    check("over open map, the map has it", on_map is False)
    check("Ctrl over a panel, the map has it", ov.open is False)
    after(300, brush)


def brush():
    r = STATE["chart"]
    drag(QPoint(int(r["x"] + r["w"] * 0.40), int(r["y"] + r["h"] * 0.20)),
         QPoint(int(r["x"] + r["w"] * 0.85), int(r["y"] + r["h"] * 0.60)))
    after(2500, lambda: (ask_page("document.querySelector('.linked-line').textContent", "brush"),
                         after(500, arrange)))


def arrange():
    check("a drag on the Chart panel brushes", "selected" in (STATE.get("brush") or ""),
          STATE.get("brush"))
    rect_of("p-hists", arrange_with)


def arrange_with(r):
    vp = iface.mapCanvas().viewport().rect()
    # Count what the header receives, so a failed drag says whether the
    # press reached the page at all.
    page("window.__head = {down: 0, move: 0, up: 0};"
         "const h = document.querySelector('#p-hists .panel-head');"
         "h.addEventListener('pointerdown', () => window.__head.down++);"
         "h.addEventListener('pointermove', () => window.__head.move++);"
         "h.addEventListener('pointerup', () => window.__head.up++); 'ok'", lambda r_: None)
    ask_page("getComputedStyle(document.body).getPropertyValue('--inset-v')", "inset")
    head = QPoint(int(r["x"] + 60), int(r["y"] + 8))
    say("Histograms header at %s; keep-out %s" % (
        (head.x(), head.y()), STATE["panel"]._linked.overlay.keep_out()))
    # Down to the bottom edge, a third of the way across.
    drag(head, QPoint(vp.width() // 3, vp.height() - 60), steps=16)
    page("document.querySelector('#p-box .panel-min').click(); 'ok'", lambda r_: None)
    ask_page("window.__head", "head")
    after(1200, lambda: (ask_page("window.equis.shell.layout", "layout"), after(400, arranged)))


def arranged():
    layout = STATE.get("layout") or {}
    say("header events %s; page inset %s" % (STATE.get("head"), STATE.get("inset")))
    saved = QSettings().value("equis_loader/overlay_layout", "")
    check("a panel dragged to another edge snaps there",
          (layout.get("p-hists") or {}).get("edge") == "bottom", layout.get("p-hists"))
    check("a minimised panel becomes a tab", (layout.get("p-box") or {}).get("minimized") is True)
    check("the layout is kept in QSettings", '"p-hists"' in str(saved))
    shot("2_arranged.png")
    after(300, pan)


def pan():
    iface.actionPan().trigger()
    canvas = iface.mapCanvas()
    before = canvas.extent().center()
    drag(STATE["empty"], STATE["empty"] + QPoint(-120, 60))
    after_ = canvas.extent().center()
    moved = abs(after_.x() - before.x()) + abs(after_.y() - before.y())
    check("a drag on open map pans", moved > 0, "%.1f map units" % moved)
    after(1200, select_on_map)


def our_layers():
    """The plugin's layers on the canvas, in drawing order (top first)."""
    return [l for l in iface.mapCanvas().layers()
            if l.name().startswith(STATE["panel"]._locations_layer.name().split(" - ")[0])]


def selection_pixels(layers=None):
    """Pixels in QGIS's selection colour, rendering the plugin's layers alone.

    Offscreen, through a render job on the canvas's settings, so a locked
    screen does not matter, and without the basemap, whose colours are not
    ours to count. "The selection is yellow" is then a number.
    """
    canvas = iface.mapCanvas()
    settings = QgsMapSettings(canvas.mapSettings())
    settings.setLayers(our_layers() if layers is None else layers)
    size = settings.outputSize()
    image = QImage(size, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    job = QgsMapRendererCustomPainterJob(settings, painter)
    job.start()
    job.waitForFinished()
    painter.end()
    want = settings.selectionColor()
    hits = 0
    for y in range(0, size.height(), 2):
        for x in range(0, size.width(), 2):
            c = image.pixelColor(x, y)
            if (c.alpha() > 0 and abs(c.red() - want.red()) < 24
                    and abs(c.green() - want.green()) < 24
                    and abs(c.blue() - want.blue()) < 24):
                hits += 1
    return hits


def layer_order():
    root = QgsProject.instance().layerTreeRoot()
    return [n.name() for n in root.findLayers()]


def settled_then(act, then, codes, label):
    """Wait until a linked view re-queries, then say what the page holds.

    The box plot is the witness that a view RE-QUERIED (its rows are a new
    array); the counts say what to: `clause` is the linked table's solid
    samples under the page's own filter (linked.js's `fg`), `codes` the
    brush's samples (it is KEPT now, and composes) at the codes asked
    for. Equal means the selection reached the views as it was meant. The chart object, its table and the absence
    of a spinner say it was done in place.
    """
    where = ("upper(coalesce(location, '')) IN (%s)" % ", ".join(
        "'%s'" % c.upper().replace("'", "''") for c in codes)) if codes else "TRUE"
    page("window.__box = window.equis.linked.box.solid; 'ok'",
         lambda r: (STATE.__setitem__("t_select", time.monotonic()), act(),
                    wait(requeried, measure, label, limit=15, every=100)))

    def measure():
        page("window.__sel = null; (async () => { const l = window.equis.linked;"
             " const f = (l.fg.predicate(null) || []).map(String).filter(x => x && x !== 'TRUE');"
             " const fw = f.length ? f.map(x => `(${x})`).join(' AND ') : 'TRUE';"
             " const q = (w) => l.coordinator.query(`SELECT count(*) AS s,"
             " count(DISTINCT upper(location)) AS at FROM ${l.table} WHERE ${w}`,"
             " {type: 'json', cache: false});"
             " const brush = f.filter(x => !x.includes('upper(coalesce(location'));"
             " const bw = brush.length ? brush.map(x => `(${x})`).join(' AND ') : 'TRUE';"
             " const [a] = await q(fw); const [b] = await q(`${bw} AND solid AND %s`);"
             " window.__sel = {clause: Number(a.s), at: Number(a.at), codes: Number(b.s),"
             " filter: fw.slice(0, 160)}; })(); 'ok'" % where, lambda r: None)

        def ask2():
            ask_page("window.__sel && Object.assign({}, window.__sel, {"
                     " table: window.equis.linked.table,"
                     " same: window.equis.chart === window.__chart,"
                     " busy: !!document.querySelector('#views .busy')})", "sel")
            return bool(STATE.get("sel"))
        wait(ask2, lambda: then(STATE["sel"]), label + " (counted)", limit=10, every=100)

    def requeried():
        ask_page("window.equis.linked.box.solid !== window.__box", "requeried")
        return STATE.get("requeried") is True
    STATE["sel"] = None
    STATE["requeried"] = None


def select_on_map():
    """QGIS's own selection, with the dock HIDDEN, reaches the charts.

    The case that did nothing until 2026-10-29: the toolbar button hides
    the dock, and the panel drew only into a visible Analysis tab. Then it
    redrew the whole page on every selection (the charts flickered off and
    back); now the selection is a clause on the views' crossfilter and they
    update in place. And the selection must still be DRAWN as QGIS draws
    one - yellow - on the map.
    """
    panel = STATE["panel"]
    stations = sorted({str(f["code"]) for f in panel._scatter_layer.getFeatures()})
    STATE["picked"] = stations[:5]
    STATE["table"] = None
    say("layer order, top first: %s" % layer_order())
    say("drawn by the canvas, top first: %s" % [l.name() for l in our_layers()])
    STATE["yellow_none"] = selection_pixels()
    # Marked, so "the same chart" is a fact and not an inference.
    page("window.__chart = window.equis.chart; 'ok'", lambda r: None)
    page("JSON.stringify(window.equis.linked.table)",
         lambda r: STATE.__setitem__("table", json.loads(r)))
    wanted = {c.lower() for c in STATE["picked"]}
    loc = panel._locations_layer
    ids = [f.id() for f in loc.getFeatures() if str(f["code"] or "").lower() in wanted]
    say("selecting %d Locations features for %s" % (len(ids), STATE["picked"]))

    after(500, lambda: settled_then(lambda: loc.selectByIds(ids), selected_on_locations,
                                    STATE["picked"], "views answer the Locations selection"))


def selected_on_locations(s):
    took = time.monotonic() - STATE["t_select"]
    check("a Locations selection, dock hidden, reaches the charts",
          0 < s["clause"] == s["codes"] and s["at"] <= len(STATE["picked"]),
          "%(clause)d samples solid under the page's filter, %(codes)d by code, "
          "at %(at)d stations" % s
          + "; %.2f s from selection to re-queried and counted (100 ms polls)" % took)
    say("page filter: %s" % s.get("filter"))
    check("...in place: same chart, same table, no spinner",
          s["same"] and s["table"] == STATE["table"] and not s["busy"],
          "chart kept %s, table %s -> %s, spinner %s"
          % (s["same"], STATE["table"], s["table"], s["busy"]))
    panel = STATE["panel"]
    yellow = selection_pixels()
    alone = selection_pixels([panel._locations_layer])
    say("Locations: %d selected; selection-colour pixels %d with our layers, %d with "
        "Locations alone (none selected: %d)"
        % (panel._locations_layer.selectedFeatureCount(), yellow, alone, STATE["yellow_none"]))
    mirrored = panel._scatter_layer.selectedFeatureCount()
    check("a Locations selection selects the same stations on the scatter map",
          mirrored == len(STATE["picked"]),
          "%d of %d stations selected there" % (mirrored, len(STATE["picked"])))
    check("the Locations selection is drawn in the selection colour",
          yellow > STATE["yellow_none"],
          "%d pixels, %d before" % (yellow, STATE["yellow_none"]))
    shot("2b_map_selection.png")
    STATE["mirror_cleared"] = lambda: panel._scatter_layer.selectedFeatureCount() == 0
    settled_then(panel._locations_layer.removeSelection, cleared_locations, None,
                 "views answer no selection")


def cleared_locations(s):
    check("clearing the selection makes every sample solid again, in place",
          s["clause"] == s["codes"] and s["same"] and not s["busy"]
          and STATE["mirror_cleared"](),
          "%(clause)d of %(codes)d (the brush's), scatter map cleared too" % s)
    layer = STATE["panel"]._scatter_layer
    two = STATE["picked"][:2]
    wanted = set(c.lower() for c in two)
    ids = [f.id() for f in layer.getFeatures() if str(f["code"] or "").lower() in wanted]
    settled_then(lambda: layer.selectByIds(ids), selected_on_scatter_map, two,
                 "views answer the scatter map's selection")


def selected_on_scatter_map(s):
    check("a selection on the scatter's map dims the charts too",
          0 < s["clause"] == s["codes"] and s["at"] <= 2 and s["same"],
          "%(clause)d samples solid, at %(at)d stations" % s)
    layer = STATE["panel"]._scatter_layer
    yellow = selection_pixels()
    alone = selection_pixels([layer])
    say("scatter map: %d selected; selection-colour pixels %d with our layers, %d alone "
        "(none selected: %d); renderer %s"
        % (layer.selectedFeatureCount(), yellow, alone, STATE["yellow_none"],
           type(layer.renderer()).__name__))
    check("the scatter map's selection is drawn in the selection colour",
          yellow > STATE["yellow_none"], "%d pixels" % yellow)
    settled_then(layer.removeSelection, cleared_scatter_map, None, "views answer clearing")


def cleared_scatter_map(s):
    check("clearing it there clears the charts and Locations too",
          s["clause"] == s["codes"] and s["same"]
          and not STATE["panel"]._locations_layer.selectedFeatureCount(),
          "%(clause)d of %(codes)d (the brush's)" % s)
    after(500, data_button)


def data_button():
    page("Array.from(document.querySelectorAll('#data-body button'))"
         ".find(b => b.textContent.includes('data')).click(); 'ok'", lambda r: None)
    after(1200, lambda: (check("Change data… brings up the dock", STATE["panel"].isVisible()),
                         shot("3_data_dock.png"), into_panel()))


def into_panel():
    page("Array.from(document.querySelectorAll('#data-body button'))"
         ".find(b => b.textContent.includes('panel')).click(); 'ok'", lambda r: None)
    panel = STATE["panel"]

    def home():
        return (panel._linked.overlay is None
                and panel.analysis_page.layout().indexOf(panel._linked.views.view) >= 0)
    wait(home, lambda: drawn_then(lambda: after(1500, lambda: (
        check("Into the panel puts the view back in the tab", True),
        shot("4_into_panel.png"), summary()))), "back in the tab", limit=20)


def summary():
    # A check that never ran is not a check that passed: a timeout stops
    # the run, and the checks after it are said not to have run.
    verdict = ("FAILED" if not all(ok for _, ok in CHECKS)
               else "INCOMPLETE" if STATE.get("timed_out") or not CHECKS
               else "ALL PASS")
    say("SUMMARY %s (%d of %d passed%s)" % (
        verdict, sum(ok for _, ok in CHECKS), len(CHECKS),
        "; stopped at %r, later checks did not run" % STATE["timed_out"]
        if STATE.get("timed_out") else ""))
    say("done; shots and this log are in %s" % OUT)
    LOG.close()
    QTimer.singleShot(500, lambda: os._exit(0))


say("loaded; data %s" % DATA)
QTimer.singleShot(3000, guard(start))
