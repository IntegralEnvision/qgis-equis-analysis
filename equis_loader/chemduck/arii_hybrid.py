#!/usr/bin/env python3
"""
Hybrid ARII source: ChemDuck over EQuIS Analytical Results II parquet,
without converting the results.

A consumer (chemrooms, in DuckDB-WASM) reads:

  results     a VIEW over the published ARII partitions, built from the same
              mapping and the same SELECT as the full loader (load_data.py's
              results_select_sql), so the two cannot drift apart.
  samples,    small parquet "sidecars" derived from ARII by the loader's own
  locations   insert_samples / insert_locations, run against a view of the
              partitions instead of a staging table.
  catalogs    analyte_summary, available_filters, method_class_summary and
              position_summary (v_method_classes, v_positions), materialized here so a
              browser never scans every result to fill a dropdown, plus the
              aggregation_rules catalog from sql/07_catalogs.sql.

and runs the ChemDuck views and macros unchanged on top. That is the point:
the domain rules live in ChemDuck's SQL once, whichever way the data arrives.

Why not convert? Conversion is cheap (seconds natively). What it cannot fix
is moving millions of rows into a browser tab; a view over analyte-sorted
partitions lets the browser read only the row groups a query needs.

    python tools/arii_hybrid.py publish <manifest.json> <out_dir> [--x COL --y COL]
    python tools/arii_hybrid.py bundle <out_dir>     # ChemDuck SQL for any consumer

The publisher refuses, rather than guessing, when:
  - a file's row count disagrees with the manifest (ManifestError);
  - DETECT_FLAG is missing: no non-detect could be told from a detection;
  - one location or sample id carries conflicting values (LoadConflictError);
  - the coordinates are not WGS84 longitude/latitude;
  - a depth unit is not one it knows how to convert.
"""

import argparse
import datetime
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import duckdb

from .load_data import (
    PROJECT_ROOT,
    SQL_DIR,
    LoadConflictError,
    ManifestError,
    _check_manifest_counts,
    _sql_path,
    SchemaConflictError,  # noqa: F401  (re-exported for hosts)
    build_sql_exprs,
    check_id_conflicts,
    check_install,
    classify_methods,
    apply_sample_rules,
    insert_locations,
    insert_samples,
    install_target,
    load_mapping,
    load_schema,
    plan_units,
    read_parquet_manifest,
    record_nd_multiplier,
    record_nd_policy,
    record_scope,
    results_select_sql,
    run_install,
    schema_cursor,  # noqa: F401  (re-exported for hosts)
    unit_lookup_sql,
)

DEFAULT_MAPPING = Path(__file__).resolve().parent / "equis_arii_parquet_mapping.yaml"

# Placeholder for the partition list in results_view.sql. The consumer
# replaces it with the names it registered the files under; the view text
# never carries a URL, so a signed (SAS) URL never lands in a view.
FILES_TOKEN = "/*ARII_FILES*/"

# result_id = (file's place in the manifest) * 2^40 + (row in file) + 1.
# It must NOT be row_number() OVER (): a window over the scan has to see
# every row before any filter applies, so no row group can be skipped and
# the first query downloads the facility (measured in the QGIS plugin:
# 31-60 s from Azure, against 1.6 s without it). This orders exactly as the
# loader's _src_row does - manifest order, then file order - so tie-breaks
# in aggregate_results agree; for a one-file manifest the ids are equal.
FILE_SPAN = 1 << 40

# Depth units the consumer can turn into metres. Anything else is refused
# at publish time: a borehole drawn at the wrong depth is a plausible
# picture, not an error.
DEPTH_UNIT_TO_M = {
    "m": 1.0, "meter": 1.0, "meters": 1.0, "metre": 1.0, "metres": 1.0,
    "cm": 0.01, "mm": 0.001,
    "ft": 0.3048, "feet": 0.3048, "foot": 0.3048,
    "in": 0.0254, "inch": 0.0254, "inches": 0.0254,
}

