/**
 * US-1: the histogram itself, and the brush that drives the map.
 *
 * The decisions this chart embodies live in histogram.js and are tested
 * without a browser; this file is the vgplot wiring around them. Kept
 * separate for that reason - a chart is awkward to test and the choices
 * inside it are not.
 *
 * NON-DETECTS ARE A SEPARATE FILL, not a merged bar (US-5). vgplot stacks
 * rectY by the fill channel, so `fill: 'detected'` gives each bin a
 * detected and a non-detect segment without any manual stacking. That is
 * the whole reason the query carries `detected` rather than filtering it
 * away.
 *
 * THE BRUSH IS DEBOUNCED ON THE WAY OUT, not on the way in. vgplot updates
 * its own selection continuously and cheaply; what costs is rebuilding a
 * QGIS memory layer, measured at 15 ms for 5,000 features over 250,000
 * points. Sending every intermediate range during a drag would queue those
 * behind each other, so the map follows the brush on a short timer and the
 * chart never waits for it.
 */
import { binCount, brushLabel, caption, histogramSql, POPULATION }
  from "./histogram.js";

// EVERY DIRECTIVE COMES FROM THE API CONTEXT, not from the module.
// `plot()` calls `connect(this, ...p.marks)` with `this` as the context, so
// a plot built from the bare import connects its marks to nothing: the
// element is created, stays empty, and reports no error. That is exactly how
// it presented - the query ran, the caption was right, and svg=NO.

// How long the map waits after the brush stops moving. Short enough to feel
// immediate, long enough that a drag does not queue a layer rebuild per
// mouse move.
const BRUSH_SETTLE_MS = 80;

const TABLE = "h";

export class Histogram {
  /**
   * @param {object} options
   *  - vg          the API context from createAPIContext
   *  - coordinator the Mosaic coordinator
   *  - element     where to put the chart
   *  - onBrush     ({from, to, label}) or (null) when cleared
   */
  constructor({ vg, coordinator, element, onBrush = null }) {
    this.vg = vg;
    this.coordinator = coordinator;
    this.element = element;
    this.onBrush = onBrush;
    // From the context as well: a Selection made by the bare import belongs
    // to no coordinator, so the marks filter against something nothing is
    // listening to.
    this.selection = vg.Selection.intersect();
    this.state = null;
    this._timer = null;
    this._lastSent = undefined;
    this._destroyed = false;
  }

  /** Stop this chart acting on the page. Called before its replacement
   *  draws.
   *
   *  app.js builds a new Histogram per draw, and nothing used to retire the
   *  old one: its ResizeObserver stayed attached to the shared element, so
   *  the next resize made EVERY chart ever drawn call show() with its own
   *  analyte, each one replacing the temp view `h` underneath the live
   *  chart. The last to finish won, whichever analyte that was. */
  destroy() {
    this._destroyed = true;
    // Disconnect its marks and selection too. A retired plot left connected
    // still answers coordinator updates, re-querying `h` - which by then
    // holds the next analyte - and its in-flight requests are cancelled
    // rather than landing late.
    this.coordinator.clear({ clients: true, cache: true });
    if (this._observer) {
      this._observer.disconnect();
      this._observer = null;
    }
    if (this._timer) {
      clearTimeout(this._timer);
      this._timer = null;
    }
  }

  /** Build (or rebuild) the chart for one analyte and set of filters. */
  /** The width a chart drawn now would get. ONE formula, used by both the
   *  draw and the resize comparison - two copies drift and the observer
   *  then disagrees with what is on screen about whether anything moved. */
  _fitWidth() {
    return Math.max(320, (this.element.clientWidth || 560) - 24);
  }

  _fitHeight() {
    return Math.max(160, (this.element.clientHeight || 260) - 90);
  }

