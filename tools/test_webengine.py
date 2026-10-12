"""Does Qt WebEngine actually work in THIS QGIS install?

Paste-and-run probe for the QGIS Python console. It answers the question the
linked-views vision hangs on (docs/vision-linked-views.md, R5): can a plugin
loaded after QGIS has started put a working Chromium view on screen, talk to
it over QWebChannel, and load ES modules from a custom scheme?

Run it from the QGIS Python console (Plugins -> Python Console):

    exec(open(r"C:\\path\\to\\equis_qgis\\tools\\test_webengine.py").read())

or, if the repo is on sys.path:

    import tools.test_webengine

It needs the console's running event loop, so it will NOT work under
python-qgis.bat as a plain script - stages 3 onward never get to run. That is
deliberate: the point is to test the environment a plugin really lives in.

Nothing is installed, nothing is written to disk, and no network is touched.
The view is created hidden; pass SHOW = True below to watch it.

Stages 1-2 print immediately. Stages 3-7 are asynchronous and print over the
next few seconds - keep the console open. A verdict block prints at the end,
formatted to paste straight back into an issue.

A hard crash of QGIS is itself a result. If that happens, report the stage
number that printed last.
"""

SHOW = False          # True to actually display the view
TIMEOUT_MS = 30000    # backstop; individual stages have their own timeouts


import platform
import sys
import traceback

RESULTS = []          # (stage, label, ok, detail)
STATE = {}            # keeps Qt objects alive against console garbage collection
QT_MESSAGES = []      # qWarning() text captured while the probe runs

# These must survive STATE.clear() between runs. A URL scheme handler stays
# installed on the profile for the life of the QGIS process, so dropping the
# Python reference leaves the profile pointing at a destroyed object and every
# later request on that scheme goes nowhere - which is exactly how a second
# run of this probe reported modules=NO after a first run reported yes.
SCHEME_HANDLERS = {}  # scheme name -> handler, never cleared
RUNS = [0]            # how many times this probe has run in this process


def record(stage, label, ok, detail=""):
    RESULTS.append((stage, label, ok, detail))
    mark = "PASS" if ok else ("INFO" if ok is None else "FAIL")
    print("[%s] %d. %s%s" % (mark, stage, label, (" - " + detail) if detail else ""))


def note(stage, label, detail=""):
    """Something worth recording that is neither a pass nor a failure."""
    record(stage, label, None, detail)


def _install_message_handler():
    """Capture qWarning() output for the duration of the probe.

    Qt explains scheme and profile failures through qWarning and nowhere
    else - "Cannot install a URL scheme handler overriding an existing one",
    "Please register the custom scheme before constructing the profile". Those
    lines are the difference between a real finding and a guess, and they do
    not otherwise reach the QGIS console.
    """
    try:
        from qgis.PyQt.QtCore import qInstallMessageHandler
    except Exception:
        return

    def handler(mode, context, message):
        text = str(message)
        QT_MESSAGES.append(text)
        print("    [qt] %s" % text)      # forwarded, never swallowed

    try:
        qInstallMessageHandler(handler)
        STATE["message_handler"] = handler
    except Exception:
        pass


def _restore_message_handler():
    try:
        from qgis.PyQt.QtCore import qInstallMessageHandler
        qInstallMessageHandler(None)
    except Exception:
        pass


# --------------------------------------------------------------- 1. bindings

