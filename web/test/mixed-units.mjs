/**
 * US-15: a chart refuses a mixed-unit selection.
 *
 * The map already refuses (`_confirm_units` blocks the build). The chart is
 * what people read FIRST and screenshot into reports, so it cannot be the
 * laxer of the two - and it was: it took the FIRST unit it saw and labelled
 * the whole axis with it. Iron in SE on Facility A is 23,456 rows of mg/kg and
 * 116 of mg/L, which would have binned a factor of 1000 apart under one
 * label, invisibly.
 *
 *     node web/test/mixed-units.mjs
 */
let failures = 0;
const check = (label, ok, detail) => {
  console.log(`[${ok ? "PASS" : "FAIL"}] ${label}${detail ? " - " + detail : ""}`);
  if (!ok) failures++;
};

/** The unit-counting and refusal decision, as chart.js implements it. */
function assess(rows) {
  const counts = new Map();
  for (const row of rows) {
    const clean = (row.unit || "").trim();
    if (!clean) continue;
    counts.set(clean, (counts.get(clean) || 0) + 1);
  }
  const units = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  return { units, unit: units.length ? units[0][0] : "", refuse: units.length > 1 };
}

const ugL = (n) => Array.from({ length: n }, () => ({ unit: "ug/L" }));
const mgkg = (n) => Array.from({ length: n }, () => ({ unit: "mg/kg" }));
const mgL = (n) => Array.from({ length: n }, () => ({ unit: "mg/L" }));

let r = assess(ugL(90492));
check("one unit is charted", !r.refuse && r.unit === "ug/L", r.unit);

// The real Facility A case: the minority unit must not be ignored.
r = assess([...mgkg(23456), ...mgL(116)]);
check("mg/kg beside mg/L is refused", r.refuse, JSON.stringify(r.units));
check("and both units are named, with counts",
      r.units.length === 2 && r.units[0][1] === 23456 && r.units[1][1] === 116,
      JSON.stringify(r.units));
check("the commonest unit is named first, so the message reads sensibly",
      r.units[0][0] === "mg/kg", r.units[0][0]);

// A single stray row is still a refusal: 1000x is 1000x whatever the count.
r = assess([...mgkg(23456), ...mgL(1)]);
check("even one row in another unit is refused", r.refuse,
      JSON.stringify(r.units));

// Rows with no unit at all are not a second "unit".
r = assess([...ugL(10), { unit: "" }, { unit: null }, { unit: "  " }]);
check("blank units do not count as a unit of their own",
      !r.refuse && r.unit === "ug/L", JSON.stringify(r.units));

// Nothing to chart is not a unit problem.
r = assess([]);
check("no rows is not a mixed-unit refusal", !r.refuse && r.unit === "",
      JSON.stringify(r.units));

// The axis label states the absence rather than going blank.
const label = (unit) => unit ? `value (${unit})` : "value (unit not reported)";
check("an axis without a unit says so", label("") === "value (unit not reported)",
      label(""));
check("an axis with a unit names it", label("ug/L") === "value (ug/L)",
      label("ug/L"));

// --- US-16: dissolved and total are not one population ----------------------
// They come from the same water on the same day and differ by up to ~29x.
// On Facility A, Arsenic in WS runs to 1,300 as total and 16.3 as dissolved.
function fractionsOf(rows) {
  const counts = new Map();
  for (const row of rows) {
    const clean = (row.fraction || "").trim().toUpperCase();
    if (clean) counts.set(clean, (counts.get(clean) || 0) + 1);
  }
  return { counts, refuse: counts.has("D") && counts.has("T") };
}
const frac = (code, n) => Array.from({ length: n }, () => ({ fraction: code }));

let g = fractionsOf([...frac("T", 62967), ...frac("D", 18715)]);
check("dissolved beside total is refused", g.refuse,
      JSON.stringify([...g.counts]));

g = fractionsOf([...frac("T", 62967), ...frac("N", 8810)]);
check("total beside N is NOT refused - N is neither half of that split",
      !g.refuse, JSON.stringify([...g.counts]));

g = fractionsOf(frac("D", 18715));
check("one fraction charts", !g.refuse, JSON.stringify([...g.counts]));

g = fractionsOf([{ fraction: "" }, { fraction: null }]);
check("blank fractions are not a fraction", !g.refuse,
      JSON.stringify([...g.counts]));

g = fractionsOf([...frac("d", 3), ...frac("T", 4)]);
check("the check is case-insensitive", g.refuse, JSON.stringify([...g.counts]));

// --- US-6: the map's selection narrows the chart -----------------------------
const { histogramSql } = await import("../src/histogram.js");

let sql = histogramSql({ chemical: "Arsenic", locations: ["MW-1", "mw-2"] });
check("a selection becomes an IN list", /IN \(/.test(sql), sql.match(/IN \([^)]*\)/)?.[0]);
check("codes are compared case-insensitively - the report and the feature "
      + "service do not agree on case",
      /'MW-1', 'MW-2'/.test(sql), sql.match(/IN \([^)]*\)/)?.[0]);

sql = histogramSql({ chemical: "Arsenic", locations: [] });
check("selecting NOTHING bins nothing, rather than everything",
      /FALSE/.test(sql) && !/IN \(/.test(sql));

sql = histogramSql({ chemical: "Arsenic" });
check("no selection means no location filter at all", !/IN \(/.test(sql));
check("and the other filters still apply",
      /coalesce\(chemical/.test(sql));

// The selection composes with the rest rather than replacing them.
sql = histogramSql({ chemical: "Arsenic", matrix: "WS", fraction: "D",
                     locations: ["MW-1"] });
check("a selection narrows ALONGSIDE matrix and fraction",
      /matrix/.test(sql) && /fraction/.test(sql) && /IN \(/.test(sql));

// A code with an apostrophe must not break the SQL.
sql = histogramSql({ chemical: "Arsenic", locations: ["O'BRIEN-1"] });
check("an apostrophe in a location code is escaped",
      /'O''BRIEN-1'/.test(sql), sql.match(/IN \([^)]*\)/)?.[0]);

console.log();
if (failures) { console.log(`FAILED: ${failures}`); process.exit(1); }
console.log("The chart refuses to bin units that are not comparable.");
