r"""The Analysis tab in an ordinary browser, from the real parquet. NO QGIS.

    python tools/analysis_preview.py                  # serve; open the URL printed
    python tools/analysis_preview.py --shots DIR      # a scripted session, screenshotted
    python tools/analysis_preview.py --matrix WS --x Manganese --y Lead

WHAT IT IS. The page the plugin ships (equis_loader/web, the built bundle)
and the host's own decisions (equis_loader/analysis.py), with a stand-in
for the one thing only Qt provides: the QWebChannel. The shim this serves
in place of qrc:///qtwebchannel/qwebchannel.js gives the page a `bridge`
whose query() runs the SQL through bridge_payload.answer - the function
web_bridge.py calls - and whose chosen()/ready() run Session.respond and
replay what it sends, as linked_views.py does with runJavaScript. So what
is seen here is the chooser and scatter as built, not a mock-up of them.

WHAT IT IS NOT. The map: the scatter map layer is QGIS's. A choice that
would build it prints its stations here instead. And the event loop: Qt
shows each "working" message before the step it announces; here they
arrive together with the answer, after it.

Written 2026-10-10 so the chooser could be seen with no QGIS to hand.
--shots needs Playwright for Python (`pip install playwright`, then
`playwright install chromium`).
"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from equis_loader import bridge_payload  # noqa: E402
from equis_loader.analysis import AnalysisError, Session  # noqa: E402
from equis_loader.pivot import PivotError  # noqa: E402
from equis_loader.store import ReportStore  # noqa: E402

WEB = os.path.join(ROOT, "equis_loader", "web")
QT_SCRIPT = '<script src="qrc:///qtwebchannel/qwebchannel.js"></script>'

# Stands in for Qt's qwebchannel.js. Each bridge slot becomes a POST; the
# host answers chosen/ready with a list of [name, payload] calls, replayed
# on window.equis in order, which is what runJavaScript does in QGIS.
SHIM = r"""
(function () {
  const post = (path, body) => fetch(path, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)});
  async function replay(response) {
    const calls = await response.json();
    for (const [name, payload] of calls) {
      const result = window.equis[name](payload);
      if (result && typeof result.then === "function") await result;
    }
    window.__previewIdle = (window.__previewIdle || 0) + 1;
  }
  const bridge = {
    query(sql, kind, callback) {
      post("/query", {sql, kind}).then((r) => r.text()).then(callback);
    },
    ready() { post("/ready", {}).then(replay); },
    chosen(json) { post("/chosen", JSON.parse(json)).then(replay); },
    brushed(json) { post("/brushed", json ? JSON.parse(json) : null); },
    log(level, message) { post("/log", {level, message}); },
  };
  window.qt = {webChannelTransport: {}};
  window.QWebChannel = function (transport, connected) {
    setTimeout(() => connected({objects: {bridge}}), 0);
  };
})();
"""


class Host(object):
    """linked_views.LinkedViewsController's chooser half, without Qt."""

    def __init__(self, store, initial):
        self.store = store
        self.session = Session(store)
        self.initial = initial
        # DuckDB connections are not for concurrent use, and the page asks
        # in parallel; QGIS serialises it all on the UI thread.
        self.lock = threading.Lock()
        self.log = []

    def query(self, sql, kind):
        with self.lock:
            try:
                data = bridge_payload.answer(self.store.con, sql,
                                             kind or bridge_payload.KIND_ARROW)
            except Exception as exc:  # noqa: BLE001 - as web_bridge.query
                print("  query failed: %s\n    %s" % (exc, sql[:200]))
                return json.dumps({"ok": False, "kind": kind,
                                   "error": "%s: %s" % (type(exc).__name__, exc)})
        return json.dumps({"ok": True, "kind": kind, "data": data})

    def ready(self):
        self.session.page_reloaded()
        choice = dict(self.initial)
        if choice.get("matrix") and (choice.get("x") or choice.get("y")):
            with self.lock:
                try:
                    self.session.pivot(choice["matrix"])
                    choice["x"] = self.session.find(choice["matrix"], choice.get("x"))
                    choice["y"] = self.session.find(choice["matrix"], choice.get("y"))
                except (AnalysisError, PivotError):
                    pass
        return self.respond(choice)

    def respond(self, choice):
        calls = []
        started = time.monotonic()

        def say(message, where):
            print("  working (%s): %s" % (where, message))
            calls.append(["working", {"message": message, "where": where}])

        def send(name, payload):
            calls.append([name, payload])

        with self.lock:
            try:
                found = self.session.respond(choice, say, send)
            except (AnalysisError, PivotError, ValueError) as exc:
                return [["note", str(exc)]]
        took = time.monotonic() - started
        print("  chosen %s -> %s (%.2f s)" % (
            json.dumps(choice), ", ".join(name for name, _ in calls) or "nothing",
            took))
        if found is not None:
            stations = found.stations
            print("  the scatter map would get %d stations (%d with a tie at the "
                  "latest date)" % (len(stations),
                                    sum(1 for s in stations if (s.get("tied") or 1) > 1)))
            calls.append(["busy", "Drawing %s x %s..." % (
                found.first["chemical"], found.second["chemical"])])
            calls.append(["scatter", found.options])
        return calls


