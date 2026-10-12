r"""Interactive mode, driven through the real panel on the real facility.

Each pick in the single-select list replaces ONE memory layer; so does
every dropdown change after the first; Keep turns it into an ordinary
layer that the next pick leaves alone. The rules themselves live in
panel_state.py and are tested without QGIS in test_state.py - this checks
that the dialog honours them against real layers.

Needs QGIS and the Facility A parquet (not in git):

    "C:\Program Files\QGIS 4.2.1\bin\python-qgis.bat" tools\test_interactive.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.chdir(ROOT)

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


try:
    from qgis.core import QgsApplication
except ImportError as exc:
    print("needs QGIS: %s" % exc)
    print("SKIPPED")
    raise SystemExit(0)

QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", ""), True)
app = QgsApplication([], False)
app.initQgis()

from qgis.core import QgsProject                   # noqa: E402
from qgis.PyQt.QtWidgets import QAbstractItemView  # noqa: E402
from test_offline import FakeIface                 # noqa: E402
from equis_loader.dialog import EquisDialog, UNAGGREGATED  # noqa: E402
from equis_loader import panel_state as st         # noqa: E402
from equis_loader.tasks import FetchManifestTask   # noqa: E402

PUBLISHED = os.path.join("data", "facility_a", "manifest.json")
if not os.path.isfile(PUBLISHED):
    print("no published manifest at %s" % PUBLISHED)
    print("SKIPPED")
    raise SystemExit(0)


class Iface(FakeIface):
    """FakeIface plus the active layer, which the chart follows."""

    def __init__(self):
        FakeIface.__init__(self)
        self._active = None

    def activeLayer(self):
        return self._active

    def setActiveLayer(self, layer):
        self._active = layer
        return True


panel = EquisDialog(Iface())
panel.parquet_edit.setText("data/facility_a")
task = FetchManifestTask("data/facility_a")
task.run()
panel._manifest_task = task
panel._on_manifest_ready(task, "data/facility_a")
check("the facility is loaded", panel._fsm.state == st.LOADED,
      panel._fsm.state)

# The startup auto-connect fires from a timer. A parquet session has no
# ReportTable and no facility list, so it used to look like "nothing loaded"
# and was replaced by a live connection. Recorded rather than run, so this
# never reaches the network.
connects = []
real_connect = panel._on_connect
panel._on_connect = lambda *a: connects.append(a)
panel.site_edit.setText("https://example.invalid")
panel.key_edit.setText("not-a-key")
panel._auto_connect()
check("auto-connect leaves a loaded parquet session alone", not connects,
      "%d connect(s) started" % len(connects))
panel._on_connect = real_connect


def ours():
    """Every layer in the project carrying this plugin's scope stamp."""
    return [layer for layer in QgsProject.instance().mapLayers().values()
            if layer.customProperty("equis/chemical", "")]


def previews():
    return [layer for layer in ours()
            if layer.customProperty("equis/preview", False)]


def pick(name):
    """Pick a chemical and let the preview settle, timing the build."""
    panel.selection_panel.set_selected_analytes([name])
    started = time.perf_counter()
    panel._update_preview()        # what the settle timer would call
    elapsed = (time.perf_counter() - started) * 1000
    print("    status: %s" % panel.status_label.text()[:160])
    return elapsed


# --- arming -------------------------------------------------------------------
panel.selection_panel.set_selected_analytes(["Arsenic", "Lead", "Zinc"])
panel.interactive_check.setChecked(True)
check("the machine is interactive", panel._fsm.interactive)
check("the list goes single-select",
      panel.selection_panel.analyte_list.selectionMode()
      == QAbstractItemView.SelectionMode.SingleSelection)
check("a multi-selection collapses to one chemical",
      len(panel._selected_analytes()) == 1,
      str([a["chemical"] for a in panel._selected_analytes()]))
check("the map button gives way to Keep",
      panel.map_btn.isHidden() and not panel.keep_btn.isHidden())
check("rolling is not offered", not panel._capabilities()["roll"])

# A single fraction, so the aggregated build is not refused for crossing
# dissolved and total - that refusal is tested separately below.
panel.selection_panel.set_selected_fractions(["T"])
# And one matrix: unfiltered, Arsenic is ug/L in water beside mg/kg in soil,
# which the unit gate refuses - correctly, and not what this section tests.
panel.selection_panel.set_selected_matrices(["WS"])

# --- picking draws ------------------------------------------------------------
ms = pick("Arsenic")
first = previews()
check("a pick draws one preview", len(first) == 1, str(len(first)))
print("    preview build: Arsenic %.0f ms" % ms)
if first:
    layer = first[0]
    check("it is a memory layer", layer.providerType() == "memory",
          layer.providerType())
    check("named the way a mapped layer is named",
          "Arsenic" in layer.name() and " - " in layer.name(), layer.name())
    check("it is the active layer the chart follows",
          panel.iface.activeLayer() is layer)
    check("and the layer the tools act on", panel._result_layer is layer)
    check("Keep is offered", panel._capabilities()["keep"])

# The same question again draws nothing new - re-entrant signals ask it.
before = first[0].id() if first else None
panel._on_selection_changed()
panel._update_preview()
check("the same pick again does not rebuild",
      [l.id() for l in previews()] == [before])

ms = pick("Lead")
now = previews()
print("    preview build: Lead %.0f ms" % ms)
check("another pick replaces it - still one preview", len(now) == 1,
      str(len(now)))
check("and it is the new chemical",
      now and now[0].customProperty("equis/chemical") == "Lead",
      now[0].customProperty("equis/chemical") if now else "")
check("the old one is gone from the project",
      QgsProject.instance().mapLayer(before) is None)