  async show(options) {
    // What this chart currently shows. The resize observer redraws from
    // THIS rather than from its own closed-over copy, so a resize after an
    // analyte change does not resurrect the previous analyte's filters.
    this._options = options;
    // The observer below must ignore everything that happens while this
    // runs: replaceChildren() empties the element, which measures as a
    // resize, which is the loop. Cleared in a finally-equivalent at the
    // two exits rather than a try/finally, because the early return for an
    // empty result is a legitimate exit too.
    this._drawing = true;
    if (this._destroyed) return this.state;
    const sql = histogramSql(options);
    // A named temp view rather than a subquery: vgplot re-queries it for
    // every brush change, and re-parsing the whole statement each time is
    // work the database does not need to repeat.
    await this.coordinator.exec(
      `CREATE OR REPLACE TEMP VIEW ${TABLE} AS ${sql}`);
    // EVERY CACHED ANSWER ABOUT `h` IS NOW WRONG. Mosaic caches by SQL
    // string, and replacing the view changes the rows without changing a
    // single query's text - the stats read below AND the plot's own bin
    // query. Opting the stats read out (cache: false, 2026-09-29) fixed
    // the caption and left the BARS cached: reported 2026-10-01, Chloride
    // then Cadmium drew Chloride's bars under Cadmium's caption. Cleared
    // here, where the view is replaced, so no query has to remember to opt
    // out.
    this.coordinator.manager.cache().clear();
    // Retired while the view was being built: a newer chart owns `h` and
    // the element now, and drawing here would put these rows under it.
    if (this._destroyed) return this.state;

    // cache: false, AND IT IS NOT AN OPTIMISATION TO REMOVE. Mosaic's
    // QueryManager caches by the SQL STRING, and this string is identical
    // for every analyte - only the view `h` underneath it changes. Cached,
    // the second analyte charted gets the FIRST one's rows back: Barium,
    // Sulfate and Thallium all drew the same bars and the same "42
    // non-detect (42.0%)", with only the caption changing. Reported from
    // the panel 2026-09-29.
    //
    // The cache key cannot know the view was replaced, so the only honest
    // fix is to opt this read out. It is one query per redraw over an
    // already-aggregated view, so there is nothing here worth caching.
    const rows = await this.coordinator.query(
      `SELECT value, detected, unit, fraction FROM ${TABLE}`,
      { type: "json", cache: false });
    if (this._destroyed) return this.state;
    const values = rows.map((row) => Number(row.value))
      .filter((v) => Number.isFinite(v));
    const nonDetects = rows.filter((row) => !row.detected).length;
    // EVERY unit present, not the first one seen. Taking the first and
    // labelling the whole axis with it is how a chart bins mg/kg beside
    // mg/L and says "mg/kg" - a factor of 1000, invisible. Iron in SE on
    // Facility A is exactly that: 23,456 rows of mg/kg and 116 of mg/L.
    const unitCounts = new Map();
    for (const row of rows) {
      const clean = (row.unit || "").trim();
      if (!clean) continue;
      unitCounts.set(clean, (unitCounts.get(clean) || 0) + 1);
    }
    const units = [...unitCounts.entries()].sort((a, b) => b[1] - a[1]);
    const unit = units.length ? units[0][0] : "";

    // Cleared before ANY branch appends. The refusal below and the
    // no-results note both write into this element, and clearing after
    // them would wipe what they had just written.
    this.element.replaceChildren();

    // DISSOLVED AND TOTAL ARE NOT ONE POPULATION (US-16). They come from
    // the same water on the same day and differ by up to ~29x - on Facility A,
    // Arsenic in WS runs to 1,300 as total and 16.3 as dissolved, so
    // binning them together stretches the axis 80x and double-counts every
    // sample. The map already refuses this; the chart must not be laxer.
    //
    // Only D and T are refused. Fractions like N are neither half of that
    // split, and refusing every combination would block 281 selections
    // where nothing is actually being crossed.
    const fractions = new Map();
    for (const row of rows) {
      const clean = (row.fraction || "").trim().toUpperCase();
      if (clean) fractions.set(clean, (fractions.get(clean) || 0) + 1);
    }
    if (fractions.has("D") && fractions.has("T")) {
      this.element.append(this._note(
        `${options.chemical || "This selection"} here is both dissolved `
        + `(${fractions.get("D").toLocaleString()}) and total `
        + `(${fractions.get("T").toLocaleString()}). They are drawn from `
        + "the same water and can differ by more than 20x, so one axis "
        + "would double-count every sample. Choose a fraction."));
      this.state = { total: rows.length, nonDetects, unit: "",
                     fractions: [...fractions.entries()], mixedFractions: true };
      this._drawing = false;
      return this.state;
    }

    if (units.length > 1) {
      // REFUSED, the way the map's _confirm_units refuses. The plugin
      // already blocks a mixed-unit aggregate; the chart is what people
      // read FIRST and screenshot into reports, so it cannot be the
      // laxer of the two. Naming the units and their counts is what makes
      // the refusal actionable - the user narrows a filter and returns.
      this.element.append(this._note(
        `${options.chemical || "This selection"} is reported in `
        + `${units.length} different units here: `
        + units.map(([u, n]) => `${u} (${n.toLocaleString()})`).join(", ")
        + ". Binning them on one axis would compare numbers that are not "
        + "comparable. Narrow the matrix, fraction or method until one "
        + "unit remains."));
      this.state = { total: rows.length, nonDetects, unit: "", units,
                     mixedUnits: true };
      this._drawing = false;
      return this.state;
    }

    this.state = {
      total: rows.length,
      nonDetects,
      unit,
      locations: options.aggregate && options.aggregate !== "unaggregated"
        ? rows.length : undefined,
      bins: options.bins || binCount(values),
    };

    if (!rows.length) {
      // An empty chart with axes looks like data that happens to be zero.
      // Say which filter emptied it instead.
      this.element.append(this._note(
        `No results for ${options.chemical || "this analyte"}`
        + `${options.matrix ? " in " + options.matrix : ""}`
        + `${options.fraction ? " (" + options.fraction + ")" : ""}.`));
      this._drawing = false;
      return this.state;
    }

    const vg = this.vg;
    // Measured ONCE and remembered, so the resize observer below can tell
    // a real resize from the element flexing under its own redraw.
    const drawWidth = options.width || this._fitWidth();
    const drawHeight = options.height || this._fitHeight();
    const chart = vg.plot(
      vg.rectY(
        vg.from(TABLE),
        {
          x: vg.bin("value", { maxbins: this.state.bins }),
          y: vg.count(),
          // Detected and non-detect stack per bin. Never merged: a
          // non-detect at half its reporting limit is an artefact of
          // laboratory sensitivity, not a measurement (US-5).
          fill: "detected",
          inset: 0.5,
        },
      ),
      vg.intervalX({ as: this.selection }),
      vg.xScale(options.logAxis ? "log" : "linear"),
      // THE UNIT IS ON THE AXIS, ALWAYS (US-15). A chart gets screenshotted
      // into a report, where the caption below it does not travel. An axis
      // reading "value" with no unit is a number nobody can check, so the
      // absence is stated rather than left blank.
      vg.xLabel(unit ? `value (${unit})` : "value (unit not reported)"),
      vg.yLabel("results"),
      vg.marginLeft(48),
      // FILL THE WINDOW. vgplot takes fixed pixels, so the size is
      // measured from the element rather than guessed: a 560x200 chart in
      // a 1000px dock sits in the corner with most of the space empty,
      // which is what the first live run looked like. The margins here
      // match the padding index.html gives #views plus the caption lines
      // below the plot.
      vg.width(drawWidth),
      vg.height(drawHeight),
    );

    this.element.append(chart);
    this.element.append(this._caption(options));
    this.element.append(this._brushLine());
    this.selection.addEventListener("value", () => this._brushed());

    // REDRAW ON RESIZE, BUT ONLY ON A REAL ONE. The size above is a
    // measurement, so it is stale the moment the dock is dragged.
    //
    // A NAIVE OBSERVER HERE IS AN INFINITE LOOP, and it is not subtle:
    // show() calls replaceChildren(), which empties the element, which
    // changes its size, which fires the observer, which calls show(). The
    // debounce only sets the frequency - measured at 2-3 redraws per
    // second, with the caption flashing, which is unusable rather than
    // merely wasteful.
    //
    // So the drawn size is remembered and compared. A redraw's own
    // teardown lands on the same width it will be redrawn at, so it fails
    // the comparison and stops there. The tolerance absorbs scrollbar
    // appearance and sub-pixel layout, which otherwise oscillate.
    this._drawnWidth = drawWidth;
    this._drawnHeight = drawHeight;
    if (typeof ResizeObserver === "function" && !this._observer) {
      let pending = null;
      this._observer = new ResizeObserver(() => {
        if (pending) clearTimeout(pending);
        pending = setTimeout(() => {
          if (this._drawing) return;
          const moved = Math.abs(this._fitWidth() - this._drawnWidth) > 8
                     || Math.abs(this._fitHeight() - this._drawnHeight) > 8;
          if (!moved) return;
          // `this._options` rather than the closed-over `options`: the
          // chart may have been redrawn for a different analyte since
          // this observer was installed, and resizing must not resurrect
          // the filters it was first created with.
          const again = Object.assign({}, this._options);
          delete again.width;
          delete again.height;
          this.show(again).catch(() => {});
        }, 200);
      });
      this._observer.observe(this.element);
    }
    // Released only now, after the element has settled at its drawn size,
    // so the resize this very draw caused is never acted on.
    this._drawing = false;
    return this.state;
  }

