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
        # A TIE ON THE MAXIMUM between two methods on one sample: the same
        # detected number, so the value, unit, date and detect all agree and
        # only the row's own columns say which won. The first to arrive
        # must, in both. A store breaking ties on row_id DESC passed every
        # check until aggregate(fields=True) compared the winner's cells.
        row("MW-12", "Benzene", "71-43-2", "03/03/2021", "15.0", "15.0", "0.5",
            "ug/L", "Y", method="M1", custom="first"),
        row("MW-12", "Benzene", "71-43-2", "03/03/2021", "15.0", "15.0", "0.5",
            "ug/L", "Y", method="ICM40B", custom="second"),
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
    ] + depth_rows()


def depth_rows():
    """Sediment cores for the depth filter. Every water row above has no
    depth, so each depth scope also checks those are left out.

    Mostly cm with one m and one ft core, as Facility A's sediment mixes cm
    and m: a filter compared as bare numbers passes these tests only if it
    gets every unit wrong the same way, and the direct checks below rule
    that out.
    """
    def core(loc, value, d0, d1, dunit):
        return row(loc, "Lead", "7439-92-1", "06/15/2020", value, value, "0.5",
                   "mg/kg", "Y", matrix="SE", d0=d0, d1=d1, dunit=dunit)
    return [
        core("SD-01", "11.0", "0", "2", "cm"),
        # Its bottom is 0.3 m only after conversion and rounding: 30 * 0.01
        # is 0.3 in floating point, but the bound must not depend on luck.
        core("SD-01", "12.0", "2", "30", "cm"),
        # A metre core, starting where a 0 to 0.3 m filter ends.
        core("SD-01", "13.0", "0.3", "0.5", "m"),
        # A start depth alone is a point; an end depth alone is one too.
        core("SD-02", "21.0", "10", "", "CM"),
        core("SD-02", "22.0", "", "15", "cm"),
        # Written bottom first: 20 to 40 cm.
        core("SD-02", "23.0", "40", "20", "cm"),
        # Feet, by a spelling the alias table resolves: 30.48 to 60.96 cm.
        core("SD-03", "31.0", "1", "2", "feet"),
        # A unit nobody can convert, and no unit at all. Each is filtered
        # only in its own terms, never compared with the others.
        core("SD-03", "32.0", "1", "2", "fathom"),
        core("SD-03", "33.0", "5", "6", ""),
        # A unit and no depth: outside every depth filter.
        core("SD-04", "41.0", "", "", "cm"),
    ] + leachate_rows()


def leachate_rows():
    """A solid's stray leachate, for the unit filter: Lead in the sediment
    matrix reported in water's units, as Facility A's sediment holds 3,401
    results in mg/L beside 635,904 in mg/kg (2026-10-30). No depth, so the
    depth checks above are untouched by them.
    """
    def stray(value, unit):
        return row("SD-05", "Lead", "7439-92-1", "06/15/2020", value, value,
                   "0.001", unit, "Y", matrix="SE")
    return [
        stray("0.004", "mg/L"),
        # Padded: compared trimmed, so it is mg/L too.
        stray("0.005", " mg/L "),
        # A different spelling is a different unit, as the guard counts it.
        stray("9.0", "MG/KG"),
        # No unit at all: "" is a unit the filter can keep or drop.
        stray("8.0", ""),
    ]


