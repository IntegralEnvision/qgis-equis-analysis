"""Path containment for the custom scheme, and the registration hazards.

`resolve()` is the only thing deciding what the page may read off disk, so it
gets the hardest tests here. It is deliberately free of Qt, which is why most
of this suite runs anywhere.

    python tools/test_web_assets.py
    "C:\\Program Files\\QGIS 4.2.1\\bin\\python-qgis.bat" tools\\test_web_assets.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


# resolve() and mime_for() need no Qt, but importing the module does. Stub
# just enough when QtWebEngine is absent so the containment rules can still
# be checked on a plain interpreter.
try:
    from qgis.PyQt.QtCore import QByteArray  # noqa: F401
    HAVE_QT = True
except ImportError:
    HAVE_QT = False
    import types

    core = types.ModuleType("qgis.PyQt.QtCore")
    for name in ("QBuffer", "QByteArray", "QUrl"):
        setattr(core, name, type(name, (object,), {
            "__init__": lambda self, *a, **k: None}))
    for name in ("qgis", "qgis.PyQt"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["qgis.PyQt.QtCore"] = core
    sys.modules["qgis.PyQt"].QtCore = core

from equis_loader import web_assets as wa  # noqa: E402

print("Qt available: %s\n" % HAVE_QT)

root = tempfile.mkdtemp()
os.makedirs(os.path.join(root, "assets"), exist_ok=True)
for relative in ("index.html", os.path.join("assets", "app.js")):
    with open(os.path.join(root, relative), "w", encoding="utf-8") as handle:
        handle.write("x")
# A file OUTSIDE the root, which nothing served should ever reach.
outside = os.path.join(os.path.dirname(root), "secret.txt")
with open(outside, "w", encoding="utf-8") as handle:
    handle.write("not for the page")

# --- what should resolve ----------------------------------------------------
for path, expected in (("/index.html", "index.html"),
                       ("index.html", "index.html"),
                       ("/", "index.html"),
                       ("", "index.html"),
                       ("/assets/app.js", os.path.join("assets", "app.js")),
                       ("/index.html?v=2", "index.html"),
                       ("/index.html#top", "index.html")):
    got = wa.resolve(root, path)
    want = os.path.join(root, expected)
    check("resolves %r" % path, got == want, "%s" % got)

# --- what must NOT ----------------------------------------------------------
# The page is the one component that could be asked to fetch an arbitrary
# path, so traversal has to fail closed rather than be sanitised into
# something plausible.
for path in ("/../secret.txt", "/../../secret.txt",
             "/assets/../../secret.txt", "/..%2fsecret.txt",
             "/./../secret.txt"):
    got = wa.resolve(root, path)
    escaped = got is not None and os.path.abspath(got) == os.path.abspath(outside)
    check("refuses traversal %r" % path, not escaped, str(got))

# A sibling directory whose name merely starts with the root's. startswith()
# would let this through; commonpath does not.
sibling = root + "evil"
os.makedirs(sibling, exist_ok=True)
with open(os.path.join(sibling, "x.txt"), "w", encoding="utf-8") as handle:
    handle.write("nope")
got = wa.resolve(root, "/../%s/x.txt" % os.path.basename(sibling))
check("refuses a sibling root with a shared prefix",
      got is None or not os.path.abspath(got).startswith(os.path.abspath(sibling)),
      str(got))

# --- content types ----------------------------------------------------------
for path, expected in (("a/b.html", b"text/html"), ("x.js", b"text/javascript"),
                       ("x.mjs", b"text/javascript"), ("x.css", b"text/css"),
                       ("x.json", b"application/json"),
                       ("x.wasm", b"application/wasm")):
    check("mime for %s" % path, wa.mime_for(path) == expected,
          str(wa.mime_for(path)))
check("an unknown extension is not guessed as text",
      wa.mime_for("x.weird") == b"application/octet-stream")

# --- the registration hazards, only meaningful with Qt ---------------------
# A QApplication has to exist first: QWebEngineProfile.defaultProfile()
# ABORTS the process without one - a hard exit, no traceback - which is how
# this suite first appeared to produce no output at all.
APP = None
if HAVE_QT and wa.available():
    try:
        from qgis.PyQt.QtWidgets import QApplication
        APP = QApplication.instance() or QApplication(sys.argv)
    except Exception as exc:  # noqa: BLE001
        print("[SKIP] no QApplication available: %r" % (exc,))

if HAVE_QT and wa.available() and APP is not None:
    # install() must refuse rather than abort when the guard is removed.
    check("ensure_scheme succeeds", wa.ensure_scheme() is True)
    # Hazard 1: a scheme cannot be registered twice in a process. Calling
    # again must be a no-op, not an exception, because a plugin reload will
    # do exactly this.
    try:
        again = wa.ensure_scheme()
        check("ensure_scheme is idempotent", again is True)
    except Exception as exc:  # noqa: BLE001
        check("ensure_scheme is idempotent", False, repr(exc))

    # Hazard 2: Qt keeps the FIRST handler installed for a scheme. So a
    # reload must repoint the existing one rather than install a rival that
    # Qt silently ignores while it serves stale files.
    first = wa.install(root)
    second_root = tempfile.mkdtemp()
    second = wa.install(second_root)
    check("install returns the same handler object on a reload",
          first is second, "%r vs %r" % (id(first), id(second)))
    check("install repoints the root instead",
          os.path.abspath(second.root) == os.path.abspath(second_root),
          second.root)

    check("url() is a host-form URL",
          wa.url("index.html").toString() == "equis://app/index.html",
          wa.url("index.html").toString())
else:
    print("[SKIP] scheme registration needs QtWebEngine and a QApplication")

print()
if FAILURES:
    print("FAILED: %s" % ", ".join(FAILURES))
    raise SystemExit(1)
print("Web assets resolve inside the root and nowhere else.")
