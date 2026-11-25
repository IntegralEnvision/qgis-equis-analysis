r"""The panel's ChemDuck shadow, in QGIS: off by default, logged when on.

Phase 1 of docs/chemduck-adoption-plan.md. tools/test_chem_shadow.py holds
the parts to the fixture under plain Python; this holds the panel's wiring:

- with the setting off, nothing loads and nothing is logged;
- on, a local parquet source loads the shadow in the task manager, and a
  map built through the panel's own button logs a line with the store's
  answer beside ChemDuck's, with every difference a listed one;
- the row scope set in the panel reaches the shadow;
- an http source is not opened, says so, and the log carries no token;
- a load superseded while it ran is closed, and closing the source
  removes the shadow's file.

Its settings and cache go to temporary folders, never the real profile.

    "C:\Program Files\QGIS 4.2.3\bin\python-qgis.bat" tools\test_chem_shadow_ui.py
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + str(detail)) if detail != "" else ""))
    if not condition:
        FAILURES.append(label)


try:
    from qgis.core import QgsApplication
except ImportError as exc:
    print("needs QGIS: %s" % exc)
    print("SKIPPED")
    raise SystemExit(0)

from qgis.PyQt.QtCore import QSettings  # noqa: E402

QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", ""), True)
app = QgsApplication([], False)
app.initQgis()

# After initQgis, which sets its own settings path (test_project_source.py).
SANDBOX = tempfile.mkdtemp(prefix="equis_chem_shadow_")
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, SANDBOX)
where = os.path.normcase(os.path.abspath(QSettings().fileName()))
if not where.startswith(os.path.normcase(os.path.abspath(SANDBOX))):
    print("QSettings writes to %s, not the sandbox; refusing to run" % where)
    raise SystemExit(1)

import chem_fixture as fx                      # noqa: E402
from test_offline import FakeIface             # noqa: E402
from equis_loader import settings as settings_store  # noqa: E402
import equis_loader.dialog as dialog_module    # noqa: E402
from equis_loader.dialog import EquisDialog    # noqa: E402
from equis_loader.tasks import ChemShadowTask, FetchManifestTask  # noqa: E402

CACHE = os.path.join(SANDBOX, "cache")
os.makedirs(CACHE)
settings_store.cache_root = lambda: CACHE


def shadow_files():
    return sorted(n for n in os.listdir(CACHE)
                  if n.startswith("chemduck_shadow_") and n.endswith(".duckdb"))

# What the panel sends to the log, recorded where it is sent. Listening on
# QgsMessageLog.messageReceived heard nothing here: it has a bool overload
# beside (message, tag, level), and the connection is the wrong one's.
LOG = []
_real_log = dialog_module.QgsMessageLog


class _Recorder(object):
    @staticmethod
    def logMessage(message, tag="", *args, **kwargs):
        if tag == "EQuIS ChemDuck":
            LOG.append(message)
        return _real_log.logMessage(message, tag, *args, **kwargs)


dialog_module.QgsMessageLog = _Recorder


def settle():
    QgsApplication.processEvents()
    return LOG


def wait_for(condition, seconds=180):
    deadline = time.time() + seconds
    while not condition() and time.time() < deadline:
        QgsApplication.processEvents()
        time.sleep(0.05)
    return condition()


def open_parquet(panel, address):
    task = FetchManifestTask(address)
    task.run()
    panel._manifest_task = task
    panel._on_manifest_ready(task, address)


def build(panel, chemical, matrix="WG", fraction="T"):
    panel.selection_panel.set_selected_matrices([matrix])
    panel.selection_panel.set_selected_fractions([fraction])
    panel.selection_panel.set_selected_analytes([chemical])
    QgsApplication.processEvents()
    del LOG[:]
    panel._on_build_layer()
    return [m for m in settle() if m.startswith("ChemDuck shadow, %s" % chemical)]


folder = tempfile.mkdtemp(prefix="chem_shadow_ui_")
panel = EquisDialog(FakeIface())
try:
    manifest = fx.write(os.path.join(folder, "facility"))
    facility = os.path.dirname(manifest)

    # --- off by default ----------------------------------------------------
    check("the setting is off by default", not settings_store.load_chemduck_shadow())
    open_parquet(panel, facility)
    check("off: opening a source loads no shadow",
          panel._chem_shadow is None and panel._chem_shadow_task is None
          and not shadow_files())
    lines = build(panel, "Arsenic")
    check("off: building a map logs nothing", not lines and not settle(), LOG[:1])

    # --- on: a local parquet source ----------------------------------------
    QSettings().setValue("%s/%s" % (settings_store.GROUP, settings_store.CHEMDUCK_SHADOW),
                         True)
    check("the setting reads back on", settings_store.load_chemduck_shadow())
    del LOG[:]
    open_parquet(panel, facility)
    loading = panel._chem_shadow_task
    check("on: opening a local source starts the shadow's load in the task manager",
          isinstance(loading, ChemShadowTask))
    early = build(panel, "Arsenic")
    check("a map built while it loads says so, not that the two agree",
          early and "still loading" in early[0], early[:1])
    landed = wait_for(lambda: panel._chem_shadow is not None)
    check("the shadow lands, in a file of its own", landed and len(shadow_files()) == 1,
          [m for m in LOG if "failed" in m][:1])
    check("its landing is logged with what it loaded",
          any("ready:" in m and "results loaded" in m for m in settle()), LOG[-1:])

    for chemical in ("Arsenic", "Lead"):
        lines = build(panel, chemical)
        line = lines[0] if lines else ""
        check("a %s map built through the panel logs the store beside ChemDuck" % chemical,
              " location" in line and "store " in line and "ChemDuck " in line, line)
        check("... and every difference in it is a listed one", "UNEXPLAINED" not in line, line)
    arsenic = build(panel, "Arsenic")
    check("the Arsenic line names the fixture's differences",
          arsenic and "points" in arsenic[0] and "same_date" in arsenic[0], arsenic[:1])

    # --- the row scope reaches it ------------------------------------------
    panel._chem_shadow.set_row_scope()           # make a change observable
    panel._apply_row_scope()
    check("the panel's row scope is set on the shadow by _apply_row_scope",
          panel._chem_shadow._scope == panel._report_store._scope,
          "%s vs %s" % (panel._chem_shadow._scope, panel._report_store._scope))

    # --- a load superseded while it runs ------------------------------------
    # The source opened again while its shadow loads: two loads in flight,
    # each with its own file, and only the second is kept.
    open_parquet(panel, facility)
    first = panel._chem_shadow_task
    open_parquet(panel, facility)
    second = panel._chem_shadow_task
    check("opening again while a shadow loads starts a second load",
          first is not None and second is not None and first is not second)
    both = wait_for(lambda: first.chem is not None or first.error)
    both = both and wait_for(lambda: panel._chem_shadow is not None)
    QgsApplication.processEvents()
    wait_for(lambda: len(shadow_files()) == 1, seconds=30)
    check("both land; the superseded one is closed and its file removed, the "
          "current one kept", both and panel._chem_shadow is second.chem
          and shadow_files() == [os.path.basename(second.path)],
          "%s; first error %r" % (shadow_files(), first.error))

    # --- closing the source closes the shadow -------------------------------
    panel._discard_store()
    check("closing the source closes the shadow and removes its file",
          panel._chem_shadow is None and not shadow_files(), shadow_files())

    # --- an http source is not opened, and the log holds no token -----------
    del LOG[:]
    secret = "sig=NOT-A-REAL-TOKEN"
    panel._start_chem_shadow("https://example.invalid/container/facility?sp=r&" + secret)
    said = " ".join(settle())
    check("an http source is not opened, and the log says why",
          panel._chem_shadow_task is None and "not opened" in said
          and "phase 6" in said, said)
    check("... without the token", secret not in said and "sig=" not in said, said)
finally:
    try:
        panel._discard_store()
    except Exception:                          # noqa: BLE001
        pass
    panel.close()
    shutil.rmtree(folder, ignore_errors=True)
    shutil.rmtree(SANDBOX, ignore_errors=True)

print()
if FAILURES:
    print("FAILED: %s" % ", ".join(FAILURES))
    raise SystemExit(1)
print("The shadow is off until asked for, and logs each map beside the store when on.")
