/**
 * US-1: a histogram of the loaded report's results, brushable.
 *
 * Built on the store's `v_results` view, which already carries the domain
 * rules as columns - `detected`, `at_limit`, and `value_half` / `value_full`
 * / `value_exclude` for the three non-detect conventions. Those are not
 * recomputed here; reimplementing them in SQL is exactly the divergence
 * report_data.py and store.py exist to prevent.
 *
 * TWO THINGS THIS REFUSES TO DO, both because they produce a plausible
 * picture rather than an error:
 *
 *   * Merge non-detects into the detections. A non-detect plotted at half
 *     its reporting limit makes a spike that is an artefact of laboratory
 *     sensitivity, not of contamination (US-5). They are counted and drawn
 *     as a separate stacked segment per bin.
 *   * Hard-code a bin count. 20 bins is a decision about the data made
 *     before seeing it; the default comes from Freedman-Diaconis (US-1).
 */

/** Freedman-Diaconis bin width, falling back to Sturges for degenerate data. */
export function binCount(values) {
  const clean = values.filter((v) => Number.isFinite(v)).sort((a, b) => a - b);
  const n = clean.length;
  if (n < 2) return 1;
  const quantile = (p) => {
    const index = (n - 1) * p;
    const low = Math.floor(index);
    const high = Math.ceil(index);
    return low === high
      ? clean[low]
      : clean[low] + (clean[high] - clean[low]) * (index - low);
  };
  const iqr = quantile(0.75) - quantile(0.25);
  const span = clean[n - 1] - clean[0];
  if (!(span > 0)) return 1;
  if (!(iqr > 0)) {
    // Sturges: the IQR collapses when most values are identical, which is
    // common when a report is dominated by one reporting limit.
    return Math.max(1, Math.ceil(Math.log2(n) + 1));
  }
  const width = (2 * iqr) / Math.cbrt(n);
  return Math.max(1, Math.min(200, Math.ceil(span / width)));
}

/**
 * The SQL behind the histogram.
 *
 * `ndColumn` picks which non-detect convention the value axis uses, matching
 * the panel's own picker: value_half, value_full or value_exclude. The
 * `detected` flag is carried through so non-detects can be drawn separately
 * rather than folded in.
 */
export function histogramSql({
  chemical,
  casRn = null,
  matrix = null,
  fraction = null,
  method = null,
  ndColumn = "value_half",
  aggregate = "unaggregated",
  locations = null,
} = {}) {
  // A CHEMICAL IS REQUIRED, not merely usual. Without one the only
  // predicate is "has a value", which over a whole-facility parquet is
  // 3,400,302 rows - 2.9 s to query and then the same rows crossing the
  // bridge as Arrow to be binned in the browser. The histogram is a view
  // OVER a filter, never a plot of everything; refusing is the honest
  // answer, and the caller shows its own message.
  if (!casRn && !chemical) {
    throw new Error("histogramSql needs a chemical or a CAS RN: " +
                    "binning every result in the store is not a view.");
  }
  const quote = (text) => `'${String(text).replace(/'/g, "''")}'`;
  const where = [`${ndColumn} IS NOT NULL`];
  // CAS RN identifies a chemical when the report has one; the name is the
  // fallback. Same rule as report_data._matches, kept in that order.
  if (casRn) {
    where.push(`coalesce(cas_rn, '') = ${quote(casRn)}`);
  } else if (chemical) {
    where.push(`coalesce(chemical, '') = ${quote(chemical)}`);
  }
  // Codes as the store compares them (store._code_filter): upper-case, any
  // of a list. The host sends ReportStore.filter_codes for each, null being
  // "no filter" - nothing chosen, or a column the source lacks, which must
  // match everything rather than drop every row. A bare string is accepted
  // as a list of one.
  for (const [column, wanted] of [["matrix", matrix], ["fraction", fraction],
                                  ["method", method]]) {
    // Neither trimmed nor emptied: these are the store's codes already, and
    // [""] is its "blank codes only", as it compares them.
    const codes = (Array.isArray(wanted) ? wanted : wanted == null ? [] : [wanted])
      .map((code) => String(code ?? "").toUpperCase());
    if (codes.length) {
      where.push(`upper(coalesce(${column}, '')) IN (${codes.map(quote).join(", ")})`);
    }
  }

  // US-6: the map's selection narrows the chart. An empty ARRAY is not the
  // same as null - it means "these zero locations", which is what a user
  // sees after selecting nothing, and binning the whole facility instead
  // would be the chart disagreeing with the map.
  if (Array.isArray(locations)) {
    if (!locations.length) {
      where.push("FALSE");
    } else {
      // Case-insensitively, because the report and the feature service do
      // not agree on case - the same rule report_data applies everywhere.
      where.push(`upper(coalesce(location, '')) IN (${
        locations.map((code) => quote(String(code).toUpperCase())).join(", ")
      })`);
    }
  }

  // `fraction` and `matrix` are carried so the chart can SEE what it was
  // asked to bin. Dissolved and total come from the same water on the same
  // day and differ by up to ~29x, so a chart spanning both is a lie the
  // same way a map spanning both is - and it cannot refuse what it cannot
  // see.
  const base = `SELECT ${ndColumn} AS value, detected, location, unit,
                       matrix, fraction, sample_date, row_id
                FROM v_results
                WHERE ${where.join(" AND ")}`;

  if (aggregate === "unaggregated") return base;

  // THE HISTOGRAM BINS WHAT THE MAP IS SHOWING (decided 2026-09-23). When
  // the layer shows one value per location, the histogram shows the same
  // population - so "keep the points in this range" means exactly what the
  // picture implies. Binning every result under an aggregated map would
  // make the brush mean "keep locations having ANY result in this range",
  // which is a different filter from the one the user sees.
  //
  // The ordering mirrors store.aggregate(): most_recent takes the latest
  // sample_date, maximum the largest value, and BOTH break ties on row_id
  // so the row that arrived first wins - the same rule the Python's strict
  // `>` comparison produces.
  //
  // A NON-DETECT CAN NEVER BE THE MAXIMUM while a real detection exists at
  // that location - `detected DESC` first, exactly as store.aggregate()
  // orders since 2026-09-28. A non-detect carries only the limit it fell
  // below, so ranking it as a measurement reports laboratory sensitivity
  // as the site maximum; on Facility A that was 759 cells, the worst by 1,250x.
  // The chart must agree with the map about which row won, or the brush
  // selects points the map is not showing.
  const order = aggregate === "maximum"
    ? "detected DESC, value DESC, row_id ASC"
    : "sample_date DESC NULLS LAST, row_id ASC";
  return `SELECT value, detected, location, unit, matrix, fraction,
                 sample_date, row_id
          FROM (
            SELECT *, row_number() OVER (
              PARTITION BY location ORDER BY ${order}) AS pick
            FROM (${base})
          )
          WHERE pick = 1`;
}

