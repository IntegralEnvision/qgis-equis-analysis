"""Measure what makes a pivoted (one row per sample) result set hard.

    python tools/measure_pivot.py [--root data/facility_a]

A pivot puts each sample on one row with one column per variable. That only
works if a (sample, variable) cell holds ONE value, and every way it can hold
more than one is a decision the pivot has to make, or a number it silently
makes up. This counts each of those ways on a real facility, so the design in
docs/pivot-design.md argues from measurements rather than from what the data
"should" look like.

Reads the published parquet directly with DuckDB. No QGIS, no network.
"""
import argparse
import glob
import os
import sys
import time

try:
    import duckdb
except ImportError:
    sys.exit("needs duckdb (pip install duckdb)")


# The value rule the store uses, reduced to what these counts need: a
# non-detect carries its limit, never a measured number (CLAUDE.md, the first
# domain rule). detected is DETECT_FLAG, defaulting to 'detected' when a value
# exists and no flag says otherwise.
RESULTS = """
CREATE OR REPLACE VIEW r AS
SELECT
    SYS_SAMPLE_CODE                         AS sample,
    SYS_LOC_CODE                            AS loc,
    upper(coalesce(SAMPLE_TYPE_CODE, ''))   AS stype,
    PARENT_SAMPLE_CODE                      AS parent,
    SAMPLE_DATE                             AS sdate,
    START_DEPTH                             AS d0,
    END_DEPTH                               AS d1,
    matrix                                  AS matrix,
    upper(coalesce(FRACTION, ''))           AS fraction,
    coalesce(nullif(CAS_RN, ''), 'name:' || CHEMICAL_NAME) AS cas,
    CHEMICAL_NAME                           AS chem,
    coalesce(ANALYTIC_METHOD, '')           AS method,
    coalesce(TEST_TYPE, '')                 AS test_type,
    DILUTION_FACTOR                         AS dilution,
    coalesce(COLUMN_NUMBER, '')             AS col,
    upper(coalesce(REPORTABLE_RESULT, ''))  AS reportable,
    coalesce(RESULT_TYPE_CODE, '')          AS rtype,
    coalesce(REPORT_RESULT_UNIT, RESULT_UNIT, '') AS unit,
    upper(coalesce(DETECT_FLAG, 'Y')) <> 'N' AS detected,
    coalesce(RESULT_NUMERIC, REPORT_RESULT_VALUE,
             REPORTING_DETECTION_LIMIT, METHOD_DETECTION_LIMIT) AS value
FROM read_parquet({files}, hive_partitioning = true)
"""


def section(title):
    print("\n## " + title)


def show(con, sql, limit=None):
    rel = con.sql(sql)
    rows = rel.fetchall()
    cols = rel.columns
    if limit:
        rows = rows[:limit]
    widths = [max(len(str(c)), *(len(_fmt(r[i])) for r in rows)) if rows
              else len(str(c)) for i, c in enumerate(cols)]
    print("  " + "  ".join(str(c).ljust(w) for c, w in zip(cols, widths)))
    for r in rows:
        print("  " + "  ".join(_fmt(v).ljust(w) for v, w in zip(r, widths)))
    return rows


