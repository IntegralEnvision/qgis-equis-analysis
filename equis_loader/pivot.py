"""One row per sample, four columns per analyte: the shape multivariate views read.

docs/pivot-design.md is the specification; this is it in SQL. Every rule
below is there with the measurement that justified it. In short:

  * a ROW is a normal field sample (SYS_SAMPLE_CODE). QC samples are left
    out: a field duplicate is not an independent sample and would
    double-weight its parent's location in every correlation;
  * a VARIABLE is (analyte, fraction) in one unit. Method and lab do not
    split it unless asked (`split`);
  * a CELL holding several results resolves to ONE by a stated rule -
    initial over replicate, detected over non-detect, then higher - which
    is the store's own `maximum` ordering, so a pivot cell and a mapped
    maximum cannot pick different rows. How many cells needed it is
    counted per variable, never hidden;
  * every analyte is `_value`, `_detect`, `_qualifier`, `_detectlimit`.
    Detection is DETECT_FLAG as recorded, never inferred from qualifiers.

Store-only by design: the pivot reads the store's one view, v_results, so
the row scope (dates, QC) and the non-detect value ladder apply exactly as
they do to the map. There is no ReportTable equivalent, and no second
implementation of either rule.

No QGIS imports, so this is testable with a plain interpreter.
"""

import hashlib
import itertools
import json
import re

from . import report_data as rd

# How many pivot tables one store keeps before dropping the oldest. Each
# build makes a NEW table (see _table_name), and a session that redraws a
# scatter many times should not grow the database without bound.
KEEP_TABLES = 8

# What the cell rule prefers, first to last. A blank or missing TEST_TYPE is
# treated as initial: a report without the column has no replicates to
# prefer anything over.
_INITIAL = ("INITIAL", "")

_counter = itertools.count(1)

# FIELD DUPLICATES AND REPLICATES are not plotted (decided 2026-10-09).
#
# A STOPGAP FOR DATA THAT DOES NOT MARK ITS QC. The right signal is EQuIS's
# own: SAMPLE_TYPE_CODE (FD, FR, ...) with a PARENT_SAMPLE_CODE, and normal-
# only already excludes any sample typed that way. Some loads skipped it -
# at Facility A every sample is typed N with no parent - leaving QC visible only
# as a suffix on the primary's code. This recognises the few suffixes that
# measured as duplicates or replicates, and nothing else. It is deliberately
# NOT grown into a catalogue of one dataset's conventions: when the data is
# typed properly this does nothing, and when it is not, the fix belongs in
# the data (see data/facility_a/qc_review/README.md).
#
# Recognised (child = primary's code + suffix):
#   _R<n>, -R<n>  replicates, n >= 2: there is no R1, the un-suffixed code
#                 is replicate 1. 7,300 + 5 at Facility A, median RPD 35% / 6%.
#   _BD           a duplicate (as decided; meaning unconfirmed). 508,
#                 median RPD 30%, 78% of analytes within 50%.
# And only when the code WITHOUT the suffix is itself a sample, so a code
# that merely ends in "_R2" or "_BD" with no primary behind it stays.
#
# NOT "same place, same time": about half of Facility A's multi-sample events
# are different samples taken together (water-column positions _F/_M/_S,
# sediment sub-samples _A.._D). Other suffixes - _SQ, _PC, _RD, _TR, _PT -
# measured as separate preparations, not duplicates, and are left in as
# ordinary samples by decision.
QC_SUFFIX = "(_R[0-9]+|-R[0-9]+|_BD)$"


def field_qc_sql(codes, column="sample"):
    """SQL selecting the field duplicate/replicate codes among `codes`.

    `codes` is a relation (table name or parenthesised query) with one
    distinct code per row in `column`. The single definition of the rule:
    the pivot and tools/survey_sample_suffixes.py both use it.
    """
    return """
        SELECT {c} AS code FROM (
          SELECT {c}, regexp_extract({c}, '{p}', 1) AS suf FROM {codes})
        WHERE suf <> ''
          AND (suf = '_BD'
               OR TRY_CAST(regexp_extract(suf, '([0-9]+)$', 1) AS INTEGER) >= 2)
          AND left({c}, length({c}) - length(suf)) IN (SELECT {c} FROM {codes})
    """.format(c=column, p=QC_SUFFIX, codes=codes)