# Row scopes the whole comparison is repeated under: (start, end, types,
# depth, units). Every question must agree under each, AND removing the scope must
# give back the unscoped answers, which the final NO_ROW_SCOPE pass checks.
SCOPES = [
    ("dates 2020-01-01..2021-01-05",
     (datetime.date(2020, 1, 1), datetime.date(2021, 1, 5), None, None)),
    ("open start ..2020-12-31",
     (None, datetime.date(2020, 12, 31), None, None)),
    ("open end 2021-01-01..", (datetime.date(2021, 1, 1), None, None, None)),
    ("normal samples only", (None, None, ["N"], None)),
    ("dates and types", (datetime.date(2019, 1, 1),
                         datetime.date(2021, 12, 31), ["n", "EB"], None)),
    ("a range holding nothing", (datetime.date(2030, 1, 1),
                                 datetime.date(2031, 1, 1), None, None)),
    ("depth 0..0.3 m", (None, None, None, (0, 0.3, "m"))),
    ("depth 30..100 cm", (None, None, None, (30, 100, "cm"))),
    ("depth open top ..12 CM", (None, None, None, (None, 12, "CM"))),
    ("depth in fathoms", (None, None, None, (0, 5, "fathom"))),
    ("depth with no unit", (None, None, None, (0, 10, ""))),
    ("depth, dates and types", (datetime.date(2019, 1, 1), None, ["N"],
                                (0, 1, "metres"))),
    ("unit mg/kg", (None, None, None, None, ["mg/kg"])),
    ("unit the leachate's", (None, None, None, None, ["mg/L"])),
    ("units several, one of them none", (None, None, None, None,
                                         ["ug/L", ""])),
    ("unit a spelling only one row uses", (None, None, None, None,
                                           ["MG/KG"])),
    ("unit nothing has", (None, None, None, None, ["Mg/l"])),
    ("unit, depth, dates and types", (datetime.date(2019, 1, 1), None, ["N"],
                                      (0, 1, "m"), [" mg/kg "])),
    ("unscoped again", rd.NO_ROW_SCOPE),
]


def scoped(label, table, store):
    """compare_all under every scope in SCOPES, then the scope's own rules.

    Parity alone cannot catch a rule both implementations break the same
    way, so the boundary cases are also asserted outright.
    """
    for name, scope in SCOPES:
        print("")
        changed = (table.set_row_scope(*scope), store.set_row_scope(*scope))
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
    bare_table.set_row_scope(datetime.date(2030, 1, 1), None, ["N"],
                             (0, 1, "m"), ["mg/L"])
    bare_store.set_row_scope(datetime.date(2030, 1, 1), None, ["N"],
                             (0, 1, "m"), ["mg/L"])
    check("bare: a scope on columns the report lacks matches everything",
          (bare_table.analytes(), bare_store.analytes()) == before
          and before[0])
    check("bare: no depth columns, no depth to offer",
          (bare_table.depth_extent(), bare_store.depth_extent())
          == (None, None))
    check("bare: no unit column, no units to offer",
          (bare_table.units_in(), bare_store.units_in()) == ([], []))
    depth_checks()


def depth_checks():
    """The depth filter's rules, against known answers (depth_rows)."""
    print("")
    table, store = build(FULL_HEADERS, awkward_rows())

    def lead(source):
        rows, _ = source.rows_for("Lead", "7439-92-1")
        return sorted(r["value"] for r in rows)

    cases = [
        # 0-2 cm and 2-30 cm are inside; 30-50 cm (the m core) and
        # 20-40 cm (written backwards) reach below; the points at 10 and 15
        # cm are inside. Bare numbers would let the m core in (0.3 to 0.5
        # read against 0 to 0.3 in cm is "0.3 to 0.5 cm") and keep the cm
        # rows out.
        ((0, 0.3, "m"), [11.0, 12.0, 21.0, 22.0],
         "metres: cm rows converted, the 30 cm bottom on the bound kept"),
        # Containment, not overlap: 20-40 cm crosses 30 and stays out.
        ((30, 100, "cm"), [13.0, 31.0],
         "cm: the m and ft cores are in, the core crossing 30 cm is not"),
        ((None, 12, "CM"), [11.0, 21.0],
         "an open top, and the unit compared case-insensitively"),
        ((0, 5, "fathom"), [32.0], "a unit nobody converts meets only itself"),
        ((0, 10, ""), [33.0], "no unit meets only no unit"),
        ((0, 1000, "km"), [], "an unknown filter unit converts nothing"),
    ]
    for source, name in ((table, "python"), (store, "sql")):
        for depth, want, why in cases:
            source.set_row_scope(None, None, None, depth)
            check("depth/%s: %s" % (name, why), lead(source) == want,
                  repr(lead(source)))
            check("depth/%s: no undepthed row survives %r" % (name, depth),
                  not source.rows_for("Benzene", "71-43-2")[0])
        source.set_row_scope()
        # cm is most rows (5), so the range is offered in cm: the metre core
        # reaches 50 cm and the feet core 60.96 cm. Fathoms and the blank
        # unit cannot be placed on it, and say so.
        # The stops are every top and bottom, converted: the m core's 30
        # and 50, the feet core's 30.48 and 60.96, the points once each.
        want = {"unit": "cm", "top": 0.0, "bottom": 60.96, "rows": 7,
                "unconverted": {"fathom": 1, "": 1},
                "stops": (0.0, 2.0, 10.0, 15.0, 20.0, 30.0, 30.48, 40.0,
                          50.0, 60.96)}
        check("depth/%s: the extent of the sediment" % name,
              source.depth_extent("SE") == want, repr(source.depth_extent("SE")))
        check("depth/%s: water has no depth to offer" % name,
              source.depth_extent("WG") is None,
              repr(source.depth_extent("WG")))
        # The extent ignores the scope, or the slider would shrink under
        # its own handles.
        source.set_row_scope(None, None, None, (0, 2, "cm"))
        check("depth/%s: the extent is the same under a depth filter" % name,
              source.depth_extent("SE") == want)
        source.set_row_scope()
    unit_checks()


