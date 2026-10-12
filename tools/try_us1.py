r"""US-1, end to end, against the real parquet. RUN IT IN QGIS.

This is the step every web test has been leading to and none has taken: a
live Chromium loads the bundle, queries the store over the equis:// scheme,
draws a histogram, and a brush dragged across it highlights the matching
locations on the map.

    exec(open(r"<path-to>\equis_qgis\tools\try_us1.py").read())

IN THE QGIS PYTHON CONSOLE, not python-qgis.bat. Qt WebEngine needs the
application QGIS itself creates; under a bare QApplication the scheme
registers, the handler is reached, the file is read, and the load still
fails with ok=False and no error anywhere.

TWO THINGS THE CONSOLE FORCES, both learned by breaking them:

  * No SystemExit - pasted into the console, it closes the console.
  * No app.exec() - the console already runs the event loop, and a second
    one nests inside it and hangs. So this returns immediately and the
    verdict arrives a few seconds later, from a callback.

WHY IT BUILDS ITS OWN WINDOW. web_view sets WA_DontShowOnScreen when it is
given no parent, so a headless histogram still receives animation frames -
Mosaic defers Arrow-only requests through requestAnimationFrame and a view
that gets no frames waits forever. That is right for the headless case.
Clearing the attribute AFTERWARDS does not help: show() has already made
the window off-screen and Qt will not move it on-screen without recreating
it, which is why the first version of this probe displayed nothing at all.
So the view is given a real parent from the start and never goes
off-screen.

WHAT IT NEEDS FIRST: the EQuIS panel open, a parquet source loaded, and a
CHEMICAL SELECTED. The histogram refuses to bin a whole store - without a
chemical the only predicate is "has a value", which here is 3.4 million
rows - so a selection is the precondition, not a convenience.
"""


def _try_us1():
    from qgis.core import Qgis, QgsMessageLog
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtWidgets import QVBoxLayout, QWidget
    from qgis.utils import iface, plugins

    def say(message):
        print(message)
        QgsMessageLog.logMessage(message, "EQuIS", Qgis.MessageLevel.Info)

    plugin = plugins.get("equis_loader")
    panel = getattr(plugin, "dialog", None) if plugin else None
    if panel is None:
        say("[1/5] The EQuIS panel is not open. Open it, load a parquet "
            "source, pick a chemical, then run this again.")
        return None
    say("[1/5] panel found")

    analytes = panel._selected_analytes()
    if not analytes:
        say("[2/5] No chemical selected. The histogram bins a FILTERED "
            "view, never the whole store - pick one and run again.")
        return None
    if panel._report_store is None:
        say("[2/5] No store: this needs a parquet source (or a built "
            "report store) to query.")
        return None
    say("[2/5] %s rows | chemical: %s | scope: %s"
        % (format(panel._report_store.count(), ",d"),
           analytes[0]["chemical"], panel._scope_label() or "unfiltered"))

    # A REAL WINDOW, and the view is built INTO it. Passing a parent is what
    # keeps web_view from marking the view WA_DontShowOnScreen; done the
    # other way round the window exists but never appears.
    window = QWidget(iface.mainWindow())
    window.setWindowTitle("US-1 probe - drag a brush across the histogram")
    # A top-level window despite having the main window as parent: parented
    # so it cannot outlive QGIS, floating so it can be moved off the map.
    window.setWindowFlag(Qt.WindowType.Window, True)
    window.resize(900, 420)
    layout = QVBoxLayout(window)
    layout.setContentsMargins(0, 0, 0, 0)

    from equis_loader.linked_views import LinkedViewsController
    from equis_loader.store import ND_COLUMN
    from equis_loader.web_view import WebViewError

    panel._close_linked_views()          # start clean on a re-run
    panel._linked = LinkedViewsController(
        connection_for=lambda: (panel._report_store.con
                                if panel._report_store is not None else None),
        layer_for=lambda: panel._result_layer,
        on_status=lambda text: panel._set_status(text) if text else None)
    if not panel._linked.available():
        say("[3/5] The linked views need QtWebEngine and the built web "
            "bundle; one of them is missing.")
        return None
    try:
        views = panel._linked.open(parent=window)
    except WebViewError as exc:
        say("[3/5] the view would not open: %s" % exc)
        return None
    say("[3/5] view built with a parent (so it is NOT off-screen)")

    layout.addWidget(views.view)
    window.show()
    window.raise_()
    say("[4/5] window shown: visible=%s size=%s"
        % (views.view.isVisible(), views.view.size()))

    panel._linked.prepare()

    # ASK FOR THE CHART. The page connects and then waits - what to bin is
    # the panel's decision, because the chart must show the same rows the
    # MAP shows. Without this the window connects and stays empty, which is
    # exactly what the first live run produced.
    analyte = analytes[0]
    scope = {
        "chemical": analyte.get("chemical"),
        "casRn": analyte.get("cas_rn") or None,
        "matrix": (panel._matrix_mode() or [None])[0],
        "fraction": (panel._fraction_mode() or [None])[0],
        "method": (panel._method_mode() or [None])[0],
        # The store's OWN mapping, not a copy: a second table of these
        # names is how the page and the store come to disagree about which
        # non-detect convention a chart is showing.
        "ndColumn": ND_COLUMN.get(panel._non_detect_mode(), "value_half"),
        "aggregate": panel._active_aggregation or "unaggregated",
    }
    asked = panel._linked.draw(scope)
    say("[5/5] layer indexed; histogram requested (%s): %s"
        % ("drawn now" if asked else "queued until the page is ready",
           {k: v for k, v in scope.items() if v}))
    say("")
    say("WHAT TO WATCH, in the order it can fail:")
    say("  a. the window is blank white     -> the page did not load")
    say("  b. the page loads, no bars       -> the query did not answer")
    say("  c. bars draw, brush does nothing -> the brush did not reach QGIS")
    say("  d. bars draw, map highlights     -> US-1 works")
    say("")
    say("Then: _US1.views.failed  shows any load error,")
    say("      _US1.views.ready_seen  is True once the page said hello.")
    say("To close: plugins['equis_loader'].dialog._close_linked_views()")

    class Probe(object):
        pass
    probe = Probe()
    probe.window = window
    probe.views = views
    probe.panel = panel
    return probe


_US1 = _try_us1()
