"""`equis://` links that drive the panel, from anywhere Qt opens a URL.

WHAT THIS IS FOR. A location on the map knows its own code, and the panel
knows how to plot a series or build a layer - but there was no way to get
from one to the other except by reading a code off an identify panel and
retyping it into a picker. A map tip can carry a link per feature:

    <a href="equis://series?location=[% "code" %]">Plot this location</a>

and clicking it calls straight into the panel with that location.

HOW IT IS CAUGHT. QDesktopServices.setUrlHandler registers a scheme
process-wide: any widget that opens a URL - map tips, attribute forms,
anything rendering HTML through Qt - routes `equis://` to a Python callable
instead of to the browser. Verified on QGIS 4.2.1: openUrl returned True,
the handler received the whole URL, and host/query parsed cleanly into an
action and its parameters.

THIS IS NOT THE web_assets.py SCHEME. That one is a QWebEngineUrlScheme
serving files INTO a web view, and it only exists inside WebEngine. This is
Qt's desktop-services hook, which works in ordinary widgets and has nothing
to do with WebEngine. They share a word and no machinery.

REGISTRATION IS PROCESS-WIDE, WHICH MATTERS ON RELOAD. Qt keeps one handler
per scheme and a plugin is reloaded often, so the handler object is held in
a module-level registry that survives the reload and is repointed at the new
panel, rather than a second one being registered over the first. That is the
same hazard web_assets.py documents, for the same reason - see hazard 2
there. unregister() puts the scheme back so a disabled plugin stops
answering links it can no longer service.

A LINK IS UNTRUSTED INPUT. It can be typed, pasted, or come from a project
file someone else wrote. Every action therefore validates what it is given
and does nothing it could not do from its own UI: there is no action here
that writes, deletes, or reaches the network by itself.
"""

from qgis.PyQt.QtCore import QObject, QUrl, QUrlQuery, pyqtSlot

SCHEME = "equis"

# The live handler, kept across plugin reloads. Qt will not replace a
# registered handler, so this one is repointed instead of re-registered.
_REGISTRY = {}


def _clean(value, limit=200):
    """One query value, trimmed and length-capped.

    A link can carry anything; the panel's pickers cannot. Nothing here is
    ever used to build SQL - the callbacks take it as parameters to the same
    functions the UI calls - but a value long enough to fill a combo box or
    a status line is its own small denial of service.
    """
    text = (value or "").strip()
    return text[:limit]


class UrlActions(QObject):
    """Turns an `equis://` URL into a call on the panel.

    The panel is passed in rather than imported, so this file has no
    knowledge of the dialog beyond the four methods it calls - and can be
    tested with a stand-in that records them.
    """

    def __init__(self, panel=None):
        super().__init__()
        self.panel = panel
        # Every URL that arrived, for tests and for the log when an action
        # is not recognised.
        self.last = None

    # ------------------------------------------------------------ handling

    @pyqtSlot(QUrl)
    def handle(self, url):
        """Qt's entry point. Never raises: this is called from Qt's event
        loop, where an exception would surface as a crash rather than as a
        message the user can act on."""
        try:
            self.dispatch(url)
        except Exception as exc:                # noqa: BLE001
            self._warn("This link could not be followed: %s" % exc)

    def dispatch(self, url):
        """Parse and route. Returns the action name, or None if unknown."""
        if isinstance(url, str):
            url = QUrl(url)
        self.last = url.toString()
        if url.scheme() != SCHEME:
            return None
        # `equis://series?...` puts the action in the HOST, which Qt
        # lower-cases for us. A path is accepted too, so `equis:///series`
        # from a hand-written link still works.
        action = (url.host() or url.path().strip("/")).lower()
        query = QUrlQuery(url)
        params = {key.lower(): _clean(value)
                  for key, value in query.queryItems()}

        if self.panel is None:
            self._warn("The EQuIS panel is not open, so this link has "
                       "nothing to act on.")
            return None

        if action == "series":
            return self._series(params)
        if action == "map":
            return self._map(params)
        if action == "select":
            return self._select(params)
        self._warn("Unknown EQuIS link action: %r" % action)
        return None

    # ------------------------------------------------------------- actions

    def _series(self, params):
        """Plot one location's history for the analyte in the link.

        The analyte is OPTIONAL: a location link that does not name one
        plots whatever the panel already has selected, which is what makes
        a single map tip useful while the user changes chemicals.
        """
        location = params.get("location") or params.get("loc")
        if not location:
            self._warn("This link names no location.")
            return None
        analyte = params.get("analyte") or params.get("chemical")
        if analyte:
            self.panel.select_analyte(analyte, params.get("cas_rn"))
        self._apply_scope(params)
        self.panel.plot_location(location)
        return "series"

    def _map(self, params):
        """Build the result layer for the analyte in the link."""
        analyte = params.get("analyte") or params.get("chemical")
        if not analyte:
            self._warn("This link names no chemical to map.")
            return None
        self.panel.select_analyte(analyte, params.get("cas_rn"))
        self._apply_scope(params)
        self.panel.map_selected(params.get("how"))
        return "map"

    def _select(self, params):
        """Change the pickers or the map selection - no plot, no new layer.

        The action that cannot surprise anyone: it sets things up and lets
        the user decide what to do with them. From the identify panel it
        selects the location on the map, which is what makes "compare these
        three wells" possible without typing any codes.
        """
        analyte = params.get("analyte") or params.get("chemical")
        if analyte:
            self.panel.select_analyte(analyte, params.get("cas_rn"))
        self._apply_scope(params)
        location = params.get("location") or params.get("loc")
        if location and hasattr(self.panel, "select_location"):
            self.panel.select_location(location,
                                       add=params.get("add") == "1")
        return "select"

    def _apply_scope(self, params):
        """Matrix, fraction and method, when the link names them.

        These are the filters CLAUDE.md forbids crossing, so a link that
        names one must be honoured exactly - and a link that names none must
        leave the panel's own choice alone rather than silently widening it.
        """
        scope = {key: params[key]
                 for key in ("matrix", "fraction", "method") if params.get(key)}
        if scope:
            self.panel.apply_scope(**scope)

    # -------------------------------------------------------------- output

    def _warn(self, message):
        panel = self.panel
        if panel is not None and hasattr(panel, "note_link_problem"):
            panel.note_link_problem(message)
            return
        try:
            from qgis.core import Qgis, QgsMessageLog
            QgsMessageLog.logMessage(message, "EQuIS", Qgis.MessageLevel.Warning)
        except Exception:                       # noqa: BLE001
            pass