def unit_checks():
    """The unit filter's rules, against known answers (leachate_rows)."""
    print("")
    table, store = build(FULL_HEADERS, awkward_rows())

    def lead(source):
        rows, _ = source.rows_for("Lead", "7439-92-1", matrix="SE")
        return sorted(r["value"] for r in rows)

    cases = [
        (["mg/kg"], [11.0, 12.0, 13.0, 21.0, 22.0, 23.0, 31.0, 32.0, 33.0,
                     41.0], "the solid's own unit keeps the cores"),
        (["mg/L"], [0.004, 0.005], "the leachate, padded spelling and all"),
        (["MG/KG"], [9.0], "a spelling is its own unit, as the guard says"),
        ([""], [8.0], "no unit is a unit that can be kept"),
        (["Mg/l"], [], "a unit nothing has keeps nothing"),
    ]
    # Commonest first, ties by name: what the guard counts, every row.
    offer = [("mg/kg", 10), ("mg/L", 2), ("", 1), ("MG/KG", 1)]
    for source, name in ((table, "python"), (store, "sql")):
        for units, want, why in cases:
            source.set_row_scope(None, None, None, None, units)
            check("unit/%s: %s" % (name, why), lead(source) == want,
                  repr(lead(source)))
            guard = source.units_for("Lead", "7439-92-1", matrix="SE")
            check("unit/%s: the guard sees only %r" % (name, units),
                  [u for u, _ in guard] == ([] if not want else
                                            [units[0].strip()]),
                  repr(guard))
            # The offer ignores the scope, or ticking one unit would take
            # the others off the list.
            check("unit/%s: the offer is whole under %r" % (name, units),
                  source.units_in("SE") == offer, repr(source.units_in("SE")))
        source.set_row_scope()
        check("unit/%s: the sediment's units, commonest first" % name,
              source.units_in("SE") == offer, repr(source.units_in("SE")))
        check("unit/%s: water's are its own" % name,
              source.units_in("WG") == [("ug/L", 25), ("mg/L", 3)],
              repr(source.units_in("WG")))


