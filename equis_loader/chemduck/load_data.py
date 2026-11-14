#!/usr/bin/env python3
"""
ChemDuck Unified Data Loader

Loads denormalized environmental chemistry data into the ChemDuck DuckDB schema.
Supports CSV, XLSX, Parquet (one file or a manifest.json of files) and
PostgreSQL source formats via YAML field mapping configs.

Usage (tools/load_data.py runs this module):
    python tools/load_data.py <input> <output> --mapping <yaml>
    python tools/load_data.py <input> <output> --mapping <yaml> --parquet
    python tools/load_data.py <input> <output> --mapping <yaml> --no-auto-classify
"""

import argparse
import contextlib
import json
import re
import sys
from pathlib import Path

import duckdb
import yaml


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent  # a checkout's root, where .env is looked for
SQL_DIR = PACKAGE_DIR / "sql"

# Write new database files in the v1.0.0 storage format so any DuckDB >= 1.0,
# including the DuckDB-WASM build used in the browser, can open them. Applies
# only when the file is created; an existing file keeps its format.
STORAGE_CONFIG = {'storage_compatibility_version': 'v1.0.0'}


# ============================================================================
# Schema & config loading
# ============================================================================

def load_mapping(mapping_path):
    """Read YAML mapping config."""
    with open(mapping_path, 'r') as f:
        return yaml.safe_load(f)


# ============================================================================
# Installing beside a host's objects
# ============================================================================
#
# A host (the QGIS plugin) opens ChemDuck inside its own DuckDB. Opened in the
# host's schema, ChemDuck's CREATE OR REPLACE VIEW results replaced the host's
# own `results`; and a CREATE TABLE IF NOT EXISTS would quietly reuse a host
# table of the same name, binding ChemDuck's views to it. So ChemDuck installs
# into a schema of its choosing, records what it creates in chemduck_objects,
# and refuses - before creating anything - when the schema already holds an
# object of the same name that it did not create.
#
# Views bind names in their own schema, from any caller. Macros do not: a
# macro binds the tables it reads in the *caller's* search path, so
# chemduck.aggregate_results(...) called from main reads main.samples. Call
# macros through schema_cursor().

REGISTRY_TABLE = "chemduck_objects"

_CREATE_RE = re.compile(
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?(?:UNIQUE\s+)?"
    r"(TABLE|VIEW|MACRO|FUNCTION|INDEX|SEQUENCE|TYPE)\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r'((?:"[^"]*"|[A-Za-z_]\w*)(?:\s*\.\s*(?:"[^"]*"|[A-Za-z_]\w*))*)',
    re.I)


class SchemaConflictError(Exception):
    """The target schema holds an object ChemDuck would create, and ChemDuck
    did not create it."""


def quote_ident(name):
    return '"' + str(name).replace('"', '""') + '"'


def sql_literal(text):
    return "'" + str(text).replace("'", "''") + "'"


def schema_cursor(conn, schema=None):
    """A cursor on conn's database whose default schema is `schema`.

    Use it to call ChemDuck's macros (see above). With schema None, conn
    itself, unchanged.
    """
    if schema is None:
        return conn
    cur = conn.cursor()
    cur.execute(f"USE {quote_ident(schema)}")
    return cur


def created_object(stmt):
    """(name, kind) a statement creates, lower-cased; None if it creates none.

    ChemDuck SQL never names a schema: the caller chooses where it goes."""
    m = _CREATE_RE.match(stmt)
    if not m:
        return None
    kind = m.group(1).lower()
    parts = re.findall(r'"[^"]*"|[A-Za-z_]\w*', m.group(2))
    if len(parts) > 1:
        raise ValueError(f"ChemDuck SQL must not name a schema: {stmt[:120]}")
    return parts[0].strip('"').lower(), "macro" if kind == "function" else kind


def _existing_objects(cur):
    """Lower-cased names of the objects in cur's current schema."""
    rows = cur.execute("""
        WITH here AS (SELECT current_database() AS db, current_schema() AS s)
        SELECT table_name FROM duckdb_tables(), here
            WHERE database_name = db AND schema_name = s
        UNION SELECT view_name FROM duckdb_views(), here
            WHERE database_name = db AND schema_name = s AND NOT internal
        UNION SELECT function_name FROM duckdb_functions(), here
            WHERE database_name = db AND schema_name = s
              AND function_type IN ('macro', 'table_macro')
        UNION SELECT index_name FROM duckdb_indexes(), here
            WHERE database_name = db AND schema_name = s
        UNION SELECT sequence_name FROM duckdb_sequences(), here
            WHERE database_name = db AND schema_name = s
    """).fetchall()
    return {r[0].lower() for r in rows}


def _registered_objects(cur, existing):
    if REGISTRY_TABLE not in existing:
        return set()
    return {r[0] for r in cur.execute(f"SELECT name FROM {REGISTRY_TABLE}").fetchall()}


def check_install(cur, statements, adopt=()):
    """Refuse, before anything runs, statements that would replace or reuse an
    object in cur's schema that ChemDuck did not create.

    statements: SQL strings. adopt: names the caller made for ChemDuck itself
    (a browser-style consumer CTAS-loads locations, samples, ... before the
    bootstrap); they are registered rather than refused.
    """
    existing = _existing_objects(cur)
    registered = _registered_objects(cur, existing)
    adopt = {a.lower() for a in adopt}
    made = {o[0] for o in map(created_object, statements) if o}
    foreign = sorted((made & existing) - registered - adopt)
    if foreign:
        schema = cur.execute("SELECT current_schema()").fetchone()[0]
        raise SchemaConflictError(
            f"Schema {schema!r} already holds {', '.join(foreign)}, which ChemDuck did "
            "not create. ChemDuck refuses to replace or reuse another program's objects: "
            "install it into a schema of its own (schema='chemduck').")
    cur.execute(f"CREATE TABLE IF NOT EXISTS {REGISTRY_TABLE} "
                "(name VARCHAR PRIMARY KEY, kind VARCHAR NOT NULL)")
    _register(cur, [(name, "adopted") for name in sorted((adopt & existing) - registered)])


def _register(cur, objects):
    """Record (name, kind) pairs in the registry: one statement, no bound
    parameters.

    The first parameter bound on a connection makes DuckDB's Python client
    import numpy, pandas and pyarrow: 1-3 s when their files are cached,
    18 s once in QGIS when they were not. An install is up to a hundred
    registrations, so they are one INSERT of literals. The values are
    ChemDuck's own identifiers."""
    objects = list(dict.fromkeys(objects))
    if objects:
        cur.execute(f"INSERT OR IGNORE INTO {REGISTRY_TABLE} VALUES " + ", ".join(
            f"({sql_literal(name)}, {sql_literal(kind)})" for name, kind in objects))


def run_install(cur, items, log=None, retry=True):
    """Run (source, statement) pairs on cur, registering what they create.

    DuckDB >=1.5 validates macro bodies at definition time; macros and views
    reference each other, so a statement that fails on a missing object is
    retried after the rest (retry=False: raised at once). Call check_install
    first. What a failed install created before it failed is registered
    too, so the next install may replace it.
    """
    deferred, made = [], []
    try:
        for again in ((False, True) if retry else (False,)):
            for source, stmt in (deferred if again else items):
                try:
                    cur.execute(stmt)
                except Exception as e:
                    err = str(e)
                    if not again and retry and ("does not exist" in err or "not found" in err):
                        deferred.append((source, stmt))
                        continue
                    if log:
                        log(f"    Error in {source}: {e}")
                        log(f"    Statement: {stmt[:200]}...")
                    raise
                obj = created_object(stmt)
                if obj:
                    made.append(obj)
            if not again and deferred and log:
                log(f"  Retrying {len(deferred)} deferred statement(s)...")
    except BaseException:
        with contextlib.suppress(Exception):
            _register(cur, made)
        raise
    _register(cur, made)


def install_target(conn, schema):
    """The cursor ChemDuck installs through: conn itself when schema is None
    (its current schema), else a cursor on `schema`, created if absent."""
    if schema is not None:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_ident(schema)}")
    return schema_cursor(conn, schema)


def _sql_statements(sql):
    """Statements of a ChemDuck SQL file, with -- comments removed."""
    lines = sql.split('\n')
    clean_lines = []
    for line in lines:
        if '--' in line:
            comment_pos = line.find('--')
            quote_count = line[:comment_pos].count("'")
            if quote_count % 2 == 0:
                line = line[:comment_pos]
        clean_lines.append(line)

    clean_sql = '\n'.join(clean_lines)

    statements = []
    current_stmt = []
    paren_depth = 0

    for line in clean_sql.split('\n'):
        line = line.strip()
        if not line:
            continue
        paren_depth += line.count('(') - line.count(')')
        if ';' in line and paren_depth <= 0:
            parts = line.split(';')
            current_stmt.append(parts[0])
            stmt = '\n'.join(current_stmt).strip()
            if stmt:
                statements.append(stmt)
            current_stmt = [parts[1]] if len(parts) > 1 and parts[1].strip() else []
        else:
            current_stmt.append(line)

    if current_stmt:
        stmt = '\n'.join(current_stmt).strip()
        if stmt:
            statements.append(stmt)

    return [s for s in statements if s and not s.isspace()]


# Optional SQL, outside the glob load_schema reads: nothing in the core
# depends on it.
SPATIAL_SQL = SQL_DIR / "optional" / "spatial.sql"


def load_spatial(conn, schema=None):
    """Add geometry views (v_locations_geom) over a loaded ChemDuck schema.

    Needs the spatial extension, which ChemDuck's core does not: locations
    carries x and y as doubles and project.crs names their CRS. A host
    without spatial installed gets the error here and nowhere else.
    """
    conn.execute("LOAD spatial")
    items = [(SPATIAL_SQL.name, s) for s in _sql_statements(SPATIAL_SQL.read_text(encoding="utf-8"))]
    cur = install_target(conn, schema)
    check_install(cur, [s for _, s in items])
    run_install(cur, items, log=print)


def load_schema(conn, sql_dir=SQL_DIR, schema=None):
    """Load all SQL schema files in order, into `schema` (default: conn's
    current schema). Refuses, creating nothing, if that schema holds an object
    of a name ChemDuck uses that ChemDuck did not create."""
    sql_files = sorted(Path(sql_dir).glob("*.sql"))
    items = []

    for sql_file in sql_files:
        print(f"  Loading {sql_file.name}...")
        items += [(sql_file.name, s) for s in _sql_statements(sql_file.read_text())]

    cur = install_target(conn, schema)
    check_install(cur, [s for _, s in items])
    run_install(cur, items, log=print)


def get_source_columns(mapping):
    """Extract all referenced source column names from the mapping."""
    cols = set()
    for section_key in ['locations', 'samples', 'results']:
        section = mapping.get(section_key, {})
        for target, source in section.items():
            if source is not None and isinstance(source, str):
                cols.add(source)
    for attr_key in ['location_attributes', 'sample_attributes', 'result_attributes']:
        for attr in mapping.get(attr_key, []):
            cols.add(attr)
    river_cfg = mapping.get('river', {})
    for key in ['river_mile_column', 'bank_side_column']:
        if river_cfg.get(key):
            cols.add(river_cfg[key])
    return cols


# ============================================================================
# Staging table creation (format-specific)
# ============================================================================

def _staging_csv(conn, source_path, mapping, limit=None):
    """Create staging table from CSV."""
    limit_clause = f"LIMIT {limit}" if limit else ""
    header = mapping.get('source', {}).get('header', True)
    conn.execute(f"""
        CREATE TEMP TABLE staging AS
        SELECT * FROM read_csv_auto('{source_path}', header={header}, all_varchar=true)
        {limit_clause}
    """)


def _staging_xlsx(conn, source_path, mapping, limit=None):
    """Create staging table from XLSX via spatial extension st_read.

    The only loader path that needs an extension: ChemDuck's schema does not.
    """
    conn.execute("INSTALL spatial; LOAD spatial;")
    limit_clause = f"LIMIT {limit}" if limit else ""
    sheet = mapping.get('source', {}).get('sheet', 'Sheet1')
    conn.execute(f"""
        CREATE TEMP TABLE staging AS
        SELECT * FROM st_read('{source_path}', layer='{sheet}')
        {limit_clause}
    """)


class ManifestError(Exception):
    """A Parquet manifest is malformed or disagrees with the files it lists."""


def _sql_path(path):
    return Path(path).as_posix().replace("'", "''")


def read_parquet_manifest(manifest_path):
    """Parquet files listed in a manifest.json, in manifest order.

    Manifest (version 1): {"version": 1, "rows": N,
    "partitions": [{"path": "<relative to manifest>", "rows": n, ...}, ...]}.
    Returns [(path, expected_rows), ...] and the manifest's total row count.
    """
    manifest_path = Path(manifest_path)
    with open(manifest_path) as f:
        manifest = json.load(f)
    if manifest.get('version') != 1:
        raise ManifestError(f"{manifest_path}: unsupported manifest version "
                            f"{manifest.get('version')!r} (expected 1)")
    partitions = manifest.get('partitions') or []
    if not partitions:
        raise ManifestError(f"{manifest_path}: no partitions listed")
    files = []
    for part in partitions:
        path = manifest_path.parent / part['path']
        if not path.exists():
            raise ManifestError(f"{manifest_path}: listed file not found: {path}")
        files.append((path, part.get('rows')))
    return files, manifest.get('rows')


