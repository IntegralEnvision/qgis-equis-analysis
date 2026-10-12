"""Transparent web overlay on the QGIS map canvas, showing live time series.

Stage 1 of the linked-views proof of concept (docs/vision-linked-views.md).
It answers the questions that are specific to putting a web view *over* the
canvas, and nothing else:

  1. Does a translucent QWebEngineView composite correctly over the canvas,
     or does Chromium paint an opaque rectangle over the map?
  2. Do charts pushed from Python stay anchored to their locations through
     pan, zoom and resize?
  3. Does toggling mouse transparency on the view's native child really hand
     the mouse back to the map?
  4. Can several charts in one page share one brush, with dim-not-remove?

Those are independent of Mosaic and of DuckDB, which is why they are tested
alone. See MOSAIC below for why this stage stops short of vgplot.

Why an overlay at all: the plugin already draws sparklines, but they are SVG
baked into each feature's symbol. That costs ~1.2 KB per location in the
layer, dirties the GeoPackage, and - the part that matters here - cannot be
brushed, linked or updated without rebuilding the renderer. An overlay chart
is live, linked, and stores nothing.

Target QGIS 4.2+ / Qt6 / PyQt6 only (docs/webengine-support.md).

Run from the QGIS Python console:

    exec(open(r"C:\\path\\to\\equis_qgis\\tools\\mosaic_poc\\overlay.py").read())

    poc = OverlayPoc(iface)                 # demo data, no plugin needed
    poc = OverlayPoc.from_plugin(iface)     # the panel's real loaded report

Then:

    poc.set_interactive(True)    # charts take the mouse; map does not
    poc.set_interactive(False)   # map takes the mouse; charts are display-only
    poc.report()                 # what worked, for pasting back
    poc.brush_test()             # brush WITHOUT the mouse, to isolate input
    poc.diagnose()               # what the page thinks its own state is
    poc.close()

MOSAIC. This stage deliberately hand-writes its SVG instead of loading
vgplot. Mosaic's whole point is a coordinator issuing SQL to a database, and
the plugin has no DuckDB yet - so a "Mosaic PoC" today would be Mosaic over a
fake connector, which proves nothing about the thing we actually want. The
chart layer is isolated behind the small page contract documented in
overlay.html so that swapping it for vgplot is a contained change once the
substrate exists.
"""

import json
import math
import random

from qgis.core import Qgis, QgsMessageLog, QgsPointXY
from qgis.PyQt.QtCore import QEvent, QObject, QTimer, QUrl, Qt, pyqtSignal, pyqtSlot
from qgis.PyQt.QtGui import QColor

# QGIS 4.2+ ships these; a missing binding is a broken install, not a
# platform we support, so fail loudly rather than with a blank rectangle.
try:
    from qgis.PyQt.QtWebChannel import QWebChannel
    from qgis.PyQt.QtWebEngineWidgets import QWebEngineView
except ImportError as exc:  # pragma: no cover - depends on the QGIS build
    raise ImportError(
        "PyQt6 WebEngine bindings are missing from this QGIS build. This "
        "proof of concept targets QGIS 4.2+, where they are present; see "
        "docs/webengine-support.md and tools/test_webengine.py."
    ) from exc

import os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HTML = os.path.join(HERE, "overlay.html")

# A chart per location stops being readable long before it stops being
# possible. Only locations inside the viewport are drawn, and only this many
# of them - the rest are reported as a count rather than silently dropped.
MAX_CHARTS = 12


def _isoformat(value):
    """Dates over the channel as ISO strings; JS parses them, Python does not
    have to guess a locale, and the JSON stays readable in a log."""
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def series_payload(location, chemical, points, unit=""):
    """One location's series, in the shape the page expects.

    Mirrors `timeseries.TimeSeriesPlot.plot`'s reading of a point exactly: a
    value plotted at a detection limit is a non-detect whatever the flag says,
    which is the rule `_is_detected` encodes there. Keeping the two in step
    matters more than it looks - the overlay and the dock plot are two views
    of one series, and a point drawn solid in one and hollow in the other
    would be the plugin contradicting itself on screen.

    No QGIS imports are used here, so it is exercised by
    tools/test_overlay_payload.py under a plain interpreter.
    """
    out = []
    for point in points or []:
        at_limit = bool(point.get("at_limit"))
        detected = (not at_limit) and str(
            point.get("detect", "")).upper() in ("Y", "YES", "TRUE", "1")
        out.append({
            "t": _isoformat(point.get("date")),
            "v": point.get("value"),
            "detected": detected,
        })
    return {
        "id": location,
        "name": location,
        "chemical": chemical,
        "unit": unit or next(
            (p.get("unit") for p in (points or []) if p.get("unit")), ""),
        "points": out,
    }