def stage1_bindings():
    from qgis.PyQt.QtCore import QT_VERSION_STR, PYQT_VERSION_STR

    print("")
    print("QGIS WebEngine probe")
    print("  Python %s on %s %s" % (platform.python_version(),
                                    platform.system(), platform.machine()))
    print("  Qt %s / PyQt %s" % (QT_VERSION_STR, PYQT_VERSION_STR))
    try:
        from qgis.core import Qgis
        print("  QGIS %s" % Qgis.QGIS_VERSION)
    except Exception:
        pass
    print("")

    ok = True

    # The widget. On a broken OSGeo4W build this is the import that fails,
    # usually with ModuleNotFoundError because the DLLs shipped and the
    # bindings did not.
    try:
        from qgis.PyQt.QtWebEngineWidgets import QWebEngineView
        STATE["QWebEngineView"] = QWebEngineView
        record(1, "import QWebEngineView", True,
               QWebEngineView.__module__)
    except Exception as exc:
        record(1, "import QWebEngineView", False, "%s: %s" % (type(exc).__name__, exc))
        # This is the fatal one - nothing downstream can run without it.
        return False

    # Qt6 moved QWebEnginePage, QWebEngineProfile, QWebEngineUrlScheme and the
    # scheme handler into QtWebEngineCore. Qt5 kept the first two in
    # QtWebEngineWidgets. Report which layout we are on, since plugin code has
    # to import from the right place.
    core_names = ("QWebEnginePage", "QWebEngineProfile", "QWebEngineSettings",
                  "QWebEngineUrlScheme", "QWebEngineUrlSchemeHandler",
                  "QWebEngineUrlRequestJob")
    found = {}
    for modname in ("QtWebEngineCore", "QtWebEngineWidgets"):
        try:
            mod = __import__("qgis.PyQt.%s" % modname, fromlist=["*"])
        except Exception:
            continue
        for name in core_names:
            if name not in found and hasattr(mod, name):
                found[name] = modname
                STATE[name] = getattr(mod, name)
    missing = [n for n in core_names if n not in found]
    record(1, "WebEngine core classes", not missing,
           ("missing: " + ", ".join(missing)) if missing
           else "in " + ", ".join(sorted(set(found.values()))))
    if missing:
        ok = False

    # The transport the whole Python<->JS design rests on.
    try:
        from qgis.PyQt.QtWebChannel import QWebChannel
        STATE["QWebChannel"] = QWebChannel
        record(1, "import QWebChannel", True)
    except Exception as exc:
        record(1, "import QWebChannel", False, "%s: %s" % (type(exc).__name__, exc))
        ok = False

    # qwebchannel.js lives in a Qt resource. Without it the JS side cannot
    # construct a QWebChannel, and there is no copy to fall back on.
    try:
        from qgis.PyQt.QtCore import QFile, QIODevice
        handle = QFile(":/qtwebchannel/qwebchannel.js")
        if handle.open(QIODevice.OpenModeFlag.ReadOnly if hasattr(QIODevice, "OpenModeFlag")
                       else QIODevice.ReadOnly):
            js = bytes(handle.readAll()).decode("utf-8")
            handle.close()
            STATE["qwebchannel_js"] = js
            record(1, "qwebchannel.js resource", True, "%d bytes" % len(js))
        else:
            record(1, "qwebchannel.js resource", False, "resource did not open")
            ok = False
    except Exception as exc:
        record(1, "qwebchannel.js resource", False, "%s: %s" % (type(exc).__name__, exc))
        ok = False

    return ok


# ------------------------------------------------------- 2. construct a view

def stage2_construct():
    try:
        view = STATE["QWebEngineView"]()
        view.resize(640, 400)
        if SHOW:
            view.setWindowTitle("WebEngine probe")
            view.show()
        STATE["view"] = view
        record(2, "construct QWebEngineView", True,
               "shown" if SHOW else "hidden (set SHOW = True to display)")
        return True
    except Exception as exc:
        record(2, "construct QWebEngineView", False, "%s: %s" % (type(exc).__name__, exc))
        traceback.print_exc()
        return False


# ------------------------------------------------- 3-5. load, run JS, probe

PROBE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>probe</title></head>
<body><p id="out">loading</p>
<script>
  window.__probe = { inlineModule: false, dynamicImport: false };
</script>
<script type="module">
  // If this runs at all, inline ES modules work.
  window.__probe.inlineModule = true;
  // A data: URL module import is the cheapest test of the import machinery
  // itself, separate from whether a given SCHEME can serve one (stage 7).
  import("data:text/javascript,export const v = 42;")
    .then(m => { window.__probe.dynamicImport = (m.v === 42); })
    .catch(e => { window.__probe.importError = String(e); });
  document.getElementById("out").textContent = "ready";