# The set is a property of the DATA, not of any scope, so it is computed once
# per store from the unscoped `results` relation and joined against after.
# Asked per pivot as a subquery it re-scanned all 3.4 M rows every time:
# measured 2026-10-09, +1.3 s (Mn x Pb) and +3.0 s (As x Cr SO) per build.
_REPLICATES_TABLE = "pivot_field_qc"
_IS_FIELD_REPLICATE = "(sample IN (SELECT code FROM %s))" % _REPLICATES_TABLE


def _ensure_replicates(store):
    """Build the store's field duplicate/replicate set once. Returns how many."""
    if getattr(store, "_field_replicates", None) is not None:
        return store._field_replicates
    store.con.execute("""
        CREATE OR REPLACE TEMP TABLE codes_for_qc AS
        SELECT DISTINCT sample FROM results WHERE coalesce(trim(sample), '') <> ''
    """)
    store.con.execute("CREATE OR REPLACE TEMP TABLE %s AS %s"
                      % (_REPLICATES_TABLE, field_qc_sql("codes_for_qc")))
    store._field_replicates = store.con.execute(
        "SELECT count(*) FROM %s" % _REPLICATES_TABLE).fetchone()[0]
    return store._field_replicates


def not_field_replicate(store):
    """SQL true for a `sample` that is NOT a field duplicate or replicate.

    For a caller counting samples the way a pivot will (analysis.py's
    matrix list): SO is 112 samples to a scatter, not the 839 its codes
    number with replicates in. The rule stays here, in one place.
    """
    _ensure_replicates(store)
    return "NOT " + _IS_FIELD_REPLICATE


# What _build_from_matched reads of each matched row. See build_pivot.
_MATCHED_COLUMNS = (
    "sample", "sample_type", "location", "matrix", "fraction", "unit",
    "sample_date", "start_depth", "end_depth", "value_full", "detected",
    "row_id", "detect", "detect_limit", "qual_interpreted", "qual_lab",
    "test_type")


class PivotError(ValueError):
    """The pivot cannot be built honestly; the message says why and what to do."""


class Variable(object):
    """One analyte to pivot: (chemical, CAS, fraction).

    `fraction` None means "whatever this analyte has" - which is refused if
    that turns out to be dissolved AND total, because the two differ by up
    to ~29x for the same water on the same day.
    """

    def __init__(self, chemical, cas_rn=None, fraction=rd.ANY_FRACTION):
        self.chemical = chemical
        self.cas_rn = cas_rn or None
        self.fraction = fraction

    def key(self):
        return [self.chemical, self.cas_rn, self.fraction]

    def __repr__(self):
        return "Variable(%r, %r, %r)" % (self.chemical, self.cas_rn,
                                         self.fraction)


class Pivot(object):
    """What a build produced: the table, and everything a view must state."""

    def __init__(self, table, columns, rows, complete, excluded, empty=()):
        self.table = table
        # One entry per pivoted column group, in order: prefix, chemical,
        # cas_rn, fraction, unit, split, and the counts a caption needs.
        self.columns = columns
        self.rows = rows
        # Rows carrying a cell for EVERY column group.
        self.complete = complete
        # {"qc_rows": n, "no_sample_rows": n, "field_replicates": samples}
        # - what was left out, and why.
        self.excluded = excluded
        # Variables asked for that have no cell at all in scope. Said, not
        # silently dropped: an axis that vanished is a question unanswered.
        self.empty = list(empty)

    def column(self, index):
        return self.columns[index]


def slug(text):
    """A safe identifier fragment: lower-case, non-alphanumerics to `_`."""
    out = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    if not out:
        out = "x"
    if out[0].isdigit():
        out = "a_" + out
    return out


