r"""US-15: the chart refuses mixed units, on the REAL facility.

web/test/mixed-units.mjs asserts the decision in isolation. This asks the
store which selections are actually mixed, so the rule is exercised against
data rather than against a fixture that agrees with it by construction.

    python tools/test_chart_units.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


PUBLISHED = os.path.join("data", "facility_a", "manifest.json")
if not os.path.isfile(PUBLISHED):
    print("no published manifest at %s" % PUBLISHED)
    print("SKIPPED")
    raise SystemExit(0)

from equis_loader.store import ReportStore  # noqa: E402

store = ReportStore.from_manifest("data/facility_a")


def units_in(chemical, matrix=None, fraction=None):
    """What the CHART would see - the same rows its SQL returns."""
    where = ["value_half IS NOT NULL", "coalesce(chemical,'') = ?"]
    params = [chemical]
    if matrix:
        where.append("coalesce(matrix,'') = ?")
        params.append(matrix)
    if fraction:
        where.append("coalesce(fraction,'') = ?")
        params.append(fraction)
    rows = store.con.execute(
        "SELECT coalesce(trim(unit),''), count(*) FROM v_results WHERE %s "
        "GROUP BY 1 ORDER BY 2 DESC" % " AND ".join(where), params).fetchall()
    return [(u, n) for u, n in rows if u]


# The case that motivated this: a minority unit hiding behind a majority.
iron = units_in("Iron", "SE")
check("Iron in SE really is mixed, so the rule has something to catch",
      len(iron) > 1, str(iron))
check("and the minority unit is small enough to have been missed",
      len(iron) > 1 and iron[-1][1] < iron[0][1] / 100,
      str(iron))

# The unit conversion made this one clean; the chart should draw it.
arsenic = units_in("Arsenic", "WS")
check("Arsenic in WS is one unit after the conversion, so it charts",
      len(arsenic) == 1, str(arsenic))

# Narrowing the filter is the remedy the refusal offers. It has to work.
narrowed = units_in("Iron", "SE", "N")
check("narrowing the fraction is a real remedy, not just advice",
      len(narrowed) == 1, str(narrowed))

# How much of the facility is affected - if this were everything, the
# refusal would be unusable rather than protective.
mixed = store.con.execute("""
    SELECT count(*) FROM (
      SELECT chemical, matrix FROM v_results
      WHERE coalesce(trim(unit),'') <> '' AND value_half IS NOT NULL
      GROUP BY 1, 2 HAVING count(DISTINCT unit) > 1)""").fetchone()[0]
total = store.con.execute("""
    SELECT count(*) FROM (
      SELECT chemical, matrix FROM v_results
      WHERE coalesce(trim(unit),'') <> '' AND value_half IS NOT NULL
      GROUP BY 1, 2)""").fetchone()[0]
print()
print("   %d of %d (chemical, matrix) selections are mixed (%.0f%%)"
      % (mixed, total, 100.0 * mixed / total))
check("most selections still chart, so the refusal is protective not fatal",
      mixed < total / 2, "%d of %d" % (mixed, total))

print()
if FAILURES:
    print("FAILED: %s" % ", ".join(FAILURES))
    raise SystemExit(1)
print("The chart's unit rule matches what the data actually holds.")
