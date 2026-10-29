"""The histogram's SQL, run against a real store.

`web/test/histogram.test.mjs` asserts on the SQL's TEXT, which catches a
wrong column name only if someone already knew it was wrong. This asks node
for the SQL the page would actually send and executes it against a real
ReportStore, so a column that does not exist fails here rather than in a
chart that quietly draws nothing.

Needs node (build-time already) and duckdb.

    python tools/test_histogram_sql.py
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

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


try:
    import duckdb  # noqa: F401
except ImportError as exc:
    print("duckdb is not importable: %s" % exc)
    print("SKIPPED")
    raise SystemExit(0)

from equis_loader.report_data import ReportTable  # noqa: E402
from equis_loader.store import ReportStore  # noqa: E402

HEADERS = ["SYS_LOC_CODE", "CHEMICAL_NAME", "CAS_RN", "RESULT_NUMERIC",
           "REPORT_RESULT_VALUE", "REPORTING_DETECTION_LIMIT", "SAMPLE_DATE",
           "RESULT_UNIT", "DETECT_FLAG", "MATRIX_CODE", "FRACTION",
           "ANALYTIC_METHOD", "SAMPLE_TYPE_CODE"]

# Every fourth row is a non-detect: RESULT_NUMERIC empty, the limit carried
# instead. That is how EQuIS actually reports them, and it is what makes the
# three value_* columns differ.
# SEVERAL ROWS PER LOCATION, on different dates and with different values.
# A fixture with one row each would let a broken aggregation pass: picking
# "the maximum" from a single row is right by accident.
ROWS = []
for index in range(1, 41):
    detected = index % 4 != 0
    location = "MW-%d" % (((index - 1) % 10) + 1)      # 10 locations
    year = 2018 + (index % 3)                           # three sample years
    ROWS.append([
        location, "Nickel", "7440-02-0",
        str(index * 1.5) if detected else "",
        str(index * 1.5) if detected else "0.5",
        "0.5", "6/%d/%d" % ((index % 28) + 1, year), "mg/kg",
        "Y" if detected else "N",
        "SO", "T", "M1", "N",
    ])

GENERATOR = """
import { histogramSql } from "./src/histogram.js";
const out = [];
for (const nd of ["value_half", "value_full", "value_exclude"]) {
  out.push({ nd, sql: histogramSql({
    chemical: "Nickel", casRn: "7440-02-0", matrix: "SO", fraction: "T",
    ndColumn: nd }) });
}
out.push({ nd: "by-name", sql: histogramSql({ chemical: "Nickel" }) });
out.push({ nd: "quoted", sql: histogramSql({ chemical: "O'Brien" }) });
for (const mode of ["most_recent", "maximum"]) {
  out.push({ nd: mode, sql: histogramSql({
    chemical: "Nickel", casRn: "7440-02-0", aggregate: mode }) });
}
// A chemical is REQUIRED. Without one the only predicate is "has a
// value", which over a whole-facility parquet is 3,400,302 rows - 2.9 s
// to query, and then the same rows crossing the bridge as Arrow to be
// binned in the browser. The histogram is a view over a filter.
try {
  histogramSql({ matrix: "SO" });
  out.push({ nd: "no-chemical", sql: "DID NOT THROW" });
} catch (err) {
  out.push({ nd: "no-chemical", sql: "threw: " + err.message });
}
console.log(JSON.stringify(out));
"""


def generated_sql():
    web = os.path.join(ROOT, "web")
    path = os.path.join(web, "_sqlcheck.mjs")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(GENERATOR)
    try:
        ensure_node()
        result = subprocess.run(["node", "_sqlcheck.mjs"], cwd=web,
                                capture_output=True, text=True)
    except (OSError, FileNotFoundError):
        # Node is a BUILD-time dependency and is deliberately absent from a
        # plain QGIS install, so this is a skip rather than a failure.
        print("node is not on PATH; it is build-time only")
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if result.returncode:
        print("node failed: %s" % result.stderr[:400])
        return None
    return json.loads(result.stdout.strip())


cases = generated_sql()
if cases is None:
    print("SKIPPED (node unavailable or the module would not load)")
    raise SystemExit(0)

store = ReportStore.from_table(ReportTable(HEADERS, ROWS))
results = {}
# The no-chemical entry carries a message rather than SQL - it is asserted
# after the loop, not executed in it.
refusal = next((c["sql"] for c in cases if c["nd"] == "no-chemical"), "")
for case in [c for c in cases if c["nd"] != "no-chemical"]:
    try:
        rows = store.con.execute(case["sql"]).fetchall()
    except Exception as exc:  # noqa: BLE001 - that IS the failure
        check("%s runs against the store" % case["nd"], False,
              "%s: %s" % (type(exc).__name__, exc))
        continue
    results[case["nd"]] = rows
    check("%s runs against the store" % case["nd"], True,
          "%d rows" % len(rows))

# 40 rows, every fourth a non-detect -> 10 non-detects.
half = results.get("value_half") or []
check("half and full keep the non-detects",
      len(half) == 40 and sum(1 for row in half if not row[1]) == 10,
      "%d rows, %d non-detect" % (len(half), sum(1 for r in half if not r[1])))

exclude = results.get("value_exclude") or []
check("exclude drops them, and only them",
      len(exclude) == 30 and all(row[1] for row in exclude),
      "%d rows" % len(exclude))

full = results.get("value_full") or []
if half and full:
    nd_half = next(row[0] for row in half if not row[1])
    nd_full = next(row[0] for row in full if not row[1])
    check("half is half of full for a non-detect",
          abs(nd_half * 2 - nd_full) < 1e-9,
          "%s vs %s" % (nd_half, nd_full))

by_name = results.get("by-name") or []
check("the name-only filter finds the same analyte", len(by_name) == 40,
      "%d rows" % len(by_name))

check("a quoted analyte name runs rather than erroring",
      "quoted" in results, "an apostrophe must not break the SQL")

# THE POINT OF THE AGGREGATION MODES: one row per location, and the same
# row the panel's own aggregate() would pick. A histogram that bins a
# different population from the map is the failure the decision of
# 2026-09-23 exists to prevent.
locations = len({row[0] for row in ROWS})
for mode in ("most_recent", "maximum"):
    got = results.get(mode) or []
    check("%s gives one row per location" % mode,
          len(got) == locations,
          "%d rows for %d locations" % (len(got), locations))

# Against the store's own answer, which is what the map draws.
summary = store.aggregate("Nickel", "7440-02-0", how="maximum")
by_location = {row[2]: row[0] for row in (results.get("maximum") or [])}
agree = all(abs(by_location.get(code, float("nan"))
                - entry["value"]) < 1e-9
            for code, entry in summary.items()
            if entry.get("value") is not None)
check("maximum picks the same values as store.aggregate",
      agree and len(by_location) == len(summary),
      "%d vs %d locations" % (len(by_location), len(summary)))

recent_summary = store.aggregate("Nickel", "7440-02-0", how="most_recent")
recent_rows = {row[2]: row[0] for row in (results.get("most_recent") or [])}
agree_recent = all(abs(recent_rows.get(code, float("nan"))
                       - entry["value"]) < 1e-9
                   for code, entry in recent_summary.items()
                   if entry.get("value") is not None)
check("most_recent picks the same values as store.aggregate",
      agree_recent and len(recent_rows) == len(recent_summary),
      "%d vs %d locations" % (len(recent_rows), len(recent_summary)))

check("binning the whole store is refused, not attempted",
      refusal.startswith("threw:"), refusal[:80])


# --- THE FILTERS SELECT THE STORE'S ROWS, under every combination ---------
# The page writes its own WHERE over v_results, a second copy of the
# matrix / fraction / method filters, and it drifted (found 2026-10-28): it
# compared codes as typed where store._code_filter compares upper-case; the
# host sent only the FIRST matrix, fraction and method of a layer, so a
# sediment-and-tailings layer was binned over sediment alone; and a filter
# on a column the source lacks dropped every row (the view carries it as
# NULL), where CLAUDE.md's rule is that it matches everything. The host now
# sends ReportStore.filter_codes for each, as dialog._chart_scope does here.
# Before the fix: 288 of 864 combinations differed on the parity fixture,
# 840 of 864 with no matrix, fraction or method column.
import itertools  # noqa: E402

from equis_loader import report_data as rd  # noqa: E402
from equis_loader.store import ND_COLUMN  # noqa: E402
import test_store_parity as parity  # noqa: E402

COMBINATIONS = """
import { readFileSync } from "node:fs";
import { histogramSql } from "./src/histogram.js";
// On stdin: every combination is past Windows' command-line limit.
const asks = JSON.parse(readFileSync(0, "utf-8"));
console.log(JSON.stringify(asks.map((a) => histogramSql(a))));
"""


def combination_sql(asks):
    web = os.path.join(ROOT, "web")
    path = os.path.join(web, "_sqlcombos.mjs")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(COMBINATIONS)
    try:
        result = subprocess.run(["node", "_sqlcombos.mjs"], input=json.dumps(asks),
                                cwd=web, capture_output=True, text=True)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if result.returncode:
        raise RuntimeError("node failed: %s" % result.stderr[:600])
    return json.loads(result.stdout.strip())


def filters_match_store(label, headers, rows):
    table = ReportTable(headers, rows)
    combo_store = ReportStore.from_table(table)
    codes = combo_store.filter_codes
    analytes = [(a["chemical"], a["cas_rn"]) for a in table.analytes()]
    combos = list(itertools.product(
        analytes[:4],
        (rd.ALL_MATRICES, "WG", "wg", ("WG", "SO"), ("so", "WG"), ()),
        (rd.ANY_FRACTION, "D", ("D", "T"), ("t",)),
        (rd.ALL_METHODS, "M1", ("m1", "ICM40B")),
        (rd.ND_HALF, rd.ND_FULL, rd.ND_EXCLUDE)))
    asks = [dict(chemical=c, casRn=k or None, matrix=codes("matrix", m),
                 fraction=codes("fraction", f), method=codes("method", me),
                 ndColumn=ND_COLUMN.get(nd, "value_half"))
            for (c, k), m, f, me, nd in combos]
    bad = []
    for ((chemical, cas), matrix, fraction, method, nd), sql in zip(
            combos, combination_sql(asks)):
        got = sorted(r[0] for r in combo_store.con.execute(
            "SELECT row_id FROM (%s)" % sql).fetchall())
        # The rows the PYTHON implementation's filters keep, by position (the
        # store's row_id): independent of filter_codes, which the store's own
        # _where calls - a predicate taken from there moved with the rule
        # under test and let a broken filter_codes pass.
        want = [i for i, rw in enumerate(table.rows)
                if table._matches(rw, chemical, cas)
                and table._in_matrix(rw, matrix)
                and table._in_fraction(rw, fraction)
                and table._in_method(rw, method)
                and table.row_value(rw, non_detect=nd)[0] is not None]
        if got != want:
            bad.append((chemical, matrix, fraction, method, nd, len(want), len(got)))
    check("%s: the filters select the store's rows, %d combinations"
          % (label, len(combos)), not bad,
          "" if not bad else "%d differ; first (chemical, matrix, fraction, "
          "method, nd, store rows, histogram rows): %r" % (len(bad), bad[0]))
    combo_store.close()


filters_match_store("parity fixture", parity.FULL_HEADERS, parity.awkward_rows())
absent = [i for i, h in enumerate(parity.FULL_HEADERS)
          if h not in ("MATRIX_CODE", "FRACTION", "ANALYTIC_METHOD")]
filters_match_store("no matrix/fraction/method columns",
                    [parity.FULL_HEADERS[i] for i in absent],
                    [[r[i] for i in absent] for r in parity.awkward_rows()])

print()
if FAILURES:
    print("FAILED: %s" % ", ".join(FAILURES))
    raise SystemExit(1)
print("The histogram's SQL runs against the store and respects non-detects.")