  _note(text) {
    const node = document.createElement("div");
    node.className = "note";
    node.textContent = text;
    return node;
  }

  _caption(options) {
    const node = document.createElement("div");
    node.className = "caption";
    node.textContent = caption({
      chemical: options.chemical,
      matrix: options.matrix,
      fraction: options.fraction,
      rows: this.state.total,
      nonDetects: this.state.nonDetects,
      unit: this.state.unit,
      population: POPULATION[options.aggregate || "unaggregated"],
    });
    this._captionNode = node;
    return node;
  }

  _brushLine() {
    const node = document.createElement("div");
    node.className = "brush";
    node.textContent = brushLabel({ from: null, to: null,
                                    total: this.state.total });
    this._brushNode = node;
    return node;
  }

  /** The brush moved. Update the words now, the map on a short timer. */
  _brushed() {
    const range = this._range();
    // The words update immediately: the brush is the user's own action and
    // should not appear to lag while the map catches up.
    if (this._brushNode) {
      this._brushNode.textContent = this._label(range);
    }
    if (this._timer) clearTimeout(this._timer);
    this._timer = setTimeout(() => this._emit(range), BRUSH_SETTLE_MS);
  }

  _range() {
    const value = this.selection.value;
    if (!value || !value.length) return null;
    const pair = Array.isArray(value[0]) ? value[0] : value;
    const low = Number(pair[0]);
    const high = Number(pair[1]);
    return Number.isFinite(low) && Number.isFinite(high)
      ? { from: Math.min(low, high), to: Math.max(low, high) }
      : null;
  }

