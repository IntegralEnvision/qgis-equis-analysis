"""The scatter's SQL, run against real pivots.

web/test/scatter.test.mjs checks the SQL's TEXT. This asks node for the SQL
the page would actually send (web/src/scatter.js) and runs it against a
pivot built by equis_loader/pivot.py - the fixture from test_pivot.py, and
the real Facility A parquet when it is present - so a column that does not
exist, or a class count that disagrees with the pivot, fails here rather
than in a chart that draws something plausible.

    python tools/test_scatter_sql.py

Needs node (build-time) and duckdb. Skips, saying so, without node.
"""
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
from equis_loader.pivot import Variable, build_pivot  # noqa: E402
from equis_loader.store import DETECT_TRUE, ReportStore  # noqa: E402
from equis_loader import bivariate as bv  # noqa: E402
import test_pivot as fixture  # noqa: E402

FAILED = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - %s" % (detail,)) if detail != "" else ""))
    if not condition:
        FAILED.append(label)


GENERATOR = """
import { brushSql, scatterCountsSql, scatterSql } from "./src/scatter.js";
const asks = JSON.parse(process.argv[2]);
const out = asks.map((a) => ({
  shown: scatterSql(a),
  counts: scatterCountsSql(a),
  brush: brushSql("sv_1_shown", a.box || { x0: 0, x1: 1e12, y0: 0, y1: 1e12 }),
}));
console.log(JSON.stringify(out));
"""


