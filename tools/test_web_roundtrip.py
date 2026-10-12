"""End to end: a real QWebEngineView loads the real bundle and queries DuckDB.

This is the suite the other web tests were leading to. Bridge, scheme handler,
bundle and connector were each checked alone; here they have to agree with
each other, in a live Chromium, over the equis:// scheme.

RUN IT FROM THE QGIS PYTHON CONSOLE:

    exec(open(r"C:\\path\\to\\equis_qgis\\tools\\test_web_roundtrip.py").read())

Qt WebEngine needs the running application QGIS itself creates - Chromium is
initialised as part of it. Under `python-qgis.bat` a bare QApplication is not
enough: the scheme registers, the handler is reached, the file is read and
`job.reply()` returns, and the load still fails with `ok=False` and no error
anywhere. `tools/test_webengine.py` carries the same warning for the same
reason, and it is the kind of failure that looks like a bug in this code
rather than in the environment.

TWO THINGS THE CONSOLE FORCES, both learned by breaking them:

  * No `SystemExit`. Pasted into the console, it closes the console.
  * No `app.exec()`. The console is already running the event loop; starting
    a second one nests inside it and hangs. So the verdict is printed from a
    CALLBACK, and this returns immediately with the result arriving a few
    seconds later.

What it proves, in the order the failures would happen:

  1. the scheme handler serves index.html and app.js
  2. the page's ES module imports resolve (file:// cannot do this at all)
  3. QWebChannel connects and the page finds `bridge`
  4. the page calls ready() - the handshake, not loadFinished
  5. a Mosaic-shaped query reaches DuckDB and comes back as rows
  6. a FAILED query arrives as a rejected promise, not as silence
"""
import os
import sys

# `exec(open(...).read())` defines no __file__, and that is the documented way
# to run this, so deriving the repository root from it fails on the first
# line. Find the package on sys.path or in the working directory instead.
try:
    SELF = os.path.abspath(__file__)
except NameError:
    SELF = None

if SELF:
    ROOT = os.path.dirname(os.path.dirname(SELF))
else:
    ROOT = None
    for candidate in [os.getcwd()] + list(sys.path):
        if candidate and os.path.isdir(os.path.join(candidate, "equis_loader")):
            ROOT = os.path.abspath(candidate)
            break
    SELF = os.path.join(ROOT or ".", "tools", "test_web_roundtrip.py")

if ROOT and ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


def preflight():
    """(app, web_view, duckdb) or None, with a reason printed."""
    if ROOT is None:
        print("Could not find the repository. Set it on sys.path first:")
        print("    import sys; sys.path.insert(0, r'C:\\path\\to\\equis_qgis')")
        return None
    try:
        from qgis.PyQt.QtWidgets import QApplication
    except ImportError as exc:
        print("needs QGIS: %s" % exc)
        return None
    try:
        import duckdb
    except ImportError as exc:
        print("needs duckdb: %s" % exc)
        return None
    # QGIS has its own equis_loader on sys.path - the INSTALLED plugin - and
    # if it was imported before this ran, that is the one already in
    # sys.modules and an insert at position 0 does not displace it. An
    # installed copy older than this branch has no web_view at all, and the
    # ImportError names the plugin directory rather than saying "stale".
    import equis_loader
    package = os.path.dirname(os.path.abspath(equis_loader.__file__))
    expected = os.path.join(ROOT, "equis_loader")
    if os.path.normcase(package) != os.path.normcase(expected):
        # Importing the INSTALLED plugin rather than the working tree is
        # normal and usually fine - `tools/install.py` copies one to the
        # other. What matters is whether the copy is CURRENT, so compare the
        # files rather than the paths. An earlier version of this check
        # refused on the path alone and rejected a perfectly good install.
        import hashlib
        stale = []
        for name in ("web_view.py", "web_bridge.py", "bridge_payload.py",
                     "web_assets.py", os.path.join("web", "app.js")):
            here = os.path.join(expected, name)
            there = os.path.join(package, name)
            if not os.path.isfile(there):
                stale.append("%s is missing" % name)
                continue
            if not os.path.isfile(here):
                continue
            def digest(path):
                with open(path, "rb") as handle:
                    return hashlib.sha256(handle.read()).hexdigest()
            if digest(here) != digest(there):
                stale.append("%s differs" % name)
        if stale:
            print("The INSTALLED plugin is not the working tree:")
            print("    %s" % package)
            for item in stale:
                print("      %s" % item)
            print("\nRun `python tools/install.py` and restart QGIS.")
            return None
        print("using the installed plugin at %s" % package)
        print("(its files match this working tree)")
    try:
        from equis_loader import web_view
    except ImportError as exc:
        print("equis_loader has no web_view: %s" % exc)
        print("The copy at %s is older than this branch." % package)
        print("Run `python tools/install.py` and restart QGIS.")
        return None
    if not web_view.available():
        print("this QGIS build has no QtWebEngine")
        return None
    if not web_view.bundle_present():
        print("no built bundle; run: cd web && npm ci && npm run build")
        return None
    application = QApplication.instance()
    if application is None:
        print("No QGIS application is running.")
        print("Qt WebEngine needs the one QGIS creates - Chromium is")
        print("initialised as part of it - so this cannot run under")
        print("python-qgis.bat. Paste it into the QGIS Python Console:")
        print("    exec(open(r'%s').read())" % SELF)
        return None
    return application, web_view, duckdb


