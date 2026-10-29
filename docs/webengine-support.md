# Qt WebEngine support, per QGIS build

Running record of what [`tools/test_webengine.py`](../tools/test_webengine.py)
finds on real machines. Kept for the same reason the README records EQuIS API
behaviours: the vendor documentation and the public bug reports both turned
out to be unreliable here, and a design decision resting on either would have
been wrong.

**Run the probe once per QGIS restart.** Stage 7 registers a process-wide URL
scheme, which cannot be re-registered, so only the first run after a restart
can test that stage honestly. The probe now detects a re-run and says so.

## Results

| Date | OS | QGIS | Qt / PyQt | Python | Chromium | webengine | channel | modules |
|---|---|---|---|---|---|---|---|---|
| 2026-09-18 | Windows | 4.2.1 Belém do Pará | 6.11.0 / 6.11.0 | 3.12.13 | 140.0.0.0 | ✅ | ✅ | ✅ |

Still to check: **macOS and Linux on 4.2+**. Builds below 4.2 are out of
scope by decision (2026-09-18), so the 3.34 LTR and 4.0 are no longer worth
measuring.

The **Python** column doubles as the packaging survey. QGIS 4.2.1 on Windows
ships 3.12.13; if macOS and Linux 4.2 builds agree, the DuckDB wheel matrix
for US-22 is one cp312 wheel per platform rather than a grid. That is the
cheapest open question in the project and this table answers it.

## What each column answers

