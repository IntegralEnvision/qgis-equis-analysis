/**
 * The page's entry point.
 *
 * PAGE CONTRACT, kept compatible with tools/mosaic_poc/overlay.html so the
 * host code does not have to care which page it is talking to:
 *
 *   out  bridge.ready()            once state is built AND signals connected
 *   out  bridge.log(level, msg)
 *   out  bridge.chosen(json)       the Analysis chooser's {matrix, x, y}
 *   in   bridge.query(sql, kind)   through the Mosaic connector
 *
 * READY IS NOT loadFinished. Constructing a QWebChannel costs an async round
 * trip, so anything Python emits on loadFinished arrives before this page has
 * connected to it and is lost. Python emits nothing until ready() is called.
 */
import { Coordinator, createAPIContext } from "@uwdata/vgplot";
import { Histogram } from "./chart.js";
import { Scatter } from "./scatterplot.js";
import { Ternary } from "./ternaryplot.js";
import { Chooser } from "./chooser.js";
import { Linked, setMapSelection } from "./linked.js";
import { DataPanel } from "./datapanel.js";
import { Shell } from "./shell.js";
import { connectBridge, qtConnector } from "./connector.js";

const status = (message) => {
  const node = document.getElementById("status");
  if (!node) return;
  node.textContent = message;
  // Startup feedback and failures only: once connected it would sit over
  // the chart's lower-left corner, which is where the colour key is.
  node.hidden = message === "connected";
};

