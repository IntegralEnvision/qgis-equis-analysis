"""The thing that makes US-1 a feature rather than a set of parts.

Everything it needs already exists and is tested alone: the store answers
SQL, the bridge carries it, the page draws a histogram, and brush_layer
highlights a map without touching the base. This is the twenty lines of
policy that connect them, kept out of dialog.py so the panel gains one
object rather than a web view's worth of state.

WHAT DECIDES WHAT. The page owns the chart: bins, marks, the brush's shape.
The MAP owns what a selection means - brush_layer's two-layer arrangement
was chosen by measurement (2.1 s for selectByIds against 49.8 ms for a
highlight layer at 250,000 points), and the page has no business knowing
that. So the page sends a range and the locations inside it; Python decides
how they are drawn.

WHY LOCATIONS AND NOT FEATURE IDS. The chart queries the store, which knows
locations; the map holds features, which know feature ids. Nothing maps one
to the other except the layer itself, and that mapping changes whenever a
layer is rebuilt. Sending ids would make the page's answer stale the moment
the map changed under it.
"""

import time

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtCore import QCoreApplication, QTimer

from . import web_view
from .brush_layer import BrushLayer
from . import bivariate
from .analysis import Session, scatter_options, column_meta
from .pivot import build_pivot
from .store import DETECT_TRUE

LOG_TAG = "equis-web"

# The field a result layer carries its location code in. dialog.py's
# LOCATION_FIELD, repeated here rather than imported: this module must not
# depend on the panel, which imports it.
LOCATION_FIELD = "code"


def _log(message, level=None):
    QgsMessageLog.logMessage(str(message), LOG_TAG,
                             level if level is not None else Qgis.Info)