</script>
</body></html>"""


PROBE_JS = """
(function () {
  var p = window.__probe || {};
  var ua = navigator.userAgent;
  var chrome = (ua.match(/Chrome\\/([0-9.]+)/) || [])[1] || "unknown";
  return JSON.stringify({
    chromium: chrome,
    userAgent: ua,
    bigint: (typeof BigInt === "function"),
    wasm: (typeof WebAssembly === "object"),
    sab: (typeof SharedArrayBuffer === "function"),
    atob: (typeof atob === "function"),
    textDecoder: (typeof TextDecoder === "function"),
    inlineModule: !!p.inlineModule,
    dynamicImport: !!p.dynamicImport,
    importError: p.importError || null,
    domReady: (document.getElementById("out") || {}).textContent || null
  });
})()
"""


def stage3_load():
    import json
    from qgis.PyQt.QtCore import QTimer

    view = STATE["view"]

    def on_loaded(ok):
        # Disconnect first: stages 6 and 7 load further pages into this same
        # view, and a handler left connected would re-enter the whole probe.
        try:
            view.loadFinished.disconnect(on_loaded)
        except Exception:
            pass
        record(3, "page load (setHtml)", bool(ok),
               "loadFinished(%s)" % ok)
        if not ok:
            finish()
            return
        # runJavaScript is the Python<->JS read path. On some builds the page
        # loads and this silently never calls back, which is why it gets its
        # own stage and its own timeout.
        def on_js(raw):
            if raw is None:
                record(4, "runJavaScript round-trip", False, "callback got None")
                finish()
                return
            record(4, "runJavaScript round-trip", True, "%d bytes returned" % len(str(raw)))
            try:
                info = json.loads(raw)
            except Exception as exc:
                record(5, "engine capabilities", False, "unparseable: %s" % exc)
                finish()
                return
            STATE["engine"] = info
            record(5, "Chromium version", True, info.get("chromium", "unknown"))
            # These four are what a vgplot/flechette bundle actually needs.
            for key, label in (("bigint", "BigInt"),
                               ("inlineModule", "inline ES modules"),
                               ("dynamicImport", "dynamic import()"),
                               ("atob", "atob (base64 Arrow IPC decode)")):
                detail = ""
                if key == "dynamicImport" and info.get("importError"):
                    detail = info["importError"]
                record(5, label, bool(info.get(key)), detail)
            note(5, "WebAssembly", "yes" if info.get("wasm") else "no")
            note(5, "SharedArrayBuffer",
                 ("yes" if info.get("sab") else "no") + " (not needed for this design)")
            stage6_webchannel()

        try:
            view.page().runJavaScript(PROBE_JS, on_js)
        except Exception as exc:
            record(4, "runJavaScript round-trip", False,
                   "%s: %s" % (type(exc).__name__, exc))
            finish()

    view.loadFinished.connect(on_loaded)
    view.setHtml(PROBE_HTML)
    print("    ... stages 3-7 are asynchronous; keep the console open")
    QTimer.singleShot(TIMEOUT_MS, _timeout)


# ------------------------------------------------------- 6. QWebChannel trip

def _make_bridge():
    from qgis.PyQt.QtCore import QObject, pyqtSlot, pyqtSignal

    class Bridge(QObject):
        fromPython = pyqtSignal(str)

        def __init__(self):
            super().__init__()
            self.heard = None
            self.ready_seen = False
            self.on_ready = None

        @pyqtSlot(str)
        def helloFromJs(self, message):
            self.heard = message

        @pyqtSlot(str, result=str)
        def echo(self, message):
            return message[::-1]

        @pyqtSlot()
        def ready(self):
            """JS says the channel handshake is complete.

            This exists because loadFinished is NOT the moment the page can
            receive a signal: constructing a QWebChannel in JS costs an async
            transport round-trip, and until it lands nothing has connected to
            fromPython. Emitting on loadFinished delivers into the void. The
            arch notes flag this (section 5, "loadFinished != app ready");
            an earlier version of this probe did it anyway and reported a
            working channel as broken.
            """
            self.ready_seen = True
            if self.on_ready is not None:
                self.on_ready()

    return Bridge()


CHANNEL_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"></head><body>
<script>%(qwebchannel_js)s</script>
<script>
  window.__chan = { constructed: false, slotCalled: false,
                    echo: null, signal: null, error: null };
  try {
    new QWebChannel(qt.webChannelTransport, function (channel) {
      var bridge = channel.objects.bridge;
      window.__chan.constructed = true;
      // Python signal -> JS
      bridge.fromPython.connect(function (text) { window.__chan.signal = text; });
      // JS -> Python slot, no return value
      bridge.helloFromJs("hello from the page");
      window.__chan.slotCalled = true;
      // JS -> Python slot WITH a return value, which is the shape the
      // Mosaic connector needs (query(sql, type) -> result).
      bridge.echo("stressed", function (reply) { window.__chan.echo = reply; });
      // Only now may Python emit: the connect() above has actually happened.
      bridge.ready();
    });
  } catch (e) {
    window.__chan.error = String(e);
  }
</script>
</body></html>"""


