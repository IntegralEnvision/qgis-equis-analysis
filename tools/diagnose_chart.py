r"""Why did the histogram not follow the pick? RUN IT IN THE QGIS CONSOLE.

    exec(open(r"<path-to>\equis_qgis\tools\diagnose_chart.py").read())

Run it with a histogram open (tools/try_us1.py) AFTER picking a different
chemical. It reads state rather than asking anyone to read logs: what the
panel thinks is open, what it would ask the page for, what it last asked,
and which build of the page script is actually loaded. Every line is also
written to the "equis-diagnose" log tab, so it can be copied in one go.

Reported 2026-10-01: after the first chart, picking another chemical
changed the caption but not the bars, and nothing appeared in equis-web.
"""


def _diagnose_chart():
    from qgis.core import Qgis, QgsMessageLog
    from qgis.utils import iface, plugins

    lines = []

    def say(message):
        lines.append(message)
        print(message)
        QgsMessageLog.logMessage(message, "equis-diagnose",
                                 Qgis.MessageLevel.Info)

    plugin = plugins.get("equis_loader")
    panel = getattr(plugin, "dialog", None) if plugin else None
    if panel is None:
        say("no EQuIS panel open")
        return None

    import equis_loader.dialog as dialog_module
    say("dialog.py loaded from: %s" % dialog_module.__file__)
    say("dialog.py has the chart logging: %s"
        % hasattr(panel, "_chart_log"))
    say("picked in the list: %s"
        % [a["chemical"] for a in panel._selected_analytes()])
    try:
        active = iface.activeLayer()
    except Exception:                           # noqa: BLE001
        active = None
    say("active layer: %s | its stamped chemical: %s"
        % (active.name() if active is not None else None,
           active.customProperty("equis/chemical", "") if active else None))
    scope, from_layer = panel._active_scope()
    say("the chart's scope comes from: %s -> %s"
        % ("the ACTIVE LAYER" if from_layer else "the list",
           (scope or {}).get("chemical")))

    linked = panel._linked
    say("panel._linked: %s" % (type(linked).__name__ if linked else None))
    views = getattr(linked, "views", None) if linked else None
    say("panel._linked.views: %s | ready_seen: %s"
        % (type(views).__name__ if views else None,
           getattr(views, "ready_seen", None)))
    say("last scope DRAWN by the panel (_drawn_scope): %s"
        % getattr(panel, "_drawn_scope", "never set"))
    say("last draw ASKED of the controller (_pending): %s"
        % (getattr(linked, "_pending", None),))
    say("scope the panel would draw now: %s" % panel._chart_scope())

    if views is None:
        say("=> No view on panel._linked: _refresh_chart returns at its "
            "first line, silently. The window on screen is not the one the "
            "panel is driving.")
        return None

    # Which page script is running. The new build logs "histogram #n"; the
    # old one has no draw counter.
    def report_page(result):
        say("page script: %s" % result)

    views.view.page().runJavaScript(
        "(function(){var e=window.equis; if(!e) return 'window.equis missing';"
        "var src=String(e.histogram);"
        "return (src.indexOf('asked')>=0 ? 'NEW build (logs each draw)' "
        ": 'OLD build (no draw logging)') + ' | chart caption: ' + "
        "((document.querySelector('.caption')||{}).textContent||'none');})()",
        report_page)

    # Then ask for a redraw the normal way and say what happened.
    before = getattr(panel, "_drawn_scope", None)
    drew = panel._refresh_chart()
    after = getattr(panel, "_drawn_scope", None)
    say("_refresh_chart() now returned %s; drawn scope %s"
        % (drew, "CHANGED" if before != after else "unchanged"))
    say("(the page's answer follows in a moment, as 'page script: ...')")
    return lines


_DIAG = _diagnose_chart()
