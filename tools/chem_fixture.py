"""A synthetic facility with one location per chemistry rule.

Phase 1 of docs/chemduck-adoption-plan.md asks for "a shared fixture
facility, synthetic and in this repository, [that] exercises every rule".
Each location below is built to show exactly one rule, and says what the
plugin's map and ChemDuck's should then do about it, per aggregation:
agree (an empty set), or differ for the named reasons, each of which must be
in tools/test_chemduck_parity.py's list of intended differences. A location
that agrees when it should differ is as much a failure as the reverse: it
means the fixture no longer exercises its rule.

Facility A carries none of several of these rules (no blank fraction, no QC
sample types, no non-reportable result, no undated sample, 2026-10-29), so
this is the only place they are exercised.

The rows are EQuIS Analytical Results II columns, as the pipeline exports
them; `write(folder)` lays them out as the pipeline does
(matrix=<code>/fraction=<code>/part-0000.parquet and a manifest.json), so
both of the adapter's ways of opening a facility read them.
"""
import json
import os

# The columns both readers use. Anything else an ARII export carries is
# absent here, which ChemDuck records as absent and the plugin ignores.
COLUMNS = [
    "FACILITY_CODE", "SYS_LOC_CODE", "SYS_SAMPLE_CODE", "SAMPLE_DATE",
    "SAMPLE_TYPE_CODE", "START_DEPTH", "END_DEPTH", "DEPTH_UNIT", "MATRIX_CODE",
    "ANALYTIC_METHOD", "ANALYSIS_DATE", "FRACTION", "TEST_TYPE", "CAS_RN",
    "CHEMICAL_NAME", "REPORT_RESULT_VALUE", "REPORT_RESULT_UNIT",
    "REPORT_RESULT_LIMIT", "REPORT_METHOD_DETECTION_LIMIT", "REPORT_REPORTING_LIMIT",
    "REPORT_QUANTITATION_LIMIT", "REPORTABLE_RESULT", "DETECT_FLAG",
    "RESULT_NUMERIC", "RESULT_UNIT", "X_COORD", "Y_COORD", "LONGITUDE", "LATITUDE",
]
ARSENIC = ("Arsenic", "7440-38-2")
MERCURY = ("Mercury", "7439-97-6")
LEAD = ("Lead", "7439-92-1")
IRON = ("Iron", "7439-89-6")
ALUMINUM = ("Aluminum", "7429-90-5")


def result(loc, sample, date, value, detect, limit=1.0, matrix="WG", fraction="T",
           method="6010B", unit="ug/L", top=None, bottom=None, chemical=ARSENIC, **raw):
    """One result as EQuIS reports it. A non-detect has no RESULT_NUMERIC,
    and its REPORT_RESULT_VALUE is the limit it fell below."""
    detected = detect == "Y"
    row = {
        "FACILITY_CODE": "FIXTURE", "SYS_LOC_CODE": loc, "SYS_SAMPLE_CODE": sample,
        "SAMPLE_DATE": date, "SAMPLE_TYPE_CODE": "N",
        "START_DEPTH": top, "END_DEPTH": bottom, "DEPTH_UNIT": "m" if top is not None else None,
        "MATRIX_CODE": matrix, "ANALYTIC_METHOD": method, "ANALYSIS_DATE": date,
        "FRACTION": fraction, "TEST_TYPE": "INITIAL",
        "CHEMICAL_NAME": chemical[0], "CAS_RN": chemical[1],
        "REPORT_RESULT_VALUE": value if detected else limit, "REPORT_RESULT_UNIT": unit,
        "REPORT_RESULT_LIMIT": limit, "REPORT_METHOD_DETECTION_LIMIT": limit,
        "REPORT_REPORTING_LIMIT": limit, "REPORT_QUANTITATION_LIMIT": None,
        "REPORTABLE_RESULT": "Yes", "DETECT_FLAG": detect,
        "RESULT_NUMERIC": value if detected else None, "RESULT_UNIT": unit,
        "X_COORD": 500000.0, "Y_COORD": 7000000.0, "LONGITUDE": -43.0, "LATITUDE": -20.0,
    }
    unknown = set(raw) - set(COLUMNS)
    if unknown:
        raise KeyError("not a fixture column: %s" % ", ".join(sorted(unknown)))
    row.update(raw)
    return row


# (location, what it exercises, rows, {aggregation: expected differences}).
# Expected differences are the keys of test_chemduck_parity.INTENDED.
AGREE = frozenset()