def register(panel):
    """Route `equis://` to `panel`, surviving a plugin reload.

    Qt keeps the FIRST handler registered for a scheme, so a reload that
    registered a new one would leave the old object answering links for a
    panel that no longer exists. The handler is therefore created once and
    repointed.
    """
    from qgis.PyQt.QtGui import QDesktopServices
    handler = _REGISTRY.get("handler")
    if handler is None:
        handler = UrlActions(panel)
        _REGISTRY["handler"] = handler
        QDesktopServices.setUrlHandler(SCHEME, handler, "handle")
    else:
        handler.panel = panel
    return handler


def unregister():
    """Stop answering `equis://`, and forget the panel.

    Called when the plugin is unloaded: a handler still pointing at a closed
    panel would swallow the link and do nothing, which is worse than the
    link failing visibly.
    """
    from qgis.PyQt.QtGui import QDesktopServices
    handler = _REGISTRY.pop("handler", None)
    if handler is not None:
        handler.panel = None
        QDesktopServices.unsetUrlHandler(SCHEME)
    return handler is not None


# What a location offers in the identify panel. Each is (title, action,
# extra query), and the feature's own code is substituted by QGIS.
LOCATION_ACTIONS = (
    ("Plot this location", "series", ""),
    ("Add to map selection", "select", "&add=1"),
)


def install_actions(layer, actions=LOCATION_ACTIONS):
    """Put `equis://` actions on a layer, for QGIS's OWN identify panel.

    WHY THIS RATHER THAN A MAP TOOL. Subclassing QgsMapToolIdentify would
    work, but it needs the user to be IN that tool - a mode to enter, and a
    second opinion about what a click on the map means while the panel
    already owns one for the time-series pick. An action needs no mode at
    all: it appears as a button in the identify results the user is already
    looking at, and in the attribute table and the feature form for free.

    OpenUrl RATHER THAN GenericPython. A GenericPython action runs plugin
    code directly, which sounds more direct and is worse here. It is a
    string of source stored in the project file - so it cannot be
    type-checked, moves out of this repository into whatever .qgz someone
    saves, and QGIS runs it through machinery that needs a live iface,
    which makes it untestable outside a running QGIS. An OpenUrl action
    goes through QDesktopServices, which is exactly the handler in this
    file. Verified on QGIS 4.2.1: the action fired, the feature's code was
    substituted into the URL, and the handler received it.

    Idempotent: a layer is rebuilt whenever the map is redrawn, and
    actions added again each time would stack up in the identify panel.
    """
    try:
        from qgis.core import QgsAction
    except ImportError:                         # pragma: no cover
        return 0

    manager = layer.actions()
    existing = {action.name() for action in manager.actions()}
    added = 0
    for title, verb, extra in actions:
        if title in existing:
            continue
        url = '%s://%s?location=[%% "code" %%]%s' % (SCHEME, verb, extra)
        manager.addAction(QgsAction(
            QgsAction.ActionType.OpenUrl, title, url, "", False, title,
            # Feature and Canvas: the identify panel and the map's own
            # right-click. Not Layer - these act on ONE location, and a
            # layer-scoped action offering to plot "the layer" is a lie.
            {"Feature", "Canvas"}))
        added += 1
    return added


def map_tip(actions=("series", "map"), analyte_field=None):
    """An HTML map tip whose links drive the panel.

    Built here rather than written into a layer by hand so the parameter
    names stay in one place: a tip written elsewhere with `loc=` instead of
    `location=` fails silently at click time, which is a poor way to find a
    typo.
    """
    lines = ['<b>[% "code" %]</b>']
    lines.append('<span style="color:#5a6b7b">'
                 '[% "results" %] result(s)</span>')
    analyte = ("&analyte=[%% %s %%]" % analyte_field) if analyte_field else ""
    if "series" in actions:
        lines.append('<a href="%s://series?location=[%% "code" %%]%s">'
                     'Plot this location</a>' % (SCHEME, analyte))
    if "map" in actions and analyte_field:
        lines.append('<a href="%s://map?analyte=[%% %s %%]">'
                     'Map this chemical</a>' % (SCHEME, analyte_field))
    return "<br/>".join(lines)