def _check_manifest_counts(conn, files, total):
    """Fail the load when a file's row count differs from the manifest's."""
    problems = []
    actual_total = 0
    for path, expected in files:
        actual = conn.execute(
            f"SELECT SUM(num_rows) FROM parquet_file_metadata('{_sql_path(path)}')"
        ).fetchone()[0]
        actual_total += actual
        if expected is not None and actual != expected:
            problems.append(f"{path.name}: manifest says {expected:,} rows, file has {actual:,}")
    if total is not None and actual_total != total:
        problems.append(f"total: manifest says {total:,} rows, files have {actual_total:,}")
    if problems:
        raise ManifestError("Parquet files disagree with the manifest:\n  " + "\n  ".join(problems))
    return actual_total


def _staging_parquet(conn, source_path, mapping, limit=None):
    """Create staging table from a Parquet file, or the files a manifest.json lists.

    Files are read in manifest order, so source row numbers (and result_ids)
    are the same on every load.
    """
    source_path = Path(source_path)
    if source_path.suffix.lower() == '.json':
        files, total = read_parquet_manifest(source_path)
        rows = _check_manifest_counts(conn, files, total)
        print(f"  Manifest: {len(files)} file(s), {rows:,} rows")
        paths = [p for p, _ in files]
    else:
        paths = [source_path]
    file_list = ', '.join(f"'{_sql_path(p)}'" for p in paths)
    limit_clause = f"LIMIT {limit}" if limit else ""
    conn.execute(f"""
        CREATE TEMP TABLE staging AS
        SELECT * FROM read_parquet([{file_list}], union_by_name = true)
        {limit_clause}
    """)


def _staging_postgresql(conn, source_path, mapping, env, limit=None):
    """Create staging table from PostgreSQL."""
    host = env.get('IDB_IDEP_HOST', 'localhost')
    port = env.get('IDB_IDEP_PORT', '5432')
    db = env.get('IDB_IDEP_DB', '')
    user = env.get('IDB_IDEP_USERNAME', '')
    password = env.get('IDB_IDEP_PASSWORD', '')
    schema = env.get('IDB_IDEP_SCHEMA', 'app')
    table = env.get('IDB_IDEP_TABLENAME', 'mv_allresults')
    limit_clause = f"LIMIT {limit}" if limit else ""

    pg_str = f"host={host} port={port} dbname={db} user={user} password={password}"

    conn.execute("INSTALL postgres; LOAD postgres;")
    conn.execute(f"ATTACH '{pg_str}' AS pg (TYPE POSTGRES, READ_ONLY)")

    try:
        conn.execute(f"""
            CREATE TEMP TABLE staging AS
            SELECT * FROM pg."{schema}"."{table}"
            {limit_clause}
        """)
    except Exception as e:
        print(f"  Note: SELECT * failed ({e}), retrying with mapped columns...")
        source_cols = get_source_columns(mapping)
        col_list = ', '.join(f'"{c}"' for c in sorted(source_cols))
        conn.execute(f"""
            CREATE TEMP TABLE staging AS
            SELECT {col_list} FROM pg."{schema}"."{table}"
            {limit_clause}
        """)

    conn.execute("DETACH pg")


def create_staging_table(conn, source_path, mapping, env=None, limit=None):
    """Dispatch to format-specific staging function."""
    fmt = mapping.get('source', {}).get('format', 'csv')

    if fmt == 'csv':
        _staging_csv(conn, source_path, mapping, limit)
    elif fmt == 'xlsx':
        _staging_xlsx(conn, source_path, mapping, limit)
    elif fmt == 'parquet':
        _staging_parquet(conn, source_path, mapping, limit)
    elif fmt == 'postgresql':
        _staging_postgresql(conn, source_path, mapping, env, limit)
    else:
        raise ValueError(f"Unsupported format: {fmt}")
    return _number_staging(conn)


def _number_staging(conn):
    # Number source rows in file order (preserve_insertion_order keeps the
    # table in read order). _src_row is the deterministic tie-breaker for
    # dedup and the basis for result_id, so the same file always loads to
    # the same ids, and result attributes can be joined back exactly.
    conn.execute("ALTER TABLE staging RENAME TO staging_raw")
    conn.execute("""
        CREATE TEMP TABLE staging AS
        SELECT CAST(rowid + 1 AS BIGINT) AS _src_row, *
        FROM staging_raw
        ORDER BY rowid
    """)
    conn.execute("DROP TABLE staging_raw")

    count = conn.execute("SELECT COUNT(*) FROM staging").fetchone()[0]
    print(f"  Staging table: {count:,} rows")
    return count


