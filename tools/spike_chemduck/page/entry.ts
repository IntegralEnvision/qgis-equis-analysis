/**
 * Spike (2026-10-22): Chemrooms' own data setup and query builder, run
 * unchanged on a plugin page, through the plugin's bridge to the plugin's
 * Python DuckDB. No SQLRooms UI, no React, no DuckDB-WASM.
 *
 * The page answers one question: can the plugin take Chemrooms' data
 * handling without taking its layout? So it imports exactly two things from
 * ../../chemrooms - initEntityLayers (what Chemrooms runs at startup) and
 * buildSamplesLayerSql (what it runs to colour a map by an analyte) - and
 * gives them a SQLRooms connector whose only job is to send SQL over the
 * plugin's bridge and decode the Arrow that comes back.
 */
import { createBaseDuckDbConnector } from "@sqlrooms/duckdb-core";
import { tableFromIPC, Table } from "apache-arrow";
import { initEntityLayers } from "chemrooms/setup/initEntityLayers";
import { buildSamplesLayerSql } from "chemrooms/setup/buildSamplesLayerSql";

declare const QWebChannel: any;
declare const qt: any;

type Bridge = {
  query(sql: string, kind: string, done: (envelope: string) => void): void;
  log(level: string, message: string): void;
  ready(): void;
};

function connectBridge(): Promise<Bridge> {
  return new Promise((resolve) => {
    new QWebChannel(qt.webChannelTransport, (channel: any) => resolve(channel.objects.bridge));
  });
}

function decodeBase64(text: string): Uint8Array {
  const binary = atob(text);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** SQL over the bridge, Arrow back - the whole of the plugin's side. */
function bridgeConnector(bridge: Bridge, counts: { n: number; ms: number }) {
  return createBaseDuckDbConnector({}, {
    async executeQueryInternal(sql: string) {
      const began = performance.now();
      const envelope: string = await new Promise((resolve) => bridge.query(sql, "arrow", resolve));
      counts.n += 1;
      counts.ms += performance.now() - began;
      const answer = JSON.parse(envelope);
      if (!answer.ok) throw new Error(answer.error);
      // A statement with no result set (CREATE, INSERT) answers "".
      return answer.data ? tableFromIPC(decodeBase64(answer.data)) : new Table();
    },
  });
}

// Chemrooms' defaults, unless the page's fragment names another recipe:
// #{"eventAgg":"maximum","dupAgg":"max"}
const RECIPE = {
  coloringAnalyte: "Arsenic", matrixFilter: "SO", fractionFilter: "Total",
  eventAgg: "most_recent", dupAgg: "avg", ndMethod: "half_dl",
  ...(location.hash.length > 1 ? JSON.parse(decodeURIComponent(location.hash.slice(1))) : {}),
};

async function main() {
  const out: Record<string, unknown> = { started: performance.now() };
  (window as any).__spike = out;
  const bridge = await connectBridge();
  const counts = { n: 0, ms: 0 };
  try {
    const connector = bridgeConnector(bridge, counts);
    await connector.initialize();
    let t = performance.now();
    const init = await initEntityLayers(connector as any);
    out.init = {
      ms: Math.round(performance.now() - t), hasGeoid: init.hasGeoid,
      hasChemduckSchema: init.hasChemduckSchema, sourceError: init.sourceError,
      elevationColumns: init.elevationColumns, groupSamples: init.groupSamples,
      locationsNeedingTerrain: init.locationsNeedingTerrain.length,
      queries: counts.n, bridgeMs: Math.round(counts.ms),
    };
    t = performance.now();
    const sql = buildSamplesLayerSql({ elevationColumns: init.elevationColumns, ...RECIPE });
    const table = await connector.query(sql);
    const rows = table.toArray().map((r: any) => r.toJSON());
    out.layer = { ms: Math.round(performance.now() - t), sql, rows: rows.length, recipe: RECIPE };
    // The location, value, flag and date the page computed, for the host to
    // check against the same SQL run directly and against the ReportStore.
    out.answers = rows.map((r: any) => ({
      id: r.location_id, result: r.result, detected: r.detected,
      date: r.rep_sample_date == null ? null : String(r.rep_sample_date),
      altitude: r.altitude, units: r.units }));
    out.ok = true;
  } catch (error) {
    out.ok = false;
    out.error = String(error && (error as Error).stack || error);
  }
  out.queries = counts.n;
  bridge.log("info", "SPIKE " + JSON.stringify(out));
  bridge.ready();
}

main();
