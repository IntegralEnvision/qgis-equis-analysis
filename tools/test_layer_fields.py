"""Every report column on the map's layers: names, types, values, GeoPackage.

Needs QGIS (and duckdb for the parquet half):

  python-qgis.bat tools\\test_layer_fields.py

Set EQUIS_PARQUET to a facility's partition glob (matrix=*/fraction=*/*.parquet)
to also build Arsenic in SE from the real thing and time it; skipped without.
"""

import datetime
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from qgis.core import QgsApplication  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", ""), True)
app = QgsApplication([], False)
app.initQgis()

from qgis.PyQt.QtCore import QMetaType  # noqa: E402

from equis_loader import layer_builder as lb  # noqa: E402
from equis_loader import report_data as rd  # noqa: E402


def names_of(layer):
    return [layer.fields().at(i).name() for i in range(layer.fields().count())]


def pages(codes):
    """Location pages as the feature service gives them, with a `name`."""
    feature_set = {
        "geometryType": "esriGeometryPoint",
        "spatialReference": {"wkid": 4326},
        "fields": [{"name": "code", "type": "esriFieldTypeString"},
                   {"name": "name", "type": "esriFieldTypeString"}],
    }
    features = [{"attributes": {"code": code, "name": "Well %s" % code},
                 "geometry": {"x": 10.0 + i, "y": 50.0}}
                for i, code in enumerate(codes)]
    return [(feature_set, features)]


# NAME and FID clash with the location's `name` and GeoPackage's own key,
# case-insensitively, which is how SQLite compares column names. RESULT_UNIT
# clashes with the layer's result_unit, and its renamed form must not then
# land on the report's own REPORT_RESULT_UNIT.
HEADERS = ["SYS_LOC_CODE", "SYS_SAMPLE_CODE", "SAMPLE_DATE", "CHEMICAL_NAME",
           "RESULT_NUMERIC", "RESULT_UNIT", "DETECT_FLAG", "MATRIX_CODE",
           "ANALYTIC_METHOD", "LAB_QUALIFIERS", "NAME", "FID",
           "REPORT_METHOD_DETECTION_LIMIT", "REPORT_RESULT_UNIT"]


def row(code, sample, when, value, detect="Y", method="M1", qual="",
        limit="0.5"):
    return [code, sample, when, "Lead", value, "ug/L", detect, "WG", method,
            qual, "lab name %s" % sample, "7", limit, "ug/L"]


ROWS = [
    row("MW-1", "S-1", "01/01/2020", "5.0"),
    row("MW-1", "S-2", "01/01/2021", "12.0", qual="J"),
    # A tie on value with a second method on the same sample: the winner is
    # the row that arrived first, and its qualifier is the one shown.
    row("MW-1", "S-2", "01/01/2021", "12.0", method="M2", qual="B"),
    row("MW-2", "S-3", "01/01/2022", "", detect="N"),
]

table = rd.ReportTable(HEADERS, ROWS)
fields = table.report_fields()
summary = table.aggregate("Lead", how=rd.MAXIMUM, fields=True)
results, _ = table.rows_for("Lead", fields=True)

layer, matched, _, _ = lb.build_joined_layer(
    "Lead (maximum)", pages(["MW-1", "MW-2"]), summary, "Lead",
    report_fields=fields)
names = names_of(layer)
check("joined: the report's columns follow the result fields",
      names.index("SYS_SAMPLE_CODE") > names.index("analyte"), str(names))
check("joined: a clash with the location's `name` is renamed, not dropped",
      "name" in names and "NAME_report" in names, str(names))
check("joined: and so is GeoPackage's fid", "FID_report" in names, str(names))
check("joined: RESULT_UNIT steps aside for result_unit, and clear of "
      "REPORT_RESULT_UNIT", "RESULT_UNIT_report" in names
      and "REPORT_RESULT_UNIT" in names
      and len({n.lower() for n in names}) == len(names), str(names))
