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
      if (histogram) histogram.destroy();
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
      if (histogram) { histogram.destroy(); histogram = null; }
      if (scatter) scatter.destroy();
      // A ternary is a chart of samples too (ternaryplot.js extends
      // Scatter): same brush, same route to the map, its own marks.
      const Chart = options.kind === "ternary" ? Ternary : Scatter;
      scatter = new Chart({
        vg, coordinator, element: target,
        onBrush: (brush) => {
          try {
            bridge.brushed(brush ? JSON.stringify(brush) : "");
          } catch (error) {
            report("error", `brush could not reach the host: ${error}`);
          }
        },
      });
      chooser.showing((options.x || {}).prefix, (options.y || {}).prefix,
                      (options.points || {}).mode || null,
                      (options.z || {}).prefix || null,
                      options.kind === "ternary" ? "ternary" : "scatter");
      const drawn = scatter;
      drawn.onTimings = (t) => report("info", `scatter #${request} timings (ms since draw asked): `
        + Object.entries(t).map(([k, v]) => `${k}=${v}`).join(" "));
      const state = await drawn.show(options);
      report("info", `scatter #${request} drew: n=${state ? state.total : "?"}`
        + `${drawn !== scatter ? " (superseded)" : ""}`);
      return state;
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

    /** The chooser, for diagnostics and the preview harness. */
    get chooser() { return chooser; },

    /** The live chart, for diagnostics. */
    get chart() { return scatter || histogram; },
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