CHANNEL_JS = "JSON.stringify(window.__chan || {missing: true})"


def stage6_webchannel():
    import json
    from qgis.PyQt.QtCore import QTimer, QUrl

    if "QWebChannel" not in STATE or "qwebchannel_js" not in STATE:
        record(6, "QWebChannel round-trip", False, "prerequisites missing from stage 1")
        finish()
        return

    view = STATE["view"]
    try:
        bridge = _make_bridge()
        channel = STATE["QWebChannel"](view.page())
        channel.registerObject("bridge", bridge)
        view.page().setWebChannel(channel)
        STATE["bridge"] = bridge
        STATE["channel"] = channel
    except Exception as exc:
        record(6, "QWebChannel setup", False, "%s: %s" % (type(exc).__name__, exc))
        finish()
        return

    html = CHANNEL_HTML_TEMPLATE % {"qwebchannel_js": STATE["qwebchannel_js"]}

    def on_loaded(ok):
        try:
            view.loadFinished.disconnect(on_loaded)
        except Exception:
            pass
        if not ok:
            record(6, "QWebChannel page load", False, "loadFinished(False)")
            finish()
            return

        def read_back():
            if STATE.get("stage6_read"):
                return
            STATE["stage6_read"] = True

            def on_js(raw):
                try:
                    chan = json.loads(raw) if raw else {}
                except Exception:
                    chan = {}
                if chan.get("error"):
                    record(6, "QWebChannel construct in JS", False, chan["error"])
                    finish()
                    return
                record(6, "QWebChannel construct in JS", bool(chan.get("constructed")))
                record(6, "JS -> Python slot", bridge.heard is not None,
                       repr(bridge.heard))
                record(6, "JS -> Python slot with return value",
                       chan.get("echo") == "desserts", repr(chan.get("echo")))
                record(6, "Python -> JS signal", chan.get("signal") is not None,
                       repr(chan.get("signal")))
                stage7_scheme()
            view.page().runJavaScript(CHANNEL_JS, on_js)

        def when_ready():
            """Called from the bridge's ready() slot, on the main thread."""
            if STATE.get("stage6_ready"):
                return
            STATE["stage6_ready"] = True
            record(6, "channel ready handshake", True,
                   "JS called back into Python")
            # Safe to emit now, and not one instant earlier.
            bridge.fromPython.emit("hello from python")
            QTimer.singleShot(250, read_back)

        def ready_timeout():
            if STATE.get("stage6_ready") or STATE.get("stage6_read"):
                return
            STATE["stage6_ready"] = True
            record(6, "channel ready handshake", False,
                   "JS never called ready() within 3 s")
            # Read back anyway: knowing which of the four parts worked is
            # more useful than dead-ending here.
            bridge.fromPython.emit("hello from python")
            QTimer.singleShot(250, read_back)

        bridge.on_ready = when_ready
        QTimer.singleShot(3000, ready_timeout)

    view.loadFinished.connect(on_loaded)
    # A real URL rather than about:blank: some builds refuse a web channel on
    # a blank page, and a custom scheme is what a plugin would really serve.
    view.setHtml(html, QUrl("https://probe.invalid/"))


