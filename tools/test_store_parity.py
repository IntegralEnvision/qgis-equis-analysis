"""Hold ReportTable and ReportStore to identical answers.

This is the gate on R2 of docs/vision-linked-views.md. Most of
report_data.py is domain judgement rather than data structure, and the whole
risk of moving it into SQL is that a rule is quietly dropped: a non-detect
that stops being a non-detect, a tie that breaks the other way, a filter on a
missing column that starts excluding every row. None of those look like bugs
in isolation - they look like slightly different numbers.

So every question is asked of both implementations and the answers must be
equal. The old path does not come out until this passes.

No QGIS and no network:

    python tools\\test_store_parity.py

It needs duckdb, from the plugin's bundle or from the environment; run
tools\\vendor_deps.py first if the import fails.
"""

import datetime
import itertools
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from equis_loader import report_data as rd          # noqa: E402
from equis_loader import store as st                # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


def same(label, left, right):
    ok = left == right
    check(label, ok, "" if ok else "\n      python: %r\n      sql   : %r"
          % (left, right))
    return ok


# --------------------------------------------------------------- fixtures

FULL_HEADERS = [
    "SYS_LOC_CODE", "CHEMICAL_NAME", "CAS_RN", "SAMPLE_DATE",
    "RESULT_NUMERIC", "REPORT_RESULT_VALUE", "REPORT_METHOD_DETECTION_LIMIT",
    "RESULT_UNIT", "DETECT_FLAG", "MATRIX_CODE", "FRACTION",
    "ANALYTIC_METHOD", "SAMPLE_TYPE_CODE", "START_DEPTH", "END_DEPTH",
    "DEPTH_UNIT", "CUSTOM_FIELD_1",
]


def row(loc, chem, cas, date, numeric, reported, limit, unit, detect,
        matrix="WG", fraction="N", method="M1", stype="N",
        d0="", d1="", dunit="", custom=""):
    return [loc, chem, cas, date, numeric, reported, limit, unit, detect,
            matrix, fraction, method, stype, d0, d1, dunit, custom]


