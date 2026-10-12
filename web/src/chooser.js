/**
 * The Analysis tab's chooser: a matrix, an X analyte and a Y analyte.
 *
 * THE PAGE HOLDS THE SELECTION AND NOTHING ELSE. What a choice means -
 * which samples, which cell of each, which breaks - is decided by the host
 * (equis_loader/analysis.py), which pivots every analyte in the matrix once
 * and answers each choice from that pivot. So this file has no SQL and no
 * rule about the data in it: it lists what it is offered, says what was
 * chosen, and labels.
 *
 *   host -> page  offer({matrices, matrix, variables, skipped, x, y, ...})
 *                 paired({x, counts})   samples each column shares with X
 *                 note(text)            what to do next, or why not
 *                 working({message, where})
 *   page -> host  bridge.chosen(JSON {matrix, x, y})
 *
 * Y IS LISTED BY WHAT IT SHARES WITH X. Two analytes measured on different
 * samples make an empty scatter, and nothing in a plain list of 628 names
 * says which pairs those are. Once X is chosen, each Y says how many
 * samples it would put on the chart, and the ones with none are set apart
 * and cannot be chosen.
 *
 * The helpers are exported pure, for web/test/chooser.test.mjs; the class
 * below them is the DOM around them.
 */

const count = (n) => Number(n || 0).toLocaleString("en-US");

/** "Arsenic, total (mg/kg)" - the analyte as a person names it. */
export function variableName(v) {
  const fraction = v.fractionName ? `, ${v.fractionName}` : "";
  const unit = v.unit ? ` (${v.unit})` : "";
  return `${v.chemical}${fraction}${unit}`;
}

/** An X option: the analyte and how much of it there is. */
export function xLabel(v) {
  return `${variableName(v)} · ${count(v.samples)} samples, `
    + `${count(v.detected)} detected`;
}

/** A Y option: the analyte and how many samples it shares with X. */
export function yLabel(v, pairs, xName) {
  if (pairs === undefined || pairs === null) return xLabel(v);
  if (!pairs) return `${variableName(v)} · never with ${xName}`;
  return `${variableName(v)} · ${count(pairs)} with ${xName}`;
}

/** "SO · 112 samples" */
export function matrixLabel(m) {
  return `${m.code} · ${count(m.samples)} samples`;
}

/** Whether `v` matches a filter text: every word, in the name or the CAS. */
export function matches(v, text) {
  const words = String(text || "").toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const hay = `${variableName(v)} ${v.prefix}`.toLowerCase();
  return words.every((w) => hay.includes(w));
}

/**
 * Y's list once X is chosen: those sharing samples with X, then those that
 * never do (shown, but not choosable - an analyte the user is looking for
 * should not seem to be missing). X itself is left out. Catalogue order
 * (alphabetical) within each, so a name is where it is expected.
 */
export function yGroups(variables, x, counts) {
  const shared = [];
  const never = [];
  for (const v of variables) {
    if (v.prefix === x) continue;
    const n = counts ? counts[v.prefix] : undefined;
    if (counts && !n) never.push(v); else shared.push(v);
  }
  return { shared, never };
}

/**
 * The line under the choosers: what the matrix holds. One short line - it
 * shares the space above the chart (asked 2026-10-12: "be more economical
 * with space"); the scatter's own caption says the rest.
 */
export function summary(offer) {
  if (!offer || !offer.matrix) return "Choose a matrix to list its analytes.";
  const parts = [`${count((offer.variables || []).length)} analytes`,
                 `${count(offer.samples)} samples`];
  const reps = (offer.excluded || {}).field_replicates;
  if (reps) parts.push(`${count(reps)} replicates not plotted`);
  return parts.join(" · ");
}

/**
 * The toggle naming what was left out, or "" when nothing was. Mixed units
 * are the usual reason, and named as such; anything else is "left out".
 */
export function warningsLabel(offer) {
  const skipped = (offer || {}).skipped || [];
  if (!skipped.length) return "";
  const units = skipped.filter((s) => s.kind === "units").length;
  return units === skipped.length
    ? `Mixed-unit warnings (${units})`
    : `${skipped.length} analytes left out`;
}

/**
 * What the warnings list shows: the advice ONCE, then one line per analyte
 * with its unit counts ("Aluminum, total: percent 324 · mg/kg 32") - not
 * the host's whole sentence sixteen times. Anything not about units keeps
 * the host's own words.
 */
