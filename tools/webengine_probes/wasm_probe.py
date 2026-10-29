r"""WebAssembly in QGIS's QtWebEngine, over plugin-style custom schemes.

Not a console probe: QGIS runs this at startup (`--code`) in a THROWAWAY
profile and it quits QGIS hard when done. Launch it with run_wasm.ps1, which
builds the page and waits for the result; results are recorded in
docs/webengine-support.md, "WebAssembly on a plugin page".

The same page is loaded over three schemes:

  wpbare   the flags equis:// registers today (Secure|LocalAccess|Cors)
  wpfetch  + FetchApiAllowed, without which fetch() on a custom scheme is
           refused, and with it instantiateStreaming and wasm-bindgen's init()
  wpcoi    + COOP/COEP response headers, to see whether a custom-scheme page
           can become cross-origin isolated (threads, SharedArrayBuffer)

Environment (the working directory must be the repository root: --code runs
this with no __file__):

  PROBE_PARQUET      one parquet partition, timed in the page's DuckDB-WASM
                     and in QGIS's own Python DuckDB with the same SQL
  PROBE_OUT          where results.json, log.txt and the generated page go
  PROBE_KRIGING_PKG  a wasm-bindgen --target web build of kriging-rs
                     (default ../kriging-rs/npm/kriging-rs-wasm/pkg); the
                     kriging stages report a failure without it
  PROBE_ANALYTE, PROBE_UNIT   kriging input (default Nitrate, ug/L)
"""
import base64
import json
import os
import time
import traceback

from qgis.PyQt.QtCore import QBuffer, QByteArray, QTimer, QUrl
from qgis.PyQt.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile,
                                       QWebEngineUrlRequestJob,
                                       QWebEngineUrlScheme,
                                       QWebEngineUrlSchemeHandler)
from qgis.PyQt.QtWebEngineWidgets import QWebEngineView

REPO = os.getcwd()
SOURCE = os.path.join(REPO, "tools", "webengine_probes")
OUT_DIR = os.environ["PROBE_OUT"]
PAGE = os.path.join(OUT_DIR, "page")
PARQUET = os.environ["PROBE_PARQUET"]
ANALYTE = os.environ.get("PROBE_ANALYTE", "Nitrate")
UNIT = os.environ.get("PROBE_UNIT", "ug/L")
KRIGING = os.environ.get("PROBE_KRIGING_PKG") or os.path.join(
    os.path.dirname(REPO), "kriging-rs", "npm", "kriging-rs-wasm", "pkg")
# Served under these prefixes; everything else from PAGE, then SOURCE.
MOUNTS = {
    "duckdb/": os.path.join(REPO, "web", "node_modules", "@duckdb", "duckdb-wasm", "dist"),
    "kriging/": KRIGING,
}
MIME = {".html": b"text/html", ".js": b"text/javascript", ".mjs": b"text/javascript",
        ".json": b"application/json", ".wasm": b"application/wasm"}
SCHEMES = ("wpbare", "wpfetch", "wpcoi")

os.makedirs(PAGE, exist_ok=True)
LOG = open(os.path.join(OUT_DIR, "log.txt"), "w", encoding="utf-8")
T0 = time.monotonic()
OUT = {"python": {}, "pages": {}, "qt": {}, "requests": {}}
KEEP = {}


def say(msg):
    LOG.write("%7.1f  %s\n" % (time.monotonic() - T0, msg))
    LOG.flush()


QUERIES = {
    "count": "select count(*) from {src}",
    "by_analyte": "select CHEMICAL_NAME, REPORT_RESULT_UNIT, count(*) n, max(RESULT_NUMERIC) mx "
                  "from {src} group by all",
    # Detects only: a demonstration input for the kernel, not a map anyone
    # should read. Kriging non-detects needs the plugin's censoring rules.
    "points": "select SYS_LOC_CODE, avg(LATITUDE) lat, avg(LONGITUDE) lon, max(RESULT_NUMERIC) v "
              "from {src} where CHEMICAL_NAME = '%s' and DETECT_FLAG = 'Y' "
              "and REPORT_RESULT_UNIT = '%s' and LATITUDE is not null and RESULT_NUMERIC > 0 "
              "group by 1" % (ANALYTE.replace("'", "''"), UNIT.replace("'", "''")),
    "to_table": "create or replace table r as select * from {src}",
    "by_analyte_table": "select CHEMICAL_NAME, REPORT_RESULT_UNIT, count(*) n, max(RESULT_NUMERIC) mx "
                        "from r group by all",
}

