/**
 * Box plots of censored data: Kaplan-Meier quartiles per group, in SQL.
 *
 * WHY NOT ORDINARY QUARTILES. A non-detect carries only the limit it fell
 * below. Substituted at half its limit it becomes a number about the lab's
 * sensitivity, and a box drawn over those numbers puts a median on the
 * page that may itself be a substitution (decided 2026-10-20: censored
 * statistics, not substitution, for every box). Kaplan-Meier uses only
 * what a non-detect actually says - "below L" - and is the estimator
 * Helsel recommends for left-censored environmental data (Statistics for
 * Censored Environmental Data, 2nd ed., ch. 6).
 *
 * THE ESTIMATE, for left-censored data, written without the usual flip:
 *
 *   F(x) = P(X <= x) = product over detected values x_k > x of
 *                      (1 - d_k / n_k)
 *
 * where d_k is the number of detections AT x_k and n_k the number of
 * results known to be <= x_k: every detection at or below it, and every
 * non-detect whose limit is at or below it. A non-detect at L and a
 * detection at L tie, and the non-detect is the smaller ("<L" is below
 * L) - so it is at risk at L but is not an event there.
 *
 * WHAT IT CANNOT SAY. Below the lowest detected value the estimate has no
 * steps: F there is F0, the mass KM puts somewhere under the lowest
 * detection, and it does not say where. A quartile p with p <= F0 is
 * therefore UNDETERMINED - returned as NULL with its flag set, and drawn
 * as unknown rather than at a guess. That is the honest form of the
 * censored box plot: the box is known where the data can place it.
 *
 * QUANTILE CONVENTION. The smallest detected x with F(x) >= p; when F is
 * exactly p there (the curve is flat AT p), the midpoint of x and the next
 * detected value above, as R's survival package does. With nothing
 * censored this is R's quantile type 2, so an all-detected group gives
 * the median a chemist computes by hand: 1,2,3,4 has median 2.5.
 */

const quote = (text) => `'${String(text).replace(/'/g, "''")}'`;

// The equality the midpoint rule needs, on a product of doubles:
// 0.75 * (2/3) is 0.49999999999999994, not 0.5.
const EPS = 1e-9;

function column(prefix, suffix) {
  if (!/^[a-z][a-z0-9_]*$/.test(prefix)) {
    throw new Error(`not a pivot column prefix: ${prefix}`);
  }
  return `${prefix}_${suffix}`;
}

function table(name) {
  if (!/^(pp?|sv|ab)_[a-z0-9_]+$/.test(name)) {
    throw new Error(`not a pivot table: ${name}`);
  }
  return name;
}

/** The groupings a box plot offers, and the SQL each one is. */
export const GROUPS = {
  none: { label: "All samples", sql: "'All samples'" },
  location: { label: "Location", sql: "coalesce(location, '(no location)')" },
  year: {
    label: "Year",
    sql: "coalesce(CAST(year(sample_date) AS VARCHAR), '(undated)')",
  },
};

export const QUARTILES = [["q1", 0.25], ["med", 0.5], ["q3", 0.75]];

/**
 * One row per group: counts, the three KM quartiles (NULL when
 * undetermined, with `<q>_below` true), Tukey whiskers over the DETECTED
 * values, and the largest non-detect limit.
 *
 *  - pivot, prefix  the pivot table and one analyte's column prefix
 *  - detectTrue     the host's detected flag values (store.DETECT_TRUE)
 *  - group          a key of GROUPS
 *  - where          extra predicates, ANDed - the brush, the map's
 *                   selection; each one a complete SQL boolean
 *  - log            fences on log10, as the axis is; values <= 0 cannot
 *                   be placed there and are left out of the fences only
 */
export function kmBoxSql({ pivot, prefix, detectTrue, group = "none",
                           where = [], log = true }) {
  if (!Array.isArray(detectTrue) || !detectTrue.length) {
    throw new Error("kmBoxSql needs the host's detect-true values");
  }
  const value = column(prefix, "value");
  const detect = column(prefix, "detect");
  const truth = `(${detectTrue.map((v) => quote(String(v).toUpperCase())).join(", ")})`;
  return boxSql(table(pivot), value, `upper(${detect}) IN ${truth}`,
                [`${value} IS NOT NULL`, `${detect} IS NOT NULL`, ...where],
                group, log);
}

/**
 * The same, over a linked view's table (linked.js): `value` a column and
 * `detected` a BOOLEAN column already decided from DETECT_FLAG - NULL
 * where the flag was blank, which is unknown and left out, never a
 * non-detect.
 */
export function kmBoxSqlOver({ source, value, detected, group = "none",
                               where = [], log = true }) {
  for (const name of [value, detected]) {
    if (!/^[a-z][a-z0-9_]*$/.test(name)) throw new Error(`not a column: ${name}`);
  }
  if (!/^lk_[0-9]+(_shown)?$/.test(source)) {
    throw new Error(`not a linked table: ${source}`);
  }
  return boxSql(source, value, detected,
                [`${value} IS NOT NULL`, `${detected} IS NOT NULL`, ...where],
                group, log);
}

