"""Import the panel with a deeply permissive QGIS stub.

Not a substitute for tools/test_offline.py under real QGIS - it proves only
that the module imports and the class is shaped as expected. That is worth
having on its own: an import error in dialog.py means the plugin does not
load at all, which is the worst failure and the easiest to introduce by
editing an import list.
"""
import sys, types

class AnyMeta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        sub = AnyMeta(name, (object,), {})
        setattr(cls, name, sub)
        return sub
    def __or__(cls, other): return cls
    def __ror__(cls, other): return cls
    def __call__(cls, *a, **k):
        obj = object.__new__(cls)
        return obj

class Any(object, metaclass=AnyMeta):
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return Any
    def __call__(self, *a, **k): return Any()
    def __or__(self, other): return self
    def __int__(self): return 0

def make(name):
    sub = AnyMeta(name, (Any,), {})
    return sub

class Stub(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = make(name) if name[:1].isupper() else (lambda *a, **k: Any())
        setattr(self, name, value)
        return value

MODS = ("qgis", "qgis.core", "qgis.gui", "qgis.utils", "qgis.PyQt",
        "qgis.PyQt.QtCore", "qgis.PyQt.QtGui", "qgis.PyQt.QtWidgets",
        "qgis.PyQt.QtSvg", "qgis.PyQt.QtXml", "qgis.PyQt.QtWebChannel",
        "qgis.PyQt.QtWebEngineWidgets")
for name in MODS:
    m = Stub(name); m.__path__ = []
    sys.modules[name] = m
for parent, child in (("qgis", "core"), ("qgis", "gui"), ("qgis", "PyQt"),
                      ("qgis.PyQt", "QtCore"), ("qgis.PyQt", "QtGui"),
                      ("qgis.PyQt", "QtWidgets")):
    setattr(sys.modules[parent], child, sys.modules[parent + "." + child])

import os
# Relative to this file, not an absolute path: this suite was written in a
# Linux container and hardcoded its path there, so it could never pass on the
# Windows machine the plugin is actually developed on.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib
ok = True
for name in ("equis_loader.store", "equis_loader.report_data",
             "equis_loader.tasks", "equis_loader.layer_builder",
             "equis_loader.dialog", "equis_loader.plugin"):
    try:
        importlib.import_module(name)
        print("[PASS] import %s" % name)
    except Exception as exc:
        ok = False
        print("[FAIL] import %s -> %s: %s" % (name, type(exc).__name__, exc))

if ok:
    dialog = sys.modules["equis_loader.dialog"]
    cls = dialog.EquisDialog
    for name in ("_analysis", "_start_store_build", "_on_store_ready",
                 "_on_store_failed", "_discard_store"):
        print("[%s] EquisDialog.%s" % ("PASS" if hasattr(cls, name) else "FAIL", name))
    print("[%s] _analysis is a property"
          % ("PASS" if isinstance(cls.__dict__.get("_analysis"), property) else "FAIL"))

    # 1b.5 rests on the export reading the TABLE, not `_analysis`. The store's
    # canonical 18 columns are not the report's own, so an innocent-looking
    # "why isn't this using the seam?" edit would silently export the wrong
    # columns - no error, just a file that is not the report. Checked as
    # source text because calling it needs a built panel and a real report.
    import inspect
    try:
        source = inspect.getsource(cls._on_export_csv)
    except (AttributeError, OSError):
        source = ""
    if source:
        reads_table = "self._report_table.filtered_rows(" in source
        reads_seam = "self._analysis.filtered_rows(" in source
        print("[%s] export reads _report_table, not _analysis (1b.5)%s"
              % ("PASS" if reads_table and not reads_seam else "FAIL",
                 "" if reads_table and not reads_seam
                 else " - the store does not carry the report's own columns"))
    else:
        print("[FAIL] could not read _on_export_csv to check the 1b.5 invariant")
    for name in ("open_linked_views", "_close_linked_views"):
        print("[%s] EquisDialog.%s"
              % ("PASS" if hasattr(cls, name) else "FAIL", name))

    # The linked views must be torn down on BOTH exits. A Chromium view left
    # behind a hidden panel keeps a process alive; one left past shutdown
    # outlives the plugin, and if it queries the store after the store is
    # closed that is a crash rather than an error.
    for method in ("closeEvent", "shutdown"):
        try:
            body = inspect.getsource(getattr(cls, method))
        except (AttributeError, OSError):
            body = ""
        print("[%s] %s closes the linked views"
              % ("PASS" if "_close_linked_views()" in body else "FAIL", method))

    # Order matters in shutdown: the page queries the store, so the view has
    # to go first.
    try:
        body = inspect.getsource(cls.shutdown)
        ok = (body.index("_close_linked_views()") < body.index("_discard_store()"))
    except (AttributeError, OSError, ValueError):
        ok = False
    print("[%s] shutdown closes the view BEFORE the store" % ("PASS" if ok else "FAIL"))

    tasks = sys.modules["equis_loader.tasks"]
    print("[%s] BuildStoreTask exported"
          % ("PASS" if hasattr(tasks, "BuildStoreTask") else "FAIL"))
sys.exit(0 if ok else 1)