by_code = {f["code"]: f for f in layer.getFeatures()}
mw1 = by_code["MW-1"]
check("joined: the winner's sample, qualifier and method",
      (mw1["SYS_SAMPLE_CODE"], mw1["LAB_QUALIFIERS"], mw1["ANALYTIC_METHOD"])
      == ("S-2", "J", "M1"),
      str((mw1["SYS_SAMPLE_CODE"], mw1["LAB_QUALIFIERS"],
           mw1["ANALYTIC_METHOD"])))
check("joined: an empty report cell is NULL",
      by_code["MW-1"]["NAME_report"] == "lab name S-2"
      and by_code["MW-2"]["RESULT_NUMERIC"] is None,
      repr(by_code["MW-2"]["RESULT_NUMERIC"]))
check("joined: a non-detect's own row is carried (it won with its limit)",
      by_code["MW-2"]["SYS_SAMPLE_CODE"] == "S-3"
      and by_code["MW-2"]["DETECT_FLAG"] == "N")

every, placed, _, _ = lb.build_result_layer(
    "Lead (every result)", pages(["MW-1", "MW-2"]), results, "Lead",
    report_fields=fields)
rows_seen = sorted((f["code"], f["result_seq"], f["SYS_SAMPLE_CODE"],
                    f["ANALYTIC_METHOD"]) for f in every.getFeatures())
check("every: one feature per row, each with its own cells",
      rows_seen == [("MW-1", 0, "S-1", "M1"), ("MW-1", 1, "S-2", "M1"),
                    ("MW-1", 2, "S-2", "M2"), ("MW-2", 0, "S-3", "M1")],
      str(rows_seen))

# Without report_fields the layers are what they always were.
plain, _, _, _ = lb.build_joined_layer(
    "plain", pages(["MW-1"]), table.aggregate("Lead", how=rd.MAXIMUM), "Lead")
check("plain: no report_fields, no extra columns",
      "SYS_SAMPLE_CODE" not in names_of(plain), str(names_of(plain)))

# GeoPackage: a clash left in would fail the write outright.
folder = tempfile.mkdtemp(prefix="layer_fields_")
gpkg = os.path.join(folder, "fields.gpkg")
for kind, build, data in (("joined", lb.build_joined_layer, summary),
                          ("every", lb.build_result_layer, results)):
    saved, _, _, warning = build(
        "Lead %s" % kind, pages(["MW-1", "MW-2"]), data, "Lead",
        report_fields=fields, storage=lb.GEOPACKAGE, gpkg_path=gpkg)
    check("gpkg/%s: written and read back from the file" % kind,
          not warning and saved.providerType() == "ogr", warning)
    check("gpkg/%s: every report column survives" % kind,
          all(n in names_of(saved) for n in
              ("SYS_SAMPLE_CODE", "NAME_report", "FID_report",
               "LAB_QUALIFIERS")), str(names_of(saved)))
    samples = sorted(str(f["SYS_SAMPLE_CODE"]) for f in saved.getFeatures())
    check("gpkg/%s: with their values" % kind,
          samples == (["S-2", "S-3"] if kind == "joined"
                      else ["S-1", "S-2", "S-2", "S-3"]), str(samples))

# ------------------------------------------- the pivot's points: per sample
# A scatter or ternary station is ONE sample, so it carries the sample's
# columns (SYS_SAMPLE_CODE, SAMPLE_DATE, SYS_LOC_CODE, MATRIX_CODE) and none
# of a result's: S-2's two rows disagree on method and qualifier, and which
# one's would a point that stands for both show?
sample_answer = table.sample_fields(["S-2", "S-3"])
sample_names = [h for h, _ in sample_answer[0]]
check("samples: the sample's columns, no result's",
      "SYS_SAMPLE_CODE" in sample_names and "SAMPLE_DATE" in sample_names
      and not {"ANALYTIC_METHOD", "LAB_QUALIFIERS", "RESULT_NUMERIC",
               "CHEMICAL_NAME", "NAME", "FID"} & set(sample_names),
      str(sample_names))
