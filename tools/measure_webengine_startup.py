r"""How much of opening a chart is Chromium starting, and how much is the page?

    exec(open(r"<path-to>\equis_qgis\tools\measure_webengine_startup.py").read())

RUN IT IN A FRESHLY STARTED QGIS, before anything else has opened a web
view: the first measurement is only "cold" once per QGIS process.

Asked 2026-10-09: opening the scatter took ~10 s, and the worry is that
every window built on Chromium + Mosaic pays that. Whether it does depends
on a split this measures and nothing has before:

  1. COLD  - the first view in the process: Chromium's browser and GPU
             processes start, then the page loads.
  2. WARM  - a second, new view: the engine is up; only a new page (a
             renderer, the 600 KB bundle, the QWebChannel handshake).
  3. RELOAD - the same view, page reloaded: the page alone.

Each is "built -> page says ready()", the same moment the panel can first
draw. No data is involved, so the numbers are the engine's, not DuckDB's.

If COLD dominates and WARM is small, one warm-up at plugin start makes
every later window fast wherever it lives. If WARM is also seconds, the
answer is one long-lived page that is reused, not re-created.
"""


def _measure_webengine():
    import time
    from qgis.core import Qgis, QgsMessageLog
    from qgis.PyQt.QtCore import Qt, QTimer
    from qgis.PyQt.QtWidgets import QVBoxLayout, QWidget
    from qgis.utils import iface

    from equis_loader import web_view

    def say(message):
        print(message)
        QgsMessageLog.logMessage(message, "EQuIS", Qgis.MessageLevel.Info)

    if not (web_view.available() and web_view.bundle_present()):
        say("QtWebEngine or the web bundle is missing; nothing to measure.")
        return None

    results = {}
    keep = []

    def window():
        # A real, shown window: Mosaic's Arrow path needs animation frames,
        # and an off-screen view is not what the panel would use.
        w = QWidget(iface.mainWindow())
        w.setWindowFlag(Qt.WindowType.Window, True)
        w.resize(500, 300)
        QVBoxLayout(w).setContentsMargins(0, 0, 0, 0)
        w.show()
        keep.append(w)
        return w

    def open_view(label, then):
        w = window()
        w.setWindowTitle("startup probe: %s" % label)
        started = time.monotonic()
        holder = {}

        def ready():
            results[label] = time.monotonic() - started
            say("%-7s %.2f s  (built -> page ready)" % (label, results[label]))
            QTimer.singleShot(300, lambda: then(holder["views"]))

        views = web_view.LinkedViews(lambda: None, parent=w, on_ready=ready)
        holder["views"] = views
        w.layout().addWidget(views.view)

    def reload(views, then):
        started = time.monotonic()
        views.ready_seen = False

        def ready():
            results["RELOAD"] = time.monotonic() - started
            say("%-7s %.2f s  (same view, page reloaded)" % ("RELOAD",
                                                             results["RELOAD"]))
            QTimer.singleShot(300, then)

        views._on_ready = ready
        views.view.reload()

    def finish():
        say("")
        cold, warm, again = (results.get(k) for k in ("COLD", "WARM", "RELOAD"))
        if None not in (cold, warm, again):
            say("Engine start-up (COLD - WARM): ~%.2f s, paid once per QGIS "
                "session." % (cold - warm))
            say("A new page (WARM): %.2f s; a reload of a live view: %.2f s."
                % (warm, again))
        say("Close the probe windows when done: _STARTUP.close()")

    class Probe(object):
        results = None

        def close(self):
            for w in keep:
                w.close()
    probe = Probe()
    probe.results = results

    say("Opening three views in turn; each prints when its page is ready...")
    open_view("COLD", lambda first: open_view(
        "WARM", lambda second: reload(second, finish)))
    return probe


_STARTUP = _measure_webengine()
