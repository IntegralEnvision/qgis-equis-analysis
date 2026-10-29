/**
 * A ternary plot's decisions, without a browser: SQL, geometry, words.
 *
 * Three analytes as shares of their sum, one point per SAMPLE (decided
 * 2026-10-12; CLAUDE.md). The rules are equis_loader/ternary.py's - one
 * unit (the host refuses otherwise), non-detects plotted at their limit
 * in grey, colour = the shares' mix of three corner hues - and this file
 * restates them in SQL only so the page can draw; tools/test_ternary.py
 * runs this SQL against real pivots and holds its colours to ternary.py's.
 *
 * Geometry: A (X) at the bottom left (0, 0), B (Y) at the bottom right
 * (1, 0), C (Z) at the top (1/2, h), h = sqrt(3)/2. A sample with shares
 * (sa, sb, sc) sits at x = sb + sc/2, y = sc * h.
 */
import { locationsSql, pointsPhrase } from "./scatter.js";

export const HEIGHT = Math.sqrt(3) / 2;

const quote = (text) => `'${String(text).replace(/'/g, "''")}'`;

function column(prefix, suffix) {
  if (!/^[a-z][a-z0-9_]*$/.test(prefix)) {
    throw new Error(`not a pivot column prefix: ${prefix}`);
  }
  return `${prefix}_${suffix}`;
}

function table(name) {
  if (!/^pp_[0-9]+$/.test(String(name))) throw new Error(`not a chart table: ${name}`);
  return name;
}

function solidTable(name) {
  if (!/^ps_[0-9]+$/.test(String(name))) {
    throw new Error(`not a solid-samples table: ${name}`);
  }
  return name;
}