export function warningLines(offer) {
  const skipped = (offer || {}).skipped || [];
  const lines = [];
  for (const s of skipped) {
    if (s.kind === "units" && Array.isArray(s.units)) {
      const name = s.fraction ? `${s.chemical}, ${FRACTIONS[s.fraction] || s.fraction}`
        : s.chemical;
      lines.push(`${name}: ${s.units.map(([u, n]) => `${u} ${count(n)}`).join(" · ")}`);
    } else {
      lines.push(s.reason);
    }
  }
  return lines;
}

/** The Points choices, named with the chosen analytes. */
export function pointsOptions(xName, yName, zName = null) {
  const out = [
    ["all", "Every sample"],
    ["recent", "Most recent per location"],
    ["max_x", `Highest ${xName} per location`],
    ["max_y", `Highest ${yName} per location`],
  ];
  // A ternary's third analyte can rank a location too.
  if (zName) out.push(["max_z", `Highest ${zName} per location`]);
  return out;
}

export const UNITS_ADVICE = "Reported in more than one unit, so not offered: "
  + "a column holds one unit. Narrow the matrix or fraction, or convert upstream.";

const FRACTIONS = { T: "total", D: "dissolved" };

// ----------------------------------------------------------------- the DOM

function option(value, text, { disabled = false, selected = false } = {}) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = text;
  node.disabled = disabled;
  node.selected = selected;
  return node;
}

/** A label in the grid's first column, beside its control. */
function label(text, control) {
  const node = document.createElement("label");
  node.className = "chooser-label";
  node.textContent = text;
  // Clicking the label focuses the control, as a wrapping label would.
  node.addEventListener("click", () => control.focus());
  return node;
}

export class Chooser {
  /**
   * @param {object} options
   *  - element   where the chooser lives (it stays when the chart changes)
   *  - onChoose  ({matrix, x, y}) when the user changes anything
   */
  constructor({ element, onChoose }) {
    this.element = element;
    this.onChoose = onChoose;
    this.offer = null;
    this.counts = null;
    this.countsFor = null;
    this.x = null;
    this.y = null;
    this.z = null;
    this.chart = "scatter";
    this.countsZ = null;
    this.countsZFor = null;
    this.mode = "all";
    this.filter = "";
    this._build();
  }

  _build() {
    this.matrix = document.createElement("select");
    this.xSelect = document.createElement("select");
    this.ySelect = document.createElement("select");
    this.search = document.createElement("input");
    this.search.type = "search";
    this.search.placeholder = "Filter analytes";
    // What each point is (decided 2026-10-12): every sample, or ONE REAL
    // SAMPLE per location by a stated rank - never X's maximum beside Y's
    // from another sample. The host does the picking.
    this.pointsSelect = document.createElement("select");
    // Scatter, or a ternary of three (decided 2026-10-12: shares of the
    // sum, one unit, non-detects grey at their limit; not a Piper).
    this.chartSelect = document.createElement("select");
    for (const [value, text] of [["scatter", "Scatter"], ["ternary", "Ternary"]]) {
      this.chartSelect.append(option(value, text));
    }
    this.zSelect = document.createElement("select");
    this.zSelect.classList.add("axis-select");
    this.swap = document.createElement("button");
    this.swap.type = "button";
    this.swap.className = "swap";
    this.swap.textContent = "⇅";
    this.swap.title = "Swap X and Y";

    // ONE GRID, label beside control (asked 2026-10-12): labels in the
    // first column so the boxes line up, three rows instead of six.
    //   Matrix [matrix........] [filter....]
    //   X      [x.......................]  ⇅
    //   Y      [y.......................]
    const grid = document.createElement("div");
    grid.className = "chooser";
    const top = document.createElement("div");
    top.className = "chooser-pair";
    top.append(this.matrix, this.search);
    this.xSelect.classList.add("axis-select");
    this.ySelect.classList.add("axis-select");
    this.zLabel = label("Z", this.zSelect);
    const points = document.createElement("div");
    points.className = "chooser-pair";
    points.append(this.pointsSelect, this.chartSelect);
    grid.append(label("Matrix", this.matrix), top,
                label("X", this.xSelect), this.xSelect, this.swap,
                label("Y", this.ySelect), this.ySelect,
                this.zLabel, this.zSelect,
                label("Points", this.pointsSelect), points);
    this._showZ();
    this.line = document.createElement("div");
    this.line.className = "chooser-line";
    this.status = document.createElement("span");
    this.status.className = "chooser-status";
    // The warnings open OVER the chart, never above it: a list that pushes
    // the chart down was the complaint (2026-10-12).
    this.warnToggle = document.createElement("button");
    this.warnToggle.type = "button";
    this.warnToggle.className = "chooser-warn";
    this.warnToggle.hidden = true;
    this.warnPanel = document.createElement("div");
    this.warnPanel.className = "chooser-warn-panel";
    this.warnPanel.hidden = true;
    this.line.append(this.status, this.warnToggle, this.warnPanel);
    this.element.replaceChildren(grid, this.line);
    this.warnToggle.addEventListener("click", (event) => {
      event.stopPropagation();
      this.warnPanel.hidden = !this.warnPanel.hidden;
    });
    // Anywhere else closes it.
    document.addEventListener("click", (event) => {
      if (!this.warnPanel.hidden && !this.warnPanel.contains(event.target)) {
        this.warnPanel.hidden = true;
      }
    });

    this.matrix.addEventListener("change", () => {
      this.counts = null;
      this._choose({ matrix: this.matrix.value });
    });
    this.xSelect.addEventListener("change", () => {
      this.x = this.xSelect.value || null;
      this.counts = null;
      this.countsZ = null;
      this._renderY();
      this._choose();
    });
    this.ySelect.addEventListener("change", () => {
      this.y = this.ySelect.value || null;
      this.countsZ = null;
      this._renderZ();
      this._renderPoints();
      this._choose();
    });
    this.zSelect.addEventListener("change", () => {
      this.z = this.zSelect.value || null;
      this._renderPoints();
      this._choose();
    });
    this.chartSelect.addEventListener("change", () => {
      this.chart = this.chartSelect.value === "ternary" ? "ternary" : "scatter";
      if (this.chart === "scatter" && this.mode === "max_z") this.mode = "all";
      this._showZ();
      this._renderZ();
      this._renderPoints();
      this._choose();
    });
    this.pointsSelect.addEventListener("change", () => {
      this.mode = this.pointsSelect.value || "all";
      this._choose();
    });
    this.swap.addEventListener("click", () => {
      if (!this.x || !this.y) return;
      [this.x, this.y] = [this.y, this.x];
      this.counts = null;
      this.countsZ = null;
      this._renderX();
      this._renderY();
      this._choose();
    });
    this.search.addEventListener("input", () => {
      this.filter = this.search.value;
      this._renderX();
      this._renderY();
    });
  }