# --- every dropdown replaces it ---------------------------------------------
lead_id = now[0].id() if now else None
panel.aggregation_combo.setCurrentIndex(
    panel.aggregation_combo.findData(UNAGGREGATED))
started = time.perf_counter()
panel._update_preview()
print("    preview build: Lead, every result %.0f ms"
      % ((time.perf_counter() - started) * 1000))
now = previews()
check("changing the aggregate replaces the preview",
      len(now) == 1 and now[0].id() != lead_id
      and now[0].customProperty("equis/aggregation") == UNAGGREGATED,
      str([(l.id() == lead_id, l.customProperty("equis/aggregation"))
           for l in now]))
panel.aggregation_combo.setCurrentIndex(0)
panel._update_preview()

lead_id = previews()[0].id() if previews() else None
panel.non_detect_combo.setCurrentIndex(
    (panel.non_detect_combo.currentIndex() + 1)
    % panel.non_detect_combo.count())
panel._update_preview()
check("changing the non-detect rule replaces the preview",
      len(previews()) == 1 and previews()[0].id() != lead_id)

# --- keep ---------------------------------------------------------------------
kept_id = previews()[0].id() if previews() else None
panel._on_keep_preview()
check("Keep leaves no preview behind", previews() == [],
      str([l.name() for l in previews()]))
kept = [l for l in ours() if l.customProperty("equis/chemical") == "Lead"]
check("the kept layer is in the project", len(kept) == 1, str(len(kept)))
check("and tracked as an ordinary view",
      kept and kept[0] in panel._layer_views.values())

# A repeat signal for the question the kept layer answers draws nothing on
# top of it.
panel._on_selection_changed()
panel._update_preview()
check("the kept question does not redraw a duplicate", previews() == [],
      str([l.name() for l in previews()]))

ms = pick("Zinc")
print("    preview build: Zinc %.0f ms" % ms)
check("the next pick draws a new preview", len(previews()) == 1)
check("and the kept layer survives it",
      kept and QgsProject.instance().mapLayer(kept[0].id()) is not None)

# Picking the kept chemical again previews it BESIDE the kept layer rather
# than replacing it: the view key would have matched, and must not decide.
pick("Lead")
leads = [l for l in ours() if l.customProperty("equis/chemical") == "Lead"]
check("a preview never replaces a kept layer of the same view",
      len(leads) == 2 and kept and kept[0] in leads,
      str([(l.name(), bool(l.customProperty("equis/preview", False)))
           for l in leads]))

# --- a refusal takes the preview down ----------------------------------------
# Every fraction ticked: an aggregate across dissolved and total is refused,
# and a preview left up would show the previous pick under this one.
panel.selection_panel.set_selected_fractions(
    [panel.selection_panel.fraction_combo.model().item(i).data(256)
     for i in range(panel.selection_panel.fraction_combo.count())])
panel._update_preview()
fractions = panel._fraction_mode()
if fractions and len(fractions) > 1:
    check("a refused pick leaves no preview up", previews() == [],
          str([l.name() for l in previews()]))
else:
    print("    (could not tick several fractions: %r - skipped)"
          % (fractions,))
panel.selection_panel.set_selected_fractions(["T"])
panel._update_preview()
check("correcting the filter draws again", len(previews()) == 1)

# --- matrices with one unit aggregate, and SAY so -----------------------------
# The unit guard is the one gate (CLAUDE.md, 2026-10-01): two solids in mg/kg
# may be aggregated together, but the status line must state it, since a
# value per location could have come from either.
panel.selection_panel.set_selected_matrices(["SE", "TAILS"])
pick("Arsenic")
status = panel.status_label.text()
check("two solids in one unit draw a preview", len(previews()) == 1, status)
check("and the status says the aggregate spans matrices",
      "across 2 matrices (SE+TAILS)" in status, status)
# Water beside a solid differs in unit, and the unit guard refuses it.
panel.selection_panel.set_selected_matrices(["WS", "SE"])
panel._update_preview()
check("water beside a solid is refused by the unit guard",
      previews() == [] and "more than one unit" in panel.status_label.text(),
      panel.status_label.text())
panel.selection_panel.set_selected_matrices(["WS"])
panel._update_preview()

# --- a source rewritten on disk is refused by name ---------------------------
# Faked by changing what the store remembers, not by touching the real data.
store = panel._report_store
real_stats = dict(store._opened_stats)
some_file = next(iter(real_stats))
store._opened_stats[some_file] = (-1, -1)
panel._preview_key = None
panel._update_preview()
check("a changed source takes the preview down", previews() == [],
      str([l.name() for l in previews()]))
check("and says which file changed and what to do",
      "changed on disk" in panel.status_label.text()
      and "Open the source again" in panel.status_label.text(),
      panel.status_label.text())
store._opened_stats = real_stats
panel._update_preview()
check("once it matches again, picks draw", len(previews()) == 1)

# --- disarming ----------------------------------------------------------------
panel.interactive_check.setChecked(False)
check("switching off removes the preview", previews() == [])
check("the kept layer stays",
      kept and QgsProject.instance().mapLayer(kept[0].id()) is not None)
check("the list is multi-select again",
      panel.selection_panel.analyte_list.selectionMode()
      == QAbstractItemView.SelectionMode.ExtendedSelection)
check("the map button is back",
      not panel.map_btn.isHidden() and panel.keep_btn.isHidden())
check("the machine is not interactive", not panel._fsm.interactive)
check("no illegal transitions along the way", not panel._fsm.violations,
      str(panel._fsm.violations))

print("\n%s" % ("ALL INTERACTIVE TESTS PASSED" if not FAILURES
                else "FAILURES: %s" % ", ".join(FAILURES)))
app.exitQgis()
sys.exit(1 if FAILURES else 0)