class LinkedViewsController(object):
    """Owns the web view, the brush layer, and the link between them.

    `connection_for` and `layer_for` are callables, not values, for the same
    reason WebBridge takes one: the panel rebuilds its store on every fetch
    and its layer on every map, and anything holding either would keep
    answering from data that is no longer on the screen.
    """

    def __init__(self, connection_for, layer_for, on_status=None,
                 store_for=None, on_scatter_map=None, locations_for=None,
                 matrix_for=None):
        self._connection_for = connection_for
        self._layer_for = layer_for
        self._on_status = on_status
        # The scatter builds a pivot, which needs the STORE (its columns and
        # predicates), not just a connection. A callable for the same reason
        # as the others: the store is rebuilt on every fetch.
        self._store_for = store_for
        # Which chart the page is showing, so a map selection redraws the
        # right one: "histogram", "scatter" or None.
        self.showing = None
        self._scatter_options = None
        # The scatter's own map layer (bivariate.py), built by the panel
        # through this callback and handed back with set_scatter_layer. The
        # brush highlights IT while a scatter shows: the result layer is a
        # different variable, and highlighting stations on it said nothing
        # about the two analytes being brushed.
        self._on_scatter_map = on_scatter_map
        self.scatter_layer = None
        # The Analysis tab's chooser (analysis.py): a Session per store, the
        # map's selection to dim a new scatter to, and the panel's matrix to
        # start from. Callables, as above.
        self._locations_for = locations_for
        self._matrix_for = matrix_for
        self.session = None
        self._offer_wanted = False
        self._task = None
        self._queued = None
        # The map's time window (QGIS Temporal Controller), as (start, end)
        # dates or None, and whether a re-pick for it is owed. See
        # set_window.
        self.window = None
        self._window_owed = False
        self._window_timer = None
        self.views = None
        self.brush = None
        self._brush_layer_for = None
        self._by_code = {}
        self._index_for = None
        # The view laid over the map canvas (map_overlay.py), or None, and
        # what the page is told when it (re)loads while over the map.
        self.overlay = None
        self._overlay_layout = None
        self._overlay_ready = None

    # ------------------------------------------------------------- lifecycle

    def available(self):
        return web_view.available() and web_view.bundle_present()

    def open(self, parent=None):
        """Build the view. Returns it, or raises WebViewError."""
        if self.views is not None:
            return self.views
        self.views = web_view.LinkedViews(
            self._connection_for, parent=parent, on_brush=self._on_brush,
            on_ready=self._page_ready, on_choice=self._on_choice)
        return self.views

    def clear_brush(self):
        """Drop any highlight. Public because the panel needs it.

        A highlight belongs to the population the chart was showing. When
        the chart is redrawn for a different analyte or a changed filter,
        leaving it would put one analyte's selection on another's map -
        which the user would read as data.
        """
        self._on_brush(None)

    def draw(self, scope):
        """Draw `scope` on the page once it is ready.

        The page says "connected" and then WAITS: what to bin is the
        panel's decision, because the chart must bin the same rows the map
        is showing. Nothing called this, which is why the first live probe
        produced a connected page above an empty chart area.

        Retries once the page reports ready, so a draw asked for during the
        two-second Chromium start is not simply lost.
        """
        self._pending = ("histogram", scope)
        self.showing = "histogram"
        views = self.views
        if views is None:
            return False
        if views.draw_histogram(scope):
            return True
        # Not ready yet. web_view calls back on ready, and that callback
        # draws whatever was last asked for.
        return False

    def _page_ready(self):
        if self.session is not None:
            self.session.page_reloaded()
        # Over the map before the page had loaded: it was told nothing then.
        self._tell_overlay()
        if self._offer_wanted:
            # The chooser was asked for while the page was starting. Its
            # answer redraws any chosen pair, so nothing else is owed.
            self.offer()
            return
        kind, payload = getattr(self, "_pending", (None, None))
        if not payload or self.views is None:
            return
        if kind == "scatter":
            self.views.draw_scatter(payload)
        else:
            self.views.draw_histogram(payload)

    # --------------------------------------------------------------- scatter

    def draw_scatter(self, x, y, matrix=None, locations=None):
        """Pivot two analytes and draw them, one point per sample (US-24).

        `x` and `y` are pivot.Variable. Returns the Pivot, whose counts the
        caller may report; raises pivot.PivotError when the pivot would be
        dishonest (mixed units, dissolved beside total, no sample codes) -
        the caller shows the message, which says what to change.
        """
        store = self._store_for() if self._store_for is not None else None
        if store is None:
            raise ValueError("the scatter reads the DuckDB store, which is not "
                             "ready - fetch or open data first")
        started = time.monotonic()
        timings = {}

        def step(message, done=None):
            # Each step runs on the UI thread, so say what is next and let
            # the window repaint BEFORE starting it - otherwise the message
            # appears only after the work it announces (asked 2026-10-09:
            # "like 10 seconds for anything to show up").
            if done:
                timings[done] = round(time.monotonic() - started, 2)
            if message:
                self._status(message)
                if self.views is not None:
                    self.views.busy(message)
                QCoreApplication.processEvents()

        step("Pivoting %s and %s: one row per sample..." % (x.chemical, y.chemical))
        pivot = build_pivot(store, [x, y], matrix=matrix)
        if pivot.empty:
            raise ValueError("no data for %s in this scope"
                             % ", ".join(v.chemical for v in pivot.empty))
        if len(pivot.columns) != 2:
            # Two analytes, unsplit: exactly two column groups. Anything else
            # is a bug in the request, not something to draw around.
            raise ValueError("expected two column groups, got %d"
                             % len(pivot.columns))
        first, second = pivot.columns
        step("Classing %s samples (Jenks breaks)..." % format(pivot.complete, ",d"),
             done="pivot")
        options, breaks = scatter_options(store, pivot, first, second, matrix,
                                          locations)
        stations = None
        if self._on_scatter_map is not None:
            step("Building the scatter map...", done="breaks")
            stations = bivariate.stations(store.con, options["pivot"],
                                          first["prefix"], second["prefix"],
                                          list(DETECT_TRUE))
        self._show_scatter(options, breaks, stations, first, second,
                           pivot.complete)
        timings["shown"] = round(time.monotonic() - started, 2)
        _log("scatter host steps (s since asked): %s"
             % " ".join("%s=%s" % kv for kv in timings.items()))
        return pivot

    def _show_scatter(self, options, breaks, stations, first, second,
                      samples=None, third=None):
        """Make a scatter the one showing: its map layer, then the draw.

        Shared by the console's draw_scatter and the Analysis chooser, so a
        scatter reached either way brushes, dims and redraws the same.
        """
        self._scatter_options = options
        self._pending = ("scatter", options)
        self.showing = "scatter"
        self.clear_brush()
        if self._on_scatter_map is not None and stations is not None:
            # A ternary's third analyte goes too: its map is coloured by
            # composition (ternary.py), not by the scatter's 3x3 classes.
            self._on_scatter_map(stations, breaks, column_meta(first),
                                 column_meta(second),
                                 (options.get("points") or {}).get("map"),
                                 column_meta(third) if third else None)
        if self.views is not None:
            names = [c["chemical"] for c in (first, second, third) if c]
            self.views.busy(
                "Drawing %s samples..." % format(samples, ",d")
                if samples is not None else "Drawing %s..."
                % (" : ".join(names) if third else " x ".join(names)))
            self.views.draw_scatter(options)

    # ------------------------------------------------------- the chooser

    def _session(self):
        """The chooser's Session for the CURRENT store, or None.

        A new store (a fetch, a parquet opened) gets a new Session, which
        keeps the last choice: the same analytes, re-pivoted from the new
        data, rather than a chooser that forgets on every reload.
        """
        store = self._store_for() if self._store_for is not None else None
        if store is None:
            return None
        if self.session is None or self.session.store is not store:
            kept = self.session.choice if self.session is not None else {}
            self.session = Session(store)
            self.session.choice = dict(kept)
            self.session.window = self.window
        return self.session

    # ------------------------------------------------------ the time window

    # Long enough that a Temporal Controller animation's frames collapse
    # into one re-pick, short enough to feel attached to the slider.
    WINDOW_SETTLE_MS = 150

    def set_window(self, window):
        """The map's time window moved: re-pick the scatter's points in it.

        Decided 2026-10-12: samples outside the window are DIMMED, the axes
        and the colour breaks fixed, and "most recent"/"highest" are taken
        within the window. Only the solid samples and the map's stations
        are re-picked (Session.repoint), in the worker; a burst of changes
        (an animation) settles into one re-pick for the latest.
        """
        if window == self.window:
            return
        self.window = window
        if self._window_timer is None:
            self._window_timer = QTimer()
            self._window_timer.setSingleShot(True)
            self._window_timer.timeout.connect(self._apply_window)
        self._window_timer.start(self.WINDOW_SETTLE_MS)

    def _apply_window(self):
        if self._task is not None:
            self._window_owed = True       # after the task in flight
            return
        session = self._session()
        if session is None:
            return
        if session.current is None or self.showing != "scatter":
            # Nothing drawn to re-pick: the next scatter starts in it.
            session.window = self.window
            return
        self._start(session, None, window=self.window)

    def offer(self):
        """Give the page's chooser what it needs, if it has not got it.

        Called whenever the Analysis tab is shown or the data under it
        changed; costs nothing when the page's catalogue is current. Before
        the page is ready, or while a choice is being answered, remembered
        and done after.
        """
        if (self.views is None or not self.views.ready_seen
                or self._task is not None):
            self._offer_wanted = True
            return False
        self._offer_wanted = False
        session = self._session()
        if session is None:
            self.views.call("note", "No data loaded: fetch or open data first.")
            return False
        if not session.needs_offer():
            return False
        choice = dict(session.choice)
        if not choice.get("matrix") and self._matrix_for is not None:
            choice["matrix"] = self._matrix_for()
        return self._on_choice(choice)

    def _on_choice(self, choice):
        """The page chose (or offer() chose for it). Called from the bridge.

        IN A WORKER (tasks.AnalysisTask): on this thread a water pivot froze
        all of QGIS for 3-4 s. One task at a time, on the Session's cursor;
        a choice made while one runs waits, and only the LATEST waits - a
        user clicking through X values wants the last one drawn, not each.
        """
        if self._task is not None:
            self._queued = choice
            return True
        session = self._session()
        if session is None or self.views is None:
            return False
        session.window = self.window
        return self._start(session, choice)

    def _start(self, session, choice, window=False):
        """Run one AnalysisTask: a choice, or (window given) a re-pick."""
        from qgis.core import QgsApplication
        from .tasks import AnalysisTask

        locations = (self._locations_for() if self._locations_for is not None
                     else None)
        task = AnalysisTask(session, choice, locations, window=window)
        task.message.connect(self._task_message)
        task.finishedOk.connect(lambda found, t=task: self._task_over(t, found))
        task.failed.connect(lambda text, t=task: self._task_over(t, None, text))
        task.cancelledByUser.connect(
            lambda t=task: self._task_over(t, None, "Cancelled."))
        # Held here as well as by the task manager: a QgsTask whose Python
        # object is collected mid-run takes its signals with it.
        self._task = task
        QgsApplication.taskManager().addTask(task)
        return True

    def _task_message(self, name, payload):
        """Something Session.respond said or sent, now on the main thread."""
        if self.views is None:
            return
        if name == "working":
            self._status((payload or {}).get("message") or "")
        self.views.call(name, payload)

    def _task_over(self, task, found, error=None):
        if task is not self._task:
            return
        self._task = None
        stale = self.session is None or task.session is not self.session
        if error and not stale:
            # The page asked, so the page is told - not only the status bar.
            if self.views is not None:
                self.views.call("note", error)
            self._status(error)
        elif not stale:
            self._status("")
            if found is not None:
                options = found.options
                if task.window is not False:
                    # A re-pick reuses the drawn scatter's options, whose
                    # map selection may be older than the map's.
                    options = dict(options)
                    options.pop("locations", None)
                    if task.locations is not None:
                        options["locations"] = list(task.locations)
                # Here, not in the worker: the map layer is QGIS's, and is
                # built on the main thread.
                self._show_scatter(options, found.breaks, found.stations,
                                   found.first, found.second,
                                   third=(found.columns[2]
                                          if len(found.columns) > 2 else None))
        queued, self._queued = self._queued, None
        if queued is not None:
            self._on_choice(queued)
        elif self._window_owed:
            self._window_owed = False
            self._apply_window()
        elif self._offer_wanted or stale:
            # The data changed while the task ran (a new store, a new row
            # scope): offer() pays only if the catalogue is now out of date.
            self.offer()

    def set_scatter_layer(self, layer):
        """The panel built the scatter's map layer: brush onto it from now on."""
        if self.brush is not None:
            self.brush.remove()
            self.brush = None
            self._brush_layer_for = None
        self.scatter_layer = layer
        self._index_for = None
        self.prepare()

    def refresh_scatter_locations(self, locations):
        """The map's selection changed: dim the scatter to it, in place.

        Not a redraw. The page puts the selection on the linked views'
        crossfilter (linked.js, setMapSelection) and only the solid marks
        re-query: no spinner, the axes stay put (US-24 criterion 4), and a
        brush is KEPT and composes with it (criterion 5). Until 2026-10-29
        this redrew the whole page and cleared the brush, so every
        selection flickered the charts off and back. The options keep the
        selection too, so a later full draw (a new window, a reload) starts
        from it. None is "no selection"; [] is "zero locations".
        """
        if self.showing != "scatter" or not self._scatter_options:
            return False
        options = dict(self._scatter_options)
        if locations is None:
            options.pop("locations", None)
        else:
            options["locations"] = list(locations)
        if options == getattr(self, "_pending", (None, None))[1]:
            return False
        self._scatter_options = options
        self._pending = ("scatter", options)
        if self.views is not None:
            return self.views.call("mapSelection", options.get("locations"))
        return False

    # --------------------------------------------------------------- overlay

    def float_over(self, canvas, on_asked=None, on_remember=None, layout=None,
                   on_ready=None, covers=()):
        """Move the view onto `canvas`, as panels against its edges. True if done.

        `on_asked(what)` receives the page's requests - "dock", "data",
        "hide" - because the panel owns where the view lives and what is
        shown. `layout` is where the panels were last put (the panel keeps
        it in QSettings through `on_remember`); `on_ready` runs whenever the
        page (re)loads over the map, so it is never left without its data.
        """
        views = self.views
        if views is None or canvas is None:
            return False
        if self.overlay is not None:
            return True
        from .map_overlay import MapOverlay
        self.overlay = MapOverlay(
            views.view, canvas, covers=covers,
            on_keep_out=lambda rects: self.views is not None
            and self.views.call("keepOut", rects))
        self._overlay_layout = layout
        self._overlay_ready = on_ready
        views.bridge.on_cards = self.overlay.set_cards
        views.bridge.on_asked = on_asked
        views.bridge.on_remember = on_remember
        self.overlay.attach()
        self._tell_overlay()
        return True

    def _tell_overlay(self):
        if self.overlay is None or self.views is None:
            return
        self.views.call("overlay", {"on": True, "layout": self._overlay_layout})
        self.overlay.report_keep_out()
        if self._overlay_ready is not None:
            self._overlay_ready()

    def set_overlay_visible(self, visible):
        if self.overlay is not None:
            self.overlay.set_visible(visible)

    @property
    def overlay_visible(self):
        return self.overlay is not None and self.overlay.visible

    def sink(self):
        """Take the view off the map. Returns it for the panel to re-seat."""
        views = self.views
        if self.overlay is not None:
            self.overlay.detach()
            self.overlay = None
        if views is None:
            return None
        views.bridge.on_cards = None
        views.bridge.on_asked = None
        views.bridge.on_remember = None
        self._overlay_ready = None
        views.view.setVisible(True)
        views.call("overlay", False)
        return views.view

    def close(self):
        if self.overlay is not None:
            self.overlay.detach()
            self.overlay = None
        if self._task is not None:
            # Interrupts its query; the store it reads is about to close.
            self._task.cancel()
            self._task = None
        if self.brush is not None:
            self.brush.remove()
            self.brush = None
            self._brush_layer_for = None
        if self.views is not None:
            self.views.close()
            self.views = None

    def prepare(self):
        """Index the current layer now, rather than on the first brush.

        The index costs O(dataset) once - 1.4 s at 250,000 features - and
        every brush after it costs O(selection), 10 ms for 5,000. Left to
        happen lazily that whole cost lands on the user's first drag, which
        is the one moment US-1 budgets at 100 ms. Called when a layer is
        built, where a second of work is already expected.

        Safe to call repeatedly: it is a no-op once the index matches.
        """
        self._features_for(())

    # ----------------------------------------------------------------- brush

    def _target_layer(self):
        """What a brush highlights: the scatter's own layer while a scatter
        shows, otherwise the panel's result layer."""
        if self.showing == "scatter" and self.scatter_layer is not None:
            try:
                self.scatter_layer.id()
                return self.scatter_layer
            except RuntimeError:            # deleted by the user
                self.scatter_layer = None
        return self._layer_for()

    def _highlight_for(self):
        """The BrushLayer for the layer currently on the map.

        Rebuilt when the panel's layer changes. A highlight belonging to a
        removed layer would draw over whatever took its place, which reads
        as the brush selecting the wrong points rather than as a stale
        object.
        """
        layer = self._target_layer()
        if layer is None:
            return None
        if self.brush is not None and self._brush_layer_for == layer.id():
            return self.brush
        if self.brush is not None:
            self.brush.remove()
        self.brush = BrushLayer(layer)
        self._brush_layer_for = layer.id()
        return self.brush

    def _features_for(self, wanted):
        """The layer's features for a set of location codes.

        INDEXED ONCE PER LAYER, not once per brush. Scanning the layer on
        every brush costs O(dataset) where the two-layer arrangement exists
        precisely to cost O(selection): measured at 250,000 features with
        5,000 brushed, the scan alone was 582 ms of an 858 ms handler,
        against a 100 ms budget.

        A provider-side expression is far worse, not better - the same case
        took 151 SECONDS with an `IN (...)` filter of 5,000 codes, because
        the memory provider evaluates it per feature per term. Hence a plain
        Python dict, built once, which turns each later brush into a
        dictionary lookup per selected code.

        Returns None when the layer cannot be matched at all, which the
        caller reports rather than silently drawing nothing.
        """
        layer = self._target_layer()
        if layer is None:
            return None
        if self._index_for != (layer.id(), layer.featureCount()):
            field = layer.fields().indexOf(LOCATION_FIELD)
            if field < 0:
                _log("the layer has no %r field; the brush cannot be mapped"
                     % LOCATION_FIELD, Qgis.Warning)
                self._by_code = {}
                self._index_for = (layer.id(), layer.featureCount())
                return None
            index = {}
            for feature in layer.getFeatures():
                # One location can carry several features - an unaggregated
                # layer has a point per sample - so every one is kept.
                index.setdefault(feature[field], []).append(feature)
            self._by_code = index
            # Keyed on the feature count too: a layer edited in place keeps
            # its id, and a stale index would highlight points that are no
            # longer there.
            self._index_for = (layer.id(), layer.featureCount())
        picked = []
        for code in wanted:
            picked.extend(self._by_code.get(code, ()))
        return picked

    def _on_brush(self, brush):
        """The page brushed, or cleared. Called from the bridge."""
        highlight = self._highlight_for()
        if highlight is None:
            # SAY SO. A brush that changes nothing on the map looks like a
            # broken link; the real cause is that no layer is mapped yet.
            if brush:
                self._status((brush.get("label") or "")
                             + "  No map layer to highlight: map an analyte "
                               "to see these locations.")
            return
        if not brush:
            highlight.clear()
            self._status("")
            return

        wanted = set(brush.get("locations") or [])
        if not wanted:
            highlight.clear()
            self._status(brush.get("label") or "")
            return

        picked = self._features_for(wanted)
        if picked is None:
            return
        highlight.show(picked)

        message = brush.get("label") or ""
        advice = highlight.advice()
        if advice:
            # A brush too wide to read says so rather than rendering a slow,
            # unreadable frame. See brush_layer.READABLE_HIGHLIGHT.
            message = (message + "  " + advice).strip()
        self._status(message)

    def _status(self, message):
        if self._on_status is not None:
            self._on_status(message)
