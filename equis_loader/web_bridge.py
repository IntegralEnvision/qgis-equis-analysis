"""The QWebChannel object the web view talks to.

Phase 2.3 of docs/implementation-plan.md. Deliberately thin: everything that
decides WHAT crosses the wire lives in bridge_payload.py, which has no Qt in
it and is tested under a plain interpreter. This file is the Qt shape around
that - slots, signals, and the handshake - and nothing else.

THE PAGE CONTRACT, which tools/mosaic_poc/overlay.html already follows:

    in   bridge.ready()                 the page has built its state AND
                                        connected to our signals
    in   bridge.query(sql, kind) -> str the Mosaic connector contract
    in   bridge.log(level, message)
    in   bridge.chosen(json)            the Analysis chooser's {matrix, x, y}
    out  bridge.datasetChanged()        the store underneath was replaced

READY, NOT loadFinished. `loadFinished` fires when the DOM is parsed, which
is before the page has constructed its QWebChannel and connected to our
signals - so anything emitted then is delivered into the void. The webengine
probe reported a working channel as broken for exactly this reason before it
grew this handshake, and the overlay POC carries the same note. Emit nothing
before `ready()`.

ERRORS TRAVEL AS DATA. A slot cannot raise across the channel: a Python
exception in a slot is logged by PyQt and the JS caller receives null, which
is indistinguishable from an empty result. So `query` catches everything and
returns a JSON envelope the page can branch on. A chart that silently draws
nothing because the SQL was wrong is the failure this avoids.
"""

import json

from qgis.PyQt.QtCore import QObject, pyqtSignal, pyqtSlot

from . import bridge_payload

try:
    from qgis.core import Qgis, QgsMessageLog
except ImportError:  # pragma: no cover - only for import outside QGIS
    Qgis = QgsMessageLog = None

LOG_TAG = "equis-web"

# Envelope keys. The page checks `ok` first and reads `error` when it is
# false; `kind` echoes what was asked for so a connector can dispatch on it
# without remembering what it sent.
KEY_OK = "ok"
KEY_KIND = "kind"
KEY_DATA = "data"
KEY_ERROR = "error"


def _log(message, level=None):
    if QgsMessageLog is None:  # pragma: no cover - outside QGIS
        return
    QgsMessageLog.logMessage(str(message), LOG_TAG,
                             level if level is not None else Qgis.Info)


