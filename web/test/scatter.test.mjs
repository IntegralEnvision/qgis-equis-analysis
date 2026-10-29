/**
 * The scatter's decisions (US-24), tested without a browser.
 *
 *   node --test web/test/scatter.test.mjs
 *
 * tools/test_scatter_sql.py runs the same SQL against real pivots; this
 * checks the rules that can be wrong while still drawing a picture.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import {
  CLASSES, axisLabel, axisType, classLabel, scatterBrushLabel,
  locationsSql, scatterCaption, scatterCountsSql, scatterSql,
} from "../src/scatter.js";

const BASE = { pivot: "p_abc123_1", x: "manganese", y: "lead",
               detectTrue: ["Y", "YES", "TRUE", "1"] };

test("four classes, four symbols, and the detected class is the only filled one", () => {
  assert.deepEqual(CLASSES.map((c) => c.key).sort(),
                   ["both", "neither", "x_only", "y_only"]);
  assert.equal(new Set(CLASSES.map((c) => c.symbol)).size, 4);
  assert.deepEqual(CLASSES.filter((c) => c.filled).map((c) => c.key), ["both"]);
});

test("a point needs BOTH analytes and a known flag for each", () => {
  const sql = scatterSql(BASE);
  for (const clause of ["manganese_value IS NOT NULL", "lead_value IS NOT NULL",
                        "manganese_detect IS NOT NULL", "lead_detect IS NOT NULL"]) {
    assert.ok(sql.includes(clause), clause);
  }
});

test("detection comes from the host's list, never a list written here", () => {
  assert.throws(() => scatterSql({ ...BASE, detectTrue: [] }), /detect-true/);
  const sql = scatterSql({ ...BASE, detectTrue: ["Y"] });
  assert.ok(sql.includes("IN ('Y')"));
  assert.ok(!sql.includes("'YES'"));
});

test("an empty map selection means zero locations, not everything (US-6)", () => {
  assert.ok(scatterSql({ ...BASE, locations: [] }).includes("FALSE"));
  assert.ok(!scatterSql({ ...BASE, locations: null }).includes("FALSE"));
  // Case-insensitive, and quotes survive.
  const sql = scatterSql({ ...BASE, locations: ["mw-1", "O'Neil"] });
  assert.ok(sql.includes("'MW-1'") && sql.includes("'O''NEIL'"));
});

test("the map's clause is the same predicate the SQL uses (linked.js)", () => {
  // The selection reaches the drawn views as a clause on the crossfilter
  // (setMapSelection), not as a redraw: the two must say the same thing.
  assert.equal(locationsSql(null), null);
  assert.equal(locationsSql(undefined), null);
  assert.equal(locationsSql([]), "FALSE");
  const clause = locationsSql(["mw-1", "O'Neil"]);
  assert.equal(clause, "upper(coalesce(location, '')) IN ('MW-1', 'O''NEIL')");
  assert.ok(scatterSql({ ...BASE, locations: ["mw-1", "O'Neil"] }).includes(clause));
});

test("names that travel through JSON into SQL are checked, not trusted", () => {
  assert.throws(() => scatterSql({ ...BASE, x: "x; DROP TABLE t" }), /prefix/);
  assert.throws(() => scatterSql({ ...BASE, pivot: "v_results" }), /pivot table/);
  assert.throws(() => scatterCountsSql({ ...BASE, pivot: "h" }), /pivot table/);
});

test("log whenever the axis has positive values; linear only with none", () => {
  // axisType takes the POSITIVE minimum: a zero elsewhere no longer forces
  // the whole axis linear (two detected-0 As values did that to 834 points).
  assert.equal(axisType(0.05), "log");
  assert.equal(axisType(null), "linear");
  assert.equal(axisType(undefined), "linear");
});

test("a log axis leaves out values of 0 or below, and they are named", async () => {
  const { excludedSql } = await import("../src/scatter.js");
  const sql = scatterSql({ ...BASE, logX: true });
  assert.ok(sql.includes("manganese_value > 0") && !sql.includes("lead_value > 0"), sql);
  assert.equal(excludedSql(BASE, false, false), null);
  assert.ok(excludedSql(BASE, true, false).includes("WHERE x <= 0"));
  const lines = scatterCaption({
    x: { chemical: "Arsenic" }, y: { chemical: "Chromium" },
    counts: { both: 810, x_only: 21, y_only: 1, neither: 2 },
    excluded: { count: 2, logX: true, logY: true, samples: [
      { sample: "ST4_R2", x: 0, y: 0.01, class: "x_only" }] },
  });
  const text = lines.join(" | ");
  assert.ok(text.includes("Not on the log axes: 2 samples"), text);
  assert.ok(text.includes("ST4_R2: Arsenic 0 (reported as detected)"), text);
  assert.ok(text.includes("and 1 more"), text);
});

test("the unit is on the axis, always (US-15)", () => {
  assert.equal(axisLabel({ chemical: "Lead", fraction: "T", unit: "ug/L" }),
               "Lead (T, ug/L)");
  assert.equal(axisLabel({ chemical: "Lead", fraction: "", unit: "" }),
               "Lead (unit not reported)");
});

test("the caption states every class and everything NOT drawn", () => {
  const lines = scatterCaption({
    x: { chemical: "Manganese" }, y: { chemical: "Lead" }, matrix: "WS",
    counts: { both: 1573, x_only: 19770, y_only: 58, neither: 41821,
              unpaired: 1702, unknown: 3, nonpositive: 0 },
    resolved: 12,
  });
  const text = lines.join("\n");
  assert.ok(text.includes("63,222 samples carrying both"), text);
  assert.ok(text.includes("Manganese detected, Lead not 19,770"), text);
  assert.ok(text.includes("neither detected (both at limits) 41,821"), text);
  assert.ok(text.includes("1,702 samples carry only one"), text);
  assert.ok(text.includes("3 have a blank detect flag"), text);
  assert.ok(text.includes("12 cells had several results"), text);
});

test("the caption says how many field replicates were not plotted", () => {
  const text = scatterCaption({
    x: { chemical: "As" }, y: { chemical: "Cr" },
    counts: { both: 109, x_only: 2, y_only: 0, neither: 1 },
    pivotExcluded: { field_replicates: 724, qc_rows: 0 },
  }).join(" | ");
  assert.ok(text.includes("724 field duplicates and replicates"), text);
  assert.ok(text.includes("primary sample is plotted"), text);
  assert.ok(!text.includes("QC results"), text);
});

test("class labels name the analytes", () => {
  assert.equal(classLabel("x_only", "Mn", "Pb"), "Mn detected, Pb not");
  assert.equal(classLabel("y_only", "Mn", "Pb"), "Pb detected, Mn not");
});

test("the brush counts samples AND locations", () => {
  assert.equal(scatterBrushLabel({ samples: 118, locations: 23, total: 5000 }),
               "118 of 5,000 samples selected, at 23 locations.");
  assert.ok(scatterBrushLabel({ total: 10 }).startsWith("Drag a rectangle"));
});

import { breakLabels, colourSql } from "../src/scatter.js";

const BI = {
  x: { breaks: [10, 100] }, y: { breaks: [1, 5] }, grey: "#9a9a9a",
  palette: { "1-1": "#e8e8e8", "2-1": "#dfb0d6", "3-1": "#be64ac",
             "1-2": "#ace4e4", "2-2": "#a5add3", "3-2": "#8c62aa",
             "1-3": "#5ac8c8", "2-3": "#5698b9", "3-3": "#3b4994" },
};

test("colours come from the host's palette, nine cases plus grey", () => {
  const sql = colourSql("xv", "yv", "xd", "yd", BI);
  for (const hex of Object.values(BI.palette)) assert.ok(sql.includes(hex), hex);
  assert.ok(sql.startsWith("CASE WHEN NOT (xd AND yd) THEN '#9a9a9a'"), sql);
  assert.ok(sql.includes("xv <= 10") && sql.includes("yv <= 5"), "breaks are inclusive");
});

test("no bivariate options: no colour column to speak of", () => {
  assert.equal(colourSql("xv", "yv", "xd", "yd", null), "NULL");
  assert.equal(colourSql("xv", "yv", "xd", "yd", { ...BI, x: { breaks: [1] } }),
               "'#9a9a9a'");
});

test("a malformed palette entry is skipped, not injected", () => {
  const sql = colourSql("xv", "yv", "xd", "yd",
    { ...BI, palette: { "1-1": "red'); DROP TABLE t; --" } });
  assert.ok(!sql.includes("DROP"), sql);
});

test("break labels keep every real digit", () => {
  assert.deepEqual(breakLabels([76.145, 810]), ["≤ 76.1", "76.1–810", "> 810"]);
  assert.deepEqual(breakLabels([1.7, 16.78]), ["≤ 1.7", "1.7–16.8", "> 16.8"]);
  assert.deepEqual(breakLabels([100, 1000]), ["≤ 100", "100–1,000", "> 1,000"]);
});

test("a pivot or its pair table is accepted as a table; nothing else is", () => {
  assert.ok(scatterSql({ ...BASE, pivot: "pp_12" }).includes("FROM pp_12"));
  assert.ok(scatterSql({ ...BASE, pivot: "p_ab12_3" }).includes("FROM p_ab12_3"));
  for (const bad of ["results", "pp_1; DROP TABLE x", "ppp_1", "P_1"]) {
    assert.throws(() => scatterSql({ ...BASE, pivot: bad }), /not a pivot table/, bad);
  }
});

test("a points mode draws only the host's picked samples, by table", () => {
  const sql = scatterSql({ ...BASE, solid: "ps_7" });
  assert.ok(sql.includes("sample IN (SELECT sample FROM ps_7)"), sql);
  assert.ok(!scatterSql(BASE).includes("ps_"), "no mode, no filter");
  for (const bad of ["pp_7", "ps_7; DROP TABLE x", "results"]) {
    assert.throws(() => scatterSql({ ...BASE, solid: bad }), /solid-samples table/, bad);
  }
});

test("the caption says what each point is, out of how many, and when", async () => {
  const { pointsPhrase } = await import("../src/scatter.js");
  assert.equal(pointsPhrase(63222, null, 63222),
               "63,222 samples carrying both, one point each");
  assert.equal(pointsPhrase(219, { mode: "recent",
                                   label: "each location's most recent sample" }, 63222),
               "219 points, each location's most recent sample (of 63,222 samples)");
  assert.equal(pointsPhrase(40, { mode: "all", window: ["2015-01-01", "2020-12-31"] }, 63222),
               "40 samples carrying both in the window (of 63,222 samples)"
               + " · 2015-01-01 to 2020-12-31");
  assert.ok(pointsPhrase(5, { mode: "all", window: [null, "2020-12-31"] }, 9)
    .endsWith("… to 2020-12-31"));
});
