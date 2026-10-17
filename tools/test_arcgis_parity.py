"""The ArcGIS export against the plugin's own rules: one answer, two writers.

    python tools/test_arcgis_parity.py            # fixture, plus data/facility_a if present
    python tools/test_arcgis_parity.py --fixture-only

arcgis_export.py lives in pipeline/, which runs on its own - Python and
pyarrow, no plugin - and so imports nothing from it. Its non-detect
columns are therefore a SECOND COPY of the rules in
ReportStore._create_views - the divergence CLAUDE.md warns about. This is
the gate that makes the copy safe:

  * its constants (detect values, the value ladder's columns, units, date
    formats) must equal report_data's and the store's;
  * every row it writes - placed and unplaced - must carry the same
    detected / value_full / at_limit / unit / sample_date as the store's
    view over the same parquet, compared as multisets;
  * a deliberately broken exporter must FAIL that comparison, so a pass
    means something.

Needs duckdb and pyarrow.
"""
import argparse
import importlib
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import pyarrow as pa                                 # noqa: E402
import pyarrow.parquet as pq                         # noqa: E402

import arcgis_export as ax                           # noqa: E402
from equis_loader import report_data as rd           # noqa: E402
from equis_loader import store                       # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


# ------------------------------------------------------------- constants

def check_constants():
    check("detect values equal the store's", ax.DETECT_TRUE == store.DETECT_TRUE)
    for key, ours in (("detect", ax.DETECT_COLUMNS), ("value", ax.VALUE_COLUMNS),
                      ("reported", ax.REPORTED_COLUMNS), ("limit", ax.LIMIT_COLUMNS),
                      ("unit", ax.UNIT_COLUMNS)):
        check("%s columns equal report_data's" % key, tuple(rd.COLUMNS[key]) == ours,
              "%s vs %s" % (ours, rd.COLUMNS[key]))
    check("date formats equal report_data's", ax.DATE_FORMATS == rd.DATE_FORMATS)


# --------------------------------------------------------------- fixture

# (flag, RESULT_NUMERIC, REPORT_RESULT_VALUE, REPORT_MDL, MDL, RESULT_UNIT,
#  REPORT_RESULT_UNIT, what the row exercises)
LADDER = [
    ("Y", 5.0, 5.0, 0.1, 0.1, "ug/L", "ug/L", "a detection"),
    ("N", None, 0.5, 0.2, 0.2, "", "ug/L", "ND: reported value, unit from REPORT_RESULT_UNIT"),
    ("N", None, None, 0.2, 0.3, None, "ug/L", "ND: only the first limit column"),
    ("N", None, None, None, 0.3, None, "ug/L", "ND: first limit column empty, a later one not"),
    ("N", 0.3, 0.3, 0.3, None, "ug/L", None, "ND with RESULT_NUMERIC filled"),
    ("Y", None, 7.0, None, None, None, "ug/L", "detected, RESULT_NUMERIC empty"),
    (None, 2.0, 2.0, None, None, "ug/L", None, "no flag at all"),
    ("yes", 4.0, 4.0, None, None, "ug/L", None, "lower-case yes"),
    ("N", None, None, None, None, " ", "  ", "nothing to value, blank units"),
]


def write_fixture(root):
    """Two cells with different schemas, as the real repack writes them."""
    def cell(matrix, fraction, rows, leachate_typed):
        n = len(rows)
        columns = {
            "SYS_LOC_CODE": ["L%d" % (i % 3) for i in range(n)],
            "LOC_NAME": ["Loc %d" % (i % 3) for i in range(n)],
            "SYS_SAMPLE_CODE": ["S%s%d" % (matrix, i) for i in range(n)],
            "CHEMICAL_NAME": ["Arsenic"] * n,
            "MATRIX_CODE": [matrix] * n,
            "FRACTION": [fraction] * n,
            "SAMPLE_TYPE_CODE": ["N"] * n,
            "DETECT_FLAG": [r[0] for r in rows],
            "RESULT_NUMERIC": pa.array([r[1] for r in rows], pa.float64()),
            "REPORT_RESULT_VALUE": pa.array([r[2] for r in rows], pa.float64()),
            "REPORT_METHOD_DETECTION_LIMIT": pa.array([r[3] for r in rows], pa.float64()),
            "METHOD_DETECTION_LIMIT": pa.array([r[4] for r in rows], pa.float64()),
            "RESULT_UNIT": [r[5] for r in rows],
            "REPORT_RESULT_UNIT": [r[6] for r in rows],
            "LONGITUDE": pa.array([None if i == 1 else -43.5 + i * 0.01 for i in range(n)],
                                  pa.float64()),
            "LATITUDE": pa.array([None if i == 1 else -19.0 - i * 0.01 for i in range(n)],
                                 pa.float64()),
            "LEACHATE_DATE": (pa.array(["5/1/2019"] * n) if leachate_typed
                              else pa.nulls(n)),
        }
        dates = ["4/4/2019 9:15:00 AM", "2019-04-05", "4/6/2019", ""]
        columns["SAMPLE_DATE"] = [dates[i % len(dates)] for i in range(n)]
        table = pa.table(columns)
        # The repack's in-file Hive keys.
        table = table.append_column("matrix", pa.array([matrix] * n).dictionary_encode())
        table = table.append_column("fraction", pa.array([fraction] * n).dictionary_encode())
        directory = os.path.join(root, "matrix=" + matrix, "fraction=" + fraction)
        os.makedirs(directory)
        pq.write_table(table, os.path.join(directory, "part-0000.parquet"))

    cell("WS", "D", LADDER, leachate_typed=True)
    cell("SE", "N", LADDER[::-1], leachate_typed=False)


