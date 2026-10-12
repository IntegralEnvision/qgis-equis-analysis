/**
 * US-24: the scatter itself, its brush, and the map's selection dimming it.
 *
 * The decisions live in scatter.js and are tested without a browser; this
 * is the vgplot wiring around them, written to the same rules the histogram
 * learned the hard way (chart.js):
 *
 *   * every directive comes from the API context, never the bare import -
 *     a plot built from the import connects its marks to nothing;
 *   * NO VIEW NAME IS EVER REUSED. Mosaic caches on the SQL string, and the
 *     histogram served one analyte's bars under another's caption until the
 *     cache was cleared (00afe8d, 59328f7). Here the pivot table is new per
 *     build and the views are new per draw, so a stale entry is
 *     unreachable rather than wrong;
 *   * the brush is debounced on the way OUT, to the map, never on the way in.
 *
 * DIM, DO NOT REMOVE (US-24 criterion 6). Every paired sample is drawn
 * faintly as context; the samples in the map's selection AND the brush are
 * drawn over them in their class symbol. The axes come from the faint layer,
 * so selecting on the map never rescales them (criterion 4), and the brush
 * filters the solid layer only, which is why the selection is intersect():
 * a view responds to its own brush.
 */
import {
  CLASSES, axisLabel, axisType, breakLabels, brushSql, excludedSql,
  scatterBrushLabel,
  scatterCaption, scatterCountsSql, scatterSql,
} from "./scatter.js";

const BRUSH_SETTLE_MS = 80;

let drawSerial = 0;

export class Scatter {
  /**
   * @param {object} options
   *  - vg, coordinator  from createAPIContext / the page
   *  - element          where to draw
   *  - onBrush          ({locations, label}) or (null) when cleared
   */
  constructor({ vg, coordinator, element, onBrush = null }) {
    this.vg = vg;
    this.coordinator = coordinator;
    this.element = element;
    this.onBrush = onBrush;
    this.selection = vg.Selection.intersect();
    this.state = null;
    this._timer = null;
    this._lastSent = undefined;
    this._destroyed = false;
  }

  destroy() {
    this._destroyed = true;
    this.coordinator.clear({ clients: true, cache: true });
    if (this._timer) {
      clearTimeout(this._timer);
      this._timer = null;
    }
  }

  /** A spinner and a message in the chart area, while the chart is built. */
  _busy(message) {
    const node = document.createElement("div");
    node.className = "busy";
    node.textContent = message;
    this.element.replaceChildren(node);
  }

