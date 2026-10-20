/**
 * Box plots of one analyte by group, Kaplan-Meier, following the brushes.
 *
 * The statistics are censored.js's; this draws them. A MosaicClient, so
 * the coordinator re-asks whenever any brush in the linked selection
 * moves, with the brush's predicate ANDed into the query. Each group is a
 * row: a faint box of EVERY sample behind (the context, never filtered)
 * and a solid box of the samples the brushes and the map leave in (US-24
 * criterion 6 for the scatter, applied here: dim, do not remove).
 *
 * WHAT IS DRAWN WHERE THE DATA CANNOT SAY. A quartile below the lowest
 * detection is unknown, so the box is not extended to a guess:
 *
 *   * Q1 unknown: the box starts at the lowest detection, hatched, with an
 *     open dashed left edge - "the lower quarter is somewhere below here";
 *   * median unknown too: no median tick, and the hatch runs to Q3;
 *   * Q3 unknown (three quarters or more below the lowest detection): no
 *     box, a "<" at the lowest detection;
 *   * no detections at all: a grey "<" at the highest limit.
 *
 * A grey tick marks each group's highest non-detect limit, which is where
 * the censoring stops being able to hide anything (Helsel's censored box
 * plot draws the same line).
 */
import { MosaicClient } from "@uwdata/vgplot";
import { boxSummary, kmBoxSqlOver } from "./censored.js";

const NS = "http://www.w3.org/2000/svg";
const ROW = 15;
const LABEL = 86;
const NOTE = 64;
const MAX_ROWS = 40;

/** A filter predicate from Mosaic, as SQL strings to AND. */
export function whereOf(filter) {
  if (!filter) return [];
  return (Array.isArray(filter) ? filter : [filter])
    .map((p) => (p === null || p === undefined ? "" : String(p)))
    .filter((s) => s && s !== "TRUE" && s !== "true");
}

/** Rows from a query answer: an Arrow table (Mosaic's default) or JSON. */
function rowsOf(data) {
  if (!data) return [];
  if (Array.isArray(data)) return data;
  if (typeof data.toArray === "function") return data.toArray().map((r) => ({ ...r }));
  return [];
}

const svg = (name, attrs = {}, text = null) => {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== null) node.textContent = text;
  return node;
};

export class BoxChart extends MosaicClient {
  /**
   * @param {object} o
   *  - element     where to draw
   *  - filterBy    the linked selection (linked.js's `fg`)
   *  - source      the linked table (lk_<n>)
   *  - coordinator for the faint, unfiltered boxes
   */
  constructor({ element, filterBy, source, coordinator }) {
    super(filterBy);
    this.element = element;
    this.source = source;
    this._coord = coordinator;
    this.column = null;     // { v, d, label, unit, log }
    this.group = "location";
    this.ghost = [];
    this.solid = null;
  }

  /** Which analyte and grouping. Re-asks both layers. */
  async set({ column = this.column, group = this.group } = {}) {
    this.column = column;
    this.group = group;
    if (!this.column) return;
    try {
      this.ghost = rowsOf(await this._coord.query(this._sql([]),
                                                  { type: "json", cache: false }));
    } catch (error) {
      if (!this.coordinator) return;   // disconnected: a newer draw cleared it
      throw error;
    }
    this.solid = null;
    this.render();
    if (this.coordinator) this.requestQuery();
  }

  _sql(where) {
    const c = this.column;
    return kmBoxSqlOver({ source: this.source, value: c.v, detected: c.d,
                          group: this.group, where, log: c.log });
  }

  query(filter) {
    if (!this.column) return null;
    return this._sql(whereOf(filter));
  }

  queryResult(data) {
    this.solid = rowsOf(data);
    this.render();
    return this;
  }

  queryError(error) {
    this.element.textContent = `Box plot failed: ${error && error.message || error}`;
    return this;
  }

