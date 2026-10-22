// Bundle the spike page from Chemrooms' SOURCE, unchanged, with the
// plugin's esbuild. Reads ../chemrooms; writes only to the given out dir.
//
//   node tools/spike_chemduck/build.mjs <out-dir> [<chemrooms-checkout>]
//
// Chemrooms is a Vite app, so two Vite-isms are supplied here: `?raw` SQL
// imports become text, and import.meta.env is empty - "no project
// configured" - which makes Chemrooms skip opening a source itself and
// leave that to the plugin's Python side.
import { copyFileSync, mkdirSync, readFileSync, realpathSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const out = resolve(process.argv[2]);
const chemrooms = resolve(process.argv[3] || join(here, "..", "..", "..", "..", "chemrooms"));
const esbuild = await import(pathToFileURL(join(here, "..", "..", "web", "node_modules",
                                                "esbuild", "lib", "main.js")).href);
mkdirSync(out, { recursive: true });

const raw = {
  name: "raw",
  setup(build) {
    build.onResolve({ filter: /\?raw$/ }, (args) => ({
      path: resolve(args.resolveDir, args.path.replace(/\?raw$/, "")), namespace: "raw" }));
    build.onLoad({ filter: /.*/, namespace: "raw" }, (args) => ({
      contents: readFileSync(args.path, "utf8"), loader: "text" }));
  },
};

const result = await esbuild.build({
  entryPoints: [join(here, "page", "entry.ts")],
  bundle: true, minify: true, format: "esm", outfile: join(out, "app.js"),
  // apache-arrow is SQLRooms' dependency, not Chemrooms' own, so pnpm keeps
  // it out of the top level: take the copy duckdb-core resolves, so the
  // connector's tables and SQLRooms' are one Arrow.
  alias: { chemrooms: join(chemrooms, "client", "src"),
           "apache-arrow": join(realpathSync(join(chemrooms, "client", "node_modules",
                                                  "@sqlrooms", "duckdb-core")),
                                "..", "..", "apache-arrow") },
  nodePaths: [join(chemrooms, "client", "node_modules")],
  define: { "import.meta.env": "{}", "import.meta.env.DEV": "false" },
  plugins: [raw], metafile: true, logLevel: "warning",
});
copyFileSync(join(here, "page", "index.html"), join(out, "index.html"));
const bytes = statSync(join(out, "app.js")).size;
const heaviest = {};
for (const [path, info] of Object.entries(result.metafile.inputs)) {
  const m = path.match(/node_modules[\\/](?:\.pnpm[\\/][^\\/]+[\\/]node_modules[\\/])?((?:@[^\\/]+[\\/])?[^\\/]+)/);
  const key = m ? m[1] : path.includes("chemrooms") ? "(chemrooms source)" : "(spike)";
  heaviest[key] = (heaviest[key] || 0) + info.bytes;
}
console.log(JSON.stringify({ app_js_kb: Math.round(bytes / 1024),
  inputs_kb: Object.fromEntries(Object.entries(heaviest).sort((a, b) => b[1] - a[1]).slice(0, 8)
    .map(([k, v]) => [k, Math.round(v / 1024)])) }));
