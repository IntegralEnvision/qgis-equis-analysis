"""arcgis_export.py, without EQuIS, a network, or ArcGIS Pro.

    python scripts/test_arcgis_export.py

What can be checked without Pro: that every file meets the written
requirements (docs/arcgis-pro-geoparquet-brief.md in the plugin repository),
that no row is lost or moved, and that coordinates are never published in a
CRS they cannot be in. Whether Pro then draws them is checked in Pro.

The non-detect columns are held to the plugin's rules by
tools/test_arcgis_parity.py in the plugin repository, which needs it.

Needs pyarrow and numpy.
"""
import json
import os
import shutil
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    print("pyarrow is not importable; skipping.")
    raise SystemExit(0)

import arcgis_export as ax  # noqa: E402

QUIET = dict(say=lambda *_: None)


def write_cell(root, matrix, fraction, n, leachate_typed=True, unplaced=(1,),
               drop=(), x0=-43.5, y0=-19.0, step=0.01):
    columns = {
        "SYS_LOC_CODE": ["L%d" % (i % 4) for i in range(n)],
        "LOC_NAME": ["Loc %d" % (i % 4) for i in range(n)],
        "CHEMICAL_NAME": ["Arsenic"] * n,
        "MATRIX_CODE": [matrix] * n,
        "FRACTION": [fraction] * n,
        "SAMPLE_DATE": ["4/4/2019 9:15:00 AM"] * n,
        "SAMPLETIME": pa.array([33300000] * n, pa.time32("ms")),
        "DETECT_FLAG": ["Y" if i % 2 else "N" for i in range(n)],
        "RESULT_NUMERIC": pa.array([float(i) if i % 2 else None for i in range(n)]),
        "REPORT_RESULT_VALUE": pa.array([float(i) for i in range(n)]),
        "RESULT_UNIT": ["ug/L"] * n,
        "X_COORD": pa.array([None if i in unplaced else 500000.0 + 10 * (i % 4)
                             for i in range(n)], pa.float64()),
        "Y_COORD": pa.array([None if i in unplaced else 7900000.0 + 10 * (i % 4)
                             for i in range(n)], pa.float64()),
        "LONGITUDE": pa.array([None if i in unplaced else x0 + step * (i % 4)
                               for i in range(n)], pa.float64()),
        "LATITUDE": pa.array([None if i in unplaced else y0 - step * (i % 4)
                              for i in range(n)], pa.float64()),
        "LEACHATE_DATE": (pa.array(["5/1/2019"] * n) if leachate_typed
                          else pa.nulls(n)),
    }
    for name in drop:
        columns.pop(name)
    table = pa.table(columns)
    table = table.append_column("matrix", pa.array([matrix] * n).dictionary_encode())
    table = table.append_column("fraction", pa.array([fraction] * n).dictionary_encode())
    directory = os.path.join(root, "matrix=" + matrix, "fraction=" + fraction)
    os.makedirs(directory, exist_ok=True)
    pq.write_table(table, os.path.join(directory, "part-0000.parquet"))


def fresh(**cell_args):
    root = tempfile.mkdtemp(prefix="arcgis_test_")
    write_cell(root, "WS", "D", 12, **cell_args)
    write_cell(root, "SE", "N", 8, leachate_typed=False, unplaced=())
    return root


def refused(label, fn, needle):
    try:
        fn()
    except ax.ExportError as exc:
        check(label, needle in str(exc), str(exc)[:110])
        return
    check(label, False, "not refused")


def decode(wkb):
    order, kind, x, y = struct.unpack("<BIdd", wkb)
    return order, kind, x, y


