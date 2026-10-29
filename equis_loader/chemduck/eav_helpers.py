"""
EAV view refresh helpers for ChemDuck.

Regenerates dynamic views that pivot EAV (Entity-Attribute-Value) tables
into wide columnar format using DuckDB's PIVOT syntax.

Call refresh_eav_views(conn) after any data load that may introduce new
field_names.
"""

import duckdb


def _get_field_names(conn, table):
    """Get distinct field_names from an EAV table."""
    rows = conn.execute(
        f"SELECT DISTINCT field_name FROM {table} ORDER BY field_name"
    ).fetchall()
    return [r[0] for r in rows]


def _sanitize_col_name(field_name):
    """Convert a field_name to a safe SQL column alias (lowercase, underscores)."""
    safe = field_name.lower().replace(' ', '_').replace('-', '_')
    safe = ''.join(c for c in safe if c.isalnum() or c == '_')
    return safe


def _pivot_view_sql(view_name, table, id_col, id_type, prefix, field_names):
    """Build CREATE VIEW SQL using PIVOT for an EAV table."""
    if not field_names:
        return (
            f"CREATE OR REPLACE VIEW {view_name} AS\n"
            f"SELECT NULL::{id_type} AS {id_col} WHERE false"
        )

    in_list = ', '.join(f"'{fn}'" for fn in field_names)
    aliases = ', '.join(
        f'"{fn}" AS "{prefix}{_sanitize_col_name(fn)}"'
        for fn in field_names
    )
    return (
        f"CREATE OR REPLACE VIEW {view_name} AS\n"
        f"SELECT {id_col}, {aliases}\n"
        f"FROM (\n"
        f"    PIVOT {table} ON field_name IN ({in_list})\n"
        f"    USING MAX(field_value) GROUP BY {id_col}\n"
        f")"
    )


def refresh_eav_views(conn):
    """
    Regenerate dynamic EAV pivot views.

    Creates/replaces:
      - v_location_attributes_wide
      - v_sample_attributes_wide
      - v_result_attributes_wide
      - v_results_denormalized_full (base view + all attribute columns)

    Returns dict with counts of field_names per table.
    """
    stats = {}

    # --- Wide views via PIVOT ---
    eav_config = [
        ('location_attributes', 'v_location_attributes_wide',
         'location_id', 'VARCHAR', 'la_'),
        ('sample_attributes', 'v_sample_attributes_wide',
         'sample_id', 'VARCHAR', 'sa_'),
        ('result_attributes', 'v_result_attributes_wide',
         'result_id', 'INTEGER', 'ra_'),
    ]

    all_fields = {}
    for table, view, id_col, id_type, prefix in eav_config:
        fields = _get_field_names(conn, table)
        all_fields[table] = fields
        stats[f'{table.replace("_attributes", "_attribute")}_fields'] = len(fields)
        conn.execute(_pivot_view_sql(view, table, id_col, id_type, prefix, fields))

    # --- Full denormalized view with all attributes ---
    loc_fields = all_fields['location_attributes']
    sam_fields = all_fields['sample_attributes']
    res_fields = all_fields['result_attributes']

    joins = []
    excludes = []
    if loc_fields:
        joins.append(
            'LEFT JOIN v_location_attributes_wide law '
            'ON d.location_id = law.location_id'
        )
        excludes.append('law.* EXCLUDE(location_id)')
    if sam_fields:
        joins.append(
            'LEFT JOIN v_sample_attributes_wide saw '
            'ON d.sample_id = saw.sample_id'
        )
        excludes.append('saw.* EXCLUDE(sample_id)')
    if res_fields:
        joins.append(
            'LEFT JOIN v_result_attributes_wide raw '
            'ON d.result_id = raw.result_id'
        )
        excludes.append('raw.* EXCLUDE(result_id)')

    extra_select = (', ' + ', '.join(excludes)) if excludes else ''
    join_sql = '\n        '.join(joins)

    conn.execute(f"""
        CREATE OR REPLACE VIEW v_results_denormalized_full AS
        SELECT d.*{extra_select}
        FROM v_results_denormalized d
        {join_sql}
    """)

    return stats
