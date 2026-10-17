"""The published partitions, rewritten as GeoParquet that ArcGIS Pro opens.

    python pipeline/arcgis_export.py --root data/<name>
    python pipeline/arcgis_export.py --root data/<name> --x X_COORD --y Y_COORD --crs EPSG:26917
    python pipeline/arcgis_export.py --root data/<name> --crs my_crs.projjson

Run after the nightly repack. Writes data/<name>/arcgis/:

  <MATRIX>_<FRACTION>.parquet           one point per result, a Pro layer
  <MATRIX>_<FRACTION>_unplaced.parquet  results whose location has no
                                        coordinates, as a stand-alone table.
                                        NOT null or empty points in the layer:
                                        Pro's persistent cache (>= 500k rows)
                                        refuses a file holding either, wherever
                                        it sits (tested in Pro 2026-10-14), while
                                        small files load - so a cell would stop
                                        loading the night it grew past 500k.
  locations.parquet                     one point per location
  manifest.json, README.txt

A SEPARATE TARGET, NOT A CHANGE TO THE PARTITIONS. Pro reads one file as one
layer and ignores Hive paths, and the partitions fail its rules as they
stand: the in-file `fraction` column collides with FRACTION once Pro folds
names to one case (measured 2026-10-14 with the brief's checker), and the
dates are text. The plugin's reader is held to those files by a parity gate;
nothing here touches them. docs/arcgis-pro-geoparquet-brief.md in the plugin
repository has the requirements and their sources. Target: ArcGIS Pro 3.6+
(zstd, Azure Blob connections and publishing all need 3.6).

COORDINATES AND THEIR CRS ARE A SETTING. EQuIS stores whatever datum it was
given, so WGS84 longitude/latitude is the default, not a fact: override with
--x/--y/--crs, or durably in data/<name>/arcgis.json:

  {"x": "X_COORD", "y": "Y_COORD", "crs": "EPSG:26917"}

`crs` is EPSG:<code> or a path to a PROJJSON file. EPSG:4326 is built in;
any other code needs pyproj, or the PROJJSON file. The CRS is ALWAYS written
explicitly - Pro's handling of a missing one is undocumented - and the
coordinates are checked against it: degrees labelled projected, or metres
labelled degrees, are refused rather than published to land in the ocean.

NON-DETECTS ARE STATED, NOT CONVERTED. A Pro user symbolising RESULT_NUMERIC
silently loses every non-detect, since it is empty for them. Four columns
carry the facts, with the plugin's own rules (`ReportStore._create_views`;
tools/test_arcgis_parity.py holds this file to them):

  IS_DETECT       from DETECT_FLAG and nothing else
  VALUE_OR_LIMIT  the result, or for a non-detect the limit it fell below:
                  RESULT_NUMERIC, then REPORT_RESULT_VALUE, then the first
                  detection-limit column present. Never halved - choosing
                  1/2 DL is the analyst's call, not the export's.
  VALUE_IS_LIMIT  true when VALUE_OR_LIMIT is a limit, not a measurement
  VALUE_UNIT      RESULT_UNIT, else REPORT_RESULT_UNIT (a non-detect has
                  only the latter)

A source without DETECT_FLAG is refused: every result would read as a
non-detect.

PRO REBUILDS ITS CACHE WHEN A FILE'S DATE CHANGES, and for the large cells
that cache is on disk and slow to build. So a cell whose source partition,
settings and schema are unchanged is not rewritten.

Needs pyarrow and numpy (pyproj only for a CRS other than EPSG:4326).
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

OUT_DIR = "arcgis"
SETTINGS_NAME = "arcgis.json"
MANIFEST_NAME = "manifest.json"
PARTITION_FILE = "part-0000.parquet"
LOCATIONS_FILE = "locations.parquet"
DEFAULTS = {"x": "LONGITUDE", "y": "LATITUDE", "crs": "EPSG:4326"}
# Bumped whenever what is written changes, so every cell is rewritten.
FORMAT_VERSION = 1
ROW_GROUP_ROWS = 50000
BATCH_ROWS = 100000

# The plugin's rules, transcribed. tools/test_arcgis_parity.py asserts these
# equal report_data's and the store's, and compares every output row.
DETECT_TRUE = ("Y", "YES", "TRUE", "1")
DETECT_COLUMNS = ("DETECT_FLAG", "DETECTED")
VALUE_COLUMNS = ("RESULT_NUMERIC", "REPORT_RESULT_VALUE", "RESULT_VALUE")
REPORTED_COLUMNS = ("REPORT_RESULT_VALUE",)
LIMIT_COLUMNS = ("REPORT_METHOD_DETECTION_LIMIT", "METHOD_DETECTION_LIMIT",
                 "REPORTING_DETECTION_LIMIT", "REPORT_REPORTING_LIMIT",
                 "REPORT_RESULT_LIMIT")
UNIT_COLUMNS = ("RESULT_UNIT", "REPORT_RESULT_UNIT", "UNIT")
DATE_FORMATS = (
    "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d", "%d-%b-%y", "%d-%b-%Y",
)
DATE_COLUMNS = ("SAMPLE_DATE", "ANALYSIS_DATE", "LEACHATE_DATE")
ADDED_COLUMNS = ("IS_DETECT", "VALUE_OR_LIMIT", "VALUE_IS_LIMIT", "VALUE_UNIT")
LOCATION_COLUMN = "SYS_LOC_CODE"
GEOMETRY = "geometry"
# The repack writes the Hive keys into the file too, as dictionary columns.
# They duplicate MATRIX_CODE and FRACTION, and `fraction` collides with
# FRACTION in Pro, which folds field names to one case.
HIVE_COLUMNS = ("matrix", "fraction")

# EPSG:4326 as PROJJSON (pyproj 3.7.2), so the default needs no pyproj.
EPSG_4326 = json.loads(
    '{"$schema":"https://proj.org/schemas/v0.7/projjson.schema.json",'
    '"type":"GeographicCRS","name":"WGS 84","datum_ensemble":{"name":'
    '"World Geodetic System 1984 ensemble","members":['
    '{"name":"World Geodetic System 1984 (Transit)","id":{"authority":"EPSG","code":1166}},'
    '{"name":"World Geodetic System 1984 (G730)","id":{"authority":"EPSG","code":1152}},'
    '{"name":"World Geodetic System 1984 (G873)","id":{"authority":"EPSG","code":1153}},'
    '{"name":"World Geodetic System 1984 (G1150)","id":{"authority":"EPSG","code":1154}},'
    '{"name":"World Geodetic System 1984 (G1674)","id":{"authority":"EPSG","code":1155}},'
    '{"name":"World Geodetic System 1984 (G1762)","id":{"authority":"EPSG","code":1156}},'
    '{"name":"World Geodetic System 1984 (G2139)","id":{"authority":"EPSG","code":1309}},'
    '{"name":"World Geodetic System 1984 (G2296)","id":{"authority":"EPSG","code":1383}}],'
    '"ellipsoid":{"name":"WGS 84","semi_major_axis":6378137,'
    '"inverse_flattening":298.257223563},"accuracy":"2.0",'
    '"id":{"authority":"EPSG","code":6326}},"coordinate_system":{"subtype":'
    '"ellipsoidal","axis":[{"name":"Geodetic latitude","abbreviation":"Lat",'
    '"direction":"north","unit":"degree"},{"name":"Geodetic longitude",'
    '"abbreviation":"Lon","direction":"east","unit":"degree"}]},'
    '"scope":"Horizontal component of 3D system.","area":"World.",'
    '"bbox":{"south_latitude":-90,"west_longitude":-180,"north_latitude":90,'
    '"east_longitude":180},"id":{"authority":"EPSG","code":4326}}')

PRO_NAME = re.compile(r"^[A-Za-z0-9_]{1,128}$")
PRO_FILE = re.compile(r"^[A-Za-z0-9_-]+\.parquet$")
PRO_CODECS = {"UNCOMPRESSED", "SNAPPY", "GZIP", "ZSTD"}


class ExportError(Exception):
    pass


# ------------------------------------------------------------------ settings

def resolve_settings(root, x=None, y=None, crs=None):
    """CLI over arcgis.json over the defaults. Returns (settings, source)."""
    settings, source = dict(DEFAULTS), {k: "default" for k in DEFAULTS}
    path = os.path.join(root, SETTINGS_NAME)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        unknown = set(saved) - set(DEFAULTS)
        if unknown:
            raise ExportError("%s: unknown keys %s (allowed: %s)"
                              % (path, sorted(unknown), sorted(DEFAULTS)))
        for key, value in saved.items():
            settings[key], source[key] = value, SETTINGS_NAME
    for key, value in (("x", x), ("y", y), ("crs", crs)):
        if value:
            settings[key], source[key] = value, "command line"
    return settings, source


def projjson_for(crs, root):
    """PROJJSON for 'EPSG:<code>' or a PROJJSON file path."""
    match = re.fullmatch(r"(?i)EPSG:(\d+)", crs.strip())
    if match:
        code = int(match.group(1))
        if code == 4326:
            return EPSG_4326
        try:
            from pyproj import CRS
        except ImportError:
            raise ExportError(
                "EPSG:%d needs pyproj to describe it; install pyproj, or "
                "pass --crs a PROJJSON file (`projinfo EPSG:%d -o PROJJSON`)"
                % (code, code))
        return CRS.from_epsg(code).to_json_dict()
    path = crs if os.path.isabs(crs) else os.path.join(root, crs)
    if not os.path.isfile(path):
        raise ExportError("--crs %r is neither EPSG:<code> nor a PROJJSON "
                          "file (looked for %s)" % (crs, path))
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def crs_kind(projjson):
    """'geographic', 'projected', or None when the type says neither."""
    kind = projjson.get("type", "")
    if kind == "BoundCRS":
        kind = projjson.get("source_crs", {}).get("type", "")
    return {"GeographicCRS": "geographic", "GeodeticCRS": "geographic",
            "ProjectedCRS": "projected"}.get(kind)


def crs_label(projjson):
    ident = projjson.get("id") or {}
    if ident.get("authority") and ident.get("code"):
        return "%s:%s" % (ident["authority"], ident["code"])
    return projjson.get("name") or "custom"


# ------------------------------------------------------------------- source

def source_cells(root):
    """(cell name, matrix, fraction, path) for every repacked partition."""
    cells = []
    for matrix_dir in sorted(os.listdir(root)):
        if not matrix_dir.startswith("matrix="):
            continue
        for fraction_dir in sorted(os.listdir(os.path.join(root, matrix_dir))):
            if not fraction_dir.startswith("fraction="):
                continue
            path = os.path.join(root, matrix_dir, fraction_dir, PARTITION_FILE)
            if not os.path.isfile(path):
                continue
            matrix = matrix_dir.split("=", 1)[1]
            fraction = fraction_dir.split("=", 1)[1]
            name = "%s_%s" % (re.sub(r"[^A-Za-z0-9-]", "-", matrix),
                              re.sub(r"[^A-Za-z0-9-]", "-", fraction))
            cells.append((name, matrix, fraction, path))
    if not cells:
        raise ExportError("no matrix=*/fraction=*/%s under %r"
                          % (PARTITION_FILE, root))
    names = [c[0] for c in cells]
    clash = {n for n in names if names.count(n) > 1}
    if clash:
        raise ExportError("matrix/fraction codes collide as file names: %s"
                          % sorted(clash))
    return cells


def _base_type(field_type):
    return (field_type.value_type if pa.types.is_dictionary(field_type)
            else field_type)


def parse_date(text):
    """report_data.parse_date without its date-half fallback: a value that
    no format reads is reported, not quietly truncated."""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(text)


def survey(cells, settings):
    """Pass 1: the target schema, the date columns that parse, the
    coordinate range, and the locations. Reads only what it needs."""
    fields, order = {}, []
    for name, _, _, path in cells:
        present = set(pq.read_schema(path).names)
        # PER PARTITION, not over their union: one partition without the
        # flag would otherwise pass here and have every row read as a
        # non-detect (found by test_arcgis_export.py, 2026-10-14).
        if not any(c in present for c in DETECT_COLUMNS):
            raise ExportError(
                "%s has no DETECT_FLAG column: detection is never inferred, "
                "and without it every result would read as a non-detect" % name)
        missing = [c for c in (settings["x"], settings["y"], LOCATION_COLUMN)
                   if c not in present]
        if missing:
            raise ExportError("%s lacks %s" % (name, missing))
        for field in pq.read_schema(path):
            if field.name in HIVE_COLUMNS:
                continue
            kind = _base_type(field.type)
            if field.name not in fields:
                order.append(field.name)
                fields[field.name] = kind
            elif pa.types.is_null(fields[field.name]) and not pa.types.is_null(kind):
                fields[field.name] = kind
            elif not pa.types.is_null(kind) and fields[field.name] != kind:
                raise ExportError("%s is %s in one partition and %s in another"
                                  % (field.name, fields[field.name], kind))
    taken = [c for c in ADDED_COLUMNS + (GEOMETRY,) if c in fields]
    if taken:
        raise ExportError("the source already has %s, which the export adds"
                          % taken)

    dates = [c for c in DATE_COLUMNS if c in fields
             and (pa.types.is_string(fields[c]) or pa.types.is_large_string(fields[c])
                  or pa.types.is_null(fields[c]))]
    distinct = {c: set() for c in dates}
    locations = {}
    x_name, y_name = settings["x"], settings["y"]
    bounds = [np.inf, np.inf, -np.inf, -np.inf]
    for _, _, _, path in cells:
        present = set(pq.read_schema(path).names)
        wanted = [c for c in dates if c in present]
        wanted += [c for c in (LOCATION_COLUMN, x_name, y_name, "LOC_NAME")
                   if c in present and c not in wanted]
        table = pq.read_table(path, columns=wanted)
        for column in dates:
            if column in present:
                distinct[column].update(
                    v for v in pc.unique(table.column(column).cast(pa.string()))
                    .to_pylist() if v)
        x = table.column(x_name).cast(pa.float64()).to_numpy(zero_copy_only=False)
        y = table.column(y_name).cast(pa.float64()).to_numpy(zero_copy_only=False)
        placed = np.isfinite(x) & np.isfinite(y)
        if placed.any():
            bounds = [min(bounds[0], x[placed].min()), min(bounds[1], y[placed].min()),
                      max(bounds[2], x[placed].max()), max(bounds[3], y[placed].max())]
        keyed = pa.table({
            "code": table.column(LOCATION_COLUMN).cast(pa.string()),
            "name": (table.column("LOC_NAME").cast(pa.string())
                     if "LOC_NAME" in present else pa.nulls(len(x), pa.string())),
            "x": pa.array(x, mask=~placed),
            "y": pa.array(y, mask=~placed),
            "placed": pa.array(placed)})
        grouped = keyed.group_by(["code", "x", "y", "placed"]).aggregate(
            [("name", "max"), ([], "count_all")])
        for row in grouped.to_pylist():
            entry = locations.setdefault(row["code"], {"name": row["name_max"],
                                                       "rows": 0, "coords": {}})
            entry["rows"] += row["count_all"]
            if row["placed"]:
                key = (row["x"], row["y"])
                entry["coords"][key] = entry["coords"].get(key, 0) + row["count_all"]

    parsed, unparsed = {}, {}
    for column in dates:
        mapping, bad = {}, []
        for text in distinct[column]:
            try:
                mapping[text] = parse_date(text)
            except ValueError:
                bad.append(text)
        if bad:
            unparsed[column] = sorted(bad)[:5]
        else:
            parsed[column] = (pa.array(list(mapping), pa.string()),
                              pa.array(list(mapping.values()), pa.timestamp("us")))

    schema = []
    for name in order:
        kind = fields[name]
        if name in parsed:
            kind = pa.timestamp("us")
        elif pa.types.is_null(kind) or pa.types.is_time(kind):
            kind = pa.string()      # Pro: TIME and null types are unsafe
        elif pa.types.is_large_string(kind):
            kind = pa.string()
        elif pa.types.is_unsigned_integer(kind):
            kind = pa.int64()
        elif pa.types.is_timestamp(kind) and kind.unit == "ns":
            kind = pa.timestamp("us")
        elif pa.types.is_nested(kind):
            raise ExportError("%s is nested (%s); Pro cannot read it"
                              % (name, kind))
        schema.append(pa.field(name, kind))
    schema += [pa.field("IS_DETECT", pa.bool_()),
               pa.field("VALUE_OR_LIMIT", pa.float64()),
               pa.field("VALUE_IS_LIMIT", pa.bool_()),
               pa.field("VALUE_UNIT", pa.string())]
    check_names([f.name for f in schema] + [GEOMETRY])
    return {"schema": pa.schema(schema), "dates": parsed, "unparsed": unparsed,
            "bounds": bounds, "locations": locations}


def check_names(names):
    for name in names:
        if not PRO_NAME.match(name):
            raise ExportError("column %r: Pro allows [A-Za-z0-9_], at most 128 "
                              "characters" % name)
        if name.upper() == "OBJECTID":
            raise ExportError("a column named OBJECTID conflicts with the "
                              "ObjectID Pro generates")
    folded = [n.lower() for n in names]
    clash = sorted({n for n in folded if folded.count(n) > 1})
    if clash:
        raise ExportError("columns collide once Pro folds case: %s" % clash)


def check_coordinates(bounds, kind, label):
    """Refuse coordinates that cannot be in the CRS they are labelled with."""
    if not np.isfinite(bounds[0]):
        return
    degrees = (-180 <= bounds[0] and bounds[2] <= 180
               and -90 <= bounds[1] and bounds[3] <= 90)
    if kind == "geographic" and not degrees:
        raise ExportError(
            "coordinates span x %.6g..%.6g, y %.6g..%.6g, which are not "
            "degrees, but %s is geographic. Set --crs to the CRS they are in "
            "(or --x/--y to LONGITUDE/LATITUDE)." % (bounds[0], bounds[2],
                                                    bounds[1], bounds[3], label))
    if kind == "projected" and degrees:
        raise ExportError(
            "coordinates span x %.6g..%.6g, y %.6g..%.6g - degrees - but %s "
            "is projected. Set --crs to the geographic CRS they are in."
            % (bounds[0], bounds[2], bounds[1], bounds[3], label))


# -------------------------------------------------------------------- write

def _first(batch, names):
    for name in names:
        if name in batch.schema.names:
            return batch.column(name)
    return None


def _as_float(column, n):
    if column is None:
        return pa.nulls(n, pa.float64())
    return column.cast(pa.float64())


def nondetect_columns(batch):
    """IS_DETECT, VALUE_OR_LIMIT, VALUE_IS_LIMIT, VALUE_UNIT - the store's
    `detected`, `value_full`, `at_limit` and `unit`, in pyarrow."""
    n = batch.num_rows
    flag = _first(batch, DETECT_COLUMNS).cast(pa.string())
    detected = pc.fill_null(pc.is_in(pc.utf8_upper(flag),
                                     value_set=pa.array(DETECT_TRUE)), False)
    value = _as_float(_first(batch, VALUE_COLUMNS), n)
    reported = _as_float(_first(batch, REPORTED_COLUMNS), n)
    limit = _as_float(_first(batch, LIMIT_COLUMNS), n)
    base = pc.coalesce(value, reported, limit)
    # value_full is `value` when detected and present, else `base` - and
    # base IS value whenever value is present - so it is base throughout.
    at_limit = pc.and_(pc.invert(detected), pc.is_valid(base))
    units = []
    for name in UNIT_COLUMNS:
        if name in batch.schema.names:
            text = pc.utf8_trim_whitespace(batch.column(name).cast(pa.string()))
            units.append(pc.if_else(pc.equal(text, ""), pa.scalar(None, pa.string()),
                                    text))
    unit = pc.coalesce(*units) if units else pa.nulls(n, pa.string())
    return [detected, base, at_limit, unit]


def wkb_points(x, y):
    """Little-endian WKB points, 21 bytes each, built without a loop."""
    n = len(x)
    record = np.dtype([("order", "u1"), ("kind", "<u4"), ("x", "<f8"), ("y", "<f8")])
    buf = np.empty(n, dtype=record)
    buf["order"], buf["kind"], buf["x"], buf["y"] = 1, 1, x, y
    offsets = np.arange(0, 21 * (n + 1), 21, dtype=np.int32)
    return pa.BinaryArray.from_buffers(pa.binary(), n, [None, pa.py_buffer(offsets),
                                                        pa.py_buffer(buf.tobytes())])


def shape(batch, plan):
    """One source batch in the target schema, plus the added columns."""
    n = batch.num_rows
    columns = []
    added = dict(zip(ADDED_COLUMNS, nondetect_columns(batch)))
    for field in plan["schema"]:
        if field.name in added:
            columns.append(added[field.name])
            continue
        if field.name not in batch.schema.names:
            columns.append(pa.nulls(n, field.type))
            continue
        column = batch.column(field.name)
        if pa.types.is_dictionary(column.type):
            column = column.dictionary_decode()
        if field.name in plan["dates"]:
            keys, values = plan["dates"][field.name]
            column = pc.take(values, pc.index_in(column.cast(pa.string()),
                                                 value_set=keys))
        elif pa.types.is_time(column.type):
            column = column.cast(pa.string())
        else:
            column = column.cast(field.type)
        columns.append(column)
    return pa.RecordBatch.from_arrays(columns, schema=plan["schema"])


def geo_metadata(projjson, bbox):
    return json.dumps({
        "version": "1.1.0", "primary_column": GEOMETRY,
        "columns": {GEOMETRY: {"encoding": "WKB", "geometry_types": ["Point"],
                               "crs": projjson, "bbox": bbox}}}).encode()


def write_cell(path, out, plan, settings, projjson):
    """The cell's placed rows as GeoParquet, the rest as a table. Returns
    (placed rows, unplaced rows, unplaced locations)."""
    source = pq.ParquetFile(path)
    x_name, y_name = settings["x"], settings["y"]
    # The bbox goes in the footer, which ParquetWriter takes up front: one
    # cheap read of the two coordinate columns first.
    xy = source.read(columns=[x_name, y_name])
    x = xy.column(x_name).cast(pa.float64()).to_numpy(zero_copy_only=False)
    y = xy.column(y_name).cast(pa.float64()).to_numpy(zero_copy_only=False)
    placed = np.isfinite(x) & np.isfinite(y)
    bbox = ([float(x[placed].min()), float(y[placed].min()),
             float(x[placed].max()), float(y[placed].max())]
            if placed.any() else [0.0, 0.0, 0.0, 0.0])
    del xy, x, y

    spatial_schema = plan["schema"].append(pa.field(GEOMETRY, pa.binary(),
                                                    nullable=False))
    spatial_schema = spatial_schema.with_metadata(
        {b"geo": geo_metadata(projjson, bbox)})
    temp = out + ".tmp"
    temp_unplaced = out[:-len(".parquet")] + "_unplaced.parquet.tmp"
    options = dict(compression="zstd", compression_level=9,
                   use_dictionary=True, write_statistics=True)
    writer = pq.ParquetWriter(temp, spatial_schema, **options) if placed.any() else None
    unplaced_writer = None
    counts = [0, 0]
    unplaced_locations = set()
    try:
        for batch in source.iter_batches(batch_size=BATCH_ROWS):
            shaped = shape(batch, plan)
            bx = batch.column(x_name).cast(pa.float64()).to_numpy(zero_copy_only=False)
            by = batch.column(y_name).cast(pa.float64()).to_numpy(zero_copy_only=False)
            keep = np.isfinite(bx) & np.isfinite(by)
            if keep.any():
                rows = shaped.filter(pa.array(keep))
                rows = rows.append_column(spatial_schema.field(GEOMETRY),
                                          wkb_points(bx[keep], by[keep]))
                writer.write_table(pa.Table.from_batches([rows], spatial_schema),
                                   row_group_size=ROW_GROUP_ROWS)
                counts[0] += rows.num_rows
            if not keep.all():
                rest = shaped.filter(pa.array(~keep))
                if unplaced_writer is None:
                    unplaced_writer = pq.ParquetWriter(temp_unplaced,
                                                       plan["schema"], **options)
                unplaced_writer.write_batch(rest)
                counts[1] += rest.num_rows
                unplaced_locations.update(rest.column(LOCATION_COLUMN).to_pylist())
    finally:
        if writer is not None:
            writer.close()
        if unplaced_writer is not None:
            unplaced_writer.close()

    if counts[0] + counts[1] != source.metadata.num_rows:
        raise ExportError("%s: wrote %d + %d rows from %d"
                          % (path, counts[0], counts[1], source.metadata.num_rows))
    unplaced_out = out[:-len(".parquet")] + "_unplaced.parquet"
    for temp_path, final in ((temp, out), (temp_unplaced, unplaced_out)):
        if os.path.exists(temp_path):
            validate(temp_path, spatial=(final == out), name=os.path.basename(final))
            _replace(temp_path, final)
        elif os.path.exists(final):
            _remove(final)          # yesterday's file, now empty: not stale data
    return counts[0], counts[1], len(unplaced_locations)


def _replace(temp, final):
    try:
        os.replace(temp, final)
    except PermissionError:
        raise ExportError("%s is in use - close it in ArcGIS Pro (or remove "
                          "the layer) and run again" % final)


def _remove(path):
    try:
        os.remove(path)
    except PermissionError:
        raise ExportError("%s is in use - close it in ArcGIS Pro and run again"
                          % path)


def write_locations(out, plan, settings, projjson):
    """One point per placed location, at its most common coordinate."""
    codes, names, xs, ys, rows, variants = [], [], [], [], [], []
    for code in sorted(c for c in plan["locations"] if c is not None):
        entry = plan["locations"][code]
        if not entry["coords"]:
            continue
        (x, y), _ = max(entry["coords"].items(), key=lambda kv: (kv[1], kv[0]))
        codes.append(code)
        names.append(entry["name"])
        xs.append(x)
        ys.append(y)
        rows.append(entry["rows"])
        variants.append(len(entry["coords"]))
    xs, ys = np.array(xs, dtype=float), np.array(ys, dtype=float)
    bbox = ([float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())]
            if len(xs) else [0.0, 0.0, 0.0, 0.0])
    table = pa.table({
        LOCATION_COLUMN: pa.array(codes, pa.string()),
        "LOC_NAME": pa.array(names, pa.string()),
        settings["x"]: pa.array(xs, pa.float64()),
        settings["y"]: pa.array(ys, pa.float64()),
        "RESULT_COUNT": pa.array(rows, pa.int64()),
        # More than 1: the location's rows disagree about where it is, and
        # the point is drawn at the coordinate most rows carry.
        "COORD_VARIANTS": pa.array(variants, pa.int64()),
        GEOMETRY: wkb_points(xs, ys),
    })
    table = table.replace_schema_metadata({b"geo": geo_metadata(projjson, bbox)})
    temp = out + ".tmp"
    pq.write_table(table, temp, compression="zstd", compression_level=9)
    validate(temp, spatial=True, name=os.path.basename(out))
    _replace(temp, out)
    return len(codes), sum(1 for v in variants if v > 1)


def validate(path, spatial, name):
    """What docs/arcgis-pro-geoparquet-brief.md section 7 checks, on the
    file as written. A failure deletes it: nothing half-right is published."""
    problems = []
    if not PRO_FILE.match(name):
        problems.append("file name %r" % name)
    # The footer alone, with no handle left open: Windows will not delete
    # a file something still holds, and a failed file must be deleted.
    meta = pq.read_metadata(path)
    schema = meta.schema.to_arrow_schema()
    codecs = {meta.row_group(i).column(j).compression
              for i in range(meta.num_row_groups) for j in range(meta.num_columns)}
    if codecs - PRO_CODECS:
        problems.append("compression %s" % sorted(codecs - PRO_CODECS))
    for field in schema:
        t = field.type
        if (pa.types.is_nested(t) or pa.types.is_dictionary(t) or pa.types.is_null(t)
                or pa.types.is_time(t) or pa.types.is_unsigned_integer(t)
                or (pa.types.is_timestamp(t) and t.unit == "ns")):
            problems.append("column %s is %s" % (field.name, t))
    try:
        check_names(schema.names)
    except ExportError as exc:
        problems.append(str(exc))
    geo = (schema.metadata or {}).get(b"geo")
    if spatial:
        if geo is None:
            problems.append("no geo metadata")
        else:
            geo = json.loads(geo)
            columns = geo.get("columns", {})
            column = columns.get(GEOMETRY, {})
            if (len(columns) != 1 or column.get("encoding") != "WKB"
                    or column.get("geometry_types") != ["Point"]
                    or not column.get("crs")):
                problems.append("geo metadata %s" % json.dumps(
                    {k: v for k, v in column.items() if k != "crs"}))
            index = schema.get_field_index(GEOMETRY)
            nulls = sum(meta.row_group(i).column(index).statistics.null_count
                        for i in range(meta.num_row_groups))
            if nulls:
                problems.append("%d null geometries" % nulls)
            # Empty points too (NaN coordinates): Pro's persistent cache
            # refused 557,691 rows holding them with no nulls at all.
            # Every geometry here is a 21-byte WKB point, so the
            # coordinates are read straight off the data buffer.
            column = pq.read_table(path, columns=[GEOMETRY]).column(GEOMETRY)
            column = column.combine_chunks() if column.num_chunks else column
            data = column.buffers()[2] if len(column) else None
            if data is not None:
                offsets = np.frombuffer(column.buffers()[1], dtype=np.int32,
                                        count=len(column) + 1, offset=column.offset * 4)
                if not np.all(np.diff(offsets) == 21):
                    problems.append("a geometry is not a 2D WKB point")
                else:
                    raw = np.frombuffer(data, dtype=np.uint8)[offsets[0]:offsets[-1]]
                    xy = raw.reshape(-1, 21)[:, 5:].copy().view("<f8")
                    empty = int((~np.isfinite(xy)).any(axis=1).sum())
                    if empty:
                        problems.append("%d empty (NaN) points" % empty)
    elif geo is not None:
        problems.append("a table carries geo metadata")
    if problems:
        os.remove(path)
        raise ExportError("%s fails the Pro checks: %s" % (name, "; ".join(problems)))


# ---------------------------------------------------------------------- run

def fingerprint(path, settings, projjson, schema):
    stat = os.stat(path)
    blob = json.dumps([FORMAT_VERSION, stat.st_size, stat.st_mtime_ns, settings,
                       projjson, schema.to_string()], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def export(root, x=None, y=None, crs=None, force=False, say=print):
    settings, source = resolve_settings(root, x, y, crs)
    projjson = projjson_for(settings["crs"], root)
    kind, label = crs_kind(projjson), crs_label(projjson)
    cells = source_cells(root)
    plan = survey(cells, settings)
    check_coordinates(plan["bounds"], kind, label)
    for column, examples in plan["unparsed"].items():
        say("  %s kept as text: no format reads e.g. %s" % (column, examples))

    out_dir = os.path.join(root, OUT_DIR)
    os.makedirs(out_dir, exist_ok=True)
    previous = {}
    manifest_path = os.path.join(out_dir, MANIFEST_NAME)
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as handle:
            previous = {f["cell"]: f for f in json.load(handle).get("files", [])}
    published = {}
    published_path = os.path.join(root, "manifest.json")
    if os.path.isfile(published_path):
        with open(published_path, encoding="utf-8") as handle:
            for p in json.load(handle).get("partitions", []):
                published[(p.get("matrix"), p.get("fraction"))] = p.get("as_of")

    say("ArcGIS export: x=%s y=%s crs=%s (%s)" % (
        settings["x"], settings["y"], label,
        ", ".join("%s from %s" % (k, source[k]) for k in ("x", "y", "crs"))))
    files, changed = [], False
    for name, matrix, fraction, path in cells:
        out = os.path.join(out_dir, name + ".parquet")
        stamp = fingerprint(path, settings, projjson, plan["schema"])
        old = previous.get(name)
        if (not force and old and old.get("fingerprint") == stamp
                and os.path.isfile(out)):
            say("  %-14s unchanged, kept (Pro's cache stays valid)" % name)
            files.append(old)
            continue
        placed, unplaced, unplaced_locs = write_cell(path, out, plan, settings,
                                                     projjson)
        changed = True
        entry = {"cell": name, "matrix": matrix, "fraction": fraction,
                 "path": name + ".parquet", "rows": placed,
                 "unplaced_rows": unplaced, "unplaced_locations": unplaced_locs,
                 "unplaced_path": (name + "_unplaced.parquet") if unplaced else None,
                 "bytes": os.path.getsize(out) if placed else 0,
                 "as_of": published.get((matrix, fraction)),
                 "fingerprint": stamp}
        files.append(entry)
        say("  %-14s %9d rows%s%s" % (
            name, placed,
            (", %d unplaced (%d locations) to %s" % (unplaced, unplaced_locs,
                                                     entry["unplaced_path"])
             if unplaced else ""),
            "  [>=500k: run Create Parquet Cache in Pro]" if placed >= 500000 else ""))

    # Cells that no longer exist must not linger as layers.
    current = {f["cell"] for f in files}
    for gone in set(previous) - current:
        for suffix in (".parquet", "_unplaced.parquet"):
            stale = os.path.join(out_dir, gone + suffix)
            if os.path.exists(stale):
                _remove(stale)
                changed = True
                say("  removed %s: the partition is gone" % os.path.basename(stale))

    locations_out = os.path.join(out_dir, LOCATIONS_FILE)
    if changed or force or not os.path.isfile(locations_out):
        count, disagree = write_locations(locations_out, plan, settings, projjson)
        say("  locations      %9d points%s" % (count, (
            ", %d with rows disagreeing about the coordinate" % disagree)
            if disagree else ""))

    manifest = {"version": 1, "format_version": FORMAT_VERSION,
                "generated": datetime.datetime.now(datetime.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "x": settings["x"], "y": settings["y"], "crs": label,
                "settings_from": source, "requires": "ArcGIS Pro 3.6+",
                "rows": sum(f["rows"] + f["unplaced_rows"] for f in files),
                "files": files, "locations": LOCATIONS_FILE,
                "dates_kept_as_text": sorted(plan["unparsed"])}
    with open(manifest_path + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=1)
    os.replace(manifest_path + ".tmp", manifest_path)
    with open(os.path.join(out_dir, "README.txt"), "w", encoding="utf-8") as handle:
        handle.write(README % {"crs": label, "x": settings["x"], "y": settings["y"]})
    return manifest


README = """GeoParquet for ArcGIS Pro 3.6+, one file per matrix and fraction.