class WebBridge(QObject):
    """Exposed to the page as `bridge`.

    `connection_for` is a callable returning the DuckDB connection to query,
    or None when there is not one yet. A callable rather than a connection so
    the bridge does not pin a store that the panel has since discarded - the
    panel rebuilds its store on every fetch, and a bridge holding the old one
    would answer from data the map no longer shows.
    """

    datasetChanged = pyqtSignal()

    def __init__(self, connection_for, on_ready=None, on_brush=None,
                 on_choice=None, parent=None):
        super().__init__(parent)
        self._connection_for = connection_for
        self._on_ready = on_ready
        self._on_brush = on_brush
        self._on_choice = on_choice
        self.ready_seen = False
        # Set by the overlay (map_overlay.py) while the view is over the map.
        self.on_cards = None
        self.on_asked = None

    # ----------------------------------------------------------- handshake

    @pyqtSlot()
    def ready(self):
        """The page has built its state and connected to our signals."""
        self.ready_seen = True
        if self._on_ready is not None:
            self._on_ready()

    # --------------------------------------------------------------- query

    @pyqtSlot(str, str, result=str)
    def query(self, sql, kind):
        """Run `sql` and return a JSON envelope. Never raises.

        The envelope exists because a slot cannot raise across the channel.
        PyQt logs the exception and hands JS a null, which a connector cannot
        tell from "the query returned nothing" - so the page would draw an
        empty chart and present it as the answer.
        """
        connection = None
        try:
            connection = self._connection_for()
        except Exception as exc:  # noqa: BLE001 - the page still needs told
            return self._fail("no data connection: %s" % exc, kind)
        if connection is None:
            return self._fail("no report is loaded", kind)
        try:
            data = bridge_payload.answer(connection, sql,
                                         kind or bridge_payload.KIND_ARROW)
        except bridge_payload.PayloadError as exc:
            _log("query failed: %s\n    %s" % (exc, sql),
                 None if Qgis is None else Qgis.Warning)
            return self._fail(str(exc), kind)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            _log("query raised %s: %s\n    %s" % (type(exc).__name__, exc, sql),
                 None if Qgis is None else Qgis.Critical)
            return self._fail("%s: %s" % (type(exc).__name__, exc), kind)
        return json.dumps({KEY_OK: True, KEY_KIND: kind, KEY_DATA: data})

    @staticmethod
    def _fail(message, kind):
        return json.dumps({KEY_OK: False, KEY_KIND: kind,
                           KEY_ERROR: str(message)})

    # ----------------------------------------------------------- diagnostics

    @pyqtSlot(str)
    def brushed(self, payload):
        """The page's brush changed. JSON, or "" when it was cleared.

        The page sends what it selected; it does not decide how a selection
        is DRAWN. That belongs to the map - see brush_layer.py, where the
        arrangement was chosen by measurement rather than taste.
        """
        if self._on_brush is None:
            return
        try:
            brush = json.loads(payload) if payload else None
        except ValueError:
            _log("brush payload was not JSON: %r" % (payload[:120],),
                 None if Qgis is None else Qgis.Warning)
            return
        try:
            self._on_brush(brush)
        except Exception as exc:  # noqa: BLE001 - a bad handler must not
            # take the channel down with it; the next brush should work.
            _log("brush handler raised %s: %s" % (type(exc).__name__, exc),
                 None if Qgis is None else Qgis.Critical)

    @pyqtSlot(str)
    def chosen(self, payload):
        """The Analysis tab's chooser changed: JSON {matrix, x, y}.

        The page holds the selection and nothing else - which samples,
        cells and breaks a choice means is analysis.py's to decide.
        """
        if self._on_choice is None:
            return
        try:
            choice = json.loads(payload) if payload else {}
        except ValueError:
            _log("choice payload was not JSON: %r" % (payload[:120],),
                 None if Qgis is None else Qgis.Warning)
            return
        try:
            self._on_choice(choice)
        except Exception as exc:  # noqa: BLE001 - as for a brush: the next
            # choice should still work.
            _log("choice handler raised %s: %s" % (type(exc).__name__, exc),
                 None if Qgis is None else Qgis.Critical)

    @pyqtSlot(str)
    def cards(self, payload):
        """Over the map: the page's cards, [{x, y, w, h}] in CSS pixels.

        The overlay gives the page the mouse over these and nowhere else
        (map_overlay.py). Sent whenever the page's layout changes.
        """
        if self.on_cards is None:
            return
        try:
            self.on_cards(json.loads(payload) if payload else [])
        except Exception as exc:  # noqa: BLE001 - as for a brush
            _log("cards handler raised %s: %s" % (type(exc).__name__, exc),
                 None if Qgis is None else Qgis.Critical)

    @pyqtSlot(str)
    def asked(self, what):
        """A page button asked the host for something it owns: "dock" puts
        the view back in the panel. The page only asks; Qt moves widgets."""
        if self.on_asked is None:
            return
        try:
            self.on_asked(str(what))
        except Exception as exc:  # noqa: BLE001
            _log("asked handler raised %s: %s" % (type(exc).__name__, exc),
                 None if Qgis is None else Qgis.Critical)

    @pyqtSlot(str, str)
    def log(self, level, message):
        """The page's console, routed to the QGIS log.

        A web view's own console is invisible unless a developer has the
        inspector open, so a page that fails quietly fails invisibly.
        """
        levels = {}
        if Qgis is not None:
            levels = {"warning": Qgis.Warning, "error": Qgis.Critical}
        _log("[page] %s" % message, levels.get((level or "").lower()))