  render() {
    const c = this.column;
    if (!c) return;
    const ghost = this.ghost || [];
    const solid = new Map((this.solid || []).map((r) => [r.grp, r]));
    const width = Math.max(260, this.element.clientWidth || 360);
    // Order by the SOLID medians when a brush is live, so the groups it
    // picks out rise to the top; the ghost's order otherwise.
    const order = ghost.slice().sort((a, b) => {
      const sa = solid.get(a.grp);
      const sb = solid.get(b.grp);
      if (this.solid && !!sa !== !!sb) return sa ? -1 : 1;
      const ka = (sa || a).med ?? (sa || a).min_detect ?? -Infinity;
      const kb = (sb || b).med ?? (sb || b).min_detect ?? -Infinity;
      return kb - ka;
    });
    const rows = order.slice(0, MAX_ROWS);
    const values = [];
    for (const r of ghost) {
      for (const k of ["lo_whisker", "hi_whisker", "min_detect", "max_detect",
                       "max_limit", "q1", "q3"]) {
        const v = Number(r[k]);
        if (r[k] !== null && r[k] !== undefined && Number.isFinite(v)
            && (!c.log || v > 0)) values.push(v);
      }
    }
    if (!values.length) {
      this.element.textContent = `No ${c.label} values to box.`;
      return;
    }
    let lo = Math.min(...values);
    let hi = Math.max(...values);
    if (lo === hi) { lo = c.log ? lo / 2 : lo - 1; hi = c.log ? hi * 2 : hi + 1; }
    const plotW = width - LABEL - NOTE - 8;
    const tx = c.log
      ? (v) => LABEL + ((Math.log10(v) - Math.log10(lo)) / (Math.log10(hi) - Math.log10(lo))) * plotW
      : (v) => LABEL + ((v - lo) / (hi - lo)) * plotW;
    const clampX = (v) => Math.max(LABEL, Math.min(LABEL + plotW, tx(v)));
    const height = rows.length * ROW + 22;
    const root = svg("svg", { width, height, class: "boxes",
                              viewBox: `0 0 ${width} ${height}` });
    const defs = svg("defs");
    const pattern = svg("pattern", { id: "km-hatch", width: 4, height: 4,
                                     patternUnits: "userSpaceOnUse",
                                     patternTransform: "rotate(45)" });
    pattern.append(svg("rect", { width: 4, height: 4, fill: "rgba(59,110,168,0.12)" }),
                   svg("line", { x1: 0, y1: 0, x2: 0, y2: 4, stroke: "#3b6ea8",
                                 "stroke-width": 1.2 }));
    defs.append(pattern);
    root.append(defs);

    rows.forEach((g, i) => {
      const y = i * ROW + 2;
      const mid = y + ROW / 2;
      const s = solid.get(g.grp);
      const row = svg("g", { class: "box-row" });
      row.append(svg("title", {}, [boxSummary(g, c.unit) + " - all samples",
                                   s ? boxSummary(s, c.unit) + " - selected" : "none selected"]
        .join("\n")));
      row.append(svg("rect", { x: 0, y, width, height: ROW, fill: "transparent" }));
      row.append(svg("text", { x: LABEL - 4, y: mid + 4, "text-anchor": "end",
                               class: s || !this.solid ? "box-label" : "box-label dim" },
                     String(g.grp).length > 14 ? String(g.grp).slice(0, 13) + "…" : g.grp));
      this._box(row, g, mid, clampX, true);
      if (s) this._box(row, s, mid, clampX, false);
      const shown = s || g;
      const pct = Number(shown.n) ? Math.round((100 * Number(shown.nondetects)) / Number(shown.n)) : 0;
      row.append(svg("text", { x: width - NOTE + 4, y: mid + 4, class: "box-note" },
                     `${Number(shown.n)}${pct ? ` · ${pct}% ND` : ""}`));
      root.append(row);
    });
    // The axis: a few ticks at decades (log) or round numbers.
    const axisY = rows.length * ROW + 6;
    root.append(svg("line", { x1: LABEL, x2: LABEL + plotW, y1: axisY, y2: axisY,
                              stroke: "#999" }));
    for (const t of ticks(lo, hi, c.log)) {
      const x = tx(t);
      root.append(svg("line", { x1: x, x2: x, y1: axisY, y2: axisY + 3, stroke: "#999" }));
      root.append(svg("text", { x, y: axisY + 13, "text-anchor": "middle",
                                class: "box-tick" }, fmt(t)));
    }
    const head = document.createElement("div");
    head.className = "box-head";
    const more = order.length > rows.length ? ` · top ${rows.length} of ${order.length}` : "";
    head.textContent = `${c.label}${c.unit ? ` (${c.unit})` : ""}, Kaplan-Meier${more}`;
    this.element.replaceChildren(head, root);
  }