def build_pivot(store, variables, matrix=rd.ALL_MATRICES, split=None,
                replicates="exclude", on_mixed="raise"):
    """Pivot `variables` from `store` into a new table. Returns a Pivot.

    `split` is None (the default: method and lab do not split a variable),
    "method" or "lab". `replicates` is "exclude" (the default: field
    duplicates and replicates are not plotted, see QC_SUFFIX) or "include".
    Raises PivotError when the pivot would be dishonest: no sample
    identity, mixed units in a variable, or dissolved beside total - or,
    with `on_mixed="skip"`, leaves such a variable out and lists it in
    Pivot.skipped, which is what an every-analyte pivot needs: one mixed
    analyte must not cost the other two hundred.
    """
    if replicates not in ("exclude", "include"):
        raise PivotError("Unknown replicates setting %r." % (replicates,))
    if on_mixed not in ("raise", "skip"):
        raise PivotError("Unknown on_mixed setting %r." % (on_mixed,))
    if not variables:
        raise PivotError("Choose at least one analyte to pivot.")
    if split not in (None, "method", "lab"):
        raise PivotError("Unknown split %r." % (split,))
    if "sample" not in store.present:
        raise PivotError(
            "This data has no SYS_SAMPLE_CODE column, so results cannot be "
            "put together by the sample they came from. A pivot needs it.")
    if split and split not in store.present:
        raise PivotError(
            "This data has no %s column to split by."
            % {"method": "ANALYTIC_METHOD", "lab": "LAB_NAME_CODE"}[split])

    normal = ("upper(coalesce(trim(sample_type), '')) = 'N'"
              if "sample_type" in store.present else "TRUE")
    has_sample = "coalesce(trim(sample), '') <> ''"
    split_expr = ("upper(coalesce(trim(%s), ''))" % split) if split else "''"

    if replicates == "exclude":
        _ensure_replicates(store)
    not_replicate = ("NOT " + _IS_FIELD_REPLICATE
                     if replicates == "exclude" else "TRUE")
    excluded = {"qc_rows": 0, "no_sample_rows": 0, "field_replicates": 0}

    # ONE PASS FOR ALL VARIABLES. This used to be a query (and a check, and
    # a count) PER variable, unioned: 3.4 s for soil's 13 analytes, which put
    # an every-analyte water pivot near a minute (2026-10-10). The variables
    # now go into a small table and are JOINED, so the data is matched once.
    # The matching is store._where's, reproduced: by CAS when the variable
    # has one and the data has the column, otherwise by exact name; the
    # fraction case-insensitively, None meaning any. CAS and name matches
    # are two equality joins, so both can hash - one join on a conditional
    # key could not, and would compare every row with every variable.
    serial = next(_counter)
    vars_table, matched = "pv_vars_%d" % serial, "pv_rows_%d" % serial
    by_cas_present = "cas_rn" in store.present
    defs = []
    for index, variable in enumerate(variables):
        fraction = variable.fraction
        if isinstance(fraction, (list, tuple)):
            if len(fraction) > 1:
                raise PivotError("One fraction per variable: %s asked for %s."
                                 % (variable.chemical, "+".join(fraction)))
            fraction = fraction[0] if fraction else rd.ANY_FRACTION
        if fraction is rd.ANY_FRACTION or "fraction" not in store.present:
            fraction_key = None
        else:
            fraction_key = str(fraction or "").strip().upper()
        by_cas = bool(variable.cas_rn) and by_cas_present
        defs.append((index, by_cas,
                     variable.cas_rn if by_cas else (variable.chemical or ""),
                     fraction_key))
    store.con.execute("CREATE TEMP TABLE %s (idx INTEGER, by_cas BOOLEAN, "
                      "key VARCHAR, frac VARCHAR)" % vars_table)
    store.con.execute(
        "INSERT INTO %s SELECT * FROM (VALUES %s)" % (vars_table, ", ".join(
            "(%d, %s, %s, %s)" % (i, "TRUE" if c else "FALSE", _sql_text(k),
                                  "NULL" if f is None else _sql_text(f))
            for i, c, k, f in defs)))
    matrix_clause, matrix_params = store._code_filter("matrix", matrix, "matrix")
    # ONLY THE COLUMNS THE PIVOT READS, not r.*: copying all 30 of the
    # view's columns for water's 2.6 M matched rows took 2.44 s, these 17
    # took 1.42 s (SE: 2.16 s to 1.23 s), with identical pivots - same
    # rows, same hash over every cell (2026-10-11, alternating A/B runs on
    # this laptop). A column added to the pivot's SQL below must be added
    # here; one left out fails loudly, at bind time.
    carried = ", ".join("r." + c for c in _MATCHED_COLUMNS + ((split,) if split else ()))
    joined = []
    for by_cas, column in ((True, "cas_rn"), (False, "chemical")):
        joined.append("""
            SELECT v.idx AS var, {carried}
            FROM v_results r
            JOIN (SELECT * FROM {vt} WHERE {flag}by_cas) v
              ON coalesce(r.{column}, '') = v.key
            WHERE (v.frac IS NULL OR upper(coalesce(r.fraction, '')) = v.frac)
              AND ({matrix})""".format(vt=vars_table, column=column,
                                       carried=carried,
                                       flag="" if by_cas else "NOT ",
                                       matrix=matrix_clause or "TRUE"))
    store.con.execute("CREATE TEMP TABLE %s AS %s" % (
        matched, " UNION ALL ".join(joined)), matrix_params * 2)
    try:
        return _build_from_matched(
            store, variables, matched, matrix, split, split_expr, normal,
            has_sample, not_replicate, replicates, excluded, on_mixed)
    finally:
        store.con.execute("DROP TABLE IF EXISTS %s" % matched)
        store.con.execute("DROP TABLE IF EXISTS %s" % vars_table)