def awkward_rows():
    """The cases that actually distinguish the two implementations.

    Every line here exists because getting it wrong would produce a plausible
    number rather than an error.
    """
    return [
        # Ordinary detections at one location, so most-recent has to choose.
        row("MW-01", "Benzene", "71-43-2", "01/05/2020", "5.0", "5.0", "0.5",
            "ug/L", "Y"),
        row("MW-01", "Benzene", "71-43-2", "01/05/2021", "7.5", "7.5", "0.5",
            "ug/L", "Y"),
        # Same date as the row above: the tie must break the same way, which
        # is "keep the one that arrived first".
        row("MW-01", "Benzene", "71-43-2", "01/05/2021", "9.9", "9.9", "0.5",
            "ug/L", "Y"),
        # A non-detect: RESULT_NUMERIC empty, limit carried instead.
        row("MW-01", "Benzene", "71-43-2", "01/05/2022", "", "0.5", "0.5",
            "ug/L", "N"),
        # A NON-DETECT AT A HIGH LIMIT, which on value alone would win the
        # maximum: 100/2 = 50 against the 9.9 detection above. It must not.
        # A non-detect carries only the limit it fell BELOW, so ranking it
        # as a measurement reports laboratory sensitivity as the site
        # maximum - measured on Facility A at up to 1,250x the real value.
        # The existing non-detect above has a limit of 0.5 and never
        # competed, which is why this went unnoticed.
        row("MW-01", "Benzene", "71-43-2", "01/05/2023", "", "100.0", "100.0",
            "ug/L", "N"),
        # A location with ONLY non-detects still reports one: "we looked and
        # found nothing" is a result, and 9,660 cells on Facility A are this.
        row("MW-04", "Benzene", "71-43-2", "02/02/2021", "", "25.0", "25.0",
            "ug/L", "N"),
        row("MW-04", "Benzene", "71-43-2", "02/02/2022", "", "40.0", "40.0",
            "ug/L", "N"),
        # DETECT_FLAG says detected but there is no number - the value falls
        # back to the limit and at_limit stays FALSE.
        row("MW-02", "Benzene", "71-43-2", "03/03/2019", "", "1.25", "1.25",
            "ug/L", "Y"),
        # No number anywhere: counted, but cannot win the aggregate.
        row("MW-02", "Benzene", "71-43-2", "04/04/2019", "", "", "",
            "ug/L", "N"),
        # Undated: may only fill an empty slot.
        row("MW-03", "Benzene", "71-43-2", "", "3.0", "3.0", "0.5",
            "ug/L", "Y"),
        row("MW-03", "Benzene", "71-43-2", "", "4.0", "4.0", "0.5",
            "ug/L", "Y"),
        # Dated AND undated at one location, which is the case the
        # "undated rows only fill an empty slot" rule exists for. Without a
        # location that mixes the two, NULLS LAST and NULLS FIRST give the
        # same answer and the rule is untested - a gap found by mutating the
        # store and watching this suite fail to notice.
        # Order matters: the undated row arrives first and carries the larger
        # number, so a wrong ordering picks it and is visible.
        row("MW-10", "Benzene", "71-43-2", "", "99.0", "99.0", "0.5",
            "ug/L", "Y"),
        row("MW-10", "Benzene", "71-43-2", "01/01/2015", "1.0", "1.0", "0.5",
            "ug/L", "Y"),
        # And one arriving after the dated row, which must not displace it.
        row("MW-10", "Benzene", "71-43-2", "", "98.0", "98.0", "0.5",
            "ug/L", "Y"),
        # A qualifier-prefixed number, which parse_number strips.
        row("MW-04", "Benzene", "71-43-2", "05/05/2020", "<0.9", "<0.9",
            "0.9", "ug/L", "N"),
        # Lowercase detect flag, and a different date format.
        row("MW-04", "Benzene", "71-43-2", "2021-06-07", "2.5", "2.5", "0.5",
            "ug/L", "y"),
        # Two methods on one sample, which is the Facility A case: same location,
        # same day, disagreeing about detection.
        row("MW-05", "Nickel", "7440-02-0", "07/07/2020", "", "0.01", "0.01",
            "mg/kg", "N", matrix="SO", fraction="N", method="ICM14B"),
        row("MW-05", "Nickel", "7440-02-0", "07/07/2020", "0.02", "0.02",
            "0.01", "mg/kg", "Y", matrix="SO", fraction="N", method="ICM40B"),
        # Dissolved and total for one sample, an order of magnitude apart.
        row("MW-06", "Iron", "7439-89-6", "08/08/2020", "0.4", "0.4", "0.05",
            "mg/L", "Y", matrix="WG", fraction="D"),
        row("MW-06", "Iron", "7439-89-6", "08/08/2020", "11.6", "11.6",
            "0.05", "mg/L", "Y", matrix="WG", fraction="T"),
        # Mixed case in the codes, which every filter compares insensitively.
        row("MW-07", "Iron", "7439-89-6", "09/09/2020", "1.1", "1.1", "0.05",
            "mg/L", "Y", matrix="wg", fraction="d", method="m1"),
        # A chemical whose name differs only by case, to exercise the
        # case-insensitive sort's stability.
        row("MW-08", "benzene", "71-43-2", "10/10/2020", "1.0", "1.0", "0.5",
            "ug/L", "Y"),
        # Blank location: dropped by the aggregate, kept by analytes.
        row("", "Toluene", "108-88-3", "11/11/2020", "2.0", "2.0", "0.5",
            "ug/L", "Y"),
        # QC sample types, which are real rows even where a filter hides them.
        row("MW-09", "Toluene", "108-88-3", "12/12/2020", "0.01", "0.01",
            "0.5", "ug/L", "Y", stype="EB"),
        # THE ROW SCOPE (dates and sample types). A sample taken in the
        # afternoon of a range's last day is INSIDE it: the bounds are whole
        # days. Comparing the timestamp to midnight would drop it.
        row("MW-11", "Benzene", "71-43-2", "01/05/2021 3:00:00 PM", "6.0",
            "6.0", "0.5", "ug/L", "Y"),
        # The day after, which a range ending 2021-01-05 must exclude.
        row("MW-11", "Benzene", "71-43-2", "01/06/2021", "8.0", "8.0", "0.5",
            "ug/L", "Y"),
        # A blank sample type is not "N": a QC filter must not let it in.
        row("MW-11", "Benzene", "71-43-2", "06/06/2020", "0.2", "0.2", "0.5",
            "ug/L", "Y", stype=""),
        # And a lowercase one, which is "N" compared case-insensitively.
        row("MW-11", "Benzene", "71-43-2", "07/07/2020", "0.3", "0.3", "0.5",
            "ug/L", "Y", stype="n"),
    ]


