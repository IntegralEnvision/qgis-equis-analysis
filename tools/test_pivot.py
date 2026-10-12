"""The pivot: one row per sample, four columns per analyte (docs/pivot-design.md).

    python tools/test_pivot.py

Plain interpreter plus duckdb. The fixture is small and every expected
answer is worked out by hand in the comments beside it, so a failure says
which RULE broke rather than that a number changed. The last section runs
against the real Facility A parquet when it is present, and skips (saying so)
when it is not.
"""
import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from equis_loader import report_data as rd  # noqa: E402
from equis_loader.store import ReportStore  # noqa: E402
from equis_loader.pivot import (  # noqa: E402
    KEEP_TABLES, PivotError, Variable, build_pivot, slug)

FAILED = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - %s" % (detail,)) if detail != "" else ""))
    if not condition:
        FAILED.append(label)


HEADERS = ["SYS_LOC_CODE", "SYS_SAMPLE_CODE", "SAMPLE_DATE", "CHEMICAL_NAME",
           "CAS_RN", "RESULT_NUMERIC", "REPORTING_DETECTION_LIMIT",
           "REPORT_RESULT_UNIT", "DETECT_FLAG", "MATRIX_CODE", "FRACTION",
           "ANALYTIC_METHOD", "SAMPLE_TYPE_CODE", "TEST_TYPE",
           "INTERPRETED_QUALIFIERS", "LAB_QUALIFIERS", "LAB_NAME_CODE"]
AS, PB = ("Arsenic", "7440-38-2"), ("Lead", "7439-92-1")


def row(loc, sample, date, chem, value, limit, detect, method="A",
        stype="N", test="INITIAL", interp="", lab_q="", unit="ug/L",
        fraction="T", matrix="WS", lab="LAB1"):
    return [loc, sample, date, chem[0], chem[1], value, limit, unit, detect,
            matrix, fraction, method, stype, test, interp, lab_q, lab]


ROWS = [
    # S1: two METHODS on one sample, disagreeing about detection. The
    # non-detect's limit (20) is ABOVE the detection (10), so "higher" alone
    # would pick a limit - the CLAUDE.md rule that a non-detect is never the
    # maximum. The detected one must win (rule 2); one cell resolved.
    row("MW-1", "S1", "01/01/2020", AS, "10", "", "Y", method="A"),
    row("MW-1", "S1", "01/01/2020", AS, "", "20", "N", method="B", lab_q="U"),
    # Interpreted qualifier wins over the lab's.
    row("MW-1", "S1", "01/01/2020", PB, "3", "", "Y", interp="J", lab_q="X"),
    # S2: a REPLICATE higher than the initial. The initial wins (rule 1),
    # even though "higher" alone would pick the replicate.
    row("MW-1", "S2", "02/01/2020", AS, "4", "", "Y"),
    row("MW-1", "S2", "02/01/2020", AS, "9", "", "Y", test="REPLICATE"),
    # A non-detect: _value is the limit, _detect N, _detectlimit the limit.
    row("MW-1", "S2", "02/01/2020", PB, "", "2", "N", lab_q="U"),
    # S3: a LAB SPLIT - identical keys, two values. Higher wins (rule 3).
    row("MW-2", "S3", "01/01/2020", AS, "6", "", "Y"),
    row("MW-2", "S3", "01/01/2020", AS, "8", "", "Y"),
    # S3 has no lead: unpaired, so not a complete case.
    # S4: a BLANK detect flag. Unknown, not a non-detect.
    row("MW-3", "S4", "03/01/2020", AS, "7", "", ""),
    row("MW-3", "S4", "03/01/2020", PB, "1", "", "Y"),
    # QC: a field duplicate. Left out, and counted.
    row("MW-1", "S5", "01/01/2020", AS, "11", "", "Y", stype="FD"),
    # No sample code: cannot be placed on a row. Left out, and counted.
    row("MW-4", "", "01/01/2020", AS, "12", "", "Y"),
    # A FIELD REPLICATE of S1 (S1 + "_R2", typed N, as at Facility A): not
    # plotted, counted once although it carries both analytes.
    row("MW-1", "S1_R2", "01/01/2020", AS, "99", "", "Y"),
    row("MW-1", "S1_R2", "01/01/2020", PB, "99", "", "Y"),
    # A code that only LOOKS like a replicate - no "Q7" sample exists - is
    # an ordinary sample and stays.
    row("MW-5", "Q7_R2", "01/01/2020", AS, "5", "", "Y"),
    # A "_BD" duplicate of S2 and a hyphen-form replicate of S3: both left
    # out (decided 2026-10-09), each counted once.
    row("MW-1", "S2_BD", "02/01/2020", AS, "77", "", "Y"),
    row("MW-2", "S3-R2", "01/01/2020", AS, "88", "", "Y"),
]


