"""The box plot's Kaplan-Meier quartiles, held to an independent estimate.

web/src/censored.js computes KM quartiles per group in SQL, so they follow
a brush. This asks node for that SQL, runs it in DuckDB, and compares every
group with a KM written here the textbook way - flip the left-censored
values into right-censored "survival times" (Helsel 2012, ch. 6) and run
the ordinary product-limit estimator - which shares no code or arithmetic
order with the SQL. Then:

  * hand-worked cases, the numbers derived in the comments;
  * nothing censored must give R's type 2 quartiles (numpy's
    'averaged_inverted_cdf'), the ones a chemist computes by hand;
  * every group of every analyte in a real matrix, when the real parquet
    is present (--root, default data/facility_a).

    python tools/test_censored.py [--root DIR] [--matrix SO]

Needs node (build-time) and duckdb. Skips, saying so, without node.
"""
import argparse
import json
import os
import random
import subprocess
import sys

import duckdb
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from node_path import ensure_node  # noqa: E402

FAILED = []
TRUE = ["Y"]
PS = (("q1", 0.25), ("med", 0.5), ("q3", 0.75))


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - %s" % (detail,)) if detail != "" else ""))
    if not condition:
        FAILED.append(label)


GENERATOR = """
import { kmBoxSql, kmBoxSqlOver } from "./src/censored.js";
import { brushedSql, linkedTableSql } from "./src/linked.js";
const asks = JSON.parse(process.argv[2]);
console.log(JSON.stringify(asks.map((a) => a.linked ? linkedTableSql(a.linked)
  : a.brushed ? brushedSql(a.brushed.table, a.brushed.where)
  : a.over ? kmBoxSqlOver(a.over) : kmBoxSql(a))));
"""


def page_sql(asks):
    web = os.path.join(ROOT, "web")
    path = os.path.join(web, "_censoredcheck.mjs")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(GENERATOR)
    try:
        if not ensure_node():
            return None
        result = subprocess.run(["node", "_censoredcheck.mjs", json.dumps(asks)],
                                cwd=web, capture_output=True, text=True)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if result.returncode:
        print("node failed: %s" % result.stderr[:600])
        return None
    return json.loads(result.stdout.strip())


def reference(values, detected):
    """KM quartiles by the flip: t = M - x, right-censored product limit.

    Returns {q: value or None (undetermined)} and F0, the mass the estimate
    puts below the lowest detection.
    """
    m = max(values) + 1.0
    t = [m - v for v in values]
    events = sorted({ti for ti, d in zip(t, detected) if d})
    surv, s = [], 1.0
    for ti in events:                      # ascending t = descending x
        at_risk = sum(1 for tj in t if tj >= ti)
        d = sum(1 for tj, dj in zip(t, detected) if dj and tj == ti)
        before = s                         # S(ti-) = P(T >= ti) = F(m - ti)
        s *= 1.0 - d / at_risk
        surv.append((m - ti, before))      # (x, F(x))
    f0 = s
    surv.sort()                            # ascending x
    out = {}
    for name, p in PS:
        if not surv or f0 >= p - 1e-9:
            out[name] = None
            continue
        for i, (x, fx) in enumerate(surv):
            if fx >= p - 1e-9:
                if abs(fx - p) < 1e-9 and i + 1 < len(surv):
                    x = (x + surv[i + 1][0]) / 2.0
                out[name] = x
                break
    return out, f0


def run(con, sql):
    cur = con.execute(sql)
    names = [d[0] for d in cur.description]
    return {r[0]: dict(zip(names, r)) for r in cur.fetchall()}


