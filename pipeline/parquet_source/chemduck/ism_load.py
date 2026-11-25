"""ISM records at load (docs/ism-and-uncertainty-spec.md §3-5).

Called once a load has its locations, samples and results (load_data._run_load,
arii_hybrid.publish). Everything it does is declared in a PROJECT's mapping -
the shipped mappings carry none of these sections - and reported in
load_report (category 'ism'):

  ism_settings          thresholds for the checks (chemduck_settings), e.g.
                        {ism_min_increments: 30, ism_replicate_rsd_target: 0.35}
  locations.parent_location_id   a source column naming an SU's DU
  geometries            DU/SU polygons: {path: <.geojson | .csv | .parquet>,
                        id_property / columns, area_units, ...}
  field_design          the field records of composite and ISM samples:
                        {composite_desc: COL, sampling_technique: COL} from
                        the source, and/or {path: <.csv>, key:, columns:}
  design_defaults       [{column, pattern, values: {design_increments: 30, ...}}]
  composite_desc_rules  [{pattern, field, value?}] read from COMPOSITE_DESC
  support_rules         [{column, pattern, support}] or [{field, min?, max?, support}]
  replicate_rules       [{column, pattern, set_group: 1, number_group: 2, retype: true}]
  increments            {path: <.csv | .parquet | .geojson>, columns:, where:}
  increment_locations   {loc_type: [...]}: source locations that are increments
  preparation           the source's per-test fields: {prep_method:, prep_date:,
                        subsample_amount:, subsample_amount_unit:, percent_moisture:}
  prep_method_rules     [{pattern, role: ism_processing | extraction}]
  analysis_groups       [{pattern, group, column: method_code | method_class}]
  vocabulary_rules      {<vocabulary>: {<source word>: <code>, ...}, ...}
  prep_supplement       {path: <.csv>, columns: {<target>: <heading>}}
  uncertainty_components  {path: <.csv>}: reported or model estimates

Rules follow apply_sample_rules: two rules that classify one sample
differently refuse the load (RuleConflictError); a supplement value that
disagrees with the EDD refuses it (LoadConflictError); a source word no rule
maps keeps its words with a NULL code, and is listed.
"""

import json
import math
import re
import struct
from pathlib import Path

from .load_data import LoadConflictError, RuleConflictError, _rule_column, _sql_path, _sql_text

ISM_KEYS = ("ism_settings", "geometries", "field_design", "design_defaults",
            "composite_desc_rules", "support_rules", "replicate_rules", "increments",
            "increment_locations", "preparation", "prep_method_rules", "analysis_groups",
            "vocabulary_rules", "prep_supplement", "uncertainty_components")

SUPPORTS = ("discrete", "composite", "ism")

# Masses are stored in grams (§4).
MASS_TO_G = {"g": 1.0, "gm": 1.0, "gms": 1.0, "gram": 1.0, "grams": 1.0, "kg": 1000.0,
             "mg": 0.001, "ug": 1e-6, "oz": 28.349523125, "lb": 453.59237, "lbs": 453.59237}

FIELD_DESIGN_COLUMNS = {
    # column: type
    "design_increments": "INTEGER", "reported_increments": "INTEGER",
    "increment_layout": "VARCHAR", "random_start": "VARCHAR", "tool_type": "VARCHAR",
    "tool_diameter_mm": "DOUBLE", "min_core_recovery_pct": "DOUBLE",
    "target_mass_min_g": "DOUBLE", "target_mass_max_g": "DOUBLE", "field_mass_g": "DOUBLE",
    "sampling_technique": "VARCHAR", "composite_desc": "VARCHAR", "hold_status": "VARCHAR",
    "hold_until": "DATE",
}
PREP_COLUMNS = {
    "lab_sample_id": "VARCHAR", "sdg": "VARCHAR", "prep_sop": "VARCHAR", "prep_date": "DATE",
    "n_containers": "INTEGER", "received_wet_mass_g": "DOUBLE", "extraneous_removed": "VARCHAR",
    "moisture_subsample_mass_g": "DOUBLE", "drying_method": "VARCHAR", "drying_temp_c": "DOUBLE",
    "drying_start": "DATE", "drying_end": "DATE", "disaggregation": "VARCHAR",
    "milling": "VARCHAR", "milling_duration_min": "DOUBLE", "sieve_opening_mm": "DOUBLE",
    "particle_size_fraction": "VARCHAR", "dry_mass_passing_g": "DOUBLE",
    "oversize_mass_g": "DOUBLE", "oversize_description": "VARCHAR", "observations": "VARCHAR",
    "deviations": "VARCHAR",
}
SUBSAMPLE_COLUMNS = {
    "prep_role": "VARCHAR", "lab_replicate_no": "INTEGER", "splitting_method": "VARCHAR",
    "slab_depth_cm": "DOUBLE", "n_subsample_increments": "INTEGER", "scoop_type": "VARCHAR",
    "target_mass_g": "DOUBLE", "mass_g": "DOUBLE", "percent_solids": "DOUBLE",
    "whole_aliquot_extracted": "BOOLEAN", "prep_method_code": "VARCHAR",
}
# Columns coded from a vocabulary; each keeps <column>_source beside it.
VOCAB_COLUMNS = {"increment_layout", "tool_type", "hold_status", "drying_method",
                 "disaggregation", "milling", "splitting_method", "coord_method", "status",
                 "prep_role"}


def has_ism(mapping):
    return any(mapping.get(k) for k in ISM_KEYS) or bool(
        (mapping.get("locations") or {}).get("parent_location_id"))


def _resolve(mapping, path):
    p = Path(path)
    if not p.is_absolute() and mapping.get("_mapping_dir"):
        p = Path(mapping["_mapping_dir"]) / p
    if not p.exists():
        raise RuleConflictError(f"ISM source file not found: {p}")
    return p


def _report(conn, rows):
    rows = [r for r in rows if r is not None]
    if rows:
        conn.executemany("INSERT OR REPLACE INTO load_report VALUES (?, ?, ?, ?)", rows)


def _read_table(conn, spec, mapping, name):
    """A file a project names, as TEMP TABLE `name` (text columns). Returns
    {lower-cased column: actual column}."""
    path = _resolve(mapping, spec["path"])
    suffix = path.suffix.lower()
    if suffix in (".geojson", ".json"):
        raise RuleConflictError(f"{path.name}: GeoJSON is read by the geometries or increments loader")
    if suffix == ".parquet":
        rel = f"read_parquet('{_sql_path(path)}')"
    else:
        rel = f"read_csv('{_sql_path(path)}', header = true, all_varchar = true)"
    conn.execute(f"CREATE OR REPLACE TEMP TABLE {name} AS SELECT * FROM {rel}")
    where = spec.get("where") or {}
    cols = {r[0].lower(): r[0] for r in conn.execute(f"DESCRIBE {name}").fetchall()}
    for col, values in where.items():
        actual = cols.get(str(col).lower())
        if not actual:
            raise RuleConflictError(f"{path.name}: where: no column {col!r}")
        vals = ", ".join(_sql_text(str(v)) for v in (values if isinstance(values, list) else [values]))
        conn.execute(f'DELETE FROM {name} WHERE CAST("{actual}" AS VARCHAR) NOT IN ({vals}) '
                     f'OR "{actual}" IS NULL')
    return cols


def _colref(cols, columns, target, default=None):
    """The quoted column of a project file that fills `target`: the mapping's
    columns: {target: heading}, else a heading named like the target."""
    src = (columns or {}).get(target, default if default is not None else target)
    if src is None:
        return "NULL"
    actual = cols.get(str(src).lower())
    return f'"{actual}"' if actual else "NULL"


def _typed(expr, typ):
    if expr == "NULL":
        return f"CAST(NULL AS {typ})"
    if typ == "VARCHAR":
        return f"NULLIF(TRIM(CAST({expr} AS VARCHAR)), '')"
    if typ == "BOOLEAN":
        return (f"CASE lower(trim(CAST({expr} AS VARCHAR))) WHEN 'true' THEN TRUE WHEN 't' THEN TRUE "
                f"WHEN 'yes' THEN TRUE WHEN 'y' THEN TRUE WHEN '1' THEN TRUE WHEN 'false' THEN FALSE "
                f"WHEN 'f' THEN FALSE WHEN 'no' THEN FALSE WHEN 'n' THEN FALSE WHEN '0' THEN FALSE END")
    return f"TRY_CAST(NULLIF(TRIM(CAST({expr} AS VARCHAR)), '') AS {typ})"