def get_staging_cols(conn):
    """Source column names in file order, excluding the loader's _src_row."""
    return [row[0] for row in conn.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'staging' AND column_name <> '_src_row'
        ORDER BY ordinal_position
    """).fetchall()]


def load_env(env_path=None):
    """Load environment variables from .env file."""
    if env_path is None:
        env_path = PROJECT_ROOT / '.env'
    if not env_path.exists():
        return {}
    env = {}
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, value = line.partition('=')
                env[key.strip()] = value.strip().strip('"').strip("'")
    return env


# ============================================================================
# SQL expression builder
# ============================================================================

class DetectFlagError(Exception):
    """The source has no detect flag, or its mapping cannot be read."""


# Read when a mapping names a detect column but gives no transforms.
DEFAULT_DETECT_VALUES = {'t': True, 'true': True, '1': True, 'yes': True, 'y': True,
                         'f': False, 'false': False, '0': False, 'no': False, 'n': False}


def detect_sql(column, transforms, col_or_null):
    """SQL for results.detected, from the detect column and its transform map.

    Detection comes from the flag and nothing else. A value the map sends to
    true or false (compared trimmed and case-blind) is that; anything else - a
    blank, a value the map sends to null, one it does not list, NULL - is
    unknown (NULL), neither detected nor not. With no detect column the load
    is refused: TRUE made every non-detect's limit a measured concentration,
    FALSE would halve every detection.
    """
    col = col_or_null(column) if column else "NULL"
    if col == "NULL":
        raise DetectFlagError(
            f"The source has no detect flag column ({column or 'none mapped'} under "
            "results.detected). Without it a non-detect's limit cannot be told from a "
            "measured concentration; the load is refused rather than guessing.")
    values = {}
    for key, val in (DEFAULT_DETECT_VALUES if transforms is None else transforms).items():
        if val is not None and not isinstance(val, bool):
            raise DetectFlagError(f"transforms.detected maps {key!r} to {val!r}; "
                                  "expected true, false or null")
        k = str(key).strip().lower()
        if values.get(k, val) != val:
            raise DetectFlagError(f"transforms.detected maps {k!r} both ways")
        values[k] = val
    whens = [f"WHEN '{k.replace(chr(39), chr(39) * 2)}' THEN {str(v).upper()}"
             for k, v in sorted(values.items()) if v is not None]
    if not whens:
        raise DetectFlagError("transforms.detected maps no value to true or false")
    return (f"CASE LOWER(TRIM(CAST({col} AS VARCHAR))) {' '.join(whens)} "
            "ELSE CAST(NULL AS BOOLEAN) END")


def build_sql_exprs(mapping, staging_cols, method_class_map=None):
    """Build SQL expressions from mapping config, shared across all formats."""
    loc_map = mapping.get('locations', {})
    sample_map = mapping.get('samples', {})
    result_map = mapping.get('results', {})
    transforms = mapping.get('transforms', {})

    # Case-insensitive column lookup
    staging_cols_lower = {c.lower(): c for c in staging_cols}

    def col_or_null(mapping_value):
        if mapping_value is None:
            return "NULL"
        if mapping_value in staging_cols:
            return f'"{mapping_value}"'
        # Case-insensitive fallback
        actual = staging_cols_lower.get(mapping_value.lower())
        if actual:
            return f'"{actual}"'
        return "NULL"

    loc_id_col = loc_map['location_id']
    x_col = loc_map.get('x')
    y_col = loc_map.get('y')

    detect_expr = detect_sql(result_map.get('detected'), transforms.get('detected'),
                             col_or_null)

    # Several results of one sample in one method class are chosen among by
    # the source's reportable designation, then a declared test-type
    # precedence, then rank_reruns (shared rules D3). Both are read here,
    # once; a source without them leaves NULL, which decides nothing.
    reportable_col = col_or_null(result_map.get('reportable'))
    if reportable_col != "NULL":
        values = transforms.get('reportable') or {
            'yes': True, 'y': True, 'true': True, 't': True, '1': True,
            'no': False, 'n': False, 'false': False, 'f': False, '0': False}
        whens = ' '.join(f"WHEN '{str(k).strip().lower()}' THEN {str(v).upper()}"
                         for k, v in values.items() if v is not None)
        reportable_expr = (f"CASE LOWER(TRIM(CAST({reportable_col} AS VARCHAR))) {whens} "
                           "ELSE CAST(NULL AS BOOLEAN) END")
    else:
        reportable_expr = "CAST(NULL AS BOOLEAN)"
    test_type_col = col_or_null(result_map.get('test_type'))
    test_type_expr = (f"NULLIF(TRIM(CAST({test_type_col} AS VARCHAR)), '')"
                      if test_type_col != "NULL" else "CAST(NULL AS VARCHAR)")
    # test_type_precedence: first to last; a blank test type counts as the
    # first (a report without the column has no re-analyses to prefer over),
    # an unlisted one after every listed one.
    precedence = [str(t).strip().upper() for t in (mapping.get('test_type_precedence') or ['INITIAL'])]
    test_rank_expr = ("CASE WHEN " + test_type_expr + " IS NULL THEN 0 "
                      + ' '.join(f"WHEN UPPER({test_type_expr}) = '{t}' THEN {i}"
                                 for i, t in enumerate(precedence))
                      + f" ELSE {len(precedence)} END")

    # The limit that stands for a non-detect (shared rules D4): the limit the
    # reporting system used, where the source says which (report_limit:
    # EQuIS REPORT_RESULT_LIMIT), then the individual limits, in the order
    # the mapping's limit_chain declares. Chosen once, here, per row.
    report_limit_expr = f"TRY_CAST({col_or_null(result_map.get('report_limit'))} AS DOUBLE)"
    links = {
        'report': report_limit_expr,
        'reporting': f"TRY_CAST({col_or_null(result_map.get('reporting_limit'))} AS DOUBLE)",
        'method_detection': f"TRY_CAST({col_or_null(result_map.get('method_detection_limit'))} AS DOUBLE)",
        'quantitation': f"TRY_CAST({col_or_null(result_map.get('quantitation_limit'))} AS DOUBLE)",
    }
    chain = mapping.get('limit_chain') or ['report', 'reporting', 'method_detection', 'quantitation']
    unknown = [c for c in chain if c not in links]
    if unknown:
        raise RuleConflictError(f"limit_chain: unknown link(s) {unknown}; known: {list(links)}")
    nd_limit_expr = f"COALESCE({', '.join(links[c] for c in chain)})"
    limit_source_expr = ("CASE " + ' '.join(f"WHEN {links[c]} IS NOT NULL THEN '{c}'" for c in chain)
                         + " ELSE 'none' END")

    # Estimated expression
    estimated_col = result_map.get('estimated')
    estimated_from_qualifier = transforms.get('estimated_from_qualifier', False)
    qualifier_col = result_map.get('qualifier')

    if estimated_col and col_or_null(estimated_col) != "NULL" and not estimated_from_qualifier:
        estimated_transforms = transforms.get('estimated', {})
        if estimated_transforms:
            true_vals = [k for k, v in estimated_transforms.items() if v is True]
            if true_vals:
                vals = ', '.join(f"'{str(v).lower()}'" for v in true_vals)
                estimated_expr = f"LOWER(CAST({col_or_null(estimated_col)} AS VARCHAR)) IN ({vals})"
            else:
                estimated_expr = "FALSE"
        else:
            estimated_expr = f"LOWER(CAST({col_or_null(estimated_col)} AS VARCHAR)) IN ('t', 'true', '1', 'yes')"
    elif estimated_from_qualifier and qualifier_col and col_or_null(qualifier_col) != "NULL":
        estimated_expr = f"CAST({col_or_null(qualifier_col)} AS VARCHAR) LIKE '%J%'"
    else:
        estimated_expr = "FALSE"

    # TIC expression
    tic_col = result_map.get('tic')
    if tic_col and col_or_null(tic_col) != "NULL":
        tic_transforms = transforms.get('tic', {})
        if tic_transforms:
            true_vals = [k for k, v in tic_transforms.items() if v is True]
            if true_vals:
                vals = ', '.join(f"'{str(v).lower()}'" for v in true_vals)
                tic_expr = f"LOWER(CAST({col_or_null(tic_col)} AS VARCHAR)) IN ({vals})"
            else:
                tic_expr = "FALSE"
        else:
            tic_expr = f"LOWER(CAST({col_or_null(tic_col)} AS VARCHAR)) IN ('t', 'true', '1', 'yes')"
    else:
        tic_expr = "FALSE"

    # Fraction transform
    fraction_col = result_map.get('fraction')
    fraction_transforms = transforms.get('fraction', {})

    if fraction_col and col_or_null(fraction_col) != "NULL" and fraction_transforms:
        cases = []
        for src_val, dst_val in fraction_transforms.items():
            if dst_val is None:
                continue
            cases.append(
                f"WHEN CAST({col_or_null(fraction_col)} AS VARCHAR) = '{src_val}' THEN '{dst_val}'"
            )
        # Unknown codes pass through raw: an unmapped code is not "no fraction"
        raw_fraction = f"NULLIF(TRIM(CAST({col_or_null(fraction_col)} AS VARCHAR)), '')"
        fraction_expr = ("CASE " + " ".join(cases) + f" ELSE {raw_fraction} END"
                         if cases else raw_fraction)
    elif fraction_col and col_or_null(fraction_col) != "NULL":
        fraction_expr = f'CAST({col_or_null(fraction_col)} AS VARCHAR)'
    else:
        fraction_expr = "NULL"

    # Meas basis
    meas_basis_col = result_map.get('meas_basis')
    meas_basis_expr = (f'CAST({col_or_null(meas_basis_col)} AS VARCHAR)'
                       if meas_basis_col and col_or_null(meas_basis_col) != "NULL"
                       else "NULL")

    # strptime formats for date columns, tried in order (source.date_formats),
    # or a single one (source.date_format, e.g. EQuIS '%m/%d/%Y %I:%M:%S %p').
    # Text dates are cast to VARCHAR first so a typed TIMESTAMP column parses
    # too: it renders as '%Y-%m-%d %H:%M:%S'. A value no format reads falls
    # back to TRY_CAST, which handles ISO dates, and then to NULL.
    source_cfg = mapping.get('source', {})
    date_formats = source_cfg.get('date_formats') or (
        [source_cfg['date_format']] if source_cfg.get('date_format') else [])

    def date_expr(ref):
        if date_formats:
            tries = ', '.join(
                f"TRY_STRPTIME(CAST({ref} AS VARCHAR), '{f}')" for f in date_formats)
            return f"CAST(COALESCE({tries}, TRY_CAST({ref} AS TIMESTAMP)) AS DATE)"
        return f'TRY_CAST({ref} AS DATE)'

    # Date analyzed
    date_analyzed_col = result_map.get('date_analyzed')
    date_analyzed_expr = (date_expr(col_or_null(date_analyzed_col))
                          if date_analyzed_col and col_or_null(date_analyzed_col) != "NULL"
                          else "NULL")

    # Sample date expression
    sample_date_col = sample_map.get('sample_date')
    fmt = mapping.get('source', {}).get('format', 'csv')
    if date_formats and sample_date_col and col_or_null(sample_date_col) != "NULL":
        sample_date_expr = date_expr(col_or_null(sample_date_col))
    elif fmt == 'csv' and sample_date_col and col_or_null(sample_date_col) != "NULL":
        sd_ref = col_or_null(sample_date_col)
        sample_date_expr = (
            f"TRY_CAST(CASE WHEN {sd_ref} LIKE '% %' "
            f"THEN SPLIT_PART({sd_ref}, ' ', 1) "
            f"ELSE {sd_ref} END AS DATE)"
        )
    elif sample_date_col and col_or_null(sample_date_col) != "NULL":
        sample_date_expr = f'TRY_CAST({col_or_null(sample_date_col)} AS DATE)'
    else:
        sample_date_expr = "NULL"

    # Sample time: the same column, read only by formats that carry a time of
    # day, so a source that records dates only leaves it NULL rather than
    # inventing midnight (shared rules D1: an event is a sample at its time).
    time_formats = [f for f in date_formats if '%H' in f or '%I' in f]
    if sample_date_col and col_or_null(sample_date_col) != "NULL":
        sd_ref = col_or_null(sample_date_col)
        tries = [f"TRY_STRPTIME(CAST({sd_ref} AS VARCHAR), '{f}')" for f in time_formats]
        tries.append(f"CASE WHEN CAST({sd_ref} AS VARCHAR) LIKE '%:%' "
                     f"THEN TRY_CAST({sd_ref} AS TIMESTAMP) END")
        sample_datetime_expr = f"COALESCE({', '.join(tries)})"
    else:
        sample_datetime_expr = "NULL"

    # Depth computation
    top_depth_col = sample_map.get('top_depth')
    bottom_depth_col = sample_map.get('bottom_depth')
    depth_col = sample_map.get('depth')

    if depth_col and col_or_null(depth_col) != "NULL":
        depth_expr = f'TRY_CAST({col_or_null(depth_col)} AS DOUBLE)'
    elif (top_depth_col and col_or_null(top_depth_col) != "NULL" and
          bottom_depth_col and col_or_null(bottom_depth_col) != "NULL"):
        depth_expr = (
            f"(TRY_CAST({col_or_null(top_depth_col)} AS DOUBLE) + "
            f"TRY_CAST({col_or_null(bottom_depth_col)} AS DOUBLE)) / 2.0"
        )
    else:
        depth_expr = "NULL"

    return {
        'col_or_null': col_or_null,
        'loc_id_col': loc_id_col,
        'x_col': x_col,
        'y_col': y_col,
        'detect_expr': detect_expr,
        'reportable_expr': reportable_expr,
        'test_type_expr': test_type_expr,
        'test_rank_expr': test_rank_expr,
        'report_limit_expr': report_limit_expr,
        'nd_limit_expr': nd_limit_expr,
        'limit_source_expr': limit_source_expr,
        # from classify_methods' map, resolved from the data at load or publish
        'method_class_expr': method_class_sql(method_class_map,
                                              col_or_null(result_map.get('method_code'))),
        'estimated_expr': estimated_expr,
        'tic_expr': tic_expr,
        'fraction_expr': fraction_expr,
        'meas_basis_expr': meas_basis_expr,
        'date_analyzed_expr': date_analyzed_expr,
        'sample_id_col': sample_map['sample_id'],
        'sample_loc_col': sample_map.get('location_id', loc_map['location_id']),
        'sample_date_col': sample_date_col,
        'sample_date_expr': sample_date_expr,
        'sample_datetime_expr': sample_datetime_expr,
        'matrix_col': sample_map.get('matrix'),
        'depth_expr': depth_expr,
        'analyte_col': result_map['analyte'],
        'result_col': result_map['result'],
        'qualifier_col': qualifier_col,
    }


# ============================================================================
# Mapping report
# ============================================================================

def analyze_unmapped_fields(staging_cols, mapping):
    """Analyze which source columns are mapped vs unmapped."""
    mapped = []
    source_cols_used = set()

    staging_cols_lower = {c.lower(): c for c in staging_cols}

    def check_mapping(table_name, table_map):
        for field, source_col in table_map.items():
            if source_col and isinstance(source_col, str):
                # Direct or case-insensitive match
                if source_col in staging_cols:
                    mapped.append((source_col, f'{table_name}.{field}'))
                    source_cols_used.add(source_col)
                elif source_col.lower() in staging_cols_lower:
                    actual = staging_cols_lower[source_col.lower()]
                    mapped.append((actual, f'{table_name}.{field}'))
                    source_cols_used.add(actual)

    check_mapping('locations', mapping.get('locations', {}))
    check_mapping('samples', mapping.get('samples', {}))
    check_mapping('results', mapping.get('results', {}))

    # River columns
    river_cfg = mapping.get('river', {})
    for key, target in [('river_mile_column', 'location_river_miles.river_mile'),
                        ('bank_side_column', 'location_river_miles.bank_side')]:
        col = river_cfg.get(key)
        if col and col in staging_cols:
            mapped.append((col, target))
            source_cols_used.add(col)

    # Attribute columns
    for attr_key, target_table in [('location_attributes', 'location_attributes'),
                                   ('sample_attributes', 'sample_attributes'),
                                   ('result_attributes', 'result_attributes')]:
        for attr in mapping.get(attr_key, []):
            if attr in staging_cols:
                mapped.append((attr, f'{target_table}.{attr}'))
                source_cols_used.add(attr)

    unmapped = [col for col in staging_cols if col not in source_cols_used]

    # Schema gap analysis
    schema_fields = {
        'locations': ['location_id', 'x', 'y', 'loc_type', 'loc_desc',
                      'aquifer', 'region', 'dataset', 'project', 'study', 'study_description',
                      'elev_ground', 'elev_measuring'],
        'samples': ['sample_id', 'location_id', 'sample_date', 'matrix', 'top_depth',
                    'bottom_depth', 'depth', 'depth_units', 'sample_type', 'composite_type',
                    'event', 'sample_material', 'parent_sample_id'],
        'results': ['result_id', 'sample_id', 'analyte', 'anl_sm_mb', 'analyte_group',
                    'cas_number', 'result', 'units', 'std_units', 'method_detection_limit',
                    'reporting_limit', 'quantitation_limit', 'upper_quantitation_limit',
                    'dilution_factor', 'modeled_relative_uncertainty',
                    'detected', 'estimated', 'tic', 'qualifier', 'lab_name', 'lab_sample_id',
                    'method_code', 'date_analyzed', 'lab_package_id', 'validation_level',
                    'lab_flags', 'validator_flags', 'meas_basis', 'fraction'],
    }

    auto_fields = {'result_id', 'depth', 'detected', 'estimated', 'tic'}
    schema_unmapped = []

    for table, fields in schema_fields.items():
        table_map = mapping.get(table, {})
        for field in fields:
            if field in auto_fields:
                continue
            if field not in table_map or table_map.get(field) is None:
                schema_unmapped.append(f'{table}.{field}')

    return {
        'mapped': mapped,
        'unmapped': unmapped,
        'schema_unmapped': schema_unmapped,
    }


def print_mapping_report(staging_cols, mapping):
    """Print a report of mapped and unmapped fields."""
    analysis = analyze_unmapped_fields(staging_cols, mapping)

    print("\n" + "=" * 60)
    print("FIELD MAPPING REPORT")
    print("=" * 60)

    print(f"\n--- Mapped Fields ({len(analysis['mapped'])}) ---")
    for source, target in sorted(analysis['mapped'], key=lambda x: x[1]):
        print(f"  {source:35s} -> {target}")

    if analysis['unmapped']:
        print(f"\n--- UNMAPPED Source Columns ({len(analysis['unmapped'])}) ---")
        print("  These source columns are NOT being loaded:")
        for col in sorted(analysis['unmapped']):
            print(f"    - {col}")

    if analysis['schema_unmapped']:
        print(f"\n--- Schema Fields Without Mapping ({len(analysis['schema_unmapped'])}) ---")
        for field in sorted(analysis['schema_unmapped']):
            print(f"    - {field}")

    print("=" * 60)


# ============================================================================
# Insert functions
# ============================================================================

class LoadConflictError(Exception):
    """One id arrived with different values for fields that must be constant."""


def check_id_conflicts(conn, label, id_expr, value_exprs, allow_conflicts=False):
    """
    Count ids whose rows disagree on fields that belong to the id.

    Dedup keeps the first row in file order; if the discarded rows carried
    different values, that choice would be silent. Raise instead, unless
    allow_conflicts is set, in which case report and keep the first row.
    NULLs are not conflicts: a value and a missing value do not disagree.
    """
    value_exprs = {k: v for k, v in value_exprs.items() if v != "NULL"}
    if not value_exprs:
        return 0
    flags = ",\n".join(
        f"COUNT(DISTINCT CAST({expr} AS VARCHAR)) > 1 AS \"{name}\""
        for name, expr in value_exprs.items()
    )
    any_flag = " OR ".join(f'"{name}"' for name in value_exprs)
    rows = conn.execute(f"""
        SELECT * FROM (
            SELECT CAST({id_expr} AS VARCHAR) AS id, {flags}
            FROM staging
            WHERE {id_expr} IS NOT NULL
            GROUP BY 1
        )
        WHERE {any_flag}
        ORDER BY id
    """).fetchall()
    if not rows:
        return 0

    names = list(value_exprs)
    examples = []
    for row in rows[:5]:
        fields = [n for n, flag in zip(names, row[1:]) if flag]
        examples.append(f"{row[0]} ({', '.join(fields)})")
    msg = (f"{len(rows):,} {label} id(s) have conflicting values across source rows, "
           f"e.g. {'; '.join(examples)}")
    if not allow_conflicts:
        raise LoadConflictError(
            msg + ". Fix the source, or pass --allow-conflicts to keep the first "
                  "row in file order for each id.")
    print(f"  Warning: {msg}. Keeping the first row in file order.")
    return len(rows)


class PolicyError(Exception):
    """The mapping names a non-detect policy that does not exist."""


def record_nd_policy(conn, mapping):
    """Record the dataset's non-detect policy in chemduck_settings.

    The mapping may name one (`nd_policy: name`, or `nd_policy: {name: ...,
    version: n}`); without one, chemduck_default. The version is resolved
    NOW and recorded, so the dataset keeps the numbers it was loaded with
    when a newer version of the policy ships. Returns (name, version).
    """
    spec = mapping.get('nd_policy') or 'chemduck_default'
    name, version = (spec, None) if isinstance(spec, str) else (spec.get('name'), spec.get('version'))
    row = conn.execute(
        "SELECT max(version) FILTER (WHERE ? IS NULL OR version = ?) FROM nd_policies WHERE name = ?",
        [version, version, name]).fetchone()
    if row is None or row[0] is None:
        known = [r[0] for r in conn.execute(
            "SELECT DISTINCT name || '@' || version FROM nd_policies ORDER BY 1").fetchall()]
        raise PolicyError(f"nd_policy {name}{'' if version is None else '@' + str(version)} "
                          f"is not in nd_policies (known: {', '.join(known)})")
    conn.execute("INSERT OR REPLACE INTO chemduck_settings VALUES ('nd_policy', ?), "
                 "('nd_policy_version', ?)", [name, str(row[0])])
    return name, row[0]


def insert_locations(conn, mapping, exprs, staging_cols, allow_conflicts=False):
    """Insert locations from staging table."""
    loc_map = mapping.get('locations', {})
    col_or_null = exprs['col_or_null']

    check_id_conflicts(conn, 'location', f'"{exprs["loc_id_col"]}"', {
        'x': f"TRY_CAST({col_or_null(exprs['x_col'])} AS DOUBLE)",
        'y': f"TRY_CAST({col_or_null(exprs['y_col'])} AS DOUBLE)",
        'loc_type': col_or_null(loc_map.get('loc_type')),
        'elev_ground': f"TRY_CAST({col_or_null(loc_map.get('elev_ground'))} AS DOUBLE)",
    }, allow_conflicts)

    conn.execute(f"""
        INSERT INTO locations (
            location_id, x, y, loc_type, loc_desc,
            aquifer, region, dataset, project, study, study_description,
            elev_ground, elev_measuring
        )
        SELECT
            CAST("{exprs['loc_id_col']}" AS VARCHAR) AS location_id,
            TRY_CAST({col_or_null(exprs['x_col'])} AS DOUBLE) AS x,
            TRY_CAST({col_or_null(exprs['y_col'])} AS DOUBLE) AS y,
            CAST({col_or_null(loc_map.get('loc_type'))} AS VARCHAR) AS loc_type,
            CAST({col_or_null(loc_map.get('loc_desc'))} AS VARCHAR) AS loc_desc,
            CAST({col_or_null(loc_map.get('aquifer'))} AS VARCHAR) AS aquifer,
            CAST({col_or_null(loc_map.get('region'))} AS VARCHAR) AS region,
            CAST({col_or_null(loc_map.get('dataset'))} AS VARCHAR) AS dataset,
            CAST({col_or_null(loc_map.get('project'))} AS VARCHAR) AS project,
            CAST({col_or_null(loc_map.get('study'))} AS VARCHAR) AS study,
            CAST({col_or_null(loc_map.get('study_description'))} AS VARCHAR) AS study_description,
            TRY_CAST({col_or_null(loc_map.get('elev_ground'))} AS DOUBLE) AS elev_ground,
            TRY_CAST({col_or_null(loc_map.get('elev_measuring'))} AS DOUBLE) AS elev_measuring
        FROM staging
        WHERE "{exprs['loc_id_col']}" IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY CAST("{exprs['loc_id_col']}" AS VARCHAR)
            -- Prefer a row that carries coordinates, then file order
            ORDER BY ({col_or_null(exprs['x_col'])} IS NULL), _src_row
        ) = 1
        ON CONFLICT (location_id) DO NOTHING
    """)

    # Handle QC/lab samples without locations
    qc_count = conn.execute(f"""
        SELECT COUNT(DISTINCT "{exprs['sample_id_col']}")
        FROM staging
        WHERE ("{exprs['sample_loc_col']}" IS NULL OR "{exprs['sample_loc_col']}" = '')
          AND "{exprs['sample_id_col']}" IS NOT NULL
    """).fetchone()[0]
    if qc_count > 0:
        print(f"  Note: {qc_count:,} samples have no location (QC/lab samples)")
        conn.execute("""
            INSERT INTO locations (location_id, loc_type, loc_desc)
            VALUES ('_QC_LAB', 'Lab QC', 'Placeholder for QC/lab samples without field location')
            ON CONFLICT (location_id) DO NOTHING
        """)

    loc_count = conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0]
    print(f"  Locations: {loc_count:,}")
    return loc_count


def insert_location_attributes(conn, mapping, exprs, staging_cols):
    """Insert location-level EAV attributes from YAML config."""
    loc_attrs = mapping.get('location_attributes', [])
    if not loc_attrs:
        return 0

    print(f"  Loading location attributes: {', '.join(loc_attrs)}")
    for attr in loc_attrs:
        if attr in staging_cols:
            conn.execute(f"""
                INSERT INTO location_attributes (location_id, field_name, field_value)
                SELECT
                    CAST("{exprs['loc_id_col']}" AS VARCHAR) AS location_id,
                    '{attr}' AS field_name,
                    CAST("{attr}" AS VARCHAR) AS field_value
                FROM staging
                WHERE "{exprs['loc_id_col']}" IS NOT NULL AND "{attr}" IS NOT NULL
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY CAST("{exprs['loc_id_col']}" AS VARCHAR) ORDER BY _src_row
                ) = 1
                ON CONFLICT (location_id, field_name) DO NOTHING
            """)
        else:
            print(f"    Warning: attribute column '{attr}' not found in source")

    count = conn.execute("SELECT COUNT(*) FROM location_attributes").fetchone()[0]
    print(f"  Location attributes: {count:,}")
    return count


def insert_samples(conn, mapping, exprs, staging_cols, allow_conflicts=False):
    """Insert samples from staging table."""
    sample_map = mapping.get('samples', {})
    col_or_null = exprs['col_or_null']

    # Build location_id expression with QC_LAB fallback
    loc_ref = col_or_null(exprs['sample_loc_col'])
    if loc_ref != "NULL":
        loc_id_expr = f"COALESCE(NULLIF(CAST({loc_ref} AS VARCHAR), ''), '_QC_LAB')"
    else:
        loc_id_expr = "'_QC_LAB'"

    check_id_conflicts(conn, 'sample', f'"{exprs["sample_id_col"]}"', {
        'location_id': loc_ref,
        'sample_date': exprs['sample_date_expr'],
        'sample_datetime': exprs['sample_datetime_expr'],
        'matrix': col_or_null(exprs['matrix_col']),
        'top_depth': f"TRY_CAST({col_or_null(sample_map.get('top_depth'))} AS DOUBLE)",
        'bottom_depth': f"TRY_CAST({col_or_null(sample_map.get('bottom_depth'))} AS DOUBLE)",
        'sample_type': col_or_null(sample_map.get('sample_type')),
        'parent_sample_id': col_or_null(sample_map.get('parent_sample_id')),
    }, allow_conflicts)

    conn.execute(f"""
        INSERT INTO samples (
            sample_id, location_id, sample_date, matrix,
            top_depth, bottom_depth, depth, depth_units,
            sample_type, composite_type, event,
            sample_material, parent_sample_id, sample_datetime
        )
        SELECT
            CAST("{exprs['sample_id_col']}" AS VARCHAR) AS sample_id,
            {loc_id_expr} AS location_id,
            {exprs['sample_date_expr']} AS sample_date,
            CAST({col_or_null(exprs['matrix_col'])} AS VARCHAR) AS matrix,
            TRY_CAST({col_or_null(sample_map.get('top_depth'))} AS DOUBLE) AS top_depth,
            TRY_CAST({col_or_null(sample_map.get('bottom_depth'))} AS DOUBLE) AS bottom_depth,
            {exprs['depth_expr']} AS depth,
            CAST({col_or_null(sample_map.get('depth_units'))} AS VARCHAR) AS depth_units,
            CAST({col_or_null(sample_map.get('sample_type'))} AS VARCHAR) AS sample_type,
            CAST({col_or_null(sample_map.get('composite_type'))} AS VARCHAR) AS composite_type,
            CAST({col_or_null(sample_map.get('event'))} AS VARCHAR) AS event,
            CAST({col_or_null(sample_map.get('sample_material'))} AS VARCHAR) AS sample_material,
            CAST({col_or_null(sample_map.get('parent_sample_id'))} AS VARCHAR) AS parent_sample_id,
            {exprs['sample_datetime_expr']} AS sample_datetime
        FROM staging
        WHERE "{exprs['sample_id_col']}" IS NOT NULL
          AND {exprs['sample_date_expr']} IS NOT NULL
          AND {col_or_null(exprs['matrix_col'])} IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY CAST("{exprs['sample_id_col']}" AS VARCHAR)
            ORDER BY _src_row
        ) = 1
        ON CONFLICT (sample_id) DO NOTHING
    """)

    sample_count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    print(f"  Samples: {sample_count:,}")
    return sample_count


class RuleConflictError(Exception):
    """A declared sample or method rule is unusable, or matches ambiguously."""


def method_class_sql(class_map, method_ref):
    """SQL giving a result's method class from its method code.

    class_map: {code (upper-cased, trimmed): class}, as classify_methods
    resolved it from the data. A code the map lacks is its own class (shared
    rules D3: an uncurated method stays apart until someone curates it).
    """
    code = f"UPPER(TRIM(CAST({method_ref} AS VARCHAR)))"
    if not class_map:
        return code
    entries = ', '.join(f"{_sql_text(k)}: {_sql_text(v)}" for k, v in sorted(class_map.items()))
    return f"COALESCE(MAP {{{entries}}}[{code}], {code})"


def record_nd_multiplier(conn, exprs, source='staging'):
    """Measure the export's non-detect multiplier, and record it (shared rules D4).

    An export may already have multiplied a non-detect's reported value
    (EQuIS's Non-Detect Multiplier: value = 0.5 x limit). Comparing each
    non-detect's reported value with its chosen limit says which: the
    ratios go to load_report, and the multiplier - the ratio at least 95% of
    them share, or 'mixed' - to chemduck_settings (nd_value_multiplier).
    Non-detects with no limit at all are counted: they have no value and can
    never win a selection. No ChemDuck rule values a non-detect from its
    reported value, so a multiplier is never applied twice; recording it
    says what the reported values mean.
    """
    value = f"TRY_CAST({exprs['col_or_null'](exprs['result_col'])} AS DOUBLE)"
    nd = f"(({exprs['detect_expr']}) IS NOT DISTINCT FROM FALSE)"
    conn.execute("DELETE FROM load_report WHERE category = 'nd_limit'")
    ratios = conn.execute(f"""
        SELECT round({value} / {exprs['nd_limit_expr']}, 3) AS ratio, count(*) AS n
        FROM {source}
        WHERE {nd} AND {value} IS NOT NULL AND {exprs['nd_limit_expr']} > 0
        GROUP BY 1 ORDER BY 2 DESC""").fetchall()
    no_limit = conn.execute(f"""SELECT count(*) FROM {source}
        WHERE {nd} AND {exprs['nd_limit_expr']} IS NULL""").fetchone()[0]
    total = sum(n for _, n in ratios)
    multiplier = None
    if total:
        top, n_top = ratios[0]
        multiplier = str(top) if n_top >= 0.95 * total else 'mixed'
        conn.execute("INSERT OR REPLACE INTO chemduck_settings VALUES ('nd_value_multiplier', ?)",
                     [multiplier])
    report = [('nd_limit', f"value / limit = {r}", n, None) for r, n in ratios[:5]]
    report.append(('nd_limit', 'non-detects with no limit (no value)', no_limit, None))
    conn.executemany("INSERT OR REPLACE INTO load_report VALUES (?, ?, ?, ?)", report)
    print(f"  Non-detect multiplier: {multiplier or 'unknown'} ({total:,} non-detects with a "
          f"value and a limit; {no_limit:,} with no limit)")
    return multiplier


# The columns a call's scope filters read, by (mapping section, field). A
# source without one has nothing to filter on: the macros let such a filter
# match everything and name it in filters_ignored.
SCOPE_COLUMNS = {
    'matrix': ('samples', 'matrix'),
    'sample_type': ('samples', 'sample_type'),
    'fraction': ('results', 'fraction'),
    'meas_basis': ('results', 'meas_basis'),
    'method_code': ('results', 'method_code'),
    'reportable': ('results', 'reportable'),
}


def record_scope(conn, mapping, exprs, source='staging'):
    """Record what a call's scope can rely on (shared rules, "one set of
    scope parameters"), in chemduck_settings and load_report.

    source_columns_absent: the SCOPE_COLUMNS the source does not carry.
    reportable_only: whether calls leave non-reportable results out by
    default - TRUE where the source carries a reportable column, unless the
    mapping says `reportable_only: false`. load_report counts the results
    not reportable, and the results dated after the load (with the latest
    date): a sample date in the future is a typo or a planned event, never
    a measurement.
    """
    absent = [name for name, (section, field) in SCOPE_COLUMNS.items()
              if exprs['col_or_null'](mapping.get(section, {}).get(field)) == "NULL"
              # declared QC rules type samples the source leaves untyped
              and not (name == 'sample_type' and mapping.get('qc_rules'))]
    reportable_only = bool(mapping.get('reportable_only', 'reportable' not in absent))
    conn.execute("INSERT OR REPLACE INTO chemduck_settings VALUES "
                 "('source_columns_absent', ?), ('reportable_only', ?)",
                 [','.join(absent), 'true' if reportable_only else 'false'])
    conn.execute("DELETE FROM load_report WHERE category IN ('reportable', 'dates')")
    report = []
    if 'reportable' not in absent:
        n = conn.execute(f"""SELECT count(*) FROM {source}
            WHERE ({exprs['reportable_expr']}) IS NOT DISTINCT FROM FALSE""").fetchone()[0]
        report.append(('reportable', 'results not reportable', n,
                       'left out of every call unless p_reportable_only := FALSE'
                       if reportable_only else 'kept: the mapping sets reportable_only: false'))
    today = conn.execute("SELECT CAST(current_date AS VARCHAR)").fetchone()[0]
    n, latest = conn.execute(f"""SELECT count(*), CAST(max(d) AS VARCHAR) FROM (
        SELECT CAST({exprs['sample_date_expr']} AS DATE) AS d FROM {source}) WHERE d > current_date
        """).fetchone()
    report.append(('dates', 'results dated after the load', n,
                   f"load {today}" + (f"; latest {latest}" if latest else '')))
    conn.executemany("INSERT OR REPLACE INTO load_report VALUES (?, ?, ?, ?)", report)
    print(f"  Scope: reportable only {reportable_only}; absent {absent or 'none'}; "
          f"{n:,} result(s) dated after the load")
    return absent


UNIT_SOURCES = ('as_reported', 'source', 'chemduck', 'unconverted', 'contradicts_matrix')


def plan_units(conn, mapping, exprs, source='staging'):
    """Choose each variable's canonical unit, and how each row reaches it (shared rules D5).

    A variable, for units, is (analyte, matrix class - or the matrix when it
    has none - , method class): the matrices of one class share a unit, so
    they can be combined (D6). Its canonical unit is the one most of its rows
    can be put in: the mapping's canonical_units pin it where they name a
    unit some row reaches ([{units, analyte?, matrix_class?}], the most
    specific first). A row reaches it by a factor from, first, the source
    system's own factors (the mapping's unit_conversions: [{from, to,
    factor, matrix_class?}]) and then ChemDuck's unit_conversions, each
    either way round, a factor scoped to the row's matrix class before a
    general one. A unit whose dimension contradicts the matrix (unit_dimensions,
    matrix_class_dimensions; a per-kilogram unit in water) is never converted
    and never canonical, unless the method class is a leach or an extract.

    Writes what it decided to load_report (category 'units'): every
    conversion, every unit left unconverted, every contradiction, and the
    row totals. Returns {(analyte, matrix, method_class, unit): (to_unit,
    factor, unit_source)} for the rows not already in their canonical unit,
    for unit_lookup_sql to freeze into the results SELECT.
    """
    col_or_null = exprs['col_or_null']
    result_map = mapping.get('results', {})
    units_ref = col_or_null(result_map.get('units'))
    conn.execute("DELETE FROM load_report WHERE category = 'units'")
    note = mapping.get('units_reported_as')
    if note:
        conn.execute("INSERT OR REPLACE INTO chemduck_settings VALUES ('units_reported_as', ?)",
                     [str(note)])
    if units_ref == "NULL":
        return {}

    source_factors = []
    for i, f in enumerate(mapping.get('unit_conversions') or []):
        if not f.get('from') or not f.get('to') or not f.get('factor'):
            raise RuleConflictError(f"unit_conversions[{i}]: needs from, to and factor")
        source_factors.append((str(f['from']), str(f['to']), float(f['factor']),
                               str(f.get('matrix_class') or '')))
    chemduck_factors = conn.execute("""SELECT from_units, to_units, multiplier, COALESCE(matrix_class, '')
        FROM unit_conversions WHERE COALESCE(analyte_group, '') = ''""").fetchall()
    # (from, to) -> [(rank, scope, factor, unit_source)]: the source's
    # factors before ChemDuck's, each direct before inverse
    factors = {}
    rank = 0
    for src, table in (('source', source_factors), ('chemduck', chemduck_factors)):
        for inverse in (False, True):
            for a, b, k, scope in table:
                if inverse:
                    a, b, k = b, a, 1.0 / k
                factors.setdefault((a, b), []).append((rank, scope, k, src))
            rank += 1

    def factor(u, c, mclass):
        usable = [(r, scope == '', k, src) for r, scope, k, src in factors.get((u, c), ())
                  if scope in ('', mclass)]
        if not usable:
            return None
        _, _, k, src = min(usable)
        return k, src

    unit_expr = f"NULLIF(TRIM(CAST({units_ref} AS VARCHAR)), '')"
    combos = conn.execute(f"""
        WITH c AS (
            SELECT CAST({col_or_null(exprs['analyte_col'])} AS VARCHAR) AS analyte,
                   CAST({col_or_null(exprs['matrix_col'])} AS VARCHAR) AS matrix,
                   CAST({exprs['method_class_expr']} AS VARCHAR) AS method_class,
                   {unit_expr} AS unit, count(*) AS n
            FROM {source}
            WHERE "{exprs['sample_id_col']}" IS NOT NULL
              AND {col_or_null(exprs['analyte_col'])} IS NOT NULL AND {unit_expr} IS NOT NULL
            GROUP BY ALL)
        SELECT c.analyte, c.matrix, mc.matrix_class, c.method_class, c.unit, c.n,
               COALESCE(ud.dimension <> mcd.dimension
                        AND NOT (COALESCE(c.method_class, '') LIKE 'leach:%'
                                 OR COALESCE(c.method_class, '') LIKE 'extract:%'), FALSE)
        FROM c
        LEFT JOIN matrix_classes mc ON mc.matrix = c.matrix
        LEFT JOIN unit_dimensions ud ON ud.units = c.unit
        LEFT JOIN matrix_class_dimensions mcd ON mcd.matrix_class = mc.matrix_class
        ORDER BY ALL""").fetchall()

    pins = mapping.get('canonical_units') or []
    for i, p in enumerate(pins):
        if not p.get('units'):
            raise RuleConflictError(f"canonical_units[{i}]: needs units")

    groups = {}
    for analyte, matrix, mclass, mc, unit, n, contra in combos:
        groups.setdefault((analyte, mclass or matrix, mc), []).append((matrix, mclass, unit, n, contra))

    plan, report = {}, {}
    totals = dict.fromkeys(UNIT_SOURCES, 0)

    def add(item, n):
        report[item] = report.get(item, 0) + n

    for (analyte, mkey, mc), rows in groups.items():
        mclass = rows[0][1]
        own = {}
        for _, _, unit, n, contra in rows:
            if not contra:
                own[unit] = own.get(unit, 0) + n

        def coverage(c):
            return sum(n for u, n in own.items() if u == c or factor(u, c, mclass))

        canonical = None
        matching = [p for p in pins
                    if (p.get('analyte') in (None, analyte))
                    and (p.get('matrix_class') in (None, mclass))]
        matching.sort(key=lambda p: -(2 * (p.get('analyte') is not None)
                                      + (p.get('matrix_class') is not None)))
        for p in matching:
            if coverage(str(p['units'])) > 0:
                canonical = str(p['units'])
                break
        if canonical is None and own:
            canonical = min(own, key=lambda c: (-coverage(c), -own[c], c))

        label = f"{analyte} | {mkey} | {mc}"
        for matrix, _, unit, n, contra in rows:
            if contra:
                plan[(analyte, matrix, mc, unit)] = (None, None, 'contradicts_matrix')
                add(f"contradicts matrix: {analyte} | {matrix} | {mc}: {unit}", n)
                totals['contradicts_matrix'] += n
            elif unit == canonical:
                totals['as_reported'] += n
            elif (f := factor(unit, canonical, mclass)):
                plan[(analyte, matrix, mc, unit)] = (canonical, f[0], f[1])
                add(f"converted: {label}: {unit} -> {canonical} x {f[0]:g} ({f[1]})", n)
                totals[f[1]] += n
            else:
                plan[(analyte, matrix, mc, unit)] = (None, None, 'unconverted')
                add(f"unconverted: {label}: {unit} (canonical {canonical})", n)
                totals['unconverted'] += n

    report_rows = [('units', item, n, None) for item, n in sorted(report.items())]
    report_rows += [('units', f"rows {s}", n, None) for s, n in totals.items()]
    conn.executemany("INSERT OR REPLACE INTO load_report VALUES (?, ?, ?, ?)", report_rows)
    print("  Units: " + ", ".join(f"{n:,} {s}" for s, n in totals.items() if n))
    return plan


def unit_lookup_sql(plan, exprs, mapping):
    """SQL giving a row's unit plan as STRUCT(u, f, s), or NULL when the row
    is in its canonical unit: plan_units' decisions, frozen as a MAP literal
    keyed on [analyte, matrix, method class, unit]."""
    if not plan:
        return None
    col_or_null = exprs['col_or_null']
    units_ref = col_or_null(mapping.get('results', {}).get('units'))

    def num(x):
        return "NULL" if x is None else repr(float(x))
    entries = ', '.join(
        f"[{', '.join(_sql_text(k if k is not None else '') for k in key)}]: "
        f"{{'u': {_sql_text(u)}, 'f': {num(f)}, 's': {_sql_text(s)}}}"
        for key, (u, f, s) in sorted(plan.items(), key=lambda kv: tuple(x or '' for x in kv[0])))
    key = ", ".join([
        f"COALESCE(CAST({col_or_null(exprs['analyte_col'])} AS VARCHAR), '')",
        f"COALESCE(CAST({col_or_null(exprs['matrix_col'])} AS VARCHAR), '')",
        f"COALESCE(CAST({exprs['method_class_expr']} AS VARCHAR), '')",
        f"COALESCE(NULLIF(TRIM(CAST({units_ref} AS VARCHAR)), ''), '')"])
    return (f"CAST(MAP {{{entries}}}[[{key}]] AS STRUCT(u VARCHAR, f DOUBLE, s VARCHAR))")


def classify_methods(conn, mapping, exprs, source='staging'):
    """Classify the method codes in `source`, once, and report the uncurated.

    Each distinct code (upper-cased, trimmed) takes the class of the
    lowest-priority pattern it matches whole: the mapping's own
    method_classes (priority 0, each {pattern, method_class}) before the
    seeded method_classes table. Two classes at one priority refuse
    (RuleConflictError). A code nothing matches is its own class; load_report
    lists each with its result count, as the load's curation list. Returns
    {code: class} for the curated codes.
    """
    method_ref = exprs['col_or_null'](mapping.get('results', {}).get('method_code'))
    conn.execute("DELETE FROM load_report WHERE category = 'method_class'")
    if method_ref == "NULL":
        return {}
    own = mapping.get('method_classes') or []
    for i, r in enumerate(own):
        if not r.get('pattern') or not r.get('method_class'):
            raise RuleConflictError(f"method_classes[{i}]: needs pattern and method_class")
    own_rows = ' UNION ALL '.join(
        f"SELECT {_sql_text(str(r['pattern']).upper())} AS pattern, "
        f"{_sql_text(r['method_class'])} AS method_class, 0 AS priority" for r in own)
    rules = ("SELECT pattern, method_class, priority FROM method_classes"
             + (f" UNION ALL {own_rows}" if own_rows else ""))
    conn.execute(f"""CREATE OR REPLACE TEMP TABLE _method_codes AS
        SELECT UPPER(TRIM(CAST({method_ref} AS VARCHAR))) AS code, count(*) AS n
        FROM {source} WHERE NULLIF(TRIM(CAST({method_ref} AS VARCHAR)), '') IS NOT NULL
        GROUP BY 1""")
    conn.execute(f"""CREATE OR REPLACE TEMP TABLE _method_hits AS
        SELECT c.code, c.n, r.pattern, r.method_class, r.priority,
               min(r.priority) OVER (PARTITION BY c.code) AS best
        FROM _method_codes c JOIN ({rules}) r ON regexp_full_match(c.code, r.pattern)""")
    clash = conn.execute("""SELECT code, list(DISTINCT method_class ORDER BY method_class)
        FROM _method_hits WHERE priority = best GROUP BY code
        HAVING count(DISTINCT method_class) > 1 ORDER BY code LIMIT 5""").fetchall()
    if clash:
        raise RuleConflictError("method codes matching patterns of two classes at one priority: "
                                + "; ".join(f"{c}: {v}" for c, v in clash))
    class_map = dict(conn.execute("""SELECT code, any_value(method_class) FROM _method_hits
                                     WHERE priority = best GROUP BY code""").fetchall())
    report = [('method_class', f"uncurated: {code}", n, 'its own class until curated')
              for code, n in conn.execute("""SELECT code, n FROM _method_codes
                  WHERE code NOT IN (SELECT code FROM _method_hits) ORDER BY n DESC, code""").fetchall()]
    for cls, n_codes, n in conn.execute("""SELECT method_class, count(DISTINCT code), sum(n)
            FROM (SELECT DISTINCT code, n, method_class FROM _method_hits WHERE priority = best)
            GROUP BY 1 ORDER BY 1""").fetchall():
        report.append(('method_class', f"class {cls}", n, f"{n_codes} code(s)"))
    if report:
        conn.executemany("INSERT OR REPLACE INTO load_report VALUES (?, ?, ?, ?)", report)
    print(f"  Method classes: {len(class_map)} code(s) curated, "
          f"{sum(1 for r in report if r[1].startswith('uncurated'))} uncurated")
    return class_map


def _sql_text(value):
    return "NULL" if value is None else "'" + str(value).replace("'", "''") + "'"


def _rule_column(exprs, rules_key, i, rule):
    col = exprs['col_or_null'](rule.get('column'))
    if not rule.get('column') or col == "NULL":
        raise RuleConflictError(f"{rules_key}[{i}]: column {rule.get('column')!r} is not in the source")
    if not rule.get('pattern'):
        raise RuleConflictError(f"{rules_key}[{i}]: no pattern")
    return col


def apply_sample_rules(conn, mapping, exprs):
    """Apply the mapping's declared sample rules, and report what they did.

    position_rules: a sample's vertical position, read from a source column
      by a pattern (shared rules D1). Each rule: column, pattern (a regular
      expression), and either position (a fixed label) or a capture group
      in the pattern that gives the label; optionally top_depth,
      bottom_depth and depth_units, where the vocabulary is understood
      (they fill a sample's depth only where the source gave none).
    qc_rules: QC samples a source typed as normal. Each rule: column,
      pattern, sample_type (e.g. FR, FD) and, if the pattern captures it,
      the parent's code; require_parent (default: true when the pattern
      captures a parent) skips a match whose parent is not a sample. A rule
      only retypes samples typed normal (N) or untyped: the source's own
      sample type is authoritative.

    A sample two rules give different positions or types refuses the load
    (RuleConflictError). An unmatched sample keeps an unknown position. What
    each rule matched, retyped or skipped is written to load_report, at every
    load, so a rule that stops matching is visible.
    """
    sid = f'CAST("{exprs["sample_id_col"]}" AS VARCHAR)'
    conn.execute("DELETE FROM load_report WHERE category IN ('position_rule', 'qc_rule')")
    report = []
    n_samples = conn.execute("SELECT count(*) FROM samples").fetchone()[0]

    def source_values(col):
        return f"(SELECT DISTINCT {sid} AS sid, CAST({col} AS VARCHAR) AS val FROM staging)"

    def conflicts(table, field, what):
        rows = conn.execute(f"""SELECT sid, list(DISTINCT {field} ORDER BY {field})
            FROM {table} GROUP BY sid HAVING count(DISTINCT {field}) > 1
            ORDER BY sid LIMIT 10""").fetchall()
        if rows:
            n = conn.execute(f"""SELECT count(*) FROM (SELECT sid FROM {table} GROUP BY sid
                HAVING count(DISTINCT {field}) > 1)""").fetchone()[0]
            raise RuleConflictError(
                f"{n} sample(s) match {what} rules that disagree, e.g. "
                + "; ".join(f"{s}: {v}" for s, v in rows))

    pos_rules = mapping.get('position_rules') or []
    if pos_rules:
        parts = []
        for i, r in enumerate(pos_rules):
            col = _rule_column(exprs, 'position_rules', i, r)
            pat = _sql_text(r['pattern'])
            label = (_sql_text(r['position']) if r.get('position') is not None
                     else f"NULLIF(regexp_extract(val, {pat}, 1), '')")
            parts.append(f"""SELECT {i} AS rule, sid, CAST({label} AS VARCHAR) AS label,
                TRY_CAST({_sql_text(r.get('top_depth'))} AS DOUBLE) AS top,
                TRY_CAST({_sql_text(r.get('bottom_depth'))} AS DOUBLE) AS bottom,
                CAST({_sql_text(r.get('depth_units'))} AS VARCHAR) AS units
                FROM {source_values(col)} WHERE regexp_matches(val, {pat})""")
        conn.execute(f"CREATE OR REPLACE TEMP TABLE _pos_matches AS {' UNION ALL '.join(parts)}")
        bad = conn.execute("SELECT rule FROM _pos_matches WHERE label IS NULL LIMIT 1").fetchone()
        if bad:
            raise RuleConflictError(
                f"position_rules[{bad[0]}]: matched a sample but gives no position: name one "
                "(position:) or capture it in the pattern")
        conflicts("_pos_matches", "label", "position")
        conflicts("_pos_matches", "concat_ws('-', top, bottom, units)", "position depth")
        conn.execute("""
            UPDATE samples SET
                position = m.label,
                top_depth = COALESCE(samples.top_depth, m.top),
                bottom_depth = COALESCE(samples.bottom_depth, m.bottom),
                depth = COALESCE(samples.depth,
                                 CASE WHEN m.top IS NOT NULL THEN (m.top + COALESCE(m.bottom, m.top)) / 2 END),
                depth_units = COALESCE(samples.depth_units, m.units)
            FROM (SELECT sid, any_value(label) AS label, any_value(top) AS top,
                         any_value(bottom) AS bottom, any_value(units) AS units
                  FROM _pos_matches GROUP BY sid) m
            WHERE samples.sample_id = m.sid""")
        for i, r in enumerate(pos_rules):
            n = conn.execute("SELECT count(DISTINCT sid) FROM _pos_matches WHERE rule = ?",
                             [i]).fetchone()[0]
            report.append(('position_rule', f"{i}: {r['column']} ~ {r['pattern']}", n,
                           r.get('position') or 'captured'))
        matched = conn.execute("SELECT count(*) FROM samples WHERE position IS NOT NULL").fetchone()[0]
        report.append(('position_rule', 'no rule matched (position unknown)', n_samples - matched, None))

    qc_rules = mapping.get('qc_rules') or []
    if qc_rules:
        parts = []
        for i, r in enumerate(qc_rules):
            col = _rule_column(exprs, 'qc_rules', i, r)
            if not r.get('sample_type'):
                raise RuleConflictError(f"qc_rules[{i}]: no sample_type")
            pat = _sql_text(r['pattern'])
            try:
                captures_parent = re.compile(r['pattern']).groups > 0
            except re.error as e:
                raise RuleConflictError(f"qc_rules[{i}]: pattern {r['pattern']!r}: {e}") from None
            require = r.get('require_parent', captures_parent)
            parent = f"NULLIF(regexp_extract(val, {pat}, 1), '')" if captures_parent else "NULL"
            parts.append(f"""SELECT {i} AS rule, sid, {_sql_text(r['sample_type'])} AS stype,
                CAST({parent} AS VARCHAR) AS parent, {'TRUE' if require else 'FALSE'} AS require_parent
                FROM {source_values(col)} WHERE regexp_matches(val, {pat})""")
        conn.execute(f"CREATE OR REPLACE TEMP TABLE _qc_matches AS {' UNION ALL '.join(parts)}")
        conflicts("_qc_matches", "stype", "QC")
        conn.execute("""CREATE OR REPLACE TEMP TABLE _qc_judged AS
            SELECT m.*,
                   CASE
                       WHEN s.sample_type IS NOT NULL AND upper(trim(s.sample_type)) <> 'N'
                           THEN 'already typed by the source'
                       WHEN m.require_parent AND (m.parent IS NULL OR m.parent = m.sid
                            OR m.parent NOT IN (SELECT sample_id FROM samples))
                           THEN 'skipped: no parent sample'
                       ELSE 'retyped'
                   END AS outcome
            FROM _qc_matches m JOIN samples s ON s.sample_id = m.sid""")
        conn.execute("""
            UPDATE samples SET
                sample_type = j.stype,
                parent_sample_id = COALESCE(samples.parent_sample_id, j.parent)
            FROM (SELECT sid, any_value(stype) AS stype, any_value(parent) AS parent
                  FROM _qc_judged WHERE outcome = 'retyped' GROUP BY sid) j
            WHERE samples.sample_id = j.sid""")
        for i, r in enumerate(qc_rules):
            for outcome, n in conn.execute("""SELECT outcome, count(DISTINCT sid) FROM _qc_judged
                                              WHERE rule = ? GROUP BY 1 ORDER BY 1""", [i]).fetchall():
                report.append(('qc_rule', f"{i}: {r['column']} ~ {r['pattern']} -> "
                               f"{r['sample_type']}: {outcome}", n, None))

    if report:
        conn.executemany("INSERT OR REPLACE INTO load_report VALUES (?, ?, ?, ?)", report)
        for category, item, n, _ in report:
            print(f"  {category}: {item}: {n:,}")


def insert_sample_attributes(conn, mapping, exprs, staging_cols):
    """Insert sample-level EAV attributes from YAML config."""
    sam_attrs = mapping.get('sample_attributes', [])
    if not sam_attrs:
        return 0

    print(f"  Loading sample attributes: {', '.join(sam_attrs)}")
    for attr in sam_attrs:
        if attr in staging_cols:
            conn.execute(f"""
                INSERT INTO sample_attributes (sample_id, field_name, field_value)
                SELECT
                    CAST("{exprs['sample_id_col']}" AS VARCHAR) AS sample_id,
                    '{attr}' AS field_name,
                    CAST("{attr}" AS VARCHAR) AS field_value
                FROM staging
                WHERE "{exprs['sample_id_col']}" IS NOT NULL AND "{attr}" IS NOT NULL
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY CAST("{exprs['sample_id_col']}" AS VARCHAR) ORDER BY _src_row
                ) = 1
                ON CONFLICT (sample_id, field_name) DO NOTHING
            """)
        else:
            print(f"    Warning: attribute column '{attr}' not found in source")

    count = conn.execute("SELECT COUNT(*) FROM sample_attributes").fetchone()[0]
    print(f"  Sample attributes: {count:,}")
    return count


RESULT_COLUMNS = (
    'result_id', 'sample_id', 'analyte', 'anl_sm_mb', 'analyte_group', 'cas_number',
    'result', 'units', 'std_units', 'method_detection_limit', 'reporting_limit',
    'quantitation_limit', 'upper_quantitation_limit', 'dilution_factor',
    'modeled_relative_uncertainty', 'detected', 'estimated', 'tic',
    'qualifier', 'lab_name', 'lab_sample_id', 'method_code',
    'date_analyzed', 'lab_package_id', 'validation_level', 'lab_flags', 'validator_flags',
    'meas_basis', 'fraction', 'method_class', 'reportable', 'test_type', 'test_rank',
    'report_limit', 'nd_limit', 'limit_source', 'reported_units', 'unit_factor', 'unit_source',
)


def results_select_sql(mapping, exprs, result_id_expr, source='staging',
                       extra_columns=()):
    """The SELECT that turns source rows into ChemDuck `results` rows.

    One definition for both outputs of a mapping: insert_results runs it
    against the staging table, and the hybrid ARII source (arii_hybrid.py)
    makes it the body of a `results` view over the source parquet. Any rule
    added here applies to both, which is what keeps them comparable.

    result_id_expr  SQL for result_id; the loader uses offset + _src_row.
    source          the relation rows come from.
    extra_columns   (name, sql) pairs appended after the ChemDuck columns.

    Every column is cast to its `results` type. Inserting into the table
    casts anyway, but a view keeps the expression's own type, and a mapping
    that sets a column to NULL (meas_basis for ARII) would otherwise give
    the view an INTEGER column where ChemDuck expects VARCHAR - which then
    fails in COALESCE(meas_basis, '') downstream.

    Units (shared rules D5): where plan_units found a factor to the
    variable's canonical unit (exprs['unit_lookup']), result, units and
    every limit are converted here, once, and the row records the unit it
    was reported in, the factor and its source.
    """
    result_map = mapping.get('results', {})
    col_or_null = exprs['col_or_null']
    extras = ''.join(f',\n            {sql} AS {name}' for name, sql in extra_columns)
    units_ref = col_or_null(result_map.get('units'))
    std_units_ref = col_or_null(result_map.get('std_units'))
    lookup = exprs.get('unit_lookup')
    if lookup:
        source = f"(SELECT *, {lookup} AS _chemduck_unit FROM {source}) AS _src"
        scale = " * COALESCE(_chemduck_unit.f, 1.0)"
        units_sql = (f"CASE WHEN _chemduck_unit.f IS NOT NULL THEN _chemduck_unit.u "
                     f"ELSE CAST({units_ref} AS VARCHAR) END")
        std_units_sql = (f"CASE WHEN _chemduck_unit.f IS NOT NULL THEN _chemduck_unit.u "
                         f"ELSE CAST({std_units_ref} AS VARCHAR) END")
        factor_sql, unit_source_sql = "_chemduck_unit.f", "_chemduck_unit.s"
    else:
        scale = ""
        units_sql = f"CAST({units_ref} AS VARCHAR)"
        std_units_sql = f"CAST({std_units_ref} AS VARCHAR)"
        factor_sql, unit_source_sql = "NULL", "NULL"
    unit_source_sql = (f"COALESCE({unit_source_sql}, CASE WHEN NULLIF(TRIM(CAST({units_ref} "
                       f"AS VARCHAR)), '') IS NOT NULL THEN 'as_reported' END)")
    return f"""
        SELECT
            {result_id_expr} AS result_id,
            CAST("{exprs['sample_id_col']}" AS VARCHAR) AS sample_id,
            CAST({col_or_null(exprs['analyte_col'])} AS VARCHAR) AS analyte,
            CAST({col_or_null(result_map.get('anl_sm_mb'))} AS VARCHAR) AS anl_sm_mb,
            CAST({col_or_null(result_map.get('analyte_group'))} AS VARCHAR) AS analyte_group,
            CAST({col_or_null(result_map.get('cas_number'))} AS VARCHAR) AS cas_number,
            TRY_CAST({col_or_null(exprs['result_col'])} AS DOUBLE){scale} AS result,
            {units_sql} AS units,
            {std_units_sql} AS std_units,
            TRY_CAST({col_or_null(result_map.get('method_detection_limit'))} AS DOUBLE){scale} AS method_detection_limit,
            TRY_CAST({col_or_null(result_map.get('reporting_limit'))} AS DOUBLE){scale} AS reporting_limit,
            TRY_CAST({col_or_null(result_map.get('quantitation_limit'))} AS DOUBLE){scale} AS quantitation_limit,
            TRY_CAST({col_or_null(result_map.get('upper_quantitation_limit'))} AS DOUBLE){scale} AS upper_quantitation_limit,
            TRY_CAST({col_or_null(result_map.get('dilution_factor'))} AS DOUBLE) AS dilution_factor,
            TRY_CAST({col_or_null(result_map.get('modeled_relative_uncertainty'))} AS DOUBLE) AS modeled_relative_uncertainty,
            CAST({exprs['detect_expr']} AS BOOLEAN) AS detected,
            CAST({exprs['estimated_expr']} AS BOOLEAN) AS estimated,
            CAST({exprs['tic_expr']} AS BOOLEAN) AS tic,
            CAST({col_or_null(exprs['qualifier_col'])} AS VARCHAR) AS qualifier,
            CAST({col_or_null(result_map.get('lab_name'))} AS VARCHAR) AS lab_name,
            CAST({col_or_null(result_map.get('lab_sample_id'))} AS VARCHAR) AS lab_sample_id,
            CAST({col_or_null(result_map.get('method_code'))} AS VARCHAR) AS method_code,
            CAST({exprs['date_analyzed_expr']} AS DATE) AS date_analyzed,
            CAST({col_or_null(result_map.get('lab_package_id'))} AS VARCHAR) AS lab_package_id,
            CAST({col_or_null(result_map.get('validation_level'))} AS VARCHAR) AS validation_level,
            CAST({col_or_null(result_map.get('lab_flags'))} AS VARCHAR) AS lab_flags,
            CAST({col_or_null(result_map.get('validator_flags'))} AS VARCHAR) AS validator_flags,
            CAST({exprs['meas_basis_expr']} AS VARCHAR) AS meas_basis,
            CAST({exprs['fraction_expr']} AS VARCHAR) AS fraction,
            CAST({exprs['method_class_expr']} AS VARCHAR) AS method_class,
            CAST({exprs['reportable_expr']} AS BOOLEAN) AS reportable,
            CAST({exprs['test_type_expr']} AS VARCHAR) AS test_type,
            CAST({exprs['test_rank_expr']} AS INTEGER) AS test_rank,
            CAST({exprs['report_limit_expr']} AS DOUBLE){scale} AS report_limit,
            CAST({exprs['nd_limit_expr']} AS DOUBLE){scale} AS nd_limit,
            CAST({exprs['limit_source_expr']} AS VARCHAR) AS limit_source,
            CAST({units_ref} AS VARCHAR) AS reported_units,
            CAST({factor_sql} AS DOUBLE) AS unit_factor,
            CAST({unit_source_sql} AS VARCHAR) AS unit_source{extras}
        FROM {source}
        WHERE "{exprs['sample_id_col']}" IS NOT NULL
          AND {col_or_null(exprs['analyte_col'])} IS NOT NULL
    """


def insert_results(conn, mapping, exprs, staging_cols):
    """Insert results from staging table.

    result_id = offset + source row number, so the same file always loads to
    the same ids, and result attributes join back by id rather than by
    (sample_id, analyte), which fans out when one sample has several methods.
    """
    offset = conn.execute("SELECT COALESCE(MAX(result_id), 0) FROM results").fetchone()[0]
    exprs['result_id_offset'] = offset

    select = results_select_sql(
        mapping, exprs, f"CAST({offset} + _src_row AS INTEGER)")
    conn.execute(f"INSERT INTO results ({', '.join(RESULT_COLUMNS)}) {select}")

    result_count = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    print(f"  Results: {result_count:,}")
    return result_count


def insert_result_attributes(conn, mapping, exprs, staging_cols):
    """Insert result-level EAV attributes from YAML config."""
    res_attrs = mapping.get('result_attributes', [])
    if not res_attrs:
        return 0

    print(f"  Loading result attributes: {', '.join(res_attrs)}")
    for attr in res_attrs:
        if attr in staging_cols:
            conn.execute(f"""
                INSERT INTO result_attributes (result_id, field_name, field_value)
                SELECT
                    r.result_id,
                    '{attr}' AS field_name,
                    CAST(s."{attr}" AS VARCHAR) AS field_value
                FROM staging s
                JOIN results r ON r.result_id = {exprs['result_id_offset']} + s._src_row
                WHERE s."{attr}" IS NOT NULL
                ON CONFLICT (result_id, field_name) DO NOTHING
            """)
        else:
            print(f"    Warning: attribute column '{attr}' not found in source")

    count = conn.execute("SELECT COUNT(*) FROM result_attributes").fetchone()[0]
    print(f"  Result attributes: {count:,}")
    return count


def insert_river_miles(conn, mapping, exprs, staging_cols):
    """Insert river mile data if configured in YAML."""
    river_cfg = mapping.get('river', {})
    if not river_cfg.get('name') or not river_cfg.get('river_mile_column'):
        return

    river_name = river_cfg['name']
    rm_col = river_cfg['river_mile_column']
    bank_col = river_cfg.get('bank_side_column')

    if rm_col not in staging_cols:
        print(f"  Warning: river_mile_column '{rm_col}' not found in source")
        return

    print(f"\n--- Loading river mile data ({river_name}) ---")

    conn.execute(f"""
        INSERT INTO rivers (river_id, river_name, active)
        VALUES (1, '{river_name}', TRUE)
        ON CONFLICT DO NOTHING
    """)

    bank_expr = f'"{bank_col}"' if bank_col and bank_col in staging_cols else "NULL"
    conn.execute(f"""
        INSERT INTO location_river_miles (location_id, river_id, river_mile, bank_side)
        SELECT DISTINCT
            CAST("{exprs['loc_id_col']}" AS VARCHAR) AS location_id,
            1 AS river_id,
            TRY_CAST("{rm_col}" AS DOUBLE) AS river_mile,
            {bank_expr} AS bank_side
        FROM staging
        WHERE "{exprs['loc_id_col']}" IS NOT NULL
          AND "{rm_col}" IS NOT NULL
          AND TRY_CAST("{rm_col}" AS DOUBLE) IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY CAST("{exprs['loc_id_col']}" AS VARCHAR) ORDER BY CAST("{exprs['loc_id_col']}" AS VARCHAR)
        ) = 1
        ON CONFLICT (location_id, river_id) DO NOTHING
    """)

    rm_count = conn.execute("SELECT COUNT(*) FROM location_river_miles").fetchone()[0]
    print(f"  River mile mappings: {rm_count:,}")


# ============================================================================
# Heuristic EAV classification
# ============================================================================

def classify_unmapped_fields(conn, staging_cols, mapping, exprs):
    """
    Heuristic classification of unmapped source columns into EAV tables.

    For each unmapped column:
      - Constant per location -> location_attributes
      - Constant per sample -> sample_attributes
      - Otherwise -> result_attributes
    """
    # Collect all mapped/skip columns
    mapped_cols = set()
    for section in ['locations', 'samples', 'results']:
        for target, source in mapping.get(section, {}).items():
            if source and isinstance(source, str):
                mapped_cols.add(source)
    for attr_key in ['location_attributes', 'sample_attributes', 'result_attributes']:
        for attr in mapping.get(attr_key, []):
            mapped_cols.add(attr)
    river_cfg = mapping.get('river', {})
    for key in ['river_mile_column', 'bank_side_column']:
        if river_cfg.get(key):
            mapped_cols.add(river_cfg[key])
    for col in mapping.get('unmapped', []):
        mapped_cols.add(col)

    unmapped = [c for c in staging_cols if c not in mapped_cols]
    if not unmapped:
        return {}

    loc_id_col = exprs['loc_id_col']
    sample_id_col = exprs['sample_id_col']

    classification = {}

    for col in unmapped:
        # Skip all-NULL columns
        non_null = conn.execute(
            f'SELECT COUNT(*) FROM staging WHERE "{col}" IS NOT NULL'
        ).fetchone()[0]
        if non_null == 0:
            continue

        # Constant per location?
        loc_card = conn.execute(f"""
            SELECT MAX(n_distinct) FROM (
                SELECT COUNT(DISTINCT CAST("{col}" AS VARCHAR)) AS n_distinct
                FROM staging
                WHERE "{col}" IS NOT NULL AND "{loc_id_col}" IS NOT NULL
                GROUP BY "{loc_id_col}"
            )
        """).fetchone()[0]

        if loc_card is not None and loc_card <= 1:
            classification[col] = 'location'
            continue

        # Constant per sample?
        sample_card = conn.execute(f"""
            SELECT MAX(n_distinct) FROM (
                SELECT COUNT(DISTINCT CAST("{col}" AS VARCHAR)) AS n_distinct
                FROM staging
                WHERE "{col}" IS NOT NULL AND "{sample_id_col}" IS NOT NULL
                GROUP BY "{sample_id_col}"
            )
        """).fetchone()[0]

        if sample_card is not None and sample_card <= 1:
            classification[col] = 'sample'
            continue

        classification[col] = 'result'

    return classification


def auto_insert_classified_attributes(conn, classification, exprs, staging_cols):
    """Insert auto-classified unmapped columns into appropriate EAV tables."""
    counts = {'location': 0, 'sample': 0, 'result': 0}

    for col, level in classification.items():
        if level == 'location':
            conn.execute(f"""
                INSERT INTO location_attributes (location_id, field_name, field_value)
                SELECT
                    CAST("{exprs['loc_id_col']}" AS VARCHAR),
                    '{col}',
                    CAST("{col}" AS VARCHAR)
                FROM staging
                WHERE "{exprs['loc_id_col']}" IS NOT NULL AND "{col}" IS NOT NULL
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY CAST("{exprs['loc_id_col']}" AS VARCHAR) ORDER BY _src_row
                ) = 1
                ON CONFLICT (location_id, field_name) DO NOTHING
            """)
            counts['location'] += 1

        elif level == 'sample':
            conn.execute(f"""
                INSERT INTO sample_attributes (sample_id, field_name, field_value)
                SELECT
                    CAST("{exprs['sample_id_col']}" AS VARCHAR),
                    '{col}',
                    CAST("{col}" AS VARCHAR)
                FROM staging
                WHERE "{exprs['sample_id_col']}" IS NOT NULL AND "{col}" IS NOT NULL
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY CAST("{exprs['sample_id_col']}" AS VARCHAR) ORDER BY _src_row
                ) = 1
                ON CONFLICT (sample_id, field_name) DO NOTHING
            """)
            counts['sample'] += 1

        elif level == 'result':
            conn.execute(f"""
                INSERT INTO result_attributes (result_id, field_name, field_value)
                SELECT
                    r.result_id,
                    '{col}',
                    CAST(s."{col}" AS VARCHAR)
                FROM staging s
                JOIN results r ON r.result_id = {exprs['result_id_offset']} + s._src_row
                WHERE s."{col}" IS NOT NULL
                ON CONFLICT (result_id, field_name) DO NOTHING
            """)
            counts['result'] += 1

    return counts


# ============================================================================
# Post-load: indexes, verification, summary
# ============================================================================

def create_indexes(conn):
    """Create performance indexes."""
    print("\n--- Creating indexes ---")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_location ON samples(location_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_date ON samples(sample_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_results_sample ON results(sample_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_results_analyte ON results(analyte)")
    print("  Indexes created")


def verify_data(conn):
    """Verify data integrity after load."""
    print("\n--- Verifying data ---")

    orphan_samples = conn.execute("""
        SELECT COUNT(*) FROM samples s
        WHERE NOT EXISTS (SELECT 1 FROM locations l WHERE l.location_id = s.location_id)
    """).fetchone()[0]
    if orphan_samples > 0:
        print(f"  Warning: {orphan_samples} samples reference non-existent locations")

    orphan_results = conn.execute("""
        SELECT COUNT(*) FROM results r
        WHERE NOT EXISTS (SELECT 1 FROM samples s WHERE s.sample_id = r.sample_id)
    """).fetchone()[0]
    if orphan_results > 0:
        print(f"  Warning: {orphan_results} results reference non-existent samples")

    try:
        view_count = conn.execute("SELECT COUNT(*) FROM v_results_denormalized").fetchone()[0]
        print(f"  v_results_denormalized: {view_count:,} rows")
    except Exception as e:
        print(f"  Warning: Could not query v_results_denormalized: {e}")

    return orphan_samples, orphan_results


def print_summary(conn):
    """Print summary statistics."""
    print("\n--- Summary Statistics ---")

    loc_count = conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0]
    sample_count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    result_count = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    print(f"  Locations: {loc_count:,}  Samples: {sample_count:,}  Results: {result_count:,}")

    date_range = conn.execute("SELECT MIN(sample_date), MAX(sample_date) FROM samples").fetchone()
    if date_range[0]:
        print(f"  Date range: {date_range[0]} to {date_range[1]}")

    matrices = conn.execute("""
        SELECT matrix, COUNT(*) AS n FROM samples GROUP BY matrix ORDER BY n DESC LIMIT 5
    """).fetchall()
    if matrices:
        print(f"  Top matrices: {', '.join(f'{m[0]}({m[1]})' for m in matrices)}")

    groups = conn.execute("""
        SELECT analyte_group, COUNT(*) AS n FROM results
        WHERE analyte_group IS NOT NULL
        GROUP BY analyte_group ORDER BY n DESC LIMIT 5
    """).fetchall()
    if groups:
        print(f"  Top analyte groups: {', '.join(f'{g[0]}({g[1]})' for g in groups)}")

    analyte_count = conn.execute("SELECT COUNT(DISTINCT analyte) FROM results").fetchone()[0]
    print(f"  Unique analytes: {analyte_count:,}")

    # EAV stats
    for table_name in ['location_attributes', 'sample_attributes', 'result_attributes']:
        count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        if count > 0:
            fields = conn.execute(
                f"SELECT COUNT(DISTINCT field_name) FROM {table_name}"
            ).fetchone()[0]
            print(f"  {table_name}: {count:,} rows ({fields} fields)")


# ============================================================================
# Main orchestrators
# ============================================================================

def _run_load(conn, mapping, auto_classify=True, allow_conflicts=False):
    """Every load-time rule over the staging table, into conn's current
    schema: the mapping, sample rules, method classes, limit chain, unit
    plan and load_report, then the tables, EAV attributes and indexes."""
    staging_cols = get_staging_cols(conn)

    print_mapping_report(staging_cols, mapping)

    exprs = build_sql_exprs(mapping, staging_cols)

    policy = record_nd_policy(conn, mapping)
    print(f"\n  Non-detect policy: {policy[0]}@{policy[1]}")
    exprs['method_class_expr'] = method_class_sql(
        classify_methods(conn, mapping, exprs),
        exprs['col_or_null'](mapping.get('results', {}).get('method_code')))
    record_nd_multiplier(conn, exprs)
    record_scope(conn, mapping, exprs)
    exprs['unit_lookup'] = unit_lookup_sql(plan_units(conn, mapping, exprs), exprs, mapping)

    print("\n--- Loading data ---")
    insert_locations(conn, mapping, exprs, staging_cols, allow_conflicts)
    insert_location_attributes(conn, mapping, exprs, staging_cols)
    insert_samples(conn, mapping, exprs, staging_cols, allow_conflicts)
    apply_sample_rules(conn, mapping, exprs)
    insert_sample_attributes(conn, mapping, exprs, staging_cols)
    insert_results(conn, mapping, exprs, staging_cols)
    insert_result_attributes(conn, mapping, exprs, staging_cols)
    insert_river_miles(conn, mapping, exprs, staging_cols)

    # Auto-classify unmapped fields
    if auto_classify:
        print("\n--- Auto-classifying unmapped fields ---")
        classification = classify_unmapped_fields(conn, staging_cols, mapping, exprs)
        if classification:
            print("  Classification results:")
            for col, level in sorted(classification.items()):
                print(f"    {col:35s} -> {level}_attributes")
            counts = auto_insert_classified_attributes(conn, classification, exprs, staging_cols)
            print(f"  Inserted: {counts['location']} location, "
                  f"{counts['sample']} sample, {counts['result']} result attributes")
        else:
            print("  No unmapped columns to classify")

    conn.execute("DROP TABLE IF EXISTS staging")

    create_indexes(conn)

    # Refresh EAV views
    print("\n--- Refreshing EAV views ---")
    from .eav_helpers import refresh_eav_views
    eav_stats = refresh_eav_views(conn)
    print(f"  EAV fields: {eav_stats}")

    verify_data(conn)
    print_summary(conn)


def load_to_duckdb(source_path, db_path, mapping, env=None, limit=None,
                   auto_classify=True, allow_conflicts=False):
    """Load data into a DuckDB file."""
    conn = duckdb.connect(str(db_path), config=STORAGE_CONFIG)

    print("\n--- Loading schema ---")
    load_schema(conn, SQL_DIR)

    print("\n--- Creating staging table ---")
    total_rows = create_staging_table(conn, source_path, mapping, env=env, limit=limit)

    _run_load(conn, mapping, auto_classify, allow_conflicts)

    conn.close()
    print(f"\n  Output: {db_path}")

    return {
        'total_rows': total_rows,
    }


# A source's format by its file name; anything else is the mapping's.
SOURCE_SUFFIXES = {'.csv': 'csv', '.txt': 'csv', '.parquet': 'parquet', '.json': 'parquet',
                   '.xlsx': 'xlsx'}


def load_source(con, source, mapping, schema='chemduck', limit=None,
                auto_classify=True, allow_conflicts=False):
    """Load source rows into schema `schema` of an open connection.

    For a host that holds its rows itself - from an API, a report's stored
    output, its own cache - with no published sidecars. The same mapping and
    load-time rules as load_to_duckdb (sample rules, method classes, limit
    chain, unit plan, load_report), the same refusals (a missing detect flag,
    conflicting ids), and the same install: ChemDuck's object registry, and
    a refusal, before anything is created, to replace an object of the
    host's (SchemaConflictError; install into a schema of ChemDuck's own).

    source: a path to CSV, Parquet, a Parquet manifest.json or XLSX (by
    suffix; else the mapping's source.format), or rows in memory - a DuckDB
    relation from any connection, a pandas DataFrame or a pyarrow table.
    schema: where ChemDuck goes; None is con's current schema. A schema that
    already holds a load is refused: a load is never merged into another.

    Call the macros through schema_cursor(con, schema). Returns
    {'total_rows', 'schema'}.
    """
    load_schema(con, SQL_DIR, schema=schema)
    cur = schema_cursor(con, schema)
    try:
        held = cur.execute("SELECT count(*) FROM results").fetchone()[0]
        if held:
            raise LoadConflictError(
                f"Schema {cur.execute('SELECT current_schema()').fetchone()[0]!r} already "
                f"holds a load ({held:,} results); load into a schema of its own, or drop "
                "this one first")

        print("\n--- Creating staging table ---")
        if isinstance(source, (str, Path)):
            fmt = SOURCE_SUFFIXES.get(Path(source).suffix.lower())
            if fmt:
                mapping = {**mapping, 'source': {**mapping.get('source', {}), 'format': fmt}}
            total_rows = create_staging_table(cur, source, mapping, limit=limit)
        else:
            # A relation belongs to its connection; its Arrow stream reads anywhere.
            rows = source.arrow() if isinstance(source, duckdb.DuckDBPyRelation) else source
            cur.register('_chemduck_source', rows)
            try:
                cur.execute(f"""CREATE TEMP TABLE staging AS SELECT * FROM _chemduck_source
                                {f'LIMIT {int(limit)}' if limit else ''}""")
            finally:
                cur.unregister('_chemduck_source')
            total_rows = _number_staging(cur)

        _run_load(cur, mapping, auto_classify, allow_conflicts)
    finally:
        cur.execute("DROP TABLE IF EXISTS staging")
        if cur is not con:
            cur.close()
    return {'total_rows': total_rows, 'schema': schema}


def export_to_parquet(source_path, output_dir, mapping, env=None, limit=None,
                      auto_classify=True, allow_conflicts=False):
    """Export data to Parquet files."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(":memory:")

    print("\n--- Creating staging table ---")
    total_rows = create_staging_table(conn, source_path, mapping, env=env, limit=limit)

    staging_cols = get_staging_cols(conn)

    print_mapping_report(staging_cols, mapping)

    exprs = build_sql_exprs(mapping, staging_cols)

    # Create tables in memory (no schema load needed, just create the tables)
    load_schema(conn, SQL_DIR)
    exprs['method_class_expr'] = method_class_sql(
        classify_methods(conn, mapping, exprs),
        exprs['col_or_null'](mapping.get('results', {}).get('method_code')))
    record_scope(conn, mapping, exprs)
    exprs['unit_lookup'] = unit_lookup_sql(plan_units(conn, mapping, exprs), exprs, mapping)

    print("\n--- Loading data ---")
    insert_locations(conn, mapping, exprs, staging_cols, allow_conflicts)
    insert_location_attributes(conn, mapping, exprs, staging_cols)
    insert_samples(conn, mapping, exprs, staging_cols, allow_conflicts)
    apply_sample_rules(conn, mapping, exprs)
    insert_sample_attributes(conn, mapping, exprs, staging_cols)
    insert_results(conn, mapping, exprs, staging_cols)
    insert_result_attributes(conn, mapping, exprs, staging_cols)
    insert_river_miles(conn, mapping, exprs, staging_cols)

    if auto_classify:
        print("\n--- Auto-classifying unmapped fields ---")
        classification = classify_unmapped_fields(conn, staging_cols, mapping, exprs)
        if classification:
            for col, level in sorted(classification.items()):
                print(f"    {col:35s} -> {level}_attributes")
            auto_insert_classified_attributes(conn, classification, exprs, staging_cols)

    conn.execute("DROP TABLE IF EXISTS staging")

    # Write Parquet files
    print("\n--- Writing Parquet files ---")
    parquet_tables = [
        ('locations', 'locations.parquet'),
        ('samples', 'samples.parquet'),
        ('results', 'results.parquet'),
        ('location_attributes', 'location_attributes.parquet'),
        ('sample_attributes', 'sample_attributes.parquet'),
        ('result_attributes', 'result_attributes.parquet'),
    ]

    for table, filename in parquet_tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if count > 0:
            path = output_dir / filename
            conn.execute(f"COPY {table} TO '{path}' (FORMAT PARQUET, COMPRESSION ZSTD)")
            size_kb = path.stat().st_size / 1024
            print(f"  {filename}: {count:,} rows ({size_kb:.1f} KB)")

    # River miles if present
    rm_count = conn.execute("SELECT COUNT(*) FROM location_river_miles").fetchone()[0]
    if rm_count > 0:
        for table in ['rivers', 'location_river_miles']:
            path = output_dir / f"{table}.parquet"
            conn.execute(f"COPY {table} TO '{path}' (FORMAT PARQUET, COMPRESSION ZSTD)")
            print(f"  {table}.parquet: exported")

    print_summary(conn)
    conn.close()
    print(f"\n  Output directory: {output_dir}")


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="ChemDuck Unified Data Loader - loads denormalized chemistry data "
                    "into the ChemDuck DuckDB schema"
    )
    parser.add_argument(
        "input", type=Path, nargs='?', default=None,
        help="Input data file (CSV, XLSX) or ignored for PostgreSQL"
    )
    parser.add_argument(
        "output", type=Path, nargs='?', default=None,
        help="Output DuckDB file or Parquet directory"
    )
    parser.add_argument(
        "--mapping", type=Path, required=True,
        help="Path to field mapping YAML file"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Limit number of rows to load (for testing)"
    )
    parser.add_argument(
        "--parquet", action="store_true",
        help="Export to Parquet files instead of DuckDB"
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Overwrite existing output file/directory"
    )
    parser.add_argument(
        "--env", type=Path, default=None,
        help="Path to .env file (for PostgreSQL connections)"
    )
    parser.add_argument(
        "--no-auto-classify", action="store_false", dest="auto_classify",
        default=True,
        help="Disable auto-classification of unmapped source columns into EAV attribute tables"
    )
    parser.add_argument(
        "--allow-conflicts", action="store_true",
        help="Load even when one sample/location id carries conflicting values "
             "across source rows (keeps the first row in file order)"
    )

    args = parser.parse_args()

    if not args.mapping.exists():
        print(f"Error: mapping file not found: {args.mapping}")
        sys.exit(1)

    mapping = load_mapping(args.mapping)
    fmt = mapping.get('source', {}).get('format', 'csv')

    # Validate input
    if fmt in ('csv', 'xlsx', 'parquet'):
        if args.input is None:
            print(f"Error: input file required for {fmt} format")
            sys.exit(1)
        if not args.input.exists():
            print(f"Error: input file not found: {args.input}")
            sys.exit(1)

    if args.output is None:
        print("Error: output path required")
        sys.exit(1)

    # Handle overwrite
    if not args.parquet and args.output.exists() and not args.overwrite:
        print(f"Error: output file exists: {args.output} (use --overwrite)")
        sys.exit(1)
    if not args.parquet and args.output.exists() and args.overwrite:
        args.output.unlink()

    env = load_env(args.env) if fmt == 'postgresql' else None

    print("=" * 60)
    print(f"ChemDuck Unified Loader")
    print(f"  Format: {fmt}")
    print(f"  Mapping: {args.mapping}")
    if args.input:
        print(f"  Input: {args.input}")
    print(f"  Output: {args.output}")
    print("=" * 60)

    if args.parquet:
        export_to_parquet(
            args.input, args.output, mapping,
            env=env, limit=args.limit, auto_classify=args.auto_classify,
            allow_conflicts=args.allow_conflicts
        )
    else:
        load_to_duckdb(
            args.input, args.output, mapping,
            env=env, limit=args.limit, auto_classify=args.auto_classify,
            allow_conflicts=args.allow_conflicts
        )

    print("\n" + "=" * 60)
    print("Load complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
