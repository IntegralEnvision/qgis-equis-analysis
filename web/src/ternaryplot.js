/**
 * The ternary plot: three analytes as shares of their sum, in a triangle.
 *
 * The decisions live in ternary.js (SQL, geometry, words), held to
 * equis_loader/ternary.py by tools/test_ternary.py; this is the vgplot
 * wiring. It extends Scatter for everything a chart of samples shares -
 * the brush and its debounce, the busy message, teardown, the drawn-time
 * probe - and draws its own marks:
 *
 *   * every placeable sample faint, never filtered: the context, as in
 *     the scatter, so a Points mode or a time window dims the triangle
 *     rather than emptying it;
 *   * the mode's samples solid on top: detected on all three in their
 *     composition's colour; any non-detect grey and hollow AT ITS LIMIT,
 *     unless hidden by the switch (decided 2026-10-12).
 *
 * vgplot has no ternary projection, so the triangle is drawn in plain x/y:
 * gridlines, frame, ticks and corner names are small literal tables, each
 * new per draw (tv_<serial>_...), like every view here - Mosaic caches on
 * the SQL string.
 */
import { Scatter } from "./scatterplot.js";
import { scatterBrushLabel } from "./scatter.js";
import {
  HEIGHT, ternaryCaption, ternaryCountsSql, ternaryGrid, ternarySql, valuesSql,
} from "./ternary.js";

let drawSerial = 0;
// The "show non-detects" switch, kept for the page's life: switching
// analytes should not switch it back.
let hideNonDetects = false;

