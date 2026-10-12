r"""The source toggle: EQuIS live, or a published parquet set.

The panel was built around one source - connect, pick a facility, pick a
report, fetch rows. A parquet source shares none of those steps and arrives
with the rows already extracted, so this checks the two things most likely
to break: that the credential rows swap with the choice, and that a store
with NO ReportTable behind it still satisfies everything `_analysis` feeds.

Needs QGIS:

    "C:\Program Files\QGIS 4.2.1\bin\python-qgis.bat" tools\test_parquet_source_ui.py

Skips itself when the real facility parquet is absent, since the
end-to-end half needs data that is deliberately not in git.
"""
import os
import sys

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

from test_offline import FakeIface            # noqa: E402
from equis_loader import settings as settings_store  # noqa: E402
from equis_loader.dialog import EquisDialog   # noqa: E402
from equis_loader import panel_state as st    # noqa: E402
import equis_loader.report_data as rd         # noqa: E402
from equis_loader.timeseries import HAVE_MATPLOTLIB as HAVE_PLOT  # noqa: E402
from equis_loader.tasks import FetchManifestTask  # noqa: E402

panel = EquisDialog(FakeIface())

check("the panel builds with the source widgets",
      hasattr(panel, "source_combo") and hasattr(panel, "parquet_edit"))
check("two sources are offered", panel.source_combo.count() == 2)

# --- the credential rows follow the choice -----------------------------------
panel.source_combo.setCurrentIndex(
    panel.source_combo.findData(settings_store.SOURCE_LIVE))
check("live shows the API key", panel.key_edit.isVisibleTo(panel))
check("live hides the parquet URL", not panel.parquet_edit.isVisibleTo(panel))
check("live says Connect", panel.connect_btn.text() == "Connect",
      panel.connect_btn.text())

panel.source_combo.setCurrentIndex(
    panel.source_combo.findData(settings_store.SOURCE_PARQUET))
# A key that is never sent is worse than no field: people spend real time on
# it. Both EQuIS rows go, not just the key.
check("parquet hides the API key", not panel.key_edit.isVisibleTo(panel))
check("parquet hides the site", not panel.site_edit.isVisibleTo(panel))
check("parquet shows the URL", panel.parquet_edit.isVisibleTo(panel))
check("parquet says Open", panel.connect_btn.text() == "Open",
      panel.connect_btn.text())
check("offline browsing is hidden for parquet",
      not panel.offline_btn.isVisibleTo(panel))
# The cache holds EQuIS API RESPONSES, keyed by site. Nothing on the parquet
# path reads or writes it - FetchManifestTask does not mention it - but the
# label kept reporting whatever was cached for the site URL still sitting in
# settings, which read as "17.9 MB in 56 report(s)" beside data from none of
# them. A number describing a different dataset is worse than no number.
check("the cache row is hidden for parquet",
      panel.cache_label.isHidden() and panel.clear_cache_btn.isHidden()
      and (panel.cache_row_label is None
           or panel.cache_row_label.isHidden()),
      "label=%s btn=%s row=%s" % (panel.cache_label.isHidden(),
                                  panel.clear_cache_btn.isHidden(),
                                  panel.cache_row_label.isHidden()
                                  if panel.cache_row_label else "n/a"))
check("and carries no stale figure to resurface",
      panel.cache_label.text() == "", repr(panel.cache_label.text()))

# --- an empty URL is refused, not crashed ------------------------------------
panel.parquet_edit.setText("")
panel._on_connect()
check("an empty URL is refused rather than opened",
      panel._report_store is None)

# --- the real facility, end to end -------------------------------------------
# The PUBLISHED manifest owns the name `manifest.json`, so the real
# directory is read as it stands. This used to back up the pipeline
# manifest, overwrite it and restore it afterwards, because the two shared
# a name - exactly the confusion the rename removed.
PUBLISHED = os.path.join("data", "facility_a", "manifest.json")
LOCAL = os.path.join("data", "facility_a", "manifest_local.json")
if not os.path.isfile(PUBLISHED):
    print("\n(no published manifest at %s - run .claude/skills/equis-parquet-export/scripts/publish_manifest.py; "
          "skipping the end-to-end half)" % PUBLISHED)