def _cases():
    r = result
    return [
        ("AGREE", "two detects on two dates: nothing to decide",
         [r("AGREE", "AG1", "2020-01-01", 3.0, "Y"),
          r("AGREE", "AG2", "2021-01-01", 2.0, "Y")],
         {"most_recent": AGREE, "maximum": AGREE}),
        ("ND-ABOVE-DETECT", "a non-detect's limit above a detection is never the maximum",
         [r("ND-ABOVE-DETECT", "NA1", "2020-01-01", 3.0, "Y"),
          r("ND-ABOVE-DETECT", "NA2", "2020-02-01", None, "N", limit=50.0)],
         {"most_recent": AGREE, "maximum": AGREE}),
        ("ND-ONLY", "only non-detects: the location still reports one, at its limit",
         [r("ND-ONLY", "NO1", "2020-01-01", None, "N", limit=6.0),
          r("ND-ONLY", "NO2", "2021-01-01", None, "N", limit=2.0)],
         {"most_recent": AGREE, "maximum": AGREE}),
        ("TIMES", "one date, two times of day: the later time is the more recent",
         [r("TIMES", "TI1", "2020-01-01 08:00:00", 4.0, "Y"),
          r("TIMES", "TI2", "2020-01-01 15:00:00", 6.0, "Y")],
         {"most_recent": AGREE, "maximum": AGREE}),
        ("FRACTIONS", "dissolved and total from one sample stay apart",
         [r("FRACTIONS", "FR1", "2020-01-01", 4.0, "Y", fraction="D"),
          r("FRACTIONS", "FR1", "2020-01-01", 6.0, "Y", fraction="T")],
         {"most_recent": AGREE, "maximum": AGREE}),
        ("BLANK-FRACTION", "a result with no fraction joins neither fraction",
         [r("BLANK-FRACTION", "BF1", "2021-01-01", 9.0, "Y", fraction=None),
          r("BLANK-FRACTION", "BF2", "2020-01-01", 1.5, "Y")],
         {"most_recent": AGREE, "maximum": AGREE}),
        ("Case-Code", "location codes are kept as reported",
         [r("Case-Code", "CC1", "2020-01-01", 4.0, "Y")],
         {"most_recent": AGREE, "maximum": AGREE}),
        ("CASE-CODE", "... so a code in another case is another location",
         [r("CASE-CODE", "CC2", "2021-01-01", 6.0, "Y")],
         {"most_recent": AGREE, "maximum": AGREE}),
        ("SOIL", "a solid, fraction N (not applicable) passes through unrenamed",
         [r("SOIL", "SO1", "2020-01-01", 12.0, "Y", matrix="SO", fraction="N", unit="mg/kg"),
          r("SOIL", "SO2", "2021-01-01", None, "N", matrix="SO", fraction="N", unit="mg/kg",
            limit=0.5)],
         {"most_recent": AGREE, "maximum": AGREE}),
        ("DEPTHS", "two depth intervals on one date: two points in ChemDuck",
         [r("DEPTHS", "DE1", "2020-01-01", 3.0, "Y", top=0.0, bottom=1.0),
          r("DEPTHS", "DE2", "2020-01-01", 7.0, "Y", top=1.0, bottom=2.0)],
         {"most_recent": {"points"}, "maximum": {"points"}}),
        ("METHOD-CLASS", "a field method beside a lab method: two variables in ChemDuck",
         [r("METHOD-CLASS", "MC1", "2020-01-01", 4.0, "Y", method="FIELD"),
          r("METHOD-CLASS", "MC2", "2021-01-01", 6.0, "Y")],
         {"most_recent": {"method_classes"}, "maximum": {"method_classes"}}),
        ("SAME-DATE", "two samples on one date: ChemDuck ranks them, the plugin takes "
                      "the first row",
         [r("SAME-DATE", "SD1", "2020-01-01", 3.0, "Y"),
          r("SAME-DATE", "SD2", "2020-01-01", 7.0, "Y")],
         {"most_recent": {"same_date"}, "maximum": AGREE}),
        ("RERUNS", "one sample, two methods, disagreeing about detection, the "
                   "non-detect first",
         [r("RERUNS", "RR1", "2020-01-01", None, "N", limit=5.0, method="6020A"),
          r("RERUNS", "RR1", "2020-01-01", 3.0, "Y", method="6010B")],
         {"most_recent": {"reruns"}, "maximum": AGREE}),
        ("DILUTION", "a non-reportable dilution of one sample: ChemDuck keeps the "
                     "reportable result",
         [r("DILUTION", "DI1", "2020-01-01", 4.0, "Y"),
          r("DILUTION", "DI1", "2020-01-01", 40.0, "Y", REPORTABLE_RESULT="No",
            TEST_TYPE="DILUTION")],
         {"most_recent": AGREE, "maximum": {"reruns"}}),
        ("TIE", "equal maxima on two dates: ChemDuck reports the later",
         [r("TIE", "TE1", "2020-01-01", 5.0, "Y"),
          r("TIE", "TE2", "2021-01-01", 5.0, "Y")],
         {"most_recent": AGREE, "maximum": {"tie_later_date"}}),
        ("QUANT-LIMIT", "a non-detect with only a quantitation limit",
         [r("QUANT-LIMIT", "QL1", "2020-01-01", None, "N", limit=None,
            REPORT_QUANTITATION_LIMIT=8.0)],
         {"most_recent": {"quantitation_limit"}, "maximum": {"quantitation_limit"}}),
        ("ND-LIMIT", "a non-detect whose reported value is not its report limit",
         [r("ND-LIMIT", "NL1", "2020-01-01", None, "N", limit=None,
            REPORT_RESULT_VALUE=5.0, REPORT_RESULT_LIMIT=2.0,
            REPORT_METHOD_DETECTION_LIMIT=1.0, REPORT_REPORTING_LIMIT=5.0)],
         {"most_recent": {"nd_limit"}, "maximum": {"nd_limit"}}),
        ("NOT-REPORTABLE", "a later sample marked not reportable",
         [r("NOT-REPORTABLE", "NR1", "2020-01-01", 4.0, "Y"),
          r("NOT-REPORTABLE", "NR2", "2021-01-01", 40.0, "Y", REPORTABLE_RESULT="No")],
         {"most_recent": {"non_reportable"}, "maximum": {"non_reportable"}}),
        ("ONLY-NOT-REPORTABLE", "a location whose one result is not reportable",
         [r("ONLY-NOT-REPORTABLE", "ON1", "2020-01-01", 4.0, "Y", REPORTABLE_RESULT="No")],
         {"most_recent": {"non_reportable"}, "maximum": {"non_reportable"}}),
        ("FIELD-DUP", "a field duplicate, later and higher than its parent",
         [r("FIELD-DUP", "FD1", "2020-01-01", 4.0, "Y"),
          r("FIELD-DUP", "FD2", "2020-03-01", 9.0, "Y", SAMPLE_TYPE_CODE="FD")],
         {"most_recent": {"qc_samples"}, "maximum": {"qc_samples"}}),
        ("MIXED-UNITS", "one variable in two water units: ChemDuck converts at load, "
                        "the plugin's guard asks",
         [r("MIXED-UNITS", "MU1", "2020-01-01", 200.0, "Y", chemical=IRON),
          r("MIXED-UNITS", "MU2", "2021-01-01", 0.03, "Y", chemical=IRON, unit="mg/L")],
         {"most_recent": {"units_converted"}, "maximum": {"units_converted"}}),
        ("MIXED-SOLID-UNITS", "water in its unit and a solid's, as Facility A's Mercury "
                              "in WS/D (7,818 ug/L, 130 ug/kg): both stop the map",
         [r("MIXED-SOLID-UNITS", "MS1", "2020-01-01", 0.2, "Y", chemical=MERCURY),
          r("MIXED-SOLID-UNITS", "MS2", "2021-01-01", 30.0, "Y", chemical=MERCURY,
            unit="ug/kg")],
         {"most_recent": {"units_refused"}, "maximum": {"units_refused"}}),
        ("UNITS-BY-CLASS", "a lab method in mg/kg and an unclassified one in me/L, as "
                           "Facility A's Aluminum in SE: one unit per ChemDuck variable",
         [r("UNITS-BY-CLASS", "UC1", "2020-01-01", 900.0, "Y", chemical=ALUMINUM,
            matrix="SE", unit="mg/kg"),
          r("UNITS-BY-CLASS", "UC2", "2021-01-01", None, "N", chemical=ALUMINUM,
            matrix="SE", unit="me/L", method="UNK", limit=0.1)],
         {"most_recent": {"units_by_method_class"}, "maximum": {"units_by_method_class"}}),
        ("PROFILE", "one station sampled at two depths, every result an equal "
                    "non-detect: the roll-up's tie between points",
         [r("PROFILE", "PR1", "2020-01-01", None, "N", limit=10.0, top=0.5, bottom=0.5),
          r("PROFILE", "PR2", "2021-01-01", None, "N", limit=10.0, top=0.5, bottom=0.5),
          r("PROFILE", "PR3", "2019-01-01", None, "N", limit=10.0, top=2.0, bottom=2.0)],
         {"most_recent": {"points"}, "maximum": {"points"}}),
        ("SOLID-UNIT", "water in a solid's unit, its only unit: ChemDuck refuses, "
                       "the plugin maps",
         [r("SOLID-UNIT", "SU1", "2020-01-01", 30.0, "Y", chemical=LEAD, unit="ug/kg")],
         {"most_recent": {"unit_contradicts_matrix"},
          "maximum": {"unit_contradicts_matrix"}}),
        ("ONLY-BLANK", "a location sampled only by an equipment blank",
         [r("ONLY-BLANK", "EB1", "2020-01-01", 0.2, "Y", SAMPLE_TYPE_CODE="EB")],
         {"most_recent": {"qc_samples"}, "maximum": {"qc_samples"}}),
    ]


