r"""The Analysis tab, timed: warm-up in the background, then tab -> drawn.

    exec(open(r"<path-to>\equis_qgis\tools\try_analysis_tab.py").read())

RUN IT IN THE QGIS CONSOLE, after loading data (the Facility A parquet) on the
Data tab - and, for an honest "cold" number, in a QGIS where nothing else
has opened a web view yet. Loading the data starts the analysis view in
the background (dialog._schedule_analysis_warmup); this probe then reports:

  1. whether the warm-up happened, and how long the view took to be ready
     after it was built (Chromium start + page load), while hidden;
  2. switches to the Analysis tab, as a click would, and draws a scatter;
  3. how long from that switch to the first mark on screen.

The point being tested (decided 2026-10-09): a view warmed while HIDDEN
must be usable the moment the tab is shown. Chromium gives a hidden view
no animation frames, and Mosaic's Arrow queries wait for one - so if the
warm-up only got as far as loading, (3) would show it.

Default pair: Arsenic x Chromium in soil, the test pair. Override with
TRY_SCATTER = ("Manganese", "Lead", "T", "WS") before running.
"""


def _try_analysis_tab():
    import json
    import time
    from qgis.core import Qgis, QgsMessageLog
    from qgis.PyQt.QtCore import QTimer
    from qgis.utils import plugins

    def say(message):
        print(message)
        QgsMessageLog.logMessage(message, "EQuIS", Qgis.MessageLevel.Info)

    x, y, fraction, matrix = globals().get(
        "TRY_SCATTER", ("Arsenic", "Chromium", None, "SO"))
    plugin = plugins.get("equis_loader")
    panel = getattr(plugin, "dialog", None) if plugin else None
    if panel is None or not hasattr(panel, "tabs"):
        say("[1/3] The EQuIS panel is not open, or is an older build without "
            "the Analysis tab. Reload the plugin, load data, run again.")
        return None
    if panel._report_store is None:
        say("[1/3] No data loaded yet. Load the parquet on the Data tab first.")
        return None

    linked = panel._linked
    views = linked.views if linked is not None else None
    if views is None:
        say("[1/3] NOT warmed: no analysis view exists yet. (Loading data "
            "should have started it 1.5 s later - check the equis-web log.)")
    elif not views.ready_seen:
        say("[1/3] warming: the view exists but is not ready yet "
            "(%.1f s since built)." % (time.monotonic() - views._built_at))
    else:
        say("[1/3] warmed while hidden: ready %.2f s after it was built, "
            "%.1f s ago." % (views.ready_at - views._built_at,
                             time.monotonic() - views.ready_at))

    clicked = time.monotonic()
    # What a click on the tab does; open_scatter would also switch, but
    # doing it first separates "tab shown" from "data asked for".
    panel.tabs.setCurrentIndex(panel.analysis_index)
    pivot = panel.open_scatter(x, y, fraction=fraction, matrix=matrix)
    if pivot is None:
        say("[2/3] the scatter was refused - the status line says why.")
        return None
    say("[2/3] tab shown and scatter asked for (%s x %s): %s samples"
        % (x, y, format(pivot.rows, ",d")))

    views = panel._linked.views
    tries = [0]

    def poll():
        def got(result):
            state = (json.loads(result) if result else {}).get("state") or {}
            if not state.get("timings") and tries[0] < 60:
                tries[0] += 1
                QTimer.singleShot(250, poll)
                return
            say("[3/3] tab shown -> scatter drawn: %.2f s  (page phases, ms: %s)"
                % (time.monotonic() - clicked, state.get("timings")))
        views.view.page().runJavaScript(
            "JSON.stringify({state: window.equis && window.equis.chart "
            "&& window.equis.chart.state})", got)

    QTimer.singleShot(250, poll)
    return panel


_ANALYSIS = _try_analysis_tab()