def unit_column_checks():
    """A non-detect's unit is in REPORT_RESULT_UNIT, whatever the source.

    A non-detect reports no result, so RESULT_UNIT is blank on its row and
    the unit is that of the limit, in REPORT_RESULT_UNIT. The parquet store
    coalesced the two; ReportTable took the first column that existed, so
    the same row read ug/L from parquet and unitless from a live report
    (found 2026-10-28). Parity between the table and a store built FROM the
    table can't see that - both read table.cell - so the parquet store is
    asked too, and the answer is asserted outright.
    """
    print("")
    headers = FULL_HEADERS + ["REPORT_RESULT_UNIT"]

    def r(loc, numeric, reported, limit, unit, report_unit, detect, chem="Arsenic"):
        return row(loc, chem, "7440-38-2" if chem == "Arsenic" else "7439-92-1",
                   "01/05/2020", numeric, reported, limit, unit, detect) + [report_unit]

    rows = [
        r("MW-01", "4.0", "4.0", "1.0", "ug/L", "ug/L", "Y"),
        # The non-detect: no result, no result unit, the limit's unit beside it.
        r("MW-02", "", "1.0", "1.0", "", "ug/L", "N"),
        # Whitespace is no unit either.
        r("MW-03", "", "2.0", "2.0", "  ", "ug/L", "N"),
        # Both blank: unitless, and said so rather than invented.
        r("MW-04", "", "1.0", "1.0", "", "", "N"),
        # The mix the guard exists for, reached only through REPORT_RESULT_UNIT.
        r("MW-05", "3.0", "3.0", "1.0", "ug/L", "ug/L", "Y", chem="Lead"),
        r("MW-06", "", "0.002", "0.002", "", "mg/L", "N", chem="Lead"),
    ]
    table, store = build(headers, rows)

    import tempfile
    folder = tempfile.mkdtemp(prefix="parity_units_")
    path = os.path.join(folder, "units.parquet").replace("\\", "/")
    con = st.duckdb_module().connect()
    con.execute("CREATE TABLE t (%s)" % ", ".join('"%s" VARCHAR' % h for h in headers))
    con.executemany("INSERT INTO t VALUES (%s)" % ", ".join("?" * len(headers)),
                    [[cell if cell != "" else None for cell in rw] for rw in rows])
    con.execute("COPY t TO '%s' (FORMAT PARQUET)" % path)
    con.close()
    parquet = st.ReportStore.from_parquet(path, hive=False)

    def unit_at(source, chemical, location):
        rws, _ = source.rows_for(chemical, None, non_detect=rd.ND_FULL)
        return [x["unit"] for x in rws if x["location"] == location]

    sources = (("python", table), ("sql from the table", store),
               ("sql over parquet", parquet))
    for name, source in sources:
        check("units/%s: a non-detect reads its limit's unit" % name,
              unit_at(source, "Arsenic", "MW-02") == ["ug/L"],
              repr(unit_at(source, "Arsenic", "MW-02")))
        check("units/%s: a blank-but-spaced unit falls through too" % name,
              unit_at(source, "Arsenic", "MW-03") == ["ug/L"],
              repr(unit_at(source, "Arsenic", "MW-03")))
        check("units/%s: no unit anywhere stays unitless" % name,
              unit_at(source, "Arsenic", "MW-04") == [""],
              repr(unit_at(source, "Arsenic", "MW-04")))
        units = getattr(source, "units_for", None)
        found = units("Arsenic", None, non_detect=rd.ND_FULL) if units else None
        check("units/%s: units_for counts the non-detects as ug/L" % name,
              found == [("ug/L", 3), ("", 1)], repr(found))
        lead = units("Lead", None, non_detect=rd.ND_FULL) if units else None
        check("units/%s: a mix through REPORT_RESULT_UNIT is seen" % name,
              lead is not None and sorted(u for u, _ in lead) == ["mg/L", "ug/L"],
              repr(lead))

    # Fields over PARQUET: from results_wide's single scan rather than a
    # join, so it is a second way to the same cells and must agree.
    expected, _ = table.rows_for("Arsenic", None, non_detect=rd.ND_FULL,
                                 fields=True)
    for name, source in sources[1:]:
        got, _ = source.rows_for("Arsenic", None, non_detect=rd.ND_FULL,
                                 fields=True)
        same("fields/%s: rows_for(fields=True)" % name, expected, got)
        same("fields/%s: aggregate(fields=True)" % name,
             table.aggregate("Lead", how=rd.MAXIMUM, fields=True),
             source.aggregate("Lead", how=rd.MAXIMUM, fields=True))
        same("fields/%s: report_fields names the report's columns" % name,
             [h for h, _ in source.report_fields()], list(headers))
    blank = [x["fields"] for x in expected if x["location"] == "MW-02"][0]
    check("fields: an empty report cell is None, whitespace is kept",
          blank[headers.index("RESULT_NUMERIC")] is None
          and [x["fields"] for x in expected if x["location"] == "MW-03"][0][
              headers.index("RESULT_UNIT")] == "  ", repr(blank))
    check("fields: a store without the report's columns offers none",
          st.ReportStore.from_table(table).report_fields() == [])
    try:
        st.ReportStore.from_table(table).rows_for("Arsenic", fields=True)
        refused = False
    except st.StoreError:
        refused = True
    check("fields: and refuses to invent them", refused)