def _build_from_matched(store, variables, matched, matrix, split, split_expr,
                        normal, has_sample, not_replicate, replicates,
                        excluded, on_mixed):
    """The pivot proper, over the rows already matched to their variables."""
    # THE CHECKS, for every variable in one query each: units, fractions,
    # and what is left out. Refusals are taken in variable order, so the
    # FIRST offending variable is the one named, as before.
    scope = "(%s) AND (%s)" % (normal, has_sample)
    units = {}
    for var, unit, count in store.con.execute("""
            SELECT var, coalesce(nullif(trim(unit), ''), '(unit not reported)'),
                   count(*)
            FROM {m} WHERE {scope} GROUP BY 1, 2 ORDER BY 1, 3 DESC
            """.format(m=matched, scope=scope)).fetchall():
        units.setdefault(var, []).append((unit, count))
    fractions = {}
    if "fraction" in store.present:
        for var, code in store.con.execute("""
                SELECT DISTINCT var, upper(coalesce(trim(fraction), ''))
                FROM {m} WHERE {scope}""".format(m=matched, scope=scope)).fetchall():
            fractions.setdefault(var, set()).add(code)
    skipped, keep = [], []
    # The same skips as data, aligned with `skipped`, for a reader that
    # lays them out rather than printing the sentence (the Analysis
    # chooser's warnings list: unit counts, not 16 copies of the advice).
    skipped_detail = []
    for index, variable in enumerate(variables):
        reason = None
        kind = None
        found_units = units.get(index, [])
        reported = [u for u in found_units if u[0] != "(unit not reported)"]
        if len(reported) > 1:
            reason = (
                "%s mixes units (%s). A column holds one unit; narrow the "
                "matrix or fraction, or convert upstream."
                % (variable.chemical, ", ".join("%s: %s" % (u, format(n, ",d"))
                                                for u, n in found_units)))
            kind = "units"
        elif (variable.fraction is rd.ANY_FRACTION
              and {"D", "T"} <= fractions.get(index, set())):
            reason = (
                "%s has dissolved and total results. They differ by up to "
                "~29x for the same water on the same day; choose one fraction."
                % variable.chemical)
            kind = "fractions"
        if reason is None:
            keep.append(index)
        elif on_mixed == "raise":
            raise PivotError(reason)
        else:
            skipped.append((variable, reason))
            skipped_detail.append({"kind": kind, "units": [
                [u, int(n)] for u, n in found_units]})
    kept = "var IN (%s)" % (", ".join(str(i) for i in keep) or "NULL")
    qc, no_sample = store.con.execute("""
        SELECT coalesce(sum(CASE WHEN NOT ({normal}) THEN 1 ELSE 0 END), 0),
               coalesce(sum(CASE WHEN ({normal}) AND NOT ({has_sample})
                                 THEN 1 ELSE 0 END), 0)
        FROM {m} WHERE {kept}
    """.format(normal=normal, has_sample=has_sample, m=matched,
               kept=kept)).fetchone()
    excluded["qc_rows"] = int(qc)
    excluded["no_sample_rows"] = int(no_sample)
    if replicates == "exclude":
        # Counted once per SAMPLE across all variables - a replicate carrying
        # both analytes is one sample left out, not two.
        excluded["field_replicates"] = int(store.con.execute(
            "SELECT count(DISTINCT sample) FROM %s WHERE %s AND (%s) AND %s"
            % (matched, kept, scope, _IS_FIELD_REPLICATE)).fetchone()[0])

    long_sql = """
        SELECT var, {split_expr} AS split, sample, location,
               matrix, fraction, unit, sample_date, start_depth,
               end_depth, value_full AS v, detected, row_id,
               nullif(upper(trim(detect)), '') AS detect_rec,
               detect_limit,
               coalesce(nullif(trim(qual_interpreted), ''),
                        nullif(trim(qual_lab), '')) AS qualifier,
               CASE WHEN upper(coalesce(trim(test_type), '')) IN {initial}
                    THEN 0 ELSE 1 END AS lab_replicate
        FROM {m}
        WHERE {kept} AND {scope} AND {not_replicate}
    """.format(split_expr=split_expr, initial=str(_INITIAL), m=matched,
               kept=kept, scope=scope, not_replicate=not_replicate)
    params = []
    # THE CELL RULE. `detected DESC, v DESC, row_id ASC` is the store's
    # maximum ordering verbatim (store.aggregate); `replicate` goes first
    # because an initial analysis outranks its replicates whatever they say.
    ranked = """
        SELECT *,
               row_number() OVER (PARTITION BY var, split, sample
                                  ORDER BY lab_replicate, detected DESC, v DESC,
                                           row_id ASC) AS rk,
               count(*) OVER (PARTITION BY var, split, sample) AS n
        FROM (%s)
    """ % long_sql
    groups = store.con.execute("""
        SELECT var, split,
               count(*) FILTER (WHERE rk = 1)                      AS cells,
               count(*) FILTER (WHERE rk = 1 AND n > 1)            AS resolved,
               -- A blank flag is UNKNOWN, not a non-detect, even though the
               -- store's `detected` reads it as false. Counted separately.
               count(*) FILTER (WHERE rk = 1 AND NOT detected
                                AND detect_rec IS NOT NULL)        AS nondetects,
               count(*) FILTER (WHERE rk = 1 AND detect_rec IS NULL) AS blank_detect,
               string_agg(DISTINCT nullif(fraction, ''), '+')      AS fractions,
               any_value(unit) FILTER (WHERE coalesce(unit, '') <> '') AS unit
        FROM (%s) GROUP BY var, split ORDER BY var, split
    """ % ranked, params).fetchall()

    columns = _name_columns(variables, groups, split)
    table = _table_name(store, variables, matrix, split)
    # WIDE BY MAP, NOT BY FILTERED AGGREGATE. Each cell was
    # `max(v) FILTER (WHERE var = k)`, four per variable. Fine for two
    # analytes; for every analyte in tailings (644 variables over 28,242
    # rows, so 2,576 filtered aggregates) it ran past five minutes and 18 GB
    # without finishing. DuckDB's own PIVOT took 72 s on the same rows. One
    # map per sample (cell key -> the winning row), read out by key, took
    # 0.26 s with identical answers (2026-10-10, this laptop). A missing key
    # is NULL, which is what an absent cell must be. `m[key]` returns the
    # value itself from DuckDB 1.2 on; older ones return a list, and the
    # field access below then fails at bind time rather than misreading.
    cells = []
    for col in columns:
        key = _sql_text("%d|%s" % (col["var"], col["split"]))
        cells.append("""
            m[{k}].v AS {p}_value, m[{k}].d AS {p}_detect,
            m[{k}].q AS {p}_qualifier, m[{k}].l AS {p}_detectlimit""".format(
            k=key, p=col["prefix"]))
    #
    # A VIEW OVER THE MAPS, NOT A TABLE. Materialising every cell cost 3.6 s
    # of water's 7.2 s (215 columns x 76,972 samples, 2026-10-10) for an
    # every-analyte pivot whose views read two analytes at a time. As a
    # view, a query naming two columns makes DuckDB look up those two, and
    # the rest are never computed. Every chart query pays a map lookup per
    # row it reads instead, which is the cheap side of that trade.
    #
    # And the maps get a statement of their own: built in a subquery of the
    # read-out, tailings took 89 s, the optimiser apparently treating each
    # of 2,512 references to `m` as its own expression. Materialised: 0.2 s.
    maps = _maps_for(table)
    store.con.execute("""
        CREATE TABLE {maps} AS
        SELECT sample,
               any_value(location)    AS location,
               any_value(matrix)      AS matrix,
               any_value(sample_date) AS sample_date,
               any_value(start_depth) AS start_depth,
               any_value(end_depth)   AS end_depth,
               map_from_entries(list((
                   var || '|' || split,
                   struct_pack(v := v, d := detect_rec, q := qualifier,
                               l := detect_limit)))) AS m,
               -- Cells that exist: a detection with no number still has a
               -- flag, and a non-detect always has a limit. One winning row
               -- per cell, so this counts cells, for `complete` below.
               count(*) FILTER (WHERE v IS NOT NULL OR detect_rec IS NOT NULL
                                OR detect_limit IS NOT NULL) AS cells_present
        FROM ({ranked}) WHERE rk = 1
        GROUP BY sample
    """.format(maps=maps, ranked=ranked), params)
    store.con.execute("""
        CREATE VIEW {table} AS
        SELECT sample, location, matrix, sample_date, start_depth, end_depth,
               {cells}
        FROM {maps}
    """.format(table=table, maps=maps,
               cells=",".join(cells) if cells else "NULL AS none"))
    _remember(store, table)

    rows, complete = store.con.execute(
        "SELECT count(*), count(*) FILTER (WHERE cells_present = %d) FROM %s"
        % (len(columns), maps)).fetchone()
    if not columns:
        complete = 0
    found = {c["var"] for c in columns}
    empty = [v for i, v in enumerate(variables)
             if i not in found and all(v is not sv for sv, _ in skipped)]
    pivot = Pivot(table, columns, rows, complete, excluded, empty)
    pivot.skipped = skipped
    pivot.skipped_detail = skipped_detail
    return pivot