function boxSql(t, value, detected, where, group, log) {
  const g = GROUPS[group];
  if (!g) throw new Error(`no such grouping: ${group}`);
  const filters = where.filter(Boolean).map((w) => `(${w})`);
  const quartiles = QUARTILES.map(([name, p]) => `
      CASE WHEN F0 >= ${p} - ${EPS} THEN NULL
           ELSE arg_min(CASE WHEN abs(F - ${p}) < ${EPS} AND nxt IS NOT NULL
                             THEN (v + nxt) / 2 ELSE v END, v)
                FILTER (WHERE F >= ${p} - ${EPS}) END AS ${name},
      F0 >= ${p} - ${EPS} AS ${name}_below`).join(",");
  return `
WITH src AS (
  SELECT ${g.sql} AS g, ${value}::DOUBLE AS v, ${detected} AS d
  FROM ${t}
  WHERE ${filters.join(" AND ")}
),
steps AS (
  SELECT g, v, sum(d::INTEGER) AS dk, count(*) AS c FROM src GROUP BY g, v
),
km AS (
  SELECT g, v, dk,
         CASE WHEN dk > 0
              THEN 1 - dk::DOUBLE / sum(c) OVER (PARTITION BY g ORDER BY v
                                                 ROWS UNBOUNDED PRECEDING)
              ELSE 1.0 END AS f
  FROM steps
),
cdf AS (
  SELECT g, v, dk,
         coalesce(product(f) OVER (PARTITION BY g ORDER BY v DESC
                  ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 1.0) AS F,
         product(f) OVER (PARTITION BY g) AS F0
  FROM km
),
det AS (
  SELECT g, v, F, F0, lead(v) OVER (PARTITION BY g ORDER BY v) AS nxt
  FROM cdf WHERE dk > 0
),
quart AS (
  SELECT g, F0,${quartiles}
  FROM det GROUP BY g, F0
),
counts AS (
  SELECT g, count(*) AS n, count(*) FILTER (WHERE d) AS detects,
         min(v) FILTER (WHERE d) AS min_detect,
         max(v) FILTER (WHERE d) AS max_detect,
         max(v) FILTER (WHERE NOT d) AS max_limit
  FROM src GROUP BY g
),
fence AS (
  SELECT q.*,
         ${fenceSql("lo", log)} AS lo_fence,
         ${fenceSql("hi", log)} AS hi_fence
  FROM quart q
),
whisk AS (
  SELECT s.g,
         min(s.v) FILTER (WHERE s.d AND f.q1 IS NOT NULL AND s.v >= f.lo_fence) AS lo_whisker,
         max(s.v) FILTER (WHERE s.d AND (f.hi_fence IS NULL OR s.v <= f.hi_fence)) AS hi_whisker,
         count(*) FILTER (WHERE s.d AND f.hi_fence IS NOT NULL
                          AND (s.v > f.hi_fence OR (f.lo_fence IS NOT NULL AND s.v < f.lo_fence))) AS outliers
  FROM src s JOIN fence f USING (g)
  ${log ? "WHERE s.v > 0" : ""}
  GROUP BY s.g
)
SELECT c.g AS grp, c.n, c.detects, c.n - c.detects AS nondetects,
       c.min_detect, c.max_detect, c.max_limit,
       q.F0 AS below_lowest, q.q1, q.q1_below, q.med, q.med_below, q.q3, q.q3_below,
       w.lo_whisker, w.hi_whisker, coalesce(w.outliers, 0) AS outliers
FROM counts c
LEFT JOIN quart q USING (g)
LEFT JOIN whisk w USING (g)
ORDER BY q.med DESC NULLS LAST, c.max_detect DESC NULLS LAST, c.g`;
}

/**
 * Tukey's fences in the axis's own space: on a log axis 1.5 IQR is a
 * ratio, so the fences are taken on log10 and brought back. NULL when a
 * quartile is undetermined (or, on log, not above zero): a whisker is then
 * drawn to the extreme detection and nothing is called an outlier.
 */
function fenceSql(side, log) {
  const sign = side === "lo" ? "-" : "+";
  const from = side === "lo" ? "q1" : "q3";
  if (!log) return `(${from} ${sign} 1.5 * (q3 - q1))`;
  return `CASE WHEN q1 > 0 AND q3 > 0
              THEN pow(10, log10(${from}) ${sign} 1.5 * (log10(q3) - log10(q1))) END`;
}

/** The text a box's tooltip and the caption say about one group. */
export function boxSummary(row, unit = "") {
  const fmt = (v) => (v === null || v === undefined ? "?"
    : Number(v).toPrecision(3).replace(/\.?0+$/, "").replace(/\.?0+e/, "e"));
  const u = unit ? ` ${unit}` : "";
  const nd = Number(row.nondetects || 0);
  const n = Number(row.n || 0);
  const pct = n ? Math.round((100 * nd) / n) : 0;
  const median = row.med_below
    ? `median below the lowest detection (${fmt(row.min_detect)}${u})`
    : `median ${fmt(row.med)}${u}`;
  if (!Number(row.detects)) {
    return `${row.grp}: n ${n}, all non-detect (limits up to ${fmt(row.max_limit)}${u})`;
  }
  return `${row.grp}: n ${n}, ${pct}% non-detect, ${median} (Kaplan-Meier)`;
}