# ------------------------------------------------------------ comparison

def compare(root, label, expect_equal=True):
    """Every exported row against the store's view, as multisets."""
    out = os.path.join(root, ax.OUT_DIR)
    files = sorted(os.path.join(out, f) for f in os.listdir(out)
                   if f.endswith(".parquet") and f != ax.LOCATIONS_FILE)
    wanted = ["SYS_LOC_CODE", "CHEMICAL_NAME", "SYS_SAMPLE_CODE", "SAMPLE_DATE",
              "IS_DETECT", "VALUE_OR_LIMIT", "VALUE_IS_LIMIT", "VALUE_UNIT"]
    exported = pa.concat_tables(
        [pq.read_table(f, columns=wanted) for f in files])
    glob = os.path.join(root, "matrix=*", "fraction=*", "*.parquet").replace("\\", "/")
    s = store.ReportStore.from_parquet(glob)
    s.con.execute("SET memory_limit='4GB'")
    s.con.register("exported", exported)
    ours = """SELECT SYS_LOC_CODE, CHEMICAL_NAME, SYS_SAMPLE_CODE, SAMPLE_DATE,
                     IS_DETECT, VALUE_OR_LIMIT, VALUE_IS_LIMIT, VALUE_UNIT FROM exported"""
    theirs = """SELECT location, chemical, sample, sample_date,
                       detected, value_full, at_limit, unit FROM v_results"""
    total = s.con.execute("SELECT count(*) FROM v_results").fetchone()[0]
    only_ours = s.con.execute("SELECT count(*) FROM (%s EXCEPT ALL %s)"
                              % (ours, theirs)).fetchone()[0]
    only_theirs = s.con.execute("SELECT count(*) FROM (%s EXCEPT ALL %s)"
                                % (theirs, ours)).fetchone()[0]
    detail = "%d rows; %d only in the export, %d only in the store" % (
        total, only_ours, only_theirs)
    same = (only_ours == 0 and only_theirs == 0 and exported.num_rows == total)
    if expect_equal:
        check("%s: export equals the store row for row" % label, same, detail)
        if not same:
            print(s.con.execute("%s EXCEPT ALL %s LIMIT 5" % (ours, theirs)).fetchall())
    else:
        check("%s: a broken exporter is caught" % label, not same, detail)
    s.con.close()


def run_export(root):
    ax.export(root, say=lambda *_: None)


def mutation(root):
    """Halve non-detects in the exporter, and the gate must notice."""
    original = ax.nondetect_columns

    def halved(batch):
        import pyarrow.compute as pc
        detected, base, at_limit, unit = original(batch)
        return [detected, pc.if_else(at_limit, pc.divide(base, 2.0), base),
                at_limit, unit]

    ax.nondetect_columns = halved
    try:
        ax.export(root, force=True, say=lambda *_: None)
        compare(root, "mutation (1/2 DL baked in)", expect_equal=False)
    finally:
        ax.nondetect_columns = original
        importlib.reload(ax)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-only", action="store_true")
    args = parser.parse_args()

    check_constants()
    work = tempfile.mkdtemp(prefix="arcgis_parity_")
    try:
        write_fixture(work)
        run_export(work)
        compare(work, "fixture")
        mutation(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    real = os.path.join(ROOT, "data", "facility_a")
    if args.fixture_only:
        print("[SKIP] real facility: --fixture-only")
    elif not os.path.isdir(os.path.join(real, "matrix=WS")):
        print("[SKIP] real facility: no data/facility_a partitions here")
    else:
        run_export(real)
        compare(real, "real facility")

    print("\n%d failure(s)" % len(FAILURES))
    raise SystemExit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