CASES = _cases()


# Rows ChemDuck can't load today, each in a facility of its own: found
# 2026-10-29, raised with ChemDuck. Its samples table requires a date and a
# matrix (load_data.py, insert_samples' WHERE) and its results insert does
# not, so the foreign key refuses the whole load. The plugin maps both
# rows: an undated result only fills an empty slot, and a filter on matrix
# leaves the blank one out.
REFUSED = [
    ("UNDATED", "a sample with no date",
     [result("UNDATED", "UD1", None, 9.0, "Y"),
      result("UNDATED", "UD2", "2020-01-01", 2.0, "Y")]),
    ("BLANK-MATRIX", "a sample with no matrix",
     [result("BLANK-MATRIX", "BM1", "2021-01-01", 9.0, "Y", matrix=None),
      result("BLANK-MATRIX", "BM2", "2020-01-01", 1.5, "Y")]),
]


def rows():
    return [row for _loc, _what, case_rows, _expect in CASES for row in case_rows]


def write(folder, rows_=None):
    """The rows as the pipeline lays a facility out. Returns the manifest's
    path. A blank matrix or fraction is written under the code '_' so the
    partition path stays a path; the rows keep their blank.

    Written by DuckDB, not pyarrow: QGIS 4.2.3's pyarrow 25.0.1 refused this
    table ("Couldn't serialize thrift: Internal buffer size overflow",
    2026-10-29) where plain Python's pyarrow 24 wrote it, and the plugin's
    own DuckDB bundle has no pyarrow at all. Forty rows, so one INSERT per
    row costs nothing here."""
    import duckdb
    rows_ = rows() if rows_ is None else rows_
    con = duckdb.connect()
    con.execute("CREATE TABLE fixture (%s)" % ", ".join(
        '"%s" %s' % (c, "DOUBLE" if c in NUMERIC else "VARCHAR") for c in COLUMNS))
    if rows_:
        con.executemany("INSERT INTO fixture VALUES (%s)" % ", ".join("?" * len(COLUMNS)),
                        [[row.get(c) for c in COLUMNS] for row in rows_])
    groups = sorted({(row["MATRIX_CODE"] or "_", row["FRACTION"] or "_") for row in rows_})
    partitions = []
    for matrix, fraction in groups:
        relative = "matrix=%s/fraction=%s/part-0000.parquet" % (matrix, fraction)
        path = os.path.join(folder, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        n = con.execute(
            "COPY (SELECT * FROM fixture WHERE coalesce(MATRIX_CODE, '_') = ? "
            "AND coalesce(FRACTION, '_') = ?) TO '%s' (FORMAT parquet)"
            % path.replace("\\", "/").replace("'", "''"), [matrix, fraction]).fetchone()[0]
        partitions.append({"path": relative, "matrix": matrix, "fraction": fraction,
                           "rows": n, "bytes": os.path.getsize(path)})
    con.close()
    manifest = {"facility": "fixture", "version": 1, "rows": len(rows_),
                "partitions": partitions}
    path = os.path.join(folder, "manifest.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=1)
    return path


# The real export's types (Facility A's partitions): dates are text,
# numbers are doubles, the rest text.
NUMERIC = {"START_DEPTH", "END_DEPTH", "REPORT_RESULT_VALUE", "REPORT_RESULT_LIMIT",
           "REPORT_METHOD_DETECTION_LIMIT", "REPORT_REPORTING_LIMIT",
           "REPORT_QUANTITATION_LIMIT", "RESULT_NUMERIC", "X_COORD", "Y_COORD",
           "LONGITUDE", "LATITUDE"}