else:
    panel.parquet_edit.setText("data/facility_a")
    task = FetchManifestTask("data/facility_a")
    task.run()                           # synchronously, for the test
    panel._manifest_task = task
    panel._on_manifest_ready(task, "data/facility_a")

    check("a parquet source opens", panel._report_store is not None)
    check("_analysis answers from the store",
          panel._analysis is panel._report_store)
    # The point of the whole exercise: rows with no ReportTable.
    check("there is no ReportTable behind it", panel._report_table is None)
    if panel._report_store is not None:
        rows = panel._report_store.count()
        check("the whole facility is open", rows == 3452146,
              format(rows, ","))
        check("the analyte picker was filled from the data",
              len(panel._offered_analytes) == 526,
              str(len(panel._offered_analytes)))
        # Export needs the report's OWN columns, which the parquet is.
        # The report's OWN columns, whatever the extract carries. Asserted
        # as "the real ones are there" rather than a count: converting the
        # units added RESULT_UNIT_ORIGINAL and RESULT_VALUE_ORIGINAL, and
        # 69 became 71 without anything being wrong.
        headers = {h.upper() for h in panel._report_store.headers}
        check("export can still produce the report's columns",
              panel._report_store.has_raw
              and {"SYS_LOC_CODE", "CHEMICAL_NAME", "RESULT_NUMERIC",
                   "RESULT_UNIT", "SAMPLE_DATE"} <= headers,
              "%d columns" % len(headers))
        # The audit trail the unit conversion leaves behind, when it ran.
        if "RESULT_UNIT_ORIGINAL" in headers:
            check("a converted extract keeps the lab's own unit and value",
                  "RESULT_VALUE_ORIGINAL" in headers,
                  "RESULT_VALUE_ORIGINAL present: %s"
                  % ("RESULT_VALUE_ORIGINAL" in headers))

    # --- the state machine, which parquet short-circuits --------------------
    # No report to prepare and nothing to fetch, so the panel enters at
    # FACILITY (named from the data) and goes straight to LOADED. Getting
    # this wrong is invisible until the map button will not light up.
    check("the machine reaches LOADED", panel._fsm.state == st.LOADED,
          panel._fsm.state)
    can = panel._capabilities()
    check("rows are recognised with no ReportTable", can["have_rows"])
    check("a parquet source is never stale", not can["stale"])
    check("and never asks for a fetch", not panel._needs_fetch())
    check("so the fetch row is not shown",
          not panel.fetch_section.isVisibleTo(panel))
    check("nor can it be pressed", not can["fetch"] and not can["refresh"]
          and not panel.run_report_btn.isEnabled())
    # The case that made it live: a client left over from an earlier
    # Connect makes the own-report stand-in "chosen", and the button read
    # "Pre-fetch data" and ran an API fetch over the parquet session.
    real_client = panel._client
    panel._client = object()
    try:
        can = panel._capabilities()
        check("a leftover live client still cannot fetch over parquet",
              not can["fetch"] and not can["refresh"])
    finally:
        panel._client = real_client
        can = panel._capabilities()
    # The parquet fills `_location_pages` with the SAME Esri pages the
    # feature service returns, so the ordinary question is the right one -
    # there is no parquet-specific geometry check to ask.
    check("locations are loaded as feature-service pages",
          panel._location_pages is not None and panel._has_geometry)
    if panel._location_pages:
        _set, features = panel._location_pages[0]
        placed = [f for f in features if f.get("geometry")]
        check("906 locations, 705 of them placeable",
              len(features) == 906 and len(placed) == 705,
              "%d / %d" % (len(placed), len(features)))
        # A COUNT PER MATRIX: 272 of 906 locations here carry more than one,
    # and a single total hides that - a location with 13 tailings results
    # and one with 13 groundwater results would look identical.
    names = [f["name"] for f in _set["fields"]]
    check("the layer carries a count per matrix",
          "count_WS" in names and "count_TAILS" in names, str(names))
    spanning = [f for f in features
                if sum(1 for k, v in f["attributes"].items()
                       if k.startswith("count_") and v) > 1]
    check("locations spanning several matrices are distinguishable",
          len(spanning) == 272, str(len(spanning)))
    check("the per-matrix counts sum to the total",
          all(sum(v for k, v in f["attributes"].items()
                  if k.startswith("count_")) == f["attributes"]["results"]
              for f in features))
    check("a feature carries code and a point",
              placed[0]["attributes"].get("code")
              and "x" in placed[0]["geometry"],
              str(placed[0])[:88])
    check("the map has a locations layer",
          panel._locations_layer is not None
          and panel._locations_layer.featureCount() == 906,
          str(panel._locations_layer.featureCount()
              if panel._locations_layer else None))

    # --- the facility comes from the rows ------------------------------------
    check("the facility is named from the data",
          panel.facility_combo.count() == 1,
          str(panel.facility_combo.count()))
    if panel.facility_combo.count():
        check("it is the facility the parquet holds",
              "Facility_A" in panel.facility_combo.itemText(0),
              panel.facility_combo.itemText(0))
    check("the facility cannot be changed by hand",
          not panel.facility_combo.isEnabled())

    # --- the result filters, which only the rows can answer ------------------
    # Fraction is a domain rule, not a nicety: CLAUDE.md forbids aggregating
    # across fractions, so a panel that cannot offer the filter cannot honour
    # it. The live path can only offer these after a fetch; this one has the
    # rows already, so it offers them at open.
    sel = panel.selection_panel
    # `not isHidden()` rather than isVisibleTo(): nothing is realised in a
    # headless test, so isVisibleTo answers about the unshown parent chain
    # rather than about this row. isHidden is the flag set_fractions sets.
    check("the fraction row is shown without any fetch",
          sel._fractions_known and not sel.fraction_combo.isHidden(),
          "known=%s hidden=%s" % (sel._fractions_known,
                                  sel.fraction_combo.isHidden()))
    check("it offers the fractions the parquet holds",
          sel.fraction_combo.count() > 1, str(sel.fraction_combo.count()))
    check("methods are offered too", bool(sel._method_offer),
          "%d" % len(sel._method_offer or []))

    # --- nothing claims a fetch or a report ----------------------------------
    panel._refresh_step_hints(panel._capabilities(), False)
    hint = panel.prefetch_hint.text()
    check("the step hint does not mention fetching",
          "fetch" not in hint.lower() and "report" not in hint.lower(), hint)
    banner = panel.status_label.text()
    check("the banner does not say a report is missing",
          "No report loaded" not in banner, banner[:90])
    check("the banner says what IS loaded",
          "3,452,146" in banner or "parquet" in banner.lower(), banner[:90])

        # --- the plot follows the ACTIVE LAYER, not the picker -------------------
    # Clicking a point on a Manganese layer used to plot whatever the panel's
    # picker said, because the handler read _current_analyte(). The picker
    # moves on after a layer is built; the layer does not. Identify works the
    # way the user expects - the highlighted layer is the subject.
    from qgis.core import QgsVectorLayer, QgsProject      # noqa: E402

    def _scratch(name):
        made = QgsVectorLayer("Point?crs=EPSG:4326&field=code:string",
                              name, "memory")
        QgsProject.instance().addMapLayer(made, False)
        return made

    manganese = _scratch("Manganese layer")
    panel._stamp_scope(manganese,
                       {"chemical": "Manganese", "cas_rn": "7439-96-5"},
                       "maximum")
    arsenic = _scratch("Arsenic layer")
    panel._stamp_scope(arsenic,
                       {"chemical": "Arsenic", "cas_rn": "7440-38-2"},
                       "maximum")
    check("a built layer records what it shows",
          panel._layer_scope(manganese)["chemical"] == "Manganese",
          str(panel._layer_scope(manganese)))
    check("a layer this plugin did not build has no scope",
          panel._layer_scope(_scratch("a basemap")) is None)

    # The picker deliberately disagrees with both layers.
    panel.selection_panel.set_selected_analytes(["Lead"])
    real_active = getattr(panel.iface, "activeLayer", None)
    panel.iface.activeLayer = lambda: manganese
    scope, from_layer = panel._active_scope()
    check("the active layer decides what is plotted",
          scope["chemical"] == "Manganese" and from_layer,
          "%s from_layer=%s" % (scope["chemical"], from_layer))
    panel.iface.activeLayer = lambda: arsenic
    check("switching layers switches the analyte",
          panel._active_scope()[0]["chemical"] == "Arsenic")
    panel.iface.activeLayer = lambda: None
    fallback, from_layer = panel._active_scope()
    check("with no layer of ours it falls back to the picker",
          fallback["chemical"] == "Lead" and not from_layer,
          "%s from_layer=%s" % (fallback["chemical"], from_layer))
    if real_active is not None:
        panel.iface.activeLayer = real_active

    # --- US-6: selecting on the map re-bins the chart -----------------------
    # The link is to layer.selectionChanged, so QGIS's own tools - rectangle,
    # polygon, freehand, select-by-expression - all drive it without the
    # plugin inventing one.
    if panel._locations_layer is not None:
        picked = [feat.id() for feat in panel._locations_layer.getFeatures()][:12]
        panel.selection_panel.set_selected_analytes(["Arsenic"])

        def charted_locations():
            scope = panel._chart_scope()
            return None if scope is None else scope.get("locations")

        check("with nothing selected the chart has no location filter",
              charted_locations() is None, repr(charted_locations()))

        panel._locations_layer.selectByIds(picked)
        selected = charted_locations()
        check("selecting on the map narrows the chart",
              selected is not None and len(selected) == 12,
              str(len(selected) if selected else 0))

        panel._locations_layer.selectByIds(picked[:5])
        narrowed = charted_locations()
        check("narrowing the selection narrows the chart again",
              narrowed is not None and len(narrowed) == 5,
              str(len(narrowed) if narrowed else 0))

        # US-6 asks for this explicitly: brush, select, clear, and you are
        # back where you were rather than in some third state.
        panel._locations_layer.removeSelection()
        check("clearing the selection returns to the unfiltered chart",
              charted_locations() is None, repr(charted_locations()))

        # THE TWO SIDES OF THE FETCH SEAM STAY SEPARATE. The checkbox means
        # "fetch only these" and is gated; the chart follows the map always.
        # One gesture, two meanings, and they must not leak into each other.
        panel._locations_layer.selectByIds(picked)
        check("a map selection does NOT silently narrow a fetch",
              panel._selected_location_codes() == [],
              str(panel._selected_location_codes()[:3]))
        check("while the chart does follow it",
              charted_locations() is not None)
        panel._locations_layer.removeSelection()

