"""Find QC and sub-preparations hidden in sample-code suffixes, and measure them.

    python tools/survey_sample_suffixes.py [--root data/facility_a] [--out data/facility_a/qc_review]

Writes `suffix_pairs.csv` (one row per child sample) and prints the family
summary that docs the situation. Background: at Facility A every sample is typed
N with no PARENT_SAMPLE_CODE, so replicates, duplicates and separate
preparations survive only as suffixes on a primary's code. This finds them
by structure and then classifies them by their VALUES, so the result does
not rest on guessing what a suffix means.

A CHILD is a sample whose code is another sample's code (its PRIMARY) plus a
suffix, at the same location, exact sample datetime and matrix. The primary
must end at a separator ("_" or "-"), and the longest such primary wins - so
X_PT_R21 is a child of X_PT, not of X_PT_R2.

Agreement is the relative percent difference (RPD) on analytes DETECTED in
both, per analyte and fraction: |a - b| / mean(a, b) x 100.
"""
import argparse
import csv
import glob
import os
import sys

try:
    import duckdb
except ImportError:
    sys.exit("needs duckdb")

# The plugin's rule, IMPORTED rather than copied, so the CSV's "excluded by
# the plugin" column cannot drift from what the pivot actually does.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from equis_loader.pivot import field_qc_sql  # noqa: E402


def classify(family):
    """A first reading of a family, from the measurements in the docs."""
    if family in ("_R#", "-R#") or family.endswith("_R#"):
        return "replicate"
    if family == "_BD":
        return "duplicate (as decided 2026-10-09; meaning unconfirmed)"
    if family in ("_SQ#", "_PC", "_RD", "_TR", "_PT"):
        return "separate preparation (not a duplicate; left in)"
    return "unclassified"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.join("data", "facility_a"))
    ap.add_argument("--out", default=os.path.join("data", "facility_a", "qc_review"))
    args = ap.parse_args()
    files = [f.replace("\\", "/") for f in glob.glob(
        os.path.join(args.root, "matrix=*", "fraction=*", "*.parquet"))]
    if not files:
        sys.exit("no parquet under %s" % args.root)
    con = duckdb.connect()
    con.sql(f"""CREATE TABLE s AS
        SELECT DISTINCT SYS_SAMPLE_CODE AS code, SYS_LOC_CODE AS loc,
               SAMPLE_DATE AS sdate, matrix,
               upper(coalesce(SAMPLE_TYPE_CODE, '')) AS stype,
               coalesce(PARENT_SAMPLE_CODE, '') AS parent_code
        FROM read_parquet({files}, hive_partitioning = true)""")
    con.sql(f"""CREATE TABLE r AS
        SELECT SYS_SAMPLE_CODE AS code, upper(coalesce(FRACTION, '')) AS frac,
               CAS_RN AS cas, max(RESULT_NUMERIC) AS v
        FROM read_parquet({files}, hive_partitioning = true)
        WHERE upper(coalesce(DETECT_FLAG, '')) = 'Y' AND RESULT_NUMERIC > 0
        GROUP BY ALL""")
    con.sql("""CREATE TABLE kids AS
        SELECT child, primary_code, loc, sdate, matrix, suffix FROM (
          SELECT c.code AS child, p.code AS primary_code, c.loc, c.sdate,
                 c.matrix, substr(c.code, length(p.code) + 1) AS suffix,
                 row_number() OVER (PARTITION BY c.code
                                    ORDER BY length(p.code) DESC) AS k
          FROM s c JOIN s p
            ON c.loc = p.loc AND c.sdate = p.sdate AND c.matrix = p.matrix
           AND c.code <> p.code AND starts_with(c.code, p.code)
           AND substr(c.code, length(p.code) + 1, 1) IN ('_', '-'))
        WHERE k = 1""")
    con.sql(r"""CREATE TABLE fam AS
        SELECT *, regexp_replace(suffix, '[0-9]+$', '#') AS fam FROM kids""")
    con.sql("""CREATE TABLE agree AS
        SELECT x.child, count(*) AS shared,
               median(abs(a.v - b.v) / ((a.v + b.v) / 2) * 100) AS med_rpd,
               avg(CASE WHEN abs(a.v - b.v) / ((a.v + b.v) / 2) * 100 <= 50
                        THEN 1 ELSE 0 END) * 100 AS pct50
        FROM fam x JOIN r a ON a.code = x.child
        JOIN r b ON b.code = x.primary_code AND b.cas = a.cas AND b.frac = a.frac
        GROUP BY x.child""")
    con.sql("CREATE TABLE codes AS SELECT DISTINCT code FROM s")
    con.sql("CREATE TABLE plugin_qc AS %s" % field_qc_sql("codes", column="code"))
    rows = con.sql("""
        SELECT f.matrix, f.loc, f.sdate, f.primary_code, f.child, f.suffix, f.fam,
               coalesce(a.shared, 0) AS shared, round(a.med_rpd, 1),
               round(a.pct50, 0),
               f.child IN (SELECT code FROM plugin_qc) AS plugin_excludes
        FROM fam f LEFT JOIN agree a ON a.child = f.child
        ORDER BY f.matrix, f.fam, f.loc, f.sdate, f.child""").fetchall()

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "suffix_pairs.csv")
    with open(path, "w", newline="", encoding="utf-8") as handle:
        out = csv.writer(handle)
        out.writerow(["matrix", "location", "sample_date", "primary_code",
                      "child_code", "suffix", "family", "assessment",
                      "shared_detected_analytes", "median_rpd_pct",
                      "pct_analytes_within_50pct_rpd",
                      "excluded_by_plugin_replicate_rule"])
        for m, loc, sdate, prim, child, suffix, fam, shared, rpd, p50, excl in rows:
            out.writerow([m, loc, sdate, prim, child, suffix, fam, classify(fam),
                          shared, "" if rpd is None else rpd,
                          "" if p50 is None else int(p50),
                          "yes" if excl else "no"])
    print("wrote %d children to %s" % (len(rows), path))

    print("\nsample types:", con.sql("SELECT stype, count(*) FROM s GROUP BY 1").fetchall())
    print("rows with a PARENT_SAMPLE_CODE:",
          con.sql("SELECT count(*) FROM s WHERE parent_code <> ''").fetchone()[0])
    print("\n| family | children | matrices | share an analyte | median RPD | within 50% | plugin excludes | assessment |")
    print("|---|---|---|---|---|---|---|---|")
    for fam, n, mats, with_shared, med, p50, excl in con.sql(f"""
        SELECT f.fam, count(*), string_agg(DISTINCT f.matrix, ', ' ORDER BY f.matrix),
               count(a.child), round(median(a.med_rpd), 0),
               round(avg(a.pct50), 0),
               sum(CASE WHEN f.child IN (SELECT code FROM plugin_qc)
                        THEN 1 ELSE 0 END)
        FROM fam f LEFT JOIN agree a ON a.child = f.child
        GROUP BY f.fam ORDER BY count(*) DESC""").fetchall():
        print("| `%s` | %s | %s | %s | %s | %s | %s | %s |" % (
            fam, format(n, ","), mats, format(with_shared, ","),
            "-" if med is None else "%d%%" % med,
            "-" if p50 is None else "%d%%" % p50, format(excl, ","),
            classify(fam)))


if __name__ == "__main__":
    main()