/** How each aggregation names its own population, for the caption. */
export const POPULATION = {
  unaggregated: "every result",
  most_recent: "most recent per location",
  maximum: "maximum per location",
};

/**
 * Is a log axis usable for these values?
 *
 * Linear is the default (decided 2026-09-23). Log is offered as a toggle
 * because these distributions are log-normal, but it cannot show zero or
 * negative values - and silently dropping them would understate the data,
 * which is the class of lie this plugin refuses elsewhere. So the toggle
 * reports what it would cost rather than just doing it.
 */
export function logAxisCost(values) {
  let dropped = 0;
  let usable = 0;
  for (const value of values) {
    if (!Number.isFinite(value)) continue;
    if (value > 0) usable++;
    else dropped++;
  }
  return { usable, dropped, safe: dropped === 0 };
}

/** What the caption has to say, per US-2 and US-5. */
export function caption({ chemical, matrix, fraction, rows, nonDetects,
                          unit, population = "every result" }) {
  const parts = [chemical || "all analytes"];
  if (matrix) parts.push(matrix);
  if (fraction) parts.push(fraction);
  parts.push(population);
  const total = rows || 0;
  let text = `${parts.join(" · ")} · n = ${total.toLocaleString()}`;
  if (nonDetects) {
    const share = total ? (100 * nonDetects) / total : 0;
    text += ` · ${nonDetects.toLocaleString()} non-detect `
          + `(${share.toFixed(1)}%)`;
  }
  if (unit) text += ` · ${unit}`;
  return text;
}

/** The brush, stated in words. US-1 asks for this explicitly. */
export function brushLabel({ from, to, matching, total, unit, locations }) {
  if (from === null || from === undefined) {
    return `no filter · ${(total || 0).toLocaleString()} results`;
  }
  const round = (v) => (Math.abs(v) >= 100 ? v.toFixed(0)
                      : Math.abs(v) >= 1 ? v.toFixed(2)
                      : v.toPrecision(3));
  const range = `${round(from)} – ${round(to)}${unit ? " " + unit : ""}`;
  const of = locations !== undefined
    ? `${matching.toLocaleString()} of ${locations.toLocaleString()} locations`
    : `${matching.toLocaleString()} of ${(total || 0).toLocaleString()} results`;
  return `${range} (${of})`;
}