# --- a chart must say what it was filtered to ----------------------------
    # CLAUDE.md forbids aggregating across matrices or fractions. A chart
    # that honours that but does not SAY which matrix it honoured invites
    # the reading the rule exists to prevent - and once exported, nothing
    # else carries the context.
    check("an unfiltered chart carries no subtitle",
          panel._scope_label() == "", repr(panel._scope_label()))
    real_matrix, real_fraction = panel._matrix_mode, panel._fraction_mode
    real_method, real_nd = panel._method_mode, panel._non_detect_mode
    try:
        panel._matrix_mode = lambda: ["WS"]
        panel._fraction_mode = lambda: ["D"]
        check("matrix and fraction reach the chart",
              panel._scope_label() == "WS · D", panel._scope_label())
        panel._method_mode = lambda: ["SW6020A", "EPA200.8"]
        check("several methods are joined as the layer names join them",
              "SW6020A+EPA200.8" in panel._scope_label(),
              panel._scope_label())
        panel._non_detect_mode = lambda: rd.ND_EXCLUDE
        check("a non-default non-detect rule is named",
              "Exclude" in panel._scope_label(), panel._scope_label())
        panel._non_detect_mode = lambda: rd.ND_HALF
        check("the DEFAULT non-detect rule is not named",
              "detection limit" not in panel._scope_label(),
              panel._scope_label())
    finally:
        panel._matrix_mode, panel._fraction_mode = real_matrix, real_fraction
        panel._method_mode, panel._non_detect_mode = real_method, real_nd

    # --- the steps that do not apply -----------------------------------------
    check("step 3 (choose a report) is hidden",
          not panel.report_box.isVisibleTo(panel))
    check("step 4 says it filters here, not at EQuIS",
          "filtered here" in panel.selection_box.title(),
          panel.selection_box.title())
    check("the connection pane folds away as it does on connect",
          panel.connect_box.isCollapsed())

    # --- switching back to EQuIS leaves nothing behind ------------------------
    panel._discard_store()
    check("the parquet markers are cleared with the store",
          panel._parquet_url is None and panel._report_store is None)
    check("so the live path can fetch again", panel._needs_fetch())

    # The pipeline's manifest is a different file and was never touched.
    if os.path.isfile(LOCAL):
        with open(LOCAL, encoding="utf-8") as handle:
            check("the pipeline manifest sits untouched beside it",
                  "report_id" in handle.read())

