"""ChemDuck's sidecars, published beside a facility's manifest.

    python pipeline/tests/test_chemduck_sidecars.py

The real step on a small facility laid out as the pipeline lays one out
(matrix=/fraction= partitions, the manifest publish_manifest.py writes):

- it writes <root>/chemduck/ by ChemDuck's own publish, and its
  sidecars.json records the hash of the manifest beside it;
- ChemDuck opens the folder over the partitions and answers;
- a ChemDuck unit conversion is an ATTENTION line, and none without one;
- a refused publish exits 2 and leaves the previous folder as it was;
- missing config refuses, naming what is missing;
- it imports no plugin code.

Needs duckdb and pyyaml (`pip install -e "pipeline[chemduck]"`); skips
without them.
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

try:
    import duckdb  # noqa: F401
    import yaml    # noqa: F401
except ImportError as exc:
    print("needs duckdb and pyyaml: %s" % exc)
    print("SKIPPED")
    raise SystemExit(0)

from parquet_source import chemduck_sidecars as cs      # noqa: E402
from parquet_source import publish_manifest             # noqa: E402

FAILURES = []

COLUMNS = ["FACILITY_CODE", "SYS_LOC_CODE", "SYS_SAMPLE_CODE", "SAMPLE_DATE",
           "SAMPLE_TYPE_CODE", "START_DEPTH", "END_DEPTH", "DEPTH_UNIT", "MATRIX_CODE",
           "ANALYTIC_METHOD", "FRACTION", "TEST_TYPE", "CAS_RN", "CHEMICAL_NAME",
           "REPORT_RESULT_VALUE", "REPORT_RESULT_UNIT", "REPORT_RESULT_LIMIT",
           "REPORTABLE_RESULT", "DETECT_FLAG", "RESULT_NUMERIC", "RESULT_UNIT",
           "LONGITUDE", "LATITUDE"]
NUMERIC = {"START_DEPTH", "END_DEPTH", "REPORT_RESULT_VALUE", "REPORT_RESULT_LIMIT",
           "RESULT_NUMERIC", "LONGITUDE", "LATITUDE"}


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + str(detail)) if detail != "" else ""))
    if not condition:
        FAILURES.append(label)


def result(loc, sample, date, chemical, value, detect, unit="ug/L", limit=1.0):
    detected = detect == "Y"
    return {"FACILITY_CODE": "T", "SYS_LOC_CODE": loc, "SYS_SAMPLE_CODE": sample,
            "SAMPLE_DATE": date, "SAMPLE_TYPE_CODE": "N", "MATRIX_CODE": "WG",
            "ANALYTIC_METHOD": "6010B", "FRACTION": "T", "TEST_TYPE": "INITIAL",
            "CHEMICAL_NAME": chemical[0], "CAS_RN": chemical[1],
            "REPORT_RESULT_VALUE": value if detected else limit,
            "REPORT_RESULT_UNIT": unit, "REPORT_RESULT_LIMIT": limit,
            "REPORTABLE_RESULT": "Yes", "DETECT_FLAG": detect,
            "RESULT_NUMERIC": value if detected else None, "RESULT_UNIT": unit,
            "LONGITUDE": -43.0 - len(loc) / 100.0, "LATITUDE": -20.0}


ARSENIC = ("Arsenic", "7440-38-2")
IRON = ("Iron", "7439-89-6")
ROWS = [result("MW-1", "S1", "2020-01-01", ARSENIC, 3.0, "Y"),
        result("MW-1", "S2", "2021-01-01", ARSENIC, None, "N", limit=2.0),
        result("MW-22", "S3", "2021-06-01", ARSENIC, 7.5, "Y")]
# Iron in two units EQuIS had no factor between: what the collect leaves.
MIXED = [result("MW-1", "S1", "2020-01-01", IRON, 300.0, "Y", unit="ug/L"),
         result("MW-22", "S3", "2021-06-01", IRON, 0.4, "Y", unit="mg/L")]


def facility(rows, chemduck=None):
    """A root as the pipeline leaves it after publish_manifest.py."""
    root = tempfile.mkdtemp(prefix="sidecars_")
    config = {"facility": 7, "sample_types": "N"}
    if chemduck is not None:
        config["chemduck"] = chemduck
    with open(os.path.join(root, "pipeline.json"), "w") as handle:
        json.dump(config, handle)
    write_partition(root, rows)
    return root


def write_partition(root, rows):
    path = os.path.join(root, "matrix=WG", "fraction=T", "part-0000.parquet")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = duckdb.connect()
    con.execute("CREATE TABLE t (%s)" % ", ".join(
        '"%s" %s' % (c, "DOUBLE" if c in NUMERIC else "VARCHAR") for c in COLUMNS))
    con.executemany("INSERT INTO t VALUES (%s)" % ", ".join("?" * len(COLUMNS)),
                    [[row.get(c) for c in COLUMNS] for row in rows])
    con.execute("COPY t TO '%s' (FORMAT parquet)" % path.replace("\\", "/"))
    con.close()
    published = publish_manifest.build(root, "test")
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(published, handle, indent=1, sort_keys=True)


def run(root):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cs.main(["--root", root])
    return code, out.getvalue()


def sha256(path):
    """Independently of the step: ChemDuck's form of the file's hash."""
    return "sha256:" + hashlib.sha256(open(path, "rb").read()).hexdigest()


def main():
    roots = []
    try:
        lonlat = {"x": "LONGITUDE", "y": "LATITUDE"}

        # --- a clean facility -------------------------------------------------
        root = facility(ROWS, lonlat)
        roots.append(root)
        code, said = run(root)
        folder = os.path.join(root, cs.FOLDER)
        check("a configured facility publishes and exits 0", code == 0, said[-300:])
        present = sorted(cs.files(root))
        check("the folder holds the sidecars, the results view and its index",
              all(cs.FOLDER + "/" + name in present for name in (
                  "sidecars.json", "locations.parquet", "samples.parquet",
                  "analyte_summary.parquet", "sql/results_view.sql")), present)
        check("files() lists sidecars.json last, for the upload",
              cs.files(root)[-1] == cs.FOLDER + "/sidecars.json", cs.files(root)[-3:])
        sidecars = json.load(open(os.path.join(folder, "sidecars.json"), encoding="utf-8"))
        check("sidecars.json records the hash of the manifest beside it",
              sidecars["source_manifest"]["sha256"]
              == sha256(os.path.join(root, "manifest.json")))
        manifest = json.load(open(os.path.join(root, "manifest.json"), encoding="utf-8"))
        check("the manifest does not list the folder (the hash would change)",
              not any("chemduck" in p["path"] for p in manifest["partitions"]))
        check("no unit conversion, no ATTENTION", cs.ATTENTION not in said, said)
        check("the summary names the tables and their rows",
              "locations 2" in said and "samples 3" in said, said.splitlines()[:1])
        check("nothing left half-built", not os.path.exists(folder + ".building"))

        # ChemDuck opens it over the partitions and answers.
        from parquet_source import chemduck
        con = duckdb.connect()
        parts = [os.path.join(root, *p["path"].split("/")) for p in manifest["partitions"]]
        chemduck.open_consumer(con, folder, parts, schema="chemduck")
        cur = chemduck.schema_cursor(con, "chemduck")
        answer = dict(cur.execute(
            "SELECT location_id, result FROM aggregate_points('Arsenic', "
            "p_event_agg := 'maximum', p_matrix := 'WG')").fetchall())
        check("ChemDuck opens the folder over the partitions and answers",
              answer == {"MW-1": 3.0, "MW-22": 7.5}, answer)
        con.close()

        # --- a ChemDuck unit conversion is reported --------------------------
        mixed = facility(ROWS + MIXED, lonlat)
        roots.append(mixed)
        code, said = run(mixed)
        attention = [line for line in said.splitlines() if line.startswith(cs.ATTENTION)]
        check("a ChemDuck conversion publishes (exit 0) and says so as ATTENTION",
              code == 0 and len(attention) == 1 and "Iron" in attention[0]
              and "own factor" in attention[0], attention or said[-300:])

        # --- a refused publish leaves the previous folder --------------------
        before = open(os.path.join(folder, "sidecars.json"), "rb").read()
        manifest["partitions"][0]["rows"] += 1        # disagrees with the file
        with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        code, said = run(root)
        check("a publish ChemDuck refuses exits 2, saying why",
              code == 2 and "row" in said.lower(), said[-200:])
        check("... and leaves the previous folder as it was, nothing half-built",
              open(os.path.join(folder, "sidecars.json"), "rb").read() == before
              and not os.path.exists(folder + ".building"))

        # --- config ----------------------------------------------------------
        bare = facility(ROWS)
        roots.append(bare)
        code, said = run(bare)
        check("no \"chemduck\" section refuses, naming it",
              code == 2 and "chemduck" in said and not os.path.exists(
                  os.path.join(bare, cs.FOLDER)), said)
        half = facility(ROWS, {"x": "LONGITUDE"})
        roots.append(half)
        code, said = run(half)
        check("a missing coordinate column refuses: they are never guessed",
              code == 2 and "\"y\"" in said, said)

        check("the step imported no plugin code",
              not any(name.startswith("equis_loader") for name in sys.modules))
    finally:
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)
    print()
    if FAILURES:
        print("%d failure(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("ChemDuck's sidecars publish beside the manifest, and refuse cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