def all_variables(store, matrix=rd.ALL_MATRICES):
    """Every (analyte, fraction) with normal samples in `matrix`, as Variables.

    The input to an every-analyte pivot. Each carries its fraction
    explicitly, so dissolved and total are separate variables rather than
    one refused one. Ordered by name, then fraction.
    """
    where, params = store._where(matrix=matrix)
    normal = ("upper(coalesce(trim(sample_type), '')) = 'N'"
              if "sample_type" in store.present else "TRUE")
    rows = store.con.execute("""
        SELECT chemical, nullif(coalesce(cas_rn, ''), ''),
               upper(coalesce(trim(fraction), '')) AS f
        FROM v_results
        WHERE ({where}) AND ({normal}) AND coalesce(chemical, '') <> ''
        GROUP BY 1, 2, 3 ORDER BY lower(chemical), 3
    """.format(where=where, normal=normal), params).fetchall()
    has_fraction = "fraction" in store.present
    return [Variable(chem, cas, f if has_fraction else rd.ANY_FRACTION)
            for chem, cas, f in rows]


KEEP_PAIRS = 4


def pair_table(store, pivot, first, second):
    """Two column groups of `pivot` as a small table of their own. See chart_table."""
    return chart_table(store, pivot, [first, second])


def chart_table(store, pivot, columns):
    """Column groups of `pivot` as a small table of their own. Returns its name.

    For a chart, which asks of the same two columns many times - counts,
    views per class, the brush. Through the pivot's view every reference
    repeats its map lookup: water's Mn x Pb counts took 1.2 s in the page,
    and the draw 3.8 s (2026-10-10). One pass here, and every later query
    reads plain columns. A new name each time, for Mosaic's SQL-keyed
    cache (see _table_name); the last KEEP_PAIRS are kept, so a chart
    still on screen never loses its table to the next one.
    """
    name = "pp_%d" % next(_counter)
    cells = ", ".join("%s_%s" % (c["prefix"], part) for c in columns
                      for part in ("value", "detect", "qualifier", "detectlimit"))
    store.con.execute(
        "CREATE TABLE %s AS SELECT sample, location, matrix, sample_date, "
        "start_depth, end_depth, %s FROM %s" % (name, cells, pivot.table))
    keep_last(store, "_pair_tables", name)
    return name