# Row scopes the whole comparison is repeated under: (start, end, types).
# Every question must agree under each, AND removing the scope must give
# back the unscoped answers, which the final NO_ROW_SCOPE pass checks.
SCOPES = [
    ("dates 2020-01-01..2021-01-05",
     (datetime.date(2020, 1, 1), datetime.date(2021, 1, 5), None)),
    ("open start ..2020-12-31", (None, datetime.date(2020, 12, 31), None)),
    ("open end 2021-01-01..", (datetime.date(2021, 1, 1), None, None)),
    ("normal samples only", (None, None, ["N"])),
    ("dates and types", (datetime.date(2019, 1, 1),
                         datetime.date(2021, 12, 31), ["n", "EB"])),
    ("a range holding nothing", (datetime.date(2030, 1, 1),
                                 datetime.date(2031, 1, 1), None)),
    ("unscoped again", rd.NO_ROW_SCOPE),
]


def scoped(label, table, store):
    """compare_all under every scope in SCOPES, then the scope's own rules.

    Parity alone cannot catch a rule both implementations break the same
    way, so the boundary cases are also asserted outright.
    """
    for name, (start, end, types) in SCOPES:
        print("")
        changed = (table.set_row_scope(start, end, types),
                   store.set_row_scope(start, end, types))
        check("%s/%s: both report the scope changed" % (label, name),
              changed == (True, True), repr(changed))
        compare_all("%s/%s" % (label, name), table, store)
    # Setting the same scope twice is not a change: the panel uses the
    # answer to decide whether to re-count everything.
    check("%s: an unchanged scope reports no change" % label,
          (table.set_row_scope(), store.set_row_scope()) == (False, False))


def direct_scope_checks():
    """The scope's rules asserted against known answers, not each other."""
    print("")
    table, store = build(FULL_HEADERS, awkward_rows())

    def values(source, location):
        rows, _ = source.rows_for("Benzene", "71-43-2")
        return sorted(r["value"] for r in rows if r["location"] == location)

    for source, name in ((table, "python"), (store, "sql")):
        source.set_row_scope(datetime.date(2021, 1, 1),
                             datetime.date(2021, 1, 5), None)
        check("%s: a 3 PM sample on the end date is inside the range" % name,
              values(source, "MW-11") == [6.0], repr(values(source, "MW-11")))
        check("%s: undated rows are outside any dated range" % name,
              values(source, "MW-03") == [], repr(values(source, "MW-03")))
        source.set_row_scope(None, None, ["N"])
        check("%s: QC filter keeps 'n', drops blank and EB" % name,
              values(source, "MW-11") == [0.3, 6.0, 8.0]
              and not source.rows_for("Toluene", "108-88-3")[0],
              repr(values(source, "MW-11")))

    # A report with no date column and no sample type column: both filters
    # match everything rather than dropping every row.
    bare_headers = ["SYS_LOC_CODE", "CHEMICAL_NAME", "RESULT_NUMERIC",
                    "DETECT_FLAG"]
    bare_rows = [[r[0], r[1], r[4], r[8]] for r in awkward_rows()]
    bare_table, bare_store = build(bare_headers, bare_rows)
    before = (bare_table.analytes(), bare_store.analytes())
    bare_table.set_row_scope(datetime.date(2030, 1, 1), None, ["N"])
    bare_store.set_row_scope(datetime.date(2030, 1, 1), None, ["N"])
    check("bare: a scope on columns the report lacks matches everything",
          (bare_table.analytes(), bare_store.analytes()) == before
          and before[0])


def build(headers, rows):
    table = rd.ReportTable(headers, rows)
    # with_raw so filtered_rows can be compared; the default is off
    # because it doubles ingest (see ReportStore.from_table).
    store = st.ReportStore.from_table(table, with_raw=True)
    return table, store


# ------------------------------------------------------------------ cases

