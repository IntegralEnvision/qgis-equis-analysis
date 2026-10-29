// The page's kriging stages again in Node: same wasm, same points, same calls.
// Says whether QtWebEngine's V8 is the constraint or the kernel is
// (2026-10-27: the kernel - Node and the page agreed within run-to-run spread).
//
//   node tools/webengine_probes/node_kriging.mjs <PROBE_OUT>/page/points.json [kriging pkg dir]
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const PKG = process.argv[3] || process.env.PROBE_KRIGING_PKG
  || join(dirname(REPO), "kriging-rs", "npm", "kriging-rs-wasm", "pkg");
const k = await import(pathToFileURL(join(PKG, "kriging_rs.js")).href);
const pts = JSON.parse(readFileSync(process.argv[2], "utf8"));
const out = {};
let t = performance.now();
k.initSync({ module: readFileSync(join(PKG, "kriging_rs_bg.wasm")) });
out.init = performance.now() - t;
const lat = Float64Array.from(pts.lat), lon = Float64Array.from(pts.lon);
const v = Float64Array.from(pts.v, (x) => Math.log(x));
t = performance.now();
const fit = k.fitVariogram(lat, lon, v, undefined, 15, k.WasmVariogramType.Exponential, undefined);
out.fit = performance.now() - t;
t = performance.now();
const model = k.WasmOrdinaryKriging.fromArrays(lat, lon, v, {
  type: fit.variogramType || fit.variogram_type || "exponential", nugget: fit.nugget, sill: fit.sill, range: fit.range });
out.build = performance.now() - t;
const b = [Math.min(...lon), Math.max(...lon), Math.min(...lat), Math.max(...lat)];
for (const n of [128, 256, 512]) {
  t = performance.now();
  model.predictGridArrays(...b, n, n);
  out["grid_" + n] = performance.now() - t;
}
for (const key in out) out[key] = +out[key].toFixed(1);
console.log(JSON.stringify({ node: process.version, n: v.length, fit, ms: out }));
