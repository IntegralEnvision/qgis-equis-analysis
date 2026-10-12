r"""US-24, the scatter, end to end against the real parquet. RUN IT IN QGIS.

    exec(open(r"<path-to>\equis_qgis\tools\try_scatter.py").read())

IN THE QGIS PYTHON CONSOLE, for the reasons tools/try_us1.py gives: Qt
WebEngine needs the application QGIS itself creates, and the console
already runs the event loop, so this returns at once and reports a few
seconds later from a timer.

WHAT IT NEEDS: the EQuIS panel open with the Facility A parquet loaded. It
draws Arsenic x Chromium in soil (SO) - the test pair since 2026-10-09 -
unless TRY_SCATTER is set first, e.g. the earlier water pair:

    TRY_SCATTER = ("Manganese", "Lead", "T", "WS")

WHAT IT SHOULD SHOW, measured headlessly on 2026-10-09 against the same
pivot (tools/test_scatter_sql.py and tools/test_bivariate.py check it):

    112 samples (the primaries), mg/kg on both axes, log axes
    724 field replicates (_R2 ... _R9) not plotted - stated in the caption
    both detected 109 | As only 2 | Cr only 0 | neither 1
    Jenks (log10): As <= 2.69 | 2.69-9.85 | > 9.85; Cr <= 2.64 | 2.64-6.34 | > 6.34
    scatter map: 10 stations, each its one most recent primary sample

It also adds a "scatter map" layer: one point per station, coloured by
its most recent sample carrying both (3x3 biscale-style colours; grey,
in the scatter's symbol, when either is a non-detect).

Then drag a rectangle on the scatter: those stations highlight on the
scatter map. Selecting stations on the scatter map (or the Locations
layer) dims the scatter without moving its axes.
"""


def _try_scatter():
    from qgis.core import Qgis, QgsMessageLog
    from qgis.PyQt.QtCore import QTimer
    from qgis.utils import iface, plugins

    def say(message):
        print(message)
        QgsMessageLog.logMessage(message, "EQuIS", Qgis.MessageLevel.Info)

    x, y, fraction, matrix = globals().get(
        "TRY_SCATTER", ("Arsenic", "Chromium", None, "SO"))

    plugin = plugins.get("equis_loader")
    panel = getattr(plugin, "dialog", None) if plugin else None
    if panel is None:
        say("[1/4] The EQuIS panel is not open. Open it, load the parquet "
            "source, then run this again.")
        return None
    if panel._report_store is None:
        say("[1/4] No store: load a parquet source (or fetch a report) first.")
        return None
    say("[1/4] panel found; %s rows in the store"
        % format(panel._report_store.count(), ",d"))

    # The panel owns the view now: it lives in the Analysis tab, warmed in
    # the background when data loaded (2026-10-09). open_scatter switches to
    # that tab and draws there - no window of the probe's own.
    if not hasattr(panel, "tabs"):
        say("[2/4] an older plugin build without the Analysis tab; reload it.")
        return None
    say("[2/4] using the panel's Analysis tab")

    # open_scatter resolves the names, builds the pivot and asks the page to
    # draw, in the panel's long-lived view.
    pivot = panel.open_scatter(x, y, fraction=fraction, matrix=matrix)
    views = panel._linked.views if panel._linked is not None else None
    if views is None:
        say("[3/4] the analysis view is not available - see the status line")
        return None
    if pivot is None:
        say("[3/4] the scatter was refused - the panel's status line says why")
        return None
    say("[3/4] pivot %s: %s samples, %s carry both, excluded %s"
        % (pivot.table, format(pivot.rows, ",d"), format(pivot.complete, ",d"),
           pivot.excluded))

    import time as _time
    asked = _time.monotonic()

    def report(tries=[0]):
        # Poll until the page says it has drawn (its timings arrive with the
        # first SVG mark), so the verdict is not a guess at a fixed delay.
        def got(result):
            import json as _json
            data = _json.loads(result) if result else {}
            state = data.get("state") or {}
            if not state.get("timings") and tries[0] < 30:
                tries[0] += 1
                QTimer.singleShot(1000, report)
                return
            say("[4/4] page drew after %.1f s: %s samples; classes %s"
                % (_time.monotonic() - asked, state.get("total"),
                   state.get("counts")))
            say("      page timings (ms): %s" % state.get("timings"))
            say("      the host's own step timings are in the equis-web log tab")
        views.view.page().runJavaScript(
            "JSON.stringify({state: window.equis && window.equis.chart "
            "&& window.equis.chart.state})", got)

    QTimer.singleShot(1000, report)
    say("[4/4] asked the page to draw; it reports when it has drawn")
    say("To close: plugins['equis_loader'].dialog._close_linked_views()")

    class Probe(object):
        pass
    probe = Probe()
    probe.views, probe.panel, probe.pivot = views, panel, pivot
    probe.report = report
    return probe


_US24 = _try_scatter()