function hexChannels(hex) {
  if (!/^#[0-9a-fA-F]{6}$/.test(hex)) throw new Error(`not a colour: ${hex}`);
  return [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
}

/** Where a share triple sits in the triangle. */
export function position(sa, sb, sc) {
  return { x: sb + sc / 2, y: sc * HEIGHT };
}

/** "Every value and flag known" for the three - a sample the chart can consider. */
function carriesAll(prefixes) {
  return prefixes.map((p) => `${column(p, "value")} IS NOT NULL AND `
    + `${column(p, "detect")} IS NOT NULL`).join(" AND ");
}

/**
 * The SQL behind the triangle: one row per placeable sample.
 *
 * Placeable: all three values and flags known, none below zero, and a
 * positive sum - anything else has no share to draw, and is counted by
 * ternaryCountsSql instead. `class` is "all" (detected on all three,
 * coloured by composition) or "nd" (any non-detect: grey, at its limit).
 */
export function ternarySql({ pivot, x, y, z, detectTrue, locations = null,
                             solid = null, corners = null, grey = "#9a9a9a" }) {
  if (!Array.isArray(detectTrue) || !detectTrue.length) {
    throw new Error("ternarySql needs the host's detect-true values");
  }
  const t = table(pivot);
  const truth = `(${detectTrue.map((v) => quote(String(v).toUpperCase())).join(", ")})`;
  const [a, b, c] = [x, y, z].map((p) => column(p, "value"));
  const detected = [x, y, z].map((p) => `upper(${column(p, "detect")}) IN ${truth}`)
    .join(" AND ");
  const where = [carriesAll([x, y, z]), `${a} >= 0`, `${b} >= 0`, `${c} >= 0`,
                 `${a} + ${b} + ${c} > 0`];
  if (solid) where.push(`sample IN (SELECT sample FROM ${solidTable(solid)})`);
  const located = locationsSql(locations);
  if (located) where.push(located);
  return `SELECT *, sb + sc / 2 AS x, sc * ${HEIGHT} AS y,
                 ${colourSql(corners, grey)} AS colour
          FROM (
            SELECT sample, location, sample_date,
                   ${a} AS a, ${b} AS b, ${c} AS c,
                   ${a} / (${a} + ${b} + ${c}) AS sa,
                   ${b} / (${a} + ${b} + ${c}) AS sb,
                   ${c} / (${a} + ${b} + ${c}) AS sc,
                   CASE WHEN ${detected} THEN 'all' ELSE 'nd' END AS class
            FROM ${t}
            WHERE ${where.join(" AND ")})`;
}

/**
 * A point's colour in SQL: its shares' mix of the corners, grey for "nd".
 * floor(v + 0.5) on both sides (here and ternary.py), so the page and the
 * map agree to the last digit.
 */
export function colourSql(corners, grey = "#9a9a9a") {
  if (!Array.isArray(corners) || corners.length !== 3) return quote(grey);
  const [ca, cb, cc] = corners.map(hexChannels);
  const channel = (i) => `CAST(floor(sa * ${ca[i]} + sb * ${cb[i]} + sc * ${cc[i]} + 0.5) AS INTEGER)`;
  return `CASE WHEN class = 'all' THEN printf('#%02x%02x%02x', ${channel(0)}, `
    + `${channel(1)}, ${channel(2)}) ELSE ${quote(grey)} END`;
}

/** What the caption must state, over every sample carrying the three. */
export function ternaryCountsSql({ pivot, x, y, z, detectTrue }) {
  const t = table(pivot);
  const truth = `(${detectTrue.map((v) => quote(String(v).toUpperCase())).join(", ")})`;
  const [a, b, c] = [x, y, z].map((p) => column(p, "value"));
  const values = [x, y, z].map((p) => `${column(p, "value")} IS NOT NULL`).join(" AND ");
  const flags = [x, y, z].map((p) => `${column(p, "detect")} IS NOT NULL`).join(" AND ");
  const detected = [x, y, z].map((p) => `upper(${column(p, "detect")}) IN ${truth}`)
    .join(" AND ");
  const placeable = `${a} >= 0 AND ${b} >= 0 AND ${c} >= 0 AND ${a} + ${b} + ${c} > 0`;
  return `SELECT
      count(*) FILTER (WHERE ${values} AND ${flags} AND ${placeable}) AS placed,
      count(*) FILTER (WHERE ${values} AND ${flags} AND ${placeable} AND ${detected}) AS all_detected,
      count(*) FILTER (WHERE ${values} AND ${flags} AND ${placeable} AND NOT (${detected})) AS with_nd,
      count(*) FILTER (WHERE ${values} AND ${flags} AND NOT (${placeable})) AS unplaceable,
      count(*) FILTER (WHERE ${values} AND NOT (${flags})) AS unknown,
      count(*) FILTER (WHERE NOT (${values})) AS incomplete
    FROM ${t}`;
}

/**
 * Gridlines every `step` of each share, and the frame. Each line holds one
 * component constant; its label sits where that component's scale reads,
 * so each corner's percentages run along one edge in its colour.
 */
export function ternaryGrid(step = 0.2) {
  const lines = [];
  const ticks = [];
  for (let f = step; f < 1 - 1e-9; f += step) {
    const pct = `${Math.round(f * 100)}%`;
    // A held at f: from the bottom edge to the left edge... (b + c = 1 - f)
    const a0 = position(f, 1 - f, 0); const a1 = position(f, 0, 1 - f);
    lines.push({ x1: a0.x, y1: a0.y, x2: a1.x, y2: a1.y, axis: "a" });
    ticks.push({ ...a0, text: pct, axis: "a" });
    const b0 = position(0, f, 1 - f); const b1 = position(1 - f, f, 0);
    lines.push({ x1: b0.x, y1: b0.y, x2: b1.x, y2: b1.y, axis: "b" });
    ticks.push({ ...b0, text: pct, axis: "b" });
    const c0 = position(1 - f, 0, f); const c1 = position(0, 1 - f, f);
    lines.push({ x1: c0.x, y1: c0.y, x2: c1.x, y2: c1.y, axis: "c" });
    ticks.push({ ...c0, text: pct, axis: "c" });
  }
  const A = position(1, 0, 0); const B = position(0, 1, 0); const C = position(0, 0, 1);
  const frame = [[A, B], [B, C], [C, A]].map(([p, q]) => ({ x1: p.x, y1: p.y, x2: q.x, y2: q.y }));
  return { lines, ticks, frame, corners: { a: A, b: B, c: C } };
}

/** Rows as a VALUES list for a literal table: Mosaic marks read tables. */
export function valuesSql(rows, columns) {
  if (!rows.length) throw new Error("no rows");
  const lit = (v) => (typeof v === "number" ? String(v) : quote(v));
  return `SELECT * FROM (VALUES ${rows.map((r) => `(${columns.map((k) => lit(r[k])).join(", ")})`)
    .join(", ")}) AS t(${columns.join(", ")})`;
}

/** The caption: what is drawn, what is grey and why, and what is not drawn. */
export function ternaryCaption({ x, y, z, matrix = null, counts, drawn, points = null,
                                 allSamples = null, hideNd = false,
                                 pivotExcluded = null }) {
  const n = (v) => Number(v || 0).toLocaleString("en-US");
  const names = `${x.chemical} : ${y.chemical} : ${z.chemical}`;
  const unit = x.unit ? ` (shares of the sum, in ${x.unit})` : " (shares of the sum)";
  const lines = [`${names}${unit}${matrix ? " · " + matrix : ""}`
    + ` · ${pointsPhrase(drawn, points, allSamples, "all three")}`];
  const parts = [`● all three detected ${n(counts.all_detected)}`,
                 `○ a non-detect ${n(counts.with_nd)}, grey at its limit`
                 + (hideNd ? " (hidden)" : "")];
  lines.push(parts.join(" · "));
  const left = [];
  if (Number(counts.incomplete)) left.push(`${n(counts.incomplete)} samples carry only some of the three`);
  if (Number(counts.unknown)) left.push(`${n(counts.unknown)} have a blank detect flag`);
  if (Number(counts.unplaceable)) {
    left.push(`${n(counts.unplaceable)} have a value below zero or a zero sum`);
  }
  if (pivotExcluded && Number(pivotExcluded.field_replicates)) {
    left.push(`${n(pivotExcluded.field_replicates)} field duplicates and replicates`);
  }
  if (left.length) lines.push(`Not drawn: ${left.join("; ")}.`);
  // AWAY, not toward: a non-detect is drawn at its limit and the true value
  // is at most that, so its true share is smaller and the sample's true
  // place is further from that analyte's corner than drawn.
  lines.push("A grey point is drawn with each non-detect at its limit; the "
    + "true value is at most that, so the sample lies further from that "
    + "analyte's corner than drawn.");
  return lines;
}