def run(app, web_view, duckdb):
    from qgis.PyQt.QtCore import QTimer

    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE results AS SELECT i AS row_id, "
        "'MW-' || (i % 4) AS location, "
        "CASE WHEN i % 3 = 0 THEN 'Arsenic' ELSE 'Lead' END AS chemical, "
        "i * 1.5 AS value FROM range(120) tbl(i)")
    # The histogram reads v_results, the store's view - not this table. Build
    # a stand-in with the columns and the non-detect conventions the chart
    # expects, so the chart is exercised without a full ReportStore.
    con.execute(
        "CREATE VIEW v_results AS SELECT row_id, location, "
        "'Nickel' AS chemical, '7440-02-0' AS cas_rn, "
        "CAST('2020-01-01' AS TIMESTAMP) + INTERVAL (row_id) DAY "
        "  AS sample_date, "
        "value AS value_half, value * 2 AS value_full, "
        "CASE WHEN row_id % 5 = 0 THEN NULL ELSE value END AS value_exclude, "
        "'mg/kg' AS unit, (row_id % 5) <> 0 AS detected, "
        "'SO' AS matrix, 'T' AS fraction, 'M1' AS method "
        "FROM results")

    state = {"ready": False, "results": {}, "done": False}
    holder = {}

    def report():
        views = holder["views"]
        check("the page reported ready", state["ready"],
              "" if state["ready"]
              else (views.failed or "no ready() within the timeout"))
        if not state["ready"]:
            print("\nThe page never connected. That is one of: the scheme "
                  "did not register, the bundle 404'd, or app.js threw "
                  "before calling ready(). Check the QGIS log, 'equis-web'.")
            print("The view is left open as equis_roundtrip['views'].")
            return

        count = state["results"].get("count")
        check("a JSON query round-tripped to DuckDB",
              count is not None and '"n":120' in str(count).replace(" ", ""),
              str(count))

        arrow = state["results"].get("arrow")
        check("an Arrow query decoded in the page",
              isinstance(arrow, str) and arrow.startswith("rows=4"),
              str(arrow))

        direct = state["results"].get("direct")
        check("the connector answers Arrow directly",
              isinstance(direct, str) and direct.startswith("rows=4"),
              str(direct))

        chart = state["results"].get("chart")
        check("the histogram renders with a caption",
              isinstance(chart, str) and "svg=yes" in chart
              and "caption=" in chart and "NO" not in chart,
              str(chart))

        trace = state["results"].get("trace")
        if trace:
            print("    connector trace: %s" % trace)
        log = state["results"].get("log")
        if log:
            print("    mosaic log: %s" % log)

        failure = state["results"].get("failure")
        check("a bad query rejects rather than resolving empty",
              isinstance(failure, str) and failure.startswith("rejected:"),
              str(failure))

        print()
        if FAILURES:
            print("FAILED: %s" % ", ".join(FAILURES))
        else:
            print("The page, the scheme, the channel and DuckDB all agree.")
        views.close()

    def finish():
        if state["done"]:
            return
        state["done"] = True
        report()

    # runJavaScript evaluates an EXPRESSION and hands back its value at once.
    # A Promise does not serialise across that boundary - the callback gets
    # {} - so awaiting in JS and returning the promise cannot work. The page
    # therefore writes its answers into a global, and this polls for them.
    DRIVE = """
    (function () {
      window.__probe = {};
      // Mosaic logs through console.debug; capture it so the manager's own
      // account of what it did comes back with everything else.
      window.__probe.log = [];
      const realDebug = console.debug.bind(console);
      console.debug = function () {
        window.__probe.log.push(Array.from(arguments).map(String).join(' ')
                                .slice(0, 120));
        return realDebug.apply(console, arguments);
      };
      try { window.equis.coordinator.logger(console); } catch (e) {}
      const q = window.equis.query;
      q('SELECT count(*) AS n FROM results')
        .then(r => window.__probe.count = JSON.stringify(r))
        .catch(e => window.__probe.count = 'ERROR: ' + e.message);
      // Count what the manager actually does: does it reach the connector
      // at all for the arrow request, and with what?
      window.__probe.trace = [];
      const conn = window.equis.connector;
      const realQuery = conn.query.bind(conn);
      conn.query = function (req) {
        window.__probe.trace.push('call type=' + req.type +
          ' keys=' + Object.keys(req).join('|'));
        return realQuery(req).then(
          v => { window.__probe.trace.push('resolved type=' + req.type);
                 return v; },
          e => { window.__probe.trace.push('rejected type=' + req.type +
                                           ' ' + e.message);
                 throw e; });
      };

      // Raced against a timeout: a promise that never settles writes
      // nothing at all, and an absent key is indistinguishable from a key
      // that was never attempted. This is what turned three runs of "None"
      // into a diagnosis.
      Promise.race([
        q('SELECT location, count(*) AS n FROM results GROUP BY 1 ORDER BY 1',
          'arrow'),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error('arrow query never settled')),
                     8000)),
      ])
        .then(t => window.__probe.arrow = (t && t.numRows !== undefined)
              ? ('rows=' + t.numRows + ' cols=' + t.numCols)
              : ('UNEXPECTED: ' + Object.prototype.toString.call(t) + ' ' +
                 JSON.stringify(Object.keys(t || {})).slice(0, 80)))
        .catch(e => window.__probe.arrow =
               'ERROR: ' + (e && e.message ? e.message : String(e)));
      q('SELECT * FROM no_such_table')
        .then(() => window.__probe.failure = 'RESOLVED - should have rejected')
        .catch(e => window.__probe.failure =
               'rejected: ' + String(e.message).slice(0, 60));
      // US-1: draw the actual histogram. This is the first time a vgplot
      // chart is rendered at all - everything before proved the plumbing.
      window.equis.histogram({chemical: 'Nickel', matrix: 'SO'})
        .then(state => {
          // vgplot fills its element ASYNCHRONOUSLY, after the bin-extent
          // queries settle - `plot()` returns the container, not a drawn
          // chart. So poll briefly rather than looking once.
          const deadline = Date.now() + 6000;
          const look = () => {
            const svg = document.querySelector('#views svg');
            const caption = document.querySelector('#views .caption');
            if (svg || Date.now() > deadline) {
              const el = document.getElementById('views');
              window.__probe.chart =
                'rows=' + state.total + ' nd=' + state.nonDetects +
                ' bins=' + state.bins +
                ' svg=' + (svg ? 'yes' : 'NO') +
                ' rects=' + document.querySelectorAll('#views rect').length +
                ' kids=' + (el ? Array.from(el.children)
                                  .map(c => c.tagName.toLowerCase() +
                                       (c.className ? '.' + c.className : ''))
                                  .join(',') : 'none') +
                ' caption=' + (caption
                               ? JSON.stringify(caption.textContent) : 'NO');
            } else {
              setTimeout(look, 150);
            }
          };
          look();
        })
        .catch(e => window.__probe.chart =
               'ERROR: ' + (e && e.message ? e.message : String(e)));

      // The same Arrow query STRAIGHT to the connector, skipping Mosaic's
      // manager and cache. If this settles and the one above does not, the
      // bridge is fine and Mosaic is holding the promise.
      Promise.race([
        window.equis.direct(
          'SELECT location, count(*) AS n FROM results GROUP BY 1 ORDER BY 1'),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error('direct never settled')), 8000)),
      ])
        .then(t => window.__probe.direct = (t && t.numRows !== undefined)
              ? ('rows=' + t.numRows + ' cols=' + t.numCols)
              : ('UNEXPECTED: ' + Object.prototype.toString.call(t)))
        .catch(e => window.__probe.direct =
               'ERROR: ' + (e && e.message ? e.message : String(e)));
      return 'started';
    })()
    """

    COLLECT = "JSON.stringify(window.__probe || {})"

    def after_ready():
        page = holder["views"].view.page()
        page.runJavaScript(DRIVE, lambda value: state.update(driving=True))

    def collect():
        """Poll the page's global until all three answers are in."""
        import json as _json

        def got(raw):
            try:
                answers = _json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                answers = {}
            state["results"].update(answers)
            # By NAME, not by count: Arrow decodes a base64 payload and can
            # settle after the other two, and "three keys" would also be
            # satisfied by three copies of the fast ones.
            if all(key in state["results"]
                   for key in ("count", "arrow", "failure", "direct",
                               "chart")):
                # The trace is a list, collected alongside; it is not part
                # of the completion condition.
                finish()

        holder["views"].view.page().runJavaScript(COLLECT, got)

    def poll(remaining=[200]):   # 200 x 250 ms = 50 s; the chart polls up to 6 s
        if state["done"]:
            return
        if state["ready"] and not state.get("driving"):
            after_ready()
        elif state.get("driving"):
            collect()
        remaining[0] -= 1
        if remaining[0] <= 0:
            finish()
            return
        QTimer.singleShot(250, poll)

    holder["views"] = web_view.LinkedViews(
        lambda: con, on_ready=lambda: state.update(ready=True))
    QTimer.singleShot(250, poll)
    print("running... the verdict prints in a few seconds.")
    # Returned so the console can inspect a failed run, and so nothing here
    # is collected while Chromium is still starting.
    return {"views": holder["views"], "state": state, "con": con}


_ready = preflight()
if _ready is None:
    print("SKIPPED")
    equis_roundtrip = None
else:
    equis_roundtrip = run(*_ready)
