"""ChemDuck: environmental chemistry in DuckDB, with the domain rules in SQL.

The package carries its SQL (chemduck/sql) and needs no DuckDB extension.
Copy the chemduck/ directory into a host (a QGIS plugin) or pip-install the
repository; either way this is the API, stable within a minor version while
the version is 0.x:

  Opening ChemDuck beside a host's own objects
    open_consumer(con, folder, partition_files, schema=None)
    load_consumer_sql(con, sql_dir, schema=None, adopt=())
    load_schema(con, sql_dir=SQL_DIR, schema=None)
    load_spatial(con, schema=None)           optional; needs spatial
    schema_cursor(con, schema)               call macros through this
    SchemaConflictError

  Publishing an EQuIS ARII facility for consumers
    publish(manifest, out_dir, mapping_path=DEFAULT_MAPPING, x_col=, y_col=)
    write_consumer_bundle(out_dir)
    SourceRefused

  Loading a .duckdb file, or rows into a host's connection
    load_to_duckdb(source, db_path, mapping)
    load_source(con, source, mapping, schema='chemduck')   a path, or rows
                                             in memory (relation, DataFrame)
    load_mapping(path)
    DetectFlagError, LoadConflictError, ManifestError, PolicyError

  Non-detect policies (docs/shared-rules-decisions-response.md, D7)
    aggregate_points(analyte, p_event_agg=, p_nd_policy=, ...)   SQL macro
    rollup_points(analyte, rollup, ...)   one value per location, by a named rule
    a mapping's position_rules: / qc_rules: (RuleConflictError), reported
    in load_report
    a mapping's `nd_policy:` names the dataset's; the loader records it

  SQL_DIR, DEFAULT_MAPPING, __version__

Anything else in chemduck.load_data or chemduck.arii_hybrid is internal.
Tested DuckDB versions are listed in SUPPORTED_DUCKDB (see tests/run_all.py).
"""

__version__ = "0.2.0"

# The Python DuckDB versions the test suite has passed on. 1.5.4 is the one
# inside QGIS 4.2.1's Python.
SUPPORTED_DUCKDB = ("1.5.4", "1.5.5")

from .arii_hybrid import (  # noqa: E402
    DEFAULT_MAPPING,
    SourceRefused,
    load_consumer_sql,
    open_consumer,
    publish,
    write_consumer_bundle,
)
from .load_data import (  # noqa: E402
    SQL_DIR,
    DetectFlagError,
    LoadConflictError,
    ManifestError,
    PolicyError,
    RuleConflictError,
    SchemaConflictError,
    load_mapping,
    load_schema,
    load_spatial,
    load_source,
    load_to_duckdb,
    schema_cursor,
)

__all__ = [
    "__version__", "SUPPORTED_DUCKDB", "SQL_DIR", "DEFAULT_MAPPING",
    "open_consumer", "load_consumer_sql", "load_schema", "load_spatial", "schema_cursor",
    "publish", "write_consumer_bundle", "load_to_duckdb", "load_source", "load_mapping",
    "SchemaConflictError", "SourceRefused", "DetectFlagError", "LoadConflictError",
    "ManifestError", "PolicyError", "RuleConflictError",
]
