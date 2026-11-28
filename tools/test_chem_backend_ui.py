r"""The chemistry switch, in QGIS: ChemDuck answering the panel's hot path.

tools/test_chem_backend.py holds the backend to the fixture under plain
Python; this holds the panel's switch:

- the Chemistry box starts at the saved choice ("off" in a fresh profile);
- set to "ChemDuck answers", a parquet source loads ChemDuck, and once it
  lands `_analysis` is ChemDuck's: a map built through the panel's button
  is named for ChemDuck and draws ChemDuck's values (a field duplicate left
  out where the store counts it), the footer counts and a location's series
  are ChemDuck's;
- a question ChemDuck can't be asked (a method filter) draws the store's
  map, not named for ChemDuck, and the panel says why;
- set back to "Plugin rules", the store answers and ChemDuck is dropped;
- EQUIS_CHEMDUCK=backend in the environment sets the box for the session
  and disables it, saving nothing.

Its settings and cache go to temporary folders, never the real profile.

    "C:\Program Files\QGIS 4.2.3\bin\python-qgis.bat" tools\test_chem_backend_ui.py
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

SANDBOX = tempfile.mkdtemp(prefix="equis_chem_backend_")
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, SANDBOX)
where = os.path.normcase(os.path.abspath(QSettings().fileName()))
if not where.startswith(os.path.normcase(os.path.abspath(SANDBOX))):
    print("QSettings writes to %s, not the sandbox; refusing to run" % where)
    raise SystemExit(1)
os.environ.pop("EQUIS_CHEMDUCK", None)

import chem_fixture as fx                      # noqa: E402
from test_offline import FakeIface             # noqa: E402
from equis_loader import chem as ch            # noqa: E402
from equis_loader import chemduck as cd        # noqa: E402
from equis_loader import settings as settings_store  # noqa: E402
from equis_loader.dialog import EquisDialog    # noqa: E402
from equis_loader.store import ReportStore     # noqa: E402
from equis_loader.tasks import FetchManifestTask  # noqa: E402

CACHE = os.path.join(SANDBOX, "cache")
os.makedirs(CACHE)
settings_store.cache_root = lambda: CACHE


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
    panel._on_build_layer()
    QgsApplication.processEvents()
    return panel._result_layer


def tick_methods(panel, codes):
    """Tick these method codes in the panel's method box, as a click does."""
    from qgis.PyQt.QtCore import Qt
    model = panel.selection_panel.method_combo.model()
    for row in range(model.rowCount()):
        item = model.item(row)
        if item is not None and item.data(Qt.ItemDataRole.UserRole):
            item.setCheckState(Qt.CheckState.Checked
                               if item.data(Qt.ItemDataRole.UserRole) in codes
                               else Qt.CheckState.Unchecked)
    panel.selection_panel._refresh_method_text()
    panel.selection_panel._on_changed()
    QgsApplication.processEvents()


def values(layer):
    return {f["code"]: f["result_value"] for f in layer.getFeatures()
            if f["result_value"] is not None}


def drawn(layer):
    """What a location shows: its value and the date it is from."""
    return {f["code"]: (f["result_value"], f["result_date"]) for f in layer.getFeatures()
            if f["result_value"] is not None}


