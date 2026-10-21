/**
 * The overlay's window manager: panels that cling to the map's edges.
 *
 * Asked for 2026-10-20: "Each panel element would be magnetically
 * attracted to any edge of the map window. They could be minimized
 * against the edge or expanded." Over the map (body.overlay) every
 * `.panel` is placed by an EDGE and an OFFSET along it:
 *
 *   * dropped anywhere, a panel snaps to the nearest edge, keeping where
 *     along that edge it was let go;
 *   * panels on one edge never overlap - a later one is pushed along it;
 *   * minimised, a panel is a tab against its edge (vertical on left and
 *     right), and a click expands it where it was.
 *
 * In the panel (no overlay) none of this applies: the panels are a plain
 * column, headers hidden, exactly the tab as it was.
 *
 * Placement is the page's own business - Qt needs only the resulting
 * rectangles, which app.js reports for the mouse gate (map_overlay.py).
 * The layout is handed to the host to keep (`onLayout`), and handed back
 * when the overlay opens, so panels stay where they were put.
 */

const GAP = 8;
const SNAP_MS = 160;
const EDGES = ["left", "right", "top", "bottom"];

/** Where each panel starts, the first time: data and choices top-left,
 *  the chart bottom-left, the linked views down the right. */
export const DEFAULT_LAYOUT = {
  "p-data": { edge: "top", offset: GAP, minimized: false },
  "p-choose": { edge: "left", offset: 140, minimized: false },
  "p-chart": { edge: "bottom", offset: GAP, minimized: false },
  "p-hists": { edge: "right", offset: GAP, minimized: false },
  "p-box": { edge: "right", offset: 300, minimized: false },
};

/**
 * The edge nearest to a rectangle, and the offset along it - what a drop
 * means. Pure, for the tests: `rect` {left, top, width, height} in a
 * viewport of `w` x `h`.
 */
/**
 * How far each edge of the map is covered by QGIS's own widgets - its
 * message bar overlays the top of the canvas - from the host's keep-out
 * rectangles: a rectangle touching an edge pushes that edge in. Panels
 * then cling to the uncovered map, and a header is never under the bar
 * (found 2026-10-20: a drag on the Histograms header went to the bar).
 */
export function insetsFrom(rects, w, h) {
  const inset = { top: 0, right: 0, bottom: 0, left: 0 };
  for (const r of rects || []) {
    if (!(r.w > 0 && r.h > 0)) continue;
    if (r.y <= 2 && r.w >= w / 2) inset.top = Math.max(inset.top, r.y + r.h);
    else if (r.y + r.h >= h - 2 && r.w >= w / 2) inset.bottom = Math.max(inset.bottom, h - r.y);
    else if (r.x <= 2 && r.h >= h / 2) inset.left = Math.max(inset.left, r.x + r.w);
    else if (r.x + r.w >= w - 2 && r.h >= h / 2) inset.right = Math.max(inset.right, w - r.x);
  }
  return inset;
}

export function snap(rect, w, h) {
  const distance = {
    left: rect.left,
    right: w - (rect.left + rect.width),
    top: rect.top,
    bottom: h - (rect.top + rect.height),
  };
  const edge = EDGES.reduce((a, b) => (distance[b] < distance[a] ? b : a));
  const along = edge === "left" || edge === "right" ? rect.top : rect.left;
  return { edge, offset: Math.max(GAP, Math.round(along)) };
}

/**
 * Pixel positions for every panel: {id: {left, top}}. `sizes` {id: {w, h}}.
 *
 * Each panel goes at its edge and offset, then is pushed ALONG its edge
 * past anything already placed that it would overlap - on its own edge or
 * another: a tab on the left edge must clear a panel in the top-left
 * corner too (seen 2026-10-20, the Choose tab under the Data panel).
 * Top and bottom are placed first, so the corners belong to them. Kept
 * inside the viewport; if there is no room left, a panel overlaps rather
 * than vanishing.
 */
export function place(layout, sizes, w, h) {
  const out = {};
  const fixed = [];
  const overlaps = (a, b) => a.left < b.left + b.w && b.left < a.left + a.w
    && a.top < b.top + b.h && b.top < a.top + a.h;
  for (const edge of ["top", "bottom", "left", "right"]) {
    const vertical = edge === "left" || edge === "right";
    const ids = Object.keys(layout)
      .filter((id) => layout[id].edge === edge && sizes[id])
      .sort((a, b) => layout[a].offset - layout[b].offset);
    for (const id of ids) {
      const { w: pw, h: ph } = sizes[id];
      const room = (vertical ? h - ph : w - pw) - GAP;
      let along = Math.max(GAP, Math.min(layout[id].offset, room));
      const at = (a) => (vertical
        ? { left: edge === "left" ? GAP : w - pw - GAP, top: a, w: pw, h: ph }
        : { left: a, top: edge === "top" ? GAP : h - ph - GAP, w: pw, h: ph });
      let rect = at(along);
      for (let guard = 0; guard < fixed.length + 1; guard++) {
        const hit = fixed.find((f) => overlaps(rect, f));
        if (!hit) break;
        const past = (vertical ? hit.top + hit.h : hit.left + hit.w) + GAP;
        if (past > room) break;           // no room further along: overlap
        along = past;
        rect = at(along);
      }
      fixed.push(rect);
      out[id] = { left: rect.left, top: rect.top };
    }
  }
  return out;
}