# add(i32, i32) -> i32, hand-assembled: the smallest module that proves a load.
ADD_HEX = ("0061736d01000000" "0107016002" "7f7f017f" "03020100"
           "070701036164640000" "0a09010700200020016a0b")


def python_side():
    """The same SQL in QGIS's DuckDB, and the files the page needs."""
    import duckdb
    OUT["python"]["duckdb"] = duckdb.__version__
    con = duckdb.connect()
    OUT["python"]["threads"] = con.sql("select current_setting('threads')").fetchone()[0]
    src = "'%s'" % PARQUET.replace("\\", "/").replace("'", "''")
    for name, sql in QUERIES.items():
        for p in ("cold", "warm"):
            t = time.perf_counter()
            res = con.execute(sql.format(src=src)).fetchall()
            OUT["python"]["q_%s_%s" % (name, p)] = {"ms": round((time.perf_counter() - t) * 1000, 1),
                                                    "rows": len(res)}
    rows = con.execute(QUERIES["points"].format(src=src)).fetchall()
    points = {"lat": [r[1] for r in rows], "lon": [r[2] for r in rows], "v": [r[3] for r in rows]}
    with open(os.path.join(PAGE, "points.json"), "w") as f:
        json.dump(points, f)                       # for node_kriging.mjs
    page_q = {k: v.format(src="'ws_n.parquet'") for k, v in QUERIES.items()}
    with open(os.path.join(PAGE, "index.html"), "w", encoding="utf-8") as f:
        f.write('<!doctype html><meta charset="utf-8"><title>wasm probe</title>'
                '<script type="application/json" id="queries">%s</script>'
                '<script type="application/json" id="points">%s</script>'
                '<script type="module" src="probe.bundle.js"></script>'
                % (json.dumps(page_q), json.dumps(points)))
    # The same bytes carried inside ES modules: module imports reach a scheme
    # handler even when fetch() is refused (equis:// as registered today).
    with open(os.path.join(PAGE, "add.wasm"), "wb") as f:
        f.write(bytes.fromhex(ADD_HEX))
    with open(os.path.join(PAGE, "addwasm.js"), "w") as f:
        f.write('export default "%s";' % base64.b64encode(bytes.fromhex(ADD_HEX)).decode())
    krig = os.path.join(KRIGING, "kriging_rs_bg.wasm")
    if os.path.isfile(krig):
        with open(krig, "rb") as src_file, open(os.path.join(PAGE, "krigwasm.js"), "w") as f:
            f.write('export default "%s";' % base64.b64encode(src_file.read()).decode())
    else:
        say("no kriging package at %s; the kriging stages will fail" % KRIGING)


def locate(path):
    """The file a page path names, confined to the roots it may read."""
    if path == "data/ws_n.parquet":
        return PARQUET
    roots = [(prefix, root) for prefix, root in MOUNTS.items() if path.startswith(prefix)]
    candidates = ([(root, path[len(prefix):]) for prefix, root in roots]
                  or [(PAGE, path), (SOURCE, path)])
    for root, rel in candidates:
        root = os.path.abspath(root)
        target = os.path.abspath(os.path.join(root, rel))
        try:
            inside = os.path.commonpath([root, target]) == root
        except ValueError:                  # different drives on Windows
            inside = False
        if inside and os.path.isfile(target):
            return target
    return None


class Handler(QWebEngineUrlSchemeHandler):
    def __init__(self, name, coi):
        super().__init__()
        self.name, self.coi = name, coi
        self.log = OUT["requests"].setdefault(name, [])

    def requestStarted(self, job):
        try:
            path = job.requestUrl().path().lstrip("/")
            target = locate(path)
            if target is None:
                self.log.append([path, "404"])
                job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
                return
            if self.coi:
                hdrs = {QByteArray(b"Cross-Origin-Opener-Policy"): QByteArray(b"same-origin"),
                        QByteArray(b"Cross-Origin-Embedder-Policy"): QByteArray(b"require-corp"),
                        QByteArray(b"Cross-Origin-Resource-Policy"): QByteArray(b"same-origin")}
                try:
                    job.setAdditionalResponseHeaders(hdrs)
                except Exception as exc:  # noqa: BLE001 - recorded, not fatal
                    OUT["qt"]["setAdditionalResponseHeaders_error"] = repr(exc)
            t = time.perf_counter()
            with open(target, "rb") as fh:
                body = fh.read()
            # Parented to the job, or it is collected and the page gets nothing.
            buf = QBuffer(job)
            buf.setData(QByteArray(body))
            buf.open(QBuffer.OpenModeFlag.ReadOnly)
            job.reply(QByteArray(MIME.get(os.path.splitext(target)[1], b"application/octet-stream")), buf)
            self.log.append([path, len(body), round((time.perf_counter() - t) * 1000, 1)])
        except Exception:  # noqa: BLE001 - a probe records, it does not stop
            self.log.append([job.requestUrl().path(), "EXC " + traceback.format_exc()])