def make_handler(host):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body, kind):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0].lstrip("/") or "index.html"
            if path == "preview_shim.js":
                return self._send(SHIM, "text/javascript")
            full = os.path.normpath(os.path.join(WEB, path))
            if not full.startswith(WEB) or not os.path.isfile(full):
                self.send_error(404)
                return
            body = open(full, "rb").read()
            if path == "index.html":
                text = body.decode("utf-8")
                if QT_SCRIPT not in text:
                    raise RuntimeError("index.html no longer loads qwebchannel.js "
                                       "the way this harness replaces it")
                return self._send(text.replace(
                    QT_SCRIPT, '<script src="preview_shim.js"></script>'),
                    "text/html; charset=utf-8")
            kind = {"js": "text/javascript", "html": "text/html",
                    "json": "application/json"}.get(path.rsplit(".", 1)[-1],
                                                     "application/octet-stream")
            return self._send(body, kind)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"null")
            if self.path == "/query":
                return self._send(host.query(body["sql"], body.get("kind")),
                                  "application/json")
            if self.path == "/ready":
                return self._send(json.dumps(host.ready()), "application/json")
            if self.path == "/chosen":
                return self._send(json.dumps(host.respond(body or {})),
                                  "application/json")
            if self.path == "/brushed":
                print("  brushed: %s" % ((body or {}).get("label") or "cleared"))
                return self._send("{}", "application/json")
            if self.path == "/log":
                message = (body or {}).get("message", "")
                host.log.append(message)
                if (body or {}).get("level") in ("error", "warning") or "timings" in message:
                    print("  [page] %s" % message)
                return self._send("{}", "application/json")
            self.send_error(404)

    return Handler


