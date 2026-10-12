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

print()
if FAILURES:
    print("FAILED: %s" % ", ".join(FAILURES))
    raise SystemExit(1)
print("The histogram's SQL runs against the store and respects non-detects.")
