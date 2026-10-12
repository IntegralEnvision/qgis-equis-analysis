"""The Analysis tab's chooser: what the page is offered and what a choice draws.

    python tools/test_analysis.py

Plain interpreter plus duckdb, no QGIS: equis_loader/analysis.py is the
host's whole decision, and linked_views.py only shows what it returns. The
fixture is test_pivot.py's, plus a soil matrix with one analyte reported in
two units; expected answers are worked by hand beside each check. The last
section runs against the real Facility A parquet when present.
"""
import datetime
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from equis_loader import report_data as rd  # noqa: E402
from equis_loader.analysis import Session  # noqa: E402
from equis_loader.pivot import (  # noqa: E402
    KEEP_PAIRS, KEEP_TABLES, Variable, build_pivot)
from equis_loader.store import ReportStore  # noqa: E402
from test_pivot import AS, HEADERS, PB, ROWS, row  # noqa: E402

FAILED = []
CU = ("Copper", "7440-50-8")


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - %s" % (detail,)) if detail != "" else ""))
    if not condition:
        FAILED.append(label)


SOIL = [
    row("SB-1", "T1", "01/01/2021", AS, "5", "", "Y", unit="mg/kg", matrix="SO", fraction=""),
    row("SB-1", "T1", "01/01/2021", PB, "7", "", "Y", unit="mg/kg", matrix="SO", fraction=""),
    row("SB-2", "T2", "02/01/2021", AS, "3", "", "Y", unit="mg/kg", matrix="SO", fraction=""),
    # Copper in two units: skipped, listed, and it must not cost As and Pb.
    row("SB-1", "T1", "01/01/2021", CU, "9", "", "Y", unit="mg/kg", matrix="SO", fraction=""),
    row("SB-2", "T2", "02/01/2021", CU, "1", "", "Y", unit="percent", matrix="SO", fraction=""),
]

# Tailings holding arsenic in two fractions: its columns are named
# arsenic_d, arsenic_t, lead_t - not soil's arsenic and lead.
TAILS = [
    row("TP-1", "U1", "01/01/2022", AS, "4", "", "Y", unit="mg/kg", matrix="TA", fraction="T"),
    row("TP-1", "U1", "01/01/2022", AS, "1", "", "Y", unit="mg/kg", matrix="TA", fraction="D"),
    row("TP-1", "U1", "01/01/2022", PB, "2", "", "Y", unit="mg/kg", matrix="TA", fraction="T"),
]


class Page(object):
    """Records what the host says and sends, as the page would receive it."""

    def __init__(self):
        self.said, self.sent = [], []

    def say(self, message, where):
        assert where in ("chooser", "chart"), where
        self.said.append(message)
        self.where = getattr(self, "where", []) + [where]

    def send(self, name, payload):
        self.sent.append((name, payload))

    def names(self):
        return [name for name, _ in self.sent]

    def last(self, name):
        for n, payload in reversed(self.sent):
            if n == name:
                return payload
        return None

    def clear(self):
        self.said, self.sent = [], []