SIDECAR_FORMAT = "chemduck-arii-sidecars"
SIDECAR_VERSION = 1


class SourceRefused(Exception):
    """The source cannot be published as given; the message says why."""


# ============================================================================
# SQL shared by the publisher and the consumer
# ============================================================================

def files_literal(names):
    """A DuckDB list literal of file names, in manifest order."""
    return "[" + ", ".join("'%s'" % str(n).replace("'", "''") for n in names) + "]"


def source_relation(files_sql):
    return (f"read_parquet({files_sql}, union_by_name = true, "
            f"filename = true, file_row_number = true)")


def row_id_sql(files_sql):
    return (f"((list_position({files_sql}, filename) - 1) * {FILE_SPAN}"
            f" + file_row_number + 1)")


def results_view_select(mapping, columns, files_sql, method_class_map=None, unit_plan=None):
    """The SELECT behind the hybrid `results` view.

    The ChemDuck columns come from results_select_sql, unchanged. The
    mapping's result_attributes become real, typed columns as well
    (lower-cased: RESULT_NUMERIC -> result_numeric): in the loader they are
    EAV rows, but for ARII the fields that matter are known in advance, and
    projection pushdown means an unused column is never read.

    method_class_map and unit_plan are what classify_methods and plan_units
    decided at publish, frozen into the view.
    """
    exprs = build_sql_exprs(mapping, columns, method_class_map)
    exprs['unit_lookup'] = unit_lookup_sql(unit_plan, exprs, mapping)
    lower = {c.lower(): c for c in columns}
    extras = []
    for attr in mapping.get("result_attributes") or []:
        actual = lower.get(attr.lower())
        if actual:
            extras.append((attr.lower(), f'"{actual}"'))
    return results_select_sql(
        mapping, exprs, row_id_sql(files_sql), source_relation(files_sql), extras)


def results_view_sql(mapping, columns, files_sql=FILES_TOKEN, method_class_map=None,
                     unit_plan=None):
    return ("CREATE OR REPLACE VIEW results AS"
            + results_view_select(mapping, columns, files_sql, method_class_map, unit_plan) + ";\n")


def _split_statements(sql):
    """Statements of a ChemDuck SQL file, with comments removed.

    A character scanner, not a split on ';': the macro files carry ';'
    inside string literals (error messages), and '--' can appear in one too.
    Tracks '...' strings (with '' escapes), "..." identifiers, -- and /* */
    comments, and splits only on a top-level ';'.
    """
    out, cur, i, n = [], [], 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and nxt == "*":
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            cur.append(" ")
            continue
        if ch in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:   # doubled quote: escaped
                        j += 2
                        continue
                    break
                j += 1
            cur.append(sql[i:j + 1])
            i = j + 1
            continue
        if ch == ";":
            stmt = "".join(cur).strip()
            if stmt:
                out.append(stmt)
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def bootstrap_sql(sql_dir=SQL_DIR):
    """DDL a consumer runs before the views, generated from 01_tables.sql.

    A consumer loads some ChemDuck tables itself (from parquet, through
    CREATE TABLE AS, so they have no primary keys) and lacks others. The
    views need every table to exist. So:
      - every CREATE TABLE becomes IF NOT EXISTS (a table or view the
        consumer already made - the hybrid `results` view included - is left
        alone; DuckDB treats IF NOT EXISTS over a view as a no-op);
      - REFERENCES clauses are dropped: a foreign key to a CTAS table fails
        ("no primary key or unique constraint"), and the consumer is
        read-only, so the constraint protects nothing there;
      - ALTER TABLE ... ADD COLUMN IF NOT EXISTS is kept, so a consumer's
        older unit_conversions gains matrix_class - except on results, which
        a hybrid consumer has as a view, written with every column;
      - seed INSERTs are kept only for the catalogs whose tables this
        script creates with their keys: matrix_classes, and the non-detect
        policies (nd_policies, nd_policy_rules). unit_conversions seeds are
        data, and a CTAS copy has no key for INSERT OR REPLACE to use.
    """
    sql = (Path(sql_dir) / "01_tables.sql").read_text(encoding="utf-8")
    out = ["-- Generated by chemduck tools/arii_hybrid.py bootstrap_sql() from",
           "-- sql/01_tables.sql. Do not edit: regenerate.", ""]
    for stmt in _split_statements(sql):
        head = stmt.lstrip().upper()
        if head.startswith("CREATE TABLE"):
            stmt = re.sub(r"CREATE TABLE\s+(IF NOT EXISTS\s+)?", "CREATE TABLE IF NOT EXISTS ",
                          stmt, count=1, flags=re.I)
            stmt = re.sub(r"\s+REFERENCES\s+\w+\s*\([^)]*\)", "", stmt, flags=re.I)
        elif head.startswith("ALTER TABLE"):
            # A consumer's results can be the hybrid view, which no ALTER
            # TABLE can touch; it is written with every column already.
            if re.match(r"ALTER TABLE\s+results\b", stmt.lstrip(), re.I):
                continue
        elif head.startswith("INSERT") and re.search(
                r"\bINTO\s+(matrix_classes|nd_policies|nd_policy_rules|method_classes"
                r"|unit_dimensions|matrix_class_dimensions)\b", stmt, re.I):
            pass
        else:
            continue
        out.append(stmt.strip() + ";")
        out.append("")
    return "\n".join(out)


