/**
 * The views linked to a scatter or ternary: a histogram per analyte and a
 * censored box plot, all brushing each other and the map.
 *
 * ONE TABLE, ONE GRAIN. Every view here reads `lk_<n>`: the chart's own
 * rows (one per SAMPLE, the pivot's grain - CLAUDE.md, "a multivariate
 * point is one sample") plus each analyte's value and detection. A
 * per-result histogram beside a per-sample scatter would link two
 * different populations, and a brush in one would dim the other by a
 * rule nobody could state.
 *
 * ONE SELECTION. `cross` is a Mosaic crossfilter: the scatter's rectangle
 * and each histogram's interval publish to it, and every view is filtered
 * by everyone else's brush (decided 2026-10-20: DIM what falls outside,
 * never remove it - each view keeps a faint layer of every sample). The
 * scatter also dims inside its own brush, as before, by an intersect that
 * includes `cross` (scatterplot.js). What the MAP highlights is the same
 * predicate, asked once here - not decided again by each chart.
 *
 * `solid` is the samples the chart draws solid - its Points mode, the
 * time window and the map's selection - as a column, and a fixed clause
 * on `fg`. It is a column rather than a narrower view because Mosaic bins
 * each mark from its own table's extent: a selected layer read from a
 * narrower view would bin differently from the grey layer behind it.
 */
import { BoxChart, whereOf } from "./boxchart.js";
import { GROUPS } from "./censored.js";
import { scatterBrushLabel } from "./scatter.js";

const BRUSH_SETTLE_MS = 80;
const ACCENT = "#3b6ea8";
const ND_GREY = "#a9a9a9";
const SOLID = { linked: "solid" };   // the fixed clause's source

let serial = 0;
const made = [];

const quote = (text) => `'${String(text).replace(/'/g, "''")}'`;

function ident(name, what) {
  if (!/^[a-z][a-z0-9_]*$/.test(String(name))) throw new Error(`not ${what}: ${name}`);
  return name;
}

/**
 * The linked table: the chart's `all` view, each analyte's value (v<i>)
 * and detection (d<i>, NULL for a blank flag - unknown, not a non-detect;
 * k<i> as words for the histogram's fill), and `solid`.
 */
export function linkedTableSql({ name, all, shown, pivot, columns, detectTrue }) {
  if (!/^lk_[0-9]+$/.test(name)) throw new Error(`not a linked table: ${name}`);
  if (!Array.isArray(detectTrue) || !detectTrue.length) {
    throw new Error("linkedTableSql needs the host's detect-true values");
  }
  const truth = `(${detectTrue.map((v) => quote(String(v).toUpperCase())).join(", ")})`;
  const parts = columns.map((c, i) => {
    const p = ident(c.prefix, "a pivot prefix");
    const d = `CASE WHEN p.${p}_detect IS NULL THEN NULL
                    ELSE upper(p.${p}_detect) IN ${truth} END`;
    return `p.${p}_value::DOUBLE AS v${i}, ${d} AS d${i},
            CASE WHEN ${d} IS NULL THEN 'unknown' WHEN ${d} THEN 'detected'
                 ELSE 'non-detect' END AS k${i}`;
  });
  return `CREATE OR REPLACE TEMP TABLE ${name} AS
          SELECT c.*, ${parts.join(", ")},
                 c.sample IN (SELECT sample FROM ${ident(shown, "a view")}) AS solid
          FROM ${ident(all, "a view")} c
          JOIN ${ident(pivot, "a pivot")} p ON p.sample = c.sample`;
}

/** What the map highlights: the samples every brush leaves, and where. */
export function brushedSql(table, where) {
  return `SELECT count(*) AS samples, count(DISTINCT location) AS locations,
                 list(DISTINCT location) AS codes
          FROM ${table} WHERE solid${where.map((w) => ` AND (${w})`).join("")}`;
}

export class Linked {
  constructor({ vg, coordinator, element, cross, onBrush = null }) {
    this.vg = vg;
    this.coordinator = coordinator;
    this.element = element;
    this.cross = cross;
    this.onBrush = onBrush;
    this.fg = vg.Selection.crossfilter({ include: [cross] });
    this._timer = null;
    this._lastSent = undefined;
    this._destroyed = false;
    this._onCross = () => this._brushed();
    cross.addEventListener("value", this._onCross);
  }

  destroy() {
    this._destroyed = true;
    if (this._timer) clearTimeout(this._timer);
    this.cross.removeEventListener("value", this._onCross);
    if (this.box) this.box.destroy();
  }

  /**
   * @param o
   *  - views     {all, shown} of the chart (scatterplot/ternaryplot)
   *  - pivot     the chart table the chart reads
   *  - columns   [{prefix, chemical, unit, fraction, field, axis}] - `field`
   *              is the chart view's column for that analyte (x, y; a, b, c)
   *  - detectTrue, total (samples drawn, for the brush line)
   */
  async show(options) {
    try {
      return await this._show(options);
    } catch (error) {
      // A newer draw cleared the coordinator under this one: Mosaic rejects
      // the pending queries with "Cleared". That is supersession, not a
      // failure, and saying "failed" for it would be noise.
      if (this._destroyed) return undefined;
      throw error;
    }
  }