# ============================================================================
# Vocabularies (§4.8)
# ============================================================================

class Vocab:
    """A project's vocabulary_rules over the shipped codes."""

    def __init__(self, conn, mapping):
        self.codes = {}
        for vocab, code in conn.execute("SELECT vocabulary, code FROM ism_vocabularies").fetchall():
            self.codes.setdefault(vocab, set()).add(code)
        self.rules = {}
        for vocab, words in (mapping.get("vocabulary_rules") or {}).items():
            if vocab not in self.codes:
                raise RuleConflictError(f"vocabulary_rules: no vocabulary {vocab!r} "
                                        f"(known: {sorted(self.codes)})")
            table = {}
            for word, code in (words or {}).items():
                if code not in self.codes[vocab]:
                    raise RuleConflictError(f"vocabulary_rules.{vocab}: {word!r} -> {code!r} is not "
                                            f"a code (codes: {sorted(self.codes[vocab])})")
                key = str(word).strip().lower()
                if table.get(key, code) != code:
                    raise RuleConflictError(f"vocabulary_rules.{vocab}: {word!r} maps two ways")
                table[key] = code
            self.rules[vocab] = table

    def sql(self, vocab, expr):
        """SQL for the code of source text `expr`: a project word, else the
        text when it is a code itself, else NULL."""
        table = dict(self.rules.get(vocab, {}))
        for code in self.codes.get(vocab, ()):
            table.setdefault(code.lower(), code)
        whens = " ".join(f"WHEN {_sql_text(k)} THEN {_sql_text(v)}" for k, v in sorted(table.items()))
        return f"(CASE lower(trim(CAST({expr} AS VARCHAR))) {whens} ELSE CAST(NULL AS VARCHAR) END)"


def _unmapped_report(conn, table, column):
    rows = conn.execute(f"""SELECT {column}_source, count(*) FROM {table}
        WHERE {column} IS NULL AND {column}_source IS NOT NULL GROUP BY 1 ORDER BY 1""").fetchall()
    return [("ism", f"vocabulary unmapped: {column}: {w}", n,
             "kept as source text, code NULL; map it in vocabulary_rules") for w, n in rows]


# ============================================================================
# Geometry, without a spatial extension
# ============================================================================

def _ring_area_centroid(ring):
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    a /= 2.0
    if a == 0:
        return 0.0, None
    return a, (cx / (6 * a), cy / (6 * a))


def polygons_area_centroid(polys):
    """Area and area-weighted centroid of [[outer, hole, ...], ...]."""
    total = sx = sy = 0.0
    for rings in polys:
        for k, ring in enumerate(rings):
            ring = [tuple(map(float, p[:2])) for p in ring]
            if len(ring) > 1 and ring[0] == ring[-1]:
                ring = ring[:-1]
            a, c = _ring_area_centroid(ring)
            a = abs(a) * (1 if k == 0 else -1)
            if c:
                total += a
                sx += a * c[0]
                sy += a * c[1]
    if total <= 0:
        return 0.0, None
    return total, (sx / total, sy / total)


def _in_ring(x, y, ring):
    inside = False
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i][:2]
        x1, y1 = ring[(i + 1) % n][:2]
        if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0) + x0:
            inside = not inside
    return inside


def point_in_polygons(x, y, polys):
    return any(_in_ring(x, y, rings[0]) and not any(_in_ring(x, y, h) for h in rings[1:])
               for rings in polys)


def wkb_polygons(polys):
    """WKB (little-endian) of a Polygon, or a MultiPolygon of several."""
    def poly(rings):
        out = struct.pack("<BII", 1, 3, len(rings))
        for ring in rings:
            pts = [tuple(map(float, p[:2])) for p in ring]
            if pts[0] != pts[-1]:
                pts.append(pts[0])
            out += struct.pack("<I", len(pts)) + b"".join(struct.pack("<dd", *p) for p in pts)
        return out
    if len(polys) == 1:
        return poly(polys[0])
    return struct.pack("<BII", 1, 6, len(polys)) + b"".join(poly(p) for p in polys)


def polygons_from_wkb(blob):
    data = bytes(blob)

    def read(off):
        order = "<" if data[off] == 1 else ">"
        kind = struct.unpack_from(order + "I", data, off + 1)[0] % 1000
        off += 5
        if kind == 3:
            (n_rings,) = struct.unpack_from(order + "I", data, off)
            off += 4
            rings = []
            for _ in range(n_rings):
                (n,) = struct.unpack_from(order + "I", data, off)
                off += 4
                rings.append([struct.unpack_from(order + "dd", data, off + 16 * i) for i in range(n)])
                off += 16 * n
            return [rings], off
        if kind == 6:
            (n,) = struct.unpack_from(order + "I", data, off)
            off += 4
            polys = []
            for _ in range(n):
                p, off = read(off)
                polys += p
            return polys, off
        raise ValueError(f"WKB geometry type {kind}: only polygons")
    return read(0)[0]


_NUM = r"-?[0-9.eE+-]+"


def polygons_from_wkt(text):
    """[[outer, hole, ...], ...] from WKT POLYGON or MULTIPOLYGON."""
    t = text.strip().upper()
    if not (t.startswith("POLYGON") or t.startswith("MULTIPOLYGON")):
        raise ValueError(f"not a polygon: {text[:40]}")
    body = t[t.index("("):]
    polys, rings, ring, depth, num = [], [], [], 0, ""
    multi = t.startswith("MULTIPOLYGON")
    token = ""
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch in ",)":
            if token.strip():
                ring.append(tuple(float(v) for v in token.split()[:2]))
            token = ""
            if ch == ")":
                if depth == (3 if multi else 2):
                    rings.append(ring)
                    ring = []
                elif depth == (2 if multi else 1):
                    polys.append(rings)
                    rings = []
                depth -= 1
        else:
            token += ch
    return polys


def _geojson_polys(geom):
    if geom is None:
        return None
    if geom["type"] == "Polygon":
        return [geom["coordinates"]]
    if geom["type"] == "MultiPolygon":
        return list(geom["coordinates"])
    return None


# ============================================================================
# Steps
# ============================================================================

def _settings(conn, mapping):
    rows = [(str(k), str(v)) for k, v in (mapping.get("ism_settings") or {}).items()]
    for k, v in rows:
        if not (k.startswith("ism_") or k == "uncertainty_basis_order"):
            raise RuleConflictError(f"ism_settings: unknown setting {k!r}")
    if rows:
        conn.executemany("INSERT OR REPLACE INTO chemduck_settings VALUES (?, ?)", rows)


def _parents_from_source(conn, mapping, exprs):
    col = exprs["col_or_null"]((mapping.get("locations") or {}).get("parent_location_id"))
    if col == "NULL":
        return []
    conn.execute(f"""
        UPDATE locations SET parent_location_id = p.parent
        FROM (SELECT CAST("{exprs['loc_id_col']}" AS VARCHAR) AS lid,
                     any_value(NULLIF(TRIM(CAST({col} AS VARCHAR)), '')) AS parent
              FROM staging GROUP BY 1) p
        WHERE locations.location_id = p.lid AND p.parent IS NOT NULL""")
    n = conn.execute("SELECT count(*) FROM locations WHERE parent_location_id IS NOT NULL").fetchone()[0]
    return [("ism", "locations with a parent (SU -> DU) from the source", n, None)]