def _chemduck_revision():
    from . import __version__
    # Only a chemduck checkout has its own git revision: vendored into
    # another repository, git would report that repository's commit.
    if not (PROJECT_ROOT / ".git").exists():
        return {"version": __version__, "commit": None, "dirty": None}
    try:
        # Pathspecs are relative to -C, so run from the repository root.
        root = str(PROJECT_ROOT)
        commit = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                                capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "-C", root, "status", "--porcelain", "--",
                                     "chemduck", "tools"],
                                    capture_output=True, text=True, check=True).stdout.strip())
        return {"version": __version__, "commit": commit, "dirty": dirty}
    except Exception:
        return {"version": __version__, "commit": None, "dirty": None}


CONSUMER_SQL = ("02_macros.sql", "03_views.sql", "08_aggregate_points.sql", "08_aggregate_results.sql",
                "10_sample_results.sql")


def write_consumer_bundle(out_dir, sql_dir=SQL_DIR):
    """Write what any ChemDuck consumer loads besides its data.

    out_dir/sql/00_bootstrap.sql, the view and macro files a browser loads,
    out_dir/aggregation_rules.parquet (the catalog 07_catalogs.sql defines),
    and out_dir/sql/SOURCE.json naming the chemduck revision they came from.
    Used by `publish` for an ARII folder, and by chemrooms' sync script for
    a converted ChemDuck folder, so both carry the same SQL.
    """
    out_dir = Path(out_dir)
    sql_out = out_dir / "sql"
    sql_out.mkdir(parents=True, exist_ok=True)
    (sql_out / "00_bootstrap.sql").write_text(bootstrap_sql(sql_dir), encoding="utf-8")
    for name in CONSUMER_SQL:
        (sql_out / name).write_text((Path(sql_dir) / name).read_text(encoding="utf-8"),
                                    encoding="utf-8")
    con = duckdb.connect()
    for stmt in _split_statements((Path(sql_dir) / "07_catalogs.sql").read_text(encoding="utf-8")):
        con.execute(stmt)
    dest = out_dir / "aggregation_rules.parquet"
    con.execute(f"COPY (SELECT * FROM aggregation_rules ORDER BY category, display_order) "
                f"TO '{_sql_path(dest)}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    n_rules = con.execute("SELECT count(*) FROM aggregation_rules").fetchone()[0]
    con.close()
    source = {"chemduck": _chemduck_revision(),
              "files": ["00_bootstrap.sql", *CONSUMER_SQL],
              "aggregation_rules": n_rules,
              "generated": _now()}
    (sql_out / "SOURCE.json").write_text(json.dumps(source, indent=1) + "\n", encoding="utf-8")
    return source


# ============================================================================
# Publisher
# ============================================================================

def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _sha256(path):
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _count(con, sql):
    return con.execute(sql).fetchone()[0]


def publish(manifest_path, out_dir, mapping_path=DEFAULT_MAPPING, x_col=None,
            y_col=None, memory_limit="8GB", quiet=False, materialize=()):
    """Write the sidecars and results_view.sql for one ARII manifest.

    x_col / y_col choose the coordinate columns. They are a setting, never
    inferred from a column's name: EQuIS stores whatever datum it was given,
    and X_COORD is longitude at one facility and a projected easting at the
    next. Whatever is chosen must be WGS84 lon/lat, which chemrooms assumes.

    materialize: SQL files of CREATE VIEW statements a consumer runs over
    the ChemDuck tables (chemrooms' catalogs, say). Each view they define is
    written here as a same-named sidecar table, so the consumer queries one
    name whether it opened converted data (where the name is a live view)
    or this folder (where it is a table). Anything that would scan every
    result - an analyte list, per-location counts - belongs here, not in a
    browser that would download the facility to answer it.
    """
    log = (lambda *a: None) if quiet else print
    manifest_path = Path(manifest_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    mapping = load_mapping(mapping_path)
    if x_col or y_col:
        mapping = dict(mapping)
        mapping["locations"] = dict(mapping["locations"])
        if x_col:
            mapping["locations"]["x"] = x_col
        if y_col:
            mapping["locations"]["y"] = y_col

    files, total = read_parquet_manifest(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = [Path(p).resolve().as_posix() for p, _ in files]
    rel_paths = [part["path"] for part in manifest["partitions"]]

    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{memory_limit}'")
    rows = _check_manifest_counts(con, files, total)
    log(f"  Manifest: {len(files)} file(s), {rows:,} rows")

    # ChemDuck schema (tables, views, macros) for the loader's inserts.
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        load_schema(con, SQL_DIR)
    nd_policy = record_nd_policy(con, mapping)

    lit = files_literal(paths)
    con.execute(f"CREATE VIEW staging AS SELECT *, {row_id_sql(lit)} AS _src_row "
                f"FROM {source_relation(lit)}")
    columns = [r[0] for r in con.execute("DESCRIBE staging").fetchall()
               if r[0] not in ("_src_row", "filename", "file_row_number")]
    lower = {c.lower(): c for c in columns}

    detect_col = mapping["results"].get("detected")
    if not detect_col or detect_col.lower() not in lower:
        raise SourceRefused(
            f"The source has no {detect_col or 'detection'} column. Without it a "
            "non-detect's limit cannot be told from a measured concentration.")
    for key in ("x", "y"):
        col = mapping["locations"].get(key)
        if not col or col.lower() not in lower:
            raise SourceRefused(f"Coordinate column {col!r} (locations.{key}) is not in the source.")

    exprs = build_sql_exprs(mapping, columns)
    sample_map = mapping["samples"]
    # method classes: resolved from the data now, frozen into the view
    with contextlib.redirect_stdout(io.StringIO()):
        method_class_map = classify_methods(con, mapping, exprs)
        record_nd_multiplier(con, exprs)
        record_scope(con, mapping, exprs)
        exprs = build_sql_exprs(mapping, columns, method_class_map)
        # units: each variable's canonical unit, decided now (shared rules D5)
        unit_plan = plan_units(con, mapping, exprs)

    # Locations and samples by the loader's own inserts. Its conflict checks
    # are the constancy assertions: an id whose rows disagree is refused,
    # never resolved with any_value().
    with contextlib.redirect_stdout(io.StringIO()):
        insert_locations(con, mapping, exprs, columns, allow_conflicts=False)
        insert_samples(con, mapping, exprs, columns, allow_conflicts=False)
        apply_sample_rules(con, mapping, exprs)
    col_or_null = exprs["col_or_null"]
    check_id_conflicts(con, "sample", f'"{exprs["sample_id_col"]}"', {
        "depth_units": col_or_null(sample_map.get("depth_units")),
        "event": col_or_null(sample_map.get("event")),
        "composite_type": col_or_null(sample_map.get("composite_type")),
    })
    n_loc = _count(con, "SELECT count(*) FROM locations")
    n_samp = _count(con, "SELECT count(*) FROM samples")
    log(f"  Locations: {n_loc:,}  Samples: {n_samp:,}")

    bad = con.execute("""
        SELECT count(*), min(x), max(x), min(y), max(y) FROM locations
        WHERE x IS NOT NULL AND (x NOT BETWEEN -180 AND 180 OR y NOT BETWEEN -90 AND 90)
    """).fetchone()
    if bad[0]:
        raise SourceRefused(
            f"{bad[0]:,} location(s) have coordinates outside WGS84 lon/lat "
            f"(x {bad[1]}..{bad[2]}, y {bad[3]}..{bad[4]}). Choose WGS84 columns with "
            "--x/--y (e.g. LONGITUDE/LATITUDE) or reproject upstream.")
    unplaced = _count(con, "SELECT count(*) FROM locations WHERE x IS NULL OR y IS NULL")

    units = dict(con.execute("""
        SELECT coalesce(lower(trim(depth_units)), '<null>'), count(*) FROM samples
        WHERE top_depth IS NOT NULL OR bottom_depth IS NOT NULL OR depth IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC
    """).fetchall())
    unknown = sorted(u for u in units if u != "<null>" and u not in DEPTH_UNIT_TO_M)
    if unknown:
        raise SourceRefused(f"Depth unit(s) {unknown} are not ones chemrooms can convert "
                            f"to metres (known: {sorted(DEPTH_UNIT_TO_M)}).")

    # results becomes the view the consumer will have.
    con.execute("DROP TABLE IF EXISTS result_attributes")
    con.execute("DROP TABLE results")
    con.execute(results_view_sql(mapping, columns, lit, method_class_map, unit_plan))
    n_results = _count(con, "SELECT count(*) FROM results")
    log(f"  Results view: {n_results:,} rows ({rows - n_results:,} source rows without "
        f"a sample id or analyte)")

    def write(table_sql, name):
        dest = out_dir / f"{name}.parquet"
        tmp = dest.with_suffix(".parquet.tmp")
        con.execute(f"COPY ({table_sql}) TO '{_sql_path(tmp)}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        tmp.replace(dest)
        return {"file": dest.name, "rows": _count(con, f"SELECT count(*) FROM ({table_sql})")}

    tables = {
        "locations": write("SELECT * FROM locations ORDER BY location_id", "locations"),
        "samples": write("SELECT * FROM samples ORDER BY location_id, sample_date, sample_id",
                         "samples"),
        "analyte_summary": write("SELECT * FROM v_analyte_summary ORDER BY analyte, matrix",
                                 "analyte_summary"),
        "available_filters": write("SELECT * FROM v_available_filters "
                                   "ORDER BY filter_type, filter_value", "available_filters"),
        "method_class_summary": write("SELECT * FROM v_method_classes", "method_class_summary"),
        "position_summary": write("SELECT * FROM v_positions", "position_summary"),
        # the dataset's settings: its non-detect policy, version resolved now
        "chemduck_settings": write("SELECT * FROM chemduck_settings ORDER BY key",
                                   "chemduck_settings"),
        # what the mapping's declared rules did: sample rules (D1), method
        # classes (D3), limits (D4), units (D5)
        "load_report": write("SELECT * FROM load_report ORDER BY ALL", "load_report"),
    }
    materialized = {}
    for sql_file in materialize or ():
        sql_file = Path(sql_file)
        views = lambda: {r[0] for r in con.execute(
            "SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()}
        before = views()
        for stmt in _split_statements(sql_file.read_text(encoding="utf-8")):
            con.execute(stmt)
        made = sorted(views() - before)
        if not made:
            raise SourceRefused(f"{sql_file.name} defines no new views to materialize")
        for v in made:
            if v in tables:
                raise SourceRefused(f"{sql_file.name}: view {v!r} would overwrite a sidecar")
            tables[v] = write(f"SELECT * FROM {v} ORDER BY ALL", v)
            materialized[v] = sql_file.name
        log(f"  Materialized from {sql_file.name}: {', '.join(made)}")
    con.close()

    bundle = write_consumer_bundle(out_dir)
    view_path = out_dir / "sql" / "results_view.sql"
    view_path.write_text(
        "-- Generated by chemduck tools/arii_hybrid.py from "
        f"{Path(mapping_path).name}. Do not edit: republish.\n"
        f"-- Replace {FILES_TOKEN} with the list of registered partition names,\n"
        "-- in manifest order (sidecars.json source_manifest.partitions).\n"
        + results_view_sql(mapping, columns, FILES_TOKEN, method_class_map, unit_plan),
        encoding="utf-8")

    sidecars = {
        "format": SIDECAR_FORMAT,
        "version": SIDECAR_VERSION,
        "generated": _now(),
        "chemduck": bundle["chemduck"],
        "mapping": {"file": Path(mapping_path).name, "sha256": _sha256(mapping_path)},
        "source_manifest": {
            "sha256": _sha256(manifest_path),
            "facility": manifest.get("facility"),
            "rows": rows,
            "partitions": [
                {"path": rel, "rows": part.get("rows"), "as_of": part.get("as_of")}
                for rel, part in zip(rel_paths, manifest["partitions"])
            ],
        },
        "coordinates": {"x": mapping["locations"]["x"], "y": mapping["locations"]["y"],
                        "crs": "EPSG:4326", "unplaced_locations": unplaced},
        "depth_units": {"samples_with_depth_by_unit": units,
                        "to_metres": {u: DEPTH_UNIT_TO_M[u] for u in units if u in DEPTH_UNIT_TO_M}},
        "tables": tables,
        "materialized": materialized,
        "results_view": {"file": "sql/results_view.sql", "files_token": FILES_TOKEN,
                         "rows": n_results},
        "aggregation_rules": {"file": "aggregation_rules.parquet",
                              "rows": bundle["aggregation_rules"]},
        "nd_policy": {"name": nd_policy[0], "version": nd_policy[1]},
    }
    (out_dir / "sidecars.json").write_text(json.dumps(sidecars, indent=1) + "\n",
                                           encoding="utf-8")
    log(f"  Wrote {out_dir}")
    return sidecars


# ============================================================================
# Consumer: open a published folder the way a browser does
# ============================================================================

def consumer_statements(sql_dir):
    """(file, statement) pairs of a bundle's SQL, in the order a consumer runs
    them: bootstrap, then the views and macros."""
    sql_dir = Path(sql_dir)
    return [(name, stmt) for name in ("00_bootstrap.sql", *CONSUMER_SQL)
            for stmt in _split_statements((sql_dir / name).read_text(encoding="utf-8"))]


def load_consumer_sql(con, sql_dir, schema=None, adopt=()):
    """Run a bundle's SQL as a consumer does, into `schema` (default: con's
    current schema), retrying statements that reference objects defined later
    (the macro and view files depend on each other, as in load_schema).

    adopt: tables the caller already loaded for ChemDuck (a browser CTAS-loads
    locations, samples, ... first). Any other existing object a bundle
    statement names is refused (SchemaConflictError), before anything runs.
    """
    items = consumer_statements(sql_dir)
    cur = install_target(con, schema)
    check_install(cur, [s for _, s in items], adopt)
    run_install(cur, items)


def open_consumer(con, folder, partition_files, schema=None):
    """Open a published ARII folder on `con` as chemrooms does in the browser.

    folder           the publish() output (sidecars.json, *.parquet, sql/).
    partition_files  where the partitions are, in manifest order: local
                     paths, or http(s) URLs (with httpfs loaded on `con`).
                     The browser registers each under its manifest path and
                     passes those names instead.
    schema           where ChemDuck goes (created if absent); default con's
                     current schema. A host with objects of its own passes
                     one, then calls the macros through
                     schema_cursor(con, schema).

    Sidecars become tables by CREATE TABLE AS (what a SQLRooms url data
    source does, so no keys), results becomes the view, then the ChemDuck
    SQL runs on top. Opening the same folder into the same schema again
    replaces what the last open made. Refuses (SchemaConflictError), creating
    nothing, if the schema holds an object of a name ChemDuck uses that
    ChemDuck did not create. Returns the parsed sidecars.json.

    Turns on parquet_metadata_cache for con: every call binds the results
    view, which reads each partition's footer, and the cache halves that
    bind. DuckDB checks a file before reusing its footer, so a partition
    re-exported under the same name is read fresh.
    """
    folder = Path(folder)
    sidecars = json.loads((folder / "sidecars.json").read_text(encoding="utf-8"))
    if sidecars.get("format") != SIDECAR_FORMAT or sidecars.get("version") != SIDECAR_VERSION:
        raise SourceRefused(f"{folder}: not a {SIDECAR_FORMAT} v{SIDECAR_VERSION} folder")
    data = [(info["file"], f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM "
                           f"read_parquet('{_sql_path(folder / info['file'])}')")
            for name, info in sidecars["tables"].items()]
    data.append(("aggregation_rules.parquet",
                 f"CREATE OR REPLACE TABLE aggregation_rules AS SELECT * FROM "
                 f"read_parquet('{_sql_path(folder / 'aggregation_rules.parquet')}')"))
    names = [str(p) if str(p).startswith(("http://", "https://"))
             else Path(p).resolve().as_posix() for p in partition_files]
    view = (folder / sidecars["results_view"]["file"]).read_text(encoding="utf-8")
    view = view.replace(sidecars["results_view"]["files_token"], files_literal(names))
    data += [(sidecars["results_view"]["file"], s) for s in _split_statements(view)]
    sql = consumer_statements(folder / "sql")

    cur = install_target(con, schema)
    check_install(cur, [s for _, s in data + sql])
    # GLOBAL: a plain SET on cur would hold for cur's session alone.
    cur.execute("SET GLOBAL parquet_metadata_cache = true")
    # The data first, with no retry: the bootstrap's CREATE TABLE IF NOT
    # EXISTS must find them, not make empty tables in their place.
    run_install(cur, data, retry=False)
    run_install(cur, sql)
    return sidecars


# ============================================================================
# CLI
# ============================================================================

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish", help="write sidecars + results_view.sql for a manifest")
    p.add_argument("manifest")
    p.add_argument("out_dir")
    p.add_argument("--mapping", default=str(DEFAULT_MAPPING))
    p.add_argument("--x", help="WGS84 longitude column (default: the mapping's locations.x)")
    p.add_argument("--y", help="WGS84 latitude column (default: the mapping's locations.y)")
    p.add_argument("--memory-limit", default="8GB")
    p.add_argument("--materialize", action="append", default=[], metavar="SQL",
                   help="CREATE VIEW file whose views become sidecar tables (repeatable)")
    b = sub.add_parser("bundle", help="write the ChemDuck SQL and catalogs a consumer loads")
    b.add_argument("out_dir")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "publish":
            publish(args.manifest, args.out_dir, args.mapping, args.x, args.y,
                    args.memory_limit, materialize=args.materialize)
        else:
            print(json.dumps(write_consumer_bundle(args.out_dir), indent=1))
    except (SourceRefused, ManifestError, LoadConflictError) as e:
        print(f"Refused: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