def sample_field_checks():
    """sample_fields(): a pivot point's columns are its SAMPLE'S, in all three.

    Only SAMPLE_COLUMNS, and only those holding one value across every
    mapped sample's rows - a sample column that disagrees anywhere is left
    off rather than shown with one row's value.
    """
    headers = ["SYS_SAMPLE_CODE", "SYS_LOC_CODE", "SAMPLE_DATE",
               "CHEMICAL_NAME", "RESULT_NUMERIC", "DETECT_FLAG",
               "MATRIX_CODE", "START_DEPTH", "TASK_CODE", "LAB_QUALIFIERS",
               "SAMPLE_TYPE_CODE", "LOC_NAME"]

    def r(sample, chem, task="T1", depth="0.5", qual="", matrix="WG",
          loc="MW-1", name="Well 1"):
        return [sample, loc, "01/05/2020", chem, "1.0", "Y", matrix, depth,
                task, qual, "N", name]

    rows = [
        # Two results, one sample: the qualifiers differ, as results' do.
        r("S-1", "Lead", qual="J"), r("S-1", "Zinc", qual=""),
        r("S-2", "Lead", loc="MW-2", name="Well 2"),
        r("S-2", "Zinc", loc="MW-2", name="Well 2"),
        # A sample whose TASK_CODE disagrees between its rows.
        r("S-3", "Lead", task="T1"), r("S-3", "Zinc", task="T2"),
        # A sample with its depth on one row and blank on the other.
        r("S-4", "Lead", depth="0.5"), r("S-4", "Zinc", depth=""),
        # Whitespace is a value, not a blank: " " and "" differ.
        r("S-5", "Lead", name=" "), r("S-5", "Zinc", name=""),
        # Another matrix.
        r("S-6", "Lead", matrix="SO"), r("S-6", "Zinc", matrix="SO"),
    ]
    table, store = build(headers, rows)
    import tempfile
    folder = tempfile.mkdtemp(prefix="parity_samples_")
    path = os.path.join(folder, "samples.parquet").replace("\\", "/")
    con = st.duckdb_module().connect()
    con.execute("CREATE TABLE t (%s)" % ", ".join('"%s" VARCHAR' % h for h in headers))
    con.executemany("INSERT INTO t VALUES (%s)" % ", ".join("?" * len(headers)),
                    [[cell if cell != "" else None for cell in rw] for rw in rows])
    con.execute("COPY t TO '%s' (FORMAT PARQUET)" % path)
    con.close()
    parquet = st.ReportStore.from_parquet(path, hive=False)

    def names(answer):
        return [h for h, _ in answer[0]]

    asks = (("two clean samples", ["S-1", "S-2"], rd.ALL_MATRICES),
            ("one disagrees on its task", ["S-1", "S-3"], rd.ALL_MATRICES),
            ("one has a half-blank depth", ["S-1", "S-4"], rd.ALL_MATRICES),
            ("whitespace against blank", ["S-5"], rd.ALL_MATRICES),
            ("the matrix narrows", ["S-1", "S-6"], "WG"),
            ("a code nothing has", ["S-1", "NOPE"], rd.ALL_MATRICES),
            ("nothing at all", ["NOPE"], rd.ALL_MATRICES),
            ("codes with spaces", [" S-1 ", "S-2"], rd.ALL_MATRICES))
    for label, samples, matrix in asks:
        expected = table.sample_fields(samples, matrix=matrix)
        for name, source in (("sql from the table", store),
                             ("sql over parquet", parquet)):
            same("samples/%s: %s" % (name, label), expected,
                 source.sample_fields(samples, matrix=matrix))

    clean = table.sample_fields(["S-1", "S-2"])
    check("samples: only the sample's columns, never a result's",
          names(clean) == ["SYS_SAMPLE_CODE", "SYS_LOC_CODE", "SAMPLE_DATE",
                           "MATRIX_CODE", "START_DEPTH", "TASK_CODE",
                           "SAMPLE_TYPE_CODE", "LOC_NAME"], repr(names(clean)))
    check("samples: each sample its own values",
          clean[1]["S-2"][names(clean).index("LOC_NAME")] == "Well 2"
          and clean[1]["S-1"][names(clean).index("SYS_SAMPLE_CODE")] == "S-1",
          repr(clean[1]))
    check("samples: a task that disagrees within a sample is left off",
          "TASK_CODE" not in names(table.sample_fields(["S-1", "S-3"]))
          and "SAMPLE_DATE" in names(table.sample_fields(["S-1", "S-3"])))
    check("samples: so is a depth filled on only some rows",
          "START_DEPTH" not in names(table.sample_fields(["S-1", "S-4"])))
    check("samples: another matrix's sample is not asked about",
          set(table.sample_fields(["S-1", "S-6"], matrix="WG")[1]) == {"S-1"})
    check("samples: no sample column, no fields",
          rd.ReportTable(["CHEMICAL_NAME", "RESULT_NUMERIC"],
                         [["Lead", "1"]]).sample_fields(["S-1"]) == ([], {}))


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
    for matrix in matrices + ["SE", "se", ["SE", "WG"]]:
        same("%s: depth_extent(matrix=%r)" % (label, matrix),
             table.depth_extent(matrix), store.depth_extent(matrix))
        same("%s: units_in(matrix=%r)" % (label, matrix),
             table.units_in(matrix), store.units_in(matrix))
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
    # The same combinations with fields: the WINNING ROW'S report columns.
    # Equal values do not prove the same row won - two methods on one
    # sample can report one number - and a layer naming the wrong sample's
    # qualifiers and lab beside a right number is the plausible-number
    # failure in another column.
    fields_bad = []
    for (chemical, cas), how, nd, matrix, fraction, method, only in itertools.product(
            analytes[:4], (rd.MOST_RECENT, rd.MAXIMUM),
            (rd.ND_HALF, rd.ND_FULL, rd.ND_EXCLUDE), (rd.ALL_MATRICES, "WG", "SO"),
            (rd.ANY_FRACTION, "D", ["D", "T"]), (rd.ALL_METHODS, "M1"),
            (False, True)):
        ask = dict(how=how, detected_only=only, non_detect=nd, matrix=matrix,
                   fraction=fraction, method=method, fields=True)
        left = table.aggregate(chemical, cas, **ask)
        right = store.aggregate(chemical, cas, **ask)
        if left != right:
            fields_bad.append(((chemical, how, nd, matrix, fraction, method,
                                only), left, right))
    check("%s: aggregate(fields=True) names the same winning row" % label,
          not fields_bad,
          "" if not fields_bad else
          "\n      first mismatch: %r\n      python: %r\n      sql   : %r"
          % fields_bad[0])
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
    for (chemical, cas) in analytes[:4]:
        for nd in (rd.ND_HALF, rd.ND_EXCLUDE):
            for cap in (0, 3):
                left = table.rows_for(chemical, cas, non_detect=nd, limit=cap,
                                      fields=True)
                right = store.rows_for(chemical, cas, non_detect=nd, limit=cap,
                                       fields=True)
                rows_checked += 1
                if left != right:
                    rows_bad.append((chemical, nd, "fields", cap, left, right))
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

    # count_for: the footer's "N locations - M results would map". Its
    # docstring promises the same predicate as rows_for, so the build can't
    # draw fewer than the footer said; nothing held it to that until
    # 2026-10-29. ReportTable has no count_for (the panel counts its rows),
    # so the store is held to the table's rows_for, counted.
    #
    # units_for: the mixed-unit guard. ReportTable had none, so the guard
    # was skipped until a live report's store landed (found 2026-10-28).
    count_bad, units_bad = [], []
    table_units = getattr(table, "units_for", None)
    combos = itertools.product(
        analytes[:4], (rd.ND_HALF, rd.ND_FULL, rd.ND_EXCLUDE),
        (rd.ALL_MATRICES, "WG", "SO"), (rd.ANY_FRACTION, "D", ["D", "T"]),
        (rd.ALL_METHODS, "M1"), (False, True))
    for (chemical, cas), nd, matrix, fraction, method, only in combos:
        ask = dict(detected_only=only, non_detect=nd, matrix=matrix,
                   fraction=fraction, method=method)
        rows, _ = table.rows_for(chemical, cas, **ask)
        expected = (len({r["location"] for r in rows}), len(rows))
        got = store.count_for(chemical, cas, **ask)
        if tuple(got) != expected:
            count_bad.append((chemical, ask, expected, got))
        if table_units is not None:
            left = table_units(chemical, cas, **ask)
            right = store.units_for(chemical, cas, **ask)
            if left != right:
                units_bad.append((chemical, ask, left, right))
    check("%s: count_for equals the table's rows_for, counted" % label,
          not count_bad, "" if not count_bad else
          "\n      first mismatch: %r" % (count_bad[0],))
    check("%s: ReportTable answers units_for" % label, table_units is not None)
    check("%s: units_for matches" % label, not units_bad,
          "" if not units_bad else
          "\n      first mismatch: %r" % (units_bad[0],))

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
    unit_column_checks()
    sample_field_checks()

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