  /** The Z row shows only for a ternary. */
  _showZ() {
    const on = this.chart === "ternary";
    this.zLabel.hidden = !on;
    this.zSelect.hidden = !on;
    this.chartSelect.value = this.chart;
  }

  _choose(override = {}) {
    const choice = { matrix: (this.offer || {}).matrix || null,
                     x: this.x, y: this.y, z: this.z, chart: this.chart,
                     mode: this.mode, ...override };
    if (this.onChoose) this.onChoose(choice);
  }

  /** The host's catalogue for a matrix (or the matrix list alone). */
  setOffer(offer) {
    this.offer = offer;
    this.element.hidden = false;
    this.x = offer.x || null;
    this.y = offer.y || null;
    this.z = offer.z || null;
    if (offer.chart) this.chart = offer.chart;
    this.counts = null;
    this.countsZ = null;
    this._showZ();
    this.matrix.replaceChildren();
    if (!offer.matrix) {
      this.matrix.append(option("", "Choose a matrix…", { selected: true }));
    }
    for (const m of offer.matrices || []) {
      this.matrix.append(option(m.code, matrixLabel(m),
                                { selected: m.code === offer.matrix }));
    }
    this._renderX();
    this._renderY();
    this.status.textContent = summary(offer);
    const toggle = warningsLabel(offer);
    this.warnToggle.hidden = !toggle;
    this.warnToggle.textContent = toggle ? `${toggle} ▾` : "";
    this.warnPanel.hidden = true;
    this.warnPanel.replaceChildren();
    if (toggle) {
      if ((offer.skipped || []).some((s) => s.kind === "units")) {
        const advice = document.createElement("div");
        advice.className = "chooser-warn-advice";
        advice.textContent = UNITS_ADVICE;
        this.warnPanel.append(advice);
      }
      const list = document.createElement("ul");
      for (const text of warningLines(offer)) {
        const item = document.createElement("li");
        item.textContent = text;
        list.append(item);
      }
      this.warnPanel.append(list);
    }
  }

  /** How many samples each column shares with X AND Y: the Z list. */
  setPairedZ({ x, y, counts }) {
    if (x !== this.x || y !== this.y) return;     // an older pair's answer
    this.countsZ = counts;
    this.countsZFor = `${x}|${y}`;
    this._renderZ();
  }

  /** How many samples each column shares with X. */
  setPaired({ x, counts }) {
    if (x !== this.x) return;            // an answer to an older X
    this.counts = counts;
    this.countsFor = x;
    this._renderY();
  }