def main():
    # ---------------------------------------------------------- the files
    root = fresh()
    out = os.path.join(root, ax.OUT_DIR)
    manifest = ax.export(root, **QUIET)
    names = sorted(os.listdir(out))
    check("one layer per cell, an unplaced table, locations",
          names == ["README.txt", "SE_N.parquet", "WS_D.parquet",
                    "WS_D_unplaced.parquet", "locations.parquet", "manifest.json"],
          str(names))
    check("every row accounted for", manifest["rows"] == 20,
          "%d" % manifest["rows"])

    ws = pq.read_table(os.path.join(out, "WS_D.parquet"))
    se = pq.read_table(os.path.join(out, "SE_N.parquet"))
    geo = json.loads(ws.schema.metadata[b"geo"])
    column = geo["columns"]["geometry"]
    check("GeoParquet 1.1.0, WKB, Point", geo["version"] == "1.1.0"
          and column["encoding"] == "WKB" and column["geometry_types"] == ["Point"])
    check("CRS written explicitly, EPSG:4326 by default",
          column["crs"].get("id") == {"authority": "EPSG", "code": 4326})
    check("no null geometry", ws.column("geometry").null_count == 0)
    lon = ws.column("LONGITUDE").to_pylist()
    lat = ws.column("LATITUDE").to_pylist()
    points = [decode(w) for w in ws.column("geometry").to_pylist()]
    check("WKB is the row's own coordinate, bit for bit, x = longitude",
          all(p == (1, 1, x, y) for p, x, y in zip(points, lon, lat)))
    check("bbox is the placed rows' extent",
          column["bbox"] == [min(lon), min(lat), max(lon), max(lat)])
    unplaced = pq.read_table(os.path.join(out, "WS_D_unplaced.parquet"))
    check("an unplaced row goes to the table, not dropped",
          unplaced.num_rows == 1 and ws.num_rows == 11)
    check("the unplaced table has no geometry or geo metadata",
          "geometry" not in unplaced.column_names
          and b"geo" not in (unplaced.schema.metadata or {}))
    check("one schema across cells",
          ws.schema.remove_metadata().equals(se.schema.remove_metadata()))
    check("Hive duplicates dropped (fraction collides with FRACTION in Pro)",
          "fraction" not in ws.column_names and "matrix" not in ws.column_names)
    check("dates are timestamps", ws.schema.field("SAMPLE_DATE").type == pa.timestamp("us")
          and ws.column("SAMPLE_DATE")[0].as_py().hour == 9)
    check("a column null-typed in one cell takes its type from another",
          se.schema.field("LEACHATE_DATE").type == pa.timestamp("us"))
    check("TIME becomes text", ws.schema.field("SAMPLETIME").type == pa.string()
          and ws.column("SAMPLETIME")[0].as_py().startswith("09:15"))
    nd = ws.filter(pa.compute.equal(ws.column("DETECT_FLAG"), "N"))
    check("non-detects carry their limit and say so",
          all(nd.column("VALUE_IS_LIMIT").to_pylist())
          and nd.column("VALUE_OR_LIMIT").to_pylist()
          == nd.column("REPORT_RESULT_VALUE").to_pylist())
    locations = pq.read_table(os.path.join(out, "locations.parquet"))
    check("one point per placed location", locations.num_rows == 4
          and locations.column("RESULT_COUNT").to_pylist() == [5, 5, 5, 5])
    check("the manifest records the settings and where they came from",
          manifest["crs"] == "EPSG:4326" and manifest["settings_from"]["crs"] == "default")

    # ------------------------------------------------- unchanged, changed
    before = os.stat(os.path.join(out, "WS_D.parquet")).st_mtime_ns
    lines = []
    ax.export(root, say=lines.append)
    check("an unchanged cell is not rewritten (Pro's cache survives)",
          os.stat(os.path.join(out, "WS_D.parquet")).st_mtime_ns == before
          and sum("unchanged" in l for l in lines) == 2)

    write_cell(root, "WS", "D", 12, unplaced=())
    ax.export(root, **QUIET)
    check("a cell that changed is rewritten, and an emptied unplaced table goes",
          not os.path.exists(os.path.join(out, "WS_D_unplaced.parquet"))
          and pq.read_metadata(os.path.join(out, "WS_D.parquet")).num_rows == 12)

    shutil.rmtree(os.path.join(root, "matrix=SE"))
    ax.export(root, **QUIET)
    check("a partition that is gone takes its layer with it",
          not os.path.exists(os.path.join(out, "SE_N.parquet")))
    shutil.rmtree(root)

    # ---------------------------------------------------------- settings
    root = fresh()
    proj = {"type": "ProjectedCRS", "name": "test UTM",
            "id": {"authority": "EPSG", "code": 32723}}
    with open(os.path.join(root, "utm.projjson"), "w") as handle:
        json.dump(proj, handle)
    with open(os.path.join(root, ax.SETTINGS_NAME), "w") as handle:
        json.dump({"x": "X_COORD", "y": "Y_COORD", "crs": "utm.projjson"}, handle)
    manifest = ax.export(root, **QUIET)
    ws = pq.read_table(os.path.join(root, ax.OUT_DIR, "WS_D.parquet"))
    column = json.loads(ws.schema.metadata[b"geo"])["columns"]["geometry"]
    check("arcgis.json chooses columns and CRS",
          manifest["crs"] == "EPSG:32723" and column["crs"] == proj
          and decode(ws.column("geometry")[0].as_py())[2] == ws.column("X_COORD")[0].as_py()
          and manifest["settings_from"]["x"] == ax.SETTINGS_NAME)
    before = os.stat(os.path.join(root, ax.OUT_DIR, "WS_D.parquet")).st_mtime_ns
    manifest = ax.export(root, x="LONGITUDE", y="LATITUDE", crs="EPSG:4326", **QUIET)
    check("the command line overrides arcgis.json, and rewrites",
          manifest["crs"] == "EPSG:4326" and manifest["settings_from"]["crs"] == "command line"
          and os.stat(os.path.join(root, ax.OUT_DIR, "WS_D.parquet")).st_mtime_ns != before)

    # ---------------------------------------------------------- refusals
    refused("metres labelled as degrees are refused",
            lambda: ax.export(root, x="X_COORD", y="Y_COORD", crs="EPSG:4326", **QUIET),
            "not degrees")
    refused("degrees labelled as projected are refused",
            lambda: ax.export(root, x="LONGITUDE", y="LATITUDE", crs="utm.projjson", **QUIET),
            "is projected")
    refused("a CRS that is neither EPSG nor a file is refused",
            lambda: ax.export(root, crs="UTM 23S", **QUIET), "neither")
    refused("a missing coordinate column is refused",
            lambda: ax.export(root, x="EASTING", **QUIET), "EASTING")
    with open(os.path.join(root, ax.SETTINGS_NAME), "w") as handle:
        json.dump({"crs": "EPSG:4326", "datum": "WGS84"}, handle)
    refused("an unknown key in arcgis.json is refused",
            lambda: ax.export(root, **QUIET), "unknown keys")
    shutil.rmtree(root)

    # Only ONE partition lacks it: the other still carries the column, so
    # a check over the union of schemas would pass.
    root = fresh(drop=("DETECT_FLAG",))
    refused("a partition without DETECT_FLAG is refused, though another has it",
            lambda: ax.export(root, **QUIET), "WS_D has no DETECT_FLAG")
    shutil.rmtree(root)

    root = tempfile.mkdtemp(prefix="arcgis_test_")
    write_cell(root, "WS", "D", 4)
    table = pq.read_table(os.path.join(root, "matrix=WS", "fraction=D", "part-0000.parquet"))
    pq.write_table(table.append_column("VALUE_OR_LIMIT", pa.array([1.0] * 4)),
                   os.path.join(root, "matrix=WS", "fraction=D", "part-0000.parquet"))
    refused("a source already holding an added column is refused",
            lambda: ax.export(root, **QUIET), "already has")
    shutil.rmtree(root)

    # -------------------------------------------- the validator itself
    work = tempfile.mkdtemp(prefix="arcgis_test_")
    bad = os.path.join(work, "bad.parquet")
    pq.write_table(pa.table({"code": pa.array(["a"]).dictionary_encode(),
                             "Code": ["b"]}), bad, compression="brotli")
    try:
        ax.validate(bad, spatial=False, name="bad.parquet")
        check("the validator refuses a non-Pro file", False, "passed")
    except ax.ExportError as exc:
        text = str(exc)
        check("the validator refuses a non-Pro file (codec, dictionary, case clash)",
              "BROTLI" in text and "dictionary" in text and "collide" in text, text[:120])
    check("...and deletes it", not os.path.exists(bad))

    # Pro's persistent cache refused files holding either of these (tested
    # in Pro 2026-10-14), so the validator must, however the file was made.
    import numpy as np
    meta = {b"geo": ax.geo_metadata(ax.EPSG_4326, [0.0, 0.0, 1.0, 1.0])}
    for label, geometry, needle in (
            ("an empty (NaN) point", ax.wkb_points(np.array([0.5, np.nan]),
                                                    np.array([0.5, np.nan])), "empty (NaN)"),
            ("a null geometry", pa.array([ax.wkb_points(np.array([0.5]), np.array([0.5]))[0]
                                          .as_py(), None], pa.binary()), "null geometries")):
        path = os.path.join(work, "points.parquet")
        pq.write_table(pa.table({"CODE": ["a", "b"], "geometry": geometry})
                       .replace_schema_metadata(meta), path, compression="zstd")
        try:
            ax.validate(path, spatial=True, name="points.parquet")
            check("the validator refuses %s" % label, False, "passed")
        except ax.ExportError as exc:
            check("the validator refuses %s" % label, needle in str(exc), str(exc)[:100])
    good = os.path.join(work, "good.parquet")
    pq.write_table(pa.table({"CODE": ["a", "b"],
                             "geometry": ax.wkb_points(np.array([0.5, 0.7]),
                                                       np.array([0.5, 0.2]))})
                   .replace_schema_metadata(meta), good, compression="zstd")
    ax.validate(good, spatial=True, name="good.parquet")
    check("...and passes the same file with every point present", os.path.exists(good))
    shutil.rmtree(work)

    print("\n%d failure(s)" % len(FAILURES))
    raise SystemExit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
