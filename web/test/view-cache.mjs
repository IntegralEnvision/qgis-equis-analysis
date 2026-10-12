/**
 * A replaced temp view must not serve cached rows.
 *
 * Mosaic's Coordinator.query defaults `cache = true` and QueryManager keys
 * the cache on the SQL STRING. The chart's read is byte-identical for every
 * analyte - `SELECT value, detected, unit, fraction FROM h` - and only the
 * view `h` underneath it changes. Cached, the second analyte charted gets
 * the FIRST one's rows: Barium, Sulfate and Thallium all drew the same bars
 * and the same "42 non-detect (42.0%)", with only the caption changing.
 * Reported from the panel 2026-09-29.
 *
 *     node web/test/view-cache.mjs
 */
import { Coordinator } from "@uwdata/mosaic-core";

let failures = 0;
const check = (label, ok, detail) => {
  console.log(`[${ok ? "PASS" : "FAIL"}] ${label}${detail ? " - " + detail : ""}`);
  if (!ok) failures++;
};

const rows = { a: [{ v: 1 }], b: [{ v: 2 }] };
let current = "a";
const connector = {
  async query(request) {
    const sql = request.query ?? request.sql;
    if (/CREATE OR REPLACE/.test(sql)) {
      current = /AS a$/.test(sql.trim()) ? "a" : "b";
      return [];
    }
    return rows[current];
  },
};

const coord = new Coordinator(connector);
const READ = "SELECT v FROM h";

await coord.exec("CREATE OR REPLACE TEMP VIEW h AS a");
const first = await coord.query(READ, { type: "json" });
check("the first read returns its own rows", first[0].v === 1,
      JSON.stringify(first));

await coord.exec("CREATE OR REPLACE TEMP VIEW h AS b");
const cached = await coord.query(READ, { type: "json" });
check("caching IS on by default, and does return stale rows here",
      cached[0].v === 1,
      `${JSON.stringify(cached)} - if this is [{"v":2}] mosaic changed its `
      + "default and the cache:false in chart.js may no longer be needed");

const fresh = await coord.query(READ, { type: "json", cache: false });
check("cache:false reads the replaced view", fresh[0].v === 2,
      JSON.stringify(fresh));

// THE PLOT'S OWN QUERY. vgplot marks query through Coordinator.query with
// the default cache: true, so opting one read out left the bars stale -
// reported 2026-10-01: the caption followed the pick and the bars did not.
// The fix is to clear the cache where the view is replaced.
await coord.exec("CREATE OR REPLACE TEMP VIEW h AS a");
coord.manager.cache().clear();
const plotA = await coord.query(READ, { type: "json" });
await coord.exec("CREATE OR REPLACE TEMP VIEW h AS b");
const stalePlot = await coord.query(READ, { type: "json" });
check("a plot-style (cached) query IS stale after the view is replaced",
      plotA[0].v === 1 && stalePlot[0].v === 1, JSON.stringify(stalePlot));
coord.manager.cache().clear();
const freshPlot = await coord.query(READ, { type: "json" });
check("after clearing the cache the same cached query reads the new view",
      freshPlot[0].v === 2, JSON.stringify(freshPlot));

// And chart.js does that clear, right after replacing the view. Read from
// the source because Histogram.show needs a DOM and vgplot; deleting the
// line is the regression this must catch.
import { readFileSync } from "node:fs";
const chart = readFileSync(new URL("../src/chart.js", import.meta.url), "utf8");
const replaced = chart.indexOf("CREATE OR REPLACE TEMP VIEW");
const cleared = chart.indexOf("this.coordinator.manager.cache().clear()");
const nextQuery = chart.indexOf("this.coordinator.query(", replaced);
check("chart.js clears the cache after replacing `h`, before reading it",
      replaced > 0 && cleared > replaced && cleared < nextQuery,
      `replace@${replaced} clear@${cleared} query@${nextQuery}`);

console.log();
if (failures) { console.log(`FAILED: ${failures}`); process.exit(1); }
console.log("A replaced view is read, not remembered.");