class Page(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):
        say("console[%s] %s (%s:%s)" % (level, message[:400], source, line))


def register():
    F = QWebEngineUrlScheme.Flag
    OUT["qt"]["FetchApiAllowed_exists"] = hasattr(F, "FetchApiAllowed")
    OUT["qt"]["setAdditionalResponseHeaders_exists"] = hasattr(
        QWebEngineUrlRequestJob, "setAdditionalResponseHeaders")
    base = F.SecureScheme | F.LocalAccessAllowed | F.CorsEnabled
    fetch = base | F.FetchApiAllowed if hasattr(F, "FetchApiAllowed") else base
    for name, flags, coi in zip(SCHEMES, (base, fetch, fetch), (False, False, True)):
        s = QWebEngineUrlScheme(QByteArray(name.encode()))
        s.setFlags(flags)
        s.setSyntax(QWebEngineUrlScheme.Syntax.Host)
        QWebEngineUrlScheme.registerScheme(s)
        KEEP[name] = Handler(name, coi)
    # Only after ALL are registered. The first defaultProfile() call freezes
    # the scheme registry: in the first run (2026-10-27) the schemes
    # registered after it came out hostless and not secure, fetch() refused
    # whatever their flags, and nothing reported it.
    for name in SCHEMES:
        QWebEngineProfile.defaultProfile().installUrlSchemeHandler(QByteArray(name.encode()), KEEP[name])


def run_mode(i):
    if i >= len(SCHEMES):
        return gpu_page()
    mode = SCHEMES[i]
    view = KEEP["view"]
    say("loading %s" % mode)
    view.load(QUrl("%s://app/index.html?mode=%s" % (mode, mode)))
    state = {"t0": time.monotonic(), "done": False}

    def got(res):
        if not state["done"] and res and res != "null":
            state["done"] = True
            OUT["pages"][mode] = json.loads(res)
            say("%s done" % mode)
            QTimer.singleShot(500, lambda: run_mode(i + 1))

    # Polled on a timer, not chained on the callback: a dead renderer never
    # calls back, and a chain would wait forever.
    def tick():
        if state["done"]:
            return
        if time.monotonic() - state["t0"] > 240:
            state["done"] = True
            OUT["pages"][mode] = {"fatal": "page never reported in 240 s"}
            say("%s timed out" % mode)
            return QTimer.singleShot(500, lambda: run_mode(i + 1))
        view.page().runJavaScript("JSON.stringify(window.__probe || null)", 0, got)
        QTimer.singleShot(500, tick)
    QTimer.singleShot(1000, tick)


def gpu_page():
    """chrome://gpu, for the flags Chromium was actually started with."""
    view = KEEP["view"]
    view.load(QUrl("chrome://gpu"))

    def got(text):
        OUT["chrome_gpu"] = text
        finish()
    QTimer.singleShot(5000, lambda: view.page().runJavaScript(
        "(function(){var b=document.querySelector('info-view');"
        "var r=b&&b.shadowRoot?b.shadowRoot.textContent:document.body.innerText;"
        "return (r||'').replace(/\\s+/g,' ').slice(0,6000);})()", 0, got))


def finish():
    with open(os.path.join(OUT_DIR, "results.json"), "w") as f:
        json.dump(OUT, f, indent=1)
    say("wrote results.json")
    LOG.close()
    QTimer.singleShot(300, lambda: os._exit(0))


def start():
    try:
        if not os.path.isfile(os.path.join(PAGE, "probe.bundle.js")):
            raise RuntimeError("no %s; run build_wasm_page.mjs first (run_wasm.ps1 does)"
                               % os.path.join(PAGE, "probe.bundle.js"))
        say("python side")
        python_side()
        say("register")
        register()
        view = QWebEngineView()
        view.setPage(Page(QWebEngineProfile.defaultProfile(), view))
        view.resize(800, 600)
        view.show()          # shown: Chromium fires no animation frames for a hidden view
        KEEP["view"] = view
        run_mode(0)
    except Exception:  # noqa: BLE001
        say(traceback.format_exc())
        finish()


QTimer.singleShot(15 * 60 * 1000, lambda: (say("watchdog"), finish()))
QTimer.singleShot(3000, start)