| Column | The question | Why it decides something |
|---|---|---|
| **webengine** | Do the PyQt bindings import, and does a `QWebEngineView` construct, load a page and call back from `runJavaScript`? | Without this there is no web view plane at all. This is the one that was reported broken on Windows OSGeo4W under QGIS 4.0 (qgis/QGIS#65398). |
| **channel** | Does `QWebChannel` round-trip both directions, including a **slot with a return value**? | This is the transport the whole design rests on, and the return-value form is the exact shape of Mosaic's `query(sql, type) -> result` connector contract. A channel that only works one way would not do. |
| **modules** | Can a custom URL scheme **registered from inside a plugin, after QGIS has already started**, serve an ES-module app whose import graph resolves? | `file://` forbids module imports, so a bundled vgplot build needs either this or a local HTTP server. Qt documents `QWebEngineUrlScheme` as needing registration before the application exists — which a plugin cannot do — so this was the stage most likely to fail. |

## What the 4.2.1 result settles

**The QGIS 4.0 Windows blocker is gone.** The compatibility matrix in
[the architecture notes](mosaic-qgis-plugin-architecture.md) called Windows
OSGeo4W "broken as of 4.0" and named the 4.2 LTR as the thing to watch. It
landed and it fixed it.

**The bridge design is viable as written.** `QWebChannel` carries values in
both directions on Windows, so §2 and §4 of the architecture notes need no
redesign.

**Open question 4 of the architecture notes is answered: bundle it.** A Vite
build of vgplot can ship inside the plugin and be served from a custom scheme.
No local HTTP server, no port to choose, nothing for a corporate firewall or
an endpoint agent to object to. The `restConnector` fallback in §6 stays a
contingency for builds that fail this stage rather than the expected route.

## WebEngine needs QGIS's own application, not just a QApplication

Found 2026-09-23 while building the round-trip test, and worth its own
section because every symptom points somewhere else.

Under `python-qgis.bat` with a hand-made `QApplication`, a custom scheme
loads **nothing**, and every intermediate step reports success:

    scheme registered           -> True
    handler reached             -> REQUEST: equis://app/index.html
    path resolved               -> ...\equis_loader\web\index.html, exists
    QBuffer opened              -> True, 1061 bytes
    job.reply() returned        -> no exception
    loadFinished                -> ok=False, url="", misses=[]

No error, no console message, no failed request. Chromium is initialised as
part of the application QGIS builds, so a bare `QApplication` gets a
WebEngine that accepts every call and draws nothing.

Two consequences:

* `tools/test_web_roundtrip.py` detects the case and skips with instructions
  rather than reporting four failures that look like broken code.
* The same constraint is why `tools/test_webengine.py` says to paste it into
  the QGIS Python Console. That warning is easy to read as fussiness; it is
  not.

Importing `QtWebEngineWidgets` before the application exists is a separate,
genuine requirement - PyQt raises a clear error for it - and satisfying it
does not fix the above.

## A hidden QWebEngineView hangs every Arrow query, silently

Found 2026-09-23. It cost most of a day and none of the symptoms point at it.

Mosaic's query consolidator defers **only** Arrow requests
(`QueryConsolidator.js`):

```js
if (entry.request.type === 'arrow') {
  id ||= wait(() => run());   // batch compatible queries next frame
} else {
  enqueue(entry, priority);   // json goes straight to the queue
}
```

and `wait()` is `requestAnimationFrame` in a browser. **Chromium fires
animation frames only for a visible surface.** A view that is constructed but
never shown therefore:

* answers `json` queries perfectly,
* propagates errors perfectly,
* and leaves every `arrow` request sitting in `pending` forever, with no
  error, no rejection, and nothing in any log.

It cannot be reproduced outside a browser: in Node `wait()` falls back to
`setImmediate` and everything works, which is what made three separate
theories look plausible before the real cause was measured.

**The required combination** for a view with no parent widget:

```python
view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
view.resize(640, 480)      # a zero-sized widget gets no frames
view.show()                # NOT optional
```

Measured with `WA_DontShowOnScreen` set: 16 animation frames in ~4 s, and the
Arrow round trip then passes. Off-screen is fine; hidden is not.

**How it was found**, because the method matters more than the fact: a probe
wrapped the connector and recorded every call Mosaic made to it. The trace
showed `type=arrow` arriving only from the test's own direct helper and never
from the manager - so the request was never issued, which pointed at the
queue rather than at the bridge, the base64, or the decoder. Reading source
had produced three wrong answers before that.

## WebAssembly on a plugin page

Measured 2026-10-27, QGIS 4.2.1 on Windows 11 (QtWebEngine 6.11.1,
Chromium 140, 14 cores). The question was whether the page can run compiled
kernels - a Rust crate built to `wasm32`, or DuckDB-WASM - rather than only
JavaScript. The page under test was served by a handler like `web_assets.py`'s
over three schemes, in a throwaway profile under `qgis --code`:

| | `equis://` flags as today | + `FetchApiAllowed` | + COOP/COEP headers |
|---|---|---|---|
| `fetch()`, so `instantiateStreaming` | **refused** | yes | yes |
| `XMLHttpRequest` | yes | yes | yes |
| WASM bytes carried in an ES module, then `instantiate` | yes | yes | yes |
| A Worker that loads WASM | starts; its `fetch()` is refused | yes | yes |
| SIMD, exceptions, bulk memory, BigInt | yes | yes | yes |
| One `WebAssembly.Memory` grown to 4 GiB | yes | yes | yes |
| `crossOriginIsolated`, `SharedArrayBuffer`, threads | no | no | **no** |
| WebGL 1, WebGL 2, WebGPU | no | no | no |

**`fetch()` is the one blocker, and it is a flag.** With the flags
`ensure_scheme` sets today (`SecureScheme | LocalAccessAllowed | CorsEnabled`)
Chromium refuses it: "Fetch API cannot load equis://... URL scheme is not
supported". Module imports and Worker scripts still load, which is why the
shipped page, which never fetches, has never noticed. Adding
`QWebEngineUrlScheme.Flag.FetchApiAllowed` (present in this PyQt) is enough
for wasm-bindgen's `init()` and DuckDB-WASM's worker. Without it,
**DuckDB-WASM does not fail, it hangs**: its worker's fetch rejects inside the
worker, `instantiate()` never settles, and the probe's 60 s timeout was the
only error. XHR and module-carried bytes work under today's flags and are
the fallbacks.

**A scheme registered after the default profile exists is half-registered.**
The probe's first run registered three schemes in a loop that called
`QWebEngineProfile.defaultProfile()` after the first. The first came out
`origin = wpbare://app`, a secure context; the other two came out
`origin = wpfetch://`, with no host, **not** a secure context, and `fetch()`
refused despite the flag. Nothing reported it. Register every scheme, then
touch the profile; `ensure_scheme`'s docstring already says so, and this is
what ignoring it looks like.

**No threads, even with the headers.** `setAdditionalResponseHeaders` exists
and the headers (COOP `same-origin`, COEP `require-corp`, CORP `same-origin`)
were set without error, but the page was still not cross-origin isolated, so
`SharedArrayBuffer` is undefined and DuckDB-WASM picks its single-threaded
`eh` bundle. Every WASM kernel on a page is one thread.

**No GPU by default.** `chrome://gpu` in the same view shows Chromium
started with `--disable-gpu --use-gl=disabled --in-process-gpu`: WebGL,
WebGL 2 and WebGPU "Disabled", compositing and canvas software only. This
holds with the view shown on screen, so it is not the off-screen trap above.
It can be turned on before QGIS starts; see
[The GPU can be turned on, with one flag more than expected](#the-gpu-can-be-turned-on-with-one-flag-more-than-expected).

### What a real kernel costs

**kriging-rs** (`../kriging-rs/npm/kriging-rs-wasm/pkg`, unchanged; 754 KB of
WASM). Ordinary kriging of log Nitrate, 338 detected WS locations at Facility A,
exponential variogram fitted in the page (nugget 0.013, sill 6.42, range
55.0 km, identical in every run). Each cell uses every station; one thread.

| | Plugin page (3 runs) | Node 24.15 |
|---|---|---|
| Load the WASM | 12-50 ms (64 ms via module bytes) | 3 ms |
| Fit variogram | 8-13 ms | 11 ms |
| 128 x 128 grid | 1.07-1.10 s | 1.09 s |
| 256 x 256 grid | 4.5-4.9 s | 5.8 s |
| 512 x 512 grid | 17.1-25.4 s | 26.4 s |

**The page is as fast as Node**, so QtWebEngine is not the constraint; the
kernel is. A grid worth drawing takes seconds, so it belongs in a Worker,
with `setNeighborhood` or a coarser grid, and its raster still has to cross
the bridge to become a QGIS layer. The test kriged detects only, which
biases high; real use has to deal with non-detects, under the same rules as
everywhere else.

**DuckDB-WASM 1.5.4** (`eh` bundle, 1 thread) against
`matrix=WS/fraction=N/part-0000.parquet` (43 MiB, 1,250,077 rows), beside
QGIS's own Python DuckDB 1.5.4 (14 threads) on the same file and SQL. Two
page runs, reported separately; Python ran once per process:

| | DuckDB-WASM | Python DuckDB |
|---|---|---|
| Instantiate | 1.47-1.52 s | - |
| Copy the parquet into the page (fetch + register) | 0.24 s | - |
| `count(*)` cold / warm | 619-973 ms / 13-18 ms | 7 ms / 5 ms |
| Group by analyte, warm | 63-77 ms | 23 ms |
| Nitrate per location, warm | 23-27 ms | 12 ms |
| `create table ... as select *` | 3.7-6.3 s | 2.5-4.4 s |
| Group by analyte on the table, warm | 78-110 ms | 21-23 ms |

It works, and it would be a second engine holding a second copy of the data,
slower at every step, with a fourth copy of the non-detect rules to keep in
parity. The architecture's choice (DuckDB in Python, the page asks over the
bridge) stands. DuckDB-WASM's remaining use is a page run in a browser
without QGIS.

### What this means

A WASM kernel is worth it for code shared with browser apps (kriging-rs,
geofield-rs, Chemrooms), for one binary on every platform instead of a
native wheel per platform, and for work off QGIS's GUI thread: the bridge's
`query` slot runs synchronously there, while a Worker runs in Chromium's
renderer process. It is not a route to threads.

The probe is [`tools/webengine_probes/wasm_probe.py`](../tools/webengine_probes/wasm_probe.py),
launched with `run_wasm.ps1 -Parquet <partition>`. It needs `npm ci` in
`web/`, and `../kriging-rs` (or `-KrigingPkg`) for the kriging stages.
`node_kriging.mjs` gives the Node column, and `summarise.py` prints the
results. Re-run from there on 2026-10-29, on QGIS 4.2.3: every stage passed
or failed as above. Timings were about twice those in the tables across the
board, Node's included, on the same machine while it was in use, so the
page-vs-Node and WASM-vs-Python comparisons held and the tables keep the
2026-10-27 numbers.

## The GPU can be turned on, with one flag more than expected

Measured 2026-10-29, **QGIS 4.2.3** on Windows 11 (QtWebEngine 6.11.1,
Chromium 140; Intel integrated graphics, device 0x7D41, driver
32.0.101.8860). Eleven launches, each a fresh QGIS in a throwaway profile,
because Chromium reads its flags once per process. Proof of a GPU is a
drawn pixel read back and a screenshot of the view, not a context object or
`chrome://gpu`'s word.

**Why it is off.** QGIS 4.2.3 runs Qt Quick on its `software` scene-graph
backend (`QQuickWindow.sceneGraphBackend()` returns `"software"`, with
`AA_ShareOpenGLContexts` set and a global share context present).
`QT_QUICK_BACKEND=rhi` in the environment does not change it, so it is set
in code. Qt WebEngine considers that backend unsupported (its own warning
says so) and starts Chromium with `--disable-gpu --use-gl=disabled`. Nothing
in `qgis.bat`, `o4w_env.bat` or `qt6_env.bat` sets GPU options.

| `QTWEBENGINE_CHROMIUM_FLAGS` | WebGL 1/2 | View shows the page | WebGPU |
|---|---|---|---|
| (none) | no context | yes | no adapter |
| `--ignore-gpu-blocklist` | no context; Qt still adds `--disable-gpu` | yes | no adapter |
| `--use-gl=angle` (alone, or with `--use-angle=d3d11 --ignore-gpu-blocklist --enable-unsafe-webgpu`) | **hardware**, ANGLE D3D11 on the Intel GPU | **no: blank white**, HTML included | no adapter |
| `--use-gl=angle --use-angle=d3d11 --disable-gpu-compositing` | **hardware** | **yes** | no adapter |
| the same + `--disable-gpu-rasterization` | hardware | yes | no adapter |

**An explicit `--use-gl` overrides Qt's decision; `--ignore-gpu-blocklist`
does not.** Qt then warns "--use-gl=angle is set with unsupported SceneGraph
Backend. Expect troubles!", and the trouble is real.

**With GPU compositing, the view is blank.** WebGL drew and read back the
right pixel, `chrome://gpu` reported everything "Hardware accelerated", and
the screen showed white: no canvas and no HTML, in a screen grab and in
`QWidget.grab` alike. Qt logged "D3D: No D3D device." over and over, and
Chromium's GPU process logged lost contexts and missing shared images. It
composites into D3D11 textures that a software scene graph has nowhere to
put. A check of `chrome://gpu` alone would have reported this configuration
as working.

**`--disable-gpu-compositing` is the missing flag.** It keeps Chromium's
compositor in software, which Qt can display, while WebGL still runs on the
GPU and is read back each frame. `chrome://gpu` then says "Hardware
accelerated but at reduced performance". The screenshot showed the
WebGL-drawn canvas and the HTML beside it, and the "No D3D device" messages
stopped.

**What it buys.** Frame intervals the view actually displayed, a 1000 x 750
canvas redrawn every animation frame for 3 s:

| | `--use-gl=angle --use-angle=d3d11 --disable-gpu-compositing` | default |
|---|---|---|
| WebGL, 250,000 points | 171 frames; p50 16.7 ms, p95 16.8 ms (60 fps) | no WebGL |
| WebGL, 1,000,000 points | 94 frames; p50 33.3 ms, p95 50.0 ms | no WebGL |
| 2D canvas, 25,000 circles | 23 frames; p50 133 ms | 10 frames; p50 350 ms |

Drawing alone, synchronised by a one-pixel `readPixels` on a 1024 x 1024
canvas, after the first draw: 25,000 points 2-3 ms, 250,000 12-17 ms,
1,000,000 39-56 ms, across seven GPU launches. For scale, the measurement
section of CLAUDE.md has QGIS drawing ~6,500 plain circles in 100 ms. These
are 2-pixel points from one shader, not symbols. The comparison says that
marks drawn on a WebGL page could stop being the scarce resource; it does
not say what a styled layer would cost.

**WebGPU did not come up in any of the six GPU-enabled configurations
tried.** `requestAdapter()` returned null each time ("Failed to create
WebGPU Context Provider"), with or without `--enable-unsafe-webgpu`,
`--use-webgpu-adapter=compat` or `--use-webgpu-adapter=d3d11`, even where
`chrome://gpu` listed WebGPU as accelerated. WebGL 2 is the GPU route.

**A plugin can turn it on, effective at the next start.** QGIS applies its
per-profile custom environment variables (Options › System › Environment)
before WebEngine initialises. A setup run wrote them with `QgsSettings`:

```python
s.setValue("qgis/customEnvVarsUse", True)
s.setValue("qgis/customEnvVars",
           ["overwrite|QTWEBENGINE_CHROMIUM_FLAGS=--ignore-gpu-blocklist "
            "--use-gl=angle --use-angle=d3d11 --enable-unsafe-webgpu"])
```

The next launch, with nothing in its process environment, started Chromium
with those flags and had a hardware WebGL context. That run used the
GPU-compositing flags, so it proves the mechanism, not a working display; a
real opt-in would write the `--disable-gpu-compositing` set. Three
consequences:

* The flags are process-wide. They change every web view in that QGIS
  profile, including other plugins' views and QGIS's own, so this is a
  setting the user opts into, with a way back, never a silent write.
* `overwrite` replaces any `QTWEBENGINE_CHROMIUM_FLAGS` the user already
  set. QGIS's other modes for this setting were not tried.
* Nothing on the page can detect the blank-view failure: WebGL works, the
  DOM is there and `chrome://gpu` says "accelerated". A configuration check
  needs a screenshot or a pixel read from the composited view, as the probe
  did.

**Two harness traps from these runs.** Launched from Git Bash through
`cmd /c`, every render process died at once with exit code -1073741515
(0xC0000135, `STATUS_DLL_NOT_FOUND`), `chrome://gpu` included. The same
launch from PowerShell worked, and every static import of
`QtWebEngineProcess.exe` resolved, so this was the launcher's environment and
not the install. And a probe that waits on `runJavaScript`'s callback waits
forever when the renderer is dead; poll on a timer and watch
`renderProcessTerminated`.

**A screen grab checks whatever is on top.** The first runs of the probe
from `tools/` grabbed another application's window, which was covering the
view's coordinates: the wrong pixels checked, and a picture of unrelated
work saved to disk. The probe now keeps its view above other windows, and
reports `QWidget.grab` beside the screen grab. In every run above the two
agreed.

Still unmeasured: a discrete or second GPU, a remote-desktop session (where
D3D may not exist at all), macOS and Linux, and whether the plugin's own
pages behave as the probe page did over long interactive use.

The probe is [`tools/webengine_probes/gpu_probe.py`](../tools/webengine_probes/gpu_probe.py),
one configuration per launch:

```
tools\webengine_probes\run_gpu.ps1 -Tag baseline
tools\webengine_probes\run_gpu.ps1 -Tag swcomp -Flags "--use-gl=angle --use-angle=d3d11 --disable-gpu-compositing" -Fps
python tools\webengine_probes\summarise.py $env:TEMP\webengine_probes
```

`-WriteSetting` and `-QgisProfile` test the profile-setting route. Run it
from PowerShell, not Git Bash. It puts a window on screen for a few
seconds per configuration.

## What it does not settle

One machine, one platform. Windows 4.2.1 is measured; macOS and Linux 4.2 are
not, and there is no reason to assume a Qt build behaves identically across
them — the whole reason this file exists is that assuming was wrong twice
already.

`metadata.txt` still declares `qgisMinimumVersion=3.34`. That is now stale
against the scope decision and should become `4.2` when the substrate work
starts. It is deliberately not changed yet: it is the install gate on the
*currently shipped* plugin, and bumping it would stop v3.31 installing for
anyone on an older QGIS who is using it today.

## Note on the probe's own history

Three runs on this machine gave three different answers before the fourth was
clean, and in each case the fault was the probe rather than Qt:

| Run | Reported | Cause |
|---|---|---|
| 1 | `channel=NO modules=yes` | The probe emitted a Python signal on `loadFinished`, before the JS side had constructed the channel and connected to it. Exactly the trap §5 of the architecture notes documents. Fixed with an explicit `ready()` handshake. |
| 2 | `channel=yes modules=NO` | Re-run in the same QGIS process. `STATE.clear()` dropped the Python reference to the scheme handler while the profile still pointed at it, so requests went nowhere and the stage silently never completed — and the verdict counted a check that never ran as a negative. Fixed by keeping handlers in module-level state, reusing an installed one, giving the stage its own timeout, and reporting `DID NOT RUN` separately from `FAIL`. |
| 3 | all green | Clean first run after a QGIS restart. |

Worth recording because it is the argument for the probe existing at all. Each
of those wrong answers would have been believed if the test had been the
one-line `import QWebEngineView` this started as — the first would have talked
us out of a design that works, and the second out of bundling that works.