def compare_all(label, table, store):
    """Ask both implementations everything, over every filter combination."""
    same("%s: analytes" % label, table.analytes(), store.analytes())
    same("%s: matrices" % label, table.matrices(), store.matrices())
    same("%s: mixed_fractions" % label,
         table.mixed_fractions(), store.mixed_fractions())
    same("%s: locations" % label, table.locations(), store.locations())

    matrices = [rd.ALL_MATRICES, "WG", "SO", ["WG", "SO"], [], "wg"]
    for matrix in matrices:
        same("%s: analytes(matrix=%r)" % (label, matrix),
             table.analytes(matrix=matrix), store.analytes(matrix=matrix))
        same("%s: locations(matrix=%r)" % (label, matrix),
             table.locations(matrix=matrix), store.locations(matrix=matrix))
        same("%s: methods(matrix=%r)" % (label, matrix),
             table.methods(matrix=matrix), store.methods(matrix=matrix))

    analytes = [(a["chemical"], a["cas_rn"]) for a in table.analytes()]
    for chemical, cas in analytes:
        same("%s: methods(%s)" % (label, chemical),
             table.methods(chemical, cas), store.methods(chemical, cas))
        same("%s: fractions_for(%s)" % (label, chemical),
             table.fractions_for(chemical, cas),
             store.fractions_for(chemical, cas))
        # Name-only lookup, which must fall through to CAS behaviour
        # identically in both.
        same("%s: fractions_for(%s, no cas)" % (label, chemical),
             table.fractions_for(chemical), store.fractions_for(chemical))

    combos = itertools.product(
        analytes[:4],
        (rd.MOST_RECENT, rd.MAXIMUM),
        (rd.ND_HALF, rd.ND_FULL, rd.ND_EXCLUDE),
        (rd.ALL_MATRICES, "WG", "SO"),
        (rd.ANY_FRACTION, "D", "T", ["D", "T"]),
        (rd.ALL_METHODS, "M1", "ICM40B"),
        (False, True),
    )
    checked = 0
    mismatched = []
    for (chemical, cas), how, nd, matrix, fraction, method, only in combos:
        left = table.aggregate(chemical, cas, how=how, detected_only=only,
                               non_detect=nd, matrix=matrix,
                               fraction=fraction, method=method)
        right = store.aggregate(chemical, cas, how=how, detected_only=only,
                                non_detect=nd, matrix=matrix,
                                fraction=fraction, method=method)
        checked += 1
        if left != right:
            mismatched.append((chemical, how, nd, matrix, fraction, method,
                               only, left, right))
    check("%s: aggregate over %d filter combinations" % (label, checked),
          not mismatched,
          "" if not mismatched else
          "\n      first mismatch: %r\n      python: %r\n      sql   : %r"
          % (mismatched[0][:7], mismatched[0][7], mismatched[0][8]))

    series_checked, series_bad = 0, []
    for chemical, cas in analytes:
        for location in table.locations():
            for nd in (rd.ND_HALF, rd.ND_FULL, rd.ND_EXCLUDE):
                left = table.series(location, chemical, cas, non_detect=nd)
                right = store.series(location, chemical, cas, non_detect=nd)
                series_checked += 1
                if left != right:
                    series_bad.append((location, chemical, nd, left, right))
    # series_many must agree with looping series(), because it exists purely
    # as a performance shape and a faster wrong answer is worse than a slow
    # right one.
    bulk_bad = []
    for chemical, cas in analytes:
        for nd in (rd.ND_HALF, rd.ND_FULL, rd.ND_EXCLUDE):
            locations = table.locations()
            expected = {loc: table.series(loc, chemical, cas, non_detect=nd)
                        for loc in locations}
            got = store.series_many(locations, chemical, cas, non_detect=nd)
            if expected != got:
                bulk_bad.append((chemical, nd, expected, got))
            # ReportTable grew series_many so the panel can hold one
            # reference and never ask which implementation it has; the two
            # must therefore agree with each other, not only with the loop.
            table_bulk = table.series_many(locations, chemical, cas,
                                           non_detect=nd)
            if table_bulk != got:
                bulk_bad.append((chemical, nd, table_bulk, got))
    check("%s: series_many agrees with looping series()" % label,
          not bulk_bad,
          "" if not bulk_bad else
          "\n      first mismatch: %r" % (bulk_bad[0][:2],))
    # None means "every location", which must equal asking for all of them.
    for chemical, cas in analytes[:2]:
        same("%s: series_many(None) == series_many(all) for %s"
             % (label, chemical),
             {k: v for k, v in store.series_many(None, chemical, cas).items()
              if v},
             {k: v for k, v in store.series_many(table.locations(), chemical,
                                                 cas).items() if v})

    # rows_for: the unaggregated layer and the depth profiles. The cap is
    # the interesting part - it drops WHOLE locations, greedily, largest
    # history first, and a location that would overflow is skipped rather
    # than ending the scan, so a smaller one after it can still fit.
    rows_checked, rows_bad = 0, []
    for (chemical, cas) in analytes[:4]:
        for nd in (rd.ND_HALF, rd.ND_FULL, rd.ND_EXCLUDE):
            for only in (False, True):
                for cap in (0, 1, 2, 3, 5, 10, 1000):
                    left = table.rows_for(chemical, cas, detected_only=only,
                                          non_detect=nd, limit=cap)
                    right = store.rows_for(chemical, cas, detected_only=only,
                                           non_detect=nd, limit=cap)
                    rows_checked += 1
                    if left != right:
                        rows_bad.append((chemical, nd, only, cap, left, right))
    check("%s: rows_for over %d combinations (including 7 row caps)"
          % (label, rows_checked), not rows_bad,
          "" if not rows_bad else
          "\n      first mismatch: %r\n      python: %r\n      sql   : %r"
          % (rows_bad[0][:4], rows_bad[0][4], rows_bad[0][5]))

    # filtered_rows: the export. Returns the REPORT's columns in the
    # report's order, which is why the store keeps a second raw table.
    export_bad = []
    analyte_sets = [None, [{"chemical": c, "cas_rn": k}
                           for c, k in analytes[:2]]]
    for chosen in analyte_sets:
        for matrix in (rd.ALL_MATRICES, "WG", ["WG", "SO"]):
            for places in (None, table.locations()[:3]):
                left = table.filtered_rows(analytes=chosen, matrix=matrix,
                                           locations=places)
                right = store.filtered_rows(analytes=chosen, matrix=matrix,
                                            locations=places)
                if left != right:
                    export_bad.append((chosen, matrix, places, left, right))
    check("%s: filtered_rows (export) matches" % label, not export_bad,
          "" if not export_bad else
          "\n      python headers/rows: %r\n      sql   headers/rows: %r"
          % (export_bad[0][3], export_bad[0][4]))

    check("%s: series over %d location/analyte/convention combinations"
          % (label, series_checked), not series_bad,
          "" if not series_bad else
          "\n      first mismatch: %r\n      python: %r\n      sql   : %r"
          % (series_bad[0][:3], series_bad[0][3], series_bad[0][4]))