stations = [{"location": "MW-1", "sample": "S-2", "date": None,
             "x": 12.0, "y": 3.0, "x_detected": True, "y_detected": True,
             "x_qualifier": None, "y_qualifier": None, "samples_here": 2,
             "tied": 1},
            {"location": "MW-2", "sample": "S-3", "date": None,
             "x": 0.25, "y": 1.0, "x_detected": False, "y_detected": True,
             "x_qualifier": None, "y_qualifier": None, "samples_here": 1,
             "tied": 1}]
scatter, placed, _ = lb.build_bivariate_layer(
    "scatter", pages(["MW-1", "MW-2"]), stations, {"chemical": "Lead"},
    {"chemical": "Zinc"}, {}, sample_fields=sample_answer)
by_code = {f["code"]: f for f in scatter.getFeatures()}
check("scatter: each station carries its own sample's columns",
      placed == 2 and by_code["MW-1"]["SYS_SAMPLE_CODE"] == "S-2"
      and by_code["MW-1"]["SAMPLE_DATE"] == "01/01/2021"
      and by_code["MW-2"]["SYS_SAMPLE_CODE"] == "S-3",
      str({c: (f["SYS_SAMPLE_CODE"], f["SAMPLE_DATE"])
           for c, f in by_code.items()}))
check("scatter: after the bv_ fields, which are unchanged",
      names_of(scatter).index("SYS_SAMPLE_CODE")
      > names_of(scatter).index("bv_tied"), str(names_of(scatter)))
ternary_stations = [{"location": "MW-1", "sample": "S-2", "date": None,
                     "values": (1.0, 2.0, 3.0),
                     "detected": (True, True, True),
                     "shares": (1 / 6.0, 2 / 6.0, 3 / 6.0),
                     "colour": "#123456", "samples_here": 1, "tied": 1},
                    {"location": "MW-2", "sample": "S-9", "date": None,
                     "values": (1.0, 1.0, 1.0),
                     "detected": (True, True, True),
                     "shares": (1 / 3.0,) * 3,
                     "colour": "#654321", "samples_here": 1, "tied": 1}]
tern, placed, _ = lb.build_ternary_layer(
    "ternary", pages(["MW-1", "MW-2"]), ternary_stations,
    [{"chemical": "Lead"}, {"chemical": "Zinc"}, {"chemical": "Copper"}],
    sample_fields=sample_answer)
by_code = {f["code"]: f for f in tern.getFeatures()}
check("ternary: the sample's columns, NULL for a sample not asked about",
      placed == 2 and by_code["MW-1"]["SYS_SAMPLE_CODE"] == "S-2"
      and by_code["MW-2"]["SYS_SAMPLE_CODE"] is None,
      str({c: f["SYS_SAMPLE_CODE"] for c, f in by_code.items()}))
plain_scatter, _, _ = lb.build_bivariate_layer(
    "plain scatter", pages(["MW-1"]), stations[:1], {"chemical": "Lead"},
    {"chemical": "Zinc"}, {})
check("scatter: without sample_fields, the layer it always was",
      names_of(plain_scatter)[-1] == "bv_tied", str(names_of(plain_scatter)))

# ------------------------------------------------------- typed, from parquet
try:
    from equis_loader import store as st
    duckdb = st.duckdb_module()
except Exception as exc:  # noqa: BLE001
    duckdb = None
    print("[SKIP] parquet: duckdb unavailable (%s)" % exc)

