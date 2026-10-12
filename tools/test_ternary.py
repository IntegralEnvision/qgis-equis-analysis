"""The ternary: shares, colours, the unit rule, and the page's SQL held to them.

    python tools/test_ternary.py

Plain interpreter plus duckdb, and node for the page's SQL (as
test_scatter_sql.py). equis_loader/ternary.py decides shares and colours;
web/src/ternary.js restates them in SQL so the page can draw. This runs
that SQL against real pivots and requires every sample's colour to match
ternary.py's, so the triangle and the map never disagree. Expected answers
in the fixture are worked by hand beside each check.
"""
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from node_path import ensure_node  # noqa: E402
from equis_loader import report_data as rd  # noqa: E402
from equis_loader import ternary as tn  # noqa: E402
from equis_loader.analysis import Session  # noqa: E402
from equis_loader.store import DETECT_TRUE, ReportStore  # noqa: E402
from test_pivot import HEADERS, row  # noqa: E402

FAILED = []
CA, MG, NA = ("Calcium", "7440-70-2"), ("Magnesium", "7439-95-4"), ("Sodium", "7440-23-5")
CL = ("Chloride", "16887-00-6")


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - %s" % (detail,)) if detail != "" else ""))
    if not condition:
        FAILED.append(label)


GENERATOR = """
import { ternarySql, ternaryCountsSql } from "./src/ternary.js";
const asks = JSON.parse(process.argv[2]);
console.log(JSON.stringify(asks.map((a) => ({
  rows: ternarySql(a), counts: ternaryCountsSql(a) }))));
"""


def page_sql(asks):
    web = os.path.join(ROOT, "web")
    path = os.path.join(web, "_ternarycheck.mjs")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(GENERATOR)
    try:
        if not ensure_node():
            return None
        result = subprocess.run(["node", "_ternarycheck.mjs", json.dumps(asks)],
                                cwd=web, capture_output=True, text=True)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if result.returncode:
        print("node failed: %s" % result.stderr[:400])
        return None
    return json.loads(result.stdout.strip())


def pure_checks():
    print("-- shares, places and colours (ternary.py)")
    check("shares sum to one", tn.shares(2, 3, 5) == (0.2, 0.3, 0.5))
    check("a negative value, a missing one or a zero sum cannot be placed",
          tn.shares(-1, 2, 3) is None and tn.shares(None, 2, 3) is None
          and tn.shares(0, 0, 0) is None)
    check("a pure corner takes its corner's colour",
          [tn.colour_for(*v, all_detected=True) for v in
           ((1, 0, 0), (0, 1, 0), (0, 0, 1))] == list(tn.CORNERS))
    check("any non-detect is grey, whatever the shares",
          tn.colour_for(1, 1, 1, all_detected=False) == tn.GREY)
    x, y = tn.position(0, 0, 1)
    check("C sits at the top", (round(x, 12), round(y, 12)) == (0.5, round(tn.HEIGHT, 12)))
    check("one unit: refused otherwise, naming each",
          tn.unit_refusal([{"chemical": "A", "unit": "ug/L"}, {"chemical": "B", "unit": "mg/L"},
                           {"chemical": "C", "unit": "ug/L"}]).count("ug/L") == 2
          and tn.unit_refusal([{"chemical": c, "unit": "ug/L"} for c in "ABC"]) is None)
    check("a unit not reported counts as different",
          tn.unit_refusal([{"chemical": "A", "unit": ""}, {"chemical": "B", "unit": "ug/L"},
                           {"chemical": "C", "unit": "ug/L"}]) is not None)


