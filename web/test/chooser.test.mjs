// The Analysis chooser's labels and lists (web/src/chooser.js), without a
// browser: the class is DOM, the decisions it shows are these.
//
//   npm test
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  matches, matrixLabel, summary, variableName, warningLines, warningsLabel,
  xLabel, yGroups,
  yLabel,
} from "../src/chooser.js";

const AS = { prefix: "arsenic", chemical: "Arsenic", fraction: "T",
             fractionName: "total", unit: "mg/kg", samples: 1120, detected: 980 };
const CR = { prefix: "chromium", chemical: "Chromium", fraction: "T",
             fractionName: "total", unit: "mg/kg", samples: 112, detected: 112 };
const PH = { prefix: "ph", chemical: "pH", fraction: "", fractionName: "",
             unit: "", samples: 40, detected: 40 };

test("an analyte is named with its fraction and unit, when it has them", () => {
  assert.equal(variableName(AS), "Arsenic, total (mg/kg)");
  assert.equal(variableName(PH), "pH");
});

test("X options say how many samples and how many detected", () => {
  assert.equal(xLabel(AS), "Arsenic, total (mg/kg) · 1,120 samples, 980 detected");
});

test("Y options say what they share with X, once that is known", () => {
  assert.equal(yLabel(CR, 109, "Arsenic"), "Chromium, total (mg/kg) · 109 with Arsenic");
  assert.equal(yLabel(PH, 0, "Arsenic"), "pH · never with Arsenic");
  // Before the counts arrive, a Y reads like an X rather than claiming 0.
  assert.equal(yLabel(CR, undefined, "Arsenic"), xLabel(CR));
});

test("a matrix says how many samples it has", () => {
  assert.equal(matrixLabel({ code: "SO", samples: 112 }), "SO · 112 samples");
});

test("Y's list leaves X out and sets apart what never shares a sample", () => {
  const { shared, never } = yGroups([AS, CR, PH], "arsenic",
                                    { arsenic: 4, chromium: 109, ph: 0 });
  assert.deepEqual(shared.map((v) => v.prefix), ["chromium"]);
  assert.deepEqual(never.map((v) => v.prefix), ["ph"]);
});

test("without counts nothing is set apart: unknown is not 'never'", () => {
  const { shared, never } = yGroups([AS, CR, PH], "arsenic", null);
  assert.deepEqual(shared.map((v) => v.prefix), ["chromium", "ph"]);
  assert.deepEqual(never, []);
});

test("the filter matches every word, anywhere in the name", () => {
  assert.ok(matches(AS, "ars tot"));
  assert.ok(matches(AS, "MG/KG"));
  assert.ok(!matches(AS, "arsenic dissolved"));
  assert.ok(matches(AS, "   "));
});

test("the summary is one short line: analytes, samples, replicates", () => {
  assert.equal(summary({ matrix: "SO", samples: 112, variables: [AS, CR],
                         excluded: { field_replicates: 727 } }),
               "2 analytes · 112 samples · 727 replicates not plotted");
  assert.equal(summary({ matrix: null }), "Choose a matrix to list its analytes.");
});

const AL = { chemical: "Aluminum", fraction: "T", kind: "units",
             units: [["percent", 324], ["mg/kg", 32]],
             reason: "Aluminum mixes units (percent: 324, mg/kg: 32). A column holds one unit; ..." };

test("mixed units are named as such, with a count, behind one toggle", () => {
  assert.equal(warningsLabel({ skipped: [AL, AL] }), "Mixed-unit warnings (2)");
  assert.equal(warningsLabel({ skipped: [] }), "");
  assert.equal(warningsLabel({ skipped: [AL, { reason: "other", kind: "fractions" }] }),
               "2 analytes left out");
});

test("each warning is the analyte and its unit counts, not the whole sentence", () => {
  assert.deepEqual(warningLines({ skipped: [AL] }),
                   ["Aluminum, total: percent 324 · mg/kg 32"]);
  // Anything not about units keeps the host's words.
  assert.deepEqual(warningLines({ skipped: [{ kind: "fractions", reason: "why" }] }),
                   ["why"]);
});

test("the Points choices name the analytes and never offer a mixed maximum", async () => {
  const { pointsOptions } = await import("../src/chooser.js");
  const got = pointsOptions("Arsenic", "Chromium");
  assert.deepEqual(got.map(([v]) => v), ["all", "recent", "max_x", "max_y"]);
  assert.equal(got[2][1], "Highest Arsenic per location");
  assert.equal(got[3][1], "Highest Chromium per location");
});

test("a ternary's third analyte can rank a location too", async () => {
  const { pointsOptions } = await import("../src/chooser.js");
  assert.equal(pointsOptions("Ca", "Mg").length, 4);
  const got = pointsOptions("Ca", "Mg", "Na");
  assert.deepEqual(got[4], ["max_z", "Highest Na per location"]);
});
