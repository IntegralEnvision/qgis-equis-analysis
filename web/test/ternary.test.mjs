// A ternary's decisions (web/src/ternary.js), without a browser.
//
//   npm test
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  HEIGHT, colourSql, position, ternaryCaption, ternaryCountsSql, ternaryGrid,
  ternarySql, valuesSql,
} from "../src/ternary.js";

const BASE = { pivot: "pp_3", x: "calcium", y: "magnesium", z: "sodium",
               detectTrue: ["Y", "YES", "TRUE", "1"] };

test("the corners are where the geometry says", () => {
  assert.deepEqual(position(1, 0, 0), { x: 0, y: 0 });
  assert.deepEqual(position(0, 1, 0), { x: 1, y: 0 });
  const top = position(0, 0, 1);
  assert.equal(top.x, 0.5);
  assert.ok(Math.abs(top.y - HEIGHT) < 1e-12);
  const middle = position(1 / 3, 1 / 3, 1 / 3);
  assert.ok(Math.abs(middle.x - 0.5) < 1e-12 && Math.abs(middle.y - HEIGHT / 3) < 1e-12);
});

test("only placeable samples: all three known, none negative, a positive sum", () => {
  const sql = ternarySql(BASE);
  for (const part of ["calcium_value >= 0", "sodium_value >= 0",
                      "calcium_value + magnesium_value + sodium_value > 0",
                      "sodium_detect IS NOT NULL"]) {
    assert.ok(sql.includes(part), part);
  }
  assert.ok(sql.includes("THEN 'all' ELSE 'nd'"), "a non-detect is its own class");
});

test("points modes, map selection and names are guarded as in the scatter", () => {
  const sql = ternarySql({ ...BASE, solid: "ps_9", locations: ["mw-1"] });
  assert.ok(sql.includes("sample IN (SELECT sample FROM ps_9)"));
  assert.ok(sql.includes("IN ('MW-1')"));
  assert.throws(() => ternarySql({ ...BASE, pivot: "results" }), /chart table/);
  assert.throws(() => ternarySql({ ...BASE, z: "x; DROP" }), /prefix/);
  assert.throws(() => ternarySql({ ...BASE, solid: "pp_1" }), /solid-samples/);
});

test("the colour is a mix of the corners, grey for any non-detect", () => {
  const sql = colourSql(["#d95f02", "#1b9e77", "#7570b3"], "#9a9a9a");
  assert.ok(sql.includes("printf('#%02x%02x%02x'"), sql);
  assert.ok(sql.includes("ELSE '#9a9a9a'"), sql);
  assert.ok(sql.includes("+ 0.5"), "rounding is floor(v + 0.5), as in ternary.py");
  assert.equal(colourSql(null, "#9a9a9a"), "'#9a9a9a'");
  assert.throws(() => colourSql(["red", "#000000", "#ffffff"]), /colour/);
});

test("the counts name every reason a sample is not drawn", () => {
  const sql = ternaryCountsSql(BASE);
  for (const name of ["placed", "all_detected", "with_nd", "unplaceable", "unknown",
                      "incomplete"]) {
    assert.ok(sql.includes(`AS ${name}`), name);
  }
});

test("gridlines: each share held constant at 20/40/60/80%, and a closed frame", () => {
  const g = ternaryGrid(0.2);
  assert.equal(g.lines.length, 12);
  assert.equal(g.ticks.length, 12);
  assert.deepEqual(g.ticks.filter((t) => t.axis === "c").map((t) => t.text),
                   ["20%", "40%", "60%", "80%"]);
  // A line holding C at 40% is horizontal at 0.4 of the height.
  const c40 = g.lines.filter((l) => l.axis === "c")[1];
  assert.ok(Math.abs(c40.y1 - 0.4 * HEIGHT) < 1e-12 && Math.abs(c40.y2 - 0.4 * HEIGHT) < 1e-12);
  assert.equal(g.frame.length, 3);
});

test("literal rows become a VALUES table, quoted", () => {
  const sql = valuesSql([{ x: 0.5, text: "O'Brien" }], ["x", "text"]);
  assert.equal(sql, "SELECT * FROM (VALUES (0.5, 'O''Brien')) AS t(x, text)");
});

test("the caption says what is grey, why, and what is not drawn", () => {
  const meta = (chemical) => ({ chemical, unit: "ug/L" });
  const text = ternaryCaption({
    x: meta("Calcium"), y: meta("Magnesium"), z: meta("Sodium"), matrix: "WS",
    counts: { all_detected: 6411, with_nd: 21, unplaceable: 2, unknown: 0, incomplete: 70000 },
    drawn: 6432, hideNd: true,
  }).join("\n");
  assert.ok(text.includes("Calcium : Magnesium : Sodium (shares of the sum, in ug/L)"), text);
  assert.ok(text.includes("a non-detect 21, grey at its limit (hidden)"), text);
  assert.ok(text.includes("2 have a value below zero or a zero sum"), text);
  // The direction matters: at most the limit means a SMALLER share, so
  // the true place is AWAY from that corner (an earlier draft said toward).
  assert.ok(text.includes("further from that analyte's corner"), text);
  assert.ok(!text.includes("toward"), text);
});

test("a ternary's caption says 'all three', not a scatter's 'both'", () => {
  const meta = (chemical) => ({ chemical, unit: "ug/L" });
  const first = ternaryCaption({ x: meta("A"), y: meta("B"), z: meta("C"),
    counts: { all_detected: 1, with_nd: 0 }, drawn: 1 })[0];
  assert.ok(first.includes("1 samples carrying all three, one point each"), first);
});