def fixture_checks():
    store = ReportStore.from_table(rd.ReportTable(HEADERS, ROWS + SOIL + TAILS))
    session, page = Session(store), Page()

    print("\n-- the matrix list")
    # WS normal samples with a code, replicates out: S1, S2, S3, S4, Q7_R2
    # (S5 is FD; S1_R2, S2_BD, S3-R2 are field replicates). SO: T1, T2.
    check("matrices counted as the pivot counts them, largest first",
          session.matrices() == [{"code": "WS", "samples": 5},
                                 {"code": "SO", "samples": 2},
                                 {"code": "TA", "samples": 1}],
          session.matrices())

    print("\n-- no matrix yet")
    got = session.respond({}, page.say, page.send)
    check("no matrix: the page is offered the matrices and nothing to draw",
          got is None and page.names() == ["offer"]
          and page.last("offer")["matrix"] is None
          and len(page.last("offer")["matrices"]) == 3, page.sent)
    page.clear()
    session.respond({"matrix": "XX"}, page.say, page.send)
    check("an unknown matrix is treated as none chosen",
          page.last("offer")["matrix"] is None, page.last("offer"))

    print("\n-- a matrix")
    page.clear()
    got = session.respond({"matrix": "WS"}, page.say, page.send)
    offer = page.last("offer")
    by = {v["prefix"]: v for v in (offer or {}).get("variables", [])}
    check("the first ask announces the pivot before building it",
          len(page.said) == 1 and page.said[0].startswith("Pivoting every analyte in WS"),
          page.said)
    check("every analyte in the matrix is offered", sorted(by) == ["arsenic", "lead"],
          sorted(by))
    # Arsenic cells: S1, S2, S3, S4, Q7_R2 = 5; S4's flag is blank, so
    # unknown rather than not detected: 4 detected. Lead: S1, S2 (ND), S4.
    check("arsenic: 5 samples, 4 detected (S4's blank flag is not counted either way)",
          (by["arsenic"]["samples"], by["arsenic"]["detected"]) == (5, 4),
          by.get("arsenic"))
    check("lead: 3 samples, 2 detected",
          (by["lead"]["samples"], by["lead"]["detected"]) == (3, 2), by.get("lead"))
    check("fraction is named for people", by["arsenic"]["fractionName"] == "total",
          by["arsenic"]["fractionName"])
    check("no pair yet: nothing drawn, and the page is told what to do",
          got is None and page.last("note", ) is not None, page.sent)

    check("the pivot's wait shows in the chooser, not over the chart",
          page.where == ["chooser"], page.where)
    check("an offered catalogue needs no re-offer", not session.needs_offer())
    page.clear()
    session.respond({"matrix": "WS"}, page.say, page.send)
    check("asked again: no rebuild, no re-offer", page.said == []
          and "offer" not in page.names(), (page.said, page.names()))

    print("\n-- choosing X")
    page.clear()
    session.respond({"matrix": "WS", "x": "arsenic"}, page.say, page.send)
    paired = page.last("paired")
    # Arsenic with value AND flag: S1, S2, S3, Q7_R2 (S4's flag is blank).
    # Of those, lead with value and flag: S1 (3, Y) and S2 (limit 2, N).
    check("pair counts: what the scatter would draw against arsenic",
          paired == {"x": "arsenic", "counts": {"arsenic": 4, "lead": 2}}, paired)
    page.clear()
    session.respond({"matrix": "WS", "x": "arsenic"}, page.say, page.send)
    check("the same X again: counts are not resent", "paired" not in page.names(),
          page.names())

    print("\n-- choosing Y")
    page.clear()
    got = session.respond({"matrix": "WS", "x": "arsenic", "y": "lead"},
                          page.say, page.send, locations=["MW-1"])
    check("a pair is a Scatter", got is not None, page.sent)
    if got is not None:
        o = got.options
        check("its options are the page's scatter contract",
              {"pivot", "x", "y", "matrix", "detectTrue", "resolved", "excluded",
               "bivariate", "locations"} <= set(o), sorted(o))
        check("x and y are the chosen columns, with their units",
              (o["x"]["prefix"], o["y"]["prefix"], o["x"]["unit"]) ==
              ("arsenic", "lead", "ug/L"), (o["x"], o["y"]))
        check("the map selection travels with it", o["locations"] == ["MW-1"],
              o.get("locations"))
        # Detected on both: S1 only (S2's lead is a non-detect, S4's As flag
        # blank). One sample cannot be cut into three classes.
        check("breaks are over samples detected on both (S1)",
              got.breaks["samples"] == 1, got.breaks)
        # MW-3's only sample, S4, has a blank As flag: not a station.
        check("the map gets a station per location carrying both: MW-1",
              sorted(s["location"] for s in got.stations) == ["MW-1"],
              [s.get("location") for s in got.stations])
        check("a pivot of every analyte, not of two",
              got.pivot is session.pivot("WS"), got.pivot.table)

    print("\n-- points: one real sample per location, and the time window")
    solid = lambda found: sorted(r[0] for r in store.con.execute(
        "SELECT sample FROM %s" % found.options["solid"]).fetchall())
    check("every sample, all time: no solid table (the page draws them all)",
          got.options.get("solid") is None
          and got.options["points"]["mode"] == "all", got.options.get("points"))
    # MW-1 holds the only two pairs: S1 (2020-01-01, As 10) and S2
    # (2020-02-01, As 4, lead a non-detect).
    page.clear()
    got = session.respond({"matrix": "WS", "x": "arsenic", "y": "lead",
                           "mode": "recent"}, page.say, page.send)
    check("most recent per location: S2 alone, and the map agrees",
          solid(got) == ["S2"] and [s["sample"] for s in got.stations] == ["S2"],
          (solid(got), [s["sample"] for s in got.stations]))
    check("the caption is told what the points are",
          got.options["points"]["label"] == "each location's most recent sample",
          got.options["points"])
    got = session.repoint(mode="max_x")
    check("highest arsenic per location: S1 (10 over 4)",
          solid(got) == ["S1"] and got.stations[0]["sample"] == "S1", solid(got))
    breaks_before = got.breaks
    got = session.repoint(window=(datetime.date(2020, 1, 15), datetime.date(2020, 3, 1)))
    check("a window re-picks within it: only S2 is inside",
          solid(got) == ["S2"] and got.options["points"]["window"]
          == ["2020-01-15", "2020-03-01"], (solid(got), got.options["points"]))
    check("...without touching the pair, the axes' data or the breaks",
          got.options["pivot"] == session.current.options["pivot"]
          and got.breaks is breaks_before)
    got = session.repoint(mode="all")
    check("every sample within the window: still a solid table, S2",
          solid(got) == ["S2"], solid(got))
    session.repoint(window=None)
    check("clearing the window: every sample, no solid table",
          session.current.options.get("solid") is None)
    check("the map's phrase follows the mode and the window",
          session.repoint(mode="max_x", window=(datetime.date(2020, 1, 1), None))
          .options["points"]["map"] == "sample with the highest Arsenic in 2020-01-01 to …"
          and session.repoint(mode="all", window=None).options["points"]["map"]
          == "most recent sample")
    check("the mode is remembered with the choice",
          session.choice.get("mode") == "all", session.choice)

    page.clear()
    got = session.respond({"matrix": "WS", "x": "lead", "y": "lead"},
                          page.say, page.send)
    check("X = Y draws nothing and says so",
          got is None and "different" in (page.last("note") or ""), page.last("note"))
    page.clear()
    got = session.respond({"matrix": "WS", "x": "arsenic", "y": "gone"},
                          page.say, page.send)
    check("a prefix the pivot lacks is dropped, not guessed at", got is None,
          page.sent)

    print("\n-- a matrix with an analyte in two units")
    page.clear()
    session.respond({"matrix": "SO", "x": "arsenic"}, page.say, page.send)
    offer = page.last("offer")
    check("the new matrix is offered", offer and offer["matrix"] == "SO", offer)
    check("copper is skipped, with the units in the reason",
          [s["chemical"] for s in offer["skipped"]] == ["Copper"]
          and "mixes units" in offer["skipped"][0]["reason"], offer["skipped"])
    check("the skip comes with its unit counts, for the page to lay out",
          offer["skipped"][0].get("kind") == "units"
          and sorted(map(tuple, offer["skipped"][0].get("units", [])))
          == [("mg/kg", 1), ("percent", 1)], offer["skipped"][0])
    check("and does not cost arsenic and lead",
          sorted(v["prefix"] for v in offer["variables"]) == ["arsenic", "lead"],
          [v["prefix"] for v in offer["variables"]])
    check("X carried over by prefix: soil's arsenic gets its own pair counts",
          page.last("paired") == {"x": "arsenic", "counts": {"arsenic": 2, "lead": 1}},
          page.last("paired"))

    print("\n-- the chart's own table")
    page.clear()
    got = session.respond({"matrix": "SO", "x": "arsenic", "y": "lead"},
                          page.say, page.send)
    table = got.options["pivot"]
    cols = [d[0] for d in store.con.execute(
        "SELECT * FROM %s LIMIT 0" % table).description]
    check("the page reads a pair table, not the every-analyte view",
          table.startswith("pp_") and table != got.pivot.table, table)
    check("which holds the six sample columns and the two analytes' eight",
          len(cols) == 14 and "arsenic_value" in cols and "lead_detectlimit" in cols,
          cols)
    for _ in range(KEEP_PAIRS + 2):
        session.respond({"matrix": "SO", "x": "lead", "y": "arsenic"},
                        page.say, page.send)
    pairs = store.con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE starts_with(table_name, 'pp_')"
        ).fetchone()[0]
    check("old pair tables are dropped past KEEP_PAIRS", pairs == KEEP_PAIRS, pairs)

    print("\n-- a choice across matrices")
    check("a name alone finds the total fraction", session.find("TA", "Arsenic")
          == "arsenic_t", session.find("TA", "Arsenic"))
    page.clear()
    got = session.respond({"matrix": "SO", "x": "arsenic", "y": "lead"},
                          page.say, page.send)
    page.clear()
    got = session.respond({"matrix": "TA", "x": "arsenic", "y": "lead"},
                          page.say, page.send)
    offer = page.last("offer")
    check("soil's arsenic x lead carries to tailings' arsenic_t x lead_t",
          (offer["x"], offer["y"]) == ("arsenic_t", "lead_t") and got is not None,
          (offer["x"], offer["y"]))
    page.clear()
    session.respond({"matrix": "TA", "x": "arsenic_d", "y": "lead_t"},
                    page.say, page.send)
    page.clear()
    session.respond({"matrix": "SO", "x": "arsenic_d", "y": "lead_t"},
                    page.say, page.send)
    check("and back: soil has no dissolved arsenic, so its arsenic - the same "
          "chemical, never another analyte",
          (page.last("offer")["x"], page.last("offer")["y"]) == ("arsenic", "lead"),
          (page.last("offer")["x"], page.last("offer")["y"]))

    print("\n-- what invalidates a pivot")
    before = session.pivot("WS").table
    store.set_row_scope(datetime.date(2020, 1, 15), None, None)
    check("a row-scope change is noticed before anything is asked",
          session.needs_offer())
    page.clear()
    session.respond({"matrix": "WS"}, page.say, page.send)
    check("a row-scope change rebuilds the pivot and re-offers",
          session.pivot("WS").table != before and "offer" in page.names(),
          (before, session.pivot("WS").table, page.names()))
    store.set_row_scope(None, None, None)

    current = session.pivot("WS").table
    # Console scatters build on the main connection and keep their own
    # list, so they cannot retire the chooser's pivots; builds on the
    # session's cursor can.
    for _ in range(KEEP_TABLES):
        build_pivot(store, [Variable(*AS)], matrix=["WS"])
    check("console pivots do not retire the chooser's",
          session.pivot("WS").table == current, (current, session.pivot("WS").table))
    for _ in range(KEEP_TABLES):
        build_pivot(session.db, [Variable(*AS)], matrix=["WS"])
    check("a pivot retired by KEEP_TABLES is rebuilt, not read after its drop",
          session.pivot("WS").table != current
          and session.pivot("WS").table in session.db._pivot_tables,
          (current, session.pivot("WS").table))

    page.clear()
    session.respond({"matrix": "WS"}, page.say, page.send)
    page.clear()
    session.page_reloaded()
    session.respond({"matrix": "WS", "x": "arsenic"}, page.say, page.send)
    check("a reloaded page is offered everything again",
          page.names()[:2] == ["offer", "paired"], page.names())