ROWS = [
    # W1 at MW-1 (2020-01-01): 20 : 10 : 10, all detected -> shares .5/.25/.25
    row("MW-1", "W1", "01/01/2020", CA, "20", "", "Y"),
    row("MW-1", "W1", "01/01/2020", MG, "10", "", "Y"),
    row("MW-1", "W1", "01/01/2020", NA, "10", "", "Y"),
    # W2 at MW-1 (2021-01-01): sodium a non-detect at 5 -> class nd, grey
    row("MW-1", "W2", "01/01/2021", CA, "5", "", "Y"),
    row("MW-1", "W2", "01/01/2021", MG, "5", "", "Y"),
    row("MW-1", "W2", "01/01/2021", NA, "", "5", "N", lab_q="U"),
    # W3 at MW-2: a zero sum - carried but unplaceable
    row("MW-2", "W3", "01/01/2020", CA, "0", "", "Y"),
    row("MW-2", "W3", "01/01/2020", MG, "0", "", "Y"),
    row("MW-2", "W3", "01/01/2020", NA, "0", "", "Y"),
    # W4 at MW-3: no sodium at all - carries only some
    row("MW-3", "W4", "01/01/2020", CA, "7", "", "Y"),
    row("MW-3", "W4", "01/01/2020", MG, "3", "", "Y"),
    # Chloride in mg/L: a ternary with it is refused for its unit
    row("MW-1", "W1", "01/01/2020", CL, "2", "", "Y", unit="mg/L"),
]


class Page(object):
    def __init__(self):
        self.sent = []

    def say(self, message, where):
        pass

    def send(self, name, payload):
        self.sent.append((name, payload))

    def last(self, name):
        return next((p for n, p in reversed(self.sent) if n == name), None)


def fixture_checks():
    print("\n-- the session's ternary (hand-worked fixture)")
    store = ReportStore.from_table(rd.ReportTable(HEADERS, ROWS))
    session, page = Session(store), Page()
    session.respond({"matrix": "WS", "x": "calcium", "y": "magnesium",
                     "chart": "ternary"}, page.say, page.send)
    # Carrying Ca and Mg (values and flags): W1, W2, W3, W4.
    # Of those, sodium too: W1, W2, W3.
    check("the third list counts samples carrying X AND Y AND each",
          (page.last("pairedZ") or {}).get("counts", {}).get("sodium") == 3,
          page.last("pairedZ"))
    check("...and says so before a third is chosen",
          "three different analytes" in (page.last("note") or ""), page.last("note"))
    page.sent.clear()
    got = session.respond({"matrix": "WS", "x": "calcium", "y": "magnesium",
                           "z": "chloride", "chart": "ternary"}, page.say, page.send)
    check("chloride in mg/L beside ug/L is refused, saying both units",
          got is None and "mg/L" in (page.last("note") or "")
          and "ug/L" in (page.last("note") or ""), page.last("note"))
    page.sent.clear()
    got = session.respond({"matrix": "WS", "x": "calcium", "y": "magnesium",
                           "z": "sodium", "chart": "ternary"}, page.say, page.send)
    check("calcium : magnesium : sodium is a ternary",
          got is not None and got.kind == "ternary" and got.options["kind"] == "ternary",
          page.sent[-1:] if got is None else got.options["kind"])
    if got is None:
        return
    check("its table carries the three analytes", got.options["pivot"].startswith("pp_")
          and got.options["z"]["prefix"] == "sodium")
    st = {s["location"]: s for s in got.stations}
    # MW-1's most recent placeable sample is W2 (2021), with a non-detect.
    check("the map takes MW-1's most recent sample (W2), grey for its non-detect",
          st["MW-1"]["sample"] == "W2" and st["MW-1"]["colour"] == tn.GREY, st.get("MW-1"))
    check("MW-2's only sample has a zero sum: no station",
          "MW-2" not in st, sorted(st))
    got = session.repoint(mode="max_x")
    st = {s["location"]: s for s in got.stations}
    check("highest calcium per location: W1 (20), coloured by .5 : .25 : .25",
          st["MW-1"]["sample"] == "W1"
          and st["MW-1"]["colour"] == tn.colour_for(20, 10, 10, True), st.get("MW-1"))
    got = session.repoint(mode="max_z", window=(datetime.date(2020, 1, 1),
                                                datetime.date(2020, 12, 31)))
    check("highest sodium in 2020: W1, the only 2020 sample at MW-1",
          [s["sample"] for s in got.stations if s["location"] == "MW-1"] == ["W1"],
          [(s["location"], s["sample"]) for s in got.stations])
    session.repoint(mode="all", window=None)

    asks = [{"pivot": got.options["pivot"], "x": "calcium", "y": "magnesium",
             "z": "sodium", "detectTrue": list(DETECT_TRUE),
             "corners": list(tn.CORNERS), "grey": tn.GREY}]
    sql = page_sql(asks)
    if sql is None:
        print("[SKIP] node unavailable - the page's SQL was not run")
        return
    rows = store.con.execute("SELECT sample, class, sa, sb, sc, colour FROM (%s) ORDER BY sample"
                             % sql[0]["rows"]).fetchall()
    check("the page places W1 and W2, not W3 (zero sum) or W4 (no sodium)",
          [r[0] for r in rows] == ["W1", "W2"], [r[0] for r in rows])
    w1 = rows[0]
    check("W1: shares .5 / .25 / .25, class all, ternary.py's colour",
          (w1[1], round(w1[2], 9), round(w1[3], 9), round(w1[4], 9), w1[5])
          == ("all", 0.5, 0.25, 0.25, tn.colour_for(20, 10, 10, True)), w1)
    check("W2: a non-detect, grey", (rows[1][1], rows[1][5]) == ("nd", tn.GREY), rows[1])
    cur = store.con.execute(sql[0]["counts"])
    c = dict(zip([d[0] for d in cur.description], cur.fetchone()))
    check("counts: 2 placed (1 all detected, 1 with a non-detect), 1 unplaceable",
          (c["placed"], c["all_detected"], c["with_nd"], c["unplaceable"]) == (2, 1, 1, 1), c)