def page_sql(asks):
    web = os.path.join(ROOT, "web")
    path = os.path.join(web, "_scattercheck.mjs")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(GENERATOR)
    try:
        if not ensure_node():
            return None
        result = subprocess.run(["node", "_scattercheck.mjs", json.dumps(asks)],
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


def ask(pivot, locations=None, box=None, bivariate=None):
    out = {"pivot": pivot.table, "x": pivot.columns[0]["prefix"],
           "y": pivot.columns[1]["prefix"], "detectTrue": list(DETECT_TRUE)}
    if bivariate is not None:
        out["bivariate"] = bivariate
    if locations is not None:
        out["locations"] = locations
    if box is not None:
        out["box"] = box
    return out


def classes(store, sql):
    rows = store.con.execute(
        "SELECT class, count(*) FROM (%s) GROUP BY class" % sql).fetchall()
    return dict(rows)


def counts(store, sql):
    cur = store.con.execute(sql)
    return dict(zip([d[0] for d in cur.description], cur.fetchone()))


def fixture_checks():
    print("\n-- fixture (tools/test_pivot.py)")
    store = ReportStore.from_table(rd.ReportTable(fixture.HEADERS, fixture.ROWS))
    p = build_pivot(store, [Variable(*fixture.AS), Variable(*fixture.PB)])
    sql = page_sql([ask(p), ask(p, locations=["mw-1"]), ask(p, locations=[])])
    if sql is None:
        return False
    whole, one_loc, nothing = sql
    # S1: both detected; S2: arsenic detected, lead not; S3: no lead
    # (unpaired); S4: arsenic's flag blank (unknown) - see test_pivot.py.
    got = classes(store, whole["shown"])
    check("classes: S1 both, S2 arsenic only", got == {"both": 1, "x_only": 1},
          got)
    c = counts(store, whole["counts"])
    check("counts agree with the drawn rows",
          (c["both"], c["x_only"], c["y_only"], c["neither"]) == (1, 1, 0, 0), c)
    # S3 (no lead) and Q7_R2 (the decoy: arsenic only) carry one analyte each.
    check("S3 and Q7_R2 are unpaired, not drawn", c["unpaired"] == 2, c["unpaired"])
    check("S4's blank flag is unknown, not drawn", c["unknown"] == 1, c["unknown"])
    check("map selection narrows the drawn rows (MW-1: S1, S2)",
          sum(classes(store, one_loc["shown"]).values()) == 2,
          classes(store, one_loc["shown"]))
    check("an empty map selection draws nothing",
          classes(store, nothing["shown"]) == {}, classes(store, nothing["shown"]))
    # The brush query runs over the shown view the page creates.
    store.con.execute("CREATE TEMP VIEW sv_1_shown AS %s" % whole["shown"])
    b = counts(store, whole["brush"])
    check("the brush query runs, and names locations",
          b["samples"] == 2 and sorted(b["codes"]) == ["MW-1"], b)
    return True


def facility_a_checks():
    print("\n-- Facility A (real parquet)")
    root = os.path.join(ROOT, "data", "facility_a")
    if not os.path.isdir(root):
        print("[SKIP] no data/facility_a - the real-data checks did not run")
        return
    store = ReportStore.from_manifest(root)
    p = build_pivot(store, [Variable("Manganese", fraction="T"),
                            Variable("Lead", fraction="T")], matrix=["WS"])
    sql = page_sql([ask(p)])
    if sql is None:
        return
    got = classes(store, sql[0]["shown"])
    # tools/measure_pivot.py, 2026-10-08.
    check("Mn x Pb WS/T: the page's classes match measure_pivot.py",
          (got.get("both"), got.get("x_only"), got.get("y_only"),
           got.get("neither")) == (1573, 19770, 58, 41821), got)
    c = counts(store, sql[0]["counts"])
    check("Facility A: unpaired = rows - complete",
          c["unpaired"] == p.rows - p.complete, (c["unpaired"], p.rows, p.complete))
    check("Facility A: no zero/negative values, so log axes",
          c["nonpositive"] == 0 and c["x_min"] > 0 and c["y_min"] > 0, c)
    colour_parity(store, p, "Facility A Mn x Pb")

    # The test pair since 2026-10-09: soil, where every station is a
    # handful of replicate-heavy sampling events rather than a time series.
    so = build_pivot(store, [Variable("Arsenic"), Variable("Chromium")],
                     matrix=["SO"])
    sql = page_sql([ask(so)])
    if sql is not None:
        got = classes(store, sql[0]["shown"])
        # Field replicates are not plotted (2026-10-09): 836 samples become
        # the 112 primaries; 724 replicates (_R2 ... _R9) are left out.
        check("As x Cr SO: 112 primaries, 109 / 2 / 0 / 1",
              so.rows == 112 and so.excluded["field_replicates"] == 724
              and (got.get("both", 0), got.get("x_only", 0), got.get("y_only", 0),
                   got.get("neither", 0)) == (109, 2, 0, 1), (so.rows, got))
        c = counts(store, sql[0]["counts"])
        # The two As values reported as a DETECTED 0 were both _R2
        # replicates, so with replicates out there is nothing to leave off
        # the log axes.
        check("As x Cr SO: no value of 0 or below among the primaries",
              c["nonpositive"] == 0, c)
    colour_parity(store, so, "Facility A As x Cr SO")
    store.close()


def colour_parity(store, p, label):
    """Every point's colour in the PAGE's SQL equals bivariate.py's answer
    for the same sample. Two implementations of one rule, held together -
    if they drift, a point on the chart and its station on the map are
    different colours."""
    x, y = p.columns[0]["prefix"], p.columns[1]["prefix"]
    breaks = bv.axis_breaks(store.con, p.table, x, y, list(DETECT_TRUE))
    options = {"x": breaks["x"], "y": breaks["y"], "palette": bv.PALETTE,
               "grey": bv.GREY}
    sql = page_sql([ask(p, bivariate=options)])
    if sql is None:
        return
    rows = store.con.execute(
        "SELECT x, y, class, colour FROM (%s)" % sql[0]["shown"]).fetchall()
    wrong = []
    for xv, yv, cls, colour in rows:
        key = bv.key_for(xv, yv, cls in ("both", "x_only"),
                         cls in ("both", "y_only"),
                         breaks["x"]["breaks"], breaks["y"]["breaks"])
        if bv.colour_for(key) != colour:
            wrong.append((xv, yv, cls, colour, bv.colour_for(key)))
    check("%s: page and bivariate.py colour every point alike (%s points)"
          % (label, format(len(rows), ",d")), not wrong and rows, wrong[:3])
    coloured = sum(1 for r in rows if r[3] != bv.GREY)
    check("%s: only both-detected points are coloured" % label,
          coloured == sum(1 for r in rows if r[2] == "both"), coloured)


if __name__ == "__main__":
    if not fixture_checks():
        print("SKIPPED (node unavailable)")
        raise SystemExit(0)
    facility_a_checks()
    print("\n%s" % ("ALL SCATTER SQL TESTS PASSED" if not FAILED
                    else "%d FAILED" % len(FAILED)))
    sys.exit(1 if FAILED else 0)