def make_demo_series(n_locations=8, n_points=40, seed=7):
    """Fake data so the overlay can be tested with no EQuIS connection.

    Deliberately shaped like real chemistry: a log-normal spread, a slow
    downward trend, and a run of non-detects once the trend drops - which is
    what makes the detect/non-detect drawing worth looking at.
    """
    import datetime

    rng = random.Random(seed)
    start = datetime.date(2008, 1, 1)
    out = []
    for index in range(n_locations):
        base = rng.uniform(1.4, 2.8)
        limit = 10 ** (base - 1.1)
        points = []
        for step in range(n_points):
            when = start + datetime.timedelta(days=int(step * 150))
            # The trend must actually CROSS the limit, or the demo contains no
            # non-detects and the hollow-marker drawing is never exercised -
            # which is what test_overlay_payload.py caught when this decayed
            # by only 0.9 decades against a limit 1.1 decades down. A plume
            # attenuating below the reporting limit is also the realistic
            # shape, so the fix and the realism agree.
            drift = -1.8 * (step / float(max(n_points - 1, 1)))
            value = 10 ** rng.gauss(base + drift, 0.22)
            detected = value > limit
            points.append({
                "date": when,
                "value": round(value if detected else limit, 3),
                "unit": "ug/L",
                "detect": "Y" if detected else "N",
                "at_limit": not detected,
            })
        out.append({
            "payload": series_payload(
                "DEMO-%02d" % index, "Trichloroethene", points, unit="ug/L"),
            "fx": rng.uniform(0.15, 0.85),
            "fy": rng.uniform(0.15, 0.85),
        })
    return out


class Bridge(QObject):
    """Exposed to the page as `bridge`."""

    dataset = pyqtSignal(str)     # JSON: [series payload, ...]
    positions = pyqtSignal(str)   # JSON: [{id, x, y, visible}, ...] canvas px

    def __init__(self, on_ready, on_brush=None):
        super().__init__()
        self._on_ready = on_ready
        self._on_brush = on_brush
        self.ready_seen = False

    @pyqtSlot()
    def ready(self):
        """JS says it has built its state and connected to our signals.

        NOT loadFinished, which fires on DOM parse - emitting a signal then
        delivers it into the void, because the page has not connected to it
        yet. tools/test_webengine.py reported a working channel as broken for
        exactly this reason before it grew this handshake.
        """
        self.ready_seen = True
        self._on_ready()

    @pyqtSlot(str)
    def brushed(self, payload):
        """The page's shared brush changed. JSON: {from, to, count} or null."""
        if self._on_brush is not None:
            self._on_brush(payload)

    @pyqtSlot(str)
    def log(self, message):
        QgsMessageLog.logMessage(str(message), "equis-overlay", Qgis.Info)