def _geometries(conn, mapping):
    spec = mapping.get("geometries")
    if not spec:
        return {}, []
    path = _resolve(mapping, spec["path"])
    feats = []   # (location_id, polys, area, parent, loc_type)
    if path.suffix.lower() in (".geojson", ".json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        idp = spec.get("id_property", "location_id")
        for f in doc.get("features", []):
            props = f.get("properties") or {}
            lid = props.get(idp)
            polys = _geojson_polys(f.get("geometry"))
            if lid is None or polys is None:
                continue
            area = props.get(spec["area_property"]) if spec.get("area_property") else None
            feats.append((str(lid), polys, area,
                          props.get(spec["parent_property"]) if spec.get("parent_property") else None,
                          props.get(spec["loc_type_property"]) if spec.get("loc_type_property") else None))
    else:
        cols = _read_table(conn, spec, mapping, "_ism_geom")
        c = spec.get("columns") or {}
        rows = conn.execute(f"""SELECT {_colref(cols, c, 'location_id')}, {_colref(cols, c, 'wkt')},
            {_colref(cols, c, 'area')}, {_colref(cols, c, 'parent_location_id')},
            {_colref(cols, c, 'loc_type')} FROM _ism_geom""").fetchall()
        for lid, wkt, area, parent, loc_type in rows:
            if lid is None or not wkt:
                continue
            feats.append((str(lid), polygons_from_wkt(str(wkt)), area, parent, loc_type))
    polys_by_loc, rows, report = {}, [], []
    no_area = 0
    for lid, polys, area, parent, loc_type in feats:
        computed, centroid = polygons_area_centroid(polys)
        area = float(area) if area not in (None, "") else (computed or None)
        no_area += area is None
        polys_by_loc[lid] = polys
        rows.append((lid, wkb_polygons(polys), area, spec.get("area_units"), path.name,
                     centroid[0] if centroid else None, centroid[1] if centroid else None,
                     None if parent in (None, "") else str(parent), loc_type))
    conn.execute("""CREATE OR REPLACE TEMP TABLE _ism_geo (location_id VARCHAR, wkb BLOB, area DOUBLE,
        area_units VARCHAR, source VARCHAR, cx DOUBLE, cy DOUBLE, parent VARCHAR, loc_type VARCHAR)""")
    if rows:
        conn.executemany("INSERT INTO _ism_geo VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.execute("""INSERT INTO locations (location_id, x, y, loc_type, parent_location_id, spatial_support)
        SELECT location_id, cx, cy, loc_type, parent, 'area' FROM _ism_geo
        WHERE location_id NOT IN (SELECT location_id FROM locations)""")
    conn.execute("""UPDATE locations SET spatial_support = 'area',
            x = COALESCE(locations.x, g.cx), y = COALESCE(locations.y, g.cy),
            parent_location_id = COALESCE(locations.parent_location_id, g.parent)
        FROM _ism_geo g WHERE locations.location_id = g.location_id""")
    conn.execute("""INSERT OR REPLACE INTO location_geometries
        SELECT location_id, wkb, area, area_units, source FROM _ism_geo""")
    report.append(("ism", f"geometries loaded from {path.name}", len(rows), spec.get("area_units")))
    if no_area:
        report.append(("ism", "geometries with no area", no_area,
                       "an area rollup refuses their DU"))
    return polys_by_loc, report


def _field_design(conn, mapping, exprs, vocab):
    """field_sample_design from the source's text, the companion file, the
    design defaults and the composite_desc rules."""
    spec = mapping.get("field_design") or {}
    col_or_null = exprs["col_or_null"]
    sid = f'CAST("{exprs["sample_id_col"]}" AS VARCHAR)'
    # one table of candidate values: (sample_id, column, value, source, rank)
    conn.execute("""CREATE OR REPLACE TEMP TABLE _fd_vals (sample_id VARCHAR, col VARCHAR,
        val VARCHAR, src VARCHAR, rnk INTEGER)""")
    report = []
    for target in ("composite_desc", "sampling_technique"):
        ref = col_or_null(spec.get(target))
        if ref != "NULL":
            conn.execute(f"""INSERT INTO _fd_vals SELECT {sid}, '{target}',
                any_value(NULLIF(TRIM(CAST({ref} AS VARCHAR)), '')), 'edd', 2
                FROM staging WHERE "{exprs['sample_id_col']}" IS NOT NULL GROUP BY 1
                HAVING any_value(NULLIF(TRIM(CAST({ref} AS VARCHAR)), '')) IS NOT NULL""")
    if spec.get("path"):
        cols = _read_table(conn, spec, mapping, "_fd_file")
        key = _colref(cols, None, "key", spec.get("key", "sys_sample_code"))
        if key == "NULL":
            raise RuleConflictError(f"field_design: {spec['path']} has no key column "
                                    f"{spec.get('key', 'sys_sample_code')!r}")
        for target in FIELD_DESIGN_COLUMNS:
            ref = _colref(cols, spec.get("columns"), target)
            if ref != "NULL":
                conn.execute(f"""INSERT INTO _fd_vals SELECT CAST({key} AS VARCHAR), '{target}',
                    NULLIF(TRIM(CAST({ref} AS VARCHAR)), ''), 'field_records', 1 FROM _fd_file
                    WHERE NULLIF(TRIM(CAST({ref} AS VARCHAR)), '') IS NOT NULL""")
        unmatched = conn.execute(f"""SELECT count(*) FROM _fd_file
            WHERE CAST({key} AS VARCHAR) NOT IN (SELECT sample_id FROM samples)""").fetchone()[0]
        report.append(("ism", "field_design rows with no sample", unmatched, None))
    for i, rule in enumerate(mapping.get("composite_desc_rules") or []):
        field = rule.get("field")
        if field not in FIELD_DESIGN_COLUMNS or not rule.get("pattern"):
            raise RuleConflictError(f"composite_desc_rules[{i}]: needs pattern and a field_sample_design "
                                    f"field (got {field!r})")
        pat = _sql_text(rule["pattern"])
        val = (_sql_text(str(rule["value"])) if rule.get("value") is not None
               else f"NULLIF(regexp_extract(val, {pat}, 1), '')")
        conn.execute(f"""INSERT INTO _fd_vals SELECT sample_id, '{field}', {val}, 'edd', 3
            FROM (SELECT sample_id, val FROM _fd_vals WHERE col = 'composite_desc' AND rnk = 2)
            WHERE regexp_matches(val, {pat})""")
    for i, rule in enumerate(mapping.get("design_defaults") or []):
        col = _rule_column(exprs, "design_defaults", i, rule)
        values = rule.get("values") or {}
        bad = [k for k in values if k not in FIELD_DESIGN_COLUMNS]
        if bad or not values:
            raise RuleConflictError(f"design_defaults[{i}]: values must name field_sample_design "
                                    f"fields (unknown: {bad})")
        for k, v in values.items():
            conn.execute(f"""INSERT INTO _fd_vals SELECT DISTINCT {sid}, '{k}', {_sql_text(str(v))},
                'design_default', 4 FROM staging
                WHERE regexp_matches(CAST({col} AS VARCHAR), {_sql_text(rule['pattern'])})""")
    conflicts = conn.execute("""SELECT sample_id, col, list(DISTINCT val ORDER BY val) FROM _fd_vals
        GROUP BY sample_id, col, rnk HAVING count(DISTINCT val) > 1 LIMIT 5""").fetchall()
    if conflicts:
        raise RuleConflictError("field design values that disagree for one sample: "
                                + "; ".join(f"{s} {c}: {v}" for s, c, v in conflicts))
    conn.execute("DELETE FROM _fd_vals WHERE sample_id NOT IN (SELECT sample_id FROM samples)")
    if not conn.execute("SELECT count(*) FROM _fd_vals").fetchone()[0]:
        return report
    sel = []
    for c, typ in FIELD_DESIGN_COLUMNS.items():
        v = f"arg_min(val, rnk) FILTER (WHERE col = '{c}')"
        if c in VOCAB_COLUMNS:
            sel.append(f"{vocab.sql(c, v)} AS {c}, {v} AS {c}_source")
        else:
            sel.append(f"{_typed(v, typ)} AS {c}")
    conn.execute(f"""CREATE OR REPLACE TEMP TABLE _fd AS
        SELECT sample_id, {', '.join(sel)},
               array_to_string(list_sort(list_distinct(list(src))), '+') AS source
        FROM _fd_vals GROUP BY sample_id""")
    cols = [r[0] for r in conn.execute("DESCRIBE _fd").fetchall()]
    conn.execute(f"INSERT OR REPLACE INTO field_sample_design ({', '.join(cols)}) "
                 f"SELECT {', '.join(cols)} FROM _fd")
    for c in ("increment_layout", "tool_type", "hold_status"):
        report += _unmapped_report(conn, "field_sample_design", c)
    report.append(("ism", "field_sample_design rows", conn.execute(
        "SELECT count(*) FROM field_sample_design").fetchone()[0], None))
    return report


def _support_rules(conn, mapping, exprs):
    rules = mapping.get("support_rules") or []
    if not rules:
        return []
    sid = f'CAST("{exprs["sample_id_col"]}" AS VARCHAR)'
    parts = []
    for i, r in enumerate(rules):
        support = r.get("support")
        if support not in SUPPORTS:
            raise RuleConflictError(f"support_rules[{i}]: support must be one of {SUPPORTS}")
        if r.get("field"):
            if r["field"] not in ("design_increments", "reported_increments"):
                raise RuleConflictError(f"support_rules[{i}]: field must be design_increments or "
                                        "reported_increments")
            conds = [f"{r['field']} IS NOT NULL"]
            if r.get("min") is not None:
                conds.append(f"{r['field']} >= {float(r['min'])}")
            if r.get("max") is not None:
                conds.append(f"{r['field']} <= {float(r['max'])}")
            parts.append(f"SELECT {i} AS rule, sample_id AS sid, '{support}' AS support "
                         f"FROM field_sample_design WHERE {' AND '.join(conds)}")
            continue
        col = _rule_column(exprs, "support_rules", i, r)
        if str(r.get("column")).upper() == "COMPOSITE_YN" and support == "ism":
            raise RuleConflictError(
                f"support_rules[{i}]: COMPOSITE_YN is Y for 5-point composites and ISM alike, so "
                "it can give only 'composite'; read COMPOSITE_DESC, SAMPLE_METHOD, a code or "
                "the design's increments for 'ism'")
        parts.append(f"""SELECT DISTINCT {i} AS rule, {sid} AS sid, '{support}' AS support
            FROM staging WHERE regexp_matches(CAST({col} AS VARCHAR), {_sql_text(r['pattern'])})""")
    conn.execute(f"CREATE OR REPLACE TEMP TABLE _support_matches AS {' UNION ALL '.join(parts)}")
    bad = conn.execute("""SELECT sid, list(DISTINCT support ORDER BY support) FROM _support_matches
        GROUP BY sid HAVING count(DISTINCT support) > 1 ORDER BY sid LIMIT 10""").fetchall()
    if bad:
        raise RuleConflictError("samples that support_rules classify differently, e.g. "
                                + "; ".join(f"{s}: {v}" for s, v in bad))
    conn.execute("""UPDATE samples SET support = m.support
        FROM (SELECT sid, any_value(support) AS support FROM _support_matches GROUP BY sid) m
        WHERE samples.sample_id = m.sid""")
    report = []
    for i, r in enumerate(rules):
        n = conn.execute(f"SELECT count(DISTINCT sid) FROM _support_matches WHERE rule = {i}").fetchone()[0]
        what = (f"{r['field']} {r.get('min', '')}..{r.get('max', '')}" if r.get("field")
                else f"{r['column']} ~ {r['pattern']}")
        report.append(("support_rule", f"{i}: {what} -> {r['support']}", n, None))
    for s, n in conn.execute("""SELECT support_code(support), count(*) FROM samples
                                GROUP BY 1 ORDER BY 1""").fetchall():
        report.append(("support_rule", f"samples of support {s}", n, None))
    return report


def _replicate_rules(conn, mapping, exprs):
    rules = mapping.get("replicate_rules") or []
    if not rules:
        return []
    sid = f'CAST("{exprs["sample_id_col"]}" AS VARCHAR)'
    parts = []
    for i, r in enumerate(rules):
        col = _rule_column(exprs, "replicate_rules", i, r)
        pat = _sql_text(r["pattern"])
        try:
            groups = re.compile(r["pattern"]).groups
        except re.error as e:
            raise RuleConflictError(f"replicate_rules[{i}]: pattern {r['pattern']!r}: {e}") from None
        sg, ng = int(r.get("set_group", 1)), int(r.get("number_group", 2))
        if groups < max(sg, ng):
            raise RuleConflictError(f"replicate_rules[{i}]: the pattern must capture the set "
                                    f"(group {sg}) and the replicate number (group {ng})")
        parts.append(f"""SELECT {i} AS rule, sid, regexp_extract(val, {pat}, {sg}) AS rset,
                TRY_CAST(regexp_extract(val, {pat}, {ng}) AS INTEGER) AS rno,
                {'TRUE' if r.get('retype', True) else 'FALSE'} AS retype
            FROM (SELECT DISTINCT {sid} AS sid, CAST({col} AS VARCHAR) AS val FROM staging)
            WHERE regexp_matches(val, {pat})""")
    conn.execute(f"CREATE OR REPLACE TEMP TABLE _rep_matches AS {' UNION ALL '.join(parts)}")
    bad = conn.execute("""SELECT sid, list(DISTINCT rset || '#' || COALESCE(CAST(rno AS VARCHAR), '?'))
        FROM _rep_matches GROUP BY sid HAVING count(DISTINCT (rset, rno)) > 1 LIMIT 10""").fetchall()
    if bad:
        raise RuleConflictError("samples that replicate_rules place in different sets, e.g. "
                                + "; ".join(f"{s}: {v}" for s, v in bad))
    # Laboratory replicates are never field replicates (§4.2).
    conn.execute("""CREATE OR REPLACE TEMP TABLE _rep AS
        SELECT m.sid, any_value(m.rset) || COALESCE('@' || any_value(s.event), '') AS rset,
               any_value(m.rno) AS rno, bool_or(m.retype) AS retype, any_value(s.sample_type) AS stype
        FROM _rep_matches m JOIN samples s ON s.sample_id = m.sid
        WHERE upper(trim(COALESCE(s.sample_type, 'N'))) <> 'LR'
        GROUP BY m.sid""")
    conn.execute("""UPDATE samples SET replicate_set = r.rset, replicate_no = r.rno,
            sample_type = CASE WHEN r.retype AND upper(trim(COALESCE(samples.sample_type, 'N'))) <> 'N'
                               THEN 'N' ELSE samples.sample_type END
        FROM _rep r WHERE samples.sample_id = r.sid""")
    report = []
    for i, r in enumerate(rules):
        n = conn.execute(f"SELECT count(DISTINCT sid) FROM _rep_matches WHERE rule = {i}").fetchone()[0]
        report.append(("replicate_rule", f"{i}: {r['column']} ~ {r['pattern']}", n, None))
    for stype, n in conn.execute("""SELECT stype, count(*) FROM _rep WHERE retype
            AND upper(trim(COALESCE(stype, 'N'))) <> 'N' GROUP BY 1 ORDER BY 1""").fetchall():
        report.append(("replicate_rule", f"retyped {stype} -> N (ISM replicates are decision data)", n,
                       "the source typed them QC; replicate_rules override it (open decision 3)"))
    n_lr = conn.execute("""SELECT count(DISTINCT m.sid) FROM _rep_matches m JOIN samples s
        ON s.sample_id = m.sid WHERE upper(trim(COALESCE(s.sample_type, 'N'))) = 'LR'""").fetchone()[0]
    report.append(("replicate_rule", "laboratory replicates matched, left out of every set", n_lr, None))
    sets = conn.execute("""SELECT count(*), count(*) FILTER (WHERE n < 3) FROM (
        SELECT replicate_set, count(*) AS n FROM samples WHERE replicate_set IS NOT NULL GROUP BY 1)""").fetchone()
    report.append(("replicate_rule", "replicate sets", sets[0], f"{sets[1]} with fewer than 3 samples"))
    return report


def _increments(conn, mapping, polys_by_loc, vocab):
    spec = mapping.get("increments")
    report = []
    conn.execute("""CREATE OR REPLACE TEMP TABLE _inc (increment_id VARCHAR, location_id VARCHAR,
        sample_id VARCHAR, hole_id VARCHAR, increment_no VARCHAR, x VARCHAR, y VARCHAR,
        coord_method VARCHAR, top_depth VARCHAR, bottom_depth VARCHAR, depth_units VARCHAR,
        recovery_pct VARCHAR, collected_at VARCHAR, mass_g VARCHAR, status VARCHAR, source VARCHAR)""")
    targets = ["increment_id", "location_id", "sample_id", "hole_id", "increment_no", "x", "y",
               "coord_method", "top_depth", "bottom_depth", "depth_units", "recovery_pct",
               "collected_at", "mass_g", "status"]
    if spec:
        path = _resolve(mapping, spec["path"])
        c = spec.get("columns") or {}
        if path.suffix.lower() in (".geojson", ".json"):
            doc = json.loads(path.read_text(encoding="utf-8"))
            rows = []
            for f in doc.get("features", []):
                props = {str(k).lower(): v for k, v in (f.get("properties") or {}).items()}
                geom = f.get("geometry") or {}
                xy = geom.get("coordinates") if geom.get("type") == "Point" else None

                def get(t):
                    if t in ("x", "y") and xy and t not in c:
                        return str(xy[0 if t == "x" else 1])
                    v = props.get(str(c.get(t, t)).lower())
                    return None if v is None else str(v)
                rows.append(tuple(get(t) for t in targets) + (path.name,))
            if rows:
                conn.executemany(f"INSERT INTO _inc VALUES ({', '.join('?' * 16)})", rows)
        else:
            cols = _read_table(conn, spec, mapping, "_inc_file")
            conn.execute(f"""INSERT INTO _inc SELECT {', '.join(
                f'CAST({_colref(cols, c, t)} AS VARCHAR)' for t in targets)}, {_sql_text(path.name)}
                FROM _inc_file""")
    moved = 0
    il = mapping.get("increment_locations") or {}
    if il.get("loc_type"):
        types = ", ".join(_sql_text(t) for t in il["loc_type"])
        conn.execute(f"""CREATE OR REPLACE TEMP TABLE _inc_locs AS SELECT location_id, parent_location_id,
            x, y FROM locations WHERE loc_type IN ({types}) AND parent_location_id IS NOT NULL
            AND location_id NOT IN (SELECT location_id FROM samples)""")
        conn.execute("""INSERT INTO _inc (increment_id, location_id, x, y, source)
            SELECT location_id, parent_location_id, CAST(x AS VARCHAR), CAST(y AS VARCHAR), 'edd'
            FROM _inc_locs""")
        conn.execute("DELETE FROM location_attributes WHERE location_id IN (SELECT location_id FROM _inc_locs)")
        conn.execute("DELETE FROM location_geometries WHERE location_id IN (SELECT location_id FROM _inc_locs)")
        conn.execute("DELETE FROM locations WHERE location_id IN (SELECT location_id FROM _inc_locs)")
        moved = conn.execute("SELECT count(*) FROM _inc_locs").fetchone()[0]
        report.append(("ism", "increment locations moved out of locations", moved, None))
    if not conn.execute("SELECT count(*) FROM _inc").fetchone()[0]:
        return report
    # The increment's sample, then its location: the source's, else the sample's.
    no_sample = conn.execute("""SELECT count(*) FROM _inc WHERE sample_id IS NOT NULL
        AND sample_id NOT IN (SELECT sample_id FROM samples)""").fetchone()[0]
    report.append(("ism", "increment_no_sample", no_sample, "increments whose sample code matched nothing"))
    conn.execute("""UPDATE _inc SET sample_id = NULL
        WHERE sample_id IS NOT NULL AND sample_id NOT IN (SELECT sample_id FROM samples)""")
    conn.execute("""UPDATE _inc SET location_id = s.location_id FROM samples s
        WHERE _inc.location_id IS NULL AND s.sample_id = _inc.sample_id""")
    unplaced = conn.execute("""SELECT count(*) FROM _inc WHERE location_id IS NULL
        OR location_id NOT IN (SELECT location_id FROM locations)""").fetchone()[0]
    if unplaced:
        report.append(("ism", "increments with no known location (not loaded)", unplaced, None))
    conn.execute("""DELETE FROM _inc WHERE location_id IS NULL
        OR location_id NOT IN (SELECT location_id FROM locations)""")
    conn.execute(f"""INSERT OR REPLACE INTO sample_increments
        (increment_id, location_id, sample_id, hole_id, increment_no, x, y, coord_method,
         coord_method_source, top_depth, bottom_depth, depth_units, recovery_pct, collected_at,
         mass_g, status, status_source, source)
        SELECT COALESCE(increment_id, location_id || '|' || COALESCE(sample_id, '') || '|'
                        || COALESCE(increment_no, CAST(row_number() OVER () AS VARCHAR))),
               location_id, sample_id, NULLIF(TRIM(hole_id), ''), TRY_CAST(increment_no AS INTEGER),
               TRY_CAST(x AS DOUBLE), TRY_CAST(y AS DOUBLE),
               {vocab.sql('coord_method', 'coord_method')}, NULLIF(TRIM(coord_method), ''),
               TRY_CAST(top_depth AS DOUBLE), TRY_CAST(bottom_depth AS DOUBLE),
               NULLIF(TRIM(depth_units), ''), TRY_CAST(recovery_pct AS DOUBLE),
               TRY_CAST(collected_at AS TIMESTAMP), TRY_CAST(mass_g AS DOUBLE),
               {vocab.sql('status', 'status')}, NULLIF(TRIM(status), ''), source
        FROM _inc""")
    # Inside the DU/SU polygon, where both are known.
    if not polys_by_loc:
        for lid, blob in conn.execute("""SELECT location_id, geom_wkb FROM location_geometries
                WHERE location_id IN (SELECT location_id FROM sample_increments)""").fetchall():
            if blob is not None:
                polys_by_loc[lid] = polygons_from_wkb(blob)
    flags = [(point_in_polygons(x, y, polys_by_loc[lid]), iid)
             for iid, lid, x, y in conn.execute("""SELECT increment_id, location_id, x, y
                 FROM sample_increments WHERE x IS NOT NULL AND y IS NOT NULL""").fetchall()
             if lid in polys_by_loc]
    if flags:
        conn.execute("CREATE OR REPLACE TEMP TABLE _inside (inside BOOLEAN, increment_id VARCHAR)")
        conn.executemany("INSERT INTO _inside VALUES (?, ?)", flags)
        conn.execute("""UPDATE sample_increments SET inside_area = i.inside FROM _inside i
            WHERE sample_increments.increment_id = i.increment_id""")
    report.append(("ism", "increments loaded", conn.execute(
        "SELECT count(*) FROM sample_increments").fetchone()[0],
        f"{conn.execute('SELECT count(DISTINCT hole_id) FROM sample_increments').fetchone()[0]} hole(s)"))
    report += _unmapped_report(conn, "sample_increments", "coord_method")
    report += _unmapped_report(conn, "sample_increments", "status")
    return report


def _mass_sql(amount, unit):
    """(grams, the text when it would not parse or convert) of a mass."""
    num = f"TRY_CAST(regexp_extract(trim(CAST({amount} AS VARCHAR)), '^([0-9]*\\.?[0-9]+)\\s*([A-Za-z]*)$', 1) AS DOUBLE)"
    unit_text = (f"lower(COALESCE(NULLIF(regexp_extract(trim(CAST({amount} AS VARCHAR)), "
                 f"'^([0-9]*\\.?[0-9]+)\\s*([A-Za-z]*)$', 2), ''), trim(CAST({unit} AS VARCHAR)), 'g'))")
    factors = " ".join(f"WHEN '{u}' THEN {f!r}" for u, f in MASS_TO_G.items())
    grams = f"({num} * CASE {unit_text} {factors} END)"
    text = (f"CASE WHEN NULLIF(trim(CAST({amount} AS VARCHAR)), '') IS NOT NULL AND {grams} IS NULL "
            f"THEN trim(CAST({amount} AS VARCHAR)) || COALESCE(' ' || trim(CAST({unit} AS VARCHAR)), '') END")
    return grams, text


def _analysis_groups(conn, mapping):
    rules = mapping.get("analysis_groups") or []
    conn.execute("DELETE FROM ism_analysis_groups")
    rows = []
    for i, r in enumerate(rules):
        col = r.get("column", "method_code")
        if col not in ("method_code", "method_class") or not r.get("pattern") or not r.get("group"):
            raise RuleConflictError(f"analysis_groups[{i}]: needs pattern, group and column "
                                    "method_code or method_class")
        try:
            re.compile(r["pattern"])
        except re.error as e:
            raise RuleConflictError(f"analysis_groups[{i}]: {e}") from None
        rows.append((i, col, str(r["pattern"]).upper(), str(r["group"])))
    if rows:
        conn.executemany("INSERT INTO ism_analysis_groups VALUES (?, ?, ?, ?)", rows)
    return rows


def _group_sql(rules, code_expr, class_expr):
    if not rules:
        return "CAST(NULL AS VARCHAR)"
    whens = " ".join(
        f"WHEN regexp_full_match(upper(trim(CAST({code_expr if col == 'method_code' else class_expr} "
        f"AS VARCHAR))), {_sql_text(pat)}) THEN {_sql_text(grp)}"
        for _, col, pat, grp in rules)
    return f"CASE {whens} END"


def _preparations(conn, mapping, exprs, vocab):
    """sample_preparations and sample_subsamples from the source's per-test
    fields (§5.2), merged with the supplement (§5.3)."""
    prep = mapping.get("preparation") or {}
    sup = mapping.get("prep_supplement")
    groups = _analysis_groups(conn, mapping)
    if not prep and not sup:
        return []
    col_or_null = exprs["col_or_null"]
    rmap = mapping.get("results") or {}
    sid = f'CAST("{exprs["sample_id_col"]}" AS VARCHAR)'
    lab_sid = f"NULLIF(TRIM(CAST({col_or_null(prep.get('lab_sample_id') or rmap.get('lab_sample_id'))} AS VARCHAR)), '')"
    lab_name = f"NULLIF(TRIM(CAST({col_or_null(rmap.get('lab_name'))} AS VARCHAR)), '')"
    sdg = f"NULLIF(TRIM(CAST({col_or_null(rmap.get('lab_package_id'))} AS VARCHAR)), '')"
    code = col_or_null(rmap.get("method_code"))
    grams, mass_text = _mass_sql(col_or_null(prep.get("subsample_amount")),
                                 col_or_null(prep.get("subsample_amount_unit")))
    pm = f"NULLIF(TRIM(CAST({col_or_null(prep.get('prep_method'))} AS VARCHAR)), '')"
    moist = f"TRY_CAST({col_or_null(prep.get('percent_moisture'))} AS DOUBLE)"
    date_fmt = (mapping.get("source") or {}).get("date_formats") or []
    pdate_ref = col_or_null(prep.get("prep_date"))
    pdate = (f"CAST(COALESCE({', '.join(f'TRY_STRPTIME(CAST({pdate_ref} AS VARCHAR), {_sql_text(f)})' for f in date_fmt)}"
             f"{', ' if date_fmt else ''}TRY_CAST({pdate_ref} AS TIMESTAMP)) AS DATE)")
    report = []
    # The samples that get preparation records: composite and ISM samples,
    # and the laboratory replicates coded as samples of their own.
    conn.execute("""CREATE OR REPLACE TEMP TABLE _prep_samples AS
        SELECT s.sample_id AS _ps_sid, s.sample_id AS _ps_prep, FALSE AS _ps_lr
        FROM samples s WHERE support_code(s.support) <> 'discrete'
          AND upper(trim(COALESCE(s.sample_type, 'N'))) <> 'LR'
        UNION ALL
        SELECT s.sample_id, s.parent_sample_id, TRUE FROM samples s
        JOIN samples p ON p.sample_id = s.parent_sample_id
        WHERE upper(trim(COALESCE(s.sample_type, '')))  = 'LR'
          AND support_code(p.support) <> 'discrete'""")
    conn.execute(f"""CREATE OR REPLACE TEMP TABLE _edd_tests AS
        SELECT ps._ps_sid AS sample_id, ps._ps_prep AS prep_sample_id, ps._ps_lr AS lr,
               {lab_sid} AS lab_sample_id, {lab_name} AS lab_name, {sdg} AS sdg,
               {_group_sql(groups, code, exprs['method_class_expr'])} AS analysis_group,
               {pm} AS prep_method_code, {grams} AS mass_g, {mass_text} AS mass_text,
               {moist} AS moisture, {pdate} AS prep_date
        FROM staging t JOIN _prep_samples ps ON ps._ps_sid = {sid}""")
    conn.execute("""CREATE OR REPLACE TEMP TABLE _edd_sub AS
        SELECT sample_id, any_value(prep_sample_id) AS prep_sample_id, bool_or(lr) AS lr,
               lab_sample_id, analysis_group, any_value(lab_name) AS lab_name,
               any_value(sdg) AS sdg, min(prep_date) AS prep_date,
               CASE WHEN count(DISTINCT prep_method_code) = 1 THEN any_value(prep_method_code)
                    WHEN count(DISTINCT prep_method_code) > 1
                    THEN array_to_string(list_sort(list(DISTINCT prep_method_code)), ', ') END AS prep_method_code,
               CASE WHEN count(DISTINCT mass_g) <= 1 THEN max(mass_g) END AS mass_g,
               CASE WHEN count(DISTINCT mass_g) > 1
                    THEN 'several: ' || array_to_string(list_sort(list(DISTINCT CAST(mass_g AS VARCHAR))), ', ')
                    ELSE max(mass_text) END AS mass_reported,
               CASE WHEN count(DISTINCT moisture) = 1 THEN 100 - max(moisture) END AS percent_solids
        FROM _edd_tests GROUP BY sample_id, lab_sample_id, analysis_group""")
    n_disagree = conn.execute("SELECT count(*) FROM _edd_sub WHERE mass_reported LIKE 'several:%'").fetchone()[0]
    if n_disagree:
        report.append(("ism", "subsample_mass_disagrees", n_disagree,
                       "aliquots whose tests give different SUBSAMPLE_AMOUNTs: mass_g left NULL; "
                       "split them with analysis_groups"))
    # The supplement, one row per aliquot, its columns renamed by the mapping.
    conn.execute(f"""CREATE OR REPLACE TEMP TABLE _sup (sample_id VARCHAR, lab_sample_id VARCHAR,
        analysis_group VARCHAR, lab_name VARCHAR,
        {', '.join(f'{c} VARCHAR' for c in [*PREP_COLUMNS, *SUBSAMPLE_COLUMNS] if c != 'lab_sample_id')},
        parent_lab_sample_id VARCHAR, mass_unit VARCHAR, prep_key VARCHAR)""")
    if sup:
        cols = _read_table(conn, sup, mapping, "_sup_file")
        c = dict(sup.get("columns") or {})
        if "sys_sample_code" in c:   # the EDD's name for the sample key
            c.setdefault("sample_id", c["sys_sample_code"])
        targets = ["sample_id", "lab_sample_id", "analysis_group", "lab_name",
                   *[x for x in [*PREP_COLUMNS, *SUBSAMPLE_COLUMNS] if x != "lab_sample_id"],
                   "parent_lab_sample_id", "mass_unit", "prep_key"]
        defaults = {"sample_id": "sys_sample_code"}
        conn.execute(f"""INSERT INTO _sup SELECT {', '.join(
            f"NULLIF(TRIM(CAST({_colref(cols, c, t, defaults.get(t))} AS VARCHAR)), '')" for t in targets)}
            FROM _sup_file""")
        unmatched = conn.execute("""SELECT count(*) FROM _sup
            WHERE sample_id NOT IN (SELECT sample_id FROM samples)""").fetchone()[0]
        report.append(("ism", "supplement_unmatched", unmatched,
                       "supplement rows with no EDD sample: not loaded"))
        conn.execute("DELETE FROM _sup WHERE sample_id NOT IN (SELECT sample_id FROM samples)")
        # a supplement's mass in another unit
        g2, t2 = _mass_sql("mass_g", "mass_unit")
        conn.execute(f"UPDATE _sup SET mass_g = CAST({g2} AS VARCHAR) WHERE mass_g IS NOT NULL")
    # Match supplement rows to the EDD's aliquots.
    conn.execute("""CREATE OR REPLACE TEMP TABLE _sup_match AS
        SELECT s.rowid AS sup_row, e.sample_id, e.lab_sample_id, e.analysis_group
        FROM _sup s JOIN _edd_sub e ON e.sample_id = s.sample_id
         AND (s.lab_sample_id IS NULL OR e.lab_sample_id IS NULL OR s.lab_sample_id = e.lab_sample_id)
         AND (s.analysis_group IS NULL OR e.analysis_group IS NULL OR s.analysis_group = e.analysis_group)""")
    amb = conn.execute("""SELECT s.sample_id, s.lab_sample_id, s.analysis_group FROM _sup_match m
        JOIN _sup s ON s.rowid = m.sup_row GROUP BY ALL HAVING count(*) > 1 LIMIT 5""").fetchall()
    if amb:
        raise LoadConflictError("supplement rows matching several EDD aliquots (give lab_sample_id and "
                                "analysis_group): " + "; ".join(map(str, amb)))
    sup_cols = [x for x in SUBSAMPLE_COLUMNS]
    sel_sup = ", ".join(f"s.{x} AS s_{x}" for x in sup_cols)
    conn.execute(f"""CREATE OR REPLACE TEMP TABLE _sub_all AS
        SELECT COALESCE(e.sample_id, s.sample_id) AS sample_id,
               COALESCE(e.lab_sample_id, s.lab_sample_id) AS lab_sample_id,
               COALESCE(e.analysis_group, s.analysis_group) AS analysis_group,
               COALESCE(e.prep_sample_id,
                        (SELECT CASE WHEN upper(trim(COALESCE(x.sample_type, ''))) = 'LR'
                                     THEN x.parent_sample_id ELSE x.sample_id END
                         FROM samples x WHERE x.sample_id = s.sample_id)) AS prep_sample_id,
               COALESCE(e.lr, upper(trim(COALESCE(smp.sample_type, ''))) = 'LR') AS lr,
               COALESCE(e.lab_name, s.lab_name) AS lab_name, e.sdg, e.prep_date,
               e.prep_method_code AS e_prep_method_code, e.mass_g AS e_mass_g,
               e.mass_reported AS e_mass_reported, e.percent_solids AS e_percent_solids,
               {sel_sup}, s.parent_lab_sample_id, s.prep_key,
               e.sample_id IS NOT NULL AS from_edd, s.sample_id IS NOT NULL AS from_sup,
               s.rowid AS sup_row
        FROM _edd_sub e
        LEFT JOIN _sup_match m ON m.sample_id = e.sample_id
             AND m.lab_sample_id IS NOT DISTINCT FROM e.lab_sample_id
             AND m.analysis_group IS NOT DISTINCT FROM e.analysis_group
        FULL JOIN _sup s ON s.rowid = m.sup_row
        LEFT JOIN samples smp ON smp.sample_id = COALESCE(e.sample_id, s.sample_id)""")
    # A supplement value that disagrees with the EDD refuses the load.
    clash = conn.execute("""SELECT sample_id, lab_sample_id, analysis_group, 'mass_g', e_mass_g, s_mass_g
        FROM _sub_all WHERE e_mass_g IS NOT NULL AND TRY_CAST(s_mass_g AS DOUBLE) IS NOT NULL
          AND abs(e_mass_g - TRY_CAST(s_mass_g AS DOUBLE)) > 1e-6 * greatest(abs(e_mass_g), 1)
        UNION ALL
        SELECT sample_id, lab_sample_id, analysis_group, 'percent_solids', e_percent_solids, s_percent_solids
        FROM _sub_all WHERE e_percent_solids IS NOT NULL AND TRY_CAST(s_percent_solids AS DOUBLE) IS NOT NULL
          AND abs(e_percent_solids - TRY_CAST(s_percent_solids AS DOUBLE)) > 1e-6
        UNION ALL
        SELECT sample_id, lab_sample_id, analysis_group, 'prep_method_code', e_prep_method_code, s_prep_method_code
        FROM _sub_all WHERE e_prep_method_code IS NOT NULL AND s_prep_method_code IS NOT NULL
          AND upper(e_prep_method_code) <> upper(s_prep_method_code)
        UNION ALL
        SELECT sample_id, lab_sample_id, analysis_group, 'prep_role', CASE WHEN lr THEN 'lab_replicate' END,
               s_prep_role
        FROM _sub_all WHERE lr AND s_prep_role IS NOT NULL AND lower(s_prep_role) NOT IN ('lab_replicate')
        LIMIT 10""").fetchall()
    if clash:
        raise LoadConflictError(
            "the preparation supplement disagrees with the EDD; ChemDuck does not pick one: "
            + "; ".join(f"{s} lab {l} group {g}: {c} EDD {e!r}, supplement {v!r}"
                        for s, l, g, c, e, v in clash))
    # Preparations: one per field sample and laboratory.
    prep_sel = []
    for col, typ in PREP_COLUMNS.items():
        if col == "lab_sample_id":
            continue
        v = f"max(s.{col})"
        if col in VOCAB_COLUMNS:
            prep_sel.append(f"{vocab.sql(col, v)} AS {col}, {v} AS {col}_source")
        else:
            prep_sel.append(f"{_typed(v, typ)} AS {col}")
    clash = conn.execute(f"""SELECT a.prep_sample_id, c FROM _sub_all a
        JOIN _sup s ON s.rowid = a.sup_row,
        unnest([{', '.join(f"CASE WHEN s.{c} IS NOT NULL THEN '{c}' END" for c in PREP_COLUMNS if c not in ('lab_sample_id', 'prep_date', 'sdg'))}]) AS t(c)
        WHERE c IS NOT NULL GROUP BY a.prep_sample_id, COALESCE(a.lab_name, ''),
              COALESCE(a.prep_key, ''), c
        HAVING count(DISTINCT CASE c {' '.join(f"WHEN '{c}' THEN s.{c}" for c in PREP_COLUMNS if c not in ('lab_sample_id', 'prep_date', 'sdg'))} END) > 1
        LIMIT 5""").fetchall()
    if clash:
        raise LoadConflictError("supplement rows of one preparation disagree: "
                                + "; ".join(f"{p}: {c}" for p, c in clash))
    conn.execute(f"""CREATE OR REPLACE TEMP TABLE _prep AS
        SELECT concat_ws('|', a.prep_sample_id, a.lab_name, a.prep_key) AS prep_id,
               a.prep_sample_id AS sample_id, a.lab_name,
               min(a.lab_sample_id) FILTER (WHERE NOT a.lr) AS lab_sample_id,
               COALESCE(any_value(a.sdg), max(s.sdg)) AS sdg,
               COALESCE(min(a.prep_date), {_typed('max(s.prep_date)', 'DATE')}) AS prep_date,
               {', '.join(c for c in prep_sel if not c.endswith(' AS sdg') and not c.endswith(' AS prep_date'))},
               CASE WHEN bool_or(a.from_edd) AND bool_or(a.from_sup) THEN 'edd+supplement'
                    WHEN bool_or(a.from_edd) THEN 'edd' ELSE 'supplement' END AS source
        FROM _sub_all a LEFT JOIN _sup s ON s.rowid = a.sup_row
        WHERE a.prep_sample_id IS NOT NULL
        GROUP BY a.prep_sample_id, a.lab_name, a.prep_key""")
    cols = [r[0] for r in conn.execute("DESCRIBE _prep").fetchall()]
    conn.execute(f"INSERT OR REPLACE INTO sample_preparations ({', '.join(cols)}) "
                 f"SELECT {', '.join(cols)} FROM _prep")
    # Aliquots. A role the supplement gives; else lab_replicate for an LR
    # sample, primary for the one aliquot of its sample and group, NULL when
    # several share them (the EDD alone cannot say which is primary).
    rules = mapping.get("prep_method_rules") or []
    for i, r in enumerate(rules):
        if r.get("role") not in ("ism_processing", "extraction") or not r.get("pattern"):
            raise RuleConflictError(f"prep_method_rules[{i}]: needs pattern and role "
                                    "ism_processing or extraction")
    def role_sql(role):
        pats = [r["pattern"] for r in rules if r["role"] == role]
        if not pats:
            return "CAST(NULL AS VARCHAR)"
        cond = " OR ".join(f"regexp_full_match(upper(trim(pmc)), {_sql_text(str(p).upper())})" for p in pats)
        return f"CASE WHEN {cond} THEN pmc END"
    conn.execute(f"""CREATE OR REPLACE TEMP TABLE _sub AS
        SELECT *, {role_sql('ism_processing')} AS ism_prep_code,
                  {role_sql('extraction')} AS extraction_method
        FROM (
            SELECT a.*,
                   concat_ws('|', a.prep_sample_id, a.lab_name, a.prep_key) AS prep_id,
                   a.sample_id || '|' || COALESCE(a.lab_sample_id, '') || '|'
                       || COALESCE(a.analysis_group, '') AS subsample_id,
                   COALESCE(a.e_prep_method_code, a.s_prep_method_code) AS pmc,
                   COALESCE({vocab.sql('prep_role', 'a.s_prep_role')},
                            CASE WHEN a.lr THEN 'lab_replicate'
                                 WHEN count(*) FILTER (WHERE NOT a.lr) OVER (
                                          PARTITION BY a.prep_sample_id, a.lab_name, a.prep_key,
                                                       a.analysis_group) = 1
                                     THEN 'primary' END) AS role
            FROM _sub_all a WHERE a.prep_sample_id IS NOT NULL)""")
    sub_sel = f"""subsample_id, prep_id, sample_id, lab_sample_id, role AS prep_role,
        TRY_CAST(s_lab_replicate_no AS INTEGER) AS lab_replicate_no,
        CAST(NULL AS VARCHAR) AS parent_subsample_id, analysis_group,
        {vocab.sql('splitting_method', 's_splitting_method')} AS splitting_method,
        s_splitting_method AS splitting_method_source,
        {_typed('s_slab_depth_cm', 'DOUBLE')} AS slab_depth_cm,
        {_typed('s_n_subsample_increments', 'INTEGER')} AS n_subsample_increments,
        s_scoop_type AS scoop_type, {_typed('s_target_mass_g', 'DOUBLE')} AS target_mass_g,
        COALESCE(e_mass_g, {_typed('s_mass_g', 'DOUBLE')}) AS mass_g,
        CASE WHEN e_mass_g IS NULL AND s_mass_g IS NULL THEN e_mass_reported END AS mass_reported,
        COALESCE(e_percent_solids, {_typed('s_percent_solids', 'DOUBLE')}) AS percent_solids,
        {_typed('s_whole_aliquot_extracted', 'BOOLEAN')} AS whole_aliquot_extracted,
        pmc AS prep_method_code, ism_prep_code, extraction_method,
        CASE WHEN from_edd AND from_sup THEN 'edd+supplement' WHEN from_edd THEN 'edd'
             ELSE 'supplement' END AS source"""
    conn.execute(f"INSERT OR REPLACE INTO sample_subsamples SELECT {sub_sel} FROM _sub")
    # A laboratory replicate's primary: named by the supplement's
    # parent_lab_sample_id, else the one primary of its preparation and group.
    conn.execute("""CREATE OR REPLACE TEMP TABLE _parents AS
        SELECT r.subsample_id, p.subsample_id AS parent
        FROM _sub r JOIN _sub p ON p.prep_id = r.prep_id AND p.lab_sample_id = r.parent_lab_sample_id
         AND p.analysis_group IS NOT DISTINCT FROM r.analysis_group
        WHERE r.parent_lab_sample_id IS NOT NULL
        UNION ALL
        SELECT r.subsample_id, p.subsample_id
        FROM _sub r JOIN (SELECT prep_id, analysis_group, any_value(subsample_id) AS subsample_id
                          FROM _sub WHERE role = 'primary'
                          GROUP BY prep_id, analysis_group HAVING count(*) = 1) p
          ON p.prep_id = r.prep_id AND p.analysis_group IS NOT DISTINCT FROM r.analysis_group
        WHERE r.role = 'lab_replicate' AND r.parent_lab_sample_id IS NULL""")
    conn.execute("""UPDATE sample_subsamples SET parent_subsample_id = p.parent
        FROM _parents p WHERE sample_subsamples.subsample_id = p.subsample_id""")
    n = conn.execute("SELECT count(*), count(*) FILTER (WHERE prep_role IS NULL) FROM sample_subsamples").fetchone()
    report.append(("ism", "preparations", conn.execute("SELECT count(*) FROM sample_preparations").fetchone()[0], None))
    report.append(("ism", "aliquots", n[0], None))
    if n[1]:
        report.append(("ism", "subsample_role_unknown", n[1],
                       "several aliquots of one sample and group, none named primary: "
                       "give prep_role in the supplement"))
    n_unparsed_codes = conn.execute("""SELECT count(DISTINCT prep_method_code) FROM sample_subsamples
        WHERE prep_method_code IS NOT NULL AND ism_prep_code IS NULL AND extraction_method IS NULL""").fetchone()[0]
    report.append(("ism", "prep_method_unclassified", n_unparsed_codes,
                   "PREP_METHOD codes no prep_method_rules entry classifies"))
    for c in ("drying_method", "disaggregation", "milling"):
        report += _unmapped_report(conn, "sample_preparations", c)
    report += _unmapped_report(conn, "sample_subsamples", "splitting_method")
    return report


def _result_aliquots(conn):
    conn.execute("DELETE FROM result_aliquots")
    conn.execute("""INSERT INTO result_aliquots
        SELECT m.result_id, m.subsample_id, ss.prep_role, sp.particle_size_fraction,
               ss.prep_role = 'lab_replicate'
               AND EXISTS (SELECT 1 FROM sample_subsamples p WHERE p.sample_id = ss.sample_id
                           AND p.prep_role = 'primary')
        FROM v_result_subsamples m
        JOIN sample_subsamples ss ON ss.subsample_id = m.subsample_id
        JOIN sample_preparations sp ON sp.prep_id = ss.prep_id
        WHERE m.match = 'one'""")


def _uncertainty_file(conn, mapping):
    spec = mapping.get("uncertainty_components")
    if not spec:
        return []
    cols = _read_table(conn, spec, mapping, "_unc_file")
    c = spec.get("columns") or {}
    ref = {t: _colref(cols, c, t) for t in ("component", "scope_kind", "scope_id", "analyte",
                                            "method_class", "matrix", "kind", "estimate", "df",
                                            "basis", "includes", "note")}
    conn.execute(f"""INSERT INTO uncertainty_components
        SELECT {_typed(ref['component'], 'VARCHAR')}, {_typed(ref['scope_kind'], 'VARCHAR')},
               {_typed(ref['scope_id'], 'VARCHAR')}, {_typed(ref['analyte'], 'VARCHAR')},
               {_typed(ref['method_class'], 'VARCHAR')}, {_typed(ref['matrix'], 'VARCHAR')},
               {_typed(ref['kind'], 'VARCHAR')}, {_typed(ref['estimate'], 'DOUBLE')},
               {_typed(ref['df'], 'INTEGER')}, {_typed(ref['basis'], 'VARCHAR')},
               CASE WHEN NULLIF(TRIM(CAST({ref['includes']} AS VARCHAR)), '') IS NULL
                    THEN CAST([] AS VARCHAR[])
                    ELSE list_transform(regexp_split_to_array(CAST({ref['includes']} AS VARCHAR), '[;,|]'),
                                        lambda s: trim(s)) END,
               {_typed(ref['note'], 'VARCHAR')}
        FROM _unc_file""")
    n = conn.execute("SELECT count(*) FROM uncertainty_components").fetchone()[0]
    return [("ism", "uncertainty components loaded", n, Path(spec["path"]).name)]


def _depth_units_mixed(conn):
    n = conn.execute("""SELECT count(*) FROM (SELECT location_id FROM samples
        WHERE support_code(support) <> 'discrete' AND depth_units IS NOT NULL
        GROUP BY 1 HAVING count(DISTINCT lower(trim(depth_units))) > 1)""").fetchone()[0]
    return [("ism", "depth_units_mixed", n,
             "DU/SU locations whose composite or ISM samples mix depth units")]


def load_ism(conn, mapping, exprs, staging_cols=None):
    """Load and check a project's ISM records; returns the load_report rows."""
    conn.execute("DELETE FROM load_report WHERE category IN ('ism', 'support_rule', 'replicate_rule')")
    # has_preparations is written only when true: absent reads as false
    # (has_preparations()), so a dataset with no ISM records keeps its settings.
    conn.execute("DELETE FROM chemduck_settings WHERE key = 'has_preparations'")
    if not has_ism(mapping):
        return []
    print("\n--- ISM records ---")
    vocab = Vocab(conn, mapping)
    _settings(conn, mapping)
    report = _parents_from_source(conn, mapping, exprs)
    polys, r = _geometries(conn, mapping)
    report += r
    report += _field_design(conn, mapping, exprs, vocab)
    report += _support_rules(conn, mapping, exprs)
    report += _replicate_rules(conn, mapping, exprs)
    report += _increments(conn, mapping, polys, vocab)
    report += _preparations(conn, mapping, exprs, vocab)
    _result_aliquots(conn)
    report += _uncertainty_file(conn, mapping)
    if conn.execute("SELECT count(*) > 0 FROM sample_preparations").fetchone()[0]:
        conn.execute("INSERT INTO chemduck_settings VALUES ('has_preparations', 'true')")
    report += [("ism", item, int(n or 0), detail)
               for item, n, detail in conn.execute("SELECT item, n, detail FROM v_ism_checks").fetchall()]
    report += _depth_units_mixed(conn)
    _report(conn, report)
    for category, item, n, _ in report:
        if n:
            print(f"  {category}: {item}: {n:,}")
    return report