def keep_last(store, kind, name, keep=KEEP_PAIRS):
    """Track a chart table under `kind`, dropping the oldest past `keep`."""
    made = getattr(store, kind, None)
    if made is None:
        made = []
        setattr(store, kind, made)
    made.append(name)
    while len(made) > keep:
        try:
            store.con.execute("DROP TABLE IF EXISTS %s" % made.pop(0))
        except Exception:  # noqa: BLE001 - housekeeping, never fatal
            pass


def pair_counts(con, pivot, prefix):
    """For each column group: how many samples could be charted with `prefix`.

    `prefix` is one column's prefix, or a list: a ternary's third list
    counts the samples carrying X AND Y AND each candidate.

    A pair is what scatter.js plots - both values AND both detect flags
    known - so a count here is the number of points the scatter would draw
    (before a log axis leaves out values of 0 or below). Returns
    {prefix: count}, zeros included.

    Counted from the map ENTRIES, not the view: the view computes a column
    only when asked for it, and asking for all of them is the cost the view
    exists to avoid - 1.4 s for water's 215 columns against 0.2 s this way,
    with identical counts (2026-10-10).
    """
    keys = {"%d|%s" % (c["var"], c["split"]): c["prefix"] for c in pivot.columns}
    wanted = [prefix] if isinstance(prefix, str) else list(prefix)
    mine = []
    for one in wanted:
        key = next((k for k, p in keys.items() if p == one), None)
        if key is None:
            raise PivotError("%s is not a column of this pivot." % one)
        mine.append(key)
    carrying = " AND ".join(
        "m[{k}].v IS NOT NULL AND m[{k}].d IS NOT NULL".format(k=_sql_text(k))
        for k in mine)
    counts = dict(con.execute("""
        SELECT e.key, count(*) FROM (
            SELECT unnest(map_entries(m)) AS e FROM {maps}
            WHERE {carrying})
        WHERE e.value.v IS NOT NULL AND e.value.d IS NOT NULL
        GROUP BY 1""".format(maps=_maps_for(pivot.table),
                             carrying=carrying)).fetchall())
    return {p: int(counts.get(k, 0)) for k, p in keys.items()}