def shots(url, out, host, args):
    """A scripted session, screenshotted. Returns the files written."""
    from playwright.sync_api import sync_playwright
    os.makedirs(out, exist_ok=True)
    written = []

    def settle(page, idle_before, timeout=60000):
        page.wait_for_function("(window.__previewIdle || 0) > %d" % idle_before,
                               timeout=timeout)
        return page.evaluate("window.__previewIdle || 0")

    def drawn(page, timeout=60000):
        page.wait_for_function(
            "() => { const c = window.equis && window.equis.chart;"
            " return c && c.state && c.state.timings; }", timeout=timeout)
        return page.evaluate("window.equis.chart.state")

    def shot(page, name):
        path = os.path.join(out, name)
        page.screenshot(path=path, full_page=True)
        written.append(path)
        print("  wrote %s" % path)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": args.width, "height": args.height},
                                device_scale_factor=1.5)
        page.goto(url)
        idle = settle(page, 0)

        # 1. Offered: the matrix chosen, nothing drawn yet.
        shot(page, "1_offered.png")

        # 2. Arsenic x Chromium in soil, chosen as a person would.
        x = host.session.find(args.matrix, args.x)
        y = host.session.find(args.matrix, args.y)
        page.select_option("#controls select.axis-select >> nth=0", x)
        idle = settle(page, idle)
        page.select_option("#controls select.axis-select >> nth=1", y)
        idle = settle(page, idle)
        state = drawn(page)
        print("  drew %s samples; classes %s; timings %s"
              % (state["total"], state["counts"], state.get("timings")))
        shot(page, "2_scatter.png")

        # 3. Y's list as it reads once X is chosen: shown as an open list
        # (a native drop-down does not appear in a screenshot).
        page.evaluate("""() => { const s = document.querySelectorAll(
            '#controls select.axis-select')[1]; s.size = 14; }""")
        shot(page, "3_y_list.png")
        page.evaluate("""() => { document.querySelectorAll(
            '#controls select.axis-select')[1].size = 0; }""")

        # 4. A brush over the upper right of the plot.
        box = page.locator("#views svg").first.bounding_box()
        page.mouse.move(box["x"] + box["width"] * 0.55, box["y"] + box["height"] * 0.10)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] * 0.95, box["y"] + box["height"] * 0.55,
                        steps=8)
        page.mouse.up()
        page.wait_for_timeout(600)
        print("  brush label: %s" % page.text_content("#views .brush"))
        shot(page, "4_brushed.png")

        # 5. Another matrix, by the chooser: the wide catalogue.
        for code, cx, cy, name in args.more:
            page.select_option("#controls .chooser select >> nth=0", code)
            idle = settle(page, idle, timeout=120000)
            px, py = host.session.find(code, cx), host.session.find(code, cy)
            if not (px and py):
                print("  %s: %s or %s not offered; skipped" % (code, cx, cy))
                continue
            page.select_option("#controls select.axis-select >> nth=0", px)
            idle = settle(page, idle)
            page.select_option("#controls select.axis-select >> nth=1", py)
            idle = settle(page, idle, timeout=120000)
            state = drawn(page, timeout=120000)
            print("  %s: drew %s samples; timings %s" % (code, state["total"],
                                                         state.get("timings")))
            shot(page, name)

        # Points: most recent per location, then a time window - pushed the
        # way the Temporal Controller would, through Session.repoint (there
        # is no map here to move).
        points = page.locator("#controls select", has=page.locator("option[value='recent']"))
        points.select_option("recent")
        idle = settle(page, idle, timeout=120000)
        state = drawn(page, timeout=120000)
        print("  points=recent: drew %s" % state["total"])
        shot(page, "8_points_recent.png")
        import datetime
        with host.lock:
            found = host.session.repoint(window=(datetime.date(2014, 1, 1),
                                                 datetime.date(2014, 12, 31)))
        page.evaluate("o => window.equis.scatter(o)", found.options)
        state = drawn(page, timeout=120000)
        print("  window 2014: drew %s solid; the map would get %d stations"
              % (state["total"], len(found.stations)))
        shot(page, "9_window_2014.png")

        # A ternary: calcium : magnesium : sodium in water, every sample,
        # then most recent per location, then without the non-detects.
        with host.lock:
            host.session.repoint(mode="all", window=None)
        chart = page.locator("#controls select", has=page.locator("option[value='ternary']"))
        chart.select_option("ternary")
        idle = settle(page, idle, timeout=120000)
        points.select_option("all")
        idle = settle(page, idle, timeout=120000)
        trio = [host.session.find("WS", n) for n in ("Calcium", "Magnesium", "Sodium")]
        for nth, prefix in enumerate(trio):
            page.select_option("#controls select.axis-select >> nth=%d" % nth, prefix)
            idle = settle(page, idle, timeout=120000)
        state = drawn(page, timeout=120000)
        print("  ternary Ca:Mg:Na: drew %s; timings %s" % (state["total"], state.get("timings")))
        shot(page, "10_ternary.png")
        points.select_option("recent")
        idle = settle(page, idle, timeout=120000)
        state = drawn(page, timeout=120000)
        print("  ternary, most recent per location: drew %s" % state["total"])
        shot(page, "11_ternary_recent.png")
        page.locator("#views .legend input[type=checkbox]").uncheck()
        page.wait_for_timeout(1500)
        print("  ternary without non-detects: %s"
              % page.text_content("#views .caption >> nth=1").encode(
                  "ascii", "replace").decode())
        shot(page, "12_ternary_hide_nd.png")

        # The warnings, opened: they must lie OVER the chart, not push it.
        toggle = page.locator("#controls .chooser-warn")
        if toggle.is_visible():
            before = page.locator("#views svg").first.bounding_box()
            toggle.click()
            page.wait_for_timeout(200)
            after = page.locator("#views svg").first.bounding_box()
            print("  warnings opened; chart moved %+.0f px" % (after["y"] - before["y"]))
            shot(page, "7_warnings_open.png")
        browser.close()
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", default=os.path.join(ROOT, "data", "facility_a"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--matrix", default="SO")
    parser.add_argument("--x", default="Arsenic")
    parser.add_argument("--y", default="Chromium")
    parser.add_argument("--shots", help="screenshot a scripted session into DIR")
    parser.add_argument("--width", type=int, default=620)
    parser.add_argument("--height", type=int, default=1000)
    args = parser.parse_args()
    args.more = [("TAILS", "Copper", "Zinc", "5_tails_cu_zn.png"),
                 ("WS", "Manganese", "Lead", "6_ws_mn_pb.png")]

    store = ReportStore.from_manifest(args.root)
    # The panel starts the chooser from its own matrix; --shots then
    # chooses X and Y through the page, as a person would.
    initial = {"matrix": args.matrix}
    if not args.shots:
        initial.update(x=args.x, y=args.y)
    host = Host(store, initial)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(host))
    url = "http://127.0.0.1:%d/" % args.port
    if not args.shots:
        print("Serving the Analysis tab at %s  (Ctrl+C to stop)" % url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        shots(url, args.shots, host, args)
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