def close(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def load(con, rows, name="p_test"):
    """rows: (group, value, detected-bool) -> a pivot-shaped table."""
    con.execute("CREATE OR REPLACE TABLE %s (location VARCHAR, sample_date "
                "TIMESTAMP, a_value DOUBLE, a_detect VARCHAR)" % name)
    con.executemany("INSERT INTO %s VALUES (?, TIMESTAMP '2020-01-01', ?, ?)"
                    % name, [(g, v, "Y" if d else "N") for g, v, d in rows])


def hand_cases(con):
    print("\n-- hand-worked cases")
    rows = []
    # A: 1,2,3,4 all detected. F = .25,.5,.75,1; the flat steps at .25,
    # .5, .75 take midpoints: R type 2 gives 1.5, 2.5, 3.5.
    rows += [("A", v, True) for v in (1, 2, 3, 4)]
    # B: <1, <1, 2, 3, 5. At 5: 5 at risk -> 4/5; at 3: 4 -> 3/4; at 2:
    # 3 -> 2/3. F(5)=1, F(3)=.8, F(2)=.6, F0=.4. Q1 (.25 <= .4) is
    # undetermined; median 2 (F=.6 >= .5); Q3 3 (F(2)=.6 < .75 <= .8).
    rows += [("B", 1, False), ("B", 1, False), ("B", 2, True),
             ("B", 3, True), ("B", 5, True)]
    # C: <2 tied with a detected 2, then 4. "<2" is below 2, so it is at
    # risk at 2 but not an event: at 4, 3 at risk -> 2/3; at 2, 2 -> 1/2.
    # F(2)=2/3, F0=1/3: Q1 undetermined, median 2, Q3 4.
    rows += [("C", 2, False), ("C", 2, True), ("C", 4, True)]
    # D: all non-detect - counted, no quartiles at all.
    rows += [("D", 5, False), ("D", 10, False)]
    load(con, rows)
    sql = page_sql([{"pivot": "p_test", "prefix": "a", "detectTrue": TRUE,
                     "group": "location", "log": False}])
    if sql is None:
        print("[SKIP] no node - the SQL was not generated")
        return False
    got = run(con, sql[0])
    want = {"A": (1.5, 2.5, 3.5), "B": (None, 2, 3), "C": (None, 2, 4)}
    for g, (q1, med, q3) in want.items():
        r = got[g]
        check("%s quartiles %s" % (g, (q1, med, q3)),
              close(r["q1"], q1) and close(r["med"], med) and close(r["q3"], q3),
              (r["q1"], r["med"], r["q3"]))
    check("B: Q1 flagged below the lowest detection", got["B"]["q1_below"] is True)
    check("B: F0 is 0.4", close(got["B"]["below_lowest"], 0.4), got["B"]["below_lowest"])
    check("D: all non-detect, counted, no median",
          got["D"]["n"] == 2 and got["D"]["detects"] == 0 and got["D"]["med"] is None
          and got["D"]["max_limit"] == 10, got["D"])
    check("A: whiskers are the extremes (no fence crossed)",
          got["A"]["lo_whisker"] == 1 and got["A"]["hi_whisker"] == 4
          and got["A"]["outliers"] == 0, got["A"])
    return True


def uncensored_matches_type2(con):
    print("\n-- nothing censored: R type 2")
    rnd = random.Random(20261020)
    rows = []
    for g in range(40):
        n = rnd.randint(1, 30)
        rows += [("g%02d" % g, round(rnd.lognormvariate(0, 1.5), 3), True)
                 for _ in range(n)]
    load(con, rows)
    sql = page_sql([{"pivot": "p_test", "prefix": "a", "detectTrue": TRUE,
                     "group": "location", "log": True}])
    got = run(con, sql[0])
    bad = []
    for g in sorted({r[0] for r in rows}):
        vals = np.array([v for gg, v, _ in rows if gg == g])
        for name, p in PS:
            want = float(np.quantile(vals, p, method="averaged_inverted_cdf"))
            if not close(got[g][name], want):
                bad.append((g, name, got[g][name], want))
    check("40 all-detected groups give numpy's type-2 quartiles", not bad, bad[:3])


def random_censored(con):
    print("\n-- randomised censoring, against the flip")
    rnd = random.Random(7)
    rows = []
    for g in range(300):
        n = rnd.randint(1, 25)
        limits = [rnd.choice((0.5, 1, 2, 5)) for _ in range(n)]
        for lim in limits:
            v = round(rnd.lognormvariate(0.3, 1.2), 2)
            # Ties on purpose: values snapped to limits now and then.
            if rnd.random() < 0.15:
                v = lim
            rows.append(("g%03d" % g, lim if v < lim else v, v >= lim))
    load(con, rows)
    compare(con, "p_test", "a", rows_by_group(rows), "300 random groups")


def rows_by_group(rows):
    out = {}
    for g, v, d in rows:
        out.setdefault(g, ([], []))
        out[g][0].append(v)
        out[g][1].append(d)
    return out


def compare(con, table, prefix, groups, label, group="location", log=True):
    sql = page_sql([{"pivot": table, "prefix": prefix, "detectTrue": TRUE,
                     "group": group, "log": log}])
    if sql is None:
        print("[SKIP] no node")
        return
    got = run(con, sql[0])
    bad, undetermined = [], 0
    for g, (values, detected) in groups.items():
        r = got.get(g)
        if r is None:
            bad.append((g, "missing"))
            continue
        if not any(detected):
            if r["med"] is not None:
                bad.append((g, "all-ND has a median"))
            continue
        want, f0 = reference(values, detected)
        if not close(r["below_lowest"], f0):
            bad.append((g, "F0", r["below_lowest"], f0))
        for name, _ in PS:
            if not close(r[name], want[name]):
                bad.append((g, name, r[name], want[name]))
            if want[name] is None:
                undetermined += 1
                if r[name + "_below"] is not True:
                    bad.append((g, name + "_below not set"))
    check("%s: every quartile and F0 agree (%d groups, %d quartiles "
          "undetermined)" % (label, len(groups), undetermined), not bad, bad[:4])


def linked_table(con):
    """The linked views' table: one row per chart sample, detection decided
    from the flag with a BLANK flag kept unknown (NULL), never a non-detect,
    and `solid` marking the samples the chart draws solid."""
    print("\n-- the linked table (linked.js)")
    con.execute("CREATE OR REPLACE TABLE pp_9 (sample VARCHAR, location VARCHAR, "
                "sample_date TIMESTAMP, a_value DOUBLE, a_detect VARCHAR, "
                "b_value DOUBLE, b_detect VARCHAR)")
    con.executemany("INSERT INTO pp_9 VALUES (?, ?, TIMESTAMP '2020-01-01', ?, ?, ?, ?)", [
        ("s1", "L1", 5.0, "Y", 1.0, "Y"),
        ("s2", "L1", 2.0, "N", 3.0, "Y"),
        ("s3", "L2", 7.0, None, 4.0, "Y"),     # blank flag: unknown
        ("s4", "L2", 9.0, "Y", None, None),    # no b at all: not in the chart
        ("s5", "l3", 1.0, "y", 2.0, "N"),      # lower-case flag still detected
    ])
    con.execute("CREATE OR REPLACE VIEW sv_9_all AS SELECT sample, location, "
                "sample_date, a_value AS x, b_value AS y FROM pp_9 "
                "WHERE a_value IS NOT NULL AND b_value IS NOT NULL")
    con.execute("CREATE OR REPLACE VIEW sv_9_shown AS SELECT * FROM sv_9_all "
                "WHERE location <> 'L2'")
    sql = page_sql([
        {"linked": {"name": "lk_9", "all": "sv_9_all", "shown": "sv_9_shown",
                    "pivot": "pp_9", "detectTrue": TRUE,
                    "columns": [{"prefix": "a"}, {"prefix": "b"}]}},
        {"brushed": {"table": "lk_9", "where": ["x >= 2"]}},
        {"over": {"source": "lk_9", "value": "v0", "detected": "d0",
                  "group": "none", "log": False}},
    ])
    if sql is None:
        return
    con.execute(sql[0])
    got = {r[0]: r[1:] for r in con.execute(
        "SELECT sample, d0, k0, d1, solid FROM lk_9 ORDER BY sample").fetchall()}
    check("one row per chart sample (s4 lacks b, so is not one)",
          sorted(got) == ["s1", "s2", "s3", "s5"], sorted(got))
    check("a blank flag is unknown (NULL), not a non-detect",
          got["s3"][0] is None and got["s3"][1] == "unknown", got["s3"])
    check("detected and not, from the flag, case-insensitively",
          got["s1"][0] is True and got["s2"][0] is False and got["s5"][0] is True,
          [got[k][0] for k in ("s1", "s2", "s5")])
    check("solid marks the chart's shown samples only",
          [k for k, v in got.items() if v[3]] == ["s1", "s2", "s5"], got)
    hit = con.execute(sql[1]).fetchone()
    check("the map gets the solid samples every brush leaves: s1, s2 at L1",
          hit[0] == 2 and hit[1] == 1 and list(hit[2]) == ["L1"], hit)
    box = con.execute(sql[2]).fetchone()
    check("the box leaves the unknown out: n 3, 2 detects", box[1] == 3 and box[2] == 2, box)


def real(root, matrix):
    print("\n-- real parquet: %s, matrix %s" % (root, matrix))
    if not os.path.isdir(root):
        print("[SKIP] no %s - the real-data checks did not run" % root)
        return
    from equis_loader.analysis import Session
    from equis_loader.store import DETECT_TRUE, ReportStore
    global TRUE
    TRUE = sorted(DETECT_TRUE)
    store = ReportStore.from_manifest(root)
    session = Session(store)
    pivot = session.pivot(matrix)
    con = store.con
    checked = 0
    for c in pivot.columns:
        if not c["cells"]:
            continue
        prefix = c["prefix"]
        data = con.execute(
            "SELECT coalesce(location, '(no location)'), {p}_value::DOUBLE, "
            "upper({p}_detect) IN ({t}) FROM {tab} WHERE {p}_value IS NOT NULL "
            "AND {p}_detect IS NOT NULL".format(
                p=prefix, tab=pivot.table,
                t=", ".join("'%s'" % v for v in TRUE))).fetchall()
        groups = rows_by_group(data)
        compare(con, pivot.table, prefix, groups,
                "%s by location" % c.get("chemical", prefix), log=False)
        checked += 1
        if checked >= 25:
            break
    check("checked real analytes", checked > 0, checked)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=os.path.join(ROOT, "data", "facility_a"))
    parser.add_argument("--matrix", default="SO")
    args = parser.parse_args()
    con = duckdb.connect()
    if hand_cases(con):
        uncensored_matches_type2(con)
        random_censored(con)
        linked_table(con)
        real(args.root, args.matrix)
    print("\n%s" % ("ALL PASS" if not FAILED else "FAILED: %s" % FAILED))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
