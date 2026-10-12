r"""The Analysis chooser's worker, in QGIS's task manager. NEEDS QGIS.

    "C:\Program Files\QGIS 4.2.1\bin\python-qgis.bat" tools\test_analysis_task.py

Drives the real LinkedViewsController and tasks.AnalysisTask against the
Facility A parquet, with the web view replaced by a recorder (the view is not
what is under test, and QtWebEngine needs a desktop). What it proves:

  * a choice is answered OFF the main thread: the main thread keeps
    ticking through water's every-analyte pivot (it froze all of QGIS for
    3-4 s on the UI thread, which is why the worker exists);
  * what the Session says and sends reaches the page in order, ON the
    main thread;
  * the map callback and the draw happen after, on the main thread;
  * choices made while a task runs collapse to the last one;
  * an offer while a task runs waits for it, and is not lost.

Written 2026-10-11.
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from qgis.core import QgsApplication  # noqa: E402
from qgis.PyQt.QtCore import QCoreApplication, QTimer  # noqa: E402

FAILED = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - %s" % (detail,)) if detail != "" else ""))
    if not condition:
        FAILED.append(label)


class Views(object):
    """Stands in for web_view.LinkedViews: records what the page is told."""

    def __init__(self):
        self.ready_seen = True
        self.calls = []
        self.main = threading.get_ident()
        self.off_main = 0

    def _note(self, name, payload):
        if threading.get_ident() != self.main:
            self.off_main += 1
        self.calls.append((name, payload))

    def call(self, name, payload):
        self._note(name, payload)
        return True

    def busy(self, message):
        self._note("busy", message)
        return True

    def draw_scatter(self, options):
        self._note("scatter", options)
        return True

    def names(self):
        return [n for n, _ in self.calls]

    def close(self):
        self.closed = True


def wait_until(condition, timeout=120.0, tick_ms=10, ticks=None):
    """Spin the event loop until `condition()`; record the gaps between ticks."""
    started = time.monotonic()
    last = time.perf_counter()
    while not condition():
        QCoreApplication.processEvents()
        time.sleep(tick_ms / 1000.0)
        now = time.perf_counter()
        if ticks is not None:
            ticks.append(now - last)
        last = now
        if time.monotonic() - started > timeout:
            return False
    return True


def main():
    app = QgsApplication([], False)
    app.initQgis()
    from equis_loader.linked_views import LinkedViewsController
    from equis_loader.store import ReportStore

    root = os.path.join(ROOT, "data", "facility_a")
    if not os.path.isdir(root):
        print("[SKIP] no data/facility_a - nothing ran")
        return
    store = ReportStore.from_manifest(root)
    maps = []
    statuses = []
    controller = LinkedViewsController(
        connection_for=lambda: store.con, layer_for=lambda: None,
        on_status=statuses.append, store_for=lambda: store,
        on_scatter_map=lambda stations, breaks, x, y, *rest: maps.append(
            (len(stations), x["chemical"], y["chemical"],
             threading.get_ident(), stations,
             rest[1] if len(rest) > 1 else None)),
        locations_for=lambda: None, matrix_for=lambda: "WS")
    views = controller.views = Views()

    print("\n-- the first offer: water's every-analyte pivot, in a worker")
    ticks = []
    t = time.monotonic()
    controller.offer()
    check("offer() returns at once, the work handed to a task",
          controller._task is not None and time.monotonic() - t < 0.5,
          round(time.monotonic() - t, 3))
    done = wait_until(lambda: controller._task is None, ticks=ticks)
    took = time.monotonic() - t
    ticks.sort()
    check("the task finished", done, round(took, 1))
    check("the main thread kept running through it (worst gap < 250 ms)",
          ticks and ticks[-1] < 0.25,
          "%d ticks over %.1f s, worst %.0f ms" % (len(ticks), took,
                                                   1000 * (ticks[-1] if ticks else 0)))
    check("the page was told on the main thread only", views.off_main == 0,
          views.off_main)
    check("it heard: working (in the chooser), then the offer, then a note",
          views.names()[:3] == ["working", "offer", "note"], views.names())
    offer = dict(views.calls)["offer"]
    check("water's catalogue: 215 analytes", len(offer["variables"]) == 215,
          len(offer["variables"]))

    print("\n-- three quick choices: only the first and the last are answered")
    session = controller.session
    x = session.find("WS", "Manganese")
    views.calls.clear()
    controller._on_choice({"matrix": "WS", "x": x, "y": session.find("WS", "Zinc")})
    controller._on_choice({"matrix": "WS", "x": x, "y": session.find("WS", "Copper")})
    controller._on_choice({"matrix": "WS", "x": x, "y": session.find("WS", "Lead")})
    wait_until(lambda: controller._task is None and controller._queued is None)
    drawn = [p["y"]["chemical"] for n, p in views.calls if n == "scatter"]
    check("Copper, chosen and replaced while Zinc ran, is never drawn",
          drawn == ["Zinc", "Lead"], drawn)
    check("the map was built for each drawn pair, on the main thread",
          [m[2] for m in maps] == ["Zinc", "Lead"]
          and all(m[3] == views.main for m in maps), maps)
    last = [p for n, p in views.calls if n == "scatter"][-1]
    check("the last scatter reads a pair table", last["pivot"].startswith("pp_"),
          last["pivot"])
    n = store.con.execute("SELECT count(*) FROM %s WHERE manganese_t_value IS NOT NULL "
                          "AND lead_t_value IS NOT NULL" % last["pivot"]).fetchone()[0]
    check("which the MAIN connection can read: Mn x Pb total, 63,222",
          n == 63222, n)

    print("\n-- points: most recent per location, then the map's time window")
    import datetime
    maps.clear()
    views.calls.clear()
    controller._on_choice({"matrix": "WS", "x": x, "y": session.find("WS", "Lead"),
                           "mode": "recent"})
    wait_until(lambda: controller._task is None)
    recent = [p for n, p in views.calls if n == "scatter"][-1]
    picked = store.con.execute("SELECT count(*) FROM %s" % recent["solid"]).fetchone()[0]
    check("most recent per location: one solid sample per station, the map agrees",
          recent["points"]["mode"] == "recent" and picked == maps[-1][0],
          (recent["points"], picked, maps[-1][:3]))

    views.calls.clear()
    maps.clear()
    started = {"n": 0}
    real_start = controller._start

    def counting(*a, **k):
        started["n"] += 1
        return real_start(*a, **k)
    controller._start = counting
    # Three moves in quick succession, as an animation would make them.
    for year in (2010, 2012, 2014):
        controller.set_window((datetime.date(year, 1, 1), datetime.date(year, 12, 31)))
    wait_until(lambda: controller._task is None
               and not controller._window_timer.isActive(), timeout=60)
    controller._start = real_start
    drawn = [p for n, p in views.calls if n == "scatter"]
    check("three quick window moves make ONE re-pick, for the last",
          started["n"] == 1 and len(drawn) == 1
          and drawn[0]["points"]["window"] == ["2014-01-01", "2014-12-31"],
          (started["n"], [d["points"]["window"] for d in drawn]))
    if drawn:
        inside = store.con.execute(
            "SELECT count(*), count(*) FILTER (WHERE sample_date >= DATE '2014-01-01' "
            "AND sample_date < DATE '2015-01-01') FROM %s s JOIN %s p USING (sample)"
            % (drawn[0]["solid"], drawn[0]["pivot"])).fetchone()
        check("its solid samples are all inside 2014, one per station",
              inside[0] == inside[1] and inside[0] == maps[-1][0] if maps else False,
              (inside, maps[-1][:3] if maps else None))
        check("the pair and the breaks are the drawn scatter's: axes and colours fixed",
              drawn[0]["pivot"] == recent["pivot"]
              and drawn[0]["bivariate"] == recent["bivariate"])
        check("the map was rebuilt for the window, on the main thread",
              maps and maps[-1][3] == views.main, maps[-1:] if maps else maps)
    views.calls.clear()
    controller.set_window(None)
    controller._on_choice({"matrix": "WS", "x": x, "y": session.find("WS", "Lead"),
                           "mode": "all"})
    wait_until(lambda: controller._task is None
               and not controller._window_timer.isActive(), timeout=60)
    last = [p for n, p in views.calls if n == "scatter"][-1]
    check("no window and every sample: back to drawing all, no solid table",
          last.get("solid") is None and last["points"]["window"] is None, last["points"])

    print("\n-- a ternary: Ca : Mg : Na in water, and its map layer")
    maps.clear()
    views.calls.clear()
    trio = [session.find("WS", n) for n in ("Calcium", "Magnesium", "Sodium")]
    controller._on_choice({"matrix": "WS", "x": trio[0], "y": trio[1], "z": trio[2],
                           "chart": "ternary", "mode": "recent"})
    wait_until(lambda: controller._task is None)
    drawn = [p for n, p in views.calls if n == "scatter"]
    check("the page is asked for a ternary, its third analyte named",
          drawn and drawn[-1].get("kind") == "ternary"
          and drawn[-1]["z"]["chemical"] == "Sodium",
          [d.get("kind") for d in drawn])
    check("the map callback gets the third analyte, on the main thread",
          maps and maps[-1][5] is not None and maps[-1][5]["chemical"] == "Sodium"
          and maps[-1][3] == views.main, maps[-1][:4] if maps else maps)
    if maps:
        from equis_loader.layer_builder import build_ternary_layer
        from qgis.core import QgsSymbolLayer
        stations = maps[-1][4]
        pages = store.location_features()
        layer, placed, unplaced = build_ternary_layer(
            "ternary test", pages, stations,
            [{"chemical": "Calcium"}, {"chemical": "Magnesium"}, {"chemical": "Sodium"}])
        check("the ternary layer places every station that has a point",
              layer is not None and placed + len(unplaced) == len(stations)
              and placed > 0, (placed, len(unplaced), len(stations)))
        by_code = {str(s["location"]).lower(): s for s in stations}
        wrong = [f["code"] for f in layer.getFeatures()
                 if f["tn_colour"] != by_code[str(f["code"]).lower()]["colour"]]
        check("each feature carries its station's colour (ternary.py's)",
              not wrong, wrong[:3])
        cats = {c.value(): c for c in layer.renderer().categories()}
        prop = cats["all_detected"].symbol().symbolLayer(0).dataDefinedProperties()
        check("detected stations are coloured from that field; non-detects hollow grey",
              set(cats) == {"all_detected", "non_detect"}
              and prop.property(QgsSymbolLayer.PropertyFillColor).field() == "tn_colour",
              sorted(cats))

    print("\n-- an offer while a task runs is kept, not lost")
    views.calls.clear()
    controller._on_choice({"matrix": "SE"})
    controller.offer()           # e.g. the tab shown again mid-build
    check("it waits rather than starting a second task",
          controller._offer_wanted, controller._offer_wanted)
    wait_until(lambda: controller._task is None)
    check("and the sediment catalogue arrived",
          any(n == "offer" and p["matrix"] == "SE" for n, p in views.calls),
          [n for n, _ in views.calls])

    print("\n-- a refusal reaches the page as a note")
    views.calls.clear()
    controller._on_choice({"matrix": "SE", "x": "no_such_column", "y": "nor_this"})
    wait_until(lambda: controller._task is None)
    check("no draw, and a note saying what to do",
          "scatter" not in views.names() and "note" in views.names(), views.names())

    controller.close()
    QgsApplication.exitQgis()


if __name__ == "__main__":
    main()
    print("\n%s" % ("ALL ANALYSIS TASK TESTS PASSED" if not FAILED
                    else "%d FAILED" % len(FAILED)))
    sys.exit(1 if FAILED else 0)
