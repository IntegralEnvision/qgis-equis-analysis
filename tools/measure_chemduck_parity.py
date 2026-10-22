"""This plugin's per-location answers beside ChemDuck's, on the same parquet.

ChemDuck (../chemduck) holds environmental-chemistry rules as SQL macros, and
Chemrooms runs them in the browser over the SAME EQuIS ARII parquet this
repository's pipeline publishes. Both projects map "the most recent" and "the
maximum" result per location. Nothing held the two to each other until this:
the third copy of the non-detect rules (pipeline/arcgis_export.py) has
tools/test_arcgis_parity.py, ChemDuck had none.

A MEASUREMENT, NOT A GATE. Some differences are design - ChemDuck keeps
depth intervals apart and collapses same-date duplicates; the plugin does
neither - so this reports every difference by cause rather than failing on
the first. Read docs/chemduck-parity.md for what the numbers mean.

    python tools/measure_chemduck_parity.py --manifest data/facility_a/manifest.json \\
        --chemduck ../chemduck --out <dir> [--db <dir>/chemduck.duckdb] [--analytes 40]

It builds a ChemDuck database from the manifest with ChemDuck's own loader
(or reuses --db), opens the plugin's ReportStore on the same manifest, and
for each (analyte, matrix, fraction) and recipe compares, per location:
presence, value, detected, and date. ChemDuck's answer is per depth interval;
it is rolled up to the plugin's per-location grain with the same rule before
comparing, and the locations where that mattered are counted apart.

Read-only towards both repositories. Writes only to --out.
"""
import argparse
import collections
import contextlib
import io
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from equis_loader import report_data as rd  # noqa: E402
from equis_loader.store import ReportStore  # noqa: E402

# (plugin how, ChemDuck event_agg, ChemDuck dup_agg). most_recent/avg/half_dl
# is what both tools show by default (Chemrooms' layerSchema.ts; the plugin's
# panel), so it is the one that says what users see today.
RECIPES = [
    (rd.MOST_RECENT, "most_recent", "avg"),
    (rd.MOST_RECENT, "most_recent", "max"),
    (rd.MAXIMUM, "maximum", "avg"),
    (rd.MAXIMUM, "maximum", "max"),
]
FRACTION_NAME = {"T": "Total", "D": "Dissolved"}   # ChemDuck's mapping


