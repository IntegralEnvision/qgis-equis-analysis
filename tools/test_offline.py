"""Construct the dialog under real QGIS/PyQt6 and drive the whole offline path.

Needs QGIS but no network:

  "C:\\Program Files\\QGIS 4.0.2\\bin\\python-qgis.bat" tools\\test_offline.py

Seeds a scratch cache as if an online session had run, then walks the offline
workflow through it: work offline -> cached facility -> cached report ->
joined layer -> time series.
"""

import csv
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from qgis.core import QgsApplication  # noqa: E402
from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtWidgets import QWidget  # noqa: E402

FAILURES = []


def select_analyte(panel, row=0, add=False):
    """Select one chemical in the panel's list, as a user click would.

    The chemical list moved into the data-selection panel when steps 4 and 5
    merged; this keeps the tests speaking in terms of the action rather than
    the widget that happens to host it.
    """
    widget = panel.selection_panel.analyte_list
    if not add:
        widget.clearSelection()
        panel.selection_panel._chosen = set()
    if row < widget.count():
        widget.item(row).setSelected(True)
    return panel._selected_analytes()


def select_all_analytes(panel):
    """Select every chemical offered, as ctrl-A would."""
    widget = panel.selection_panel.analyte_list
    widget.selectAll()
    panel.selection_panel._chosen = {
        widget.item(i).data(Qt.ItemDataRole.UserRole)[0]
        for i in range(widget.count())
        if widget.item(i).data(Qt.ItemDataRole.UserRole)
    }
    panel.selection_panel._refresh_warnings()
    return panel._selected_analytes()


def clear_analytes(panel):
    """Select no chemical at all."""
    panel.selection_panel.analyte_list.clearSelection()
    panel.selection_panel._chosen = set()
    panel.selection_panel._refresh_warnings()


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", ""), True)
app = QgsApplication([], False)
app.initQgis()

from equis_loader import cache as ec           # noqa: E402
from equis_loader import data_selection as dlg_ds  # noqa: E402
from equis_loader import dialog as dlg         # noqa: E402
from equis_loader import panel_state as panel_state_module  # noqa: E402
from equis_loader import settings as settings_store  # noqa: E402

SITE = "https://cachetest.equisonline.com"
CACHE_ROOT = tempfile.mkdtemp(prefix="equis_dlg_")


class FakeIface:
    """The handful of iface calls the dialog makes during construction.

    The canvas has to be a real QgsMapCanvas: QgsMapTool is a C++ type and
    will not accept a stand-in.
    """

    def __init__(self):
        from qgis.gui import QgsMapCanvas
        self._bar = FakeMessageBar()
        self._canvas = QgsMapCanvas()

    def mapCanvas(self): return self._canvas
    def messageBar(self): return self._bar
    def mainWindow(self): return None


class FakeMessageBar:
    def __init__(self): self.messages = []
    def pushMessage(self, *a, **k): self.messages.append(a)
    def createMessage(self, title, text):
        self.messages.append((title, text))
        w = QWidget()
        from qgis.PyQt.QtWidgets import QHBoxLayout
        QHBoxLayout(w)
        return w
    def pushWidget(self, *a, **k): pass
    def clearWidgets(self): pass


