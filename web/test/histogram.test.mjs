/**
 * The histogram's decisions, tested without a browser.
 *
 *   node --test web/test/
 *
 * Bin counts, the SQL's filter rules and the caption wording are all
 * decisions that can be wrong in a way that still draws a chart, so they are
 * checked here rather than by looking at one.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { POPULATION, binCount, brushLabel, caption, histogramSql, logAxisCost }
  from "../src/histogram.js";

test("bin count comes from the data, not a constant", () => {
  // A wide spread should not collapse to a handful of bins.
  const spread = Array.from({ length: 1000 }, (_, i) => i);
  const bins = binCount(spread);
  assert.ok(bins > 5 && bins <= 200, `got ${bins}`);

  // Two values: one bin is the honest answer, not 20 mostly-empty ones.
  assert.equal(binCount([1, 1, 1, 1]), 1);
  assert.equal(binCount([]), 1);
  assert.equal(binCount([42]), 1);
});

test("a collapsed IQR falls back to Sturges rather than dividing by zero", () => {
  // Very common in real reports: most results sit at one reporting limit.
  const values = Array(500).fill(0.5);
  values.push(12);
  const bins = binCount(values);
  assert.ok(Number.isFinite(bins) && bins >= 1, `got ${bins}`);
  assert.ok(bins <= 200);
});

test("NaNs and nulls do not become bins", () => {
  const bins = binCount([1, 2, 3, NaN, undefined, null, 4, 5]);
  assert.ok(Number.isFinite(bins) && bins >= 1);
});

test("the SQL reads the store's view, not raw columns", () => {
  const sql = histogramSql({ chemical: "Nickel", matrix: "SO" });
  assert.match(sql, /FROM v_results/);
  // The non-detect convention must come from the view's prepared columns.
  // Recomputing it here would be a second implementation of a domain rule.
  assert.match(sql, /value_half AS value/);
  assert.match(sql, /detected/);
});

test("CAS RN identifies a chemical when present, the name otherwise", () => {
  const byCas = histogramSql({ chemical: "Nickel", casRn: "7440-02-0" });
  assert.match(byCas, /cas_rn, ''\) = '7440-02-0'/);
  assert.ok(!byCas.includes("chemical, '') = 'Nickel'"),
            "must not filter on BOTH, which would drop rows whose name "
            + "differs from the picker's spelling");

  const byName = histogramSql({ chemical: "Nickel" });
  assert.match(byName, /chemical, ''\) = 'Nickel'/);
});

test("matrix, fraction and method compare case-blind, as the store does", () => {
  // store._code_filter compares upper(coalesce(col, '')); this compared the
  // raw text, so 'so' found nothing in a report coded SO (found 2026-10-28).
  const sql = histogramSql({ chemical: "Nickel", matrix: "so",
                             fraction: "d", method: "sw6010" });
  assert.match(sql, /upper\(coalesce\(matrix, ''\)\) IN \('SO'\)/);
  assert.match(sql, /upper\(coalesce\(fraction, ''\)\) IN \('D'\)/);
  assert.match(sql, /upper\(coalesce\(method, ''\)\) IN \('SW6010'\)/);
});

test("every matrix of a multi-matrix layer is binned, not the first", () => {
  // A layer over sediment AND tailings (both solids, both mg/kg) was binned
  // over sediment alone: the host sent only the first of the list.
  const sql = histogramSql({ chemical: "Nickel", matrix: ["SE", "tails"],
                             method: ["M1", "M2"] });
  assert.match(sql, /upper\(coalesce\(matrix, ''\)\) IN \('SE', 'TAILS'\)/);
  assert.match(sql, /upper\(coalesce\(method, ''\)\) IN \('M1', 'M2'\)/);
});

test("an empty or absent code list is no filter", () => {
  // The store's rule: None or an empty collection filters nothing. The host
  // also sends null for a column the source lacks, which must match
  // everything rather than drop every row.
  for (const matrix of [null, undefined, []]) {
    const sql = histogramSql({ chemical: "Nickel", matrix });
    assert.ok(!/coalesce\(matrix/.test(sql), `matrix=${JSON.stringify(matrix)}`);
  }
});

test("quotes in an analyte name cannot break the SQL", () => {
  const sql = histogramSql({ chemical: "O'Brien's compound" });
  assert.match(sql, /'O''Brien''s compound'/);
});

test("the non-detect convention selects the view's column", () => {
  for (const column of ["value_half", "value_full", "value_exclude"]) {
    const sql = histogramSql({ chemical: "Nickel", ndColumn: column });
    assert.match(sql, new RegExp(`${column} AS value`));
    assert.match(sql, new RegExp(`${column} IS NOT NULL`));
  }
});

test("the caption names the population and the non-detect split", () => {
  const text = caption({
    chemical: "Nickel", matrix: "SO", rows: 46113, nonDetects: 8204,
    unit: "mg/kg",
  });
  assert.match(text, /Nickel/);
  assert.match(text, /SO/);
  assert.match(text, /every result/);
  assert.match(text, /n = 46,113/);
  assert.match(text, /8,204 non-detect \(17\.8%\)/);
  assert.match(text, /mg\/kg/);
});

test("the caption distinguishes aggregated from raw populations", () => {
  const raw = caption({ chemical: "Nickel", rows: 46113 });
  const agg = caption({ chemical: "Nickel", rows: 5312,
                        population: "maximum per location" });
  assert.notEqual(raw, agg);
  assert.match(agg, /maximum per location/);
});

test("the brush states itself in words", () => {
  const text = brushLabel({ from: 2.4, to: 88, matching: 312, total: 5312,
                            unit: "mg/kg", locations: 5312 });
  assert.match(text, /2\.40 – 88/);
  assert.match(text, /mg\/kg/);
  assert.match(text, /312 of 5,312 locations/);
});

test("no brush says so rather than showing an empty range", () => {
  const text = brushLabel({ from: null, to: null, total: 5312 });
  assert.match(text, /no filter/);
  assert.match(text, /5,312/);
});

test("the default population is whatever the map is showing", () => {
  // Decided 2026-09-23: binning every result under an aggregated map would
  // make the brush mean something other than what the picture implies.
  const raw = histogramSql({ chemical: "Nickel" });
  assert.ok(!raw.includes("row_number"), "unaggregated needs no window");

  for (const mode of ["most_recent", "maximum"]) {
    const sql = histogramSql({ chemical: "Nickel", aggregate: mode });
    assert.match(sql, /row_number\(\) OVER/);
    assert.match(sql, /PARTITION BY location/);
    assert.match(sql, /WHERE pick = 1/);
  }
});

test("each aggregation orders the way store.aggregate does", () => {
  const recent = histogramSql({ chemical: "Nickel", aggregate: "most_recent" });
  assert.match(recent, /sample_date DESC NULLS LAST/);

  const max = histogramSql({ chemical: "Nickel", aggregate: "maximum" });
  assert.match(max, /value DESC/);

  // Both break ties on row_id: the row that arrived first wins, which is
  // what the Python's strict `>` comparison produces.
  assert.match(recent, /row_id ASC/);
  assert.match(max, /row_id ASC/);
});

test("the method filter is applied when given", () => {
  assert.match(histogramSql({ chemical: "Nickel", method: "SW6020A" }),
               /upper\(coalesce\(method, ''\)\) IN \('SW6020A'\)/);
  assert.ok(!histogramSql({ chemical: "Nickel" }).includes("method, ''"));
});

test("each aggregation names its own population", () => {
  assert.equal(POPULATION.unaggregated, "every result");
  assert.match(POPULATION.most_recent, /per location/);
  assert.match(POPULATION.maximum, /maximum/);
  // The caption must be able to say which one it is showing.
  const text = caption({ chemical: "Nickel", rows: 5312,
                         population: POPULATION.maximum });
  assert.match(text, /maximum per location/);
});

test("a log axis reports what it would drop rather than dropping it", () => {
  // Log cannot show zero, and silently omitting those results would
  // understate the data.
  const clean = logAxisCost([1, 2, 3]);
  assert.deepEqual(clean, { usable: 3, dropped: 0, safe: true });

  const withZero = logAxisCost([0, 1, 2, -1]);
  assert.equal(withZero.safe, false);
  assert.equal(withZero.dropped, 2);
  assert.equal(withZero.usable, 2);
});
