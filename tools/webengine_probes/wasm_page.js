// What a page served over a plugin-style custom scheme can do with WASM,
// inside QGIS's own QtWebEngine. Every stage records ok/ms/detail on its
// own; nothing is summed or averaged.
import * as duckdb from "@duckdb/duckdb-wasm";

const R = { mode: new URLSearchParams(location.search).get("mode"), stages: {}, order: [] };
const since = (t) => +(performance.now() - t).toFixed(1);
const here = (p) => new URL(p, location.href).href;

async function stage(name, fn) {
  const t = performance.now();
  R.order.push(name);
  try {
    const v = await fn();
    R.stages[name] = { ok: true, ms: since(t), ...(v || {}) };
  } catch (e) {
    R.stages[name] = { ok: false, ms: since(t), error: String((e && e.message) || e) };
  }
}

function timeout(p, ms, what) {
  return Promise.race([p, new Promise((_, no) => setTimeout(() => no(new Error(what + " timed out after " + ms + " ms")), ms))]);
}

async function run() {
  await stage("env", async () => ({
    href: location.href,
    origin: location.origin,
    ua: navigator.userAgent,
    secureContext: isSecureContext,
    crossOriginIsolated: self.crossOriginIsolated === true,
    webAssembly: typeof WebAssembly === "object",
    sharedArrayBuffer: typeof SharedArrayBuffer === "function",
    cores: navigator.hardwareConcurrency,
  }));

  await stage("fetch_wasm_bytes", async () => {
    const r = await fetch(here("add.wasm"));
    const b = await r.arrayBuffer();
    return { status: r.status, contentType: r.headers.get("content-type"), bytes: b.byteLength };
  });

  await stage("instantiate_from_bytes", async () => {
    const b = await (await fetch(here("add.wasm"))).arrayBuffer();
    const { instance } = await WebAssembly.instantiate(b);
    return { add_2_3: instance.exports.add(2, 3) };
  });

  await stage("instantiate_streaming", async () => {
    const { instance } = await WebAssembly.instantiateStreaming(fetch(here("add.wasm")));
    return { add_2_3: instance.exports.add(2, 3) };
  });

  await stage("xhr_wasm_bytes", () => new Promise((ok, no) => {
    const x = new XMLHttpRequest();
    x.open("GET", here("add.wasm"));
    x.responseType = "arraybuffer";
    x.onload = () => ok({ status: x.status, bytes: x.response && x.response.byteLength });
    x.onerror = () => no(new Error("xhr error"));
    x.send();
  }));

  const b64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
  await stage("instantiate_from_module_bytes", async () => {
    const bytes = b64((await import(here("addwasm.js"))).default);
    const { instance } = await WebAssembly.instantiate(bytes);
    return { add_2_3: instance.exports.add(2, 3) };
  });

  await stage("features", async () => {
    const f = await duckdb.getPlatformFeatures();
    let shared = false;
    try {
      const m = new WebAssembly.Memory({ initial: 1, maximum: 16, shared: true });
      shared = m.buffer instanceof SharedArrayBuffer;
    } catch (e) { shared = "threw: " + e.message; }
    return { ...f, sharedWasmMemory: shared };
  });

  await stage("memory_ceiling", async () => {
    // wasm32 tops out at 65536 pages = 4 GiB; how far does this renderer let one grow?
    const m = new WebAssembly.Memory({ initial: 1, maximum: 65536 });
    let pages = 1, failedAt = null;
    for (const target of [4096, 8192, 16384, 32768, 49152, 65535]) {
      try { m.grow(target - pages); pages = target; } catch (e) { failedAt = target; break; }
    }
    return { reachedMiB: pages * 64 / 1024, failedAtMiB: failedAt && failedAt * 64 / 1024 };
  });

  const workerTry = (url, opts) => timeout(new Promise((ok, no) => {
    const w = new Worker(url, opts);
    w.onmessage = (e) => { w.terminate(); e.data.ok ? ok(e.data) : no(new Error(e.data.error)); };
    w.onerror = (e) => { w.terminate(); no(new Error("worker error: " + (e.message || "no message"))); };
    w.postMessage(here("add.wasm"));
  }), 10000, "worker");
  await stage("worker_classic", () => workerTry(here("worker.js")));
  await stage("worker_module", () => workerTry(here("worker.js"), { type: "module" }));
  await stage("worker_blob", async () => {
    const src = await (await fetch(here("worker.js"))).text();
    return workerTry(URL.createObjectURL(new Blob([src], { type: "text/javascript" })));
  });

  await stage("webgl2", async () => {
    const gl = document.createElement("canvas").getContext("webgl2");
    if (!gl) throw new Error("no webgl2 context");
    const dbg = gl.getExtension("WEBGL_debug_renderer_info");
    return { renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
             maxTexture: gl.getParameter(gl.MAX_TEXTURE_SIZE) };
  });
  await stage("webgl1", async () => {
    const gl = document.createElement("canvas").getContext("webgl");
    if (!gl) throw new Error("no webgl context");
    const dbg = gl.getExtension("WEBGL_debug_renderer_info");
    return { renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER) };
  });
  await stage("webgpu", async () => {
    if (!navigator.gpu) throw new Error("navigator.gpu is undefined");
    const a = await navigator.gpu.requestAdapter();
    if (!a) throw new Error("requestAdapter returned null");
    return { adapter: a.info ? `${a.info.vendor} ${a.info.architecture} ${a.info.description}` : "present" };
  });

  // --- A real Rust kernel: kriging-rs, wasm-bindgen --target web, unbundled.
  let k = null;
  await stage("kriging_init", async () => {
    const mod = await import(here("kriging/kriging_rs.js"));
    try {
      await mod.default({ module_or_path: here("kriging/kriging_rs_bg.wasm") });
      k = mod;
      return { via: "fetch" };
    } catch (e) {
      // fetch refused: the same bytes, carried in an ES module, which the scheme does serve
      const t = performance.now();
      const bytes = b64((await import(here("krigwasm.js"))).default);
      const decodeMs = since(t);
      mod.initSync({ module: bytes });
      k = mod;
      return { via: "module bytes", fetchError: String(e.message || e), decodeMs, bytes: bytes.length };
    }
  });
  if (k) {
    const pts = JSON.parse(document.getElementById("points").textContent);
    const lat = Float64Array.from(pts.lat), lon = Float64Array.from(pts.lon);
    const v = Float64Array.from(pts.v, (x) => Math.log(x));
    let fit = null, model = null;
    await stage("kriging_fit_variogram", async () => {
      fit = k.fitVariogram(lat, lon, v, undefined, 15, k.WasmVariogramType.Exponential, undefined);
      return { n: v.length, fit };
    });
    await stage("kriging_build_model", async () => {
      model = k.WasmOrdinaryKriging.fromArrays(lat, lon, v, {
        type: fit.variogramType || fit.variogram_type || "exponential",
        nugget: fit.nugget, sill: fit.sill, range: fit.range });
      return {};
    });
    const bounds = [Math.min(...lon), Math.max(...lon), Math.min(...lat), Math.max(...lat)];
    for (const n of [128, 256, 512]) {
      await stage(`kriging_grid_${n}`, async () => {
        const g = model.predictGridArrays(...bounds, n, n);
        let finite = 0;
        for (const x of g.values) if (Number.isFinite(x)) finite++;
        return { cells: n * n, finite };
      });
    }
  }

  // --- DuckDB-WASM in a worker, against the facility's largest partition.
  let db = null, conn = null;
  await stage("duckdb_instantiate", async () => {
    const base = here("duckdb/");
    const bundles = {
      mvp: { mainModule: base + "duckdb-mvp.wasm", mainWorker: base + "duckdb-browser-mvp.worker.js" },
      eh: { mainModule: base + "duckdb-eh.wasm", mainWorker: base + "duckdb-browser-eh.worker.js" },
      coi: { mainModule: base + "duckdb-coi.wasm", mainWorker: base + "duckdb-browser-coi.worker.js",
             pthreadWorker: base + "duckdb-browser-coi.pthread.worker.js" },
    };
    const bundle = await duckdb.selectBundle(bundles);
    const worker = new Worker(bundle.mainWorker);
    db = new duckdb.AsyncDuckDB(new duckdb.VoidLogger(), worker);
    await timeout(db.instantiate(bundle.mainModule, bundle.pthreadWorker), 60000, "instantiate");
    conn = await db.connect();
    const ver = (await conn.query("select version() v, current_setting('threads') t")).toArray()[0];
    return { bundle: bundle.mainModule.split("/").pop(), version: String(ver.v), threads: String(ver.t) };
  });
  if (conn) {
    let buf = null;
    await stage("parquet_fetch", async () => {
      buf = new Uint8Array(await (await fetch(here("data/ws_n.parquet"))).arrayBuffer());
      return { MiB: +(buf.byteLength / 1048576).toFixed(1) };
    });
    await stage("parquet_register", async () => { await db.registerFileBuffer("ws_n.parquet", buf); return {}; });
    const Q = JSON.parse(document.getElementById("queries").textContent);
    for (const [name, sql] of Object.entries(Q)) {
      for (const pass of ["cold", "warm"]) {
        await stage(`q_${name}_${pass}`, async () => {
          const t = await conn.query(sql);
          return { rows: t.numRows };
        });
      }
    }
  }
}

run().catch((e) => { R.fatal = String(e); }).finally(() => { window.__probe = R; });