def close(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= 1e-9 * max(abs(a), abs(b), 1.0)


def chemduck_db(args):
    path = args.db or os.path.join(args.out, "chemduck.duckdb")
    if os.path.exists(path):
        return path, None
    sys.path.insert(0, os.path.join(args.chemduck, "tools"))
    from load_data import load_mapping, load_to_duckdb   # ChemDuck's loader
    import arii_hybrid as hy
    began = time.time()
    with contextlib.redirect_stdout(io.StringIO()):
        load_to_duckdb(args.manifest, path, load_mapping(hy.DEFAULT_MAPPING))
    return path, round(time.time() - began, 1)


def rollup(rows, event):
    """ChemDuck's per-interval winners -> one per location, by the same rule
    the event uses. Returns {location: (row, n_intervals, tied)}."""
    by_loc = collections.defaultdict(list)
    for r in rows:
        by_loc[r["location_id"]].append(r)
    out = {}
    for loc, group in by_loc.items():
        if event == "maximum":
            key = lambda r: (r["detected"] is True,
                             (r["result"] if r["detected"] else r["detection_limit"]) or -math.inf)
        else:
            key = lambda r: (str(r["rep_sample_date"] or ""),)
        group.sort(key=key, reverse=True)
        tied = len(group) > 1 and key(group[0]) == key(group[1])
        out[loc] = (group[0], len(group), tied)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--chemduck", default=os.path.join(ROOT, "..", "..", "chemduck"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--db")
    ap.add_argument("--recipes", default="",
                    help="comma-separated event/dup names to run, e.g. most_recent/avg")
    ap.add_argument("--analytes", type=int, default=40,
                    help="the most-measured analytes to compare (0: all)")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    import duckdb

    db_path, load_s = chemduck_db(args)
    cd = duckdb.connect(db_path, read_only=True)
    cd.execute("LOAD spatial")
    store = ReportStore.from_manifest(os.path.dirname(os.path.abspath(args.manifest)))

    combos = store.con.execute("""
        SELECT chemical, matrix, fraction, count(*) AS n
        FROM v_results WHERE coalesce(location, '') <> ''
        GROUP BY ALL ORDER BY n DESC""").fetchall()
    totals = collections.Counter()
    for chem, _m, _f, n in combos:
        totals[chem] += n
    keep = {c for c, _ in totals.most_common(args.analytes or None)}
    combos = [c for c in combos if c[0] in keep]

    report = {"manifest": args.manifest, "chemduck_db": db_path, "chemduck_load_s": load_s,
              "analytes": len(keep), "combos": len(combos), "recipes": {}}
    examples = collections.defaultdict(list)
    began = time.time()
    wanted = {r for r in args.recipes.split(",") if r}
    for how, event, dup in RECIPES:
        name = "%s/%s/half_dl" % (event, dup)
        if wanted and "%s/%s" % (event, dup) not in wanted:
            continue
        ratios = []
        tally = collections.Counter()
        per_analyte = collections.Counter()
        for chem, matrix, fraction, _n in combos:
            plugin = store.aggregate(chem, how=how, non_detect=rd.ND_HALF,
                                     matrix=[matrix], fraction=fraction or rd.ANY_FRACTION)
            frac = FRACTION_NAME.get(fraction, fraction)
            cur = cd.execute(
                "SELECT * FROM aggregate_results(?, ?, NULL, NULL, ?, ?, 'half_dl', 0.0, 1.0,"
                " p_fraction := ?)", [chem, matrix, event, dup, frac])
            names = [d[0] for d in cur.description]
            rows = [dict(zip(names, r)) for r in cur.fetchall()]
            ours = rollup(rows, event)
            for loc in set(plugin) | set(ours):
                p = plugin.get(loc)
                c = ours.get(loc)
                tally["cells"] += 1
                if c is None:
                    tally["only in the plugin"] += 1
                    continue
                if p is None:
                    tally["only in ChemDuck"] += 1
                    continue
                crow, n_intervals, tied = c
                if n_intervals > 1:
                    tally["location has several depth intervals"] += 1
                pv, pd = p["value"], p["detect"] in rd_detect_true()
                cv, cdet = crow["result"], bool(crow["detected"])
                same_value = close(pv, cv)
                same_detect = (pv is None) or (pd == cdet)
                pdate = p["date"].date() if p["date"] is not None else None
                cdate = crow["rep_sample_date"]
                cdate = cdate.date() if hasattr(cdate, "date") else cdate
                same_date = pdate == cdate
                if same_value and same_detect and same_date:
                    tally["agree"] += 1
                    continue
                tally["differ"] += 1
                if pv and cv and pv > 0 and cv > 0 and not same_value:
                    ratios.append(max(cv / pv, pv / cv))
                tally["differs in: " + ("value" if not same_value
                                        else "detected" if not same_detect
                                        else "date only")] += 1
                per_analyte[chem] += 1
                cause = classify(store, chem, matrix, fraction, loc, p, crow, tied,
                                 n_intervals, event)
                tally["cause: " + cause] += 1
                if len(examples[name + " | " + cause]) < 3:
                    examples[name + " | " + cause].append({
                        "analyte": chem, "matrix": matrix, "fraction": fraction,
                        "location": loc,
                        "plugin": {"value": pv, "detected": pd, "date": str(pdate),
                                   "at_limit": p["at_limit"]},
                        "chemduck": {"value": cv, "detected": cdet, "date": str(cdate),
                                     "n_events": crow["n_events"], "intervals": n_intervals},
                        "ratio": (cv / pv) if pv and cv else None})
        ratios.sort()
        pick = lambda q: round(ratios[min(len(ratios) - 1, int(q * len(ratios)))], 3) if ratios else None
        report["recipes"][name] = {"tally": dict(tally),
                                   "value_ratio": {"n": len(ratios), "median": pick(0.5),
                                                   "p90": pick(0.9), "p99": pick(0.99),
                                                   "max": round(ratios[-1], 1) if ratios else None,
                                                   "over_2x": sum(r > 2 for r in ratios),
                                                   "over_10x": sum(r > 10 for r in ratios)},
                                   "worst_analytes": per_analyte.most_common(8)}
        print("%-28s %s" % (name, dict(tally)), flush=True)
    report["seconds"] = round(time.time() - began, 1)
    report["examples"] = examples
    report["inputs"] = input_facts(store)
    report["units_conflicts"] = cd.execute("""
        SELECT count(*) FROM (SELECT location_id, matrix, fraction, analyte
            FROM v_results_denormalized GROUP BY ALL HAVING count(DISTINCT units) > 1)""").fetchone()[0]
    with open(os.path.join(args.out, "chemduck_parity.json"), "w", encoding="utf-8") as h:
        json.dump(report, h, indent=1, default=str)
    print("wrote", os.path.join(args.out, "chemduck_parity.json"))


