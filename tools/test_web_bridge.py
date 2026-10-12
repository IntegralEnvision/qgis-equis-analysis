"""The bridge's Qt shape: handshake, envelope, and what happens on failure.

Runs under real QGIS if it is available, and under a stub otherwise - the
logic being checked is the envelope and the connection indirection, neither of
which needs a live channel. tools/test_webengine.py is what proves the channel
itself carries values; this proves what we put on it.

    python tools/test_web_bridge.py
    "C:\\Program Files\\QGIS 4.2.1\\bin\\python-qgis.bat" tools\\test_web_bridge.py
"""
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from qgis.PyQt.QtCore import QObject  # noqa: F401
    STUBBED = False
except ImportError:
    STUBBED = True
    # Minimal stand-ins. pyqtSlot must return the function unchanged so the
    # slots stay directly callable, and pyqtSignal only needs to exist.
    core = types.ModuleType("qgis.PyQt.QtCore")

    class _Signal(object):
        def __init__(self, *a, **k):
            pass

        def __get__(self, obj, owner=None):
            return self

        def emit(self, *a, **k):
            pass

        def connect(self, *a, **k):
            pass

    core.QObject = type("QObject", (object,),
                        {"__init__": lambda self, parent=None: None})
    core.pyqtSignal = lambda *a, **k: _Signal()
    core.pyqtSlot = lambda *a, **k: (lambda fn: fn)
    for name in ("qgis", "qgis.PyQt", "qgis.PyQt.QtCore"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["qgis.PyQt.QtCore"] = core
    sys.modules["qgis.PyQt"].QtCore = core

from equis_loader.web_bridge import WebBridge  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


try:
    import duckdb
except ImportError as exc:
    print("duckdb is not importable: %s" % exc)
    print("SKIPPED")
    raise SystemExit(0)

print("running %s QGIS\n" % ("WITHOUT" if STUBBED else "under"))


def fixture():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE results AS SELECT i AS row_id, "
                "'MW-' || (i % 4) AS location, i * 2 AS value "
                "FROM range(50) tbl(i)")
    return con


# --- the handshake ----------------------------------------------------------
seen = []
bridge = WebBridge(lambda: None, on_ready=lambda: seen.append(True))
check("ready_seen is false before the page says so", bridge.ready_seen is False)
bridge.ready()
check("ready() sets the flag and calls back",
      bridge.ready_seen is True and seen == [True])

# --- a healthy query --------------------------------------------------------
con = fixture()
bridge = WebBridge(lambda: con)
envelope = json.loads(bridge.query("SELECT * FROM results", "json"))
check("a good query reports ok", envelope.get("ok") is True, str(envelope)[:80])
check("the envelope echoes the kind", envelope.get("kind") == "json")
rows = json.loads(envelope["data"])
check("the data is the rows", len(rows) == 50 and rows[0]["location"] == "MW-0",
      str(rows[:1]))

envelope = json.loads(bridge.query("SELECT * FROM results", "arrow"))
check("arrow answers with base64 in the envelope",
      envelope.get("ok") is True and isinstance(envelope.get("data"), str)
      and len(envelope["data"]) > 100)

# kind is allowed to be empty: the connector should get Arrow by default
# rather than an error, because that is the path charts use.
envelope = json.loads(bridge.query("SELECT 1 AS n", ""))
check("an empty kind defaults to arrow, not an error",
      envelope.get("ok") is True, str(envelope)[:80])

# --- failures are data, never a raise --------------------------------------
# A slot cannot raise across the channel: PyQt logs it and JS receives null,
# which a connector cannot distinguish from an empty result.
envelope = json.loads(bridge.query("SELECT * FROM nope", "json"))
check("a bad query returns ok:false rather than raising",
      envelope.get("ok") is False)
check("the error message survives to the page",
      "nope" in (envelope.get("error") or "").lower(),
      str(envelope.get("error"))[:90])
check("a failed query carries no data key",
      "data" not in envelope, str(sorted(envelope)))

envelope = json.loads(bridge.query("SELECT 1", "nonsense"))
check("an unknown kind is reported, not guessed at",
      envelope.get("ok") is False and envelope.get("error"))

# --- no report loaded -------------------------------------------------------
bridge = WebBridge(lambda: None)
envelope = json.loads(bridge.query("SELECT 1", "json"))
check("no connection is a clear message, not a crash",
      envelope.get("ok") is False and "no report" in envelope.get("error", ""),
      str(envelope.get("error")))