def window_checks():
    from equis_loader.analysis import window_from_range
    print("\n-- the map's time range as the scatter's whole-day window")
    d = datetime.datetime
    check("a one-day frame [Jan 1 00:00, Jan 2 00:00) is Jan 1 only",
          window_from_range(d(2020, 1, 1), d(2020, 1, 2)) ==
          (datetime.date(2020, 1, 1), datetime.date(2020, 1, 1)))
    check("...unless the frame includes its end",
          window_from_range(d(2020, 1, 1), d(2020, 1, 2), include_end=True) ==
          (datetime.date(2020, 1, 1), datetime.date(2020, 1, 2)))
    check("an end later in a day keeps that day",
          window_from_range(d(2020, 1, 1), d(2020, 3, 5, 14, 30))[1]
          == datetime.date(2020, 3, 5))
    check("open ends stay open; no range is no window",
          window_from_range(None, d(2021, 1, 1)) == (None, datetime.date(2020, 12, 31))
          and window_from_range(None, None) is None)


def facility_a_checks():
    print("\n-- Facility A (real parquet)")
    root = os.path.join(ROOT, "data", "facility_a")
    if not os.path.isdir(root):
        print("[SKIP] no data/facility_a - the real-data checks did not run")
        return
    store = ReportStore.from_manifest(root)
    session, page = Session(store), Page()
    soil = {m["code"]: m["samples"] for m in session.matrices()}.get("SO")
    check("SO is 112 samples, as the scatter has (839 codes with replicates)",
          soil == 112, soil)
    t = time.time()
    session.respond({"matrix": "SO"}, page.say, page.send)
    took = time.time() - t
    offer = page.last("offer")
    names = {v["chemical"] for v in offer["variables"]}
    check("soil offers 13 analytes, including Arsenic and Chromium",
          len(offer["variables"]) == 13 and {"Arsenic", "Chromium"} <= names,
          len(offer["variables"]))
    x, y = session.find("SO", "Arsenic"), session.find("SO", "Chromium")
    page.clear()
    got = session.respond({"matrix": "SO", "x": x, "y": y}, page.say, page.send)
    counts = page.last("paired")["counts"]
    # test_scatter_sql.py: As x Cr SO is 112 primaries.
    check("As x Cr pairs: 112, as the two-analyte scatter has", counts[y] == 112,
          counts[y])
    b = got.breaks
    # test_bivariate.py / try_scatter.py: As <= 2.69 | 9.85; Cr <= 2.64 | 6.34.
    check("the breaks are the two-analyte scatter's",
          [round(v, 2) for v in b["x"]["breaks"]] == [2.69, 9.85]
          and [round(v, 2) for v in b["y"]["breaks"]] == [2.64, 6.34],
          (b["x"]["breaks"], b["y"]["breaks"]))
    check("10 stations on the scatter map", len(got.stations) == 10,
          len(got.stations))
    print("     (soil every-analyte pivot and offer: %.2f s)" % took)
    for matrix in ("TAILS", "SE", "WS"):
        page.clear()
        t = time.time()
        session.respond({"matrix": matrix}, page.say, page.send)
        offer = page.last("offer")
        print("     %-5s %3d analytes offered, %2d skipped, %6s samples: %.1f s"
              % (matrix, len(offer["variables"]), len(offer["skipped"]),
                 format(offer["samples"], ",d"), time.time() - t))


if __name__ == "__main__":
    fixture_checks()
    window_checks()
    facility_a_checks()
    print("\n%s" % ("ALL ANALYSIS TESTS PASSED" if not FAILED
                    else "%d FAILED" % len(FAILED)))
    sys.exit(1 if FAILED else 0)