export class Ternary extends Scatter {
  async show(options) {
    if (this._destroyed) return this.state;
    this._options = options;
    const started = performance.now();
    const timings = {};
    const lap = (name) => { timings[name] = Math.round(performance.now() - started); };
    const names = [options.x, options.y, options.z].map((m) => m.chemical);
    this._busy(`Placing ${names.join(" : ")}…`);
    const serial = ++drawSerial;
    const view = (part) => `tv_${serial}_${part}`;
    const all = view("all");
    const set = view("set");
    const shown = view("shown");
    this._view = shown;
    const solid = options.solid || null;
    const t = options.ternary || {};
    const base = { pivot: options.pivot, x: options.x.prefix, y: options.y.prefix,
                   z: options.z.prefix, detectTrue: options.detectTrue,
                   corners: t.corners || null, grey: t.grey || "#9a9a9a" };

    const [counts] = await this.coordinator.query(ternaryCountsSql(base),
                                                  { type: "json", cache: false });
    if (this._destroyed) return this.state;
    lap("counts");
    const exec = (sql) => this.coordinator.exec(sql);
    await exec(`CREATE OR REPLACE TEMP VIEW ${all} AS ${ternarySql(base)}`);
    await exec(`CREATE OR REPLACE TEMP VIEW ${set} AS ${ternarySql({ ...base, solid })}`);
    await exec(`CREATE OR REPLACE TEMP VIEW ${shown} AS ${ternarySql({
      ...base, solid,
      locations: Array.isArray(options.locations) ? options.locations : null })}`);
    for (const cls of ["all", "nd"]) {
      await exec(`CREATE OR REPLACE TEMP VIEW ${view(cls)} AS
                  SELECT * FROM ${shown} WHERE class = '${cls}'`);
    }
    const grid = ternaryGrid(0.2);
    await exec(`CREATE OR REPLACE TEMP TABLE ${view("grid")} AS
                ${valuesSql(grid.lines, ["x1", "y1", "x2", "y2", "axis"])}`);
    await exec(`CREATE OR REPLACE TEMP TABLE ${view("frame")} AS
                ${valuesSql(grid.frame, ["x1", "y1", "x2", "y2"])}`);
    await exec(`CREATE OR REPLACE TEMP TABLE ${view("ticks")} AS
                ${valuesSql(grid.ticks, ["x", "y", "text", "axis"])}`);
    const c = grid.corners;
    await exec(`CREATE OR REPLACE TEMP TABLE ${view("corners")} AS ${valuesSql([
      { x: c.a.x, y: c.a.y, text: names[0], corner: "a" },
      { x: c.b.x, y: c.b.y, text: names[1], corner: "b" },
      { x: c.c.x, y: c.c.y, text: names[2], corner: "c" },
    ], ["x", "y", "text", "corner"])}`);
    if (this._destroyed) return this.state;
    lap("views");

    const perClass = await this.coordinator.query(
      `SELECT class, count(*) AS n FROM ${set} GROUP BY class`,
      { type: "json", cache: false });
    const drawnCounts = { all: 0, nd: 0 };
    for (const r of perClass) drawnCounts[r.class] = Number(r.n);
    const drawn = drawnCounts.all + (hideNonDetects ? 0 : drawnCounts.nd);
    const allSamples = solid ? Number(counts.placed) : null;
    this.element.replaceChildren();
    this.state = { total: drawn, counts: { ...counts, drawn: drawnCounts },
                   kind: "ternary" };
    if (!Number(counts.placed)) {
      this.element.append(this._note(`No sample carries ${names.join(", ")} with `
        + "known values and detect flags"
        + `${options.matrix ? " in " + options.matrix : ""}.`));
      return this.state;
    }

    const vg = this.vg;
    // Equilateral: the plot's height is its width times sqrt(3)/2, inside
    // margins wide enough for the tick labels and corner names.
    const margins = { left: 40, right: 40, top: 26, bottom: 44 };
    const plotWidth = Math.max(260, Math.min(
      (this.element.clientWidth || 600) - margins.left - margins.right - 12,
      ((this.element.clientHeight || 600) - 160) / HEIGHT));
    const width = plotWidth + margins.left + margins.right;
    const height = plotWidth * HEIGHT + margins.top + margins.bottom;
    const corners = t.corners || ["#555555", "#555555", "#555555"];
    const grey = t.grey || "#9a9a9a";

    const marks = [
      vg.link(vg.from(view("grid")), { x1: "x1", y1: "y1", x2: "x2", y2: "y2",
                                       stroke: "#e2e2e2", strokeWidth: 0.6 }),
      vg.link(vg.from(view("frame")), { x1: "x1", y1: "y1", x2: "x2", y2: "y2",
                                        stroke: "#777777", strokeWidth: 1 }),
      // Context: every placeable sample, faint, never filtered; the brush
      // rides on it.
      vg.dot(vg.from(all), { x: "x", y: "y", r: 1.8, fill: "#c4c4c4",
                             fillOpacity: 0.35, stroke: "none" }),
      vg.intervalXY({ as: this.selection }),
      vg.dot(vg.from(view("all"), { filterBy: this.selection }),
             { x: "x", y: "y", r: 2.8, fill: "colour", stroke: "#4d4d4d",
               strokeWidth: 0.35 }),
    ];
    if (!hideNonDetects) {
      marks.push(vg.dot(vg.from(view("nd"), { filterBy: this.selection }),
                        { x: "x", y: "y", r: 3, fill: "none", stroke: grey,
                          strokeWidth: 1 }));
    }
    // Each component's ticks along the edge where its scale reads.
    for (const [axis, anchor, dx, dy] of [["a", "middle", 0, 12],
                                          ["b", "start", 6, 0],
                                          ["c", "end", -6, 0]]) {
      const name = `${view("ticks")}_${axis}`;
      await exec(`CREATE OR REPLACE TEMP VIEW ${name} AS
                  SELECT * FROM ${view("ticks")} WHERE axis = '${axis}'`);
      marks.push(vg.text(vg.from(name), {
        x: "x", y: "y", text: "text", fontSize: 9, textAnchor: anchor, dx, dy,
        fill: corners[{ a: 0, b: 1, c: 2 }[axis]] }));
    }
    // A and B under their corners, anchored inward so a long name stays
    // in the frame; C above its own.
    for (const [corner, anchor, dx, dy] of [["a", "start", -24, 30],
                                            ["b", "end", 24, 30],
                                            ["c", "middle", 0, -10]]) {
      const name = `${view("corners")}_${corner}`;
      await exec(`CREATE OR REPLACE TEMP VIEW ${name} AS
                  SELECT * FROM ${view("corners")} WHERE corner = '${corner}'`);
      marks.push(vg.text(vg.from(name), {
        x: "x", y: "y", text: "text", fontSize: 12, fontWeight: "bold",
        textAnchor: anchor, dx, dy, fill: corners[{ a: 0, b: 1, c: 2 }[corner]] }));
    }
    this._busy(`Drawing ${drawn.toLocaleString("en-US")} samples…`);
    const chart = vg.plot(
      ...marks,
      vg.colorScale("identity"),
      vg.xDomain([0, 1]), vg.yDomain([0, HEIGHT]),
      vg.xAxis(null), vg.yAxis(null),
      vg.marginLeft(margins.left), vg.marginRight(margins.right),
      vg.marginTop(margins.top), vg.marginBottom(margins.bottom),
      vg.width(width), vg.height(height),
    );
    this.element.replaceChildren(chart);
    this.element.append(this._ternaryLegend(options, drawnCounts));
    for (const line of ternaryCaption({
      x: options.x, y: options.y, z: options.z, matrix: options.matrix,
      counts: { ...counts, all_detected: drawnCounts.all, with_nd: drawnCounts.nd },
      drawn, points: options.points || null, allSamples,
      hideNd: hideNonDetects, pivotExcluded: options.excluded || null,
    })) {
      const node = document.createElement("div");
      node.className = "caption";
      node.textContent = line;
      this.element.append(node);
    }
    this._brushNode = document.createElement("div");
    this._brushNode.className = "brush";
    this._brushNode.textContent = scatterBrushLabel({ total: drawn });
    this.element.append(this._brushNode);
    if (!this._listening) {
      this._listening = true;
      this.selection.addEventListener("value", () => this._brushed());
    }
    lap("plot built");
    this._whenDrawn(chart).then((ms) => {
      timings.drawn = ms === null ? null : Math.round(ms - started);
      this.state.timings = timings;
      if (this.onTimings) this.onTimings(timings);
    });
    return this.state;
  }

  /** The corner hues and the non-detect switch, on one line. */
  _ternaryLegend(options, drawnCounts) {
    const node = document.createElement("div");
    node.className = "legend";
    const t = options.ternary || {};
    const note = document.createElement("div");
    note.className = "bikey-note";
    const hues = [options.x, options.y, options.z].map((m, i) => {
      const span = document.createElement("span");
      span.style.color = (t.corners || [])[i] || "#555";
      span.textContent = `■ ${m.chemical}`;
      return span;
    });
    note.append(...hues.flatMap((h) => [h, document.createTextNode("  ")]),
                document.createTextNode("Colour is the mix of the three shares. "));
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = !hideNonDetects;
    box.addEventListener("change", () => {
      hideNonDetects = !box.checked;
      this.show(this._options);
    });
    label.append(box, document.createTextNode(
      ` show non-detects (${Number(drawnCounts.nd).toLocaleString("en-US")}, grey at their limit)`));
    note.append(label);
    node.append(note);
    return node;
  }
}