# ------------------------------------------- 7. custom scheme + real modules

SCHEME_NAME = b"equisprobe"

MODULE_INDEX = b"""<!doctype html>
<html><head><meta charset="utf-8"></head><body>
<script type="module">
  import { answer } from "./dep.js";
  window.__mod = { imported: (answer === 42) };
</script>
</body></html>"""

MODULE_DEP = b"export const answer = 42;\n"


def stage7_scheme():
    """Can a bundled ES-module app be served from a custom scheme?

    This is the stage that decides whether a vgplot bundle can be shipped
    inside the plugin. file:// forbids module imports, so a custom scheme
    handler is the documented way round it - but QWebEngineUrlScheme wants
    registering before the application is created, and a plugin is loaded
    long after that. This finds out what really happens.
    """
    import json
    from qgis.PyQt.QtCore import QBuffer, QByteArray, QTimer, QUrl

    needed = ("QWebEngineUrlScheme", "QWebEngineUrlSchemeHandler", "QWebEngineProfile")
    if any(n not in STATE for n in needed):
        note(7, "custom URL scheme", "skipped - classes not available")
        finish()
        return

    QWebEngineUrlScheme = STATE["QWebEngineUrlScheme"]
    QWebEngineUrlSchemeHandler = STATE["QWebEngineUrlSchemeHandler"]
    QWebEngineProfile = STATE["QWebEngineProfile"]

    if RUNS[0] > 1 and SCHEME_NAME in SCHEME_HANDLERS:
        note(7, "second run in one QGIS process",
             "a custom scheme cannot be re-registered; restart QGIS for an "
             "authoritative answer to this stage")

    try:
        existing = QWebEngineUrlScheme.schemeByName(QByteArray(SCHEME_NAME))
        already = bool(bytes(existing.name()))
        if not already:
            scheme = QWebEngineUrlScheme(QByteArray(SCHEME_NAME))
            # Qt6 scopes these enums; Qt5 hangs them off the class itself.
            flags = getattr(QWebEngineUrlScheme, "Flag", QWebEngineUrlScheme)
            scheme.setFlags(flags.SecureScheme | flags.LocalAccessAllowed
                            | flags.CorsEnabled)
            syntax = getattr(QWebEngineUrlScheme, "Syntax", QWebEngineUrlScheme)
            try:
                scheme.setSyntax(syntax.Host)
            except Exception:
                pass
            QWebEngineUrlScheme.registerScheme(scheme)
        record(7, "register custom scheme", True,
               "already registered" if already
               else "registered AFTER app start (Qt docs say to do it before)")
    except Exception as exc:
        record(7, "register custom scheme", False, "%s: %s" % (type(exc).__name__, exc))
        finish()
        return

    class Handler(QWebEngineUrlSchemeHandler):
        def requestStarted(self, job):
            path = job.requestUrl().path() or "/index.html"
            body = MODULE_DEP if path.endswith("dep.js") else MODULE_INDEX
            mime = b"text/javascript" if path.endswith(".js") else b"text/html"
            buf = QBuffer(job)          # parented to the job so it outlives this call
            buf.setData(QByteArray(body))
            buf.open(QBuffer.OpenModeFlag.ReadOnly if hasattr(QBuffer, "OpenModeFlag")
                     else QBuffer.ReadOnly)
            job.reply(QByteArray(mime), buf)

    try:
        reused = SCHEME_NAME in SCHEME_HANDLERS
        if reused:
            # Qt refuses to install a second handler over an existing one, so
            # installing again would leave the FIRST one in charge. Keep using
            # it rather than creating an orphan.
            handler = SCHEME_HANDLERS[SCHEME_NAME]
        else:
            handler = Handler()
            SCHEME_HANDLERS[SCHEME_NAME] = handler   # outlives STATE.clear()
            QWebEngineProfile.defaultProfile().installUrlSchemeHandler(
                QByteArray(SCHEME_NAME), handler)
        STATE["handler"] = handler
        record(7, "install scheme handler", True,
               "reused the handler installed by an earlier run" if reused else "")
    except Exception as exc:
        record(7, "install scheme handler", False, "%s: %s" % (type(exc).__name__, exc))
        finish()
        return

    view = STATE["view"]

    def scheme_timeout():
        """A load that never comes back must not pass as silence."""
        if STATE.get("stage7_done"):
            return
        STATE["stage7_done"] = True
        record(7, "load page over custom scheme", False,
               "no loadFinished within 6 s - the request never reached a "
               "live handler")
        finish()

    QTimer.singleShot(6000, scheme_timeout)

    def on_loaded(ok):
        try:
            view.loadFinished.disconnect(on_loaded)
        except Exception:
            pass
        if STATE.get("stage7_done"):
            return
        STATE["stage7_done"] = True
        record(7, "load page over custom scheme", bool(ok))
        if not ok:
            finish()
            return

        def read_back():
            def on_js(raw):
                try:
                    mod = json.loads(raw) if raw else {}
                except Exception:
                    mod = {}
                record(7, "ES module import over custom scheme",
                       bool(mod.get("imported")),
                       "" if mod.get("imported")
                       else "module graph did not resolve - a bundled vgplot app "
                            "would not load this way")
                finish()
            view.page().runJavaScript(
                "JSON.stringify(window.__mod || {})", on_js)
        QTimer.singleShot(400, read_back)

    view.loadFinished.connect(on_loaded)
    view.load(QUrl("%s://app/index.html" % SCHEME_NAME.decode()))


