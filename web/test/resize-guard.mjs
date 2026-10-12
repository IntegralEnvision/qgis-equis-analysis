/**
 * The resize observer must not redraw the chart it is watching.
 *
 * show() calls replaceChildren(), which empties the element, which changes
 * its size, which fires the observer, which calls show(). Measured live on
 * 2026-09-28: 2-3 redraws per second with the caption flashing between
 * "results" and the full description - unusable, not merely wasteful.
 *
 * Two guards, and this asserts both: `_drawing` makes the observer stand
 * down while a draw is in progress, and a size comparison against the size
 * actually drawn ignores jitter. The comparison uses ONE formula shared
 * with the draw (_fitWidth/_fitHeight), because two copies drift and the
 * observer then disagrees with the screen about whether anything moved.
 *
 *     node web/test/resize-guard.mjs
 */
let failures = 0;
const check = (label, ok, detail) => {
  console.log(`[${ok ? "PASS" : "FAIL"}] ${label}${detail ? " - " + detail : ""}`);
  if (!ok) failures++;
};

class FakeElement {
  constructor() { this.clientWidth = 924; this.clientHeight = 500; }
}

/** The two guards, as chart.js implements them. */
class Harness {
  constructor() {
    this.element = new FakeElement();
    this._drawing = false;
    this._drawnWidth = 0;
    this._drawnHeight = 0;
    this.draws = 0;
  }
  _fitWidth() { return Math.max(320, (this.element.clientWidth || 560) - 24); }
  _fitHeight() { return Math.max(160, (this.element.clientHeight || 260) - 90); }
  show() {
    this._drawing = true;
    this.draws++;
    // replaceChildren(): the element is empty and measures as nothing.
    this.element.clientWidth = 0;
    this.element.clientHeight = 0;
    this.onResize();                    // this is the loop's trigger
    this.element.clientWidth = 924;     // the chart is appended
    this.element.clientHeight = 500;
    this._drawnWidth = this._fitWidth();
    this._drawnHeight = this._fitHeight();
    this._drawing = false;
  }
  onResize() {
    if (this._drawing) return;
    const moved = Math.abs(this._fitWidth() - this._drawnWidth) > 8
               || Math.abs(this._fitHeight() - this._drawnHeight) > 8;
    if (!moved) return;
    this.show();
  }
}

const h = new Harness();
h.show();
check("one draw stays one draw", h.draws === 1, `${h.draws} draws`);

h.onResize();
check("a resize to the same size does nothing", h.draws === 1, `${h.draws} draws`);

h.element.clientWidth = 922;            // sub-pixel / scrollbar jitter
h.onResize();
check("jitter within tolerance does nothing", h.draws === 1, `${h.draws} draws`);

h.element.clientWidth = 600;            // the dock was dragged
h.onResize();
check("a genuine resize still redraws", h.draws === 2, `${h.draws} draws`);

console.log();
if (failures) { console.log(`FAILED: ${failures}`); process.exit(1); }
console.log("The chart redraws when the window moves, and never because of itself.");
