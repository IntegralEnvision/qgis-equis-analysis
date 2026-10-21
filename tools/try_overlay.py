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

from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer
from qgis.PyQt.QtCore import QEvent, QPoint, QPointF, QSettings, Qt, QTimer
from qgis.PyQt.QtGui import QGuiApplication, QMouseEvent
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
    after(1200, data_button)


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
    say("SUMMARY %s (%d of %d)" % (
        "ALL PASS" if CHECKS and all(ok for _, ok in CHECKS) else "FAILED",
        sum(ok for _, ok in CHECKS), len(CHECKS)))
    say("done; shots and this log are in %s" % OUT)
    LOG.close()
    QTimer.singleShot(500, lambda: os._exit(0))


say("loaded; data %s" % DATA)
QTimer.singleShot(3000, guard(start))