  async show(options) {
    if (this._destroyed) return this.state;
    this._options = options;
    // Where the time goes, logged with the draw so a slow open can be
    // pinned on a phase instead of guessed at (asked for 2026-10-09).
    const started = performance.now();
    const timings = {};
    const lap = (name) => { timings[name] = Math.round(performance.now() - started); };
    this._busy(`Preparing ${options.x.chemical} × ${options.y.chemical}…`);
    const serial = ++drawSerial;
    const all = `sv_${serial}_all`;
    const shown = `sv_${serial}_shown`;
    // The points mode's samples (one per location, or those in the time
    // window), WITHOUT the map selection: what the counts and caption
    // describe. `all` stays every sample, faint, and sets the axes, so a
    // mode or a window dims the plot and never rescales it (2026-10-12).
    const set = `sv_${serial}_set`;
    const solid = options.solid || null;
    this._view = shown;

    const base = { pivot: options.pivot, x: options.x.prefix,
                   y: options.y.prefix, detectTrue: options.detectTrue,
                   bivariate: options.bivariate || null };
    const grey = (options.bivariate || {}).grey || "#9a9a9a";
    // COUNT FIRST: the axis types decide which rows the views hold (a log
    // axis leaves out values of 0 or below), so they come before the views.
    this._busy("Counting samples and detection classes…");
    const [counts] = await this.coordinator.query(
      scatterCountsSql(base), { type: "json", cache: false });
    if (this._destroyed) return this.state;
    lap("counts");
    const xType = axisType(counts.x_min);
    const yType = axisType(counts.y_min);
    base.logX = xType === "log";
    base.logY = yType === "log";
    const leaveOut = excludedSql(base, base.logX, base.logY);
    const excluded = { count: 0, samples: [], logX: base.logX, logY: base.logY };
    if (leaveOut && ((base.logX && Number(counts.x_nonpositive))
                     || (base.logY && Number(counts.y_nonpositive)))) {
      excluded.samples = await this.coordinator.query(
        leaveOut, { type: "json", cache: false });
      // Samples with x OR y at 0 or below, counting each sample once.
      excluded.count = base.logX && base.logY ? Number(counts.nonpositive)
        : Number(base.logX ? counts.x_nonpositive : counts.y_nonpositive);
    }

    await this.coordinator.exec(
      `CREATE OR REPLACE TEMP VIEW ${all} AS ${scatterSql(base)}`);
    await this.coordinator.exec(
      `CREATE OR REPLACE TEMP VIEW ${set} AS ${scatterSql({ ...base, solid })}`);
    await this.coordinator.exec(
      `CREATE OR REPLACE TEMP VIEW ${shown} AS ${scatterSql({
        ...base, solid,
        locations: Array.isArray(options.locations) ? options.locations : null,
      })}`);
    // One view per class: a vgplot mark has no row filter of its own, and
    // a single mark with per-class symbol AND per-class hollow fill would
    // need an identity colour scale. Four marks over four views say the
    // same thing plainly.
    const byClass = {};
    for (const cls of CLASSES) {
      byClass[cls.key] = `sv_${serial}_${cls.key}`;
      await this.coordinator.exec(
        `CREATE OR REPLACE TEMP VIEW ${byClass[cls.key]} AS
         SELECT * FROM ${shown} WHERE class = '${cls.key}'`);
    }
    if (this._destroyed) return this.state;
    lap("views");
    // The legend and caption count what is DRAWN, so the classes come from
    // the view, after any values a log axis cannot place were left out.
    const perClass = await this.coordinator.query(
      `SELECT class, count(*) AS n FROM ${set} GROUP BY class`,
      { type: "json", cache: false });
    // Out of how many, when a mode or window draws fewer than every sample.
    const allSamples = solid ? Number((await this.coordinator.query(
      `SELECT count(*) AS n FROM ${all}`, { type: "json", cache: false }))[0].n)
      : null;
    if (this._destroyed) return this.state;
    const drawnCounts = { ...counts, both: 0, x_only: 0, y_only: 0, neither: 0 };
    for (const row of perClass) drawnCounts[row.class] = Number(row.n);

    this.element.replaceChildren();
    const drawn = ["both", "x_only", "y_only", "neither"]
      .reduce((sum, key) => sum + Number(drawnCounts[key] || 0), 0);
    this.state = { total: drawn, counts: drawnCounts, excluded,
                   axes: { x: xType, y: yType } };
    // An empty WINDOW still draws: every sample faint, nothing solid - a
    // time animation passing through an empty year must not blank the
    // plot. Only no samples at all is a note.
    if (!drawn && (!solid || !allSamples)) {
      this.element.append(this._note(
        `No sample carries both ${options.x.chemical} and `
        + `${options.y.chemical} with a known detect flag`
        + `${options.matrix ? " in " + options.matrix : ""}`
        + `${((options.points || {}).window) ? " in this time window" : ""}.`));
      return this.state;
    }

    const vg = this.vg;
    // SQUARE. A scatter's shape is part of what it says: stretched wide, a
    // relationship looks flatter than it is (asked for 2026-10-09). The
    // side is the smaller of what the window offers, less the margins, so
    // the plotting area stays square whatever the dock's proportions.
    const side = Math.max(320, Math.min(
      (this.element.clientWidth || 600) - 24,
      (this.element.clientHeight || 600) - 150));
    const width = options.width || side + 56;      // + marginLeft
    const height = options.height || side + 40;    // + marginBottom

    const marks = [
      // Context: every paired sample, faint, never filtered. The brush is
      // attached to THIS mark, so its fields are the scatter's x and y.
      vg.dot(vg.from(all), { x: "x", y: "y", r: 2, fill: "#b8b8b8",
                             fillOpacity: 0.35, stroke: "none" }),
      vg.intervalXY({ as: this.selection }),
    ];
    for (const cls of CLASSES) {
      // BOTH DETECTED takes its bivariate colour per point, from the
      // `colour` column (the host's breaks and palette). The three classes
      // involving a non-detect are one grey, told apart by symbol
      // (decided 2026-10-09).
      const coloured = cls.key === "both" && options.bivariate;
      marks.push(vg.dot(
        vg.from(byClass[cls.key], { filterBy: this.selection }),
        {
          x: "x", y: "y", r: cls.filled ? 2.8 : 3.2,
          symbol: cls.symbol,
          stroke: coloured ? "#4d4d4d" : (cls.key === "both" ? cls.color : grey),
          fill: coloured ? "colour" : (cls.filled ? cls.color : "none"),
          strokeWidth: coloured ? 0.4 : 1,
        },
      ));
    }
    this._busy(`Drawing ${drawn.toLocaleString("en-US")} samples…`);
    const chart = vg.plot(
      ...marks,
      // The `colour` column holds hex values, so the colour scale must pass
      // them through untouched rather than map them to a scheme.
      vg.colorScale("identity"),
      vg.xScale(xType), vg.yScale(yType),
      // Plain numbers on a log axis. The default wrote SI prefixes - 0.2
      // mg/kg as "200m", 0.3 ug/L as "300µ" - which on a concentration
      // axis read as milli- and micro- of the unit (seen 2026-10-10).
      ...(xType === "log" ? [vg.xTickFormat("~g")] : []),
      ...(yType === "log" ? [vg.yTickFormat("~g")] : []),
      vg.xLabel(axisLabel(options.x)), vg.yLabel(axisLabel(options.y)),
      vg.marginLeft(56), vg.marginBottom(40),
      vg.width(width), vg.height(height),
    );
    this.element.replaceChildren(chart);
    this.element.append(this._legend(options));
    for (const line of scatterCaption({
      x: options.x, y: options.y, matrix: options.matrix, counts: drawnCounts,
      resolved: options.resolved || 0, excluded,
      pivotExcluded: options.excluded || null,
      points: options.points || null, allSamples,
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
    this.selection.addEventListener("value", () => this._brushed());
    lap("plot built");
    // vgplot fills the plot asynchronously, once its mark queries settle,
    // so "drawn" is when an SVG mark first appears, polled briefly.
    this._whenDrawn(chart).then((ms) => {
      timings.drawn = ms === null ? null : Math.round(ms - started);
      this.state.timings = timings;
      if (this.onTimings) this.onTimings(timings);
    });
    return this.state;
  }

  _whenDrawn(chart, limitMs = 15000) {
    return new Promise((resolve) => {
      const begun = performance.now();
      const look = () => {
        if (this._destroyed) return resolve(null);
        if (chart.querySelector && chart.querySelector("svg g[aria-label='dot'] *")) {
          return resolve(performance.now());
        }
        if (performance.now() - begun > limitMs) return resolve(null);
        setTimeout(look, 50);
      };
      look();
    });
  }

  /** The 3x3 key beside a one-line note. The four classes, with their
   *  marks and counts, are the caption's first line (scatterCaption). */
  _legend(options) {
    const node = document.createElement("div");
    node.className = "legend";
    if (options.bivariate) node.append(...this._bivariateKey(options));
    return node;
  }

  /** biscale's 3x3 key: x across, y up, with each axis's break values. */
  _bivariateKey(options) {
    const bi = options.bivariate;
    const wrap = document.createElement("div");
    wrap.className = "bikey";
    wrap.style.cssText = "display:inline-grid;grid-template-columns:auto repeat(3, 14px);"
      + "gap:1px;align-items:center;margin:2px 0;font-size:10px;flex:none";
    const xl = breakLabels((bi.x || {}).breaks);
    const yl = breakLabels((bi.y || {}).breaks);
    for (const yc of [3, 2, 1]) {
      const label = document.createElement("span");
      label.textContent = `${yl[yc - 1]} `;
      label.style.textAlign = "right";
      wrap.append(label);
      for (const xc of [1, 2, 3]) {
        const cell = document.createElement("span");
        cell.style.cssText = `width:14px;height:14px;background:${bi.palette[`${xc}-${yc}`]}`;
        cell.title = `${options.x.chemical} ${xl[xc - 1]}, ${options.y.chemical} ${yl[yc - 1]}`;
        wrap.append(cell);
      }
    }
    const note = document.createElement("div");
    note.className = "bikey-note";
    const scale = (bi.x || {}).scale === "log10" && (bi.y || {}).scale === "log10"
      ? " (log10)" : "";
    note.textContent = `↑ ${options.y.chemical} → ${options.x.chemical}: `
      + `Jenks classes${scale} of samples detected on both. Grey: a non-detect. `
      + `Map: each station's ${(options.points || {}).map || "most recent sample"}.`;
    // Side by side, so the key costs its own height and no more.
    return [wrap, note];
  }

  _note(text) {
    const node = document.createElement("div");
    node.className = "note";
    node.textContent = text;
    return node;
  }

  _box() {
    const value = this.selection.value;
    if (!value || !value.length || !Array.isArray(value[0])) return null;
    const [[x0, x1], [y0, y1]] = value;
    const box = { x0: Math.min(x0, x1), x1: Math.max(x0, x1),
                  y0: Math.min(y0, y1), y1: Math.max(y0, y1) };
    return Object.values(box).every(Number.isFinite) ? box : null;
  }

  _brushed() {
    if (this._timer) clearTimeout(this._timer);
    this._timer = setTimeout(() => this._emit(this._box()), BRUSH_SETTLE_MS);
  }

  async _emit(box) {
    if (!this.onBrush || this._destroyed) return;
    const key = box ? `${box.x0}:${box.x1}:${box.y0}:${box.y1}` : "";
    if (key === this._lastSent) return;
    this._lastSent = key;
    if (!box) {
      if (this._brushNode) {
        this._brushNode.textContent = scatterBrushLabel({ total: this.state.total });
      }
      this.onBrush(null);
      return;
    }
    // The samples inside the rectangle AND the map's selection, and the
    // locations they belong to - what the map highlights. Asked of the
    // database: the view already holds both filters.
    const rows = await this.coordinator.query(
      brushSql(this._view, box), { type: "json", cache: false });
    if (this._destroyed) return;
    const hit = rows[0] || { samples: 0, locations: 0, codes: [] };
    const label = scatterBrushLabel({ samples: hit.samples,
                                      locations: hit.locations,
                                      total: this.state.total });
    if (this._brushNode) this._brushNode.textContent = label;
    this.onBrush({ locations: Array.from(hit.codes || []), label });
  }
}