def facility_a_checks():
    print("\n-- Facility A: every placed sample's colour, page against ternary.py")
    root = os.path.join(ROOT, "data", "facility_a")
    if not os.path.isdir(root):
        print("[SKIP] no data/facility_a - the real-data checks did not run")
        return
    store = ReportStore.from_manifest(root)
    session, page = Session(store), Page()
    session.pivot("WS")
    trio = [session.find("WS", n) for n in ("Calcium", "Magnesium", "Sodium")]
    got = session.respond({"matrix": "WS", "x": trio[0], "y": trio[1], "z": trio[2],
                           "chart": "ternary"}, page.say, page.send)
    check("Ca : Mg : Na in water is drawable (all ug/L)", got is not None, page.last("note"))
    if got is None:
        return
    sql = page_sql([{"pivot": got.options["pivot"], "x": trio[0], "y": trio[1],
                     "z": trio[2], "detectTrue": list(DETECT_TRUE),
                     "corners": list(tn.CORNERS), "grey": tn.GREY}])
    if sql is None:
        print("[SKIP] node unavailable")
        return
    rows = session.db.con.execute(
        "SELECT a, b, c, class, colour FROM (%s)" % sql[0]["rows"]).fetchall()
    wrong = [r for r in rows if r[4] != tn.colour_for(r[0], r[1], r[2], r[3] == "all")]
    check("all %s placed samples: the page's colour is ternary.py's" % format(len(rows), ",d"),
          rows and not wrong, wrong[:3])
    cur = session.db.con.execute(sql[0]["counts"])
    c = dict(zip([d[0] for d in cur.description], cur.fetchone()))
    print("     placed %s: %s all detected, %s with a non-detect; %s unplaceable"
          % tuple(format(c[k], ",d") for k in ("placed", "all_detected", "with_nd",
                                                "unplaceable")))
    check("the counts agree with the rows", c["placed"] == len(rows)
          and c["all_detected"] == sum(1 for r in rows if r[3] == "all"), c)


if __name__ == "__main__":
    pure_checks()
    fixture_checks()
    facility_a_checks()
    print("\n%s" % ("ALL TERNARY TESTS PASSED" if not FAILED else "%d FAILED" % len(FAILED)))
    sys.exit(1 if FAILED else 0)