class OverlayPoc(QObject):
    """A translucent QWebEngineView pinned over the map canvas."""

    def __init__(self, iface, series=None, html_path=None):
        super().__init__()
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.html_path = html_path or DEFAULT_HTML
        self.events = []          # what happened, for report()
        self.last_brush = None

        if not os.path.exists(self.html_path):
            raise IOError("overlay.html not found beside overlay.py: %s"
                          % self.html_path)

        self.series = series if series is not None else self._demo_at_extent()
        self._note("%d series to draw" % len(self.series))

        viewport = self.canvas.viewport()
        self.view = QWebEngineView(viewport)
        # The three that decide question 1. WA_TranslucentBackground alone is
        # not enough: the page also has to declare a transparent background
        # colour, or Chromium composites white underneath it.
        self.view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.view.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.view.setStyleSheet("background: transparent")
        self.view.page().setBackgroundColor(QColor(0, 0, 0, 0))
        self.view.setGeometry(viewport.rect())
        self.view.show()
        self.view.raise_()

        self.channel = QWebChannel(self.view.page())
        self.bridge = Bridge(on_ready=self._on_page_ready,
                             on_brush=self._on_brush)
        self.channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self.channel)

        # Anchoring: both of these move the charts, and both must be caught or
        # the charts drift away from their locations.
        self.canvas.extentsChanged.connect(self.push_positions)
        viewport.installEventFilter(self)

        self._interactive = False
        self.set_interactive(False)
        self.view.load(QUrl.fromLocalFile(self.html_path))

        # A page that never calls ready() is the failure worth naming.
        QTimer.singleShot(8000, self._ready_timeout)

    # ------------------------------------------------------- construction

    @classmethod
    def from_plugin(cls, iface, chemical=None, max_locations=MAX_CHARTS,
                    html_path=None):
        """Build from the panel's currently loaded report.

        Reaches into the running plugin rather than re-fetching: the report is
        already in memory and running one again can take minutes.
        """
        panel = cls._find_panel()
        if panel is None:
            raise RuntimeError(
                "The EQuIS panel is not open, or has no report loaded. Open "
                "it and load a report first, or use OverlayPoc(iface) for "
                "demo data.")
        table = getattr(panel, "_report_table", None)
        if table is None:
            raise RuntimeError("The panel has no report loaded yet.")

        if chemical is None:
            analytes = table.analytes()
            if not analytes:
                raise RuntimeError("The loaded report lists no chemicals.")
            first = analytes[0]
            chemical = first[0] if isinstance(first, (list, tuple)) else first
            if isinstance(chemical, dict):
                chemical = chemical.get("chemical")

        locations = list(table.locations())[:max_locations]
        layer = getattr(panel, "_locations_layer", None)
        coords = cls._location_coords(layer)

        series = []
        for code in locations:
            points = table.series(code, chemical)
            if not points:
                continue
            entry = {"payload": series_payload(code, chemical, points)}
            if code.upper() in coords:
                entry["map_xy"] = coords[code.upper()]
            series.append(entry)
        if not series:
            raise RuntimeError(
                "No location in the loaded report has a plottable series for "
                "%r." % chemical)
        return cls(iface, series=series, html_path=html_path)

    @staticmethod
    def _find_panel():
        """The plugin's dock, however it was registered."""
        from qgis.PyQt.QtWidgets import QApplication
        for widget in QApplication.allWidgets():
            if hasattr(widget, "_report_table") and hasattr(widget, "_offline"):
                return widget
        return None

    @staticmethod
    def _location_coords(layer):
        """SYS_LOC_CODE -> QgsPointXY from the plugin's locations layer.

        Case-insensitive, because the report and the feature service do not
        always agree on case - the same reason the plugin's own join does it.
        """
        out = {}
        if layer is None:
            return out
        try:
            fields = [f.name() for f in layer.fields()]
        except Exception:
            return out
        key = next((n for n in fields if n.lower() in
                    ("code", "sys_loc_code", "loc_code")), None)
        if key is None:
            return out
        for feature in layer.getFeatures():
            geom = feature.geometry()
            if geom is None or geom.isEmpty():
                continue
            value = feature[key]
            if value:
                out[str(value).upper()] = geom.asPoint()
        return out

    def _demo_at_extent(self):
        """Scatter demo series across whatever the canvas is showing now."""
        extent = self.canvas.extent()
        out = []
        for entry in make_demo_series():
            out.append({
                "payload": entry["payload"],
                "map_xy": QgsPointXY(
                    extent.xMinimum() + entry["fx"] * extent.width(),
                    extent.yMinimum() + entry["fy"] * extent.height()),
            })
        return out

    # ------------------------------------------------------------ signals

    def _on_page_ready(self):
        self._note("page called ready()")
        self.bridge.dataset.emit(
            json.dumps([entry["payload"] for entry in self.series]))
        self.push_positions()
        # focusProxy() - Chromium's native child - does not exist until the
        # page is up, so the mouse attribute has to be re-applied here.
        self.set_interactive(self._interactive)

    def _ready_timeout(self):
        if not self.bridge.ready_seen:
            self._note("FAIL: page never called ready() within 8 s")

    def _on_brush(self, payload):
        try:
            self.last_brush = json.loads(payload) if payload else None
        except ValueError:
            self.last_brush = None
        self._note("brush -> %s" % (self.last_brush or "cleared"))

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Resize:
            self.view.setGeometry(obj.rect())
            self.push_positions()
        return False

    def push_positions(self):
        """Map coordinates -> canvas pixels, for every series."""
        transform = self.canvas.getCoordinateTransform()
        rect = self.canvas.viewport().rect()
        out = []
        for entry in self.series:
            point = entry.get("map_xy")
            if point is None:
                continue
            pixel = transform.transform(point)
            x, y = pixel.x(), pixel.y()
            # Off-screen charts are told to hide rather than being dropped, so
            # the page keeps their state and a pan back reveals them intact.
            visible = (-200 <= x <= rect.width() + 200
                       and -200 <= y <= rect.height() + 200)
            if math.isnan(x) or math.isnan(y):
                continue
            out.append({"id": entry["payload"]["id"], "x": x, "y": y,
                        "visible": visible})
        self.bridge.positions.emit(json.dumps(out))

    # -------------------------------------------------------------- mouse

    def set_interactive(self, interactive):
        """True: charts take the mouse. False: the map does, charts display only.

        The attribute has to go on the native child Chromium paints into, not
        only on the QWebEngineView - focusProxy() is that child, and setting it
        on the view alone leaves the map unclickable under the overlay.
        """
        self._interactive = bool(interactive)
        transparent = not self._interactive
        self.view.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, transparent)
        child = self.view.focusProxy()
        if child is not None:
            child.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, transparent)
        else:
            self._note("focusProxy() was None - mouse toggle may not apply")
        self.view.page().runJavaScript(
            "window.setInteractive && window.setInteractive(%s)"
            % ("true" if self._interactive else "false"))
        self._note("interactive=%s" % self._interactive)
        return self._interactive

    # -------------------------------------------------------- diagnostics

    def diagnose(self):
        """Ask the page what it thinks its own state is.

        Printed asynchronously, because runJavaScript returns through a
        callback. The fields that matter when a brush does nothing:

          bodyPointerEvents / svgPointerEvents  - "none" means the DOM is
              refusing the mouse regardless of what Qt does
          input.pointerdowns  - 0 means no click ever reached a chart, so the
              fault is upstream of the brush code entirely
          input.pointermoves  - downs without moves means the drag was
              claimed by something else after it started
          input.captured      - false means setPointerCapture was refused and
              the window fallback is carrying the drag
        """
        def got(raw):
            print("[overlay] diagnose: %s" % raw)
        self.view.page().runJavaScript("window.diagnose && window.diagnose()",
                                       got)

    def brush_test(self, lo=0.3, hi=0.7):
        """Set a brush from Python, with no mouse involved.

        Splits the remaining unknown in two. If the charts dim when this runs,
        drawing works and only input is at fault; if they do not, input was
        never the problem and the fault is downstream.
        """
        times = [p["t"] for entry in self.series
                 for p in entry["payload"]["points"]]
        if not times:
            print("[overlay] brush_test: no points to brush")
            return
        import datetime

        def epoch_ms(text):
            text = text.replace("Z", "+00:00")
            try:
                when = datetime.datetime.fromisoformat(text)
            except ValueError:
                when = datetime.datetime.fromisoformat(text[:19])
            if when.tzinfo is None:
                when = when.replace(tzinfo=datetime.timezone.utc)
            return when.timestamp() * 1000.0

        stamps = sorted(epoch_ms(t) for t in times)
        low, high = stamps[0], stamps[-1]
        span = high - low
        a, b = low + span * lo, low + span * hi

        def got(raw):
            print("[overlay] brush_test applied: %s" % raw)
            print("[overlay] LOOK AT THE MAP: are out-of-range points dimmed?")
        self.view.page().runJavaScript(
            "window.applyBrush && window.applyBrush(%f, %f)" % (a, b), got)

    def clear_brush(self):
        self.view.page().runJavaScript(
            "window.applyBrush && window.applyBrush(null, null)")

    # ------------------------------------------------------------ results

    def _note(self, message):
        self.events.append(message)
        print("[overlay] %s" % message)

    def report(self):
        """What happened, for pasting back."""
        print("")
        print("=" * 64)
        print("OVERLAY POC")
        print("=" * 64)
        for message in self.events:
            print("  %s" % message)
        print("")
        print("  Answer these by looking at the map:")
        print("   1. compositing  - is the map visible THROUGH the overlay,")
        print("                     or is there a white/grey rectangle?")
        print("   2. anchoring    - pan and zoom: do charts follow their")
        print("                     locations, or drift/lag?")
        print("   3. mouse        - set_interactive(False): can you pan the")
        print("                     map by dragging over a chart?")
        print("                     set_interactive(True): does dragging on a")
        print("                     chart brush it instead?")
        print("   4. shared brush - brushing one chart: do the others dim")
        print("                     their out-of-range points too?")
        print("")
        print("  ready=%s  brush=%s  series=%d"
              % (self.bridge.ready_seen, self.last_brush, len(self.series)))
        print("")
        print("  If 4 does not respond:  poc.brush_test()  then  poc.diagnose()")
        print("  brush_test bypasses the mouse, so it separates a drawing")
        print("  fault from an input one.")

    def close(self):
        try:
            self.canvas.extentsChanged.disconnect(self.push_positions)
        except Exception:
            pass
        try:
            self.canvas.viewport().removeEventFilter(self)
        except Exception:
            pass
        self.view.setParent(None)
        self.view.deleteLater()
        self._note("closed")


print("overlay.py loaded. Try:  poc = OverlayPoc(iface)"
      "   or   poc = OverlayPoc.from_plugin(iface)")