export class Shell {
  /**
   * @param o
   *  - root      the element holding the .panel sections
   *  - onLayout  (layout) whenever a panel is moved, minimised or expanded
   *  - onChange  () after any placement, so the mouse gate can follow
   */
  constructor({ root, onLayout = null, onChange = null }) {
    this.root = root;
    this.onLayout = onLayout;
    this.onChange = onChange;
    this.layout = JSON.parse(JSON.stringify(DEFAULT_LAYOUT));
    this.on = false;
    this.panels = Array.from(root.querySelectorAll(".panel"));
    for (const panel of this.panels) this._chrome(panel);
    this._frame = 0;
    const later = () => this.schedule();
    new ResizeObserver(later).observe(document.documentElement);
    const sizes = new ResizeObserver(later);
    for (const panel of this.panels) sizes.observe(panel);
    window.addEventListener("resize", later);
  }

  /** Over the map or not; `saved` is the host's copy of the last layout. */
  setOverlay(on, saved = null) {
    this.on = !!on;
    if (saved && typeof saved === "object") {
      for (const [id, spot] of Object.entries(saved)) {
        if (this.layout[id] && EDGES.includes(spot.edge)) {
          this.layout[id] = { edge: spot.edge, offset: Number(spot.offset) || GAP,
                              minimized: !!spot.minimized };
        }
      }
    }
    for (const panel of this.panels) {
      if (!this.on) {
        panel.style.left = panel.style.top = "";
        panel.classList.remove("minimized", "edge-left", "edge-right",
                               "edge-top", "edge-bottom");
      }
    }
    this.schedule();
  }

  schedule() {
    if (!this._frame) {
      this._frame = requestAnimationFrame(() => { this._frame = 0; this.apply(); });
    }
  }

  /** QGIS widgets over the canvas, [{x, y, w, h}] in page pixels. */
  setKeepOut(rects) {
    this.keepOut = Array.isArray(rects) ? rects : [];
    this.schedule();
  }

  /** The uncovered map: its offset, and its size. */
  _box() {
    const inset = insetsFrom(this.keepOut, window.innerWidth, window.innerHeight);
    document.body.style.setProperty("--inset-v", `${inset.top + inset.bottom}px`);
    return { x: inset.left, y: inset.top,
             w: window.innerWidth - inset.left - inset.right,
             h: window.innerHeight - inset.top - inset.bottom };
  }

  apply() {
    if (this.on) {
      const box = this._box();
      const w = box.w;
      const h = box.h;
      const sizes = {};
      for (const panel of this.panels) {
        const spot = this.layout[panel.id];
        if (!spot) continue;
        panel.classList.toggle("minimized", spot.minimized);
        for (const edge of EDGES) panel.classList.toggle(`edge-${edge}`, spot.edge === edge);
        if (panel.offsetParent === null) continue;     // hidden: nothing to show
        sizes[panel.id] = { w: panel.offsetWidth, h: panel.offsetHeight };
      }
      const spots = place(this.layout, sizes, w, h);
      for (const [id, xy] of Object.entries(spots)) {
        const panel = document.getElementById(id);
        if (panel.classList.contains("dragging")) continue;
        panel.style.left = `${xy.left + box.x}px`;
        panel.style.top = `${xy.top + box.y}px`;
      }
    }
    if (this.onChange) this.onChange();
  }

  _save() {
    if (this.onLayout) this.onLayout(JSON.parse(JSON.stringify(this.layout)));
  }

  setMinimized(id, minimized) {
    if (!this.layout[id]) return;
    this.layout[id].minimized = !!minimized;
    this.schedule();
    this._save();
  }

  /** A header with the title and a minimise button; the tab a minimised
   *  panel becomes is the same header, so one click target does both. */
  _chrome(panel) {
    const head = document.createElement("header");
    head.className = "panel-head";
    const title = document.createElement("span");
    title.className = "panel-title";
    title.textContent = panel.dataset.title || panel.id;
    const min = document.createElement("button");
    min.type = "button";
    min.className = "panel-min";
    min.title = "Minimise against the edge";
    min.textContent = "–";
    head.append(title, min);
    panel.prepend(head);
    min.addEventListener("click", (event) => {
      event.stopPropagation();
      this.setMinimized(panel.id, true);
    });
    // Minimised, a click on the tab expands; expanded, the header drags.
    let drag = null;
    head.addEventListener("pointerdown", (event) => {
      if (!this.on || event.button !== 0 || event.target === min) return;
      const r = panel.getBoundingClientRect();
      drag = { x: event.clientX, y: event.clientY, left: r.left, top: r.top, moved: false };
      try { head.setPointerCapture(event.pointerId); } catch (_) { /* fine */ }
    });
    head.addEventListener("pointermove", (event) => {
      if (!drag) return;
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      if (!drag.moved && Math.hypot(dx, dy) < 4) return;   // a click, not a drag
      drag.moved = true;
      panel.classList.add("dragging");
      panel.style.left = `${drag.left + dx}px`;
      panel.style.top = `${drag.top + dy}px`;
    });
    const end = () => {
      if (!drag) return;
      const was = drag;
      drag = null;
      panel.classList.remove("dragging");
      const spot = this.layout[panel.id];
      if (!was.moved) {
        if (spot && spot.minimized) this.setMinimized(panel.id, false);
        return;
      }
      const r = panel.getBoundingClientRect();
      const box = this._box();
      const snapped = snap({ left: r.left - box.x, top: r.top - box.y,
                             width: r.width, height: r.height }, box.w, box.h);
      this.layout[panel.id] = { ...spot, ...snapped };
      // Glide the last few pixels to the edge rather than jump.
      panel.classList.add("snapping");
      setTimeout(() => { panel.classList.remove("snapping"); this.schedule(); }, SNAP_MS);
      this.apply();
      this._save();
    };
    head.addEventListener("pointerup", end);
    head.addEventListener("pointercancel", end);
    head.addEventListener("dblclick", () => {
      if (this.on) this.setMinimized(panel.id, !(this.layout[panel.id] || {}).minimized);
    });
  }
}