async function start() {
  const bridge = await connectBridge();

  // Route uncaught page errors to the QGIS log. A web view's console is
  // invisible unless someone has the inspector open, so without this a
  // failing page fails silently.
  const report = (level, message) => {
    try {
      bridge.log(level, String(message));
    } catch (_) {
      /* the channel is the thing that failed; nothing else to try */
    }
  };
  window.addEventListener("error", (event) =>
    report("error", `${event.message} (${event.filename}:${event.lineno})`));
  window.addEventListener("unhandledrejection", (event) =>
    report("error", `unhandled rejection: ${event.reason}`));

  let histogram = null;
  let scatter = null;
  let linked = null;
  // The scatter's crossfilter and the options it was drawn from, so a map
  // selection can be put on the chart already there (mapSelection).
  let cross = null;
  let scatterOptions = null;
  // The last draw, so laying the page over the map (or back) can redraw it
  // at the new size rather than leave a chart sized for the other place.
  let lastDraw = null;

  // OVER THE MAP (map_overlay.py). The host decides who gets the mouse, by
  // these rectangles: over a panel the page does, everywhere else the map.
  // The page reports them whenever its layout moves; it keeps no gate of
  // its own, because two gates on one mouse desynced in the overlay spike.
  let overlayOn = false;
  let cardsFrame = 0;
  const CARDS = ".panel, .chooser-warn-panel:not([hidden])";
  const sendCards = () => {
    cardsFrame = 0;
    if (!overlayOn) return;
    const rects = Array.from(document.querySelectorAll(CARDS))
      .map((node) => node.getBoundingClientRect())
      .filter((r) => r.width > 0 && r.height > 0)
      .map((r) => ({ x: Math.floor(r.left), y: Math.floor(r.top),
                     w: Math.ceil(r.width), h: Math.ceil(r.height) }));
    try {
      bridge.cards(JSON.stringify(rects));
    } catch (_) {
      /* an older host without the slot: nothing to gate */
    }
  };
  const cardsMoved = () => {
    if (overlayOn && !cardsFrame) cardsFrame = requestAnimationFrame(sendCards);
  };
  new MutationObserver(cardsMoved).observe(document.body,
    { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden", "class"] });
  window.addEventListener("resize", cardsMoved);
  document.addEventListener("scroll", cardsMoved, true);
  const ask = (what) => {
    try { bridge.asked(what); } catch (_) { /* an older host */ }
  };
  // The panels (shell.js). Their layout is the host's to keep (QSettings),
  // so it survives a restart; the page only reports it.
  const shell = new Shell({
    root: document.getElementById("shell"),
    onChange: cardsMoved,
    onLayout: (layout) => {
      try { bridge.remember("overlay_layout", JSON.stringify(layout)); } catch (_) { /* older host */ }
    },
  });
  const dataPanel = new DataPanel(document.getElementById("data-body"), ask);
  let drawCount = 0;
  const coordinator = new Coordinator();
  const connector = qtConnector(bridge);
  coordinator.databaseConnector(connector);
  const vg = createAPIContext({ coordinator });

  // The Analysis chooser. Hidden until the host offers a catalogue, and
  // outside #views, so a chart replacing a chart never takes it with it.
  const chooser = new Chooser({
    element: document.getElementById("controls"),
    onChoose: (choice) => {
      // Said at once, before the host answers: the answer can be a pivot
      // of every analyte in the matrix, seconds away for water.
      chooser.setWorking("Asking QGIS…");
      try {
        bridge.chosen(JSON.stringify(choice));
      } catch (error) {
        report("error", `choice could not reach the host: ${error}`);
      }
    },
  });

  // Exposed so the host and the smoke test can drive the page without
  // reaching into module scope.
  window.equis = {
    vg,
    coordinator,
    bridge,
    connector,
    /** Through the Coordinator, as a chart would go. */
    query: (sql, kind = "json") => coordinator.query(sql, { type: kind }),
    /** Straight to the connector, skipping Mosaic's manager and cache.
     *  Worth keeping: it is what separates "our bridge is broken" from
     *  "Mosaic is holding the promise", and those look identical from
     *  outside. */
    direct: (sql, type = "arrow") => connector.query({ type, query: sql }),

    /**
     * Draw the histogram. Called by the panel with the same filters the
     * map layer was built from, so the chart bins what is on the screen
     * (decided 2026-09-23).
     */
    async histogram(options = {}) {
      const target = document.getElementById("views");
      // One request per call, numbered, so the log shows which request
      // produced which chart - and whether a later one overtook it.
      const request = ++drawCount;
      report("info", `histogram #${request} asked: `
        + `${options.chemical || "?"} matrix=${options.matrix || "*"} `
        + `fraction=${options.fraction || "*"} `
        + `method=${options.method || "*"} `
        + `aggregate=${options.aggregate || "?"} `
        + `nd=${options.ndColumn || "?"} `
        + `locations=${(options.locations || []).length || "all"}`);
      // Retire the chart being replaced BEFORE the new one exists. See
      // Histogram.destroy for what an unretired chart did on resize. A
      // scatter shares the element, so it is retired too.
      lastDraw = () => window.equis.histogram(options);
      if (histogram) histogram.destroy();
      if (linked) { linked.destroy(); linked = null; }
      document.getElementById("linked-hists").replaceChildren();
      document.getElementById("linked-box").replaceChildren();
      if (scatter) { scatter.destroy(); scatter = null; }
      histogram = new Histogram({
        vg, coordinator, element: target,
        onBrush: (brush) => {
          // Straight to Python: the map owns what a brush means, and the
          // page should not decide how a selection is drawn.
          try {
            bridge.brushed(brush ? JSON.stringify(brush) : "");
          } catch (error) {
            report("error", `brush could not reach the host: ${error}`);
          }
        },
      });
      const drawn = histogram;
      const state = await drawn.show(options);
      report("info", `histogram #${request} drew ${options.chemical || "?"}: `
        + `n=${state ? state.total : "?"} `
        + `nd=${state ? state.nonDetects : "?"} `
        + `unit=${state ? state.unit || "-" : "?"}`
        + `${drawn !== histogram ? " (superseded)" : ""}`);
      return state;
    },

    /**
     * Draw a two-analyte scatter (US-24) from a pivot table the host built
     * (equis_loader/pivot.py). Same retire-first rule as the histogram, and
     * the same brush route to the map.
     */
    async scatter(options = {}) {
      const target = document.getElementById("views");
      const request = ++drawCount;
      report("info", `scatter #${request} asked: `
        + `${(options.x || {}).chemical || "?"} x ${(options.y || {}).chemical || "?"} `
        + `pivot=${options.pivot || "?"} `
        + `locations=${(options.locations || []).length || "all"}`);
      // Held, not copied: mapSelection updates its locations, so a redraw
      // on resize draws the selection as it is now.
      scatterOptions = options;
      lastDraw = () => window.equis.scatter(scatterOptions);
      if (histogram) { histogram.destroy(); histogram = null; }
      if (linked) { linked.destroy(); linked = null; }
      if (scatter) scatter.destroy();
      const toHost = (brush) => {
        try {
          bridge.brushed(brush ? JSON.stringify(brush) : "");
        } catch (error) {
          report("error", `brush could not reach the host: ${error}`);
        }
      };
      // The chart on top, its linked views under it (linked.js): one
      // crossfilter per draw, shared by every view of these samples.
      const main = document.createElement("div");
      main.className = "card main";
      target.replaceChildren(main);
      // The linked views have panels of their own (shell.js).
      const below = document.getElementById("linked-hists");
      const boxPanel = document.getElementById("linked-box");
      below.replaceChildren();
      boxPanel.replaceChildren();
      cross = vg.Selection.crossfilter();
      // A ternary is a chart of samples too (ternaryplot.js extends
      // Scatter): same brush, same route to the map, its own marks.
      const ternary = options.kind === "ternary";
      const Chart = ternary ? Ternary : Scatter;
      scatter = new Chart({ vg, coordinator, element: main, onBrush: toHost, cross });
      // The map's selection is a clause, set once the chart's selection
      // relays `cross` and before its marks first query - never baked
      // into the chart's views, or a later, wider selection could not
      // bring back what they left out.
      setMapSelection(vg, cross, Array.isArray(options.locations) ? options.locations : null);
      chooser.showing((options.x || {}).prefix, (options.y || {}).prefix,
                      (options.points || {}).mode || null,
                      (options.z || {}).prefix || null,
                      options.kind === "ternary" ? "ternary" : "scatter");
      const drawn = scatter;
      drawn.onTimings = (t) => report("info", `scatter #${request} timings (ms since draw asked): `
        + Object.entries(t).map(([k, v]) => `${k}=${v}`).join(" "));
      const state = await drawn.show({ ...options, locations: null });
      report("info", `scatter #${request} drew: n=${state ? state.total : "?"}`
        + `${drawn !== scatter ? " (superseded)" : ""}`);
      if (drawn === scatter && state && state.total && drawn.views) {
        const axes = state.axes || {};
        const columns = ternary
          ? [["x", "a"], ["y", "b"], ["z", "c"]].map(([k, field]) => ({ ...options[k], field }))
          : [{ ...options.x, field: "x", axis: axes.x }, { ...options.y, field: "y", axis: axes.y }];
        linked = new Linked({ vg, coordinator, element: below, boxElement: boxPanel,
                              cross, onBrush: toHost });
        try {
          const began = performance.now();
          await linked.show({ views: drawn.views, pivot: options.pivot, columns,
                              detectTrue: options.detectTrue, total: state.total });
          report("info", `linked views #${request} drew in ${Math.round(performance.now() - began)} ms`);
        } catch (error) {
          report("error", `linked views failed: ${error && error.message || error}`);
          below.textContent = `Linked views failed: ${error && error.message || error}`;
        }
      }
      return state;
    },

    /**
     * The map's selection changed: dim the scatter or ternary and every
     * linked view to it IN PLACE - no spinner, no new views, the brush
     * kept. `locations` is an array of codes, or null for no selection.
     * False when there is no scatter to apply it to.
     */
    mapSelection(locations = null) {
      const codes = Array.isArray(locations) ? locations : null;
      if (scatterOptions) scatterOptions.locations = codes;
      if (!cross || !scatter) return false;
      setMapSelection(vg, cross, codes);
      report("info", `map selection: ${codes ? codes.length : "none"}`);
      return true;
    },

    /**
     * Show a spinner and a message in the chart area. The host calls this
     * before each long step (pivoting, classing, building the map) so the
     * window is never a blank wait.
     */
    busy(message) {
      const target = document.getElementById("views");
      const node = document.createElement("div");
      node.className = "busy";
      node.textContent = message || "Working…";
      target.replaceChildren(node);
    },

    /** The chooser's catalogue for a matrix: analytes, counts, left-outs. */
    offer(payload) {
      chooser.setOffer(payload || {});
      report("info", `chooser offered ${payload && payload.matrix || "no matrix"}: `
        + `${((payload || {}).variables || []).length} analytes, `
        + `${((payload || {}).skipped || []).length} left out`);
    },

    /** How many samples each analyte shares with the chosen X. */
    paired(payload) { chooser.setPaired(payload || {}); },

    /** ...and with X and Y, for a ternary's Z. */
    pairedZ(payload) { chooser.setPairedZ(payload || {}); },

    /** What to do next, or why a choice cannot be drawn. */
    note(text) { chooser.setNote(text || ""); },

    /** A slow host step: in the chart area when a chart will follow it,
     *  in the chooser's line when it is the chooser's own (a pivot). */
    working({ message, where } = {}) {
      if (where === "chart") {
        // The chart says it now; the chooser's "Asking QGIS..." is done.
        chooser.settle();
        window.equis.busy(message);
      } else {
        chooser.setWorking(message);
      }
    },

    /** Laid over the map (true) or back in the panel (false). The host
     *  has already moved the view; this re-lays the page for it. */
    overlay(arg) {
      // true/false, or {on, layout} with the host's saved panel layout.
      const on = arg && typeof arg === "object" ? !!arg.on : !!arg;
      overlayOn = on;
      document.body.classList.toggle("overlay", overlayOn);
      shell.setOverlay(on, arg && typeof arg === "object" ? arg.layout : null);
      report("info", `overlay ${overlayOn ? "on" : "off"}`);
      // The redraw drops any brush; the map's highlight must go with it.
      try { bridge.brushed(""); } catch (_) { /* nothing to clear */ }
      if (lastDraw) lastDraw();
      cardsMoved();
    },

    /** What the panel has loaded and its warnings, for the Data panel.
     *  The host's banner decides what is cached or cut short; this shows
     *  what it decided (datapanel.js). */
    dataStatus(payload) { dataPanel.update(payload || {}); },

    /** QGIS's own widgets over the canvas (its message bar), which the
     *  panels keep clear of (shell.js insetsFrom). */
    keepOut(rects) { shell.setKeepOut(rects || []); },

    /** The panels' window manager, for diagnostics and the preview. */
    get shell() { return shell; },

    /** The chooser, for diagnostics and the preview harness. */
    get chooser() { return chooser; },

    /** The live chart, for diagnostics. */
    get chart() { return scatter || histogram; },

    /** The linked views under a scatter or ternary, for diagnostics. */
    get linked() { return linked; },
  };

  status("connected");
  // Connected and IDLE: plain words, no spinner. A spinner says work is
  // under way, and nothing is until the host asks - shown spinning here, a
  // page that is simply waiting to be used looked stuck (seen 2026-10-09
  // in the startup probe, which never asks for a chart). The host calls
  // busy() when it starts real work.
  const idle = document.createElement("div");
  idle.className = "idle";
  idle.textContent = "Ready. Nothing to draw yet.";
  document.getElementById("views").replaceChildren(idle);
  report("info", `page ready ${Math.round(performance.now())} ms after navigation`);
  // LAST: everything above must exist before Python is told it may emit.
  bridge.ready();
}

start().catch((error) => {
  status(`failed: ${error.message}`);
  // No bridge to log through if we got here, so leave it in the console too.
  console.error(error);
});
