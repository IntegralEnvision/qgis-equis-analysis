"""The QWebEngineView that hosts the linked-views page.

Phase 2.5. This is the piece that makes the other three real: the bundle
(web/), the scheme handler (web_assets.py) and the bridge (web_bridge.py)
were each tested alone, and until something constructs a view the contract
between them is written rather than exercised.

ORDER OF CONSTRUCTION MATTERS, and each step is here because getting it
wrong fails in a way that looks like something else:

  1. Register the scheme BEFORE any page exists. QWebEngineUrlScheme wants
     registration before the application; a plugin cannot do that, but it
     must at least happen before the profile serves a request.
  2. Install the handler, which needs a QApplication -
     QWebEngineProfile.defaultProfile() aborts the process without one.
  3. Build the channel and register the bridge BEFORE load(). A page that
     loads first may run its script before the transport exists.
  4. load() last, and emit nothing until the page calls ready().

WHY A TIMEOUT. A page that never calls ready() is indistinguishable from one
still starting, and the failure is silent - the view sits blank. The timeout
turns that into a message naming what did not happen.
"""

import os
import time

from qgis.PyQt.QtCore import QTimer, Qt
from qgis.PyQt.QtGui import QColor

from . import web_assets
from .web_bridge import WebBridge

try:
    from qgis.PyQt.QtWebEngineWidgets import QWebEngineView
    from qgis.PyQt.QtWebChannel import QWebChannel
except ImportError:  # pragma: no cover - build without WebEngine
    QWebEngineView = None
    QWebChannel = None

try:
    from qgis.core import Qgis, QgsMessageLog
except ImportError:  # pragma: no cover
    Qgis = QgsMessageLog = None

# Where the built bundle lives, relative to this file. tools/package.py ships
# it; tools/test_web_bundle.py checks that it is intact and gets into the zip.
BUNDLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# Long enough for a cold Chromium start on a slow machine, short enough that
# a wedged page is reported while the user still remembers asking.
READY_TIMEOUT_MS = 15000


class WebViewError(RuntimeError):
    pass


def _log(message, level=None):
    if QgsMessageLog is None:  # pragma: no cover
        return
    QgsMessageLog.logMessage(str(message), "equis-web",
                             level if level is not None else Qgis.Info)


def available():
    """Whether this build can host the page at all."""
    return (QWebEngineView is not None and QWebChannel is not None
            and web_assets.available())


def bundle_present(path=BUNDLE):
    """Whether the built assets are actually there.

    A plugin installed from a zip built before `npm run build` ran would have
    everything except the page, and the view would fail with a confusing
    404 from our own scheme handler rather than saying what is missing.
    """
    return os.path.isfile(os.path.join(path, web_assets.INDEX))