# ------------------------------------------------------------------ verdict

def _timeout():
    if STATE.get("done"):
        return
    print("")
    print("    !! timed out after %d ms - the stages above are all that ran."
          % TIMEOUT_MS)
    print("    A stage that neither passed nor failed is a stage whose callback")
    print("    never fired, which is itself a failure worth reporting.")
    finish()


def finish():
    if STATE.get("done"):
        return
    STATE["done"] = True

    _restore_message_handler()
    fails = [r for r in RESULTS if r[2] is False]
    passes = [r for r in RESULTS if r[2] is True]
    seen = {label for _s, label, _ok, _d in RESULTS}
    never_ran = [name for name in EXPECTED if name not in seen]
    engine = STATE.get("engine", {})

    print("")
    print("=" * 68)
    print("VERDICT")
    print("=" * 68)

    def got(label):
        for _, name, ok, _detail in RESULTS:
            if name == label:
                return ok
        return None

    channel_ok = all(got(x) for x in ("import QWebChannel",
                                      "QWebChannel construct in JS",
                                      "JS -> Python slot with return value",
                                      "Python -> JS signal"))
    modules_ok = bool(got("ES module import over custom scheme"))

    if got("import QWebEngineView") is False:
        print("  NO WEBENGINE. The bindings are not importable in this install.")
        print("  The Mosaic view plane is not available here; a native Qt view")
        print("  plane (matplotlib) is the only option on this machine.")
    elif channel_ok and modules_ok:
        print("  FULLY USABLE. View, QWebChannel both ways with return values,")
        print("  and a bundled ES-module app over a custom scheme all work.")
        print("  The architecture in docs/mosaic-qgis-plugin-architecture.md")
        print("  can be implemented as written on this machine.")
    elif channel_ok and got("ES module import over custom scheme") is None:
        print("  INCOMPLETE. The bridge works, but the custom-scheme stage")
        print("  never reported - see DID NOT RUN below. On a re-run in one")
        print("  QGIS process that is expected; restart QGIS and run once.")
    elif channel_ok and not modules_ok:
        print("  PARTIAL. The bridge works but a bundled ES-module app does not")
        print("  load over a custom scheme. Either register the scheme earlier")
        print("  than a plugin can, or serve the bundle over a local HTTP")
        print("  server (the restConnector fallback, arch notes section 6).")
    else:
        print("  PARTIAL / BROKEN. WebEngine imports but the pieces the design")
        print("  needs do not all work. See the FAIL lines above.")

    print("")
    print("  %d passed, %d failed, %d never ran"
          % (len(passes), len(fails), len(never_ran)))
    for _stage, label, _ok, detail in fails:
        print("    FAIL: %s%s" % (label, (" - " + detail) if detail else ""))
    for label in never_ran:
        print("    DID NOT RUN: %s" % label)
    if never_ran:
        print("    (a check that never ran is an unanswered question, not a"
              " negative)")

    if QT_MESSAGES:
        print("")
        print("  Qt said:")
        for text in QT_MESSAGES[-12:]:
            print("    %s" % text)

    print("")
    print("  Paste this line into the issue / docs:")
    try:
        from qgis.PyQt.QtCore import QT_VERSION_STR, PYQT_VERSION_STR
        qt = "Qt %s / PyQt %s" % (QT_VERSION_STR, PYQT_VERSION_STR)
    except Exception:
        qt = "Qt ?"
    try:
        from qgis.core import Qgis
        qgis_version = Qgis.QGIS_VERSION
    except Exception:
        qgis_version = "?"
    # Name the failures inline: a bare "channel=NO" throws away the one
    # piece of information needed to tell a broken build from a broken probe.
    failed_names = "+".join(label for _s, label, ok, _d in RESULTS if ok is False)

    def flag(label):
        """yes / NO / ? - the third being a check that never reported."""
        value = got(label)
        return "yes" if value else ("NO" if value is False else "?")
    print("    %s | QGIS %s | %s | Python %s | Chromium %s | "
          "webengine=%s channel=%s modules=%s%s"
          % (platform.system(), qgis_version, qt, platform.python_version(),
             engine.get("chromium", "-"),
             "yes" if got("import QWebEngineView") else "NO",
             "yes" if channel_ok else "NO",
             flag("ES module import over custom scheme"),
             (" | failed: " + failed_names) if failed_names
             else (" | never ran: " + "+".join(never_ran) if never_ran else "")))
    print("")
    if not SHOW:
        print("  (The view was hidden. Set SHOW = True at the top to watch it.)")
    print("  Objects are kept alive in test_webengine.STATE - "
          "del it when finished.")