def _sql_text(value):
    return "'%s'" % str(value).replace("'", "''")


# --------------------------------------------------------------- internals

def _name_columns(variables, groups, split):
    """Column-group names: slug(chemical), plus fraction and split when needed."""
    # The fraction joins the name only when it tells columns apart: Arsenic
    # dissolved beside Arsenic total, not Arsenic beside Lead.
    with_fraction = len({(g[6] or "") for g in groups}) > 1
    used, out = set(), []
    for var, split_value, cells, resolved, nds, blank, fracs, unit in groups:
        variable = variables[var]
        parts = [slug(variable.chemical)]
        if with_fraction and (variable.fraction or fracs):
            parts.append(slug(variable.fraction or fracs))
        if split:
            parts.append(slug(split_value) if split_value else "none")
        base = "_".join(parts)
        name, k = base, 2
        while name in used:
            name, k = "%s_%d" % (base, k), k + 1
        used.add(name)
        out.append({
            "var": var, "prefix": name, "chemical": variable.chemical,
            "cas_rn": variable.cas_rn, "fraction": variable.fraction or fracs,
            "unit": unit, "split": split_value or "", "cells": cells,
            "resolved": resolved, "nondetects": nds, "blank_detect": blank,
            "rule": "initial over replicate; detected over non-detect; then "
                    "higher",
        })
    return out


def _table_name(store, variables, matrix, split):
    """A NEW name for every build.

    Mosaic caches query results keyed on the SQL string. Replacing a table
    under the same name served the first analyte's rows under every later
    analyte's caption (00afe8d, 59328f7). A name that is never reused makes
    a stale cache entry unreachable rather than wrong.
    """
    spec = json.dumps([[v.key() for v in variables], matrix, split,
                       [str(part) for part in store.scope]], sort_keys=True,
                      default=str)
    digest = hashlib.sha1(spec.encode("utf-8")).hexdigest()[:10]
    return "p_%s_%d" % (digest, next(_counter))


def _remember(store, table):
    """Track this store's pivot tables, dropping the oldest past KEEP_TABLES."""
    made = getattr(store, "_pivot_tables", None)
    if made is None:
        made = store._pivot_tables = []
    made.append(table)
    while len(made) > KEEP_TABLES:
        old = made.pop(0)
        try:
            store.con.execute("DROP VIEW IF EXISTS %s" % old)
            store.con.execute("DROP TABLE IF EXISTS %s" % _maps_for(old))
        except Exception:  # noqa: BLE001 - housekeeping, never fatal
            pass


def _maps_for(table):
    """The table of per-sample maps a pivot view reads: p_x_3 -> pm_x_3."""
    return "pm" + table[1:]
