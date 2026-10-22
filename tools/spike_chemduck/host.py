r"""Spike host: ChemDuck in the plugin's DuckDB, Chemrooms' code on a plugin page.

    node tools/spike_chemduck/build.mjs <work>/page
    python tools/spike_chemduck/host.py --manifest <facility>/manifest.json \
        --chemduck ../chemduck --work <work>            # in Chromium, no QGIS

The QGIS half is tools/spike_chemduck/qgis_run.py, which uses open_chemduck()
and check() from here with the real QWebChannel instead of the shim below.

What it sets up, as the plugin would:
  1. the plugin's own ReportStore on the manifest (schema `main`);
  2. ChemDuck's hybrid ARII source - sidecars published once, then ChemDuck's
     SQL and a `results` view over the same partitions - in its OWN schema,
     `chemduck`, of that same database. Opened in `main` it replaces the
     plugin's `results` view and the plugin's next query fails (found
     2026-10-22);
  3. a cursor whose default schema is `chemduck`, handed to the page's bridge,
     so Chemrooms' unqualified names (locations, samples, aggregate_results)
     resolve there and the plugin's do not move.

Then it checks what the page computed: against the same SQL run directly
(the bridge must be lossless) and against the plugin's ReportStore (the
differences docs/chemduck-parity.md explains).
"""
import argparse
import base64
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from equis_loader import bridge_payload  # noqa: E402
from equis_loader import report_data as rd  # noqa: E402


def open_chemduck(store, manifest, chemduck, work):
    """ChemDuck beside the plugin: its own schema, its own cursor. Returns
    (cursor, timings)."""
    sys.path.insert(0, os.path.join(chemduck, "tools"))
    import arii_hybrid as hy
    timings = {}
    pub = os.path.join(work, "pub")
    if not os.path.exists(os.path.join(pub, "sidecars.json")):
        began = time.time()
        # WGS84 named, never guessed: ChemDuck's publisher refuses to infer it.
        hy.publish(manifest, pub, x_col="LONGITUDE", y_col="LATITUDE", quiet=True)
        timings["publish_s"] = round(time.time() - began, 1)
    root = os.path.dirname(os.path.abspath(manifest))
    with open(manifest, encoding="utf-8") as handle:
        parts = [os.path.join(root, p["path"]) for p in json.load(handle)["partitions"]]
    cur = store.con.cursor()
    began = time.time()
    # ChemDuck's views build point geometries, so it needs DuckDB's spatial
    # extension - 55.5 MB on Windows, and not installed in QGIS's own DuckDB
    # (found 2026-10-22). SPIKE_EXT_DIR keeps the spike's copy out of the
    # user's ~/.duckdb.
    if os.environ.get("SPIKE_EXT_DIR"):
        cur.execute("SET extension_directory = '%s'"
                    % os.environ["SPIKE_EXT_DIR"].replace("\\", "/"))
    began_ext = time.time()
    cur.execute("INSTALL spatial")
    cur.execute("LOAD spatial")
    timings["spatial_s"] = round(time.time() - began_ext, 1)
    cur.execute("CREATE SCHEMA IF NOT EXISTS chemduck")
    cur.execute("USE chemduck")
    hy.open_consumer(cur, pub, parts)
    timings["open_s"] = round(time.time() - began, 2)
    return cur, timings


def envelope(cursor, sql, kind):
    """What WebBridge.query returns, from the same bridge_payload.answer."""
    try:
        data = bridge_payload.answer(cursor, sql, kind or "arrow")
        return json.dumps({"ok": True, "kind": kind, "data": data})
    except Exception as exc:  # noqa: BLE001 - the page needs the message
        return json.dumps({"ok": False, "kind": kind, "error": str(exc)})