Add a <MATRIX>_<FRACTION>.parquet file to a map: one point per result, at
its location's coordinates (%(x)s, %(y)s; %(crs)s). Results at a location
whose coordinates are missing are in <MATRIX>_<FRACTION>_unplaced.parquet,
a table. locations.parquet has one point per location.

Files of 500,000 rows or more: run Create Parquet Cache on them first.
Over 10,000 points Pro draws bins; filter with a definition query (on
CHEMICAL_NAME, say) to see points.

NON-DETECTS. RESULT_NUMERIC is EMPTY for a non-detect - symbolising it
drops them silently. Use these instead:
  IS_DETECT       detected, from DETECT_FLAG
  VALUE_OR_LIMIT  the result; for a non-detect, the limit it fell below
                  (not halved - apply 1/2 DL yourself if that is the
                  convention you need)
  VALUE_IS_LIMIT  true when VALUE_OR_LIMIT is a limit, not a measurement
  VALUE_UNIT      the unit of VALUE_OR_LIMIT
A non-detect is not a small measurement: never take a maximum over
VALUE_OR_LIMIT without excluding VALUE_IS_LIMIT rows where a detection
exists.

Never combine dissolved and total (the files are already split), or
matrices whose units differ.

manifest.json lists each file with its row counts and the age of the data.
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", required=True, help="data/<name>")
    parser.add_argument("--x", help="x / longitude column (default %s)" % DEFAULTS["x"])
    parser.add_argument("--y", help="y / latitude column (default %s)" % DEFAULTS["y"])
    parser.add_argument("--crs", help="EPSG:<code> or a PROJJSON file "
                                      "(default %s)" % DEFAULTS["crs"])
    parser.add_argument("--force", action="store_true",
                        help="rewrite every file, even unchanged ones")
    args = parser.parse_args(argv)
    try:
        manifest = export(args.root, args.x, args.y, args.crs, args.force)
    except ExportError as exc:
        raise SystemExit("arcgis_export: %s" % exc)
    print("%d rows in %d files -> %s" % (manifest["rows"], len(manifest["files"]),
                                        os.path.join(args.root, OUT_DIR)))


if __name__ == "__main__":
    main()
