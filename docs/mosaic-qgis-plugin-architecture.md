# Mosaic + Qt WebView QGIS Plugin — Architecture Notes

**Status:** design sketch, September 2026
**Scope:** 2D linked charts (Mosaic vgplot) inside a QGIS dock widget, backed by a DuckDB instance the plugin owns, with two-way selection linking to the QGIS map canvas. No 3D, no DuckDB-WASM.

---

## 1. The one-paragraph version

Mosaic separates *rendering* from *computation*: chart components publish SQL queries to a JS coordinator, and the coordinator forwards them to whatever database connector it's given. That split lets us keep DuckDB in the QGIS Python process — where the layers already are — and run only the coordinator and Observable Plot inside a `QWebEngineView`. Python and JS talk over `QWebChannel`. A Mosaic `Selection` (a shared SQL predicate) becomes the single object that links a brushed histogram to `layer.selectByIds()` and vice-versa.

---

## 2. Components

```
┌───────────────────────────── QGIS process ─────────────────────────────┐
│                                                                        │
│  ┌── Plugin (Python) ──────────────────┐   ┌── QWebEngineView ───────┐ │
│  │                                     │   │  (Chromium)             │ │
│  │  DuckDB connection                  │   │                         │ │
│  │   ├─ spatial extension              │   │  vgplot / mosaic-core   │ │
│  │   ├─ registered QGIS layers (Arrow) │   │   ├─ Coordinator        │ │
│  │   └─ cube tables (Mosaic-managed)   │   │   ├─ Selection objects  │ │
│  │                                     │   │   ├─ Plot marks (SVG)   │ │
│  │  Bridge (QObject on QWebChannel)    │◄──┤   └─ QtChannelConnector │ │
│  │   ├─ query(sql, type) -> Arrow IPC  │   │                         │ │
│  │   ├─ selectionChanged(predicate)    │   │  QWebChannel transport  │ │
│  │   └─ signal: canvasSelection(fids)  │──►│                         │ │
│  │                                     │   └─────────────────────────┘ │
│  │  Layer sync                         │                               │
│  │   ├─ layer -> DuckDB (on load/edit) │   ┌── Map canvas ───────────┐ │
│  │   └─ selectByIds / selectionChanged │◄─►│  QgsVectorLayer          │ │
│  └─────────────────────────────────────┘   └─────────────────────────┘ │
│                                                                        │
│  Asset serving: QWebEngineUrlSchemeHandler  (app://index.html, app://…)│
└────────────────────────────────────────────────────────────────────────┘
```

### 2.1 Python side (plugin)

| Piece | Responsibility | Notes |
|---|---|---|
| `DuckDBHost` | Owns the connection; loads `spatial`; registers layers | One connection, one thread. DuckDB is not thread-safe across cursors without care — see §5. |
| `LayerSync` | Mirrors selected `QgsVectorLayer`s into DuckDB tables | Via `pyarrow` (feature iterator → RecordBatch) or direct file read for GPKG/Parquet/PostGIS. Keep `fid` as a column. |
| `Bridge(QObject)` | Exposed on `QWebChannel` as `bridge` | Slots: `query(sql, type) -> str`, `setSelection(name, predicate)`. Signals: `canvasSelection(str)`. |
| `SchemeHandler` | Serves the bundled web app under a custom scheme | Needed so ES modules load; `file://` breaks them. Set `Content-Type` correctly. |
| `DockWidget` | `QgsDockWidget` holding the `QWebEngineView` | Nothing else; UI lives in the web app. |

### 2.2 JS side (bundled web app)

| Piece | Responsibility | Notes |
|---|---|---|
| `QtChannelConnector` | Mosaic database connector over `QWebChannel` | Implements `query({type, sql})`. ~25 lines. |
| `Coordinator` | Stock `mosaic-core` coordinator with the connector installed | Cache and cube optimization work unchanged. |
| Selections | `Selection.crossfilter()` for chart-to-chart; a dedicated `$canvas` selection for map linkage | Predicate objects, not row arrays. |
| `CanvasLinkClient` | A minimal `MosaicClient` subscribed to a selection; forwards its predicate to Python | This is the QGIS integration point. |
| vgplot spec | Charts, inputs, tables | Authored in JS or as a `mosaic-spec` JSON/YAML doc. |