# --- the chart draws what the scope says -------------------------------------
# Rendered, not just called: a first attempt placed the subtitle at y=1.005
# in axes coordinates, which lands exactly on the title and prints through
# it. Every call succeeded; only the picture showed it.
if HAVE_PLOT:
    import datetime
    points = [{"date": datetime.datetime(2024, month, 1),
               "value": 1.0 + month, "unit": "mg/L", "detect": "Y"}
              for month in range(1, 5)]
    panel.plot_dock.plot_series("MW-1", "Arsenic", points,
                                scope="WS · D · SW6020A")
    axes = panel.plot_dock.plot.figure.axes[0]
    check("the title is the chemical and location",
          axes.get_title() == "Arsenic at MW-1", axes.get_title())
    subtitles = [t.get_text() for t in axes.texts]
    check("the scope is drawn as its own line, not inside the title",
          subtitles == ["WS · D · SW6020A"], str(subtitles))
    check("and is smaller than the title, so it reads as context",
          axes.texts[0].get_fontsize() < axes.title.get_fontsize(),
          "%s vs %s" % (axes.texts[0].get_fontsize(),
                        axes.title.get_fontsize()))
    # An empty chart must say what was filtered: the filter is the likely
    # reason there is nothing to show.
    panel.plot_dock.plot_series("MW-99", "Arsenic", [], scope="SO · T")
    empty = " ".join(t.get_text()
                     for t in panel.plot_dock.plot.figure.axes[0].texts)
    check("an empty chart names the filter that emptied it",
          "SO · T" in empty, empty.replace("\n", " "))
    # Nothing filtered, nothing claimed.
    panel.plot_dock.plot_series("MW-1", "Lead", points)
    plain = panel.plot_dock.plot.figure.axes[0]
    check("an unfiltered chart carries no subtitle at all",
          not plain.texts, str([t.get_text() for t in plain.texts]))

panel.close()

print()
if FAILURES:
    print("FAILED: %s" % ", ".join(FAILURES))
    raise SystemExit(1)
print("The panel switches between EQuIS and published parquet.")