def by_sample(store, pivot):
    cur = store.con.execute("SELECT * FROM %s ORDER BY sample" % pivot.table)
    names = [d[0] for d in cur.description]
    return {r[0]: dict(zip(names, r)) for r in cur.fetchall()}


def fixture_checks():
    table = rd.ReportTable(HEADERS, ROWS)
    store = ReportStore.from_table(table)
    p = build_pivot(store, [Variable(*AS), Variable(*PB)])
    got = by_sample(store, p)

    print("\n-- shape")
    check("one row per normal sample with a code; replicate S1_R2 left out",
          sorted(got) == ["Q7_R2", "S1", "S2", "S3", "S4"], str(sorted(got)))
    check("duplicates and replicates counted once per sample (S1_R2, S2_BD, S3-R2)",
          p.excluded["field_replicates"] == 3, p.excluded)
    check("_BD and -R2 forms are left out too",
          "S2_BD" not in got and "S3-R2" not in got, sorted(got))
    check("S1's cell is not touched by its replicate's 99",
          got["S1"]["arsenic_value"] == 10, got["S1"]["arsenic_value"])
    inc = build_pivot(store, [Variable(*AS), Variable(*PB)], replicates="include")
    check("replicates='include' brings them back",
          {"S1_R2", "S2_BD", "S3-R2"} <= set(by_sample(store, inc))
          and inc.excluded["field_replicates"] == 0,
          sorted(by_sample(store, inc)))
    check("column groups are named by analyte", [c["prefix"] for c in p.columns]
          == ["arsenic", "lead"], str([c["prefix"] for c in p.columns]))
    check("four columns per analyte",
          all(k in got["S1"] for k in ("arsenic_value", "arsenic_detect",
                                       "arsenic_qualifier", "arsenic_detectlimit")))
    check("complete cases: S1, S2, S4 carry both", p.complete == 3, p.complete)
    check("QC rows are excluded and counted", p.excluded["qc_rows"] == 1,
          p.excluded)
    check("Q7_R2 (no Q7 sample) is not mistaken for a replicate",
          "Q7_R2" in got, sorted(got))
    check("rows without a sample code are excluded and counted",
          p.excluded["no_sample_rows"] == 1, p.excluded)
    check("S3 has no lead cell: unpaired, not invented",
          got["S3"]["lead_value"] is None and got["S3"]["lead_detect"] is None)

    print("\n-- the cell rule")
    check("two methods disagreeing: the detected result wins (S1)",
          got["S1"]["arsenic_value"] == 10 and got["S1"]["arsenic_detect"] == "Y",
          (got["S1"]["arsenic_value"], got["S1"]["arsenic_detect"]))
    check("initial beats a higher replicate (S2)",
          got["S2"]["arsenic_value"] == 4, got["S2"]["arsenic_value"])
    check("a lab split resolves to the higher value (S3)",
          got["S3"]["arsenic_value"] == 8, got["S3"]["arsenic_value"])
    arsenic = p.columns[0]
    check("resolved cells are counted: S1, S2, S3", arsenic["resolved"] == 3,
          arsenic["resolved"])

    print("\n-- detection and qualifiers")
    check("a non-detect's _value is its limit (S2 lead)",
          got["S2"]["lead_value"] == 2 and got["S2"]["lead_detect"] == "N"
          and got["S2"]["lead_detectlimit"] == 2,
          (got["S2"]["lead_value"], got["S2"]["lead_detect"],
           got["S2"]["lead_detectlimit"]))
    check("_qualifier: interpreted over lab (S1 lead J, not X)",
          got["S1"]["lead_qualifier"] == "J", got["S1"]["lead_qualifier"])
    check("_qualifier: lab when interpreted is empty (S2 lead U)",
          got["S2"]["lead_qualifier"] == "U", got["S2"]["lead_qualifier"])
    check("_qualifier comes from the CHOSEN row (S1 arsenic: none, not U)",
          got["S1"]["arsenic_qualifier"] is None, got["S1"]["arsenic_qualifier"])
    check("a blank flag is _detect NULL, never inferred (S4)",
          got["S4"]["arsenic_detect"] is None and got["S4"]["arsenic_value"] == 7,
          (got["S4"]["arsenic_detect"], got["S4"]["arsenic_value"]))
    check("blank flags are counted as unknown, not as non-detects",
          arsenic["blank_detect"] == 1 and arsenic["nondetects"] == 0,
          (arsenic["blank_detect"], arsenic["nondetects"]))
    check("lead: one non-detect counted", p.columns[1]["nondetects"] == 1,
          p.columns[1]["nondetects"])

    print("\n-- agrees with the map's maximum")
    # MW-3 holds exactly one sample and no QC, so the pivot cell and the
    # store's per-location maximum must be the same row.
    agg = store.aggregate(AS[0], AS[1], how=rd.MAXIMUM, non_detect=rd.ND_FULL)
    check("cell == mapped maximum at MW-3",
          agg["MW-3"]["value"] == got["S4"]["arsenic_value"],
          (agg["MW-3"]["value"], got["S4"]["arsenic_value"]))
    # A KNOWN DIVERGENCE, recorded rather than hidden: the classic map path
    # does not exclude field duplicates/replicates (only the pivot does, as
    # decided 2026-10-09), so at MW-2 its maximum is S3-R2's 88 where the
    # pivot plots the primary's 8. If the map path ever adopts the rule,
    # this check flips and should become an equality.
    check("known: the map's maximum at MW-2 still counts the replicate",
          agg["MW-2"]["value"] == 88 and got["S3"]["arsenic_value"] == 8,
          (agg["MW-2"]["value"], got["S3"]["arsenic_value"]))

    print("\n-- splitting by method is an option, not the default")
    ps = build_pivot(store, [Variable(*AS), Variable(*PB)], split="method")
    names = [c["prefix"] for c in ps.columns]
    check("split='method' gives one column group per method",
          names == ["arsenic_a", "arsenic_b", "lead_a"], str(names))
    gs = by_sample(store, ps)
    check("the method-B non-detect is its own cell when split",
          gs["S1"]["arsenic_b_value"] == 20 and gs["S1"]["arsenic_b_detect"] == "N",
          (gs["S1"]["arsenic_b_value"], gs["S1"]["arsenic_b_detect"]))
    # By lab too. The matched rows carry only the columns the pivot reads,
    # plus the split column; a split the copy forgot would fail here.
    labs = ReportStore.from_table(rd.ReportTable(HEADERS, [
        row("MW-9", "L1", "01/01/2020", AS, "3", "", "Y", lab="LAB1"),
        row("MW-9", "L1", "01/01/2020", AS, "5", "", "Y", lab="LAB2")]))
    pl = build_pivot(labs, [Variable(*AS)], split="lab")
    gl = by_sample(labs, pl)
    check("split='lab' gives one column group per lab, each its own value",
          [c["prefix"] for c in pl.columns] == ["arsenic_lab1", "arsenic_lab2"]
          and (gl["L1"]["arsenic_lab1_value"], gl["L1"]["arsenic_lab2_value"]) == (3, 5),
          ([c["prefix"] for c in pl.columns], gl.get("L1")))

    print("\n-- refusals")
    mixed = rd.ReportTable(HEADERS, [
        row("MW-1", "S1", "01/01/2020", ("Zinc", "7440-66-6"), "5", "", "Y"),
        row("MW-1", "S2", "02/01/2020", ("Zinc", "7440-66-6"), "0.5", "", "Y",
            unit="mg/L")])
    try:
        build_pivot(ReportStore.from_table(mixed), [Variable("Zinc", "7440-66-6")])
        message = None
    except PivotError as exc:
        message = str(exc)
    check("mixed units are refused, naming both",
          message is not None and "ug/L" in message and "mg/L" in message,
          str(message))
    dt = rd.ReportTable(HEADERS, [
        row("MW-1", "S1", "01/01/2020", ("Copper", "7440-50-8"), "5", "", "Y",
            fraction="T"),
        row("MW-1", "S1", "01/01/2020", ("Copper", "7440-50-8"), "1", "", "Y",
            fraction="D")])
    dt_store = ReportStore.from_table(dt)
    try:
        build_pivot(dt_store, [Variable("Copper", "7440-50-8")])
        message = None
    except PivotError as exc:
        message = str(exc)
    check("dissolved beside total is refused", message is not None
          and "dissolved" in message, str(message))
    one = build_pivot(dt_store, [Variable("Copper", "7440-50-8", fraction="D")])
    check("...and a chosen fraction is accepted", one.rows == 1, one.rows)
    no_sample = rd.ReportTable([h for h in HEADERS if h != "SYS_SAMPLE_CODE"],
                               [[c for i, c in enumerate(r)
                                 if HEADERS[i] != "SYS_SAMPLE_CODE"]
                                for r in ROWS])
    try:
        build_pivot(ReportStore.from_table(no_sample), [Variable(*AS)])
        message = None
    except PivotError as exc:
        message = str(exc)
    check("no SYS_SAMPLE_CODE column is refused", message is not None
          and "SYS_SAMPLE_CODE" in message, str(message))

    print("\n-- the row scope applies first, and names are never reused")
    store.set_row_scope(datetime.date(2020, 1, 15), None, None)
    scoped = build_pivot(store, [Variable(*AS), Variable(*PB)])
    check("a date scope narrows the pivot (S2, S4 only)",
          sorted(by_sample(store, scoped)) == ["S2", "S4"],
          str(sorted(by_sample(store, scoped))))
    store.set_row_scope(None, None, None)
    again = build_pivot(store, [Variable(*AS), Variable(*PB)])
    check("an identical build gets a NEW table name (Mosaic cache)",
          again.table != p.table, (again.table, p.table))
    for _ in range(KEEP_TABLES + 2):
        build_pivot(store, [Variable(*AS)])
    # A pivot is a view (p_) over a table of per-sample maps (pm_); both
    # must go, or the maps of every pivot ever built stay in memory.
    kept = store.con.execute(
        "SELECT count(*) FROM duckdb_views() WHERE view_name LIKE 'p\\_%' "
        "ESCAPE '\\'").fetchone()[0]
    maps_kept = store.con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE table_name LIKE 'pm\\_%' "
        "ESCAPE '\\'").fetchone()[0]
    check("old pivots and their maps are dropped past KEEP_TABLES",
          (kept, maps_kept) == (KEEP_TABLES, KEEP_TABLES), (kept, maps_kept))
    check("slug makes safe, readable identifiers",
          (slug("1,1-Dichloroethane"), slug("Lead")) == ("a_1_1_dichloroethane", "lead"),
          (slug("1,1-Dichloroethane"), slug("Lead")))
    empty = build_pivot(store, [Variable(*AS), Variable("Mercury", "7439-97-6")])
    check("an analyte with no data is reported, not silently dropped",
          [v.chemical for v in empty.empty] == ["Mercury"],
          [v.chemical for v in empty.empty])