---

## 3. Data flow

### 3.1 Query path (chart render or filter update)

1. A vgplot mark computes its query (e.g. binned count with the current filter predicate appended).
2. Coordinator dedupes/caches; calls `connector.query({type: 'arrow', sql})`.
3. `QtChannelConnector` invokes `bridge.query(sql, 'arrow')` via QWebChannel (async, callback-based).
4. Python executes on DuckDB, serializes the result with `pyarrow.ipc` (stream format), base64-encodes, returns.
5. JS decodes base64 → `Uint8Array` → hands to the coordinator, which parses with `flechette`.
6. Mark re-renders.

QWebChannel is JSON-only, hence base64. For typical aggregate results (hundreds to tens of thousands of rows) the overhead is negligible. Raw-row table views over millions of rows are the case that hurts — paginate them (Mosaic's table client already does load-on-scroll).

### 3.2 Chart → map selection

1. User brushes a histogram; the `intervalX` interactor writes a clause into `$canvas`.
2. `CanvasLinkClient.query()` is called with the resolved predicate (Mosaic passes it as a SQL expression).
3. Client calls `bridge.setSelection('samples', predicate)`.
4. Python runs `SELECT fid FROM samples WHERE <predicate>` and calls `layer.selectByIds(fids)`.
5. Guard with a re-entrancy flag so step 4 doesn't trigger §3.3.

### 3.3 Map → chart selection

1. `layer.selectionChanged` fires with selected FIDs.
2. Python emits `bridge.canvasSelection(json_fids)`.
3. JS handler writes a clause into `$canvas`: `fid IN (…)` for small sets; for large sets, Python instead writes a `_canvas_sel` temp table and the clause becomes `fid IN (SELECT fid FROM _canvas_sel)`.
4. Every chart filtered by `$canvas` re-queries via §3.1.

### 3.4 Layer lifecycle

- On plugin activation: enumerate vector layers; user picks which to mirror.
- On mirror: `CREATE OR REPLACE TABLE <name> AS SELECT * FROM arrow_scan(...)` (or read the source file directly when it's GPKG/Parquet — faster, and geometry comes through `spatial`).
- On `layer.editingStopped` / `dataChanged`: re-mirror (cheap for site-scale tables) and call `coordinator.clear()` in JS so cached results and cube tables are dropped.

---

## 4. Code sketches

### 4.1 Python bridge

```python
import base64, json
import duckdb, pyarrow as pa
from qgis.PyQt.QtCore import QObject, pyqtSlot, pyqtSignal
from qgis.core import QgsProject

class Bridge(QObject):
    canvasSelection = pyqtSignal(str)   # JSON list of fids

    def __init__(self, con: duckdb.DuckDBPyConnection):
        super().__init__()
        self.con = con
        self._suppress = False

    @pyqtSlot(str, str, result=str)
    def query(self, sql: str, kind: str) -> str:
        if kind == "exec":
            self.con.execute(sql)
            return ""
        tbl = self.con.execute(sql).arrow()
        if kind == "json":
            return json.dumps(tbl.to_pylist(), default=str)
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, tbl.schema) as w:
            w.write_table(tbl)
        return base64.b64encode(sink.getvalue().to_pybytes()).decode()

    @pyqtSlot(str, str)
    def setSelection(self, layer_name: str, predicate: str):
        layer = QgsProject.instance().mapLayersByName(layer_name)[0]
        where = predicate or "TRUE"
        fids = [r[0] for r in self.con.execute(
            f"SELECT fid FROM {layer_name} WHERE {where}").fetchall()]
        self._suppress = True
        try:
            layer.selectByIds(fids)
        finally:
            self._suppress = False

    def on_layer_selection_changed(self, selected, deselected, clear):
        if self._suppress:
            return
        self.canvasSelection.emit(json.dumps(list(selected)))
```

Wiring:

```python
from qgis.PyQt.QtWebChannel import QWebChannel
from qgis.PyQt.QtWebEngineWidgets import QWebEngineView

view = QWebEngineView()
channel = QWebChannel(view.page())
bridge = Bridge(con)
channel.registerObject("bridge", bridge)
view.page().setWebChannel(channel)
view.load(QUrl("app://index.html"))
```

### 4.2 JS connector

```js
import { Coordinator, createAPIContext, decodeIPC } from "@uwdata/vgplot";

function qtChannelConnector(bridge) {
  return {
    async query({ type = "json", sql }) {
      const raw = await new Promise(res => bridge.query(sql, type, res));
      if (type === "exec") return;
      if (type === "json") return JSON.parse(raw);
      const bytes = Uint8Array.from(atob(raw), c => c.charCodeAt(0));
      return decodeIPC(bytes);      // flechette table
    }
  };
}

new QWebChannel(qt.webChannelTransport, ({ objects: { bridge } }) => {
  const coordinator = new Coordinator(qtChannelConnector(bridge));
  const vg = createAPIContext({ coordinator });
  window.vg = vg;
  window.bridge = bridge;
  buildDashboard(vg, bridge);
});
```

(`decodeIPC` is exported from `mosaic-core`; check the exact name against the installed version — the Arrow decoding path moved from `apache-arrow` to `flechette` in 0.20.)

### 4.3 Canvas link client

```js
import { MosaicClient } from "@uwdata/mosaic-core";

class CanvasLinkClient extends MosaicClient {
  constructor(selection, bridge, layerName) {
    super(selection);
    this.bridge = bridge;
    this.layerName = layerName;
  }
  query(filter) {
    const pred = filter ? String(filter) : "";
    this.bridge.setSelection(this.layerName, pred);
    return null;                 // nothing to fetch
  }
}
```

Map → chart:

```js
const $canvas = vg.Selection.single();
bridge.canvasSelection.connect(json => {
  const fids = JSON.parse(json);
  $canvas.update({
    source: "canvas",
    value: fids,
    predicate: fids.length ? `fid IN (${fids.join(",")})` : null
  });
});
coordinator.connect(new CanvasLinkClient($canvas, bridge, "samples"));
```

---

## 5. Process and threading

- **All DuckDB calls on the Qt main thread.** QWebChannel slots run on the main thread anyway. A query over a few million rows is single-digit ms; a cube build (first brush on a fresh chart) might be a few hundred ms and will freeze the UI for that long. Acceptable initially; if not, move execution to a `QThread` with a queue and have `query()` return via a signal instead of a return value.
- **One connection.** Don't share a `duckdb` connection across threads. If you add a worker thread, give it its own `con.cursor()`.
- **`loadFinished` ≠ app ready.** Fire an explicit `bridge.ready()` from JS once the coordinator is constructed; don't rely on `QWebEngineView.loadFinished`. (DataPlotly hit exactly this on Qt6.)

---

## 6. Compatibility matrix

| Environment | WebView | Verdict |
|---|---|---|
| QGIS 3.x / Qt5 / `QtWebKitWidgets` | 2016 WebKit | **No.** No BigInt, no ES modules — `flechette` and vgplot won't load. |
| QGIS 3.x / Qt5 / `QtWebEngineWidgets` | Chromium 87-ish | Works if the bindings are present and importable after QGIS starts. Varies by build; Linux distro packages are the reliable case. |
| QGIS 4.x / Qt6 / Linux | Chromium 11x | Works with `python3-pyqt6.qtwebengine` installed. |
| QGIS 4.x / Qt6 / Windows OSGeo4W | Chromium 11x | **Broken as of 4.0**: DLLs ship, PyQt6 bindings don't (qgis/QGIS#65398). Watch the 4.2 LTR (Oct 2026). |
| QGIS 4.x / Qt6 / macOS | — | Unverified. |

> **Measured 2026-09-18** (added after this sketch was written; full record
> in [webengine-support.md](webengine-support.md), probe in
> `tools/test_webengine.py`). The Windows row above is **out of date — the 4.2
> LTR fixed it**:
>
> ```
> Windows | QGIS 4.2.1-Belém do Pará | Qt 6.11.0 / PyQt 6.11.0 |
> Python 3.12.13 | Chromium 140.0.0.0 | webengine=yes channel=yes modules=yes
> ```
>
> All three confirmed on a clean run: the view constructs and renders,
> `QWebChannel` round-trips both ways including a slot with a return value
> (the shape §4's `query(sql, type)` needs), and — **answering open question
> 4** — a custom URL scheme registered from inside a plugin, long after QGIS
> started, served an ES-module app whose import graph resolved. Qt documents
> that registration as needing to happen before the application exists, which
> a plugin cannot do; it works anyway.
>
> So §4 can be implemented as written, and the Vite bundle of open question 4
> ships inside the plugin served from a custom scheme. The `restConnector`
> fallback in §6 stays a contingency for builds that fail the scheme stage,
> not the expected route. Caveat: one machine, one build — nothing between
> QGIS 3.34 and 4.2 has been measured.

Since no WASM or WebGL is needed, the GPU/`SharedArrayBuffer` problems that block Qgis2threejs don't apply here. The only hard requirement is a modern JS engine plus `QWebChannel`. (Measured 2026-10-27/29, QGIS 4.2.1 and 4.2.3 on Windows. By default Chromium starts with `--disable-gpu`, so there is no WebGL. `QTWEBENGINE_CHROMIUM_FLAGS="--use-gl=angle --use-angle=d3d11 --disable-gpu-compositing"`, set before QGIS starts, gives hardware WebGL 2 that displays, but no WebGPU. A custom-scheme page cannot become cross-origin isolated, so there are no WASM threads; single-threaded WASM works. See [webengine-support.md](webengine-support.md#webassembly-on-a-plugin-page).)

**Fallback:** the connector interface is the seam. Swap `qtChannelConnector` for Mosaic's stock `restConnector("http://127.0.0.1:PORT")`, run a small HTTP server thread in the plugin (Mosaic ships a Python `duckdb-server` package that speaks its protocol), and open the same page in the system browser. Map linkage then goes over the same HTTP server. The JS dashboard code doesn't change.

---

## 7. Precedents worth reading

- **QDuckDB** (Oslandia) — DuckDB as a QGIS vector data provider, implemented as a plugin. Shows the DuckDB-inside-QGIS packaging and the spatial-extension loading. https://gitlab.com/Oslandia/qgis/qduckdb
- **DataPlotly** — Plotly in a dock widget and in print layouts; its Qt6 migration PR (#416) documents the QtWebKit → QtWebEngine pitfalls (parenting, `loadFinished` timing, offscreen render). https://github.com/ghtmtt/DataPlotly
- **Qgis2threejs** — WebEngine preview inside QGIS; its wiki page on WebEngine support status is the best running log of which QGIS builds have working `QWebEngineView`. https://github.com/minorua/Qgis2threejs/wiki
- **`boro`** (PyPI) — Mosaic clients as anywidgets: JS coordinator, Python DuckDB, Arrow IPC over a comm. Structurally identical to this design with a different transport; read its Python side for the serialization pattern. https://pypi.org/project/boro/
- **`mosaic-widget`** — the official Jupyter widget, same idea. https://idl.uw.edu/mosaic/jupyter/
- **Mosaic connector docs** — the `query({type, sql})` contract and `Coordinator.databaseConnector`. https://idl.uw.edu/mosaic/core/#data-source
- **Qt WebEngine Markdown Editor example** — canonical `QWebChannel` two-way pattern. https://doc.qt.io/qt-6/qtwebengine-webenginewidgets-markdowneditor-example.html

---

## 8. Open questions

1. **Geometry in DuckDB vs. in QGIS.** Mirror geometry into DuckDB (enables spatial predicates in Mosaic selections, e.g. brush a map extent → filter charts) or keep DuckDB attribute-only and let QGIS own spatial selection? Start attribute-only; add `ST_*` once a chart needs it.
2. **Cube tables and mirrored layers.** Mosaic materializes cube tables as real DuckDB tables. Namespace them (Mosaic uses a `cube_` prefix by default) so `LayerSync` doesn't try to treat them as layers.
3. **Large canvas selections.** Threshold for switching from `fid IN (...)` to the temp-table join. Probably ~1,000 fids.
4. **Bundling.** Vite build of vgplot + app into a single ES bundle checked into the plugin; no runtime npm. Size is ~1 MB gzipped; fine.
5. **Dual QGIS 3/4 support.** Import `QtWebEngineWidgets` through `qgis.PyQt` and feature-detect at plugin load; degrade to a "open in browser" button using the REST fallback when the view isn't available.