  /** A scatter the host drew (the console's, say): show it as chosen. */
  showing(xPrefix, yPrefix, mode = null, zPrefix = null, chart = null) {
    this.settle();
    if (mode) this.mode = mode;
    if (chart) { this.chart = chart; this._showZ(); }
    if (zPrefix) this.z = zPrefix;
    const known = new Set(((this.offer || {}).variables || []).map((v) => v.prefix));
    if (!known.has(xPrefix) || !known.has(yPrefix)) return;
    if (xPrefix !== this.x) this.counts = null;
    this.x = xPrefix;
    this.y = yPrefix;
    this._renderX();
    this._renderY();
  }

  setNote(text) {
    this.status.textContent = text ? `${summary(this.offer)} · ${text}` : summary(this.offer);
    this.status.classList.remove("working");
  }

  /** The host answered with a chart, not words: back to the summary. */
  settle() {
    if (this.status.classList.contains("working")) this.setNote("");
  }

  setWorking(message) {
    this.status.textContent = message;
    this.status.classList.add("working");
  }

  _variables() {
    return ((this.offer || {}).variables || []);
  }

  _renderX() {
    const s = this.xSelect;
    s.replaceChildren(option("", this._variables().length ? "Choose X…" : "—",
                             { selected: !this.x }));
    for (const v of this._variables()) {
      // The chosen one stays listed whatever the filter, or the select
      // would show something other than what is drawn.
      if (v.prefix !== this.x && !matches(v, this.filter)) continue;
      s.append(option(v.prefix, xLabel(v), { selected: v.prefix === this.x }));
    }
    s.disabled = !this._variables().length;
  }

  _renderY() {
    const s = this.ySelect;
    const variables = this._variables();
    s.replaceChildren(option("", variables.length ? "Choose Y…" : "—",
                             { selected: !this.y }));
    const xVar = variables.find((v) => v.prefix === this.x);
    const counts = this.countsFor === this.x ? this.counts : null;
    const { shared, never } = yGroups(variables, this.x, counts);
    const keep = (v) => v.prefix === this.y || matches(v, this.filter);
    const xName = xVar ? xVar.chemical : "X";
    for (const v of shared.filter(keep)) {
      s.append(option(v.prefix, yLabel(v, counts ? counts[v.prefix] : null, xName),
                      { selected: v.prefix === this.y }));
    }
    const apart = never.filter(keep);
    if (apart.length) {
      const group = document.createElement("optgroup");
      group.label = `Never measured on a sample with ${xName}`;
      for (const v of apart) {
        group.append(option(v.prefix, yLabel(v, 0, xName),
                            { disabled: v.prefix !== this.y,
                              selected: v.prefix === this.y }));
      }
      s.append(group);
    }
    s.disabled = !variables.length;
    this.swap.disabled = !(this.x && this.y);
    this._renderZ();
    this._renderPoints();
  }

  /** Z, as Y: by the samples it shares with X AND Y; never-shared set apart. */
  _renderZ() {
    const s = this.zSelect;
    const variables = this._variables().filter((v) => v.prefix !== this.y);
    s.replaceChildren(option("", variables.length ? "Choose Z…" : "—",
                             { selected: !this.z }));
    const name = (p, fallback) => ((variables.concat(this._variables())
      .find((v) => v.prefix === p) || {}).chemical || fallback);
    const pairName = `${name(this.x, "X")} & ${name(this.y, "Y")}`;
    const counts = this.countsZFor === `${this.x}|${this.y}` ? this.countsZ : null;
    const { shared, never } = yGroups(variables, this.x, counts);
    const keep = (v) => v.prefix === this.z || matches(v, this.filter);
    for (const v of shared.filter(keep)) {
      s.append(option(v.prefix, yLabel(v, counts ? counts[v.prefix] : null, pairName),
                      { selected: v.prefix === this.z }));
    }
    const apart = never.filter(keep);
    if (apart.length) {
      const group = document.createElement("optgroup");
      group.label = `Never measured on a sample with ${pairName}`;
      for (const v of apart) {
        group.append(option(v.prefix, yLabel(v, 0, pairName),
                            { disabled: v.prefix !== this.z,
                              selected: v.prefix === this.z }));
      }
      s.append(group);
    }
    s.disabled = !(this.x && this.y);
  }

  _renderPoints() {
    const name = (prefix, fallback) => ((this._variables().find(
      (v) => v.prefix === prefix) || {}).chemical || fallback);
    const s = this.pointsSelect;
    s.replaceChildren();
    for (const [value, text] of pointsOptions(
      name(this.x, "X"), name(this.y, "Y"),
      this.chart === "ternary" ? name(this.z, "Z") : null)) {
      s.append(option(value, text, { selected: value === this.mode }));
    }
  }
}