if duckdb is not None:
    path = os.path.join(folder, "typed.parquet").replace("\\", "/")
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE t AS SELECT * FROM (VALUES
            ('MW-1', 'S-1', '01/01/2020', 'Lead', 5.0, 'ug/L', 'Y', 'WG',
             'M1', 101::BIGINT, TIME '10:30:00', 52.25::DECIMAL(9,3)),
            ('MW-1', 'S-2', '01/01/2021', 'Lead', 12.0, 'ug/L', 'Y', 'WG',
             'M1', 102::BIGINT, NULL, 1.5::DECIMAL(9,3))
        ) v(SYS_LOC_CODE, SYS_SAMPLE_CODE, SAMPLE_DATE, CHEMICAL_NAME,
            RESULT_NUMERIC, RESULT_UNIT, DETECT_FLAG, MATRIX_CODE,
            ANALYTIC_METHOD, TEST_ID, SAMPLETIME, PERCENT_MOISTURE)""")
    con.execute("COPY t TO '%s' (FORMAT PARQUET)" % path)
    con.close()
    store = st.ReportStore.from_parquet(path, hive=False)
    typed = store.report_fields()
    layer, _, _, _ = lb.build_joined_layer(
        "typed", pages(["MW-1"]), store.aggregate("Lead", how=rd.MAXIMUM,
                                                  fields=True),
        "Lead", report_fields=typed)
    f = layer.fields()
    kinds = {n: f.at(f.indexFromName(n)).type()
             for n in ("RESULT_NUMERIC", "TEST_ID", "SAMPLETIME",
                       "PERCENT_MOISTURE", "SYS_SAMPLE_CODE")}
    check("typed: DOUBLE, BIGINT and DECIMAL stay numbers; TIME is text",
          kinds == {"RESULT_NUMERIC": QMetaType.Type.Double,
                    "TEST_ID": QMetaType.Type.LongLong,
                    "SAMPLETIME": QMetaType.Type.QString,
                    "PERCENT_MOISTURE": QMetaType.Type.Double,
                    "SYS_SAMPLE_CODE": QMetaType.Type.QString}, str(kinds))
    feature = next(layer.getFeatures())
    check("typed: and the winner's values arrive as numbers",
          (feature["TEST_ID"], feature["RESULT_NUMERIC"],
           feature["PERCENT_MOISTURE"], feature["SAMPLETIME"])
          == (102, 12.0, 1.5, None),
          str((feature["TEST_ID"], feature["RESULT_NUMERIC"],
               feature["PERCENT_MOISTURE"], feature["SAMPLETIME"])))
    every_typed, _, _, _ = lb.build_result_layer(
        "typed every", pages(["MW-1"]), store.rows_for("Lead", fields=True)[0],
        "Lead", report_fields=typed)
    times = sorted(str(f["SAMPLETIME"]) for f in every_typed.getFeatures())
    check("typed: a TIME reads as its ISO text, a missing one as NULL",
          times == ["10:30:00", "None"], str(times))

# ------------------------------------------------------- the real facility
real = os.environ.get("EQUIS_PARQUET")
if duckdb is not None and real:
    store = st.ReportStore.from_parquet(real)
    location_pages = store.location_features()
    report_fields = store.report_fields()
    t = time.time()
    summary = store.aggregate("Arsenic", how=rd.MAXIMUM, matrix=["SE"],
                              fields=True)
    asked = time.time() - t
    t = time.time()
    layer, matched, _, _ = lb.build_joined_layer(
        "Arsenic (maximum, SE)", location_pages, summary, "Arsenic",
        report_fields=report_fields)
    built = time.time() - t
    print("[INFO] real: aggregate %.2f s, joined layer %.2f s, %d features x "
          "%d fields" % (asked, built, layer.featureCount(),
                         layer.fields().count()))
    missing = [h for h, _ in report_fields
               if h not in names_of(layer)
               and h + "_report" not in names_of(layer)]
    check("real: all %d report columns on the layer" % len(report_fields),
          not missing, "missing %s; layer has %s"
          % (missing, names_of(layer)[-len(report_fields):]))
    # The winner named by the layer is the row the store ranks first.
    feature = max((f for f in layer.getFeatures()
                   if f["result_value"] is not None and f["result_detect"] == "Y"),
                  key=lambda f: f["result_value"])
    top = store.con.execute("""
        SELECT max(value) FROM v_results WHERE chemical = 'Arsenic'
          AND matrix = 'SE' AND detected AND upper(location) = upper(?)
    """, [feature["code"]]).fetchone()[0]
    check("real: the top location's RESULT_NUMERIC is its maximum detection",
          feature["RESULT_NUMERIC"] == top == feature["result_value"],
          "%r %r %r" % (feature["RESULT_NUMERIC"], top,
                        feature["result_value"]))
    t = time.time()
    rows, _ = store.rows_for("Arsenic", matrix=["SE"], fields=True,
                             limit=25000)
    asked = time.time() - t
    t = time.time()
    every, placed, _, _ = lb.build_result_layer(
        "Arsenic (every result, SE)", location_pages, rows, "Arsenic",
        report_fields=report_fields)
    built = time.time() - t
    t = time.time()
    saved, _ = lb.persist_to_geopackage(every, os.path.join(folder, "real.gpkg"))
    written = time.time() - t
    print("[INFO] real: rows_for %.2f s, every-result layer %.2f s, "
          "GeoPackage %.2f s, %d features x %d fields"
          % (asked, built, written, every.featureCount(),
             every.fields().count()))
    check("real: the every-result layer round-trips through GeoPackage",
          saved is not None and saved.featureCount() == every.featureCount())
    # The same layer as it was before it carried fields, for the cost.
    t = time.time()
    rows, _ = store.rows_for("Arsenic", matrix=["SE"], limit=25000)
    asked = time.time() - t
    t = time.time()
    narrow, _, _, _ = lb.build_result_layer(
        "Arsenic (every result, SE, no fields)", location_pages, rows,
        "Arsenic")
    built = time.time() - t
    t = time.time()
    lb.persist_to_geopackage(narrow, os.path.join(folder, "narrow.gpkg"))
    written = time.time() - t
    print("[INFO] real, without fields: rows_for %.2f s, layer %.2f s, "
          "GeoPackage %.2f s, %d fields"
          % (asked, built, written, narrow.fields().count()))
    t = time.time()
    store.aggregate("Arsenic", how=rd.MAXIMUM, matrix=["SE"])
    print("[INFO] real, without fields: aggregate %.2f s" % (time.time() - t))
    # A scatter's stations: one sample per location, as bivariate picks.
    picks = store.con.execute("""
        SELECT location, any_value(sample) FROM v_results
        WHERE matrix = 'SE' AND coalesce(sample, '') <> '' GROUP BY 1
    """).fetchall()
    t = time.time()
    answer = store.sample_fields([p[1] for p in picks], matrix=["SE"])
    asked = time.time() - t
    stations = [{"location": loc, "sample": sample, "date": None, "x": 1.0,
                 "y": 1.0, "x_detected": True, "y_detected": True,
                 "samples_here": 1, "tied": 1} for loc, sample in picks]
    scatter, placed, _ = lb.build_bivariate_layer(
        "SE scatter", location_pages, stations, {"chemical": "a"},
        {"chemical": "b"}, {}, sample_fields=answer)
    print("[INFO] real: sample_fields for %d SE stations %.2f s, %d columns"
          % (len(picks), asked, len(answer[0])))
    feature = next(f for f in scatter.getFeatures() if f["SYS_SAMPLE_CODE"])
    check("real: a scatter station names its own sample and location",
          str(feature["SYS_LOC_CODE"]).upper() == str(feature["code"]).upper()
          and [s for l, s in picks if l == feature["code"]]
          == [feature["SYS_SAMPLE_CODE"]],
          "%r %r" % (feature["code"], feature["SYS_SAMPLE_CODE"]))
elif not real:
    print("[SKIP] real facility: EQUIS_PARQUET not set")

print("")
if FAILURES:
    print("%d FAILED" % len(FAILURES))
    for label in FAILURES:
        print("  - " + label)
    sys.exit(1)
print("ALL LAYER FIELD TESTS PASSED")