# Every check the probe intends to make. A name missing from RESULTS at the
# end means its stage never ran - which is NOT the same as failing, and
# reporting the two alike is how a silent hang looked like a clean negative.
EXPECTED = (
    "import QWebEngineView", "WebEngine core classes", "import QWebChannel",
    "qwebchannel.js resource", "construct QWebEngineView", "page load (setHtml)",
    "runJavaScript round-trip", "Chromium version", "BigInt",
    "inline ES modules", "dynamic import()", "atob (base64 Arrow IPC decode)",
    "channel ready handshake", "QWebChannel construct in JS",
    "JS -> Python slot", "JS -> Python slot with return value",
    "Python -> JS signal", "register custom scheme", "install scheme handler",
    "load page over custom scheme", "ES module import over custom scheme",
)


def main():
    del RESULTS[:]
    del QT_MESSAGES[:]
    STATE.clear()
    RUNS[0] += 1
    _install_message_handler()
    if RUNS[0] > 1:
        print("")
        print("    NOTE: run %d in this QGIS process. Custom URL schemes are"
              % RUNS[0])
        print("    registered process-wide and cannot be re-registered, so"
              " stage 7")
        print("    is only authoritative on the first run after a QGIS"
              " restart.")
    if not stage1_bindings():
        finish()
        return
    if not stage2_construct():
        finish()
        return
    stage3_load()


main()