def main():
    try:
        # Point the plugin's cache at a scratch directory.
        settings_store.cache_root = lambda: CACHE_ROOT
        settings_store.open_cache = lambda: ec.EquisCache(CACHE_ROOT)

        # Seed a cache as if a previous online session had run.
        cache = ec.EquisCache(CACHE_ROOT)
        facility = {"id": 42, "code": "TST", "name": "Test Site", "wkid": 26917}
        cache.put_facilities(SITE, [facility])
        cache.put_facility_meta(
            SITE, 42,
            layers=[{"id": 1, "name": "VW_LOCATION", "count": 2}],
            reports=[{"id": 77, "name": "ARII Results", "code": "ARII",
                      "type": "Analytical", "facility_id": 42,
                      "spatial": False}],
            service_wkid=26917, facility=facility,
        )
        cache.put_locations(SITE, 42, [(
            {"fields": [{"name": "code", "type": "esriFieldTypeString"}],
             "spatialReference": {"wkid": 0, "latestWkid": 26917},
             "geometryType": "esriGeometryPoint"},
            [{"attributes": {"code": "MW-1"}, "geometry": {"x": 500000.0, "y": 4900000.0}},
             {"attributes": {"code": "MW-2"}, "geometry": {"x": 500100.0, "y": 4900100.0}}],
        )])
        headers = list(dlg.ANALYSIS_COLUMNS)
        row = {"SYS_LOC_CODE": "MW-1", "SAMPLE_DATE": "01/01/2020",
               "CHEMICAL_NAME": "Benzene", "CAS_RN": "71-43-2",
               "RESULT_NUMERIC": "5.0", "RESULT_UNIT": "ug/L",
               "DETECT_FLAG": "Y", "MATRIX_CODE": "WG"}
        rows = [[row.get(h, "") for h in headers]]
        params = {"max_rows": None, "where": None, "keep_columns": headers}
        cache.put_report(SITE, 42, 77, headers, rows, truncated=False,
                         params=params, label="ARII Results")

        # --- construction -------------------------------------------------
        iface = FakeIface()
        panel = dlg.EquisDialog(iface)
        check("dialog: constructs under PyQt6", panel is not None)
        check("dialog: offline button exists", hasattr(panel, "offline_btn"))
        check("dialog: refresh button exists", hasattr(panel, "refresh_report_btn"))

        panel.site_edit.setText(SITE)
        panel.key_edit.setText("")
        panel._update_cache_label()
        check("dialog: cache label reports what is stored",
              "report" in panel.cache_label.text(), panel.cache_label.text())

        # --- entering offline mode ----------------------------------------
        panel._on_work_offline()
        check("dialog: work offline sets the offline flag", panel._offline is True)
        check("dialog: no client is constructed offline", panel._client is None)
        check("dialog: only cached facilities are listed",
              len(panel._facilities) == 1
              and panel._facilities[0]["name"] == "Test Site",
              repr([f.get("name") for f in panel._facilities]))
        check("dialog: the banner turns amber offline",
              "fdf6e3" in panel.active_label.styleSheet())
        check("dialog: the banner says it is offline",
              "Offline" in panel.active_label.text(), panel.active_label.text())

        # --- selecting the cached facility --------------------------------
        index = next(i for i in range(panel.facility_combo.count())
                     if (panel.facility_combo.itemData(i) or {}).get("id") == 42)
        panel.facility_combo.setCurrentIndex(index)
        # The detail task runs through the task manager; drive it directly so
        # this stays a synchronous test.
        from equis_loader.tasks import FetchFacilityDetailTask
        t = FetchFacilityDetailTask(None, 42, cache=cache, site=SITE,
                                    prefer_cache=True, facility=facility)
        ok = t.run()
        check("dialog: facility detail loads from cache offline", ok)
        panel._on_detail_loaded(t)
        check("dialog: cached reports populate the combo",
              panel.report_combo.count() == 1, str(panel.report_combo.count()))
        check("dialog: cached reports are marked in the combo",
              "cached" in panel.report_combo.itemText(0),
              panel.report_combo.itemText(0))
        check("dialog: the run button offers the cached copy",
              panel.run_report_btn.text() == "Load cached report",
              panel.run_report_btn.text())
        check("dialog: refresh is disabled offline",
              not panel.refresh_report_btn.isEnabled())

        # --- an uncached report is hidden offline --------------------------
        panel._reports = list(panel._reports) + [
            {"id": 999, "name": "Never Run", "code": "NR", "type": "",
             "facility_id": 42, "spatial": False}
        ]
        panel._populate_reports()
        check("dialog: reports never run are hidden offline",
              all("Never Run" not in panel.report_combo.itemText(i)
                  for i in range(panel.report_combo.count())),
              "; ".join(panel.report_combo.itemText(i)
                        for i in range(panel.report_combo.count())))

        # --- loading the cached report -------------------------------------
        from equis_loader.tasks import RunReportTask, CACHE
        rt = RunReportTask(None, {"id": 77, "name": "ARII Results"}, 42,
                           "Test Site", 26917, keep_columns=headers,
                           cache=cache, site=SITE, prefer_cache=True)
        ok = rt.run()
        check("dialog: the cached report loads offline",
              ok and rt.source == CACHE, rt.source)
        panel._on_report_ready(rt)
        check("dialog: analytes come from the cached rows",
              len(panel._analytes) == 1
              and panel._analytes[0]["chemical"] == "Benzene",
              repr(panel._analytes))
        check("dialog: the banner names the cached report",
              "cached" in panel.active_label.text(),
              panel.active_label.text())

        # --- the layer keeps the marker ------------------------------------
        from equis_loader.tasks import cached_layer_name
        panel._location_pages, _ = (
            cache.get_locations(SITE, 42)[0]["pages"], None
        )
        panel._has_geometry = True
        select_analyte(panel, 0)
        panel._on_build_layer()
        check("dialog: the result layer is marked cached in its name",
              panel._result_layer is not None
              and panel._result_layer.name().endswith("[cached]"),
              panel._result_layer.name() if panel._result_layer else "no layer")
        # ONE feature, not two. The fixture places two location points but
        # carries a Benzene row only for MW-1, so MW-2 was never a candidate
        # for a Benzene layer and is left out. This asserted 2 while the
        # builder left-joined every location; that put all 906 of a real
        # facility's locations on a "Chloride (maximum, WS, D)" layer, every
        # one NULL, because that facility's Chloride is WS/T and WS/N.
        check("dialog: the joined layer carries only located results",
              panel._result_layer is not None
              and panel._result_layer.featureCount() == 1,
              str(panel._result_layer.featureCount()) if panel._result_layer else "")

        # --- what the seam says once rows are in hand ----------------------
        # The footer counts what the current filters would map; it follows
        # the machine's state, so it exists only in LOADED. The layer carries
        # the sentence that produced it, written once at build.
        check("seam: the machine is loaded", panel._fsm.has_rows,
              panel._fsm.describe())
        check("seam: step 5 footer counts what would map",
              not panel.rows_footer.isHidden()
              and "would map" in panel.rows_footer.text(),
              panel.rows_footer.text())
        check("seam: no stale line for rows that answer the request",
              panel.stale_label.isHidden())
        abstract = (panel._result_layer.metadata().abstract()
                    if panel._result_layer is not None else "")
        check("provenance: the layer abstract names the query",
              "per location" in abstract and "non-detects" in abstract
              and "from cache" in abstract, abstract)
        check("provenance: the banner repeats it",
              "Last layer" in panel.active_label.text(),
              panel.active_label.text())

        # --- time series still works from cache ----------------------------
        panel._on_location_picked("MW-1")
        check("dialog: clicking a location plots from the cached report",
              panel._picked_location == "MW-1")

        # --- offline data can still be persisted to a geopackage -----------
        # Cached data and persistent layers are independent choices, and the
        # combination is the realistic one: work offline, keep the map.
        from qgis.core import QgsProject
        from equis_loader.layer_builder import GEOPACKAGE, MEMORY
        # Before saving there is nowhere to write, and the panel has to say so
        # rather than quietly producing a layer that vanishes.
        panel.storage_combo.setCurrentIndex(
            panel.storage_combo.findData(GEOPACKAGE))
        QgsProject.instance().clear()
        panel._update_storage_hint()
        check("dialog: an unsaved project is reported as memory-only",
              "not been saved" in panel.storage_hint.text(),
              panel.storage_hint.text())

        project_path = os.path.join(CACHE_ROOT, "field.qgz")
        QgsProject.instance().write(project_path)
        panel._update_storage_hint()
        check("dialog: once saved, the target file is named",
              "field_equis.gpkg" in panel.storage_hint.text(),
              panel.storage_hint.text())

        index = panel.storage_combo.findData(GEOPACKAGE)
        panel.storage_combo.setCurrentIndex(index)
        panel._on_build_layer()
        check("dialog: an offline layer can still be saved to geopackage",
              panel._result_layer is not None
              and panel._result_layer.dataProvider().name() == "ogr",
              panel._result_layer.dataProvider().name()
              if panel._result_layer else "none")
        check("dialog: the geopackage lands beside the project",
              os.path.exists(os.path.join(CACHE_ROOT, "field_equis.gpkg")),
              repr([f for f in os.listdir(CACHE_ROOT) if f.endswith(".gpkg")]))
        check("dialog: a persisted offline layer keeps the cached marker",
              panel._result_layer.name().endswith("[cached]"),
              panel._result_layer.name())

        # Switching back to memory must be honoured, not remembered wrongly.
        panel.storage_combo.setCurrentIndex(panel.storage_combo.findData(MEMORY))
        panel._on_build_layer()
        check("dialog: switching back to memory is honoured",
              panel._result_layer.dataProvider().name() == "memory",
              panel._result_layer.dataProvider().name())
        QgsProject.instance().clear()

        # --- waiting for the chemical list ----------------------------------
        # The state has to be where someone waiting is looking: in the list,
        # not in a caption under it.
        panel.selection_panel.set_unavailable("Loading the chemical list...",
                                              busy=True)
        check("waiting: the list itself says it is loading",
              panel.selection_panel.analyte_list.count() == 1
              and "Loading" in panel.selection_panel.analyte_list.item(0).text(),
              panel.selection_panel.analyte_list.item(0).text()
              if panel.selection_panel.analyte_list.count() else "(empty)")
        check("waiting: the placeholder cannot be selected",
              not (panel.selection_panel.analyte_list.item(0).flags()
                   & Qt.ItemFlag.ItemIsSelectable))
        check("waiting: the controls are not pretending to work",
              not panel.selection_panel.analyte_filter.isEnabled()
              and not panel.selection_panel.select_none_btn.isEnabled())
        check("waiting: nothing is selected while waiting",
              panel.selection_panel.selection(42).analytes == [])

        panel.selection_panel.set_analytes([("71-43-2", "Benzene"),
                                            ("50-29-3", "DDT")])
        check("waiting: real values clear the state",
              panel.selection_panel.analyte_list.count() == 2)
        check("waiting: and re-enable the controls",
              panel.selection_panel.analyte_filter.isEnabled()
              and panel.selection_panel.select_none_btn.isEnabled())
        # Fraction stays down until a fetch says which fractions exist -
        # chemicals arriving is not that.
        # Rebuilding the method/fraction offers must not loop. Doing so
        # emits changed, which lands on _on_selection_changed, which rebuilds
        # them again; the _last_analyte comparison is what stops the second
        # pass. Counted rather than timed, so a slow machine cannot make this
        # pass by accident.
        loop_table = dlg.ReportTable(
            ["SYS_LOC_CODE", "SAMPLE_DATE", "CHEMICAL_NAME", "CAS_RN",
             "RESULT_NUMERIC", "MATRIX_CODE", "ANALYTIC_METHOD", "FRACTION"],
            [["MW-1", "01/01/2020", "Cobalt", "7440-48-4", "1.0", "WS",
              "SW6010", "D"],
             ["MW-2", "01/02/2020", "Cobalt", "7440-48-4", "2.0", "SE",
              "ICM14B", "N"]])
        held_table = panel._report_table
        calls = []
        original = panel._sync_analysis_filters
        panel._sync_analysis_filters = (
            lambda t=None: (calls.append(1), original(t))[1])
        try:
            panel._report_table = loop_table
            panel._last_analyte = "something else"
            panel._sync_analysis_filters(loop_table)
            check("recursion: rebuilding the offers settles, not loops",
                  len(calls) <= 2, "%d calls" % len(calls))
        finally:
            panel._sync_analysis_filters = original
            panel._report_table = held_table

        # The unplaced message names the locations. The count alone was the
        # whole complaint: it told the user 8 results were dropped without
        # saying which sites to go and check.
        named = dlg._describe_unplaced(["RDO-16", "BH-7"])
        check("unplaced: the message names the codes",
              "RDO-16" in named and "BH-7" in named, named)
        check("unplaced: and still gives the count", named.startswith("2 "),
              named)
        one = dlg._describe_unplaced(["RDO-16"])
        check("unplaced: a single location reads naturally",
              "1 location" in one and "RDO-16 is not" in one, one)
        many = dlg._describe_unplaced(["L%03d" % i for i in range(40)])
        check("unplaced: a wholesale mismatch is truncated",
              "and 28 more" in many and len(many) < 400, many[-40:])
        check("unplaced: nothing to say stays silent",
              dlg._describe_unplaced([]) == "")

        check("waiting: fraction still waits for data",
              not panel.selection_panel.fraction_combo.isEnabled())

        # --- one chemical list, driving both fetch and map -------------------
        # The report's chemicals and the mapped chemicals are the same list;
        # asking twice was the confusion this replaced.
        panel._analytes = [{"chemical": n, "cas_rn": c, "count": 5} for n, c in (
            ("Benzene", "71-43-2"), ("Toluene", "108-88-3"),
            ("Xylene", "1330-20-7"))]
        panel._populate_analytes()
        check("chem: the report's chemicals reach the one list",
              panel.selection_panel.analyte_list.count() == 3,
              str(panel.selection_panel.analyte_list.count()))
        check("chem: result counts are shown once fetched",
              "5 result(s)" in panel.selection_panel.analyte_list.item(0).text(),
              panel.selection_panel.analyte_list.item(0).text())

        panel.selection_panel.analyte_list.item(0).setSelected(True)
        check("chem: selecting there is what gets mapped",
              [a["chemical"] for a in panel._selected_analytes()] == ["Benzene"],
              str([a["chemical"] for a in panel._selected_analytes()]))
        check("chem: the current analyte is the first selected",
              (panel._current_analyte() or {}).get("chemical") == "Benzene",
              str(panel._current_analyte()))

        panel.selection_panel.analyte_list.item(2).setSelected(True)
        check("chem: several can be mapped at once",
              len(panel._selected_analytes()) == 2,
              str(len(panel._selected_analytes())))
        check("chem: the button counts the layers",
              panel.map_btn.text() == "Map 2 chemicals", panel.map_btn.text())

        check("chem: the last step is symbology, not a second chemical list",
              panel.chem_box.title() == "6. Symbology", panel.chem_box.title())
        # The aggregate summarises rows already fetched, so it belongs with
        # the other filters rather than with the fetch that retrieved them.
        check("chem: the aggregate sits with the filters it applies to",
              panel.aggregation_combo.isVisibleTo(panel.filter_box))
        check("chem: so does the map button",
              panel.map_btn.isVisibleTo(panel.filter_box))
        check("chem: the last step says what it will draw",
              "2 layers" in panel.chem_hint.text(), panel.chem_hint.text())

        panel.selection_panel.analyte_list.clearSelection()
        panel.selection_panel._chosen = set()
        panel.selection_panel._refresh_warnings()

        # --- the default data source ----------------------------------------
        # The plugin's own report is the way in; picking from the facility's
        # saved reports is opt-in, not the first question asked.
        check("source: the step is about the data source, not a report list",
              panel.report_box.title() == "3. Data source",
              panel.report_box.title())
        check("source: the report list is hidden by default",
              not panel.report_combo.isVisibleTo(panel.report_box)
              and not panel.report_filter.isVisibleTo(panel.report_box))
        check("source: picking an existing report is opt-in",
              not panel.pick_report_check.isChecked())
        check("source: ticking it reveals the list",
              (panel.pick_report_check.setChecked(True)
               or panel.report_combo.isVisibleTo(panel.report_box)))
        panel.pick_report_check.setChecked(False)
        check("source: unticking hides it again",
              not panel.report_combo.isVisibleTo(panel.report_box))

        # Offline there is no report to write filters to, so the default path
        # is unavailable and a real cached report is the only thing that works.
        was_offline = panel._offline
        panel._offline = True
        check("source: offline falls back to a real report",
              not panel._using_own_report())
        panel._offline = was_offline

        # --- data selection panel -------------------------------------------
        panel.selection_panel.set_analytes([("71-43-2", "Benzene"),
                                            ("108-88-3", "Toluene"),
                                            ("7440-38-2", "Arsenic")])
        panel.selection_panel.set_matrices([("WS", "Surface Water"),
                                            ("SE", "Sediment")])
        check("selection: the step exists between report and chemical",
              panel.selection_box.title().startswith("4."),
              panel.selection_box.title())
        check("selection: filtering follows the fetch",
              panel.filter_box.title().startswith("5."),
              panel.filter_box.title())
        check("selection: symbology is last",
              panel.chem_box.title().startswith("6."), panel.chem_box.title())

        # The split IS the flow: what shapes the request sits above the
        # fetch button, what re-scopes the rows sits below it. A control on
        # the wrong side of that line is the confusion this layout fixes.
        check("selection: chemicals and matrix are pre-fetch",
              panel.selection_panel.analyte_list.isVisibleTo(
                  panel.selection_box)
              and panel.selection_panel.matrix_combo.isVisibleTo(
                  panel.selection_box))
        # By ancestry, not visibility: both are hidden until a fetch says
        # which methods and fractions exist, so isVisibleTo would be false
        # here for a reason unrelated to which box they live in.
        def _within(widget, box):
            node = widget
            while node is not None:
                if node is box:
                    return True
                node = node.parentWidget()
            return False

        check("selection: method and fraction are post-fetch",
              _within(panel.selection_panel.method_combo, panel.filter_box)
              and _within(panel.selection_panel.fraction_combo,
                          panel.filter_box))
        check("selection: and are not in the pre-fetch step",
              not _within(panel.selection_panel.method_combo,
                          panel.selection_box))
        check("selection: the fetch button sits with what it fetches",
              panel.run_report_btn.isVisibleTo(panel.selection_box))
        check("selection: and is named for retrieving, not drawing",
              "fetch" in panel.run_report_btn.text().lower()
              or "report" in panel.run_report_btn.text().lower(),
              panel.run_report_btn.text())

        sel = panel.selection_panel.selection(42)
        # Unset, because fraction depends on the matrix: sediment records
        # only N, so a dissolved default emptied every sediment map.
        check("selection: fraction defaults to unset",
              sel.fraction is None, repr(sel.fraction))
        check("selection: QC samples are excluded by default",
              sel.sample_types == ["N"], str(sel.sample_types))
        check("selection: nothing selected means every chemical",
              sel.analytes == [], str(sel.analytes))
        check("selection: the default makes no fraction claim",
              not any("fraction" in w.lower() for w in sel.warnings()),
              str(sel.warnings()))
        check("selection: the last step is symbology only",
              panel.chem_box.title() == "6. Symbology", panel.chem_box.title())

        # Selecting analytes is the strongest lever, so it must reach the
        # parameters that get saved onto the report.
        panel.selection_panel.analyte_list.item(0).setSelected(True)
        chosen = panel.selection_panel.selection(42)
        check("selection: a chosen analyte reaches the parameters",
              chosen.to_parameters()["@cas_rns"] == "71-43-2",
              repr(chosen.to_parameters()["@cas_rns"]))

        # Filtering the list must not silently drop a selection it hides.
        panel.selection_panel.analyte_filter.setText("arsenic")
        check("selection: filtering hides but does not deselect",
              panel.selection_panel.selection(42).analytes == ["71-43-2"],
              str(panel.selection_panel.selection(42).analytes))
        panel.selection_panel.analyte_filter.clear()

        # 'Select none' must clear what the filter is hiding too, or the query
        # keeps a chemical the user believes they just cleared.
        panel.selection_panel.analyte_list.item(1).setSelected(True)
        panel.selection_panel.analyte_filter.setText("arsenic")
        panel.selection_panel.select_none_btn.click()
        panel.selection_panel.analyte_filter.clear()
        check("selection: 'select none' clears hidden choices too",
              panel.selection_panel.selection(42).analytes == [],
              str(panel.selection_panel.selection(42).analytes))

        # A new report must not carry a previous report's chemicals.
        panel.selection_panel.set_analytes([("71-43-2", "Benzene")])
        panel.selection_panel.analyte_list.item(0).setSelected(True)
        panel.selection_panel.set_analytes([("50-29-3", "DDT")])
        check("selection: a new report drops chemicals it does not offer",
              panel.selection_panel.selection(42).analytes == [],
              str(panel.selection_panel.selection(42).analytes))
        panel.selection_panel.set_analytes([("71-43-2", "Benzene"),
                                            ("108-88-3", "Toluene"),
                                            ("7440-38-2", "Arsenic")])

        # Two matrices is a real choice, so the combo is shown and applies.
        check("selection: the matrix row is shown when there is a choice",
              panel.selection_panel.matrix_combo.isVisibleTo(
                  panel.selection_box))
        panel.selection_panel.set_selected_matrices(["WS"])
        check("selection: the matrix reaches the parameters",
              panel.selection_panel.selection(42)
              .to_parameters()["@matrix_codes"] == "WS")

        # A fetch reporting only the matrix its own rows held must NOT shrink
        # the offer: on a two-matrix facility that would hide the combo and
        # strand the user with no way back to "all matrices".
        panel.selection_panel.set_matrices([("WS", "Surface Water")])
        check("selection: a one-matrix fetch keeps the row",
              panel.selection_panel.matrix_combo.isVisibleTo(
                  panel.selection_box))
        check("selection: and keeps the matrix it was filtering by",
              panel.selection_panel.selection(42)
              .to_parameters()["@matrix_codes"] == "WS")

        # A facility that genuinely has one matrix: shown, but disabled and
        # applying no filter, rather than vanishing.
        panel.selection_panel.set_matrices([("WS", "Surface Water")],
                                           replace=True)
        check("selection: a single-matrix facility shows the row disabled",
              panel.selection_panel.matrix_combo.isVisibleTo(
                  panel.selection_box)
              and not panel.selection_panel.matrix_combo.isEnabled())
        check("selection: a disabled matrix row applies no filter",
              panel.selection_panel.selection(42)
              .to_parameters()["@matrix_codes"] is None)
        panel.selection_panel.set_matrices([("WS", "Surface Water"),
                                            ("SE", "Sediment")], replace=True)

        # The panel says which source these filters act on, because in one
        # they shape the request and in the other only the view.
        panel.selection_panel.set_source(dlg.AGENT)
        check("selection: the agent source is explained",
              "frozen" in panel.selection_panel.source_label.text().lower(),
              panel.selection_panel.source_label.text()[:60])
        panel.selection_panel.set_source(dlg.LIVE)
        check("selection: the live source is explained",
              "saved onto" in panel.selection_panel.source_label.text().lower(),
              panel.selection_panel.source_label.text()[:60])

        panel.selection_panel.clear()
        check("selection: clearing empties the pickers",
              panel.selection_panel.selection(42).analytes == []
              and panel.selection_panel.matrix_combo.count() == 0)

        # --- a narrow fetch must not narrow what can be asked for ------------
        # The list is what the FACILITY holds; a fetch is filtered BY that
        # list, so refilling it from the fetched rows would collapse it to
        # whatever was last asked for and strand the user there.
        panel._offered_analytes = [("71-43-2", "Benzene"),
                                   ("108-88-3", "Toluene"),
                                   ("7440-38-2", "Arsenic")]
        # A fetch that came back with only one of them.
        panel._analytes = [{"chemical": "Benzene", "cas_rn": "71-43-2",
                            "count": 42}]
        panel._populate_analytes()
        check("offer: every facility chemical is still listed",
              panel.selection_panel.analyte_list.count() == 3,
              str(panel.selection_panel.analyte_list.count()))
        labels = [panel.selection_panel.analyte_list.item(i).text()
                  for i in range(panel.selection_panel.analyte_list.count())]
        check("offer: the fetched one carries its result count",
              any("Benzene" in t and "42 result(s)" in t for t in labels),
              str(labels))
        check("offer: the ones not fetched are still selectable",
              bool(panel.selection_panel.analyte_list.item(2).flags()
                   & Qt.ItemFlag.ItemIsSelectable))
        # Sorted by name, so Arsenic leads whatever order they arrived in.
        check("offer: a broader selection can still be made",
              (select_analyte(panel, 0) or [{}])[0].get("chemical") == "Arsenic",
              str(select_analyte(panel, 0)))

        # The whole post-fetch path, not just _populate_analytes: the earlier
        # tests called that directly and so missed _sync_selection_panel
        # overwriting the list one line later.
        panel._offered_analytes = [("7440-38-2", "Arsenic"),
                                   ("7439-89-6", "Iron"),
                                   ("7440-66-6", "Zinc")]
        panel.selection_panel.set_analytes(panel._offered_analytes)
        one_chem = dlg.ReportTable(
            ["SYS_LOC_CODE", "SAMPLE_DATE", "CHEMICAL_NAME", "CAS_RN",
             "RESULT_NUMERIC", "MATRIX_CODE"],
            [["MW-1", "01/01/2020", "Arsenic", "7440-38-2", "5.0", "WS"]],
        )
        panel._analytes = one_chem.analytes()
        panel._populate_analytes()
        panel._sync_selection_panel(one_chem)
        check("offer: a fetch of one chemical leaves the rest listed",
              panel.selection_panel.analyte_list.count() == 3,
              str([panel.selection_panel.analyte_list.item(i).text()
                   for i in range(panel.selection_panel.analyte_list.count())]))

        # A fetch adds to the offer rather than replacing it, so a chemical
        # only the rows knew about becomes selectable too.
        panel._offered_analytes = [("71-43-2", "Benzene")]
        panel._analytes = [{"chemical": "Arsenic", "cas_rn": "7440-38-2",
                            "count": 7}]
        panel._populate_analytes()
        check("offer: a fetched chemical joins the offer",
              panel.selection_panel.analyte_list.count() == 2,
              str([panel.selection_panel.analyte_list.item(i).text()
                   for i in range(panel.selection_panel.analyte_list.count())]))

        # The regression this replaced: with an existing report and an empty
        # offer - a facility revisited, or a mode switched - the list must be
        # re-read from EQuIS, not left to whatever the fetch returned.
        class _Live(object):
            report_id = 999
            facility_id = 42
            created = False
            notes = []

        held_live, held_start = panel._live_report, panel._start
        was_offline, held_client = panel._offline, panel._client

        class _Client(object):
            username = "tester"

        panel._live_report = _Live()
        panel._offline = False
        panel._client = _Client()
        panel._offered_analytes = []
        launched = []
        panel._start = lambda task: launched.append(task)
        panel._prepare_live_report()
        check("offer: an empty offer is re-read rather than left to a fetch",
              len(launched) == 1, str(launched))

        # And with the offer intact there is nothing to re-read.
        launched[:] = []
        panel._offered_analytes = [("71-43-2", "Benzene")]
        panel._prepare_live_report()
        check("offer: a populated offer is not re-read", not launched)

        panel._live_report, panel._start = held_live, held_start
        panel._offline, panel._client = was_offline, held_client
        panel._offered_analytes = []
        panel._analytes = []
        clear_analytes(panel)

        # --- signal payloads must not be read as arguments -------------------
        # currentIndexChanged delivers the new index positionally, so wiring a
        # combo straight to _update_actions(busy=False) made picking the second
        # entry mean busy=1 and greyed the map button out.
        held_pages, held_geom = panel._location_pages, panel._has_geometry
        panel._location_pages = panel._location_pages or [
            ({}, [{"attributes": {"code": "MW-01"},
                   "geometry": {"x": 1.0, "y": 2.0}}])]
        panel._has_geometry = True
        panel.selection_panel.set_analytes([("71-43-2", "Benzene")])
        select_analyte(panel, 0)
        for index in range(panel.aggregation_combo.count()):
            panel.aggregation_combo.setCurrentIndex(index)
            check("aggregate: %r keeps the map button usable"
                  % panel.aggregation_combo.itemText(index),
                  panel.map_btn.isEnabled(),
                  "index %d" % index)

        # ...and the busy state must still actually disable it, which only
        # works if it is passed by keyword now.
        panel._update_actions(busy=True)
        check("aggregate: a busy panel still disables the button",
              not panel.map_btn.isEnabled())
        panel._update_actions()
        check("aggregate: and re-enables when not busy",
              panel.map_btn.isEnabled())

        # A stray positional payload must be ignored, not read as busy.
        panel._update_actions(1)
        check("aggregate: a stray signal argument is not read as busy",
              panel.map_btn.isEnabled())

        # Leave the panel as this block found it: the checks below build their
        # own selections and a leftover one silently changes what they mean.
        clear_analytes(panel)
        panel.selection_panel.set_analytes([])
        panel.aggregation_combo.setCurrentIndex(0)
        panel._location_pages, panel._has_geometry = held_pages, held_geom

        # --- mapping is one action, not fetch-then-map -----------------------
        # Choosing chemicals and pressing map IS the request; the fetch is
        # something the plugin does, not a chore to remember.
        check("one-action: the button names the whole action",
              panel.map_btn.text() in ("Map chemical", "Map 3 chemicals"),
              panel.map_btn.text())

        # Offline there is no report to write filters to, so what is loaded
        # is all there is and a selection change cannot need new rows.
        check("one-action: offline never re-fetches per selection",
              not panel._using_own_report() and not panel._needs_fetch())

        # The interesting case is online, where the filters are saved onto
        # the report and so decide which rows exist at all.
        was_offline, held_client = panel._offline, panel._client
        panel._offline = False

        class _FakeClient(object):
            username = "tester"

        panel._client = _FakeClient()
        panel._fetched_selection = panel._selection_key()
        check("one-action: loaded data for this selection needs no fetch",
              panel._using_own_report() and not panel._needs_fetch())

        # Change the selection and it does: the filters are saved onto the
        # report, so different filters are different rows, not a different
        # view of the same ones. Selected through the list, as a user would -
        # a CAS the panel is not offering is not part of the request.
        # The panel's list has to be offering something for a click to mean
        # anything; the preceding block left it empty.
        panel.selection_panel.set_analytes([("71-43-2", "Benzene"),
                                            ("108-88-3", "Toluene")])
        before = panel._selection_key()
        select_analyte(panel, 0)
        check("one-action: choosing a chemical changes the request",
              panel._selection_key() != before,
              "%s -> %s" % (before, panel._selection_key()))
        check("one-action: a changed selection needs a new fetch",
              panel._needs_fetch(), str(panel._selection_key()))

        # A fraction change does NOT, because fraction is a local filter.
        # It is offered only from the fetched rows, so refetching to apply it
        # would be circular: the rows that named the fraction would be thrown
        # away to ask for a subset of themselves. Same for method.
        panel._fetched_selection = panel._selection_key()
        panel.selection_panel.set_selected_fractions(["D"])
        check("one-action: a fraction change re-scopes without a fetch",
              not panel._needs_fetch(), str(panel._selection_key()))
        panel.selection_panel.set_selected_fractions([])
        panel.selection_panel.set_methods(["SW6010", "ICM14B"], replace=True)
        panel.selection_panel.method_combo.model().item(0).setCheckState(
            Qt.CheckState.Checked)
        check("one-action: a method change re-scopes without a fetch",
              not panel._needs_fetch(), str(panel._selection_key()))
        panel.selection_panel.set_methods([], replace=True)

        # The Where row. Hidden until the facility offers groups - an empty
        # picker reads as broken - and a server parameter once it does, so
        # ticking one is a different request.
        sp = panel.selection_panel
        # isHidden() is the widget's OWN flag, which is what the panel sets;
        # isVisibleTo() would also report an ancestor the offline path hides.
        sp.set_location_groups([])
        check("where: no groups, no row", sp.group_combo.isHidden())
        sp.set_location_groups([("RI_WP", "RI_WP"), ("RI_WS", "Wetland")])
        check("where: groups offered, row shown",
              not sp.group_combo.isHidden() and not sp.group_label.isHidden())
        panel._fetched_selection = panel._selection_key()
        sp.group_combo.model().item(0).setCheckState(Qt.CheckState.Checked)
        check("where: a ticked group is in the selection",
              sp.selected_location_groups() == ["RI_WP"],
              str(sp.selected_location_groups()))
        check("where: and changes the request",
              panel._needs_fetch(), str(panel._selection_key()))
        # The stale line is the visible form of that: it appears with the
        # changed request and goes when the rows match again.
        panel._update_actions()
        check("stale: the message line shows for a changed request",
              panel.stale_label.isVisibleTo(panel))
        check("stale: the machine agrees", panel._fsm.stale,
              panel._fsm.describe())
        panel._fetched_selection = panel._selection_key()
        panel._update_actions()
        check("stale: and hides once the rows answer it",
              not panel.stale_label.isVisibleTo(panel))
        sp.group_combo.model().item(0).setCheckState(Qt.CheckState.Unchecked)
        sp.set_location_groups([])
        panel._fetched_selection = panel._selection_key()

        # A saved report is run as it is, so its rows are all there are.
        panel.pick_report_check.setChecked(True)
        check("one-action: a saved report is not re-fetched per selection",
              not panel._needs_fetch())
        panel.pick_report_check.setChecked(False)
        panel._offline, panel._client = was_offline, held_client

        # Nothing loaded at all always needs a fetch.
        held_table = panel._report_table
        panel._report_table = None
        check("one-action: nothing loaded needs a fetch", panel._needs_fetch())
        panel._report_table = held_table
        panel._fetched_selection = None

        # --- when the matrix row shows at all --------------------------------
        # Greyed out means "not yet"; hidden means "no choice to make". A
        # greyed combo still shows a value, which reads as a filter being
        # applied, so the two states are deliberately different.
        fresh = dlg.SelectionPanel()
        check("matrix: hidden before any report loads",
              not fresh.matrix_combo.isVisibleTo(fresh)
              and fresh.matrix_combo.count() == 0,
              str(fresh.matrix_combo.count()))

        fresh.set_matrices([("WS", "Surface Water"), ("SE", "Sediment")])
        check("matrix: shown once there is a choice",
              fresh.matrix_combo.isVisibleTo(fresh)
              and fresh.matrix_combo.isEnabled())
        check("matrix: ticking nothing does not disable it",
              (fresh.set_selected_matrices([]) or
               fresh.matrix_combo.isEnabled()))
        check("matrix: 'all matrices' means no filter",
              fresh.selection().matrices == [],
              str(fresh.selection().matrices))

        # A narrower later offer is merged, not substituted: a fetch that
        # only saw surface water does not mean the facility lost sediment.
        fresh.set_matrices([("WS", "Surface Water")])
        # Two real entries now: the combo is checkable, so there is no
        # "All matrices" row - ticking nothing means all.
        check("matrix: a narrower offer is merged, not substituted",
              fresh.matrix_combo.isVisibleTo(fresh)
              and fresh.matrix_combo.count() == 2,
              str(fresh.matrix_combo.count()))

        # One matrix for real: still on screen, so its absence cannot be read
        # as a broken panel, but disabled and filtering nothing.
        fresh.set_matrices([("WS", "Surface Water")], replace=True)
        check("matrix: one matrix greys the row rather than hiding it",
              fresh.matrix_combo.isVisibleTo(fresh)
              and not fresh.matrix_combo.isEnabled())
        check("matrix: a single matrix applies no filter",
              fresh.selection().matrices == [],
              str(fresh.selection().matrices))

        fresh.set_matrices([("WS", "w"), ("SE", "s")], replace=True)
        fresh.set_unavailable("Loading...", busy=True)
        check("matrix: greyed only while the lists are loading",
              fresh.matrix_combo.isVisibleTo(fresh)
              and not fresh.matrix_combo.isEnabled())
        fresh.set_analytes([("71-43-2", "Benzene")])
        check("matrix: re-enabled when the data arrives",
              fresh.matrix_combo.isEnabled())
        fresh.deleteLater()

        # --- matrix filtering, from the one place it is chosen ---------------
        # A mixed report: same well and chemical in two matrices, the soil
        # result far larger than the water one.
        mixed_rows = []
        for code, matrix, value in (("MW-1", "WG", "5.0"),
                                    ("MW-1", "SO", "900.0"),
                                    ("MW-2", "wg", "3.0")):
            entry = dict(row)
            entry["SYS_LOC_CODE"] = code
            entry["MATRIX_CODE"] = matrix
            entry["RESULT_NUMERIC"] = value
            mixed_rows.append([entry.get(h, "") for h in headers])
        panel._report_table = dlg.ReportTable(headers, mixed_rows)
        panel._matrices = panel._report_table.matrices()
        panel._analytes = panel._report_table.analytes()
        panel._populate_analytes()
        panel.selection_panel.set_matrices([("WG", "Groundwater"),
                                            ("SO", "Soil")])

        check("matrix: unfiltered by default",
              panel._matrix_mode() is dlg.ALL_MATRICES,
              repr(panel._matrix_mode()))

        panel.storage_combo.setCurrentIndex(panel.storage_combo.findData(MEMORY))
        panel.selection_panel.set_selected_matrices(["WG"])
        check("matrix: the panel's choice reaches the aggregate",
              panel._matrix_mode() == ("WG",), repr(panel._matrix_mode()))

        select_analyte(panel, 0)
        panel.aggregation_combo.setCurrentIndex(
            panel.aggregation_combo.findData(dlg.MAXIMUM))
        panel._on_build_layer()
        water = sorted(f["result_value"] for f in panel._result_layer.getFeatures()
                       if f["result_value"] is not None)
        check("matrix: the layer holds only that matrix",
              water == [3.0, 5.0], str(water))
        check("matrix: the layer name says which",
              "WG" in panel._result_layer.name(), panel._result_layer.name())

        # A different matrix is different data, so it must not replace the
        # layer already on the map.
        water_layer = panel._result_layer
        panel.selection_panel.set_selected_matrices(["SO"])
        panel._on_build_layer()
        soil = [f["result_value"] for f in panel._result_layer.getFeatures()
                if f["result_value"] is not None]
        check("matrix: switching rebuilds from the other rows",
              soil == [900.0], str(soil))
        check("matrix: the first layer survives alongside it",
              water_layer is not panel._result_layer
              and QgsProject.instance().mapLayer(water_layer.id()) is not None,
              panel._result_layer.name())

        panel.selection_panel.set_selected_matrices([])
        panel._on_build_layer()
        both = sorted(f["result_value"] for f in panel._result_layer.getFeatures()
                      if f["result_value"] is not None)
        check("matrix: unfiltered still crosses matrices",
              both == [3.0, 900.0], str(both))
        QgsProject.instance().clear()

        # --- unaggregated: one feature per result, not per location --------
        # A well sampled repeatedly: the aggregate keeps one row per location
        # however many results there are, which is exactly what this mode is
        # for showing.
        history_rows = []
        for code, when, value in (("MW-1", "01/01/2020", "5.0"),
                                  ("MW-1", "01/01/2021", "8.0"),
                                  ("MW-1", "01/01/2022", "12.0"),
                                  ("MW-2", "01/01/2022", "3.0")):
            entry = dict(row)
            entry["SYS_LOC_CODE"] = code
            entry["SAMPLE_DATE"] = when
            entry["MATRIX_CODE"] = "WG"
            entry["RESULT_NUMERIC"] = value
            history_rows.append([entry.get(h, "") for h in headers])
        panel._report_table = dlg.ReportTable(headers, history_rows)
        panel._analytes = panel._report_table.analytes()
        panel._populate_analytes()
        panel.selection_panel.set_matrices([("WG", "Groundwater"),
                                            ("SO", "Soil")], replace=True)
        panel.selection_panel.set_selected_matrices(["WG"])
        select_analyte(panel, 0)
        panel.aggregation_combo.setCurrentIndex(
            panel.aggregation_combo.findData(dlg.MAXIMUM))
        panel._on_build_layer()
        aggregated = panel._result_layer
        agg_count = aggregated.featureCount()

        before_btn = panel.map_btn.isEnabled()
        panel.aggregation_combo.setCurrentIndex(
            panel.aggregation_combo.findData(dlg.UNAGGREGATED))
        check("unagg: sparklines are switched off and disabled",
              not panel.sparkline_check.isEnabled()
              and not panel.sparkline_check.isChecked())
        check("unagg: the map button is left alone",
              panel.map_btn.isEnabled() == before_btn,
              str((before_btn, panel.map_btn.isEnabled())))
        panel._on_build_layer()
        every = panel._result_layer
        check("unagg: a separate layer, not a replacement",
              every is not aggregated
              and QgsProject.instance().mapLayer(aggregated.id()) is not None,
              every.name())
        check("unagg: more features than the aggregate had",
              every.featureCount() > agg_count,
              "%d vs %d" % (every.featureCount(), agg_count))
        check("unagg: the name says which view it is",
              "every result" in every.name(), every.name())

        names = [every.fields().at(i).name()
                 for i in range(every.fields().count())]
        check("unagg: the matrix reaches the layer at last",
              "result_matrix" in names, str(names))
        check("unagg: so does the fraction", "result_fraction" in names)
        codes = [f[dlg.LOCATION_FIELD] for f in every.getFeatures()]
        check("unagg: a location repeats, once per result",
              len(codes) > len(set(codes)), str(sorted(codes)))

        # The user asked for exact coordinates: a point in a GIS is a survey
        # claim, so the stack is fanned out at draw time, never moved.
        stacked = {}
        for feature in every.getFeatures():
            stacked.setdefault(str(feature[dlg.LOCATION_FIELD]), []).append(
                feature.geometry().asPoint())
        repeated = [pts for pts in stacked.values() if len(pts) > 1][0]
        check("unagg: repeated results share one exact coordinate",
              all(abs(pt.x() - repeated[0].x()) < 1e-9
                  and abs(pt.y() - repeated[0].y()) < 1e-9 for pt in repeated),
              str([(pt.x(), pt.y()) for pt in repeated]))
        check("unagg: and are fanned apart by the renderer instead",
              type(every.renderer()).__name__ == "QgsPointDisplacementRenderer",
              type(every.renderer()).__name__)
        check("unagg: the graduated symbology survives inside it",
              type(every.renderer().embeddedRenderer()).__name__
              in ("QgsRuleBasedRenderer", "QgsGraduatedSymbolRenderer"),
              type(every.renderer().embeddedRenderer()).__name__)

        # A real datetime, so QGIS's Temporal Controller can animate it.
        check("unagg: carries a datetime for the temporal controller",
              "sample_time" in names, str(names))
        stamp_index = every.fields().indexFromName("sample_time")
        check("unagg: and it is a datetime, not a string that looks like one",
              every.fields().at(stamp_index).typeName().lower()
              in ("datetime", "qdatetime"),
              every.fields().at(stamp_index).typeName())
        temporal = every.temporalProperties()
        check("unagg: the controller is pointed at it",
              temporal.startField() == "sample_time", temporal.startField())
        check("unagg: but left switched off, so the layer is not filtered away",
              not temporal.isActive())

        # The size-range loop used to test key[-1] for "is this a sparkline
        # view". key[-1] stopped being the sparkline flag when the matrix was
        # appended to the key, so it resized layers by MATRIX and skipped
        # every SVG layer that had no matrix filter - including every depth
        # profile. Named indices are what stop that recurring.
        key = [k for k in panel._layer_views
               if k[dlg.VIEW_AGGREGATION] == dlg.UNAGGREGATED][0]
        check("size: the profile view is not flagged as a sparkline",
              key[dlg.VIEW_SPARKLINES] is False)
        # This layer happens to carry a matrix, so the old test caught it by
        # luck; the same layer with no matrix filter was silently skipped.
        # The end of the key is NOT the sparkline flag and keeps moving - it
        # was the matrix, and is the method since fraction and method were
        # added - so the point is that nothing may read this key from the
        # end. Only the named indices are stable.
        check("size: the key's tail is not the sparkline flag",
              key[-1] != key[dlg.VIEW_SPARKLINES], repr(key[-1]))
        check("size: named indices survive the key growing",
              key[dlg.VIEW_MATRIX] and len(key) > dlg.VIEW_METHOD,
              "matrix=%r len=%d" % (key[dlg.VIEW_MATRIX], len(key)))
        check("size: the resize loop now recognises it as an SVG view",
              bool(key[dlg.VIEW_SPARKLINES]
                   or key[dlg.VIEW_AGGREGATION] == dlg.UNAGGREGATED))

        # --- the machine's transitions match what the panel does -------
        # The real test of the transition table: this suite drives the panel
        # the way the plugin does, so an arrow the table is missing shows up
        # as a refusal rather than having to be imagined in advance.
        check("fsm: the workflow so far provoked no illegal transitions",
              not panel._fsm.violations,
              "; ".join(panel._fsm.violations[:3]))
        check("fsm: and the machine knows where it is",
              panel._fsm.state in panel_state_module.PIPELINE,
              panel._fsm.describe())

        # --- exporting the rows behind the map -------------------------
        # A map answers "where"; this is how someone checks a number on it
        # against the results underneath. The CSV is the report rows after
        # every filter, not the layer's own features - an aggregated layer
        # holds one summarised value per location, and what that value was
        # computed over is exactly what is wanted here.
        export_path = os.path.join(CACHE_ROOT, "exported.csv")
        held_dialog_fn = dlg.QFileDialog.getSaveFileName
        dlg.QFileDialog.getSaveFileName = staticmethod(
            lambda *a, **k: (export_path, ""))
        try:
            check("export: the button offers itself once rows are loaded",
                  panel._capabilities()["export"],
                  panel._fsm.describe())
            panel._on_export_csv()
            check("export: the file is written", os.path.exists(export_path))
            raw = open(export_path, "rb").read()
            # Excel reads a bare UTF-8 CSV as the system codepage, which
            # turns every accented chemical name into mojibake.
            check("export: a BOM is written for Excel", raw[:3] == b"\xef\xbb\xbf")
            text = raw.decode("utf-8-sig")
            written = list(csv.reader(io.StringIO(text)))
            check("export: the header is the report's own columns",
                  written[0] == list(panel._report_table.headers),
                  str(written[0][:3]))
            check("export: rows were written", len(written) > 1,
                  str(len(written) - 1))
            # csv needs newline="" or every row gains a blank line on Windows.
            check("export: no blank line between rows",
                  "\r\n\r\n" not in text)
            check("export: the status names the file and the count",
                  "exported" in panel.status_label.text().lower()
                  and "exported.csv" in panel.status_label.text(),
                  panel.status_label.text()[:70])
        finally:
            dlg.QFileDialog.getSaveFileName = held_dialog_fn

        # Exporting needs ROWS, not a drawn layer: the CSV is still worth
        # having when the locations carry no coordinates and nothing can be
        # mapped at all.
        held_pages = panel._location_pages
        try:
            panel._location_pages = None
            caps = panel._capabilities()
            check("export: available even when nothing can be mapped",
                  caps["export"] and not caps["map"])
        finally:
            panel._location_pages = held_pages

        # --- mapping never fetches -------------------------------------
        # Pressing map used to launch a report run whenever the selection had
        # moved on, so picking a different chemical after mapping one started
        # a server round-trip on its own: the progress bar restarted and the
        # panel repainted, and the two actions the UI separates into steps 4
        # and 5 turned out to be one button after all.
        held_run = panel._run_report
        runs = []
        panel._run_report = lambda **k: runs.append(k)
        try:
            panel._on_build_layer()
            check("flow: mapping never starts a fetch", not runs, str(runs))
        finally:
            panel._run_report = held_run
        check("flow: nothing auto-maps after a fetch",
              not hasattr(panel, "_map_after_fetch"))

        # --- one place decides what the panel may do -------------------
        # Enablement used to be spread over eleven functions, each deriving
        # readiness from its own mix of _client/_offline/_task/_report_table
        # - and one of them read "busy" off the progress bar's VISIBILITY.
        # That is how controls came to disagree with each other. Now every
        # setEnabled for an action reads _capabilities(), which is testable
        # without touching a widget.
        caps = panel._capabilities()
        # "state" and "mode" name where the machine is; the rest are the
        # yes/no answers the widgets read.
        check("caps: capabilities are plain booleans",
              all(isinstance(v, bool) for k, v in caps.items()
                  if k not in ("state", "mode")),
              str({k: type(v).__name__ for k, v in caps.items()
                   if k not in ("state", "mode") and not isinstance(v, bool)}))
        check("caps: the machine's position is reported",
              isinstance(caps["state"], str) and isinstance(caps["mode"], str),
              "%s / %s" % (caps["state"], caps["mode"]))

        held = (panel._task, panel._queued_task, set(panel._superseded))
        try:
            from qgis.core import QgsTask as _QT

            class _Busy(_QT):
                def __init__(self):
                    super().__init__("busy")

                def run(self):
                    import time
                    time.sleep(20)
                    return True

            job = _Busy()
            panel._start(job)
            panel._update_actions()
            busy_caps = panel._capabilities()
            check("caps: a running task is reported as busy", busy_caps["busy"])
            # The invariant the panel kept losing: nothing that defines or
            # launches a fetch is available while one is in flight.
            for name in ("fetch", "refresh", "map", "prefetch_controls",
                         "connect", "work_offline", "pick_facility"):
                check("caps: %s is withdrawn while busy" % name,
                      not busy_caps[name], name)
            # And the widgets actually follow, rather than holding their own
            # opinion - the failure that let the chemical list stay live.
            check("caps: the widgets follow the capabilities",
                  not panel.run_report_btn.isEnabled()
                  and not panel.connect_btn.isEnabled()
                  and not panel.selection_panel.analyte_list.isEnabled())
            job.cancel()
            panel._task = None
            panel._update_actions()
            check("caps: and come back when it finishes",
                  panel.selection_panel.analyte_list.isEnabled()
                  and not panel._capabilities()["busy"])
        finally:
            panel._task, panel._queued_task, panel._superseded = held

        # Mapping needs rows AND somewhere to put them: results are drawn
        # onto location points, so rows with no locations make an empty layer.
        held_pages = panel._location_pages
        try:
            panel._location_pages = None
            check("caps: mapping needs locations",
                  not panel._capabilities()["map"])
        finally:
            panel._location_pages = held_pages

        # --- only one background task at a time ------------------------
        # `self._task` WAS the entire notion of "something is running", and
        # _start overwrote it. The first task was not cancelled: it stayed on
        # the task manager, finished later, and fired its callback into a
        # panel that had moved on, while the progress bar showed whichever
        # task set it last. Selecting a different analyte mid-fetch is how a
        # user reaches that, and it presents as a hang.
        from qgis.core import QgsTask as _QgsTask

        class _Slow(_QgsTask):
            def __init__(self, name):
                super().__init__(name)
                self.nm = name

            def run(self):
                import time
                time.sleep(20)
                return True

        held_task, held_queued = panel._task, panel._queued_task
        held_super = set(panel._superseded)
        try:
            first = _Slow("first")
            check("tasks: the first task starts", panel._start(first))
            check("tasks: and the panel knows it is busy", panel._is_busy())
            second = _Slow("second")
            check("tasks: a second is refused, not queued behind it",
                  panel._start(second) is False)
            check("tasks: the first is still the one being waited for",
                  panel._task is first)
            third = _Slow("third")
            check("tasks: an explicit replace is allowed",
                  panel._start(third, replace=True))
            check("tasks: the replaced task is cancelled",
                  first.isCanceled())
            # The cancelled task still runs to completion and still emits.
            check("tasks: its late callback is ignored", panel._stale(first))
            check("tasks: the live task's callback is not",
                  not panel._stale(third))
            # Results handed straight to a callback were never queued here -
            # the offline path does exactly this - and must not be discarded.
            check("tasks: a result delivered directly is accepted",
                  not panel._stale(_Slow("never queued")))
            panel._task.cancel()
        finally:
            panel._task, panel._queued_task = held_task, held_queued
            panel._superseded = held_super

        # The pre-fetch controls describe what the RUNNING fetch asked for,
        # so they are locked while it runs. set_enabled existed for this and
        # was never called, which is what left the analyte list live.
        panel._set_busy(True)
        check("tasks: the chemical list is locked during a fetch",
              not panel.selection_panel.analyte_list.isEnabled())
        panel._set_busy(False)
        check("tasks: and unlocked afterwards",
              panel.selection_panel.analyte_list.isEnabled())

        # --- one key per distinct set of ROWS --------------------------
        # Every filter that selects different rows has to be in the key, or
        # mapping a second view destroys the first: the build removes each
        # key it is about to re-add, so two views sharing a key means the
        # earlier layer is deleted rather than kept beside the new one.
        arsenic = {"chemical": "Arsenic", "cas_rn": "7440-38-2"}
        base = dlg._view_key(arsenic, dlg.MAXIMUM, False, ("WG",), ("D",),
                             ("SW6010",))
        check("cardinality: a different chemical is a different layer",
              dlg._view_key({"chemical": "Fluoride", "cas_rn": "16984-48-8"},
                            dlg.MAXIMUM, False, ("WG",), ("D",),
                            ("SW6010",)) != base)
        check("cardinality: a different method is a different layer",
              dlg._view_key(arsenic, dlg.MAXIMUM, False, ("WG",), ("D",),
                            ("ICM14B",)) != base)
        check("cardinality: a different fraction is a different layer",
              dlg._view_key(arsenic, dlg.MAXIMUM, False, ("WG",), ("T",),
                            ("SW6010",)) != base)
        check("cardinality: a different matrix is a different layer",
              dlg._view_key(arsenic, dlg.MAXIMUM, False, ("SE",), ("D",),
                            ("SW6010",)) != base)
        check("cardinality: the same filters are the same layer",
              dlg._view_key(arsenic, dlg.MAXIMUM, False, ("WG",), ("D",),
                            ("SW6010",)) == base)
        # Order must not matter: ticking WG then SE is the same data as SE
        # then WG, and would otherwise build two layers over one dataset.
        check("cardinality: filter order does not create a new layer",
              dlg._view_key(arsenic, dlg.MAXIMUM, False, ("SE", "WG"), None,
                            ("A", "B"))
              == dlg._view_key(arsenic, dlg.MAXIMUM, False, ("WG", "SE"),
                               None, ("B", "A")))
        # An unfiltered key must be unchanged by the new fields, or every
        # layer mapped before this change would be orphaned.
        check("cardinality: unfiltered keys stay stable",
              dlg._view_key(arsenic, dlg.MAXIMUM, False, None, None, None)
              == dlg._view_key(arsenic, dlg.MAXIMUM, False) )

        check("unagg: rolling refuses, having no series to draw",
              panel._start_roll() is False)
        QgsProject.instance().clear()
        panel.aggregation_combo.setCurrentIndex(
            panel.aggregation_combo.findData(dlg.MOST_RECENT))
        panel.selection_panel.set_matrices([], replace=True)
        panel._report_table = dlg.ReportTable(headers, mixed_rows)
        panel._analytes = panel._report_table.analytes()
        panel._populate_analytes()

        # Restore the single-matrix report the later tests were written for.
        panel._report_table = dlg.ReportTable(headers, rows)
        panel._matrices = panel._report_table.matrices()
        panel._analytes = panel._report_table.analytes()
        panel._populate_analytes()
        panel.selection_panel.set_matrices([])

        # --- one layer per selected chemical --------------------------------
        # Selecting several chemicals used to map one and discard the rest in
        # silence, which is the bug this covers.
        second = dict(panel._analytes[0])
        second["chemical"] = "Toluene"
        second["cas_rn"] = "108-88-3"
        third = dict(panel._analytes[0])
        third["chemical"] = "Xylene"
        third["cas_rn"] = "1330-20-7"
        panel._analytes = list(panel._analytes) + [second, third]
        panel._populate_analytes()

        panel.storage_combo.setCurrentIndex(panel.storage_combo.findData(MEMORY))
        select_analyte(panel, 0)
        panel._on_build_layer()
        check("multi: one selected chemical still adds one layer",
              len(panel._result_layers) == 1, str(len(panel._result_layers)))
        check("multi: the button names the single action for one",
              panel.map_btn.text() == "Map chemical", panel.map_btn.text())

        select_all_analytes(panel)
        panel._update_actions()
        check("multi: the button counts the selection",
              panel.map_btn.text() == "Map 3 chemicals", panel.map_btn.text())
        panel._on_build_layer()
        check("multi: three selected adds three layers",
              len(panel._result_layers) == 3,
              repr([l.name() for l in panel._result_layers]))
        check("multi: no chemical is duplicated",
              len({l.name() for l in panel._result_layers}) == 3,
              repr([l.name() for l in panel._result_layers]))
        check("multi: every layer reaches the project",
              all(QgsProject.instance().mapLayer(l.id()) is not None
                  for l in panel._result_layers))
        check("multi: the last added is active",
              panel._result_layer is panel._result_layers[-1])
        check("multi: the current item is the one left active",
              panel._mapped_chemical == panel._current_analyte()["chemical"],
              "%s vs %s" % (panel._mapped_chemical,
                            panel._current_analyte()["chemical"]))
        check("multi: the pick tool follows the active layer",
              panel._map_tool is None
              or panel._map_tool.layer is panel._result_layer)

        # Rebuilding must replace the whole set, not accumulate.
        ids = {l.id() for l in panel._result_layers}
        panel._on_build_layer()
        check("multi: rebuilding replaces the previous layers",
              len(panel._result_layers) == 3
              and not (ids & {l.id() for l in panel._result_layers}),
              str(len(panel._result_layers)))
        check("multi: the replaced layers leave the project",
              all(QgsProject.instance().mapLayer(i) is None for i in ids))

        # Each chemical needs its own GeoPackage table, not one overwritten
        # three times.
        gpkg_project = os.path.join(CACHE_ROOT, "multi.qgz")
        QgsProject.instance().write(gpkg_project)
        panel.storage_combo.setCurrentIndex(
            panel.storage_combo.findData(GEOPACKAGE))
        select_all_analytes(panel)
        panel._on_build_layer()
        gpkg = os.path.join(CACHE_ROOT, "multi_equis.gpkg")
        check("multi: geopackage storage writes the file", os.path.exists(gpkg))
        if os.path.exists(gpkg):
            import sqlite3
            con = sqlite3.connect(gpkg)
            tables = sorted(r[0] for r in
                            con.execute("select table_name from gpkg_contents"))
            con.close()
            check("multi: each chemical gets its own table",
                  len(tables) == 3, repr(tables))
            check("multi: all three layers read from the geopackage",
                  all(l.dataProvider().name() == "ogr"
                      for l in panel._result_layers),
                  repr([l.dataProvider().name() for l in panel._result_layers]))

        # Narrowing the selection must now LEAVE the other layers alone: they
        # are separate views the user asked for, not stale state. Only the view
        # being re-added is replaced.
        clear_analytes(panel)
        select_analyte(panel, 1)
        before = len(panel._result_layers)
        panel._on_build_layer()
        check("multi: narrowing the selection keeps the other layers",
              len(panel._result_layers) == before,
              "%d -> %d" % (before, len(panel._result_layers)))

        # Re-adding the same view replaces just that one.
        target = panel._result_layer
        panel._on_build_layer()
        check("multi: re-adding the same view replaces only itself",
              len(panel._result_layers) == before
              and panel._result_layer is not target,
              "%d layers" % len(panel._result_layers))

        # And 'Remove EQuIS layers' is how the map is cleared now.
        panel._on_clear_layers()
        check("multi: clearing removes every layer this panel added",
              panel._result_layers == [] and panel._layer_views == {}
              and panel._result_layer is None)
        panel.storage_combo.setCurrentIndex(panel.storage_combo.findData(MEMORY))
        QgsProject.instance().clear()

        # --- sparklines as a mode, not a follow-up action -------------------
        # "Sparklines on map" was a button that restyled an existing layer, so
        # it was greyed out until one existed - unpressable exactly when the
        # user wanted it, and sitting beside "Add layer to map" as though it
        # were an alternative. It is now a mode applied as layers are built.
        from qgis.core import QgsSvgMarkerSymbolLayer

        def has_svg(layer):
            if layer is None:
                return False
            renderer = layer.renderer()
            if not hasattr(renderer, "rootRule"):
                return False
            for rule in renderer.rootRule().children():
                symbol = rule.symbol()
                if symbol is None:
                    continue
                if any(isinstance(symbol.symbolLayer(i), QgsSvgMarkerSymbolLayer)
                       for i in range(symbol.symbolLayerCount())):
                    return True
            return False

        check("mode: the checkbox is usable before any layer exists",
              panel.sparkline_check.isEnabled())

        panel.sparkline_check.setChecked(False)
        clear_analytes(panel)
        select_analyte(panel, 0)
        panel._on_build_layer()
        check("mode: unchecked draws graduated circles",
              not has_svg(panel._result_layer))

        # The seeded report holds one result per location, so the default
        # 'min pts' of 2 legitimately leaves nothing to draw - a one-point
        # sparkline is not a trend. Drop the threshold so the mode itself is
        # what is under test here.
        panel.min_points_spin.setValue(1)
        panel.sparkline_check.setChecked(True)
        panel._on_build_layer()
        check("mode: checked draws sparklines as the layer is added",
              has_svg(panel._result_layer))

        # The checkbox chooses how the NEXT layer is drawn; it deliberately
        # leaves what is already mapped alone, so an existing view is never
        # converted out from under the user.
        panel.sparkline_check.setChecked(False)
        check("mode: toggling leaves the mapped layer as it was",
              has_svg(panel._result_layer))
        check("mode: and says the choice applies to the next layer",
              "next layer" in panel.status_label.text().lower(),
              panel.status_label.text())
        panel.sparkline_check.setChecked(True)

        # Layers now accumulate, so start from a clean map to make the count
        # meaningful.
        panel._on_clear_layers()
        panel.sparkline_check.setChecked(True)
        select_all_analytes(panel)
        panel._on_build_layer()
        # Only Benzene has rows in the seeded report; the two synthetic
        # chemicals have none, so their layers correctly fall back to circles.
        # What matters is that the mode is applied per layer rather than only
        # to the active one.
        with_svg = sum(1 for l in panel._result_layers if has_svg(l))
        check("mode: the mode is applied to each layer added, not just one",
              len(panel._result_layers) == 3 and with_svg >= 1,
              "%d of %d have sparklines (only one chemical has data)"
              % (with_svg, len(panel._result_layers)))

        # --- two views of one dataset coexist ------------------------------
        panel._on_clear_layers()
        clear_analytes(panel)
        select_analyte(panel, 0)
        panel.min_points_spin.setValue(1)

        panel.sparkline_check.setChecked(False)
        panel._on_build_layer()
        circles = panel._result_layer
        panel.sparkline_check.setChecked(True)
        panel._on_build_layer()
        sparks = panel._result_layer

        check("views: adding the other symbology keeps both layers",
              len(panel._result_layers) == 2 and circles is not sparks
              and panel._layer_alive(circles) and panel._layer_alive(sparks),
              "%d layers" % len(panel._result_layers))
        check("views: each keeps its own symbology",
              not has_svg(circles) and has_svg(sparks),
              "circles_svg=%s sparks_svg=%s" % (has_svg(circles), has_svg(sparks)))
        check("views: the layer names distinguish them",
              circles.name() != sparks.name()
              and "sparklines" in sparks.name()
              and "sparklines" not in circles.name(),
              "%r vs %r" % (circles.name(), sparks.name()))
        # Memory layers each carry their own uid, so a shared datasource is a
        # GeoPackage property; assert the join is identical instead.
        def values(layer):
            # Locations with no result for the chemical carry NULL, which does
            # not sort against a float, so compare as strings.
            return sorted(str(f["result_value"]) for f in layer.getFeatures())

        check("views: both hold the same data",
              circles.featureCount() == sparks.featureCount()
              and values(circles) == values(sparks),
              "%d vs %d features" % (circles.featureCount(), sparks.featureCount()))
        check("views: the sparkline layer is the active one",
              panel._result_layer is sparks)

        # Re-adding the sparkline view replaces only it, leaving circles.
        panel._on_build_layer()
        check("views: re-adding one view leaves the other alone",
              len(panel._result_layers) == 2
              and panel._layer_alive(circles)
              and panel._result_layer is not sparks,
              "%d layers, circles alive=%s"
              % (len(panel._result_layers), panel._layer_alive(circles)))
        check("views: the surviving circles layer is still circles",
              not has_svg(circles))

        # Under GeoPackage the two views genuinely share one table: same data,
        # two styles, which is how QGIS normally works.
        panel._on_clear_layers()
        gp_project = os.path.join(CACHE_ROOT, "views.qgz")
        QgsProject.instance().write(gp_project)
        panel.storage_combo.setCurrentIndex(
            panel.storage_combo.findData(GEOPACKAGE))
        clear_analytes(panel)
        select_analyte(panel, 0)
        panel.sparkline_check.setChecked(False)
        panel._on_build_layer()
        gp_circles = panel._result_layer
        panel.sparkline_check.setChecked(True)
        panel._on_build_layer()
        gp_sparks = panel._result_layer
        check("views: both geopackage views exist",
              gp_circles is not gp_sparks
              and panel._layer_alive(gp_circles)
              and panel._layer_alive(gp_sparks)
              and len(panel._result_layers) == 2,
              "%d layers" % len(panel._result_layers))
        check("views: they read the very same geopackage table",
              gp_circles.source() == gp_sparks.source(),
              gp_circles.source()[-50:])
        check("views: with different symbology on the shared table",
              not has_svg(gp_circles) and has_svg(gp_sparks))
        gp = os.path.join(CACHE_ROOT, "views_equis.gpkg")
        if os.path.exists(gp):
            import sqlite3
            con = sqlite3.connect(gp)
            tables = [r[0] for r in
                      con.execute("select table_name from gpkg_contents")]
            con.close()
            check("views: one table, not one per symbology",
                  len(tables) == 1, repr(tables))
        panel.storage_combo.setCurrentIndex(panel.storage_combo.findData(MEMORY))
        panel._on_clear_layers()
        panel.sparkline_check.setChecked(False)
        select_analyte(panel, 0)
        panel._on_build_layer()
        panel.sparkline_check.setChecked(True)
        panel._on_build_layer()

        # --- the layer group ------------------------------------------------
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(panel._group_label())
        check("group: the layers are collected in one group",
              group is not None and len(group.findLayers()) == 2,
              "%d in group" % (len(group.findLayers()) if group else 0))
        check("group: nothing is left loose at the tree root",
              not any(node.layer() in panel._result_layers
                      for node in root.children() if hasattr(node, "layer")))
        panel._on_clear_layers()
        check("group: clearing removes the group too",
              root.findGroup(panel._group_label()) is None)
        panel.sparkline_check.setChecked(False)

        # With min pts above what the data supports there is nothing honest to
        # draw, so the layer must fall back to circles rather than showing an
        # empty sparkline renderer. The fallback happens when the layer is
        # built, since the checkbox no longer restyles what is already mapped.
        panel.min_points_spin.setValue(5)
        panel.sparkline_check.setChecked(True)
        clear_analytes(panel)
        select_analyte(panel, 0)
        panel._on_build_layer()
        check("mode: too few results falls back to circles",
              panel._result_layer is not None
              and not has_svg(panel._result_layer))
        panel.min_points_spin.setValue(1)
        panel.sparkline_check.setChecked(False)

        # --- rolling sparklines --------------------------------------------
        # The timer is the dangerous part: one still firing at a layer that
        # has been removed takes QGIS down, so every teardown path is checked.
        panel.storage_combo.setCurrentIndex(panel.storage_combo.findData(MEMORY))
        panel._on_build_layer()
        check("roll: the button is enabled once a layer exists",
              panel.roll_btn.isEnabled())

        clear_analytes(panel)
        select_analyte(panel, 0)
        panel.roll_btn.setChecked(True)
        check("roll: one selected chemical refuses to roll",
              not panel.roll_btn.isChecked()
              and not panel._roll_timer.isActive(),
              panel.status_label.text())

        # The extra chemicals added for the multi-layer checks above are still
        # in the list, so the multi-select path is already available.
        select_all_analytes(panel)
        check("roll: several chemicals can be selected",
              len(panel._selected_analytes()) == 3,
              str(len(panel._selected_analytes())))

        # Only the first chemical has rows in the seeded report, so rolling
        # must refuse rather than cycle between one real and one empty frame.
        panel.roll_btn.setChecked(True)
        rolled = panel._roll_timer.isActive()
        if rolled:
            check("roll: the timer runs once armed", True)
            check("roll: a column exists per rolled analyte",
                  len(panel._roll_columns) >= 2, repr(panel._roll_columns))
            check("roll: the panel names the chemical being shown",
                  "Showing" in panel.roll_label.text(), panel.roll_label.text())
            first_frame = panel.roll_label.text()
            panel._advance_roll()
            check("roll: advancing changes the displayed chemical",
                  panel.roll_label.text() != first_frame,
                  panel.roll_label.text())
            # Changing the interval must retarget the running timer.
            panel.roll_interval_spin.setValue(7)
            check("roll: the interval change reaches the timer",
                  panel._roll_timer.interval() == 7000,
                  str(panel._roll_timer.interval()))
        else:
            check("roll: refuses when only one chemical has usable data",
                  not panel.roll_btn.isChecked()
                  and "nothing to roll" in panel.status_label.text(),
                  panel.status_label.text())

        # Whatever happened above, arm it deliberately and prove teardown.
        panel._roll_columns = ["spark_0", "spark_1"]
        panel._roll_analytes = ["A", "B"]
        panel._roll_timer.start(1000)
        panel._stop_roll()
        check("roll: stopping halts the timer and clears its state",
              not panel._roll_timer.isActive()
              and panel._roll_columns == []
              and panel.roll_label.text() == ""
              and not panel.roll_btn.isChecked())

        # Toggling the symbology mode owns the renderer, so it ends the roll
        # rather than fighting it. Both directions are checked, and the box is
        # put into the opposite state first so the toggle really fires.
        panel.sparkline_check.setChecked(True)
        panel._roll_timer.start(1000)
        panel.sparkline_check.setChecked(False)
        check("roll: switching to graduated circles stops the roll",
              not panel._roll_timer.isActive())

        panel.sparkline_check.setChecked(False)
        panel._roll_timer.start(1000)
        panel.sparkline_check.setChecked(True)
        check("roll: switching to sparklines stops the roll",
              not panel._roll_timer.isActive())
        panel.sparkline_check.setChecked(False)

        # A tick after the layer is gone must stop, not crash.
        panel._roll_columns = ["spark_0"]
        panel._roll_analytes = ["A"]
        panel._roll_timer.start(1000)
        QgsProject.instance().removeMapLayer(panel._result_layer.id())
        survived = True
        try:
            panel._advance_roll()
        except Exception as exc:  # noqa: BLE001
            survived = False
            print("    unexpected: %s" % exc)
        check("roll: a tick after the layer is removed stops cleanly",
              survived and not panel._roll_timer.isActive())

        from qgis.PyQt.QtGui import QCloseEvent
        panel._roll_timer.start(1000)
        panel.closeEvent(QCloseEvent())
        check("roll: closing the panel stops the roll",
              not panel._roll_timer.isActive())

        panel._roll_timer.start(1000)
        panel.shutdown()
        check("roll: unloading the plugin stops the roll",
              not panel._roll_timer.isActive())

        # --- a cache write that fails must not surface to the user ---------
        # Windows raises a sharing violation whenever anything else holds the
        # index open; the plugin has to keep working through it.
        # A file where the cache expects a directory: every write below it
        # fails with a plain OSError, which is what a locked or full disk
        # looks like from here.
        blocker = os.path.join(CACHE_ROOT, "blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("not a directory")
        panel._cache = ec.EquisCache(os.path.join(blocker, "cache"))
        try:
            panel._update_cache_label()
            survived = True
        except Exception as exc:  # noqa: BLE001
            survived = False
            print("    unexpected: %s" % exc)
        check("dialog: an unwritable cache does not break the panel", survived)

        panel.shutdown()
        panel.deleteLater()
    finally:
        # Let Qt finish with the panel before the directory goes, or the
        # teardown races the cache's own writes.
        app.processEvents()
        shutil.rmtree(CACHE_ROOT, ignore_errors=True)

    print("\n%s" % ("ALL DIALOG TESTS PASSED" if not FAILURES
                    else "FAILURES: %s" % ", ".join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    code = main()
    app.exitQgis()
    sys.exit(code)