  async _show({ views, pivot, columns, detectTrue, total = 0 }) {
    const vg = this.vg;
    const name = `lk_${++serial}`;
    this.table = name;
    this.total = total;
    this.columns = columns;
    await this.coordinator.exec(linkedTableSql({
      name, all: views.all, shown: views.shown, pivot, columns, detectTrue }));
    // Keep the last few: a chart still on screen must not lose its table.
    made.push(name);
    while (made.length > 3) {
      await this.coordinator.exec(`DROP TABLE IF EXISTS ${made.shift()}`);
    }
    if (this._destroyed) return;
    this.fg.update({ source: SOLID, value: true, predicate: vg.column("solid") });
    const mins = (await this.coordinator.query(
      `SELECT ${columns.map((_, i) => `min(v${i}) AS m${i}`).join(", ")} FROM ${name}`,
      { type: "json", cache: false }))[0] || {};
    const cols = columns.map((c, i) => ({
      ...c, v: `v${i}`, d: `d${i}`, k: `k${i}`,
      log: (c.axis || (Number(mins[`m${i}`]) > 0 ? "log" : "linear")) === "log",
      label: c.chemical + (c.fraction ? ` (${c.fraction})` : ""),
    }));
    this.cols = cols;

    const strip = document.createElement("div");
    strip.className = "linked-line";
    this._line = document.createElement("span");
    this._line.className = "brush";
    this._line.textContent = scatterBrushLabel({ total });
    strip.append(this._line);

    const hists = document.createElement("div");
    hists.className = "linked-hists";
    const width = Math.max(160, Math.floor(((this.element.clientWidth || 600) - 8)
                                           / Math.min(cols.length, 3)) - 6);
    for (const c of cols) {
      const cell = document.createElement("div");
      cell.className = "card hist";
      cell.append(this._histogram(c, width));
      hists.append(cell);
    }

    const boxCard = document.createElement("div");
    boxCard.className = "card box-card";
    const bar = document.createElement("div");
    bar.className = "box-bar";
    const pick = document.createElement("select");
    cols.forEach((c, i) => pick.append(new Option(c.chemical, String(i))));
    const group = document.createElement("select");
    for (const [key, g] of Object.entries(GROUPS)) {
      group.append(new Option(`by ${g.label.toLowerCase()}`, key, key === "location",
                              key === "location"));
    }
    bar.append(pick, group);
    const area = document.createElement("div");
    area.className = "box-area";
    boxCard.append(bar, area);

    this.element.replaceChildren(strip, hists, boxCard);
    this.box = new BoxChart({ element: area, filterBy: this.fg, source: name,
                              coordinator: this.coordinator });
    this.coordinator.connect(this.box);
    const set = () => this.box.set({ column: cols[Number(pick.value)],
                                     group: group.value });
    pick.addEventListener("change", set);
    group.addEventListener("change", set);
    await set();
  }

  _histogram(c, width) {
    const vg = this.vg;
    const bin = () => vg.bin(c.field, { steps: 40 });
    return vg.plot(
      // Context: every sample, faint, never filtered. The brush rides on it.
      vg.rectY(vg.from(this.table), { x: bin(), y: vg.count(),
                                      fill: "#e3e6ea", inset: 0.5 }),
      vg.intervalX({ as: this.cross }),
      vg.rectY(vg.from(this.table, { filterBy: this.fg }),
               { x: bin(), y: vg.count(), fill: c.k, inset: 0.5 }),
      vg.colorDomain(["detected", "non-detect", "unknown"]),
      vg.colorRange([ACCENT, ND_GREY, "#d9b45a"]),
      vg.xScale(c.log ? "log" : "linear"),
      ...(c.log ? [vg.xTickFormat("~g")] : []),
      // SQUARE-ROOT COUNTS. Non-detects pile up at a handful of limits:
      // 41,821 of water's 63,222 Mn x Pb samples sit on 19 limit pairs, and
      // on a linear axis that one bar flattens every detection to nothing
      // (seen 2026-10-20). The axis is labelled so, because a sqrt axis
      // understates a tall bar's lead.
      // Each tick on its own: d3's "~s" shares one prefix across the ticks
      // and wrote 0 as "0k".
      vg.yScale("sqrt"), vg.yTickFormat((d) => (d >= 1000 ? `${d / 1000}k` : String(d))),
      vg.xLabel(`${c.label}${c.unit ? ` (${c.unit})` : ""} →`),
      vg.yLabel("√n"), vg.yTicks(3), vg.xTicks(4),
      vg.width(width), vg.height(96),
      vg.marginLeft(34), vg.marginRight(8), vg.marginTop(6), vg.marginBottom(28),
    );
  }

  _brushed() {
    if (this._timer) clearTimeout(this._timer);
    this._timer = setTimeout(() => this._emit(), BRUSH_SETTLE_MS);
  }

  async _emit() {
    if (this._destroyed || !this.table) return;
    const where = whereOf(this.cross.predicate(null));
    const key = where.join(" AND ");
    if (key === this._lastSent) return;
    this._lastSent = key;
    if (!where.length) {
      this._line.textContent = scatterBrushLabel({ total: this.total });
      if (this.onBrush) this.onBrush(null);
      return;
    }
    let rows;
    try {
      rows = await this.coordinator.query(brushedSql(this.table, where),
                                          { type: "json", cache: false });
    } catch (error) {
      if (this._destroyed) return;
      throw error;
    }
    if (this._destroyed) return;
    const hit = rows[0] || { samples: 0, locations: 0, codes: [] };
    const label = scatterBrushLabel({ samples: hit.samples, locations: hit.locations,
                                      total: this.total });
    this._line.textContent = label;
    if (this.onBrush) this.onBrush({ locations: Array.from(hit.codes || []), label });
  }
}