def _fmt(v):
    if isinstance(v, float):
        return "%.1f" % v if abs(v) >= 10 else "%.3g" % v
    if isinstance(v, int):
        return format(v, ",d")
    return str(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.join("data", "facility_a"))
    args = ap.parse_args()
    files = sorted(glob.glob(os.path.join(args.root, "matrix=*", "fraction=*",
                                          "*.parquet")))
    if not files:
        sys.exit("no partitions under %s" % args.root)
    con = duckdb.connect()
    con.sql(RESULTS.format(files=[f.replace("\\", "/") for f in files]))
    t0 = time.time()
    print("# Pivot measurements: %s, %s rows" % (
        args.root, format(con.sql("SELECT count(*) FROM r").fetchone()[0], ",d")))

    # A variable is (analyte, fraction): dissolved and total are different
    # quantities and never share a column (CLAUDE.md). The cell is
    # (sample, variable).
    con.sql("""
        CREATE OR REPLACE TEMP TABLE cells AS
        SELECT sample, cas, fraction, matrix,
               count(*)                       AS n,
               count(DISTINCT method)         AS methods,
               count(DISTINCT test_type)      AS test_types,
               count(DISTINCT dilution)       AS dilutions,
               count(DISTINCT col)            AS columns_,
               count(DISTINCT unit)           AS units,
               count(DISTINCT detected)       AS detect_states,
               sum(CASE WHEN reportable = 'Y' THEN 1 ELSE 0 END) AS reportable_y,
               min(value) AS vmin, max(value) AS vmax
        FROM r GROUP BY ALL
    """)

    section("1. Cells holding more than one result")
    show(con, """
        SELECT count(*) AS cells,
               sum(CASE WHEN n > 1 THEN 1 ELSE 0 END) AS multi,
               round(100.0 * sum(CASE WHEN n > 1 THEN 1 ELSE 0 END) / count(*), 2) AS pct_multi,
               max(n) AS worst
        FROM cells""")
    print("  by matrix:")
    show(con, """
        SELECT matrix, count(*) AS cells,
               sum(CASE WHEN n > 1 THEN 1 ELSE 0 END) AS multi,
               round(100.0 * sum(CASE WHEN n > 1 THEN 1 ELSE 0 END) / count(*), 2) AS pct
        FROM cells GROUP BY ALL ORDER BY multi DESC""")

    section("2. Why: what differs between the rows of a multi-result cell")
    show(con, """
        SELECT
          sum(CASE WHEN methods > 1 THEN 1 ELSE 0 END)     AS differ_method,
          sum(CASE WHEN test_types > 1 THEN 1 ELSE 0 END)  AS differ_test_type,
          sum(CASE WHEN dilutions > 1 THEN 1 ELSE 0 END)   AS differ_dilution,
          sum(CASE WHEN columns_ > 1 THEN 1 ELSE 0 END)    AS differ_column,
          sum(CASE WHEN units > 1 THEN 1 ELSE 0 END)       AS differ_unit,
          sum(CASE WHEN detect_states > 1 THEN 1 ELSE 0 END) AS disagree_detect,
          sum(CASE WHEN methods = 1 AND test_types = 1 AND dilutions <= 1
                    AND columns_ = 1 AND units = 1 THEN 1 ELSE 0 END) AS identical_keys
        FROM cells WHERE n > 1""")
    print("  value spread inside multi-result cells (detected-only cells, max/min):")
    show(con, """
        SELECT
          sum(CASE WHEN vmin > 0 AND vmax / vmin >= 2 THEN 1 ELSE 0 END)  AS ratio_ge_2x,
          sum(CASE WHEN vmin > 0 AND vmax / vmin >= 10 THEN 1 ELSE 0 END) AS ratio_ge_10x,
          max(CASE WHEN vmin > 0 THEN vmax / vmin END)                     AS worst_ratio
        FROM cells WHERE n > 1 AND detect_states = 1""")

    print("  cells identical on method/test/dilution/column/unit - what DOES differ:")
    show(con, """
        WITH k AS (SELECT sample, cas, fraction FROM cells
                   WHERE n > 1 AND methods = 1 AND test_types = 1
                     AND dilutions <= 1 AND columns_ = 1 AND units = 1),
        x AS (
          SELECT p.SYS_SAMPLE_CODE AS sample,
                 coalesce(nullif(p.CAS_RN, ''), 'name:' || p.CHEMICAL_NAME) AS cas,
                 upper(coalesce(p.FRACTION, '')) AS fraction,
                 count(DISTINCT p.LAB_SDG) AS sdg,
                 count(DISTINCT p.ANALYSIS_DATE) AS adate,
                 count(DISTINCT p.LAB_SAMPLE_ID) AS labid,
                 count(DISTINCT p.PREP_METHOD) AS prep,
                 count(DISTINCT p.LEACHATE_METHOD) AS leach,
                 count(DISTINCT p.TEST_ID) AS test_id,
                 count(DISTINCT coalesce(p.RESULT_NUMERIC, p.REPORT_RESULT_VALUE)) AS vals
          FROM read_parquet({files}, hive_partitioning = true) p
          GROUP BY ALL)
        SELECT count(*) AS cells,
               sum(CASE WHEN sdg > 1 THEN 1 ELSE 0 END)   AS differ_sdg,
               sum(CASE WHEN adate > 1 THEN 1 ELSE 0 END) AS differ_analysis_date,
               sum(CASE WHEN labid > 1 THEN 1 ELSE 0 END) AS differ_lab_id,
               sum(CASE WHEN prep > 1 THEN 1 ELSE 0 END)  AS differ_prep,
               sum(CASE WHEN leach > 1 THEN 1 ELSE 0 END) AS differ_leachate,
               sum(CASE WHEN test_id > 1 THEN 1 ELSE 0 END) AS differ_test_id,
               sum(CASE WHEN vals > 1 THEN 1 ELSE 0 END)  AS differ_value
        FROM x JOIN k USING (sample, cas, fraction)
    """.format(files=[f.replace("\\", "/") for f in files]))

    section("2b. A third kind of absence: results the validator rejected")
    show(con, """
        SELECT count(*) AS rows,
               sum(CASE WHEN regexp_matches(upper(coalesce(VALIDATOR_QUALIFIERS, '') || ' '
                         || coalesce(INTERPRETED_QUALIFIERS, '')), '(^|[^A-Z])R([^A-Z]|$)')
                   THEN 1 ELSE 0 END) AS qualified_R,
               sum(CASE WHEN coalesce(APPROVAL_CODE, '') <> '' THEN 1 ELSE 0 END) AS with_approval_code
        FROM read_parquet({files}, hive_partitioning = true)
    """.format(files=[f.replace("\\", "/") for f in files]))

    section("3. Does EQuIS's own REPORTABLE_RESULT pick one row per cell?")
    show(con, """
        SELECT reportable, count(*) AS rows FROM r GROUP BY ALL ORDER BY rows DESC""")
    show(con, """
        SELECT
          sum(CASE WHEN n > 1 AND reportable_y = 1 THEN 1 ELSE 0 END) AS multi_with_exactly_one_Y,
          sum(CASE WHEN n > 1 AND reportable_y > 1 THEN 1 ELSE 0 END) AS multi_with_several_Y,
          sum(CASE WHEN n > 1 AND reportable_y = 0 THEN 1 ELSE 0 END) AS multi_with_no_Y
        FROM cells""")
    print("  TEST_TYPE values:")
    show(con, "SELECT test_type, count(*) AS rows FROM r GROUP BY ALL ORDER BY rows DESC", 8)

    section("4. Sample identity and QC")
    show(con, """
        SELECT stype, count(DISTINCT sample) AS samples,
               count(DISTINCT CASE WHEN parent IS NOT NULL AND parent <> '' THEN sample END) AS with_parent
        FROM r GROUP BY ALL ORDER BY samples DESC""", 10)
    print("  samples spanning more than one matrix:")
    show(con, """
        SELECT count(*) AS samples FROM (
            SELECT sample FROM r GROUP BY sample HAVING count(DISTINCT matrix) > 1)""")
    print("  (location, date) holding more than one normal sample, i.e. depth intervals or splits:")
    show(con, """
        SELECT matrix, count(*) AS loc_dates,
               sum(CASE WHEN k > 1 THEN 1 ELSE 0 END) AS with_several_samples,
               max(k) AS worst
        FROM (SELECT matrix, loc, sdate, count(DISTINCT sample) AS k
              FROM r WHERE stype = 'N' GROUP BY ALL)
        GROUP BY ALL ORDER BY loc_dates DESC""")

    section("5. Identity of a variable: one name per CAS?")
    show(con, """
        SELECT
          (SELECT count(*) FROM (SELECT cas FROM r GROUP BY cas HAVING count(DISTINCT chem) > 1)) AS cas_with_several_names,
          (SELECT count(*) FROM (SELECT chem FROM r GROUP BY chem HAVING count(DISTINCT cas) > 1)) AS names_with_several_cas,
          (SELECT count(DISTINCT cas) FROM r WHERE cas LIKE 'name:%') AS analytes_without_cas""")

    section("6. Mixed units inside one variable (analyte, matrix, fraction)")
    show(con, """
        SELECT count(*) AS variables,
               sum(CASE WHEN u > 1 THEN 1 ELSE 0 END) AS mixed_unit
        FROM (SELECT cas, matrix, fraction, count(DISTINCT unit) AS u
              FROM r GROUP BY ALL)""")

    section("7. Coverage: how fast complete cases vanish as variables are added")
    # The most-measured variables in water, normal samples only. A
    # multivariate view needs samples carrying ALL k of them.
    for matrix, fraction in (("WS", "T"), ("WS", "D"), ("SE", "N")):
        top = [row[0] for row in con.sql("""
            SELECT cas FROM r WHERE matrix = ? AND fraction = ? AND stype = 'N'
            GROUP BY cas ORDER BY count(DISTINCT sample) DESC LIMIT 12""",
            params=[matrix, fraction]).fetchall()]
        if len(top) < 2:
            continue
        samples = con.sql("""SELECT count(DISTINCT sample) FROM r
                             WHERE matrix = ? AND fraction = ? AND stype = 'N'""",
                          params=[matrix, fraction]).fetchone()[0]
        print("  %s/%s: %s normal samples; complete cases over the top-k variables:"
              % (matrix, fraction, format(samples, ",d")))
        out = []
        for k in (1, 2, 3, 5, 8, 12):
            if k > len(top):
                break
            n = con.sql("""
                SELECT count(*) FROM (
                  SELECT sample FROM r
                  WHERE matrix = ? AND fraction = ? AND stype = 'N' AND cas IN ({})
                  GROUP BY sample HAVING count(DISTINCT cas) = ?)
            """.format(",".join("?" * k)), params=[matrix, fraction] + top[:k] + [k]).fetchone()[0]
            out.append("k=%d: %s" % (k, format(n, ",d")))
        print("    " + ", ".join(out))

    section("8. Non-detects in pairs: how much of a scatter sits on limits")
    # The two most co-measured variables in WS/T.
    pair = [row[0] for row in con.sql("""
        SELECT cas FROM r WHERE matrix='WS' AND fraction='T' AND stype='N'
        GROUP BY cas ORDER BY count(DISTINCT sample) DESC LIMIT 2""").fetchall()]
    names = dict(con.sql("SELECT cas, any_value(chem) FROM r WHERE cas IN (?, ?) GROUP BY cas",
                         params=pair).fetchall())
    print("  pair: %s x %s (WS/T)" % (names.get(pair[0]), names.get(pair[1])))
    # A sample counts as detected for a variable if ANY of its rows detected;
    # that is generous to detection, which is the point - the counts below
    # are a floor on how much of the scatter sits on limits.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE s AS
        SELECT sample, cas, bool_or(detected) AS det
        FROM r WHERE matrix='WS' AND fraction='T' AND stype='N' AND cas IN (?, ?)
        GROUP BY ALL""", pair)
    show(con, """
        SELECT
          sum(CASE WHEN a.det AND b.det THEN 1 ELSE 0 END)         AS both_detected,
          sum(CASE WHEN a.det AND NOT b.det THEN 1 ELSE 0 END)     AS x_only,
          sum(CASE WHEN NOT a.det AND b.det THEN 1 ELSE 0 END)     AS y_only,
          sum(CASE WHEN NOT a.det AND NOT b.det THEN 1 ELSE 0 END) AS neither
        FROM s a JOIN s b ON a.sample = b.sample
        WHERE a.cas = '%s' AND b.cas = '%s'""" % (pair[0], pair[1]))

    section("9. Cost of the pivot itself")
    t = time.time()
    n = con.sql("""
        SELECT count(*) FROM (
          PIVOT (SELECT sample, cas, value FROM r
                 WHERE matrix='WS' AND fraction='T' AND stype='N')
          ON cas USING first(value) GROUP BY sample)""").fetchone()[0]
    width = con.sql("""SELECT count(DISTINCT cas) FROM r
                       WHERE matrix='WS' AND fraction='T' AND stype='N'""").fetchone()[0]
    print("  full WS/T pivot: %s samples x %d variables in %.2f s"
          % (format(n, ",d"), width, time.time() - t))
    print("\n(total %.1f s)" % (time.time() - t0))


if __name__ == "__main__":
    main()