folder = tempfile.mkdtemp(prefix="chem_backend_ui_")
panel = EquisDialog(FakeIface())
try:
    # Published with its sidecars, so ChemDuck opens through them.
    manifest = fx.write(os.path.join(folder, "facility"))
    facility = os.path.dirname(manifest)
    ch._quiet(cd.publish, manifest, os.path.join(facility, ch.SIDECARS),
              x_col="LONGITUDE", y_col="LATITUDE", quiet=True)
    combo = panel.chemduck_combo

    check("the Chemistry box starts at the plugin's rules in a fresh profile",
          combo.currentData() == "off" and combo.isEnabled())
    open_parquet(panel, facility)
    store_layer = build(panel, "Arsenic")
    store_values = values(store_layer)
    store_drawn = drawn(store_layer)
    check("off: a map is the store's, not named for ChemDuck",
          store_layer is not None and "ChemDuck" not in store_layer.name()
          and isinstance(panel._analysis, ReportStore), store_layer.name())

    # --- ChemDuck answers ------------------------------------------------------
    combo.setCurrentIndex(combo.findData("backend"))
    check("choosing 'ChemDuck answers' saves it", settings_store.load_chemduck_mode() == "backend")
    landed = wait_for(lambda: panel._chem_shadow is not None)
    check("... and loads ChemDuck for the open source", landed)
    analysis = panel._analysis
    check("once it lands, _analysis is ChemDuck's", hasattr(analysis, "take_notes"),
          type(analysis).__name__)

    layer = build(panel, "Arsenic")
    chem_values = values(layer)
    chem_drawn = drawn(layer)
    check("a map built through the panel is named for ChemDuck",
          layer is not None and "ChemDuck" in layer.name(), layer.name() if layer else None)
    expected = {loc: e["value"] for loc, e in analysis.aggregate(
        "Arsenic", how="most_recent", matrix=("WG",), fraction=("T",)).items()
        if e["value"] is not None}
    # Codes that differ only in case (the fixture's Case-Code and CASE-CODE)
    # share one value in any layer, the store's too: the layer builder
    # joins case-insensitively and the later entry wins. Left out here; it
    # is not the switch's (reported with this change).
    folded = {}
    for code in set(expected) | set(chem_values):
        folded.setdefault(code.lower(), set()).add(code)
    unique = {next(iter(c)) for c in folded.values() if len(c) == 1}
    check("... and draws ChemDuck's values (codes unique without case)",
          {k: v for k, v in chem_values.items() if k in unique}
          == {k: v for k, v in expected.items() if k in unique},
          sorted(set(chem_values.items()) ^ set(expected.items()))[:4])
    # Non-reportable only: the panel's own row scope already leaves QC
    # samples out of the store's map by default, so FIELD-DUP agrees here.
    differs = [loc for loc, _what, _rows, expect in fx.CASES
               if "non_reportable" in expect["most_recent"] and loc in unique]
    check("... which differ from the store's (value or date) where ChemDuck leaves out "
          "a non-reportable result",
          differs and all(chem_drawn.get(loc) != store_drawn.get(loc) for loc in differs),
          {loc: (chem_drawn.get(loc), store_drawn.get(loc)) for loc in differs})
    count = analysis.count_for("Arsenic", matrix=("WG",), fraction=("T",))
    check("the footer count is ChemDuck's (point_counts)",
          count == panel._chem_shadow.counts("Arsenic", matrix=("WG",), fraction=("T",))
          and count != panel._report_store.count_for("Arsenic", matrix=("WG",),
                                                     fraction=("T",)), count)
    series = analysis.series("AGREE", "Arsenic", matrix=("WG",), fraction=("T",))
    check("a location's time series is ChemDuck's (point_series)",
          series and all("sample_id" in r for r in series), len(series))

    # --- a question ChemDuck can't be asked -------------------------------------
    panel.iface.messageBar().messages.clear()
    tick_methods(panel, ["6010B"])
    if panel._method_mode():
        layer = build(panel, "Arsenic")
        said = " ".join(str(m) for m in panel.iface.messageBar().messages) \
            + " " + panel.status_label.text()
        check("a method filter draws the store's map, not named for ChemDuck",
              layer is not None and "ChemDuck" not in layer.name(), layer.name())
        check("... and the panel says why", "answered by the plugin's rules" in said,
              said[-200:])
        tick_methods(panel, [])
    else:
        check("the method box takes a tick (for the fallback check)", False)

    # --- back to the plugin's rules ------------------------------------------------
    combo.setCurrentIndex(combo.findData("off"))
    check("'Plugin rules' drops ChemDuck, and the store answers again",
          panel._chem_shadow is None and isinstance(panel._analysis, ReportStore))
    layer = build(panel, "Arsenic")
    again = {k: v for k, v in values(layer).items() if k in unique} if layer else None
    check("... and the next map is the store's (codes unique without case: the case "
          "variants' shared value changes from one store build to the next)",
          layer is not None and "ChemDuck" not in layer.name()
          and again == {k: v for k, v in store_values.items() if k in unique},
          "%s; %s" % (layer.name() if layer else None,
                      sorted(set(values(layer).items()) ^ set(store_values.items()))[:6]
                      if layer else ""))
finally:
    try:
        panel._discard_store()
    except Exception:                          # noqa: BLE001
        pass
    panel.close()

# --- the command-line switch ---------------------------------------------------
os.environ["EQUIS_CHEMDUCK"] = "backend"
try:
    other = EquisDialog(FakeIface())
    check("EQUIS_CHEMDUCK=backend sets the box for the session and disables it",
          other.chemduck_combo.currentData() == "backend"
          and not other.chemduck_combo.isEnabled()
          and settings_store.load_chemduck_mode() == "backend")
    saved = QSettings().value("%s/%s" % (settings_store.GROUP, settings_store.CHEMDUCK_MODE))
    check("... and saves nothing: the saved choice is still the last one made",
          saved == "off", saved)
    other.close()
finally:
    os.environ.pop("EQUIS_CHEMDUCK", None)
    shutil.rmtree(folder, ignore_errors=True)
    shutil.rmtree(SANDBOX, ignore_errors=True)

print()
if FAILURES:
    print("FAILED: %s" % ", ".join(FAILURES))
    raise SystemExit(1)
print("The Chemistry switch puts ChemDuck on the hot path, and says where it can't.")