  _box(parent, r, mid, x, faint) {
    const k = (name) => (r[name] === null || r[name] === undefined ? null : Number(r[name]));
    const h = faint ? 9 : 6;
    const top = mid - h / 2;
    const stroke = faint ? "#c9ced4" : "#3b6ea8";
    const fill = faint ? "#eceff2" : "rgba(59,110,168,0.30)";
    if (k("max_limit") !== null && !faint) {
      const lx = x(k("max_limit"));
      parent.append(svg("line", { x1: lx, x2: lx, y1: mid - 6, y2: mid + 6,
                                  stroke: "#8a8a8a", "stroke-dasharray": "2,1" }));
    }
    if (!Number(r.detects)) {
      if (k("max_limit") !== null) {
        parent.append(svg("text", { x: x(k("max_limit")) - 2, y: mid + 4,
                                    "text-anchor": "end",
                                    class: faint ? "box-lt faint" : "box-lt" }, "<"));
      }
      return;
    }
    const lw = k("lo_whisker");
    const hw = k("hi_whisker");
    const q1 = k("q1");
    const med = k("med");
    const q3 = k("q3");
    const lowest = k("min_detect");
    if (q3 === null) {
      // Three quarters or more below the lowest detection.
      parent.append(svg("text", { x: x(lowest) - 2, y: mid + 4, "text-anchor": "end",
                                  class: faint ? "box-lt faint" : "box-lt" }, "<"));
      if (hw !== null) {
        parent.append(svg("line", { x1: x(lowest), x2: x(hw), y1: mid, y2: mid, stroke }));
      }
      return;
    }
    const left = q1 === null ? lowest : q1;
    if (hw !== null) parent.append(svg("line", { x1: x(q3), x2: x(hw), y1: mid, y2: mid, stroke }));
    if (lw !== null && q1 !== null) {
      parent.append(svg("line", { x1: x(lw), x2: x(q1), y1: mid, y2: mid, stroke }));
    }
    const bx = x(left);
    const bw = Math.max(1, x(q3) - bx);
    parent.append(svg("rect", {
      x: bx, y: top, width: bw, height: h,
      fill: q1 === null && !faint ? "url(#km-hatch)" : fill,
      stroke, "stroke-width": faint ? 1 : 1.2,
    }));
    if (q1 === null && !faint) {
      // The open edge: the box goes on below, nobody knows how far.
      parent.append(svg("line", { x1: bx, x2: bx, y1: top - 1, y2: top + h + 1,
                                  stroke: "#fff", "stroke-width": 2 }));
      parent.append(svg("line", { x1: bx, x2: bx, y1: top - 1, y2: top + h + 1,
                                  stroke, "stroke-dasharray": "2,2" }));
      parent.append(svg("text", { x: bx - 2, y: mid + 4, "text-anchor": "end",
                                  class: "box-lt" }, "<"));
    }
    if (med !== null) {
      parent.append(svg("line", { x1: x(med), x2: x(med), y1: top - 1, y2: top + h + 1,
                                  stroke: faint ? "#9aa3ad" : "#173d66",
                                  "stroke-width": faint ? 1 : 2 }));
    }
  }
}

function ticks(lo, hi, log) {
  if (log) {
    const out = [];
    for (let e = Math.ceil(Math.log10(lo)); e <= Math.floor(Math.log10(hi)); e++) {
      out.push(10 ** e);
    }
    return out.length >= 2 ? out : [lo, hi];
  }
  const step = 10 ** Math.floor(Math.log10((hi - lo) / 4 || 1));
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(v);
  return out.length > 8 ? out.filter((_, i) => i % 2 === 0) : out;
}

function fmt(v) {
  return Number(v).toLocaleString("en-US", { maximumSignificantDigits: 3 });
}