def facility_a_checks():
    print("\n-- Facility A (real parquet)")
    root = os.path.join(ROOT, "data", "facility_a")
    if not os.path.isdir(root):
        print("[SKIP] no data/facility_a - the real-data checks did not run")
        return
    store = ReportStore.from_manifest(root)
    t = datetime.datetime.now()
    p = build_pivot(store, [Variable("Manganese", fraction="T"),
                            Variable("Lead", fraction="T")], matrix=["WS"])
    took = (datetime.datetime.now() - t).total_seconds()
    pair = store.con.execute("""
        SELECT
          sum(CASE WHEN manganese_detect = 'Y' AND lead_detect = 'Y' THEN 1 ELSE 0 END),
          sum(CASE WHEN manganese_detect = 'Y' AND lead_detect = 'N' THEN 1 ELSE 0 END),
          sum(CASE WHEN manganese_detect = 'N' AND lead_detect = 'Y' THEN 1 ELSE 0 END),
          sum(CASE WHEN manganese_detect = 'N' AND lead_detect = 'N' THEN 1 ELSE 0 END)
        FROM %s""" % p.table).fetchone()
    # tools/measure_pivot.py, 2026-10-08: 1,573 / 19,770 / 58 / 41,821. That
    # counted a sample as detected if ANY of its rows detected; the cell rule
    # puts detected first, so the two must agree exactly.
    check("Mn x Pb WS/T detection classes match measure_pivot.py",
          tuple(pair) == (1573, 19770, 58, 41821), str(pair))
    check("Mn x Pb complete cases match measure_pivot.py (63,222)",
          p.complete == 63222, p.complete)
    print("     (built in %.2f s; %s rows, %s resolved Mn cells)"
          % (took, format(p.rows, ",d"), format(p.columns[0]["resolved"], ",d")))
    store.close()


if __name__ == "__main__":
    fixture_checks()
    facility_a_checks()
    print("\n%s" % ("ALL PIVOT TESTS PASSED" if not FAILED
                    else "%d FAILED" % len(FAILED)))
    sys.exit(1 if FAILED else 0)