  _label(range) {
    if (!range) {
      return brushLabel({ from: null, to: null, total: this.state.total });
    }
    return brushLabel({
      from: range.from, to: range.to,
      matching: this._matching === undefined ? 0 : this._matching,
      total: this.state.total,
      unit: this.state.unit,
      locations: this.state.locations,
    });
  }

  async _emit(range) {
    if (!this.onBrush) return;
    const key = range ? `${range.from}:${range.to}` : "";
    if (key === this._lastSent) return;      // the drag ended where it was
    this._lastSent = key;
    if (!range) {
      this.onBrush(null);
      return;
    }
    // Which locations the brush covers. Asked of the database rather than
    // computed in the page: it is a GROUP BY over data already loaded, and
    // the answer is what the map needs anyway.
    // cache: false for the same reason as the read above - this SQL varies
    // with the range but still reads `h`, so brushing two analytes to the
    // same range would return the first one's locations and highlight the
    // wrong points on the map.
    const matched = await this.coordinator.query(
      `SELECT DISTINCT location FROM ${TABLE}
       WHERE value BETWEEN ${range.from} AND ${range.to}`,
      { type: "json", cache: false });
    const locations = matched.map((row) => row.location);
    this._matching = locations.length;
    if (this._brushNode) this._brushNode.textContent = this._label(range);
    this.onBrush({ ...range, locations, label: this._label(range) });
  }
}
