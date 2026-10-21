r"""The Analysis view over the map, driven end to end in a real QGIS.

Not a console probe: QGIS runs this at startup, in a THROWAWAY profile,
and it QUITS QGIS when it is done (hard - a save prompt must not hang an
unattended run). It opens the panel, loads a parquet facility, adds an
OpenStreetMap basemap, draws a scatter with its linked views, lays them
over the map, and then checks the mouse gate (map_overlay.py) with Qt's
synthetic input - so the real mouse is never touched:

  1. hovering a card gives the page the mouse;
  2. hovering open map gives it to the map;
  3. Ctrl over a card gives it to the map;
  4. a drag on the scatter card brushes ("N of M samples selected");
  5. a drag on open map, with the Pan tool, moves the extent;
  6. the view goes back into the tab and redraws.

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

Then read <OUT>\log.txt; its last lines are the SUMMARY. First run
2026-10-20, QGIS 4.2.1 on Windows 11, Facility A, SO As x Cr: all six held;
the drag selected 93 of 112 samples at 10 locations.
"""
import json
import os
import tempfile
import time
import traceback

from qgis.core import QgsApplication, QgsProject, QgsRasterLayer, QgsVectorLayer
from qgis.PyQt.QtCore import QEvent, QPoint, QPointF, Qt, QTimer
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


def say(message):
    LOG.write("%7.1f  %s\n" % (time.monotonic() - T0, message))
    LOG.flush()


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
            finish()
        else:
            QTimer.singleShot(every, poll)
    poll()


def guard(fn):
    def run():
        try:
            fn()
        except Exception:  # noqa: BLE001
            say("ERROR\n" + traceback.format_exc())
            finish()
    return run


def finish():
    say("done; shots and this log are in %s" % OUT)
    LOG.close()
    QTimer.singleShot(500, lambda: os._exit(0))


# ------------------------------------------------------------------ steps

def start():
    win = iface.mainWindow()
    win.showNormal()
    win.setGeometry(40, 40, 1600, 980)
    win.raise_()
    win.activateWindow()
    if "equis_loader" not in plugins:
        loadPlugin("equis_loader")
        startPlugin("equis_loader")
    plugin = plugins["equis_loader"]
    plugin.run()
    panel = plugin.dialog
    STATE["panel"] = panel
    QgsProject.instance().addMapLayer(QgsRasterLayer(
        "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19&zmin=0",
        "OpenStreetMap", "wms"))
    panel.source_combo.setCurrentIndex(1)          # published parquet
    panel.parquet_edit.setText(DATA)
    panel._open_parquet_source()
    wait(lambda: panel._report_store is not None, loaded, "store open")


def loaded():
    panel = STATE["panel"]
    layers = [lyr for lyr in QgsProject.instance().mapLayers().values()
              if isinstance(lyr, QgsVectorLayer)]
    if layers:
        iface.setActiveLayer(layers[0])
        iface.zoomToActiveLayer()
    wait(lambda: panel._linked is not None and panel._linked.views is not None
         and panel._linked.views.ready_seen, warmed, "analysis view warm")


def warmed():
    panel = STATE["panel"]
    panel.tabs.setCurrentIndex(panel.analysis_index)
    linked = panel._linked
    idle = {"n": 0}

    def quiet():
        # A dozen polls with no task: the page's own first choice (it picks
        # a matrix when offered none) has come and gone, and cannot
        # overtake ours - a later choice wins (linked_views._on_choice).
        idle["n"] = idle["n"] + 1 if linked._task is None else 0
        return idle["n"] >= 12
    wait(quiet, choose, "controller quiet", limit=60)


def choose():
    panel = STATE["panel"]
    linked = panel._linked
    # By NAME: a prefix depends on the data (arsenic, or arsenic_t where
    # fractions split it), and a prefix the catalogue lacks is dropped.
    catalogue = linked.session.catalogue(MATRIX)
    prefix = {v["chemical"].lower(): v["prefix"] for v in catalogue["variables"]}
    choice = {"matrix": MATRIX, "x": prefix.get(X_NAME.lower()),
              "y": prefix.get(Y_NAME.lower()), "chart": "scatter", "mode": "all"}
    say("choosing %s" % choice)
    views = linked.views
    original = views.call

    def traced(name, payload):
        text = ("matrix=%s" % payload.get("matrix") if name == "offer"
                else json.dumps(payload)[:200])
        say("host -> page %s %s" % (name, text))
        return original(name, payload)
    views.call = traced
    # As the page's own chooser does it: through the bridge.
    page("window.equis.bridge.chosen(%s); 'sent'" % json.dumps(json.dumps(choice)),
         lambda r: None)
    drawn_then(lambda: QTimer.singleShot(1500, guard(lambda: (shot("1_tab.png"),
                                                             go_over()))))


