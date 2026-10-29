/**
 * Bundles wasm_page.js (and @duckdb/duckdb-wasm from web/node_modules) into
 * <outdir>/probe.bundle.js for wasm_probe.py. Build time only; nothing here
 * ships with the plugin.
 *
 *   cd web && npm ci            # once: duckdb-wasm comes in with vgplot
 *   node tools/webengine_probes/build_wasm_page.mjs <outdir>
 *
 * kriging-rs is NOT bundled: the page imports its wasm-bindgen output at run
 * time over the probe's scheme, as a plugin page would.
 */
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const WEB = resolve(HERE, "..", "..", "web");
const outdir = process.argv[2];
if (!outdir) {
  console.error("usage: node build_wasm_page.mjs <outdir>");
  process.exit(2);
}
const { build } = await import(pathToFileURL(join(WEB, "node_modules", "esbuild", "lib", "main.js")).href);
await build({
  entryPoints: [join(HERE, "wasm_page.js")],
  outfile: join(resolve(outdir), "probe.bundle.js"),
  bundle: true, format: "esm", platform: "browser", target: ["chrome120"],
  nodePaths: [join(WEB, "node_modules")],
  logLevel: "warning",
});
console.log("wrote %s", join(resolve(outdir), "probe.bundle.js"));