def main():
    try:
        st.duckdb_module()
    except ImportError as exc:
        print("duckdb is not importable: %s" % exc)
        print("Build the bundle first: python tools/vendor_deps.py --target local")
        return 1

    print("")
    print("ReportTable vs ReportStore")
    print("")

    rows = awkward_rows()
    table, store = build(FULL_HEADERS, rows)
    check("both see the same row count",
          len(table.rows) == store.count(),
          "%d vs %d" % (len(table.rows), store.count()))
    compare_all("full", table, store)
    scoped("full", table, store)

    # A report missing the optional columns. Every filter on a column the
    # report does not have must match EVERYTHING - the rule that turns a
    # missing column into an ignored filter rather than an empty map.
    print("")
    lean_headers = ["SYS_LOC_CODE", "CHEMICAL_NAME", "SAMPLE_DATE",
                    "RESULT_NUMERIC", "DETECT_FLAG"]
    lean_rows = [[r[0], r[1], r[3], r[4], r[8]] for r in rows]
    lean_table, lean_store = build(lean_headers, lean_rows)
    check("lean report has no matrix column",
          lean_table.column("matrix") < 0)
    compare_all("lean", lean_table, lean_store)
    # Lean has dates but no sample type, so a type filter must be ignored
    # while the dates still apply.
    scoped("lean", lean_table, lean_store)

    direct_scope_checks()

    # WHICH COLUMNS A SOURCE CANNOT BE VALUED WITHOUT. Both implementations
    # read rd.REQUIRED_FOR_ANALYSIS, and must agree on every case: the panel
    # refuses a report by the table's answer and a parquet source by the
    # store's. DETECT_FLAG matters most - without it both valued every result
    # as a non-detect and halved it, and agreed with each other doing so.
    print("")
    same("required: full report lacks nothing",
         table.missing_for_analysis, store.missing_for_analysis)
    check("required: full report lacks nothing (and says so)",
          table.missing_for_analysis == [])
    no_flag = [h for h in FULL_HEADERS if h != "DETECT_FLAG"]
    keep = [FULL_HEADERS.index(h) for h in no_flag]
    nf_table, nf_store = build(no_flag, [[r[i] for i in keep] for r in rows])
    same("required: no DETECT_FLAG",
         nf_table.missing_for_analysis, nf_store.missing_for_analysis)
    check("required: no DETECT_FLAG is named as missing",
          nf_table.missing_for_analysis == ["DETECT_FLAG"],
          str(nf_table.missing_for_analysis))

    # An empty report must not raise in either implementation.
    print("")
    empty_table, empty_store = build(FULL_HEADERS, [])
    same("empty: analytes", empty_table.analytes(), empty_store.analytes())
    same("empty: aggregate", empty_table.aggregate("Benzene"),
         empty_store.aggregate("Benzene"))
    same("empty: series", empty_table.series("MW-01", "Benzene"),
         empty_store.series("MW-01", "Benzene"))

    print("")
    if FAILURES:
        print("%d failure(s):" % len(FAILURES))
        for name in FAILURES:
            print("  %s" % name)
        return 1
    print("ReportTable and ReportStore agree on every question asked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