def drawn_then(then):
    STATE["drawn"] = False

    def ask():
        page("JSON.stringify(!!(window.equis && window.equis.linked && "
             "window.equis.linked.box && window.equis.linked.box.solid))",
             lambda r: STATE.__setitem__("drawn", r == "true"))
        return STATE["drawn"]
    wait(ask, then, "chart and linked views drawn", limit=90)


def go_over():
    STATE["panel"].overlay_check.setChecked(True)
    drawn_then(lambda: QTimer.singleShot(2000, guard(lambda: (
        shot("2_over_map.png"), gate_tests()))))


def to_window(viewport_point):
    win = iface.mainWindow()
    g = iface.mapCanvas().viewport().mapToGlobal(viewport_point)
    return win.windowHandle(), win.mapFromGlobal(g)


def gate_tests():
    ov = STATE["panel"]._linked.overlay
    vp = iface.mapCanvas().viewport().rect()
    say("viewport %dx%d, cards %s" % (vp.width(), vp.height(), [
        (c.x(), c.y(), c.width(), c.height()) for c in ov.cards]))
    main = max(ov.cards, key=lambda c: c.bottom() - c.left() * 0.5)  # bottom-left
    STATE["main"] = main
    empty = QPoint(vp.width() // 2, vp.height() // 2)
    while ov.over_card(empty):
        empty += QPoint(0, 40)
    STATE["empty"] = empty
    window, p = to_window(main.center())
    QTest.mouseMove(window, p)
    QApplication.processEvents()
    STATE["card"] = ov.open
    window, q = to_window(empty)
    QTest.mouseMove(window, q)
    QApplication.processEvents()
    STATE["map"] = ov.open
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
    STATE["ctrl"] = ov.open
    say("page has the mouse: over a card %s (want True), over the map %s "
        "(want False), Ctrl over a card %s (want False)"
        % (STATE["card"], STATE["map"], STATE["ctrl"]))
    QTimer.singleShot(300, guard(brush_test))


def drag(start, end, steps=12):
    window, a = to_window(start)
    _, b = to_window(end)
    QTest.mouseMove(window, a)
    QApplication.processEvents()
    QTest.mousePress(window, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, a)
    for i in range(1, steps + 1):
        QTest.mouseMove(window, QPoint(a.x() + (b.x() - a.x()) * i // steps,
                                       a.y() + (b.y() - a.y()) * i // steps), 15)
        QApplication.processEvents()
    QTest.mouseRelease(window, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier, b)
    QApplication.processEvents()


def brush_test():
    main = STATE["main"]
    drag(QPoint(main.left() + int(main.width() * 0.40), main.top() + int(main.height() * 0.15)),
         QPoint(main.left() + int(main.width() * 0.85), main.top() + int(main.height() * 0.55)))
    QTimer.singleShot(2500, guard(lambda: page(
        "document.querySelector('.linked-line').textContent",
        lambda r: (STATE.__setitem__("brush", r), say("brush line: %s" % r),
                   QTimer.singleShot(800, guard(lambda: (shot("3_brushed.png"),
                                                        pan_test())))))))


def pan_test():
    iface.actionPan().trigger()
    canvas = iface.mapCanvas()
    before = canvas.extent().center()
    drag(STATE["empty"], STATE["empty"] + QPoint(-160, 90))
    after = canvas.extent().center()
    STATE["pan"] = abs(after.x() - before.x()) + abs(after.y() - before.y())
    say("a drag on open map moved the extent %.1f map units (want > 0)" % STATE["pan"])
    QTimer.singleShot(1500, guard(lambda: (shot("4_panned.png"), back())))


def back():
    panel = STATE["panel"]
    panel.overlay_check.setChecked(False)
    STATE["home"] = panel.analysis_page.layout().indexOf(panel._linked.views.view) >= 0
    drawn_then(lambda: QTimer.singleShot(1500, guard(lambda: (
        shot("5_back_in_tab.png"), summary()))))


def summary():
    checks = [("over a card, the page has the mouse", STATE.get("card") is True),
              ("over open map, the map has it", STATE.get("map") is False),
              ("Ctrl over a card, the map has it", STATE.get("ctrl") is False),
              ("a drag on the scatter brushes", "selected" in (STATE.get("brush") or "")),
              ("a drag on open map pans", (STATE.get("pan") or 0) > 0),
              ("the view goes back into the tab", STATE.get("home") is True)]
    for label, ok in checks:
        say("[%s] %s" % ("PASS" if ok else "FAIL", label))
    say("SUMMARY %s" % ("ALL PASS" if all(ok for _, ok in checks) else "FAILED"))
    finish()


say("loaded; data %s" % DATA)
QTimer.singleShot(3000, guard(start))