class LinkedViews(object):
    """A web view wired to the report store.

    `connection_for` is a callable returning the DuckDB connection, passed
    straight to the bridge - see WebBridge for why it is a callable and not a
    connection.
    """

    def __init__(self, connection_for, parent=None, bundle=BUNDLE,
                 on_ready=None, on_brush=None, on_choice=None):
        if not available():
            raise WebViewError(
                "This QGIS build has no QtWebEngine; the linked views need "
                "it. Everything else in the plugin works without it.")
        if not bundle_present(bundle):
            raise WebViewError(
                "The web bundle is missing from %s. It is built with "
                "`cd web && npm ci && npm run build` and is normally "
                "committed." % bundle)

        self._on_ready = on_ready
        # From construction to the page's ready(): Chromium's start plus the
        # bundle's load, the longest silent wait on a first open. Logged so
        # a slow open says where its time went.
        self._built_at = time.monotonic()
        self.ready_seen = False
        self.failed = None

        # 1 and 2: the scheme and its handler, before anything loads.
        web_assets.install(bundle)

        self.view = QWebEngineView(parent)
        # Transparent so the same object can later be laid over the canvas,
        # as the POC does. WA_TranslucentBackground alone is not enough -
        # Chromium composites white unless the page background is cleared too.
        self.view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.view.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.view.setStyleSheet("background: transparent")
        self.view.page().setBackgroundColor(QColor(0, 0, 0, 0))

        # 3: the channel before the load, so the page's script cannot run
        # before the transport it expects exists.
        self.bridge = WebBridge(connection_for, on_ready=self._page_ready,
                                on_brush=on_brush, on_choice=on_choice)
        self.channel = QWebChannel(self.view.page())
        self.channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self.channel)

        # 4: THE VIEW MUST BE VISIBLE. This is not cosmetic.
        #
        # Chromium only fires requestAnimationFrame for a visible surface, and
        # Mosaic's query consolidator schedules every ARROW request with
        # `requestAnimationFrame` (QueryConsolidator.js - JSON goes straight
        # to the queue, arrow waits a frame so compatible queries can be
        # batched). A view that is never shown therefore answers JSON
        # perfectly and hangs every Arrow query forever, with no error: the
        # request sits in `pending` and nothing ever runs `run()`.
        #
        # That is the entire failure this cost a long afternoon to find, and
        # nothing about it points at the view being hidden. In Node the same
        # code falls back to setImmediate and works, which is why it could not
        # be reproduced outside the browser.
        if parent is None:
            # No parent means no layout will show it. Keep it off-screen
            # rather than popping a window over the user's map, but keep it
            # REAL - a zero-sized or fully hidden widget gets no frames.
            self.view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            self.view.resize(640, 480)
        self.view.show()

        # 5: load, then wait for the page to say it is ready.
        self.view.load(web_assets.url())
        QTimer.singleShot(READY_TIMEOUT_MS, self._ready_timeout)

    # ------------------------------------------------------------- lifecycle

    def _page_ready(self):
        self.ready_seen = True
        # Kept, not just logged, so a probe can time "warm-up done" against
        # when the user reached the tab.
        self.ready_at = time.monotonic()
        _log("linked views ready %.1f s after the view was built"
             % (self.ready_at - self._built_at))
        if self._on_ready is not None:
            self._on_ready()

    def _ready_timeout(self):
        if self.ready_seen:
            return
        # Name what did not happen. A blank view with nothing in the log is
        # the single most confusing outcome here, and it has several causes -
        # a scheme that did not register, a bundle that 404'd, a page that
        # threw before connecting - which this at least narrows.
        self.failed = ("The page did not report ready within %d s. Check the "
                       "equis-web log for a page error, and that %s is "
                       "served." % (READY_TIMEOUT_MS // 1000,
                                    web_assets.url().toString()))
        _log(self.failed, None if Qgis is None else Qgis.Warning)

    def draw_histogram(self, scope):
        """Ask the page to draw `scope`. Returns False if it cannot yet.

        THE PAGE DOES NOT ASK FOR ITSELF. app.js builds its state, says
        "connected", and then waits: what to bin is the panel's decision,
        because the chart must show the same rows the MAP shows and only
        the panel knows the filters the layer was built from. Nothing
        called this, which is why the probe produced a connected page with
        an empty chart area - the page was idle, not broken.
        """
        if not self.ready_seen:
            return False
        import json as _json
        # json.dumps, not str(): a chemical can contain an apostrophe
        # (O'Brien) or a quote, and this string is evaluated as JavaScript.
        payload = _json.dumps({k: v for k, v in (scope or {}).items()
                               if v not in (None, "", [])})
        self.view.page().runJavaScript(
            "window.equis && window.equis.histogram(%s)" % payload)
        return True

    def busy(self, message):
        """Show a spinner and `message` in the chart area. False if not ready.

        Before the page is ready the shell's own spinner ("Starting the
        chart engine...") is already showing, so there is nothing to add.
        """
        if not self.ready_seen:
            return False
        import json as _json
        self.view.page().runJavaScript(
            "window.equis && window.equis.busy(%s)" % _json.dumps(message))
        return True

    def draw_scatter(self, options):
        """Ask the page to draw a scatter from a pivot. False if not ready.

        Same contract as draw_histogram: the page draws only what the host
        asks for, and the payload is JSON because it is evaluated as
        JavaScript and analyte names carry quotes.
        """
        if not self.ready_seen:
            return False
        import json as _json
        self.view.page().runJavaScript(
            "window.equis && window.equis.scatter(%s)" % _json.dumps(options))
        return True

    def call(self, name, payload):
        """Call window.equis[name](payload) on the page. False if not ready.

        The Analysis chooser's messages (offer, paired, note, working) go
        through here; analysis.py names them. JSON for the same reason as
        draw_histogram: analyte names carry quotes.
        """
        if not self.ready_seen:
            return False
        import json as _json
        name = _json.dumps(str(name))
        self.view.page().runJavaScript(
            "window.equis && window.equis[%s] && window.equis[%s](%s)"
            % (name, name, _json.dumps(payload)))
        return True

    def close(self):
        """Tear down in the order that avoids a use-after-free.

        The channel holds the bridge and the page holds the channel, so the
        page goes last; deleting the view first has been seen to take the
        channel's objects with it while a queued call was still in flight.
        """
        try:
            if self.channel is not None:
                self.channel.deregisterObject(self.bridge)
        except Exception:  # noqa: BLE001 - teardown must not raise
            pass
        try:
            self.view.page().setWebChannel(None)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.view.setParent(None)
            self.view.deleteLater()
        except Exception:  # noqa: BLE001
            pass
        self.channel = None