def check(store, cursor, spike):
    """The page's answers, held to direct SQL and to the ReportStore."""
    report = {"page_ok": spike.get("ok"), "init": spike.get("init"),
              "layer_ms": (spike.get("layer") or {}).get("ms"),
              "queries": spike.get("queries")}
    if not spike.get("ok"):
        report["error"] = spike.get("error")
        return report
    sql = spike["layer"]["sql"]
    direct = cursor.execute(sql).fetchall()
    names = [d[0] for d in cursor.description]
    direct = {r["location_id"]: r for r in (dict(zip(names, row)) for row in direct)}
    page = {a["id"]: a for a in spike["answers"]}
    same = 0
    for key, d in direct.items():
        a = page.get(key)
        if (a is not None and _close(a["result"], d["result"])
                and bool(a["detected"]) == bool(d["detected"])):
            same += 1
    report["bridge_lossless"] = {"rows_page": len(page), "rows_direct": len(direct),
                                 "identical": same}
    # Against the plugin: per location, the latest of ChemDuck's depth
    # intervals (the recipe is most recent), as the parity script rolls up.
    recipe = spike["layer"]["recipe"]
    how = rd.MAXIMUM if recipe["eventAgg"] == "maximum" else rd.MOST_RECENT
    plugin = store.aggregate(recipe["coloringAnalyte"], how=how,
                             non_detect=rd.ND_HALF, matrix=[recipe["matrixFilter"]],
                             fraction={"Total": "T", "Dissolved": "D"}[recipe["fractionFilter"]])
    by_loc = {}
    for d in direct.values():
        loc = d["location_id"].split("|")[0]
        best = by_loc.get(loc)
        if how == rd.MAXIMUM:
            key = lambda r: (bool(r["detected"]), r["result"] or 0)
        else:
            key = lambda r: str(r["rep_sample_date"])
        if best is None or key(d) > key(best):
            by_loc[loc] = d
    agree = sum(1 for loc, d in by_loc.items()
                if loc in plugin and _close(plugin[loc]["value"], d["result"]))
    report["vs_report_store"] = {"locations_chemduck": len(by_loc),
                                 "locations_plugin": len(plugin), "same_value": agree}
    return report


def _close(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= 1e-9 * max(abs(float(a)), abs(float(b)), 1.0)


SHIM = r"""
window.qt = {webChannelTransport: {}};
window.QWebChannel = function (transport, connected) {
  const post = (path, body) => fetch(path, {method: "POST", body: JSON.stringify(body)});
  const bridge = {
    query(sql, kind, done) { post("/query", {sql, kind}).then((r) => r.text()).then(done); },
    log(level, message) { post("/log", {level, message}); },
    ready() { post("/ready", {}); },
  };
  setTimeout(() => connected({objects: {bridge}}), 0);
};
"""


def serve(page_dir, cursor, port):
    state = {"spike": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            name = self.path.split("?")[0].lstrip("/") or "index.html"
            path = os.path.join(page_dir, name)
            if not os.path.isfile(path):
                self.send_error(404)
                return
            body = open(path, "rb").read()
            if name == "index.html":
                body = body.replace(b'<script src="qrc:///qtwebchannel/qwebchannel.js"></script>',
                                    b"<script>" + SHIM.encode() + b"</script>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html" if name.endswith(".html")
                             else "text/javascript")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            reply = b""
            if self.path == "/query":
                reply = envelope(cursor, payload["sql"], payload["kind"]).encode()
            elif self.path == "/log" and payload["message"].startswith("SPIKE "):
                state["spike"] = json.loads(payload["message"][6:])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(reply)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


def main():
    from equis_loader.store import ReportStore
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--chemduck", default=os.path.join(ROOT, "..", "..", "chemduck"))
    ap.add_argument("--work", required=True)
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--recipe", default="", help='JSON, e.g. {"eventAgg":"maximum","dupAgg":"max"}')
    args = ap.parse_args()
    store = ReportStore.from_manifest(os.path.dirname(os.path.abspath(args.manifest)))
    cursor, timings = open_chemduck(store, args.manifest, args.chemduck, args.work)
    # The plugin's own queries still answer, from `main`.
    plugin_ok = len(store.aggregate("Arsenic", matrix=["SO"], fraction="T"))
    server, state = serve(os.path.join(args.work, "page"), cursor, args.port)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto("http://127.0.0.1:%d/%s" % (args.port, "#" + args.recipe if args.recipe else ""))
        page.wait_for_function("window.__spike && window.__spike.ok !== undefined", timeout=180000)
        browser.close()
    server.shutdown()
    result = check(store, cursor, state["spike"] or {})
    result["chemduck_open"] = timings
    result["plugin_still_answers"] = plugin_ok
    print(json.dumps(result, indent=1, default=str))
    with open(os.path.join(args.work, "browser_result.json"), "w", encoding="utf-8") as h:
        json.dump(result, h, indent=1, default=str)


if __name__ == "__main__":
    main()