def rd_detect_true():
    from equis_loader.store import DETECT_TRUE
    return set(DETECT_TRUE)


def classify(store, chem, matrix, fraction, loc, p, crow, tied, n_intervals, event):
    """Why a cell differs: the first cause that explains it, from the raw rows.

    Checked on BOTH deciding dates - the plugin's winner's and ChemDuck's -
    because averaging can move ChemDuck's winner to another day entirely
    (Iron at one station: 2,400 on the plugin's date was averaged down, and
    a later date won).
    """
    where, params = store._where(chemical=chem, matrix=[matrix],
                                 fraction=fraction or rd.ANY_FRACTION, location=loc)
    rows = store.con.execute(
        "SELECT CAST(sample_date AS DATE) d, detected, value_half, start_depth,"
        " end_depth, sample, method FROM v_results WHERE " + where, params).fetchall()
    if p["value"] is None or (crow["result"] is not None and any(
            r[2] is None and not r[1] for r in rows)):
        # No REPORT_RESULT_VALUE and no REPORT_METHOD_DETECTION_LIMIT: the
        # plugin has no number; ChemDuck's chain reaches the quantitation limit.
        return "a non-detect only ChemDuck can value (quantitation limit)"
    cdate = crow["rep_sample_date"]
    cdate = cdate.date() if hasattr(cdate, "date") else cdate
    pdate = p["date"].date() if p["date"] is not None else None
    for day in (pdate, cdate):
        on_day = [r for r in rows if r[0] == day]
        by_interval = collections.defaultdict(list)
        for r in on_day:
            by_interval[(r[3], r[4])].append(r)
        crowded = [g for g in by_interval.values() if len(g) > 1]
        if crowded:
            g = max(crowded, key=len)
            mixed = len({r[1] for r in g}) > 1
            what = ("one sample, several methods or tests" if len({r[5] for r in g}) == 1
                    else "several samples")
            if mixed:
                return "same date and depth, %s, detect and non-detect combined" % what
            return "same date and depth, %s, combined" % what
    if event == "most_recent" and len({(r[3], r[4]) for r in rows if r[0] == cdate}) > 1:
        return "a depth profile: one date, several depths"
    if close(p["value"], crow["result"]) and pdate != cdate:
        return "equal values on different dates: the tie broken differently"
    return "unexplained"


def input_facts(store):
    con = store.con
    return dict(zip(
        ("rows", "detects", "non_detects", "non_detects_without_a_number"),
        con.execute("""SELECT count(*), count(*) FILTER (WHERE detected),
                              count(*) FILTER (WHERE NOT detected),
                              count(*) FILTER (WHERE NOT detected AND value_half IS NULL)
                       FROM v_results""").fetchone()))


if __name__ == "__main__":
    main()