def explode():
    raise RuntimeError("store was discarded")


bridge = WebBridge(explode)
envelope = json.loads(bridge.query("SELECT 1", "json"))
check("a raising connection provider is caught",
      envelope.get("ok") is False
      and "discarded" in (envelope.get("error") or ""),
      str(envelope.get("error")))

# --- the indirection is the point -------------------------------------------
# The panel rebuilds its store on every fetch. A bridge holding a connection
# rather than a callable would keep answering from the report the map no
# longer shows.
current = {"con": fixture()}
bridge = WebBridge(lambda: current["con"])
first = json.loads(bridge.query("SELECT count(*) AS n FROM results", "json"))
replacement = duckdb.connect(":memory:")
replacement.execute("CREATE TABLE results AS SELECT 1 AS row_id, "
                    "'X' AS location, 9 AS value")
current["con"] = replacement
second = json.loads(bridge.query("SELECT count(*) AS n FROM results", "json"))
check("the bridge follows the current store, not the one it started with",
      json.loads(first["data"])[0]["n"] == 50
      and json.loads(second["data"])[0]["n"] == 1,
      "%s then %s" % (first["data"], second["data"]))

# --- logging must never take the page down ---------------------------------
try:
    bridge.log("warning", "something the page wants recorded")
    bridge.log(None, "a level the page forgot to send")
    logged = True
except Exception as exc:  # noqa: BLE001
    logged = False
    print("   log raised: %r" % (exc,))
check("log tolerates a missing level", logged)

# --- the brush comes back as data ------------------------------------------
seen = []
bridge = WebBridge(lambda: con, on_brush=seen.append)
bridge.brushed(json.dumps({"from": 2.4, "to": 88,
                           "locations": ["MW-1", "MW-2"]}))
check("a brush reaches the handler as parsed JSON",
      len(seen) == 1 and seen[0]["from"] == 2.4
      and seen[0]["locations"] == ["MW-1", "MW-2"], str(seen))

bridge.brushed("")
check("an empty payload means CLEARED, not an error",
      len(seen) == 2 and seen[1] is None, str(seen[-1]))

# Malformed input must not take the channel down: the next brush has to work.
bridge.brushed("{not json")
check("a malformed brush is ignored rather than raising", len(seen) == 2)
bridge.brushed(json.dumps({"from": 1, "to": 2}))
check("the channel still works after a malformed brush", len(seen) == 3)


def angry(_brush):
    raise RuntimeError("the map handler is broken")


bridge = WebBridge(lambda: con, on_brush=angry)
try:
    bridge.brushed(json.dumps({"from": 1, "to": 2}))
    survived = True
except Exception:  # noqa: BLE001
    survived = False
check("a raising brush handler does not break the channel", survived)

# --- reachable the way the CHANNEL reaches it ------------------------------
# QWebChannel does not call a slot as Python; it goes through Qt's metaobject.
# A method that is callable from Python but not registered as a slot - a
# missing or mistyped @pyqtSlot - would pass every test above and be invisible
# to the page. Only meaningful under real QGIS.
if not STUBBED:
    from qgis.PyQt.QtCore import QMetaObject, Q_ARG, Q_RETURN_ARG

    con = fixture()
    bridge = WebBridge(lambda: con)
    meta = bridge.metaObject()
    exposed = {bytes(meta.method(i).methodSignature()).decode()
               for i in range(meta.methodOffset(), meta.methodCount())}
    for signature in ("query(QString,QString)", "ready()",
                      "log(QString,QString)", "datasetChanged()",
                      "brushed(QString)"):
        check("Qt sees %s" % signature, signature in exposed,
              str(sorted(exposed)))
    returned = QMetaObject.invokeMethod(
        bridge, "query", Q_RETURN_ARG(str),
        Q_ARG(str, "SELECT count(*) AS n FROM results"), Q_ARG(str, "json"))
    check("query returns a value through a Qt metacall",
          isinstance(returned, str)
          and json.loads(json.loads(returned)["data"])[0]["n"] == 50,
          str(returned)[:80])

print()
if FAILURES:
    print("FAILED: %s" % ", ".join(FAILURES))
    raise SystemExit(1)
print("The bridge answers the page as the contract says.")
