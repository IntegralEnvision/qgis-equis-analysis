"""The EQuIS panel.

A dock widget that walks one path: API key -> facility -> location points on
the map -> report -> analyte -> aggregation -> a joined layer whose points
carry the aggregated result. Clicking a point plots that analyte's time series
in the panel itself.

Each step is a collapsible section, so the finished steps fold away and leave
room for the plot.
"""

import csv
import datetime
import os
import re
import threading

from qgis.core import (Qgis, QgsApplication, QgsFeatureRequest,
                       QgsMessageLog, QgsProject, QgsTask)
from qgis.gui import QgsCollapsibleGroupBox

try:
    from qgis.gui import QgsRangeSlider
except ImportError:  # QGIS built without the range slider
    QgsRangeSlider = None
from qgis.PyQt.QtCore import QSettings, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QFileDialog,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import settings as settings_store
from .cache import describe_age, describe_size
from .equis_api import EquisClient, EquisError, token_expiry, utc_now
from .layer_builder import (
    apply_point_displacement,
    build_result_layer,
    enable_temporal,
    build_feature_layer,
    build_joined_layer,
    build_bivariate_layer,
    build_ternary_layer,
    GEOPACKAGE,
    MEMORY,
    persist_to_geopackage,
    set_roll_column,
    SPARK_LIMIT_MAX_MM,
    SPARK_LIMIT_MIN_MM,
    style_by_result,
    style_with_rolling_sparklines,
    style_with_sparklines,
)
from .maptool import LocationPickTool
from .store import ReportStore
from .report_data import (
    AGGREGATION_SHORT,
    AGGREGATIONS,
    ALL_MATRICES,
    ALL_METHODS,
    ANALYSIS_COLUMNS,
    ANY_FRACTION,
    MAXIMUM,
    MOST_RECENT,
    ND_HALF,
    ND_HANDLING,
    NO_ROW_SCOPE,
    UNAGGREGATED,
    ReportTable,
    missing_message,
)
from .data_selection import (
    FRACTION_DISSOLVED,
    FRACTION_TOTAL,
    FRACTIONS,
    LIST_SEPARATOR,
    PARAM_ANALYTES,
    PARAM_FRACTIONS,
    PARAM_METHODS,
)
from .live_report import BASE_REPORT_CODE, LiveReport, report_name
from .panel_state import PanelState
from . import panel_state as st
from .selection_panel import (AGENT, LIVE, PARQUET as PARQUET_SOURCE,
                              SelectionPanel)
from .sparkline import embed_depth_profiles, embed_sparklines
from .tasks import (
    BuildStoreTask,
    CACHE,
    CACHE_FALLBACK,
    CACHED_SUFFIX,
    EIA,
    cached_layer_name,
    FetchFacilitiesTask,
    FetchFacilityDetailTask,
    FetchManifestTask,
    LoadLayersTask,
    PrepareLiveReportTask,
    RunReportTask,
)
from .timeseries import TimeSeriesDock

# The layer holding location points; layer 1 is VW_LOCATION on this service.
LOCATION_LAYER_ID = 1
# The stand-in id for the plugin's own report, which may not exist on the
# server until a fetch creates it. Negative so it can never collide with a
# real report id, and stable so the cache keys on the facility + selection
# rather than on whichever report id the user happens to have been given.
OWN_REPORT_ID = -1

LOCATION_FIELD = "code"

# Adding a layer per selected chemical is cheap for a handful and slow for a
# hundred, so past this many the user is asked first.
BULK_LAYER_WARNING = 8
# How long interactive mode waits for the controls to settle before it
# rebuilds the preview. Long enough to swallow an arrow key held down the
# chemical list, short enough to read as immediate on a single click.
PREVIEW_SETTLE_MS = 150

# How many result rows an unaggregated layer may hold. One chemical over a
# decade at a few hundred locations is easily tens of thousands of features,
# and QGIS redraws every one of them on every pan. Whole locations are
# dropped to stay under this, never part of a location's history: a truncated
# stack would misstate how many results a location has.
UNAGGREGATED_ROW_LIMIT = 25000

SPARKLINE_TOOLTIP = (
    "Unchecked: circles graduated by concentration.\n"
    "Checked: a small time-series sparkline per location, captioned\n"
    "with the location code.\n\n"
    "Applies to layers as they are added, and restyles the layers\n"
    "already on the map."
)


def _analyte_key(analyte):
    """What makes two analyte records the same chemical.

    Not the dict itself: PyQt marshals list-item data through QVariant, so the
    record read back from one item is not the same object as the record read
    from another, even for the same row.
    """
    return ((analyte or {}).get("chemical", ""), (analyte or {}).get("cas_rn", ""))


def _describe_eia_age(executed):
    """A short ", run <when>" for an agent execution timestamp.

    The service reports these in UTC without a zone marker. Only the date is
    shown: knowing an agent ran three weeks ago is what matters, and a
    to-the-minute time would imply a precision the comparison does not have.
    """
    text = str(executed or "").strip()
    if not text:
        return ""
    stamp = text.replace("Z", "").split(".")[0]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            when = datetime.datetime.strptime(stamp, fmt)
        except ValueError:
            continue
        days = (utc_now() - when).days
        if days <= 0:
            return ", run today"
        if days == 1:
            return ", run yesterday"
        if days < 30:
            return ", run %d days ago" % days
        return ", run %s" % when.strftime("%d %b %Y")
    return ""


# How many location codes to name before summarising. Enough that a normal
# mismatch - a handful of sites spelled differently - is named in full, while
# a wholesale code mismatch does not paste hundreds of codes into a status bar.
MAX_NAMED_CODES = 12


def _name_codes(codes):
    """Name these location codes, truncating a very long list.

    Sorted for the reader rather than left in first-seen order: someone
    checking them against a location layer is scanning, not tracing the order
    results happened to arrive in.
    """
    codes = sorted(codes or [])
    if not codes:
        return ""
    if len(codes) <= MAX_NAMED_CODES:
        return ", ".join(codes)
    shown = ", ".join(codes[:MAX_NAMED_CODES])
    return "%s and %d more" % (shown, len(codes) - MAX_NAMED_CODES)


def _describe_unplaced(codes):
    """Say which locations could not be placed, and why it matters.

    Names them rather than counting them. "8 could not be placed" tells the
    user something is wrong but not which sites to go and look at, and the
    codes ARE the problem: they are precisely what differs between the report
    and the location layer, so naming them turns a mystery into a lookup.
    """
    count = len(codes or [])
    if not count:
        return ""
    if count == 1:
        return ("1 location could not be placed - %s is not in the location "
                "layer." % _name_codes(codes))
    return ("%d locations could not be placed - these are not in the "
            "location layer: %s." % (count, _name_codes(codes)))


def _flatten_codes(value):
    """One stable string for a filter that may be a tuple of codes.

    A tuple would work as a dict key but sorts and prints unpredictably, and
    these keys end up in comparisons and in debugging output.
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        return "+".join(sorted(str(v) for v in value))
    return value or ""


def _view_key(analyte, aggregation, sparklines, matrix=None,
              fraction=None, method=None):
    """What makes two mapped layers the same view of the data.

    Every filter that selects DIFFERENT ROWS belongs here, or mapping one
    view silently destroys another built from data it does not contain.
    Symbology is part of it too: a chemical drawn as sparklines and the same
    chemical drawn as graduated circles are two ways of looking at one
    dataset, and keeping both is the point of adding the second.

    So: chemical, matrix, fraction, method, aggregation and symbology. Soil
    and groundwater TCE are different data; so are dissolved and total, and
    so are two analytic methods that can disagree about whether the analyte
    was detected at all. Only re-adding every one of them the same replaces
    what is there.
    """
    return _analyte_key(analyte) + (
        aggregation, bool(sparklines), _flatten_codes(matrix),
        _flatten_codes(fraction), _flatten_codes(method),
    )


# Positions within a view key. Indexed by name because the key has grown
# twice now, and the size-range loop was reading key[-1] for the sparkline
# flag long after the matrix was appended after it - so it resized layers by
# matrix and skipped every sparkline layer that had no matrix filter.
# APPEND new fields, never insert: these indices are read positionally.
VIEW_AGGREGATION = 2
VIEW_SPARKLINES = 3
VIEW_MATRIX = 4
VIEW_FRACTION = 5
VIEW_METHOD = 6

# The banner changes colour wholesale when the session is offline. A per-item
# "(cached)" suffix is easy to read past; a panel that changes colour is not.
BANNER_LIVE = (
    "QLabel { background: #eef4fb; border: 1px solid #c5d9ee;"
    " border-radius: 3px; padding: 6px; }"
)
BANNER_CACHED = (
    "QLabel { background: #fdf6e3; border: 1px solid #d9c48a;"
    " border-radius: 3px; padding: 6px; }"
)


class EquisDialog(QDockWidget):
    # Carries a finished count back from the worker thread. A signal rather
    # than a direct setText: Qt forbids touching widgets from another
    # thread, and a queued signal is how the result crosses back.
    _count_ready = pyqtSignal(int, int, int)

    def __init__(self, iface, parent=None, pick_action=None):
        super().__init__(parent)
        self.iface = iface
        # The toolbar's checkable "Chemistry Time Series" action, when the plugin
        # created one. Kept in step with the in-dialog button.
        self.pick_action = pick_action
        self.setWindowTitle("QGIS Chemistry Explorer")
        # The old object name, kept: QGIS restores a dock's position by it,
        # so renaming it would put every user's panel back at the default.
        self.setObjectName("EquisChemicalExplorerPanel")
        self.setMinimumWidth(380)
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
        )

        self._client = None
        self._facilities = []
        # Where the panel is in its own sequence, written down rather than
        # re-derived. The flags below still exist and still hold the DATA -
        # this holds the position, and refuses moves that have no path.
        self._fsm = PanelState(on_violation=self._on_state_violation)
        self._layers = []
        self._reports = []
        self._service_wkid = 0
        self._task = None
        # The task _start actually queued, which is not the same as _task
        # being set: a task built but never queued reports the same status as
        # one waiting to run, so only this tells a real fetch in flight from
        # a result handed straight to a callback.
        self._queued_task = None
        # ids of tasks cancelled because the user asked for something else.
        # Their callbacks still arrive and must not land on the panel.
        self._superseded = set()

        self._cache = settings_store.open_cache()
        # Whether this site has anything cached. Maintained by
        # _update_cache_label so the capabilities need not touch the disk.
        self._has_cache = False
        # True when working from the cache with no live client at all. Set
        # either by the user pressing "Work offline" or automatically when a
        # connection attempt fails and this site has cached data to fall back
        # on - which is the case that actually happens, since nobody decides
        # in advance that the network is down.
        self._offline = False
        # Provenance of the data currently loaded, so the banner and the layer
        # names can say where it came from.
        self._report_source = None
        self._report_entry = None
        self._locations_source = None

        self._locations_layer = None
        # Every layer this panel has on the map. `_result_layer` is the active
        # one among them - the last added - which the time-series tool and the
        # sparkline controls act on.
        self._result_layers = []
        # {(chemical, cas, aggregation, sparklines): layer}. Keyed so that
        # re-adding one view replaces only itself, leaving other views of the
        # same data alone.
        self._layer_views = {}
        # INTERACTIVE MODE'S ONE SCRATCH LAYER. Deliberately NOT in
        # _layer_views: that dict replaces by view key, and a preview of
        # Benzene must never delete the Benzene layer the user chose to keep.
        # Tracked alone, removed alone, and handed to _layer_views only by
        # "Keep layer". `_preview_key` is the question it answers, so a
        # re-entrant change signal asking the same question draws nothing.
        self._preview_layer = None
        self._preview_key = None
        # Zoom once per session of browsing. Re-framing the map on every
        # pick would undo the pan the user made to look at something.
        self._preview_zoomed = False
        # Coalesces a burst of changes - arrow keys down the list, or the
        # re-entrant `changed` signals one pick produces - into one build of
        # whatever the controls say when it settles.
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(PREVIEW_SETTLE_MS)
        self._preview_timer.timeout.connect(self._update_preview)
        # The QGIS layer group our layers live in, so a long session does not
        # scatter them through the panel.
        self._group_name = None
        self._location_pages = None
        # False when the facility's locations carry no coordinates, which the
        # service signals by returning nothing for returnGeometry=true.
        self._has_geometry = False
        self._report_table = None
        # The same rows in DuckDB, built in the background after a report
        # lands. May be None - it is an accelerator, not the source of truth,
        # and `_analysis` below is the ONE place that decides which answers.
        self._report_store = None
        self._store_task = None
        # The linked-views controller, built only when asked for. QtWebEngine
        # and the built web bundle can both be absent on a perfectly valid
        # install, so nothing outside this feature may assume it exists.
        self._linked = None
        self._analytes = []
        # Matrix codes in the loaded report, most common first. Empty when the
        # report has no matrix column, which hides the filter entirely.
        self._matrices = []
        # The report this user's live queries are written to, created on first
        # use. None until a facility is chosen.
        self._live_report = None
        # Which source the next fetch uses. The selection means something
        # different in each, which the panel says rather than hiding it.
        self._source_mode = LIVE
        # A published parquet source, when one is open: the URL it came from
        # and the in-flight manifest fetch. Both None on the live path.
        self._parquet_url = None
        self._manifest_task = None
        # What the histogram was last drawn for; see _refresh_chart.
        self._drawn_scope = None
        # The matrix the panel last reported, so a change can be told from
        # the many other things that emit `changed`.
        self._last_matrix = ALL_MATRICES
        # The chemical the method/fraction offers were last built for. Both
        # stops a filter-box keystroke rebuilding them and breaks the
        # changed -> rebuild -> changed cycle after one pass.
        self._last_analyte = None
        # The footer's count runs off-thread; the generation number is what
        # makes a stale answer discardable rather than confusing.
        self._count_generation = 0
        self._count_timer = None
        self._count_thread = None
        self._count_ready.connect(
            lambda locations, results, generation:
            self._show_visible_count((locations, results), generation))
        # The selection the loaded rows were fetched for, so a changed filter
        # is known to need new rows rather than a re-read of the old ones.
        self._fetched_selection = None
        # The DataSelection those rows were fetched for, kept whole so a
        # layer's provenance can describe the request that produced its rows
        # rather than whatever the controls say at the moment of mapping.
        self._fetched_selection_obj = None
        # Every chemical the facility holds, as reported by the parameter
        # service. Kept apart from what any one fetch returned so a narrow
        # fetch cannot narrow what can be asked for next.
        self._offered_analytes = []
        # Set when a fetch was started by pressing map, so the layers follow
        # as soon as the rows land.
        self._result_layer = None
        self._map_tool = None
        self._syncing_pick = False
        # Exactly one facility/report pair is active at a time; these record
        # which, so reopening the dialog can say what the map is showing.
        self._active_report = None
        self._active_aggregation = None
        # What the layer on the map actually shows, which is not always what
        # is selected in the list right now.
        self._mapped_chemical = None
        self._picked_location = None
        # How the layer currently on the map was stored, which is not always
        # what the combo asks for: an unsaved project forces memory.
        self._layer_storage = MEMORY

        # Rolling sparklines: a timer advances which analyte's marker column
        # the renderer reads. The timer is owned here so it can be stopped
        # from every teardown path - a timer still firing at a deleted layer
        # is a crash, not a cosmetic bug.
        self._roll_timer = QTimer(self)
        self._roll_timer.timeout.connect(self._advance_roll)
        self._roll_columns = []
        self._roll_analytes = []
        # The markers the roll is built from, kept so changing the marker
        # size range can rebuild the renderer without re-rendering every
        # sparkline SVG - that is seconds of work for a large site.
        self._roll_markers = []
        self._roll_index = 0

        self._build_ui()
        self._load_settings()
        # Saving the project is what gives GeoPackage storage somewhere to
        # write, so the hint has to follow it rather than going stale.
        QgsProject.instance().fileNameChanged.connect(self._update_storage_hint)
        QTimer.singleShot(0, self._auto_connect)

    # ------------------------------------------------------------------- ui

    def _build_ui(self):
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)

        # Always-visible summary of the one active facility/report/chemical,
        # so reopening the panel immediately says what the map is showing.
        self.active_label = QLabel("Nothing loaded yet.")
        self.active_label.setWordWrap(True)
        self.active_label.setStyleSheet(
            "QLabel { background: #eef4fb; border: 1px solid #c5d9ee;"
            " border-radius: 3px; padding: 6px; }"
        )
        outer.addWidget(self.active_label)

        # The steps live in a scroll area so the panel works at any dock width,
        # and each is collapsible so finished steps fold away for the plot.
        steps = QWidget()
        steps_layout = QVBoxLayout(steps)
        steps_layout.setContentsMargins(0, 0, 0, 0)
        steps_layout.setSpacing(4)

        # --- 1. connection ---------------------------------------------
        self.connect_box = QgsCollapsibleGroupBox("1. Connect")
        self.connect_box.setSaveCheckedState(False)
        conn_form = QFormLayout(self.connect_box)
        conn_form.setContentsMargins(6, 4, 6, 6)
        self.site_edit = QLineEdit()
        self.site_edit.setPlaceholderText("https://yoursite.equisonline.com")
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("EQuIS API key (JWT)")
        # Where results come from. Two sources answer the same questions:
        # the live API, and a published parquet set read through its
        # manifest. The credential rows below swap with this, because a
        # signed blob URL carries its own credential and an API key means
        # nothing to it.
        self.source_combo = QComboBox()
        self.source_combo.addItem("EQuIS (live)", settings_store.SOURCE_LIVE)
        self.source_combo.addItem("Published parquet",
                                  settings_store.SOURCE_PARQUET)
        conn_form.addRow("Source:", self.source_combo)

        conn_form.addRow("Site:", self.site_edit)
        conn_form.addRow("API key:", self.key_edit)

        self.parquet_edit = QLineEdit()
        self.parquet_edit.setPlaceholderText(
            "https://account.blob.core.windows.net/container/facility_a?sv=...")
        self.parquet_edit.setToolTip(
            "The folder the parquet partitions live in. The plugin reads "
            "manifest.json from it, which names the files - so a signed "
            "URL works and no account key is needed.")
        # A SAS URL is ~190 characters and the field must never make the dock
        # want to be that wide. The panel's own size hint is 408 px with or
        # without one - measured - but a field that shows the whole token is
        # unreadable anyway, so it scrolls within a modest width and the full
        # value stays available as a tooltip.
        self.parquet_edit.setMinimumWidth(180)
        self.parquet_edit.setMaximumWidth(16777215)
        self.parquet_edit.setCursorPosition(0)
        self.parquet_edit.textChanged.connect(self._on_parquet_url_changed)
        conn_form.addRow("Parquet URL:", self.parquet_edit)
        self.parquet_label = conn_form.labelForField(self.parquet_edit)

        # Keys carry their own expiry. Showing it beside the field turns a
        # mid-session wall of 401s into something the user saw coming.
        self.key_hint = QLabel("")
        self.key_hint.setWordWrap(True)
        self.key_hint.setStyleSheet("color: #5a6b7b;")
        conn_form.addRow("", self.key_hint)
        self.key_edit.textChanged.connect(self._on_key_changed)
        self.site_label = conn_form.labelForField(self.site_edit)
        self.key_label = conn_form.labelForField(self.key_edit)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)

        conn_row = QHBoxLayout()
        self.verify_ssl_check = QCheckBox("Verify SSL")
        self.verify_ssl_check.setChecked(True)
        self.read_only_check = QCheckBox("Read-only")
        self.read_only_check.setToolTip(
            "Never create, change or delete anything on the EQuIS site.\n"
            "The plugin's own filtered report is then unavailable: only the\n"
            "facility's existing reports, or an agent's stored output, can\n"
            "be read. For read-only API keys, or sites whose administrators\n"
            "want nothing created."
        )
        self.read_only_check.toggled.connect(self._on_read_only_toggled)
        self.offline_btn = QPushButton("Work offline")
        self.offline_btn.setToolTip(
            "Browse previously loaded facilities and reports from this\n"
            "machine's cache, without contacting EQuIS."
        )
        self.offline_btn.clicked.connect(self._on_work_offline)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._on_connect)
        conn_row.addWidget(self.verify_ssl_check)
        conn_row.addWidget(self.read_only_check)
        conn_row.addStretch(1)
        conn_row.addWidget(self.offline_btn)
        conn_row.addWidget(self.connect_btn)
        conn_form.addRow("", conn_row)

        # What is on disk for this site, and a way to be rid of it.
        cache_row = QHBoxLayout()
        self.cache_label = QLabel("")
        self.cache_label.setWordWrap(True)
        self.cache_label.setStyleSheet("color: #5a6b7b;")
        self.clear_cache_btn = QPushButton("Clear cache")
        self.clear_cache_btn.setToolTip(
            "Delete cached EQuIS data for this site from this machine."
        )
        self.clear_cache_btn.clicked.connect(self._on_clear_cache)
        cache_row.addWidget(self.cache_label, 1)
        cache_row.addWidget(self.clear_cache_btn)
        # Held so the whole row can be hidden: the cache is a store of EQuIS
        # API responses and means nothing to a published parquet, which is
        # read straight from its files.
        self.cache_row = cache_row
        conn_form.addRow("Cache:", cache_row)
        self.cache_row_label = conn_form.labelForField(cache_row)
        steps_layout.addWidget(self.connect_box)

        # --- 2. facility ------------------------------------------------
        self.facility_box = QgsCollapsibleGroupBox("2. Facility")
        self.facility_box.setSaveCheckedState(False)
        fac_layout = QVBoxLayout(self.facility_box)
        fac_layout.setContentsMargins(6, 4, 6, 6)
        self.facility_filter = QLineEdit()
        self.facility_filter.setPlaceholderText("Filter facilities...")
        self.facility_filter.textChanged.connect(self._filter_facilities)
        self.facility_combo = QComboBox()
        self.facility_combo.currentIndexChanged.connect(self._on_facility_changed)
        fac_layout.addWidget(self.facility_filter)
        fac_layout.addWidget(self.facility_combo)
        self.locations_label = QLabel("")
        self.locations_label.setWordWrap(True)
        fac_layout.addWidget(self.locations_label)
        steps_layout.addWidget(self.facility_box)

        # --- 3. report --------------------------------------------------
        self.report_box = QgsCollapsibleGroupBox("3. Data source")
        self.report_box.setSaveCheckedState(False)
        rpt_layout = QVBoxLayout(self.report_box)
        rpt_layout.setContentsMargins(6, 4, 6, 6)

        # The default is the plugin's own report, built on Analytical Results
        # II and re-filtered per fetch. Choosing from the facility's hundreds
        # of saved reports is the exception, not the way in, so it is folded
        # away behind a checkbox rather than being the first thing asked.
        self.own_report_label = QLabel("")
        self.own_report_label.setWordWrap(True)
        rpt_layout.addWidget(self.own_report_label)

        self.pick_report_check = QCheckBox("Use an existing report instead")
        self._pick_report_tip = (
            "By default the plugin queries through its own report, filtered\n"
            "by the data selection below.\n"
            "Tick this to run one of the facility's saved reports as it is -\n"
            "useful for a report someone has already tuned, or one whose\n"
            "output an information agent has stored."
        )
        self.pick_report_check.setToolTip(self._pick_report_tip)
        self.pick_report_check.toggled.connect(self._on_pick_report_toggled)
        rpt_layout.addWidget(self.pick_report_check)

        self.report_filter = QLineEdit()
        self.report_filter.setPlaceholderText("Filter reports...")
        self.report_filter.textChanged.connect(self._populate_reports)
        self.report_filter.setVisible(False)
        rpt_layout.addWidget(self.report_filter)
        self.report_combo = QComboBox()
        self.report_combo.setVisible(False)
        rpt_layout.addWidget(self.report_combo)

        # The row limit and the fetch button live in step 4 now, with the
        # analytes and matrix that decide what the fetch asks for. This box
        # only chooses WHERE the data comes from.
        row = QHBoxLayout()
        self.report_limit_check = QCheckBox("Limit rows:")
        self.report_limit_check.setChecked(False)
        self.report_limit_spin = QSpinBox()
        self.report_limit_spin.setRange(1, 20000000)
        self.report_limit_spin.setValue(500000)
        self.report_limit_spin.setSingleStep(50000)
        self.report_limit_spin.setGroupSeparatorShown(True)
        self.report_limit_spin.setEnabled(False)
        limit_hint = (
            "Off by default: truncating a report drops whole sample dates, so\n"
            "a chemical's history can silently end early.\n"
            "A 462,000-row report loads in about 20 s and holds roughly 200 MB\n"
            "in memory - set a limit if that is too much for this machine."
        )
        self.report_limit_check.setToolTip(limit_hint)
        self.report_limit_spin.setToolTip(limit_hint)
        self.report_limit_check.toggled.connect(self.report_limit_spin.setEnabled)
        self.run_report_btn = QPushButton("Run report")
        self.run_report_btn.clicked.connect(self._on_run_report)
        # Cached is the default path, so re-running against EQuIS needs its own
        # control rather than being hidden behind a modifier.
        self.refresh_report_btn = QPushButton("↻")
        self.refresh_report_btn.setToolTip(
            "Re-run this report against EQuIS, replacing the cached copy."
        )
        self.refresh_report_btn.setMaximumWidth(32)
        self.refresh_report_btn.clicked.connect(self._on_refresh_report)
        row.addWidget(self.report_limit_check)
        row.addWidget(self.report_limit_spin)
        row.addStretch(1)
        # Held for step 4, which owns the fetch: this row is built here only
        # because the widgets above it belong to the source choice.
        self._prefetch_limit_row = row
        self.report_hint = QLabel(
            "<i>Reports run on demand and may take a few minutes.</i>"
        )
        self.report_hint.setWordWrap(True)
        rpt_layout.addWidget(self.report_hint)
        # Keep the run/refresh buttons in step with whether the highlighted
        # report is already cached.
        self.report_combo.currentIndexChanged.connect(self._update_report_actions)
        self.report_limit_check.toggled.connect(self._update_report_actions)
        self.report_limit_spin.valueChanged.connect(self._update_report_actions)
        steps_layout.addWidget(self.report_box)

        # --- 4. data selection ------------------------------------------
        # Before the chemical step, because it decides which chemicals exist:
        # on the live path these filters are saved onto the report and shape
        # what EQuIS generates at all.
        # The title names the contract, not just the step: everything in
        # this box is saved onto the report and decides which rows EQuIS
        # generates. The box below re-scopes rows already here. Naming that
        # seam is what stops "Fetch" and "Add layer" reading as two buttons
        # for one thing.
        self.selection_box = QgsCollapsibleGroupBox(
            "4. Pre-fetch Data — sent to EQuIS")
        self.selection_box.setSaveCheckedState(False)
        sel_layout = QVBoxLayout(self.selection_box)
        sel_layout.setContentsMargins(6, 4, 6, 6)
        # What this box means right now, by state: filled in by
        # _refresh_step_hints. No count is shown here before a fetch, because
        # none is knowable - the parameter service cannot say how many rows a
        # selection will return, only what values exist.
        self.prefetch_hint = QLabel("")
        self.prefetch_hint.setWordWrap(True)
        self.prefetch_hint.setStyleSheet("color: #5a6b7b;")
        sel_layout.addWidget(self.prefetch_hint)
        self.selection_panel = SelectionPanel()
        self.selection_panel.changed.connect(self._on_selection_changed)
        # Individual locations, by selecting points on the Locations layer
        # with QGIS's own selection tools. A facility can have 11,000
        # locations, so a list is the wrong control and the map is the
        # right one; the panel cannot see the layer, so this lives here.
        # Saved onto the report as @sys_loc_codes, so it is a prefetch
        # filter - and Where, so it goes above the panel's own controls.
        self.map_selection_check = QCheckBox(
            "Only locations selected on the map (0)")
        self.map_selection_check.setToolTip(
            "Select points on this facility's Locations layer with any QGIS\n"
            "selection tool, then tick this to fetch only those locations."
        )
        self.map_selection_check.toggled.connect(self._on_selection_changed)
        sel_layout.addWidget(self.map_selection_check)

        # Only the half that shapes the REQUEST. The panel's other half sits
        # in step 5, because it filters rows already fetched.
        sel_layout.addWidget(self.selection_panel.prefetch_section)

        # The row limit and the fetch button, as ONE widget so the parquet
        # path can remove them together. Two bare layouts cannot be hidden,
        # and leaving them showed a live "Pre-fetch data" button beside rows
        # that were already loaded - which, with a client left over from an
        # earlier Connect, saved filters onto the EQuIS report and replaced
        # the parquet session with an API fetch.
        self.fetch_section = QWidget()
        fetch_layout = QVBoxLayout(self.fetch_section)
        fetch_layout.setContentsMargins(0, 0, 0, 0)
        fetch_layout.setSpacing(4)
        sel_layout.addWidget(self.fetch_section)

        # The row limit belongs to the fetch, so it comes down here with it.
        fetch_layout.addLayout(self._prefetch_limit_row)

        # Centred and alone on its row: this is the one action in the panel
        # that costs minutes and reaches the server, and it was previously a
        # small button in the corner of the source box, indistinguishable
        # from "Add layer to map" two boxes below. "Pre-fetch" names the
        # separation the flow depends on - this retrieves rows, it does not
        # draw anything.
        fetch_row = QHBoxLayout()
        fetch_row.addStretch(1)
        self.run_report_btn.setMinimumWidth(160)
        fetch_row.addWidget(self.run_report_btn)
        fetch_row.addWidget(self.refresh_report_btn)
        fetch_row.addStretch(1)
        fetch_layout.addLayout(fetch_row)

        steps_layout.addWidget(self.selection_box)

        # --- 5. filter and aggregate -------------------------------------
        # Everything here re-scopes rows ALREADY fetched, so changing any of
        # it is instant and costs no request. That is the whole reason it is
        # a separate step from the one above.
        self.filter_box = QgsCollapsibleGroupBox(
            "5. Filter / Aggregate — applied here")
        self.filter_box.setSaveCheckedState(False)
        filter_layout = QVBoxLayout(self.filter_box)
        filter_layout.setContentsMargins(6, 4, 6, 6)
        # INTERACTIVE MODE: the shortcut through steps 4-5. The chemical
        # list goes single-select, each pick is drawn at once as a memory
        # layer, and every change below replaces it. An explicit toggle
        # rather than a behaviour the panel slides into, because it changes
        # what a click in the list MEANS - and that must never be a surprise.
        self.interactive_check = QCheckBox(
            "Interactive: map each chemical as it is picked")
        self.interactive_check.setToolTip(
            "The chemical list becomes single-select. Picking a chemical\n"
            "draws it straight away as a temporary memory layer and re-bins\n"
            "the histogram; picking another, or changing any filter below,\n"
            "replaces that layer.\n\n"
            "Press 'Keep layer' to make the one on screen permanent.\n"
            "Rolling sparklines need several chemicals, so they are off\n"
            "while this is on.")
        self.interactive_check.toggled.connect(self._on_interactive_toggled)
        filter_layout.addWidget(self.interactive_check)
        # Shown only when the rows in hand were fetched for a different
        # request than step 4 now describes. The map button already goes grey
        # for this, but its explanation lived in a tooltip - discoverable only
        # by hovering over a button that does nothing. The rows stay usable:
        # they are real data from a real request, and nothing here is dimmed.
        self.stale_label = QLabel(
            "These rows are from your last fetch. Fetch again to apply the "
            "changes above.")
        self.stale_label.setWordWrap(True)
        self.stale_label.setStyleSheet(
            "QLabel { color: #7a5a00; background: #fdf6e3;"
            " border: 1px solid #d9c48a; border-radius: 3px; padding: 4px; }"
        )
        self.stale_label.setVisible(False)
        filter_layout.addWidget(self.stale_label)
        self.filter_hint = QLabel(
            "Re-scopes the rows already fetched. Instant, and costs no request.")
        self.filter_hint.setWordWrap(True)
        self.filter_hint.setStyleSheet("color: #5a6b7b;")
        filter_layout.addWidget(self.filter_hint)
        filter_layout.addWidget(self.selection_panel.filter_section)

        # How the selected chemicals are summarised per location, and what to
        # do with a non-detect. These sit with the filters because they
        # describe the same rows: each selects or summarises what is already
        # in hand rather than changing what was asked for.
        agg_form = QFormLayout()
        agg_form.setContentsMargins(0, 0, 0, 0)
        self.aggregation_combo = QComboBox()
        for key, label in AGGREGATIONS:
            self.aggregation_combo.addItem(label, key)
        # Not connected directly: currentIndexChanged passes the new index as
        # the first argument, which _update_actions would read as `busy`.
        self.aggregation_combo.currentIndexChanged.connect(
            self._on_aggregation_changed)
        # "Per location" names what the aggregate does - one value per point
        # - which "Aggregate" never quite said.
        agg_form.addRow("Per location:", self.aggregation_combo)

        # Non-detects are reported only as the limit they fell below, and the
        # convention for what to plot differs by workflow.
        self.non_detect_combo = QComboBox()
        for key, label in ND_HANDLING:
            self.non_detect_combo.addItem(label, key)
        self.non_detect_combo.setToolTip(
            "How to treat non-detects, which report only a detection limit.\n"
            "1/2 DL is the usual convention for statistics and trends;\n"
            "full DL is conservative for screening against criteria."
        )
        self.non_detect_combo.currentIndexChanged.connect(
            self._on_non_detect_changed)
        agg_form.addRow("Non-detects:", self.non_detect_combo)

        # Where the result layer lives. A memory layer is not saved with the
        # project - QGIS keeps the reference but not the data - so the default
        # is a GeoPackage beside the .qgz.
        self.storage_combo = QComboBox()
        self.storage_combo.addItem("GeoPackage (saved beside project)",
                                   GEOPACKAGE)
        self.storage_combo.addItem("Memory (this session only)", MEMORY)
        self.storage_combo.setToolTip(
            "GeoPackage writes the layer next to the project file, so\n"
            "reopening the project brings the data back and the layer can be\n"
            "shared with the project folder.\n\n"
            "Memory layers are discarded when QGIS closes: a saved project\n"
            "reopens with an empty layer. Faster by a fraction of a second,\n"
            "and useful for throwaway exploration."
        )
        self.storage_combo.currentIndexChanged.connect(self._on_storage_changed)
        agg_form.addRow("Store as:", self.storage_combo)
        filter_layout.addLayout(agg_form)

        self.storage_hint = QLabel("")
        self.storage_hint.setWordWrap(True)
        self.storage_hint.setStyleSheet("color: #5a6b7b;")
        filter_layout.addWidget(self.storage_hint)

        # What the filters above leave to map, counted from the rows in hand
        # with the same scoping the build uses - so this is the number that
        # will actually land on the map, not a guess. Empty until LOADED.
        self.rows_footer = QLabel("")
        self.rows_footer.setWordWrap(True)
        self.rows_footer.setStyleSheet("color: #5a6b7b;")
        filter_layout.addWidget(self.rows_footer)

        btn_row = QHBoxLayout()
        self.map_btn = QPushButton("Add layer to map")
        self.map_btn.clicked.connect(self._on_build_layer)
        self.clear_layers_btn = QPushButton("Remove EQuIS layers")
        self.clear_layers_btn.setToolTip(
            "Remove every layer this panel added, and the group holding them.\n"
            "Adding a layer no longer clears the others, so this is how to\n"
            "start the map over."
        )
        self.clear_layers_btn.clicked.connect(self._on_clear_layers)
        # Beside the map button because it exports the same selection: the
        # rows the mapped layers were built from, with every filter applied.
        # A map answers "where"; the CSV is how someone checks a number on it
        # against the results underneath.
        self.export_btn = QPushButton("Export CSV")
        self.export_btn.setToolTip(
            "Save the report rows behind the mapped layers, after the "
            "matrix, fraction, method and QC filters - one row per "
            "result, with every column the report returned.\n\n"
            "This is the data an aggregated layer summarised, so it is "
            "what to check a mapped value against."
        )
        self.export_btn.clicked.connect(self._on_export_csv)
        # Interactive mode's version of the map button: the layer is already
        # drawn, so the only thing left to ask for is keeping it.
        self.keep_btn = QPushButton("Keep layer")
        self.keep_btn.setToolTip(
            "Make the layer on screen permanent: it is written beside the\n"
            "project like any other (or kept in memory if the project is not\n"
            "saved), and the next pick draws a new temporary layer.")
        self.keep_btn.clicked.connect(self._on_keep_preview)
        self.keep_btn.setVisible(False)
        btn_row.addWidget(self.map_btn)
        btn_row.addWidget(self.keep_btn)
        btn_row.addWidget(self.export_btn)
        btn_row.addWidget(self.clear_layers_btn)
        filter_layout.addLayout(btn_row)

        steps_layout.addWidget(self.filter_box)

        # --- 6. how the layer looks --------------------------------------
        # Not a second copy of step 4's picker: that one chooses what to
        # FETCH from EQuIS, this one chooses which of the fetched chemicals
        # to draw. The list here can only ever be a subset of what step 4
        # asked for, which the hint below says out loud.
        self.chem_box = QgsCollapsibleGroupBox("6. Symbology")
        self.chem_box.setSaveCheckedState(False)
        chem_layout = QVBoxLayout(self.chem_box)
        chem_layout.setContentsMargins(6, 4, 6, 6)

        # The chemical list, the matrix filter and the aggregate all moved to
        # step 4: the analytes a report holds and the analytes that get mapped
        # are one list, and offering it twice was the confusion. What is left
        # here is how the mapped layers are drawn.
        self.chem_hint = QLabel(
            "<i>How the layers from step 5 are drawn.</i>"
        )
        self.chem_hint.setWordWrap(True)
        chem_layout.addWidget(self.chem_hint)

        # How the layer is drawn, chosen before it is built rather than as a
        # restyle afterwards: "Sparklines on map" as a button looked parallel
        # to "Add layer to map" but could only ever act on a layer that already
        # existed, so it was greyed out exactly when you wanted to press it.
        self.sparkline_check = QCheckBox("Draw as time-series sparklines")
        self.sparkline_check.setToolTip(SPARKLINE_TOOLTIP)
        self.sparkline_check.toggled.connect(self._on_sparkline_mode_changed)
        chem_layout.addWidget(self.sparkline_check)

        # Depth profiles only mean anything for "every result": they draw
        # a location's results down a depth axis, and an aggregated layer
        # has one result per location to draw.
        self.profile_check = QCheckBox("Draw depth profiles where sampled "
                                       "at several depths")
        self.profile_check.setChecked(True)
        self.profile_check.setToolTip(
            "For 'Every result' layers only.\n"
            "A borehole sampled down its length has every result on one\n"
            "coordinate, so the map alone cannot tell 2 m from 40 m. This\n"
            "draws each location as a small profile - depth down the side,\n"
            "concentration across - instead of a pile of markers.\n\n"
            "Locations sampled at a single depth keep the ordinary graduated\n"
            "marker.")
        self.profile_check.toggled.connect(self._on_profile_mode_changed)
        chem_layout.addWidget(self.profile_check)

        chem_layout.addLayout(self._build_spark_size_row())

        roll_row = QHBoxLayout()
        self.roll_btn = QPushButton("Roll sparklines")
        self.roll_btn.setCheckable(True)
        self.roll_btn.setEnabled(False)
        self.roll_btn.setToolTip(
            "Cycle the map markers through every chemical selected in the\n"
            "list above, one at a time. Each marker is captioned with the\n"
            "chemical it is showing.\n\n"
            "Ctrl-click in the chemical list to select more than one."
        )
        self.roll_btn.toggled.connect(self._on_roll_toggled)
        self.roll_interval_spin = QSpinBox()
        self.roll_interval_spin.setRange(1, 60)
        self.roll_interval_spin.setValue(3)
        self.roll_interval_spin.setSuffix(" s")
        self.roll_interval_spin.setToolTip(
            "Seconds each chemical stays on the map before the next one."
        )
        self.roll_interval_spin.valueChanged.connect(self._on_roll_interval_changed)

        # A "sparkline" of one point is a dot pretending to be a trend, so
        # locations below this many results keep a plain marker. Shared by
        # both the static and the rolling sparklines.
        self.min_points_spin = QSpinBox()
        self.min_points_spin.setRange(1, 100)
        self.min_points_spin.setValue(2)
        self.min_points_spin.setPrefix("min pts ")
        self.min_points_spin.setToolTip(
            "Minimum results a location needs before it is drawn as a\n"
            "sparkline. Locations with fewer keep a plain marker, since a\n"
            "line through one point is not a trend."
        )
        roll_row.addWidget(self.roll_btn)
        roll_row.addWidget(self.roll_interval_spin)
        roll_row.addWidget(self.min_points_spin)
        roll_row.addStretch(1)
        chem_layout.addLayout(roll_row)

        self.roll_label = QLabel("")
        self.roll_label.setWordWrap(True)
        self.roll_label.setStyleSheet("color: #5a6b7b;")
        chem_layout.addWidget(self.roll_label)

        # The pick tool acts on the active layer rather than the selection, so
        # it sits apart from the three above.
        pick_row = QHBoxLayout()
        self.pick_btn = QPushButton("Time series tool")
        self.pick_btn.setCheckable(True)
        self.pick_btn.setEnabled(False)
        self.pick_btn.setToolTip(
            "Arm the map cursor so clicking a location plots its time series.\n"
            "Untoggle, or pick any other QGIS map tool, to get the normal\n"
            "cursor back.\n\n"
            "Clicks are tested against the active layer - the last one added -\n"
            "and plot whichever chemical is current in the list above."
        )
        self.pick_btn.toggled.connect(self._on_pick_button_toggled)
        pick_row.addWidget(self.pick_btn)
        pick_row.addStretch(1)
        chem_layout.addLayout(pick_row)
        steps_layout.addWidget(self.chem_box)
        steps_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(steps)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        # --- time series, in a dock of its own --------------------------
        # It used to be the lower half of a vertical splitter in this panel,
        # sharing a ~380 px column with six steps of controls. That is enough
        # to confirm a click landed on the right location and not enough to
        # read a chart - and the pan/zoom toolbar it has always carried only
        # made the crowding worse when used. The sparklines are already the
        # small view of a series; this is the large one, so it gets room that
        # a control panel cannot spare.
        #
        # Created here but docked by the plugin, which owns the main window.
        self.plot_dock = TimeSeriesDock(self.parent())
        self.plot = self.plot_dock.plot

        # --- two tabs: Data, then Analysis -------------------------------
        # Decided 2026-10-09. The steps above are tab 1, unchanged. Tab 2
        # holds ONE long-lived web view - created once, warmed in the
        # background when data loads, never recreated - because a redraw of
        # a live page measured 0.05 s against 0.44 s for a new page and
        # ~1.4 s for Chromium's start (tools/measure_webengine_startup.py).
        # The banner above and the status line below are shared by both.
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(scroll, "Data")
        self.analysis_page = QWidget()
        analysis_layout = QVBoxLayout(self.analysis_page)
        analysis_layout.setContentsMargins(0, 0, 0, 0)
        self.analysis_placeholder = QLabel(
            "Load data on the Data tab; the analysis view starts in the "
            "background once it is loaded.")
        self.analysis_placeholder.setWordWrap(True)
        self.analysis_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.analysis_placeholder.setStyleSheet(
            "QLabel { color: #666; padding: 24px; }")
        analysis_layout.addWidget(self.analysis_placeholder)
        # Over the map (map_overlay.py, asked for 2026-10-20 to be seen):
        # the same view, moved onto the canvas as translucent cards.
        overlay_row = QHBoxLayout()
        overlay_row.setContentsMargins(6, 4, 6, 0)
        self.overlay_check = QCheckBox("Over the map")
        self.overlay_check.setToolTip(
            "Lay the charts over the map as translucent cards. The cards "
            "take the mouse; hold Ctrl to reach the map through one.")
        self.overlay_check.toggled.connect(self._set_overlay)
        overlay_row.addWidget(self.overlay_check)
        self.overlay_note = QLabel("Shown over the map. Hold Ctrl to reach "
                                   "the map through a chart.")
        self.overlay_note.setStyleSheet("QLabel { color: #666; }")
        self.overlay_note.setVisible(False)
        overlay_row.addWidget(self.overlay_note, 1)
        analysis_layout.insertLayout(0, overlay_row)
        self.analysis_index = self.tabs.addTab(self.analysis_page, "Analysis")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        outer.addWidget(self.tabs, 1)

        # --- status -----------------------------------------------------
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        outer.addWidget(self.progress)

        status_row = QHBoxLayout()
        self.status_label = QLabel("Not connected.")
        self.status_label.setWordWrap(True)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Expanding,
                                        QSizePolicy.Policy.Preferred)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self._cancel_task)
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.cancel_btn)
        outer.addLayout(status_row)

        self.setWidget(container)
        self._set_busy(False)
        self._update_actions()


    def _data_source(self):
        """Where rows come from: the live API, or published parquet.

        Named apart from `_source_mode`, which is a different question (LIVE
        vs CACHE - whether the filters shape the request or the view). Two
        similarly-named things meaning different things is how this panel
        got into trouble before; see panel_state.py.
        """
        return self.source_combo.currentData() or settings_store.SOURCE_LIVE

    def _on_parquet_url_changed(self, text):
        """Keep the long token out of the way and readable on hover."""
        self.parquet_edit.setToolTip(text or "")

    def _on_source_changed(self):
        """Show only the credentials the chosen source actually uses.

        A published parquet URL carries its own credential - a SAS token is
        part of the URL - so the API key row is not merely unnecessary
        there, it is misleading: a filled-in key that is never sent is the
        kind of thing people spend an afternoon on.
        """
        parquet = self._data_source() == settings_store.SOURCE_PARQUET
        for widget in (self.site_edit, self.site_label,
                       self.key_edit, self.key_label, self.key_hint):
            if widget is not None:
                widget.setVisible(not parquet)
        for widget in (self.parquet_edit, self.parquet_label):
            if widget is not None:
                widget.setVisible(parquet)
        # Offline browsing is a cache of API responses; a parquet set is
        # already local-or-remote on its own terms and has no such cache.
        self.offline_btn.setVisible(not parquet)
        # A parquet set is read from files, so there is nothing to write to.
        self.read_only_check.setVisible(not parquet)
        self.connect_btn.setText("Open" if parquet else "Connect")

        # Step 3 chooses which report EQuIS should run. A parquet set has no
        # report and nothing to run, so the step is hidden rather than shown
        # disabled: a control that can never apply is worse than one that is
        # absent, because it invites the question of how to enable it.
        self.report_box.setVisible(not parquet)
        # The fetch goes for the same reason: every row is in hand once the
        # set opens. _capabilities() also refuses it, so this is the visible
        # half of the rule, not the rule.
        if hasattr(self, "fetch_section"):
            self.fetch_section.setVisible(not parquet)

        # The cache holds EQuIS API responses, keyed by SITE. On the parquet
        # path nothing reads or writes it - FetchManifestTask does not
        # mention it at all - but the label kept reporting whatever is
        # cached for the site URL still sitting in settings. That read as
        # "17.9 MB in 56 report(s)" beside data that came from none of them,
        # which is worse than useless: it describes a different dataset.
        for widget in (self.cache_label, self.clear_cache_btn,
                       getattr(self, "cache_row_label", None)):
            if widget is not None:
                widget.setVisible(not parquet)
        # Cleared as well as hidden. A hidden widget holding a false string
        # is how a stale number comes back the moment someone shows the row
        # again; _update_cache_label refills it on the live path.
        if parquet:
            self.cache_label.setText("")
        else:
            self._update_cache_label()

        # Step 4 survives, but it means something different. On the live path
        # it is saved onto a report and decides which rows EQuIS GENERATES;
        # here every row is already in hand and the same picker re-scopes
        # them locally. The title has to say which, or the seam the live
        # path's own comment is careful about is lost.
        self.selection_box.setTitle(
            "3. Choose chemicals - filtered here"
            if parquet else "4. Pre-fetch Data - sent to EQuIS")
        self._update_actions()

    def _load_settings(self):
        cfg = settings_store.load()
        self.site_edit.setText(cfg["site"])
        self.key_edit.setText(cfg["api_key"])
        self.verify_ssl_check.setChecked(cfg["verify_ssl"])
        self.read_only_check.setChecked(settings_store.load_read_only())
        self.parquet_edit.setText(cfg.get("parquet_url", ""))
        index = self.source_combo.findData(cfg.get("source",
                                                   settings_store.SOURCE_LIVE))
        if index >= 0:
            self.source_combo.blockSignals(True)
            self.source_combo.setCurrentIndex(index)
            self.source_combo.blockSignals(False)
        self._on_source_changed()
        # A key saved once and since expired keeps being sent, and every
        # request 401s in a way that looks like the service is down.
        self._offer_fresh_key()
        self._update_cache_label()
        # Re-checking the cache when the site changes keeps the label and the
        # offline button honest about which site they refer to.
        self.site_edit.textChanged.connect(self._update_cache_label)

        storage = settings_store.load_storage()
        index = self.storage_combo.findData(storage)
        if index >= 0:
            self.storage_combo.blockSignals(True)
            self.storage_combo.setCurrentIndex(index)
            self.storage_combo.blockSignals(False)
        self._update_storage_hint()

    # --------------------------------------------------------------- state

    def _set_status(self, message, level=Qgis.MessageLevel.Info):
        colors = {
            Qgis.MessageLevel.Info: "#333333",
            Qgis.MessageLevel.Warning: "#b36b00",
            Qgis.MessageLevel.Critical: "#b00020",
            Qgis.MessageLevel.Success: "#1b7f33",
        }
        self.status_label.setStyleSheet("color: %s;" % colors.get(level, "#333333"))
        self.status_label.setText(message)
        self._status_level = {Qgis.MessageLevel.Warning: "warning",
                              Qgis.MessageLevel.Critical: "critical",
                              Qgis.MessageLevel.Success: "success"}.get(level, "info")
        self._push_overlay_status()

    def _connected(self):
        """True when there is something to drive the workflow with.

        Offline counts: the cache stands in for the client, and every step
        below this point works from it.
        """
        return self._client is not None or self._offline

    def _set_busy(self, busy, indeterminate=True):
        """Show or hide the progress bar, then re-apply every capability.

        Only the progress widgets are touched here. Which CONTROLS a busy
        panel allows is _update_actions' business, and it derives that from
        _capabilities() - this used to enable five of them itself, with its
        own idea of the preconditions, which is how the panel ended up with
        controls that disagreed about whether it was busy.
        """
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0 if indeterminate else 100)
        self.cancel_btn.setVisible(busy)
        self._update_cache_label()
        self._update_report_actions()
        # By keyword: positional arguments are absorbed as stray signal
        # payloads, so passing this one positionally would silently do nothing.
        self._update_actions(busy=busy)

    def _update_cache_label(self):
        """Say how much is cached for this site, so the cache is not invisible."""
        if self._data_source() == settings_store.SOURCE_PARQUET:
            # Nothing on this path caches, so there is nothing honest to
            # report. The row is hidden too; this stops it being computed.
            self.cache_label.setText("")
            self._has_cache = False
            self.clear_cache_btn.setEnabled(False)
            return
        site = self._site()
        if not site:
            self.cache_label.setText("")
            self._has_cache = False
            self.clear_cache_btn.setEnabled(False)
            return
        entries = self._cache.list_entries(site=site)
        if not entries:
            self.cache_label.setText("Nothing cached for this site yet.")
            self._has_cache = False
            self.clear_cache_btn.setEnabled(False)
            return
        reports = [e for e in entries if e.report_id is not None]
        newest = min((e.age_seconds for e in entries), default=0.0)
        self.cache_label.setText(
            "%s in %d report(s), newest %s."
            % (describe_size(sum(e.bytes or 0 for e in entries)), len(reports),
               describe_age(newest))
        )
        # Whether there is anything TO clear; whether it may be cleared
        # right now is the capabilities' call, since a running task must not
        # have its cache deleted underneath it.
        self._has_cache = True
        self.clear_cache_btn.setEnabled(not self._is_busy())

    def _on_clear_cache(self):
        """Delete this site's cached data, after confirming."""
        site = self._site()
        if not site:
            return
        entries = self._cache.list_entries(site=site)
        if not entries:
            self._set_status("Nothing is cached for this site.")
            return
        total = describe_size(sum(e.bytes or 0 for e in entries))
        answer = QMessageBox.question(
            self,
            "Clear cached data?",
            "Delete %s of cached EQuIS data for %s from this machine?\n\n"
            "Anything still loaded stays on the map, but working offline will "
            "no longer be possible until these are fetched again."
            % (total, site),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = self._cache.clear(site=site)
        self._update_cache_label()
        self._populate_reports()
        self._set_status("Cleared %d cached item(s) (%s)." % (removed, total),
                         Qgis.MessageLevel.Success)

    def _on_profile_mode_changed(self, checked):
        """Redraw the active layer when depth profiles are switched.

        Unlike the sparkline checkbox this DOES restyle what is already
        mapped: a profile and a graduated circle are two drawings of the same
        rows, not two datasets, so there is no second view to preserve.
        """
        layer = self._result_layer
        if layer is None or not self._layer_alive(layer):
            return
        if self._active_aggregation != UNAGGREGATED:
            return
        analyte = self._analyte_for_layer(layer) or self._current_analyte()
        self._apply_symbology(layer, analyte, how=UNAGGREGATED)
        layer.triggerRepaint()

    def _sync_sparkline_availability(self):
        """Sparklines and "every result" are mutually exclusive.

        A sparkline draws a location's whole series into one marker, so it
        IS an aggregation; asking for one per result row would paint the same
        marker on top of itself once per result. Unchecked as well as
        disabled, so the state the user can see is the state that will be
        used - a checked-but-ignored box would be a lie.

        Deliberately touches only the checkbox: the map button must stay
        usable whatever this says.
        """
        unaggregated = self.aggregation_combo.currentData() == UNAGGREGATED
        if unaggregated and self.sparkline_check.isChecked():
            self.sparkline_check.setChecked(False)
        self.sparkline_check.setEnabled(not unaggregated)
        # The mirror image: profiles are meaningless on an aggregated layer,
        # which has one result per location and so no profile to draw.
        self.profile_check.setEnabled(unaggregated)
        self.sparkline_check.setToolTip(
            "Every result is already one point per sample, so there is no "
            "series left to draw into a marker."
            if unaggregated else SPARKLINE_TOOLTIP)

    def _on_aggregation_changed(self, *_args):
        """The aggregate changed, so the mapped layer is now out of date."""
        self._sync_sparkline_availability()
        self._update_actions()
        if self._fsm.interactive:
            self._schedule_preview()
            return
        if self._result_layer is not None and self._mapped_chemical:
            self._set_status(
                "Aggregate changed. Press '%s' to rebuild the layer with it."
                % self.map_btn.text(),
                Qgis.MessageLevel.Info,
            )

    # -------------------------------------------------------------- state

    def _on_state_violation(self, message):
        """A transition with no path was attempted.

        Logged and ignored rather than raised: in the field a missed edge
        case should degrade to the old behaviour, not break the panel in the
        middle of someone's afternoon. The message goes to the QGIS log
        rather than the status bar, because it describes a programming
        mistake and there is nothing the user can do about it.
        """
        QgsMessageLog.logMessage(
            "EQuIS panel: %s (from %s)" % (message, self._fsm.describe()),
            "EQuIS", Qgis.MessageLevel.Warning,
        )

    def _enter(self, state, note=""):
        """Move the machine, then re-apply what the new state allows.

        Every caller that changes what the panel can do goes through here, so
        the widgets follow the state instead of each caller remembering to
        update them.
        """
        moved = self._fsm.enter(state, note)
        self._update_actions()
        if self._fsm.interactive:
            # Rows arrived (draw the pick) or went (take the preview down).
            self._schedule_preview()
        return moved

    # ------------------------------------------------------- capabilities

    def _row_count(self):
        """How many rows are in hand, whichever source they came from.

        `len(table.rows)` was asked in three places and is wrong in all of
        them for a parquet source, where there is no table - one of them
        raised AttributeError the moment the panel reached LOADED.
        """
        analysis = self._analysis
        if analysis is None:
            return 0
        rows = getattr(analysis, "rows", None)
        if rows is not None:
            return len(rows)
        try:
            return analysis.count()
        except Exception:                       # noqa: BLE001
            return 0

    def _capabilities(self):
        """Everything the panel may do right now, derived in ONE place.

        Enablement used to be decided in eleven functions, each re-deriving
        readiness from its own combination of _client, _offline, _task,
        _report_table and _location_pages. That is what let controls
        contradict each other: the chemical list stayed live during a fetch
        because the only function that would have locked it was never called,
        and "busy" was read in one place from a progress bar's VISIBILITY -
        a widget standing in for program state.

        This is not a state machine. The panel is not sequential: offline is
        orthogonal to every stage, a cache lets fetching be skipped, and
        rolling is a mode on top of a loaded layer. Modelling that as states
        needs parallel regions and buys less than naming the preconditions
        directly. What it does give is the property a machine would: exactly
        one place to be right, and one that can be tested without a UI.

        Returns {name: bool}. Read it; never re-derive a member of it.
        """
        busy = self._is_busy()
        # Kept in step with the machine here rather than being set at every
        # call site: busy is about a TASK, the state is about progress, and
        # conflating them is what made "busy" get read off a progress bar.
        self._fsm.busy = busy
        online = self._client is not None and not self._offline
        have_facilities = bool(self._facilities)
        have_report = self._current_report() is not None
        parquet = self._parquet_url is not None
        # Rows in hand. Not the same as "a report is chosen": a chosen report
        # that has never run can draw nothing.
        # `_analysis`, not `_report_table`: a parquet source has rows with no
        # table behind them, and asking the narrower question left the map
        # button dead while 3.45 million rows sat loaded.
        have_rows = self._analysis is not None
        have_locations = (self._location_pages is not None
                          and self._has_geometry)
        have_analyte = self._current_analyte() is not None
        # Whether the rows in hand answer the CURRENT selection. When they do
        # not, mapping would draw the previous fetch's chemicals under the
        # new selection's name, so the map button goes down and the fetch
        # button is what lights up instead.
        stale = have_rows and self._needs_fetch()
        # Kept in step with the machine the same way busy is, so the stale
        # line, the map button and the fetch tooltip all read one answer.
        # have_rows is true exactly when LOADED was entered, so this never
        # asks the machine to be stale in a state that has no rows.
        self._fsm.set_stale(stale)
        mapped = (self._result_layer is not None
                  and self._layer_alive(self._result_layer))

        return {
            # The machine's own view, so a caller can ask where the panel is
            # without re-deriving it from the flags below.
            "state": self._fsm.state,
            "mode": self._fsm.mode,
            # --- connection ---------------------------------------------
            "connect": not busy,
            "work_offline": not busy,
            # _has_cache is maintained by _update_cache_label; listing the
            # cache here would hit the disk on every enablement pass.
            "clear_cache": not busy and self._has_cache,
            # --- choosing what to fetch ---------------------------------
            "pick_facility": not busy and have_facilities,
            "prefetch_controls": not busy,
            # Fetching needs a report to run; refreshing also needs the
            # network, since it deliberately bypasses the cache.
            # A parquet session never fetches. have_report alone said yes
            # whenever a client survived from an earlier Connect, because
            # the own-report stand-in is always "chosen".
            "fetch": not busy and have_report and not parquet,
            "refresh": not busy and have_report and online and not parquet,
            # --- acting on fetched rows ---------------------------------
            # Locations are required: results are drawn onto location points,
            # so rows with nowhere to sit produce an empty layer.
            "map": (not busy and have_analyte and have_locations
                    and have_rows and not stale),
            # Exporting needs rows and something to export, but NOT a mapped
            # layer or locations: the CSV is the report data behind the map,
            # and it is still worth having when the locations have no
            # coordinates and nothing can be drawn at all.
            "export": not busy and have_rows and have_analyte,
            # Surfaced so the panel can say WHY mapping is unavailable, which
            # is the difference between a dead button and an obvious next step.
            "stale": stale,
            "clear_layers": not busy,
            # --- acting on a mapped layer -------------------------------
            "pick_tool": mapped,
            # A roll cycles a multi-selection; interactive mode is single.
            "roll": mapped and not self._fsm.interactive,
            # Interactive mode: something drawn that can be kept.
            "keep": (not busy and self._fsm.interactive
                     and self._preview_layer is not None
                     and self._layer_alive(self._preview_layer)),
            "have_rows": have_rows,
            "busy": busy,
        }

    def _update_actions(self, *args, busy=False):
        """Apply the current capabilities to every control.

        This is the ONLY place that enables or disables an action, and it
        reads _capabilities() rather than deciding anything itself. Anything
        that changes what the panel can do calls this and stops.

        `busy` is accepted for callers that know a task is starting before
        _task is set, and is OR-ed with what _capabilities() derives - never
        allowed to claim NOT busy when a task is genuinely in flight.

        Qt signals deliver positional arguments, and a combo's index arriving
        as `busy` silently disabled the map button for whichever entry was
        not index 0, so extra positional arguments are absorbed rather than
        trusted.
        """
        can = self._capabilities()
        busy = bool(busy) or can["busy"]
        has_analyte = self._current_analyte() is not None
        has_locations = self._location_pages is not None and self._has_geometry

        self.map_btn.setEnabled(can["map"] and not busy)
        self.keep_btn.setEnabled(can["keep"])
        self.clear_layers_btn.setEnabled(can["clear_layers"] and not busy)
        self.export_btn.setEnabled(can["export"] and not busy)
        self.connect_btn.setEnabled(can["connect"] and not busy)
        self.offline_btn.setEnabled(can["work_offline"] and not busy)
        self.facility_combo.setEnabled(can["pick_facility"] and not busy)
        self.run_report_btn.setEnabled(can["fetch"] and not busy)
        self.refresh_report_btn.setEnabled(can["refresh"] and not busy)
        # The controls that describe what a running fetch asked for. Leaving
        # these live during a fetch is what allowed a second one to be
        # launched on top of the first.
        self.selection_panel.set_enabled(not busy)
        if busy:
            self.map_selection_check.setEnabled(False)
        else:
            self._refresh_map_selection_label()
        self.pick_btn.setEnabled(can["pick_tool"])
        self.roll_btn.setEnabled(can["roll"] and not busy)
        if self.pick_action is not None:
            self.pick_action.setEnabled(can["pick_tool"])

        # A disabled button with no explanation is the worst outcome, so say
        # which precondition is missing.
        if busy:
            reason = "Busy - wait for the current operation to finish."
        elif not has_locations:
            info = self._location_layer_info() or {}
            if self._location_pages is not None and not self._has_geometry:
                reason = (
                    "This facility's locations have no coordinates in EQuIS, "
                    "so results cannot be placed on the map."
                )
            elif info.get("count"):
                reason = (
                    "This facility's locations have no coordinates in EQuIS, "
                    "so results cannot be mapped."
                )
            else:
                reason = "This facility has no location points to map."
        elif not has_analyte:
            reason = "Select a chemical to map."
        elif not can["have_rows"]:
            reason = ("Nothing fetched yet. Press 'Pre-fetch data' in step 4 "
                      "to retrieve the rows to map.")
        elif can["stale"]:
            # The single most useful message this panel shows: the selection
            # has moved past the data, and the fix is one button away.
            reason = ("The chemicals selected are not in the data fetched. "
                      "Press 'Pre-fetch data' in step 4 to retrieve them.")
        else:
            reason = "Add a layer of this chemical's aggregated results."

        # Say how many layers the button will add, so a multi-selection is not
        # a surprise at the point of pressing it.
        selected = len(self._selected_analytes())
        if selected > 1:
            self.map_btn.setText("Map %d chemicals" % selected)
        else:
            self.map_btn.setText("Map chemical")
        if can["map"]:
            reason = ("Add %s from the data already fetched."
                      % ("one layer per chemical (%d)" % selected
                         if selected > 1 else "a layer"))
        self.map_btn.setToolTip(reason)

        # When the data is stale the fetch is the next step, so say so on the
        # button that performs it rather than leaving the user to work out
        # which of the two is now the live one.
        if can["stale"] and not busy:
            self.run_report_btn.setToolTip(
                "The chemicals selected are not in the data fetched. This "
                "retrieves them.")
        self._refresh_step_hints(can, busy)

        # Whatever changed what the panel can do changed what the overlay's
        # Data panel should say.
        self._push_overlay_status()

    def _auto_connect(self):
        # Never discard a session that is already loaded: this fires from a
        # timer, so it can land after the user has a report open.
        #
        # `_analysis`, not `_report_table`: a parquet session has rows and no
        # ReportTable, and no facility list either, so it passed both checks
        # and the timer replaced it with a live connection. Found 2026-10-01
        # when a test harness ran the event loop and a parquet session turned
        # into "Connected. 33 facilities available."
        if self._analysis is not None or self._facilities:
            return
        if self.site_edit.text().strip() and self.key_edit.text().strip():
            self._on_connect()
        elif self.site_edit.text().strip():
            # No key, but this site may still have been cached by an earlier
            # session; offer that rather than an empty panel.
            if self._cache.cached_facility_ids(self._site()):
                self._set_status(
                    "No API key entered. Cached data for this site is "
                    "available - press 'Work offline' to use it.",
                    Qgis.MessageLevel.Info,
                )

    # --------------------------------------------------- parquet source

    def _open_parquet_source(self):
        """Open a published parquet set instead of querying the API.

        The manifest is fetched in the background because a bad URL can hang
        for the full timeout; the store is opened here on the main thread,
        where it is cheap and where the connection stays on the thread that
        will use it.
        """
        url = self.parquet_edit.text().strip()
        if not url:
            self._set_status(
                "Enter the URL of the folder holding the parquet partitions. "
                "The plugin reads manifest.json from it.",
                Qgis.MessageLevel.Warning)
            return
        if self._report_table is not None or self._report_store is not None:
            answer = QMessageBox.question(
                self, "Replace the loaded data?",
                "Opening a parquet source clears what is currently loaded "
                "and the layers built from it. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return

        settings_store.save(
            self.site_edit.text().strip(),
            self.key_edit.text().strip(),
            self.verify_ssl_check.isChecked(),
            source=self._data_source(),
            parquet_url=url)

        self._set_busy(True)
        self._set_status("Opening the published parquet…")
        task = FetchManifestTask(url)
        # Each phase says what it is doing. Opening is several seconds and
        # most of it is not the manifest, so one static message read as a
        # hang - and named the wrong thing while it did.
        task.note.connect(self._on_parquet_note)
        task.finishedOk.connect(
            lambda _r, t=task, u=url: self._on_manifest_ready(t, u))
        task.failed.connect(self._on_parquet_failed)
        self._manifest_task = task
        QgsApplication.taskManager().addTask(task)

    def _on_manifest_ready(self, task, url):
        if task is not getattr(self, "_manifest_task", None):
            return                      # superseded by a newer open
        self._manifest_task = None
        # The task opened AND warmed the store; nothing slow is left to do
        # here. Its DuckDB connection crosses threads exactly once, at this
        # point, and is only ever used from this one afterwards - unlike
        # BuildStoreTask, which writes a file precisely because its store
        # would otherwise be used on both.
        store = getattr(task, "store", None)
        if store is None:
            self._on_parquet_failed("the parquet did not open")
            return
        self._discard_store()
        self._report_store = store
        # Before the pickers are filled from it, so their counts are the
        # rows a build would draw under the dates and QC already set.
        self._apply_row_scope()
        self._schedule_analysis_warmup()
        self._parquet_url = url
        self._offline = False
        self._set_busy(False)
        rows = store.count()

        # THE STATE MACHINE, DELIBERATELY. A parquet source arrives with the
        # rows already in hand, so it short-circuits the pipeline the live
        # path walks: no report to prepare, nothing to fetch.
        #
        # FACILITY then LOADED, not straight to LOADED, because every step
        # here must be a legal arrow - panel_state's whole point is that an
        # unlisted one means the panel reached somewhere it has no path to.
        # FACILITY -> LOADED already exists for the saved-report and cached
        # paths, which skip PREPARED for the same reason this does.
        self._show_parquet_facility(store)
        self._load_parquet_locations(store)
        self._enter(st.FACILITY, "parquet facility named by the data")
        self._populate_from_store()
        self._enter(st.LOADED, "parquet rows are already loaded")

        self._set_status(
            "Opened %s rows from the published parquet."
            % format(rows, ",d"), Qgis.MessageLevel.Success)
        QgsMessageLog.logMessage(
            "Parquet source opened: %s rows from %s"
            % (format(rows, ",d"), url), "EQuIS", Qgis.Info)
        # Fold the connection away exactly as a successful Connect does.
        self._advance_to(self.connect_box, self.selection_box)
        self._update_actions()

    def _populate_from_store(self):
        """Fill the pickers from the store, with no report and no API.

        The live path learns its choices from the parameter service before
        any rows exist. A parquet set has the rows already, so every choice
        is answered by the data itself - which is both simpler and more
        honest: these are the analytes that ARE here, not the ones the
        facility might have.
        """
        store = self._report_store
        if store is None:
            return
        rows = store.analytes()
        self._offered_analytes = [
            (row.get("cas_rn") or "", row.get("chemical") or "")
            for row in rows]
        # Counts are passed here where the live path cannot: the rows are
        # already in hand, so the list can say how much of each analyte is
        # present rather than only that it exists.
        counts = {(row.get("cas_rn") or ""): row.get("count") or 0
                  for row in rows}
        self.selection_panel.set_analytes(self._offered_analytes, counts)
        # (code, label) pairs. The parquet carries no descriptive matrix
        # labels - those come from an EQuIS lookup the extract does not
        # include - so the code stands as its own label rather than showing
        # a blank beside it.
        self.selection_panel.set_matrices(
            [(row["matrix"], row["matrix"]) for row in store.matrices()],
            replace=True)
        # No location groups: those are an EQuIS grouping the parquet does
        # not carry. An empty list hides the Where row rather than offering
        # something that cannot be honoured.
        self.selection_panel.set_location_groups([])
        # Method and fraction come from the rows, which are all here. The
        # live path cannot offer them until after a fetch; this one can
        # offer them immediately, and _sync_analysis_filters re-scopes them
        # whenever the chemical or matrix changes.
        self.selection_panel.clear_result_filters()
        self._sync_analysis_filters(store)
        self.selection_panel.set_source(PARQUET_SOURCE)

    def _show_parquet_facility(self, store):
        """Name the facility from the rows, since nothing else can.

        The live path learns the facility from EQuIS before any rows exist.
        A parquet set carries FACILITY_ID and FACILITY_CODE on every row, so
        the answer is in the data - and it is the honest one: whatever the
        extract holds, not whatever the tenant lists.
        """
        code_column = store._raw_alias("FACILITY_CODE")
        id_column = store._raw_alias("FACILITY_ID")
        found = []
        if code_column:
            try:
                found = store.con.execute(
                    "SELECT DISTINCT %s, %s FROM raw "
                    "WHERE coalesce(CAST(%s AS VARCHAR), '') <> '' ORDER BY 1"
                    % (code_column, id_column or "NULL", code_column)
                ).fetchall()
            except Exception:                   # noqa: BLE001
                found = []
        self.facility_combo.blockSignals(True)
        self.facility_combo.clear()
        for code, identifier in found:
            # The SAME dict shape the live path stores, because
            # _current_facility() is read by everything downstream -
            # _update_active_label, the layer names, the fallback WKID. A
            # bare id here raised TypeError the moment a layer was named.
            # wkid 4326: the parquet's LATITUDE/LONGITUDE are degrees.
            self.facility_combo.addItem(
                "%s (%s)" % (code, identifier),
                {"name": code, "code": code, "id": identifier, "wkid": 4326})
        self.facility_combo.blockSignals(False)
        # The filter would offer to narrow a list that came from the data and
        # cannot be changed.
        self.facility_filter.setVisible(False)
        self.facility_combo.setEnabled(False)

    def _parquet_facility_name(self):
        return (self.facility_combo.currentText() or "").split(" (")[0]

    def _load_parquet_locations(self, store):
        """Location points from the parquet, as the live path gets them from
        the feature service.

        The SAME `[(feature_set, features)]` pages, so build_feature_layer
        and the result builders take them unchanged - see
        ReportStore.location_features for why that matters more than the
        convenience.
        """
        pages = store.location_features()
        if not pages:
            self._location_pages = None
            self._has_geometry = False
            self.locations_label.setText(
                "The parquet carries no location coordinates, so results "
                "cannot be placed on the map.")
            return
        self._location_pages = pages
        self._has_geometry = any(
            feature.get("geometry") for _, features in pages
            for feature in features)
        total = sum(len(features) for _, features in pages)
        placed = sum(1 for _, features in pages for feature in features
                     if feature.get("geometry"))

        if not self._has_geometry:
            # Adding this layer would put an invisible, unclickable thing on
            # the map, so say so instead - the same choice the live path
            # makes when a facility's locations carry no coordinates.
            self.locations_label.setText(
                "%s location(s) in the parquet, none with coordinates, so "
                "results cannot be placed on the map." % format(total, ",d"))
            return

        layer, skipped = build_feature_layer(
            "%s - Locations" % (self._parquet_facility_name() or "Parquet"),
            pages)
        if layer is None or layer.featureCount() == 0:
            self._location_pages = None
            self._has_geometry = False
            self._set_status("Could not build the locations layer.",
                             Qgis.MessageLevel.Warning)
            return

        # One source at a time: replace whatever points were there before.
        # A map tip with equis:// links, so a location can be plotted from
        # the map itself rather than by reading its code off an identify
        # panel and retyping it into a picker.
        # Two routes to the same links, deliberately. The map tip shows on
        # hover; the ACTIONS appear in QGIS's own identify panel, the
        # attribute table and the feature form - no mode to enter and no
        # second opinion about what a click on the map means, which a
        # QgsMapToolIdentify subclass would have introduced beside the
        # panel's existing pick tool.
        try:
            from .url_actions import install_actions, map_tip
            layer.setMapTipTemplate(map_tip())
            install_actions(layer)
        except Exception:                       # noqa: BLE001
            pass                                # neither is worth a failure

        self._remove_layer(self._locations_layer)
        QgsProject.instance().addMapLayer(layer)
        self._locations_layer = layer
        layer.selectionChanged.connect(self._on_map_selection_changed)
        self._refresh_map_selection_label()
        self._zoom_to(layer)
        self._update_active_label()

        # Unplaced locations are reported rather than quietly dropped: the
        # store can know a location the map cannot place, and a count that
        # silently shrinks is how "why is my site missing" starts.
        if placed < total:
            self.locations_label.setText(
                "%s location point(s) on the map; %s of %s in the parquet "
                "have no coordinates."
                % (format(placed, ",d"), format(total - placed, ",d"),
                   format(total, ",d")))
        else:
            self.locations_label.setText(
                "%s location point(s) on the map." % format(placed, ",d"))

    def _on_parquet_note(self, message):
        """Say what the open is doing right now.

        Status line only, never the message bar: these are progress notes,
        several per open, and each one replaces the last.
        """
        self._set_status(message)

    def _on_parquet_failed(self, message):
        self._manifest_task = None
        self._set_busy(False)
        # Shown, not just logged: unlike the store accelerator, this IS the
        # data the user asked for, so a failure leaves them with nothing and
        # they need to know why.
        self._set_status("Could not open the parquet source: %s" % message,
                         Qgis.MessageLevel.Critical)

    # ----------------------------------------------------------- 1. connect

    def _on_connect(self):
        # A parquet source shares none of the steps below - no client, no
        # token expiry, no facility list to fetch - so it branches before
        # any of it rather than threading `if parquet` through each one.
        if self._data_source() == settings_store.SOURCE_PARQUET:
            self._open_parquet_source()
            return
        # Reconnecting drops the loaded report, which can be minutes of work.
        if self._report_table is not None:
            answer = QMessageBox.question(
                self,
                "Reconnect?",
                "Reconnecting clears the loaded report '%s' and the layer built "
                "from it. Continue?" % (self._active_report or {}).get("name", ""),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            client = EquisClient(
                self.site_edit.text(),
                self.key_edit.text(),
                verify_ssl=self.verify_ssl_check.isChecked(),
                read_only=self.read_only_check.isChecked(),
            )
        except EquisError as exc:
            self._set_status(str(exc), Qgis.MessageLevel.Critical)
            return
        # An expired key fails every request with a 401 that looks like a
        # server problem. The expiry is readable from the key itself, so catch
        # it here rather than after a round trip.
        expiry = token_expiry(self.key_edit.text().strip())
        if expiry is not None and expiry <= utc_now():
            self._set_status(
                "This API key expired on %s UTC. Generate a new one in EQuIS "
                "and paste it above." % expiry.strftime("%Y-%m-%d %H:%M"),
                Qgis.MessageLevel.Critical,
            )
            return

        self._client = client
        self._offline = False
        settings_store.save(
            self.site_edit.text().strip(),
            self.key_edit.text().strip(),
            self.verify_ssl_check.isChecked(),
            source=self._data_source(),
            parquet_url=self.parquet_edit.text().strip(),
        )

        self.facility_combo.clear()
        self._reset_report_state()
        self._set_busy(True)
        self._set_status("Connecting to EQuIS...")
        self._warn_if_key_expiring(expiry)

        task = FetchFacilitiesTask(self._client, cache=self._cache,
                                   site=self._site())
        task.finishedOk.connect(lambda _r, t=task: self._on_facilities_loaded(t))
        task.failed.connect(self._on_task_failed)
        self._start(task)

    def _site(self):
        return self.site_edit.text().strip()

    def _on_work_offline(self):
        """Enter offline mode explicitly, with no client at all."""
        site = self._site()
        if not site:
            self._set_status("Enter the site URL to work offline with its "
                             "cached data.", Qgis.MessageLevel.Warning)
            return
        facilities, entry = self._cache.get_facilities(site)
        cached_ids = set(self._cache.cached_facility_ids(site))
        if not facilities and not cached_ids:
            self._set_status(
                "Nothing is cached for %s, so there is nothing to work with "
                "offline. Connect once to build the cache." % site,
                Qgis.MessageLevel.Warning,
            )
            return

        self._client = None
        self._offline = True
        # Cached data is a source like any other, so this is CONNECTED with
        # the offline flag set - not a state of its own.
        self._fsm.offline = True
        self._fsm.enter(st.CONNECTED, "working offline")
        self.facility_combo.clear()
        self._reset_report_state()
        self._facilities = self._offline_facilities(facilities, cached_ids)
        self._populate_facilities()
        self._advance_to(self.connect_box, self.facility_box)
        self._announce_offline(
            "Working offline. %d facility(ies) available from the cache%s."
            % (len(self._facilities),
               ", stored %s" % entry.age_text if entry else "")
        )
        self._update_active_label()
        self._update_cache_label()

    def _offline_facilities(self, facilities, cached_ids):
        """Facilities worth showing offline: those with cached contents.

        A list of 400 facilities where 399 cannot be opened is worse than a
        list of the one that can, so this hides the rest. Facilities cached
        without their site's facility list are recovered from their own stored
        metadata.
        """
        usable = [f for f in (facilities or []) if f.get("id") in cached_ids]
        known = {f.get("id") for f in usable}
        for facility_id in cached_ids:
            if facility_id in known:
                continue
            meta, _ = self._cache.get_facility_meta(self._site(), facility_id)
            record = (meta or {}).get("facility") or {}
            if record.get("id") is None:
                record = {"id": facility_id, "code": "",
                          "name": "Facility %s" % facility_id, "wkid": 0}
            usable.append(record)
        usable.sort(key=lambda f: str(f.get("name", "")).lower())
        return usable

    def _offer_fresh_key(self):
        """Swap in a newer .env key when the saved one has expired.

        The saved key wins over the .env by design, so a key that expires
        after being saved keeps being sent and every request comes back 401.
        That reads as the service being down, not as a stale credential -
        which is exactly the confusion this avoids.
        """
        try:
            expiry, fresh = settings_store.stale_saved_key()
        except Exception:  # noqa: BLE001 - never block startup on this
            return
        if not fresh:
            return
        self.key_edit.setText(fresh)
        self._set_status(
            "The saved API key expired on %s UTC, so requests were being "
            "refused. A newer key was found beside the plugin and has been "
            "filled in - press Connect to use it."
            % expiry.strftime("%Y-%m-%d %H:%M"),
            Qgis.MessageLevel.Warning,
        )

    def _on_key_changed(self, text):
        """Describe a pasted key's expiry immediately, before connecting."""
        expiry = token_expiry((text or "").strip())
        if expiry is None:
            self.key_hint.setText("")
            return
        now = utc_now()
        stamp = expiry.strftime("%Y-%m-%d %H:%M")
        if expiry <= now:
            self.key_hint.setText("This key expired on %s UTC." % stamp)
            self.key_hint.setStyleSheet("color: #a01010;")
        elif (expiry - now).days <= 7:
            self.key_hint.setText("This key expires %s UTC." % stamp)
            self.key_hint.setStyleSheet("color: #a05a00;")
        else:
            self.key_hint.setText("Key valid until %s UTC." % stamp)
            self.key_hint.setStyleSheet("color: #5a6b7b;")

    def _warn_if_key_expiring(self, expiry):
        """Flag a key that is about to lapse, while it still works.

        A key that dies mid-session looks like the service breaking, and the
        report loaded at the time can be minutes of work. A week's notice is
        enough to renew it between sessions.
        """
        if expiry is None:
            return
        days = (expiry - utc_now()).total_seconds() / 86400.0
        if 0 < days <= 7:
            self.key_hint.setText(
                "This API key expires %s (%s UTC)."
                % ("today" if days < 1 else "in %d day(s)" % int(days),
                   expiry.strftime("%Y-%m-%d %H:%M"))
            )
            self.key_hint.setStyleSheet("color: #a05a00;")
        else:
            self.key_hint.setText("")

    def _announce_offline(self, message):
        """Say once, loudly, that the session is no longer live."""
        self._set_status(message, Qgis.MessageLevel.Warning)
        bar = self.iface.messageBar()
        widget = bar.createMessage("EQuIS - offline", message)
        retry = QPushButton("Retry connection")
        retry.pressed.connect(self._on_retry_connection)
        widget.layout().addWidget(retry)
        bar.pushWidget(widget, Qgis.MessageLevel.Warning, 0)

    def _on_retry_connection(self):
        self.iface.messageBar().clearWidgets()
        self._on_connect()

    def _on_facilities_loaded(self, task):
        self._task = None
        self._set_busy(False)
        facilities = task._result or []
        if task.source == CACHE_FALLBACK:
            # The connection failed but this site had been loaded before, so
            # continue from the cache rather than dead-ending on a red error.
            self._client = None
            self._offline = True
            # A live read failed and the cache answered instead. Offline is
            # orthogonal to the pipeline - it changes the SOURCE, not the
            # progress - but a fallback can be the FIRST thing that succeeds,
            # so the panel may still be DISCONNECTED here. Entering CONNECTED
            # is what makes cached data a source like any other.
            self._fsm.offline = True
            self._fsm.enter(st.CONNECTED, "fell back to cache")
            cached_ids = set(self._cache.cached_facility_ids(self._site()))
            self._facilities = self._offline_facilities(facilities, cached_ids)
            self._populate_facilities()
            self._advance_to(self.connect_box, self.facility_box)
            self._announce_offline(
                "Could not reach EQuIS (%s). Working offline from cached data "
                "stored %s - %d facility(ies) available."
                % (task.error, task.entry.age_text if task.entry else "earlier",
                   len(self._facilities))
            )
            self._update_active_label()
            self._update_cache_label()
            return

        self._facilities = facilities
        if not self._facilities:
            self._set_status(
                "Connected, but no facilities are available to this account.",
                Qgis.MessageLevel.Warning,
            )
            return
        self._populate_facilities()
        self._advance_to(self.connect_box, self.facility_box)
        self._set_status(
            "Connected. %d facilities available. Select one."
            % len(self._facilities),
            Qgis.MessageLevel.Success,
        )
        self._update_cache_label()

    # ---------------------------------------------------------- 2. facility

    def _populate_facilities(self):
        """Fill the combo, leaving nothing selected.

        Loading a facility fetches its locations and reports, which is slow and
        not something to do to whichever site happens to sort first. The user
        picks.
        """
        needle = self.facility_filter.text().strip().lower()
        current = self._current_facility()
        self.facility_combo.blockSignals(True)
        self.facility_combo.clear()
        # A placeholder in slot 0 so there is a "nothing chosen" state.
        self.facility_combo.addItem("-- select a facility --", None)
        for facility in self._facilities:
            haystack = "%s %s" % (facility["name"], facility["code"])
            if needle and needle not in haystack.lower():
                continue
            label = facility["name"]
            if facility["code"] and facility["code"] != facility["name"]:
                label = "%s  (%s)" % (facility["name"], facility["code"])
            self.facility_combo.addItem(label, facility)

        # Filtering must not silently drop the facility already loaded.
        index = 0
        if current is not None:
            for row in range(1, self.facility_combo.count()):
                data = self.facility_combo.itemData(row)
                if data and data.get("id") == current.get("id"):
                    index = row
                    break
        self.facility_combo.setCurrentIndex(index)
        self.facility_combo.blockSignals(False)
        self._update_actions()

    def _filter_facilities(self):
        if self._facilities:
            self._populate_facilities()

    def _current_facility(self):
        index = self.facility_combo.currentIndex()
        return self.facility_combo.itemData(index) if index >= 0 else None

    def _on_facility_changed(self, _index):
        facility = self._current_facility()
        if not facility or not self._connected():
            # The placeholder row: clear whatever the last facility left.
            if self._connected() and self._facilities:
                self._reset_report_state()
                self._enter(st.CONNECTED, "facility cleared")
                self.locations_label.setText("")
                self._set_status("Select a facility.")
            return
        # Switching facility is how the user changes context, so it replaces
        # the active session outright rather than asking - but say so, since
        # a loaded report and its layer are being discarded.
        had_report = self._report_table is not None
        self._reset_report_state()
        self._enter(st.FACILITY, "facility chosen")
        self.locations_label.setText("")
        self._set_busy(True)
        self._set_status(
            "Loading %s...%s" % (
                facility["name"],
                " Previous report and layers cleared." if had_report else "",
            )
        )

        task = FetchFacilityDetailTask(
            self._client, facility["id"], cache=self._cache, site=self._site(),
            prefer_cache=self._offline, facility=facility,
        )
        task.finishedOk.connect(lambda _r, t=task: self._on_detail_loaded(t))
        task.failed.connect(self._on_task_failed)
        self._start(task)

    def _on_detail_loaded(self, task):
        self._task = None
        detail = task._result or {}
        self._layers = detail.get("layers") or []
        self._reports = detail.get("reports") or []
        self._service_wkid = detail.get("service_wkid") or 0
        if task.source == CACHE_FALLBACK:
            self._client = None
            self._offline = True
            # A live read failed and the cache answered instead. Offline is
            # orthogonal to the pipeline - it changes the SOURCE, not the
            # progress - but a fallback can be the FIRST thing that succeeds,
            # so the panel may still be DISCONNECTED here. Entering CONNECTED
            # is what makes cached data a source like any other.
            self._fsm.offline = True
            self._fsm.enter(st.CONNECTED, "fell back to cache")
            self._announce_offline(
                "Could not reach EQuIS (%s). Showing this facility from cached "
                "data stored %s."
                % (task.error, task.entry.age_text if task.entry else "earlier")
            )
        self._populate_reports()
        self._update_own_report_label()
        # Step 4: the location points go on the map straight away.
        self._load_location_points()

    # --------------------------------------------- 3/4. location points

    def _location_layer_info(self):
        for layer in self._layers:
            if layer["id"] == LOCATION_LAYER_ID:
                return layer
        return None

    def _load_location_points(self):
        facility = self._current_facility()
        info = self._location_layer_info()
        if not facility or info is None:
            self._set_busy(False)
            self._set_status(
                "This facility publishes no location point layer.",
                Qgis.MessageLevel.Warning,
            )
            return
        if info.get("count") == 0:
            self._set_busy(False)
            self.locations_label.setText("This facility has no location points.")
            self._set_status("No locations to map.", Qgis.MessageLevel.Warning)
            return

        self._set_busy(True, indeterminate=False)
        self._set_status(
            "Loading cached location points..." if self._offline
            else "Loading location points..."
        )
        task = LoadLayersTask(
            self._client,
            facility["id"],
            facility["name"],
            [info],
            facility.get("wkid") or self._service_wkid,
            cache=self._cache,
            site=self._site(),
            prefer_cache=self._offline,
        )
        task.progressChanged.connect(self._on_progress)
        task.finishedOk.connect(lambda _r, t=task: self._on_locations_loaded(t))
        task.failed.connect(self._on_task_failed)
        # Prepared after the locations rather than beside them: _start tracks
        # one task at a time, so launching both would leave the first
        # untrackable. Queued on either outcome - the pickers do not depend on
        # the locations having loaded.
        task.finishedOk.connect(lambda *_a: self._prepare_live_report())
        task.failed.connect(lambda *_a: self._prepare_live_report())
        self._start(task)

    def _on_locations_loaded(self, task):
        self._task = None
        self._set_busy(False)
        self._locations_source = task.source
        if task.source == CACHE_FALLBACK:
            self._client = None
            self._offline = True
            # A live read failed and the cache answered instead. Offline is
            # orthogonal to the pipeline - it changes the SOURCE, not the
            # progress - but a fallback can be the FIRST thing that succeeds,
            # so the panel may still be DISCONNECTED here. Entering CONNECTED
            # is what makes cached data a source like any other.
            self._fsm.offline = True
            self._fsm.enter(st.CONNECTED, "fell back to cache")
            self._announce_offline(
                "Could not reach EQuIS (%s). Showing locations cached %s."
                % (task.error, task.entry.age_text if task.entry else "earlier")
            )
        pages = []
        for entry in task._result or []:
            pages.extend(entry["pages"])
        if not pages:
            info = self._location_layer_info() or {}
            expected = info.get("count") or 0
            if expected:
                # The layer has rows but served none, which on this service
                # means none of them carry coordinates.
                message = (
                    "%s location(s) exist for this facility but none have "
                    "coordinates in EQuIS, so nothing can be mapped. The "
                    "report will still load; results just cannot be placed."
                    % format(expected, ",d")
                )
            else:
                message = "No location points returned."
            self.locations_label.setText(message)
            self._set_status(message, Qgis.MessageLevel.Warning)
            self._update_actions()
            return
        self._location_pages = pages
        self._has_geometry = any(
            f.get("geometry") for _, features in pages for f in features
        )

        facility = self._current_facility() or {}
        layer, skipped = build_feature_layer(
            cached_layer_name(
                "%s - Locations" % facility.get("name", "EQuIS"),
                task.from_cache,
            ),
            pages,
            fallback_wkid=facility.get("wkid") or self._service_wkid,
        )
        if layer is None or layer.featureCount() == 0:
            self._set_status("Could not build the locations layer.",
                             Qgis.MessageLevel.Warning)
            self._update_actions()
            return

        count = layer.featureCount()
        if not self._has_geometry:
            # Adding this layer would put an invisible, unclickable thing on
            # the map, so report the situation instead.
            message = (
                "%s location(s) exist for this facility but none have "
                "coordinates in EQuIS, so results cannot be placed on the map. "
                "The report still loads for inspection." % format(count, ",d")
            )
            self.locations_label.setText(message)
            self._set_status(message, Qgis.MessageLevel.Warning)
            self._update_actions()
            return

        # One facility at a time: replace the previous facility's points.
        self._remove_layer(self._locations_layer)
        QgsProject.instance().addMapLayer(layer)
        self._locations_layer = layer
        # The previous layer went with _remove_layer, so nothing to disconnect.
        layer.selectionChanged.connect(self._on_map_selection_changed)
        self._refresh_map_selection_label()
        self._zoom_to(layer)
        self._update_active_label()
        self.locations_label.setText(
            "%s location point(s) on the map." % format(count, ",d")
        )
        message = "Added %s locations." % format(count, ",d")
        if skipped:
            message += " %d had no coordinates." % skipped
        self._advance_to(self.facility_box, self.report_box)
        self._set_status(message, Qgis.MessageLevel.Success)
        self._update_actions()

    # ------------------------------------------------------ analysis source

    @property
    def _analysis(self):
        """Whatever should answer questions about the loaded rows.

        The store when it is ready, the table otherwise. Exactly one place
        decides this, because two places deciding one thing is how this
        codebase has been bitten before - see panel_state.py's docstring and
        the overlay's double mouse gate. Every caller asks `_analysis`; none
        of them asks whether a store exists.

        ReportStore mirrors ReportTable's signatures, which is what
        tools/test_store_parity.py exists to keep true.

        THE INVARIANT HAS TWO CASES NOW. On the LIVE path the store is only
        ever set while a table is set, and `_discard_store` runs wherever
        `_report_table` is cleared - so the many `if self._report_table is
        None` guards remain correct there without mentioning the store.

        A PARQUET source sets the store with NO table, because there is no
        report and nothing was ingested. Those same guards then read as "no
        data", which is wrong: there is data, it simply never passed through
        a ReportTable. Anything that asks "are there rows?" must ask
        `_analysis is None`, not `_report_table is None`. The places that
        still ask the old question are the ones that only make sense for a
        live report anyway - running one, exporting its own columns - and
        they are gated on the source instead.
        """
        return self._report_store or self._report_table

    def _apply_row_scope(self):
        """Narrow the rows in hand to the panel's dates and sample types.

        THE ONE PLACE THE ROW SCOPE IS SET, for every source. On the live
        path the server already applied the same filters, so this repeats
        them and changes nothing. On a parquet source - every row the
        facility has - it is the only thing that applies them: before
        2026-10-03 nothing did, and a map with Dates ticked drew results
        from twelve years outside the range. A saved report or an agent's
        stored output was never re-scoped either.

        Both holders get it, not just `_analysis`: on the live path the store
        is built in the background and takes over from the table when it
        lands, and the two must already agree when it does.

        Returns True when the rows changed, so the caller re-counts.
        """
        selection = self.selection_panel.selection()
        changed = False
        for holder in (self._report_table, self._report_store):
            if holder is not None and holder.set_row_scope(
                    selection.start, selection.end, selection.sample_types):
                changed = True
        return changed

    def _on_rows_rescoped(self, what):
        """The rows in hand were narrowed or widened: re-count what shows them.

        Shared by the matrix and the row scope because both re-cut rows
        already loaded, and they used to differ by accident: the matrix
        handler was gated on a ReportTable existing, so on a parquet source
        it never ran at all.
        """
        if self._analysis is None:
            return
        self._analytes = self._analysis.analytes(self._matrix_mode())
        # Counts only: the offer itself only ever grows (see
        # _populate_analytes), so a narrow date range cannot strand the
        # chemicals someone had ticked.
        self._populate_analytes()
        # Method and fraction are matrix-scoped too: surface water carries
        # D and T where sediment carries only N, and a metals suite and an
        # organics suite share a report but never a matrix. Leaving the old
        # offer up would list choices that match nothing in the matrix now
        # selected. Only these two - rebuilding the matrix combo from inside
        # its own change signal would fight the user's click.
        self._sync_analysis_filters(self._analysis)
        self._refresh_chem_hint()
        if self._picked_location:
            self._on_location_picked(self._picked_location)
        if self._result_layer is not None and self._mapped_chemical:
            self._set_status(
                "%s changed. Press 'Add layer to map' to rebuild the layer "
                "with it." % what,
                Qgis.MessageLevel.Info,
            )

    def _start_store_build(self, table):
        """Load the rows into DuckDB in the background.

        Not on this thread: ~7.4 s on a real report. Not blocking anything
        either - the panel is fully usable on the Python path while it runs,
        and if it never finishes nothing breaks.
        """
        self._discard_store()
        try:
            path = os.path.join(settings_store.cache_root(), "session.duckdb")
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError as exc:
            QgsMessageLog.logMessage(
                "No place to build the report store: %s" % exc,
                "EQuIS", Qgis.Info)
            return
        task = BuildStoreTask(table, path)
        task.finishedOk.connect(lambda _p, t=task: self._on_store_ready(t))
        task.failed.connect(self._on_store_failed)
        self._store_task = task
        QgsApplication.taskManager().addTask(task)

    def _on_store_ready(self, task):
        if task is not self._store_task:
            # A newer report superseded this build; its file is already gone.
            return
        self._store_task = None
        try:
            self._report_store = ReportStore.open(task.path)
        except Exception as exc:               # noqa: BLE001
            self._on_store_failed(str(exc))
            return
        # It takes over from the table, which is already scoped; it must
        # answer identically from its first question.
        self._apply_row_scope()
        QgsMessageLog.logMessage(
            "Report store ready (%s rows)."
            % format(self._report_store.count(), ",d"), "EQuIS", Qgis.Info)
        self._schedule_analysis_warmup()

    def _on_store_failed(self, message):
        self._store_task = None
        self._report_store = None
        # Logged, not shown: the user asked for a report and got one. Losing
        # the accelerator is not their problem to solve.
        QgsMessageLog.logMessage(
            "Report store unavailable, using the in-memory path: %s"
            % message, "EQuIS", Qgis.Info)

    def _discard_store(self):
        """Drop the store whenever the rows it describes are replaced."""
        if self._store_task is not None:
            try:
                self._store_task.cancel()
            except Exception:
                pass
            self._store_task = None
        if self._report_store is not None:
            self._report_store.close()
            self._report_store = None
        # The parquet marker goes with the store it describes. Left set,
        # _needs_fetch would keep answering "no fetch needed" after a switch
        # back to EQuIS, and the live path would never fetch again.
        self._parquet_url = None

    # ------------------------------------------------------ linked views

    # ------------------------------------------------------- analysis tab

    def _analysis_showing(self):
        """True when the analysis view is on screen: over the map, or in its tab.

        Over the map counts. It did not until 2026-10-29: the overlay's
        toolbar button hides this dock, so every map selection made over
        the map was owed to a tab nobody was looking at and never drawn -
        the scatter, ternary and linked histograms ignored QGIS's selection
        tools exactly where the map is beside them.
        """
        linked = getattr(self, "_linked", None)
        if linked is not None and linked.overlay_visible:
            return True
        return (getattr(self, "tabs", None) is not None
                and self.tabs.currentIndex() == self.analysis_index
                and self.isVisible())

    def _draw_owed(self):
        """The view came on screen: draw what was asked while it was hidden."""
        if getattr(self, "_analysis_owed", False) and self._analysis_showing():
            self._analysis_owed = False
            self._refresh_chart()

    def _schedule_analysis_warmup(self):
        """Start the analysis view in the background, once, after data loads.

        Not at QGIS start-up: Chromium's processes cost memory every user
        would pay, including the ones who only make maps. Data loaded is the
        earliest moment there is anything to analyse. A short delay lets
        the panel finish its own updates first.
        """
        if self._linked is not None and self._linked.views is not None:
            if self._linked.overlay is not None:
                # Over the map there is no tab to click to be offered one.
                self._linked.offer()
            return
        if getattr(self, "_warmup_pending", False):
            return
        self._warmup_pending = True
        QTimer.singleShot(1500, self._warm_analysis)

    def _warm_analysis(self):
        self._warmup_pending = False
        views = self.open_linked_views(show=False)
        self._chart_log("analysis view warming in the background%s"
                        % ("" if views is not None else " - FAILED, see above"))

    def _on_tab_changed(self, index):
        """Shown: draw what was owed while the tab was hidden."""
        if index != getattr(self, "analysis_index", -1):
            return
        if self._linked is None or self._linked.views is None:
            # Not warmed yet (data still loading, or no QtWebEngine): start
            # now rather than leave an empty tab.
            if self._report_store is not None and self.open_linked_views(
                    show=False) is not None:
                # The page is still starting; offer() remembers the ask and
                # makes it on ready. Without this the first visit to the tab
                # sat at "Nothing to draw yet" until it was shown again.
                self._linked.offer()
            return
        self._draw_owed()
        # The chooser: offered on first sight, and again whenever the data
        # or its row scope changed since. Free when nothing did.
        self._linked.offer()

    def open_linked_views(self, show=True, need_store=True):
        """Open the analysis view in the Analysis tab. Returns it, or None.

        `show=False` builds it without switching tabs - the background
        warm-up. The view is parented to the tab, so it is created once and
        stays: hiding the tab hides it, it is never recreated.

        Everything this needs can be missing on a valid install - a QGIS
        build without QtWebEngine, or a plugin zip built before the web
        bundle was made - so the failure is a message rather than an
        exception, and the rest of the panel is untouched either way.
        """
        from .linked_views import LinkedViewsController
        from .web_view import WebViewError

        if self._linked is None:
            self._linked = LinkedViewsController(
                # Callables, not values: the store is rebuilt on every fetch
                # and the layer on every map, and anything holding either
                # would answer from data no longer on the screen.
                connection_for=lambda: (self._report_store.con
                                        if self._report_store is not None
                                        else None),
                layer_for=lambda: self._result_layer,
                on_status=lambda text: self._set_status(text) if text else None,
                # The scatter builds a pivot from the store itself.
                store_for=lambda: self._report_store,
                on_scatter_map=self._build_scatter_map,
                # The Analysis chooser: a new scatter is dimmed to the map's
                # selection, and starts from the panel's matrix when exactly
                # one is chosen (several is a console-only scatter for now).
                locations_for=self._map_selection_codes,
                matrix_for=self._single_matrix,
            )
        if not self._linked.available():
            self._set_status(
                "The linked views need QtWebEngine and the built web bundle; "
                "one of them is missing. Everything else still works.",
                Qgis.MessageLevel.Warning)
            return None
        if self._report_store is None and need_store:
            self._set_status(
                "The histogram reads the report's DuckDB store, which is "
                "still building (or unavailable). Try again in a moment.",
                Qgis.MessageLevel.Warning)
            return None
        try:
            views = self._linked.open(parent=self.analysis_page)
        except WebViewError as exc:
            self._set_status(str(exc), Qgis.MessageLevel.Warning)
            return None
        self._follow_time_window()
        layout = self.analysis_page.layout()
        if layout.indexOf(views.view) < 0:
            layout.addWidget(views.view, 1)
        self.analysis_placeholder.setVisible(False)
        if show:
            self.tabs.setCurrentIndex(self.analysis_index)
        # The layer is usually already built by the time anyone asks for a
        # chart, so index it now rather than on the first drag.
        self._linked.prepare()
        return views

    def _set_overlay(self, on):
        """Move the analysis view over the map, or back into this tab."""
        linked = self._linked
        if on:
            if linked is None or linked.views is None:
                # Over the map the Data panel stands in for this tab, so the
                # view opens before anything is loaded (overlay shell, step 1).
                if self.open_linked_views(show=False, need_store=False) is None:
                    self.overlay_check.setChecked(False)
                    return
                linked = self._linked
            if not linked.float_over(
                    self.iface.mapCanvas(),
                    on_asked=self._on_overlay_asked,
                    on_remember=self._remember_overlay,
                    layout=self._saved_overlay_layout(),
                    on_ready=self._push_overlay_status,
                    # QGIS's message bar lies over the top of the canvas;
                    # the panels keep clear of it.
                    covers=[self.iface.messageBar()]):
                self.overlay_check.setChecked(False)
                return
            self.overlay_note.setVisible(True)
            if self._report_store is not None:
                linked.offer()
            self._draw_owed()
            return
        if linked is None:
            return
        view = linked.sink()
        if view is not None:
            layout = self.analysis_page.layout()
            if layout.indexOf(view) < 0:
                layout.addWidget(view, 1)
            view.show()
        self.overlay_note.setVisible(False)
        self._draw_owed()

    # ----------------------------------------------------- the overlay shell

    OVERLAY_LAYOUT_KEY = "equis_loader/overlay_layout"

    def toggle_overlay(self):
        """The toolbar button, when the overlay is the interface.

        First press: the analysis view goes over the map as panels and this
        dock stays out of the way - the Data panel there stands in for it,
        and asks for it (_on_overlay_asked) when the user wants to change
        what is loaded. Later presses hide and show the panels.
        """
        linked = self._linked
        if linked is not None and linked.overlay is not None:
            linked.set_overlay_visible(not linked.overlay_visible)
            self._draw_owed()
            return True
        self.overlay_check.setChecked(True)
        if self._linked is None or self._linked.overlay is None:
            return False
        self.hide()
        return True

    def _on_overlay_asked(self, what):
        """A Data panel button. The page asks; Qt moves the widgets."""
        if what == "dock":
            self.overlay_check.setChecked(False)
            self.show()
            self.raise_()
            self.tabs.setCurrentIndex(self.analysis_index)
        elif what == "data":
            self.show()
            self.raise_()
            self.tabs.setCurrentIndex(0)
        elif what == "hide" and self._linked is not None:
            self._linked.set_overlay_visible(False)

    def _saved_overlay_layout(self):
        import json
        raw = QSettings().value(self.OVERLAY_LAYOUT_KEY, "")
        try:
            return json.loads(raw) if raw else None
        except (TypeError, ValueError):
            return None

    def _remember_overlay(self, key, value):
        if key == "overlay_layout":
            QSettings().setValue(self.OVERLAY_LAYOUT_KEY, value)

    def _push_overlay_status(self):
        """Tell the Data panel what is loaded and what the banner warns of.

        It shows this panel's decisions and makes none: the banner
        (_update_active_label) stays the one authority for what is cached,
        offline or cut short, and the overlay repeats it - CLAUDE.md, cached
        data is marked everywhere it appears.
        """
        linked = getattr(self, "_linked", None)
        if linked is None or linked.overlay is None or linked.views is None:
            return
        from qgis.PyQt.QtGui import QTextDocument
        facility = self._current_facility()
        name = facility["name"] if facility else ""
        parquet = self._data_source() == settings_store.SOURCE_PARQUET
        if not name and parquet and self._analysis is not None:
            name = self._parquet_facility_name() or ""
        doc = QTextDocument()
        doc.setHtml(self.active_label.text())
        linked.views.call("dataStatus", {
            # The state machine's answer, not a second opinion derived here
            # from _analysis: one authority for "are there rows" (CLAUDE.md).
            "loaded": self._fsm.has_rows,
            "state": self._fsm.describe(),
            "facility": name,
            "source": ("published parquet" if parquet
                       else "EQuIS, offline cache" if self._offline else "EQuIS"),
            "warn": not self.active_label.isHidden(),
            "banner": doc.toPlainText(),
            "status": self.status_label.text(),
            "level": getattr(self, "_status_level", "info"),
        })

    def _follow_time_window(self):
        """Let the scatter follow the map's Temporal Controller. Once.

        Decided 2026-10-12: samples outside the map's time window are
        dimmed in the scatter, and "most recent"/"highest" are taken within
        it. The controller does the re-picking; this only reads the range.
        """
        if getattr(self, "_following_time", False):
            return
        try:
            canvas = self.iface.mapCanvas()
            canvas.temporalRangeChanged.connect(self._on_time_window_changed)
        except Exception:  # noqa: BLE001 - no canvas (a test, a headless run)
            return
        self._following_time = True
        self._on_time_window_changed()

    def _on_time_window_changed(self):
        if self._linked is None:
            return
        from .analysis import window_from_range
        window = None
        try:
            canvas = self.iface.mapCanvas()
            if canvas.mapSettings().isTemporal():
                span = canvas.temporalRange()
                begin = span.begin().toPyDateTime() if span.begin().isValid() else None
                end = span.end().toPyDateTime() if span.end().isValid() else None
                window = window_from_range(begin, end, span.includeEnd())
        except Exception:  # noqa: BLE001 - an unreadable range is no window
            window = None
        self._linked.set_window(window)

    def open_scatter(self, x, y, fraction=None, matrix=None):
        """Scatter two analytes, one point per sample (US-24). Console entry.

        `x` and `y` are chemical names or CAS numbers; `fraction` applies to
        both ("T", "D", ...), or None to take whatever each analyte has -
        refused if that is dissolved AND total. `matrix` is a code or a list.

        Console-only for now, like the histogram: a control for it is step 6
        of docs/phase3-next-steps.md. Returns the Pivot, or None with the
        reason on the status line.
        """
        from .pivot import PivotError, Variable
        if self._report_store is None:
            self._set_status(
                "The scatter reads the report's DuckDB store, which is not "
                "ready yet. Fetch or open data first.",
                Qgis.MessageLevel.Warning)
            return None
        known = self._report_store.analytes()

        def resolve(wanted):
            text = str(wanted).strip().lower()
            for entry in known:
                if text in ((entry["chemical"] or "").lower(),
                            (entry["cas_rn"] or "").lower()):
                    return Variable(entry["chemical"], entry["cas_rn"] or None,
                                    fraction)
            return None

        pair = [resolve(x), resolve(y)]
        missing = [name for name, v in zip((x, y), pair) if v is None]
        if missing:
            self._set_status("Not in this data: %s." % ", ".join(map(str, missing)),
                             Qgis.MessageLevel.Warning)
            return None
        if isinstance(matrix, str):
            matrix = [matrix]
        if self.open_linked_views() is None:
            return None
        try:
            pivot = self._linked.draw_scatter(
                pair[0], pair[1], matrix=matrix,
                locations=self._map_selection_codes())
        except (PivotError, ValueError) as exc:
            self._set_status(str(exc), Qgis.MessageLevel.Warning)
            return None
        self._chart_log("scatter %s x %s: %d samples, %d complete, "
                        "excluded %s" % (pair[0].chemical, pair[1].chemical,
                                         pivot.rows, pivot.complete,
                                         pivot.excluded))
        return pivot

    def _build_scatter_map(self, stations, breaks, x_meta, y_meta, phrase=None,
                           z_meta=None):
        """The scatter's own map layer: one point per station, bivariate.

        Called by LinkedViewsController.draw_scatter with the stations and
        breaks bivariate.py chose; this only places them on the facility's
        location points and files the layer. One scatter map at a time: a
        new scatter replaces the previous one rather than stacking layers
        whose colours mean different breaks.
        """
        old = getattr(self, "_scatter_layer", None)
        if old is not None and self._layer_alive(old):
            QgsProject.instance().removeMapLayer(old.id())
        self._scatter_layer = None
        if not self._location_pages:
            self._set_status(
                "The scatter has no map: this facility's location points are "
                "not loaded.", Qgis.MessageLevel.Warning)
            return None
        facility = self._current_facility() or {}
        # Says which sample each station is, and the window: a layer kept
        # in a project must not read as "most recent" when it is the
        # highest arsenic in 2014.
        wkid = facility.get("wkid") or self._service_wkid
        if z_meta is not None:
            # A ternary: coloured by composition (ternary.py), not classes.
            name = "%s - %s : %s : %s (ternary map, %s)" % (
                facility.get("name", "EQuIS"), x_meta.get("chemical"),
                y_meta.get("chemical"), z_meta.get("chemical"),
                phrase or "most recent sample")
            layer, placed, unplaced = build_ternary_layer(
                name, self._location_pages, stations,
                [x_meta, y_meta, z_meta], fallback_wkid=wkid,
                location_field=LOCATION_FIELD)
        else:
            name = "%s - %s x %s (scatter map, %s)" % (
                facility.get("name", "EQuIS"), x_meta.get("chemical"),
                y_meta.get("chemical"), phrase or "most recent sample")
            layer, placed, unplaced = build_bivariate_layer(
                name, self._location_pages, stations, x_meta, y_meta, breaks,
                fallback_wkid=wkid, location_field=LOCATION_FIELD)
        if layer is None:
            self._set_status("The scatter map could not be built.",
                             Qgis.MessageLevel.Warning)
            return None
        # _add_to_group connects its selection: selecting on this layer dims
        # the scatter, the same as selecting on Locations.
        self._add_to_group(layer)
        self._scatter_layer = layer
        if self._linked is not None:
            self._linked.set_scatter_layer(layer)
        self._chart_log("scatter map: %d stations placed, %d with no point%s"
                        % (placed, len(unplaced),
                           (" (%s)" % ", ".join(unplaced[:5])) if unplaced else ""))
        tied = [s for s in stations if (s.get("tied") or 1) > 1]
        if tied:
            # The map shows ONE sample per station. Where several share the
            # latest date (field replicates, at Facility A's SO stations 6-9 of
            # them) that one was picked by sample code, which is arbitrary.
            self._set_status(
                "Scatter map: at %d of %d stations the latest date holds "
                "several samples (up to %d); each shows the first by sample "
                "code. The scatter shows them all."
                % (len(tied), len(stations), max(s["tied"] for s in tied)),
                Qgis.MessageLevel.Info)
        return layer

    def _chart_scope(self):
        """What the histogram should bin, or None if nothing can be binned.

        The page does not ask for itself: the chart must show the same rows
        the MAP shows, and only the panel knows the filters the layer was
        built from. See web_view.draw_histogram.

        Built from the ACTIVE LAYER when there is one, so the chart follows
        the same rule the time series does - highlight a Manganese layer and
        the chart is Manganese, whatever the picker has moved on to.
        """
        from .store import ND_COLUMN
        scope, _from_layer = self._active_scope()
        if scope is None or not scope.get("chemical"):
            return None
        # The page writes its own WHERE over the store's view, so it is
        # handed the STORE's codes: upper-cased, every one of a multi-matrix
        # layer, and None for a column the source lacks. This sent the first
        # of each list as typed, so a sediment-and-tailings layer was binned
        # over sediment alone (found 2026-10-28; tools/test_histogram_sql.py).
        if self._report_store is None:
            return None                      # it reads the store's view
        codes = self._report_store.filter_codes
        return {
            "chemical": scope.get("chemical"),
            "casRn": scope.get("cas_rn") or None,
            "matrix": codes("matrix", scope.get("matrix") or None),
            "fraction": codes("fraction", scope.get("fraction") or None),
            "method": codes("method", scope.get("method") or None),
            # The store's own mapping, never a second copy: two tables of
            # these names is how the page and the store come to disagree
            # about which non-detect convention a chart is showing.
            "ndColumn": ND_COLUMN.get(scope.get("non_detect"), "value_half"),
            "aggregate": scope.get("aggregation") or "unaggregated",
            # US-6: the map's selection narrows the chart. None when
            # nothing is selected, so the key is simply absent from the
            # options the page receives.
            "locations": self._map_selection_codes(),
        }

    def _refresh_chart(self):
        """Redraw the histogram for the current selection, if one is open.

        Cheap and safe to call from anything that changes what is shown: it
        does nothing at all when no view is open, which is the normal case.
        """
        linked = self._linked
        if linked is None or getattr(linked, "views", None) is None:
            return False
        if not self._analysis_showing():
            # NEVER DRAW INTO A HIDDEN TAB. Chromium gives a hidden view no
            # animation frames and Mosaic's Arrow queries wait for one, so a
            # draw now would sit pending. It is owed instead, and drawn when
            # the tab is shown (_on_tab_changed) - which also skips the work
            # entirely for a user who never looks.
            self._analysis_owed = True
            return False
        # New data or a new row scope under a visible tab: the chooser is
        # re-offered, and a chosen pair redrawn from the new pivot. Free when
        # neither changed, which is nearly every call.
        if linked.offer() and getattr(linked, "showing", None) == "scatter":
            return True
        if getattr(linked, "showing", None) == "scatter":
            # A scatter answers only to the MAP's selection (US-24 criterion
            # 4): it dims to the selected locations without rebuilding its
            # pivot. A picker change does not turn it back into a histogram.
            return linked.refresh_scatter_locations(self._map_selection_codes())
        scope = self._chart_scope()
        # EVERY EXIT IS LOGGED, to the equis-web tab beside the page's own
        # "histogram #n asked/drew" lines. Reported 2026-09-29: re-picking
        # an analyte left the chart unchanged, and from outside "the panel
        # never asked", "the panel asked the same question" and "the page
        # drew the wrong rows" all look identical.
        if scope is None:
            self._chart_log("not redrawn: nothing to bin (no analyte, or "
                            "the active layer has no chemical)")
            return False
        # REDRAW ONLY WHEN THE QUESTION CHANGED. _on_selection_changed
        # re-enters - _sync_analysis_filters emits `changed`, which lands
        # back there - so an unguarded redraw fires several times per
        # selection, each one tearing down and rebuilding the chart. The
        # same comparison the analyte branch above already relies on.
        if scope == getattr(self, "_drawn_scope", None):
            _scope, from_layer = self._active_scope()
            picked = (self._current_analyte() or {}).get("chemical")
            if from_layer and picked and picked != scope.get("chemical"):
                # The case that reads as a bug: the ACTIVE LAYER decides
                # (see _chart_scope), so the list moving does not.
                self._chart_log(
                    "not redrawn: the chart follows the active layer (%s), "
                    "not the list (%s)" % (scope.get("chemical"), picked))
            return False
        self._chart_log("drawing %s (%s): %s" % (
            scope.get("chemical"),
            "active layer" if self._active_scope()[1] else "picker",
            ", ".join("%s=%s" % (k, v) for k, v in sorted(scope.items())
                      if k != "chemical" and v not in (None, "", []))))
        self._drawn_scope = scope
        # A BRUSH OVER A CHART THAT HAS CHANGED IS A LIE. The highlight
        # belongs to the population the chart was showing; redrawing for a
        # different analyte and leaving it would put a Manganese selection
        # on an Arsenic map. Cleared first, deliberately.
        try:
            linked.clear_brush()
        except Exception:                       # noqa: BLE001
            pass
        return linked.draw(scope)

    @staticmethod
    def _chart_log(message):
        QgsMessageLog.logMessage("panel: chart %s" % message, "equis-web",
                                 Qgis.MessageLevel.Info)

    def _close_linked_views(self):
        if self._linked is not None:
            try:
                self._linked.close()
            except Exception as exc:  # noqa: BLE001 - teardown must not
                # block the panel from closing.
                QgsMessageLog.logMessage(
                    "linked views did not close cleanly: %s" % exc,
                    "equis-web", Qgis.Warning)
            self._linked = None
        if getattr(self, "analysis_placeholder", None) is not None:
            self.analysis_placeholder.setVisible(True)
        if getattr(self, "overlay_check", None) is not None:
            # The view it moved is gone; the box must not claim otherwise.
            self.overlay_check.blockSignals(True)
            self.overlay_check.setChecked(False)
            self.overlay_check.blockSignals(False)
            self.overlay_note.setVisible(False)

    # ----------------------------------------------------------- 5. report

    def _populate_reports(self):
        needle = self.report_filter.text().strip().lower()
        facility = self._current_facility() or {}
        self.report_combo.blockSignals(True)
        self.report_combo.clear()
        shown = []
        for report in self._reports:
            haystack = "%s %s" % (report["name"], report["code"])
            if needle and needle not in haystack.lower():
                continue
            entry = self._report_cache_entry(facility.get("id"), report)
            # Offline, a report that was never run cannot be opened at all, so
            # listing it is only an invitation to a dead end.
            if self._offline and entry is None:
                continue
            label = report["name"]
            if report["type"]:
                label += "   [%s]" % report["type"]
            if entry is not None:
                # Marked so the user can see, before committing to a run that
                # may take minutes, that this one is already on disk.
                label += "   ● cached %s" % entry.fetched_text("%d %b")
            self.report_combo.addItem(label, report)
            shown.append(report)
        self.report_combo.blockSignals(False)

        # Default to the first ARII report, which is the usual analytical
        # results export at these sites.
        for index, report in enumerate(shown):
            if "arii" in (report["name"] or "").lower():
                self.report_combo.setCurrentIndex(index)
                break
        if self._offline and not shown:
            self.report_hint.setText(
                "<i>No report for this facility has been run before, so none "
                "is available offline.</i>"
            )
        self._update_report_actions()

    def _report_params(self):
        """Cache parameters for the run the current controls describe.

        The data selection is part of it on the default path: those filters
        decide which rows the report generates, so changing one describes a
        different dataset and must not read as already cached.
        """
        facility = self._current_facility() or {}
        selection = None
        if self._using_own_report():
            selection = self._current_selection(
                facility.get("id")).to_parameters()
        return {
            "max_rows": (self.report_limit_spin.value()
                         if self.report_limit_check.isChecked() else None),
            "where": None,
            "keep_columns": list(ANALYSIS_COLUMNS),
            "selection": selection,
        }

    def _report_cache_entry(self, facility_id, report):
        """The cached entry matching the current controls, if any."""
        if facility_id is None or not report:
            return None
        return self._cache.report_entry(
            self._site(), facility_id, report["id"], self._report_params()
        )

    def _using_own_report(self):
        """Whether the fetch goes through the plugin's own report.

        The default. Picking from the facility's saved reports is opt-in,
        and offline there is nothing to write filters to, so a cached run of
        a real report is the only thing that can work.
        """
        if self._offline or self._client is None:
            return False
        # The client's flag, not the checkbox: it is what actually refuses
        # the writes this path needs, so it is the one that can say no.
        # getattr because the panel tests hand in duck-typed clients; that
        # cannot let a write through, since _request is where it is refused.
        if getattr(self._client, "read_only", False):
            return False
        return not self.pick_report_check.isChecked()

    def _current_report(self):
        """The report a fetch will run.

        On the default path this is a stand-in describing the plugin's own
        report: it may not exist on the server yet, and its id is only known
        after the fetch creates it. Everything downstream - the cache key,
        the status text, the layer name - needs a stable identity now, so it
        is named for the facility rather than for a report id.
        """
        if self._using_own_report():
            facility = self._current_facility() or {}
            return {
                "id": OWN_REPORT_ID,
                "code": BASE_REPORT_CODE,
                "name": "%s (filtered)" % BASE_REPORT_CODE,
                "type": "class",
                "facility_id": facility.get("id"),
                "spatial": False,
                "has_eia": False,
            }
        index = self.report_combo.currentIndex()
        return self.report_combo.itemData(index) if index >= 0 else None

    def _on_read_only_toggled(self, read_only):
        """Hold the tenant read-only, or release it.

        The client is told directly so the change holds mid-session; the
        report step is then pinned to existing reports, since the plugin's
        own report cannot be created or re-filtered without writing.
        """
        settings_store.save_read_only(read_only)
        if self._client is not None:
            self._client.read_only = read_only
        if read_only:
            self.pick_report_check.setChecked(True)
        self.pick_report_check.setEnabled(not read_only)
        self.pick_report_check.setToolTip(
            "Read-only mode is on (step 1), so the plugin's own report,\n"
            "which it creates and re-filters on the site, is unavailable."
            if read_only else self._pick_report_tip)
        self._update_own_report_label()
        self._update_report_actions()

    def _on_pick_report_toggled(self, picking):
        """Show the report list only when the user asks for one."""
        self.report_filter.setVisible(picking)
        self.report_combo.setVisible(picking)
        if picking and not self._reports:
            self._populate_reports()
        self._update_own_report_label()
        self._update_report_actions()

    def _update_own_report_label(self):
        """Say which report the default path will use, before it is made."""
        if not hasattr(self, "own_report_label"):
            return
        if self.pick_report_check.isChecked():
            self.own_report_label.setText(
                "<i>Running a saved report as it is. The data selection below "
                "filters the rows after they arrive.</i>"
            )
            return
        if self._offline:
            self.own_report_label.setText(
                "<i>Working offline, so only reports already cached can be "
                "opened. The data selection filters those rows after they "
                "are loaded.</i>"
            )
            return
        if self._client is None:
            self.own_report_label.setText(
                "<i>Connect to EQuIS to query through your own report.</i>"
            )
            return
        facility = self._current_facility() or {}
        who = self._client.username if self._client else ""
        name = report_name(who, facility.get("name", ""), facility.get("id"))
        self.own_report_label.setText(
            "<i>Querying through your own report, built on %s and filtered by "
            "the data selection below.<br>%s</i>"
            % (BASE_REPORT_CODE, name)
        )

    def _update_report_actions(self, *_args):
        """Make the buttons say which of the two paths they will take."""
        if not hasattr(self, "refresh_report_btn"):
            return
        facility = self._current_facility() or {}
        report = self._current_report()
        entry = self._report_cache_entry(facility.get("id"), report)
        # Enablement is _update_actions' job. This function only decides what
        # the buttons SAY - it used to read `busy` off the progress bar's
        # visibility, a widget standing in for program state, and disagreed
        # with _set_busy about which controls that implied.
        own = self._using_own_report()
        if entry is not None:
            self.run_report_btn.setText(
                "Load cached data" if own else "Load cached report")
            self.run_report_btn.setToolTip(
                "Load the copy stored on this machine (%s rows, %s, saved %s)."
                % (format(entry.row_count, ",d"), describe_size(entry.bytes),
                   entry.fetched_text())
            )
            self.report_hint.setText(
                "<i>Cached %s (%s rows). Press ↻ to re-run against EQuIS.</i>"
                % (entry.age_text, format(entry.row_count, ",d"))
                + ("<br><b>This copy was truncated by a row limit.</b>"
                   if entry.truncated else "")
            )
        else:
            self.run_report_btn.setText(
                "Pre-fetch data" if own else "Run report")
            self.run_report_btn.setToolTip(
                "Save the chemicals and matrix onto your report and "
                "fetch the matching rows from EQuIS. This retrieves data; "
                "nothing is drawn until you map it in step 5." if own else
                "Run this report against EQuIS and cache the result."
            )
            if not self._offline:
                self.report_hint.setText(
                    "<i>Reports run on demand and may take a few minutes.</i>"
                )
        self.refresh_report_btn.setToolTip(
            "Working offline - EQuIS is not available." if self._offline
            else "Re-run this report against EQuIS, replacing the cached copy."
        )

    def _on_run_report(self):
        self._run_report(prefer_cache=True)

    def _on_refresh_report(self):
        """Force a live run, replacing whatever is cached."""
        if self._offline:
            self._set_status("Working offline - reconnect to re-run reports.",
                             Qgis.MessageLevel.Warning)
            return
        self._run_report(prefer_cache=False)

    def _run_report(self, prefer_cache=True):
        facility = self._current_facility()
        report = self._current_report()
        if not facility or report is None:
            # Only reachable when picking a saved report and none is chosen;
            # the default path always describes one.
            self._set_status("Choose a report to run.", Qgis.MessageLevel.Warning)
            return
        max_rows = (
            self.report_limit_spin.value()
            if self.report_limit_check.isChecked()
            else None
        )

        cached = self._report_cache_entry(facility.get("id"), report)
        self._set_busy(True)
        if prefer_cache and cached is not None:
            self._set_status(
                "Loading '%s' from the cache (%s rows, saved %s)..."
                % (report["name"], format(cached.row_count, ",d"),
                   cached.fetched_text())
            )
        else:
            self._set_status(
                "Running '%s'. This can take a few minutes..." % report["name"]
            )
        # The live path saves the selection onto this user's own report before
        # running it; EQuIS ignores a filter that is not stored, so this is
        # what makes a large facility fetchable at all. Offline and cached
        # reads carry no selection - there is nothing to write to.
        live_report = None
        selection = None
        if self._using_own_report():
            live_report = self._live_report_for(facility)
            selection = self._current_selection(facility["id"])

        task = RunReportTask(
            self._client,
            report,
            facility["id"],
            facility["name"],
            facility.get("wkid") or self._service_wkid,
            max_rows=max_rows,
            live_report=live_report,
            selection=selection,
            # Only the columns the chemical workflow reads. Reports served
            # from EQuIS's cache ignore out_fields and return everything, so
            # keep_columns drops the rest as the response is parsed.
            out_fields=",".join(ANALYSIS_COLUMNS),
            keep_columns=ANALYSIS_COLUMNS,
            cache=self._cache,
            site=self._site(),
            prefer_cache=prefer_cache,
        )
        task.finishedOk.connect(lambda _r, t=task: self._on_report_ready(t))
        task.failed.connect(self._on_task_failed)
        # replace=True: pressing fetch again is the user asking for a
        # different fetch, so the one in flight is cancelled rather than
        # left running to overwrite the new one when it lands.
        # _set_busy(True) was already called above; a refusal here would
        # leave the panel looking busy with nothing running, so clear it.
        if not self._start(task, replace=True):
            self._set_busy(False)
            return
        self._enter(st.FETCHING, "report run started")

    def _on_report_ready(self, task):
        # A cancelled fetch still runs to completion and still emits, so a
        # superseded one must not land its rows on top of the fetch that
        # replaced it.
        if self._stale(task):
            return
        self._task = None
        self._set_busy(False)
        if not task._result:
            # Nothing landed, so the panel must not sit in FETCHING with no
            # controls - that is what "the progress bar never stopped" was.
            self._enter(st.PREPARED, "fetch returned nothing")
            self._set_status("The report returned no data.", Qgis.MessageLevel.Warning)
            return
        headers, rows = task._result
        table = ReportTable(headers, rows)

        missing = table.missing_for_analysis
        if missing:
            self._report_table = None
            self._discard_store()
            self._analytes = []
            self._matrices = []
            self._enter(st.PREPARED, "report lacks the needed columns")
            # The offer stands: this report cannot be mapped, but the facility
            # still holds the same chemicals to try another way.
            self._populate_analytes()
            self._set_status(missing_message(missing),
                             Qgis.MessageLevel.Warning)
            self._update_actions()
            return

        self._report_table = table
        # Same rows into DuckDB, in the background. Everything below works
        # without it; `_analysis` starts answering from it once it lands.
        # It reads `table.rows`, which the scope never narrows.
        self._start_store_build(table)
        # Before anything counts: a saved report or an agent's output was not
        # filtered by these controls, and the counts below must agree with
        # what a build will draw.
        self._apply_row_scope()
        # Rows are in hand: this is the only place LOADED is entered, and the
        # only thing that makes mapping possible.
        self._enter(st.LOADED, "rows loaded")
        self._active_report = task.report
        self._report_source = task.source
        self._report_entry = task.entry
        # Matrices first: the combo's selection scopes the chemical list, so
        # populating analytes before it would count rows the user filtered out.
        self._matrices = table.matrices()
        self._analytes = table.analytes(self._matrix_mode())
        self._populate_analytes()
        self._update_active_label()
        self._update_report_actions()
        self._advance_to(self.report_box, self.chem_box)
        # What these rows were fetched for, so the next map press can tell
        # whether they still answer the question being asked.
        self._fetched_selection = getattr(task, "selection_key", None)
        self._fetched_selection_obj = getattr(task, "selection", None)

        # Fraction cannot be chosen safely before fetching, so the mixing is
        # reported here where the data can actually answer it. Aggregating a
        # chemical that carries both dissolved and total compares values that
        # differ by up to ~29x for the same sample.
        mixed = table.mixed_fractions()
        if mixed:
            detail = "; ".join(
                "%s: %s" % (matrix or "no matrix", "/".join(fractions))
                for matrix, fractions in sorted(mixed.items())
            )
            self._set_status(
                "This data mixes fractions (%s). Results for one sample can "
                "differ by more than 20x between them, so set Fraction in "
                "Fraction in step 5 and map again." % detail,
                Qgis.MessageLevel.Warning,
            )
        # The panel's pickers describe what was actually fetched, so they are
        # refreshed from the loaded report rather than left showing the values
        # that produced it.
        self._sync_selection_panel(table)

        if task.source == CACHE_FALLBACK:
            self._client = None
            self._offline = True
            # A live read failed and the cache answered instead. Offline is
            # orthogonal to the pipeline - it changes the SOURCE, not the
            # progress - but a fallback can be the FIRST thing that succeeds,
            # so the panel may still be DISCONNECTED here. Entering CONNECTED
            # is what makes cached data a source like any other.
            self._fsm.offline = True
            self._fsm.enter(st.CONNECTED, "fell back to cache")
            self._announce_offline(
                "Could not reach EQuIS (%s). Loaded the copy cached %s instead."
                % (task.error, task.entry.age_text if task.entry else "earlier")
            )

        summary = "%s rows, %d chemical(s) at %d location(s)." % (
            format(len(rows), ",d"), len(self._analytes), len(table.locations())
        )
        if task.from_cache and task.entry is not None:
            summary = "Loaded from cache (saved %s). " % task.entry.fetched_text() + summary
        elif task.source == EIA and getattr(task, "eia", None):
            # Stored output is as old as the agent's last run, and saying so
            # matters: the report may have been re-run since, and this is not
            # a live read however quickly it arrived.
            summary = ("Read stored output from '%s'%s. "
                       % (task.eia.get("name") or "information agent",
                          _describe_eia_age(task.eia.get("executed")))) + summary
        if getattr(task, "truncated", False):
            # Truncation cuts the report mid-way, so later sample dates are
            # missing entirely - the time series would look like it stops.
            # This has to keep being said for a cached copy too, or the cache
            # would launder a knowingly partial report into an authoritative one.
            self._set_status(
                summary + " Row limit reached, so this is only part of the "
                "report and time series may end early. Clear the row limit "
                "for the full history.",
                Qgis.MessageLevel.Warning,
            )
        elif task.from_cache:
            self._set_status(summary, Qgis.MessageLevel.Warning)
        else:
            self._set_status(summary, Qgis.MessageLevel.Success)
        self._update_cache_label()

        # Deliberately does NOT draw. Fetching retrieves rows; mapping draws
        # them, and they are steps 4 and 5 precisely because they are
        # different actions with different costs. The panel used to blur
        # this: pressing map would silently fetch, and fetching would then
        # map, so neither button did only what it said.
        self._update_actions()

    # ---------------------------------------------------------- 6. analytes

    def _live_report_for(self, facility):
        """This user's report for this facility, built once per facility.

        Rebuilt when the facility changes rather than cached forever: the
        report's own @facility_id is what scopes it, so one carried across a
        facility switch would quietly fetch the wrong site.
        """
        if (self._live_report is not None
                and self._live_report.facility_id == facility["id"]):
            return self._live_report
        self._live_report = LiveReport(
            self._client, facility["id"], facility.get("name", ""),
            self._client.username if self._client else "",
        )
        return self._live_report

    def _on_selection_changed(self):
        """The data selection changed: say what it now means for a fetch.

        It feeds the cache key on the default path, so the run button has to
        be re-examined - a new selection is a different dataset and is not
        the one sitting in the cache.
        """
        # Dates and QC re-cut the rows in hand on every source. On the live
        # path they are also prefetch filters, so the fetch row still says a
        # new fetch is due - this only stops the rows in hand disagreeing
        # with the controls meanwhile.
        rescoped = self._apply_row_scope()
        # A matrix change re-scopes what is already fetched, without needing
        # another fetch: the rows are in hand and can simply be re-read.
        # `_analysis`, not `_report_table`: on a parquet source there is no
        # table, and this never ran there at all.
        matrix = self._matrix_mode()
        if matrix != self._last_matrix:
            self._last_matrix = matrix
            if self._analysis is not None:
                self._on_matrix_changed()
                rescoped = False            # already re-counted
        if rescoped:
            self._on_rows_rescoped("Date range or sample types")
        # Which methods and fractions exist is analyte-specific - a metals
        # suite and an organics suite share a report but never a result - so
        # the offers follow the chosen chemical, not just the matrix.
        #
        # The comparison is what makes this safe as well as cheap: rebuilding
        # the offers emits changed, which lands back here, and only an
        # unchanged analyte on the second pass stops it going round again.
        # It also keeps a keystroke in the chemical filter box from rebuilding
        # anything.
        analyte = (self._current_analyte() or {}).get("chemical")
        if analyte != self._last_analyte:
            self._last_analyte = analyte
            # `_analysis`: a parquet source answers methods() and
            # fractions_for() exactly as a ReportTable does, and asking the
            # narrower question left the Fraction row missing entirely.
            if self._analysis is not None:
                self._sync_analysis_filters(self._analysis)
        self._update_own_report_label()
        self._refresh_chem_hint()
        # The chart follows the selection. No-op unless a linked view is
        # open, which is the normal case.
        self._refresh_chart()
        self._update_actions()
        self._update_report_actions()
        self._schedule_preview()

    def _sync_selection_panel(self, table=None):
        """Tell the panel what the fetched data turned out to hold.

        Deliberately NOT the chemical list: _populate_analytes owns that, and
        it merges the fetch into the facility's full offer. Refilling the list
        here from table.analytes() - as this used to - discarded that merge
        one line later and left the user with only what they had just mapped.

        Matrices are different: a matrix the fetched rows do not contain
        cannot be mapped from them, so narrowing that combo to what is present
        is honest rather than limiting.
        """
        if table is None:
            return
        self.selection_panel.set_matrices(
            [(m["matrix"], "") for m in table.matrices() if m["matrix"]]
        )
        self._sync_analysis_filters(table)
        self.selection_panel.set_source(self._source_mode)

    def _sync_analysis_filters(self, table=None):
        """Offer the method and fraction the fetched rows actually carry.

        Separate from _sync_selection_panel because this runs again whenever
        the MATRIX changes, and the matrix combo must not be rebuilt from
        underneath the user at that moment - set_matrices() emits changed,
        which is what brought us here.

        Only the rows can answer these two: both are properties of the
        analysis, so the parameter service's facility-wide answer would be
        mostly choices that match nothing in the data at hand. Both filter
        locally, so they re-scope rows already held rather than costing
        another fetch.
        """
        table = table if table is not None else self._analysis
        if table is None:
            return
        analyte = self._current_analyte() or {}
        found = [m["method"] for m in table.methods(analyte.get("chemical"),
                                                    analyte.get("cas_rn"),
                                                    self._matrix_mode())
                 if m["method"]]
        # replace=True: the offer IS what this data holds. Unlike the matrix
        # combo, there is no wider facility list to preserve underneath.
        self.selection_panel.set_methods(found, replace=True, present=found)
        self.selection_panel.set_fractions(
            [(code, code) for code in table.fractions_for(
                analyte.get("chemical"), analyte.get("cas_rn"),
                self._matrix_mode())]
        )

    def _prepare_live_report(self):
        """Create this user's report for the facility and fill the pickers.

        Deliberately at facility time, not fetch time: a facility large enough
        to need filtering is one whose unfiltered report cannot be generated
        at all, so "fetch once to learn what to filter by" is not available.
        """
        facility = self._current_facility()
        if not facility or self._client is None or self._offline:
            return
        if not self._using_own_report():
            return
        live = self._live_report_for(facility)
        if live.report_id and self._offered_analytes:
            # Already prepared, and the offer survived; nothing to do.
            return
        if live.report_id:
            # The report exists but this panel has no list - a facility
            # revisited, or a mode switched. Read the values again rather than
            # leaving the offer to be filled by whatever a fetch returns.
            self._reload_selection_choices(live, facility)
            return
        self.selection_panel.set_unavailable(
            "Loading the chemical list from EQuIS...", busy=True)
        task = PrepareLiveReportTask(live, facility["id"])
        task.finishedOk.connect(lambda _r, t=task: self._on_live_report_ready(t))
        task.failed.connect(self._on_live_report_failed)
        self._start(task)

    def _reload_selection_choices(self, live, facility):
        """Re-read the pickers for a report that already exists.

        Same work as preparing one, without the creation: cheap enough
        (about a second) that re-reading beats carrying stale values, and it
        keeps the offer from being defined by whatever was last fetched.
        """
        task = PrepareLiveReportTask(live, facility["id"])
        task.finishedOk.connect(lambda _r, t=task: self._on_live_report_ready(t))
        task.failed.connect(self._on_live_report_failed)
        self._start(task)

    def _on_live_report_ready(self, task):
        if self._stale(task):
            return
        self._task = None
        self._set_busy(False)
        # The report exists and its pickers are about to be filled, which is
        # what PREPARED means. Re-entrant: re-reading the choices for a
        # report that already exists lands here again.
        self._enter(st.PREPARED, "report prepared")
        choices = task.choices or {}
        # The facility's full set, which stays on offer however narrow a
        # fetch is made from it. Merged with anything already known so a
        # re-read cannot drop a chemical the fetched rows carried.
        known = {cas for cas, _ in self._offered_analytes}
        for cas, name in choices.get("@cas_rns") or []:
            if cas not in known:
                known.add(cas)
                self._offered_analytes.append((cas, name))
        self._offered_analytes.sort(key=lambda pair: (pair[1] or "").lower())
        self.selection_panel.set_analytes(self._offered_analytes)
        # replace=True: this is a freshly prepared report for one facility,
        # and the previous facility's matrices must not linger.
        self.selection_panel.set_matrices(
            choices.get("@matrix_codes") or [], replace=True)
        # Facility-scoped and knowable before any fetch, unlike method and
        # fraction below - so the Where row is shown or hidden right here.
        self.selection_panel.set_location_groups(
            choices.get("@location_groups") or [])
        # Method and fraction are deliberately NOT offered yet. The parameter
        # service can only answer per FACILITY - it returns all 54 methods and
        # all 7 fractions whatever analyte and matrix are saved - so offering
        # them here would show a list that is mostly irrelevant to the data
        # about to be fetched, and invite a filter that returns nothing.
        # Which methods and fractions actually ran is analyte- and
        # matrix-specific, and only the rows can say. Both are filtered
        # locally, so waiting costs nothing: once the rows are in hand,
        # changing either re-scopes what is already loaded without refetching.
        self.selection_panel.clear_result_filters()
        self.selection_panel.set_source(self._source_mode)
        self._update_own_report_label()
        self._update_report_actions()
        for note in task.live_report.notes:
            self._set_status(note, Qgis.MessageLevel.Warning)
        if task.live_report.created:
            self._set_status(
                "Created your report for this facility. Choose what to fetch "
                "below - filtering by chemical is what keeps a large facility "
                "inside the time limit.",
                Qgis.MessageLevel.Info,
            )

    def _on_live_report_failed(self, message):
        """The report could not be prepared.

        Not fatal: the facility's own saved reports are still there, so the
        panel says what happened and points at that route rather than leaving
        a dead step.
        """
        self._task = None
        self._set_busy(False)
        self.selection_panel.set_unavailable(
            "Could not prepare your report, so there is nothing to filter by."
        )
        self._set_status(
            "%s Tick 'Use an existing report instead' to run one of the "
            "facility's saved reports." % message,
            Qgis.MessageLevel.Warning,
        )
        self._update_report_actions()

    def _matrix_mode(self):
        """The matrix the map and plots are restricted to, or ALL_MATRICES.

        Read from the data selection, which is the only place a matrix is
        chosen now. On the live path it was applied server-side as well, so
        the fetched rows usually carry just the one - but a cached or agent
        fetch can still hold several, and the aggregate must not cross them.
        """
        chosen = self.selection_panel.selection().matrices
        # A tuple, not the first element: several matrices can be mapped
        # together now (sediment and tailings are both solids), and taking
        # [0] would silently drop every one the user ticked after the first.
        return tuple(chosen) if chosen else ALL_MATRICES

    def _single_matrix(self):
        """The one matrix chosen in the data selection, or None."""
        chosen = self._matrix_mode()
        return chosen[0] if chosen and len(chosen) == 1 else None

    def _method_mode(self):
        """The analytic methods to keep, or ALL_METHODS.

        Several is normal and correct: one sample analysed twice yields two
        genuine results, and the depth profile draws a line per method so
        they can be compared rather than mistaken for duplicate data.
        """
        chosen = self.selection_panel.selected_methods()
        return tuple(chosen) if chosen else ALL_METHODS

    def _fraction_mode(self):
        """The fraction the map and plots are restricted to, or ANY_FRACTION.

        Mirrors _matrix_mode, and matters for the same reason: dissolved and
        total are sampled from the same water on the same day and can differ
        by more than an order of magnitude, so an aggregate taken across both
        reports whichever happened to be higher.

        On the live path the fraction is applied server-side too, but a
        cached copy, an agent's stored output or a saved report can still
        hold both, and until this was threaded through the picker was simply
        ignored on those paths.
        """
        chosen = self.selection_panel.selected_fractions()
        return tuple(chosen) if chosen else ANY_FRACTION

    def _on_matrix_changed(self):
        """Re-scope the chemical list, and say what is now stale.

        The analyte list is rebuilt rather than merely re-filtered: which
        chemicals exist and how many results each has are both matrix-specific.
        """
        self._on_rows_rescoped("Matrix")

    def _refresh_chem_hint(self):
        """Say what step 6 will draw, which depends on how much is selected."""
        if not hasattr(self, "chem_hint"):
            return
        count = len(self._selected_analytes())
        if count > 1:
            self.chem_hint.setText(
                "<i>How the %d layers from step 5 are drawn.</i>" % count)
        elif count:
            self.chem_hint.setText(
                "<i>How the layer from step 5 is drawn.</i>")
        else:
            self.chem_hint.setText(
                "<i>Select chemicals in step 4, then map them in step 5.</i>")

    def _refresh_step_hints(self, can, busy):
        """Say what steps 4 and 5 mean right now, by the machine's state.

        Step 4 never shows a count before a fetch: none is knowable, and an
        estimate would be two data points dressed up. Step 5 shows the count
        that would map, and the stale line when the rows no longer answer
        the request above. Text follows fsm.state; nothing here decides
        anything, so a wrong sentence cannot become a wrong action.
        """
        if not hasattr(self, "rows_footer"):
            return
        state = self._fsm.state
        if self._parquet_url is not None:
            # No fetch exists on this path, so no hint may mention one.
            prefetch = ("Every result is already loaded; these filters "
                        "re-scope what is here and take effect at once.")
        elif not self._using_own_report():
            # A saved report or an agent's stored output runs as it is, so
            # the box's title is only half true for it and the hint says so.
            prefetch = ("This report runs as saved, so these filters apply "
                        "after its rows arrive.")
        elif state in (st.DISCONNECTED, st.CONNECTED):
            prefetch = "Pick a facility first."
        elif state == st.FACILITY:
            prefetch = "Preparing your report and its filter lists…"
        elif state == st.PREPARED:
            prefetch = ("Decides which rows EQuIS returns. Count known after "
                        "fetch.")
        elif state == st.FETCHING:
            prefetch = "Fetching…"
        else:
            rows = self._row_count()
            prefetch = "%s rows fetched for the selection above." % format(rows, ",d")
            if can["stale"]:
                prefetch = ("%s rows fetched. The selection has changed since; "
                            "fetch again to apply it." % format(rows, ",d"))
        self.prefetch_hint.setText(prefetch)

        self.stale_label.setVisible(bool(can["stale"]) and not busy)

        if state != st.LOADED or self._analysis is None:
            self.rows_footer.setText("")
            self.rows_footer.setVisible(False)
            return
        if not self._selected_analytes():
            self.rows_footer.setText(
                "%s rows in hand. Select a chemical to count what would map."
                % format(self._row_count(), ",d"))
            self.rows_footer.setVisible(True)
            return
        # Counted OFF the UI thread. The footer is the only consumer, and
        # nothing gates on it, so a number that arrives a moment late is
        # simply late - which is what makes this safe to make asynchronous
        # where the pickers and the map button are not.
        # Keep the PREVIOUS number visible while the new one is counted,
        # rather than blanking. A footer that empties on every click reads
        # as breakage; one that lags by a moment reads as a footer.
        if not self.rows_footer.text():
            self.rows_footer.setText("Counting…")
        self._request_visible_count()
        self.rows_footer.setVisible(True)

    # ------------------------------------------------ the footer's count
    # Measured 2026-09-24 on the 3,452,146-row Facility A: clicking an
    # analyte cost 1.64 s on the UI thread, and 1.53 s of it was this
    # count - which rows_for produced by materialising one Python dict per
    # matching row, 115,017 of them, to arrive at two integers.
    #
    # Counting in SQL instead took it to 0.28 s. The rest is the non-detect
    # ladder, which is real work implementing a domain rule and is not
    # shortcut - so it moves off the thread rather than being made cheaper.

    # Long enough to swallow a burst of clicks, short enough that the number
    # feels like a response. A user scrolling the chemical list fires this
    # on every row; only the row they stop on should cost a query.
    COUNT_DEBOUNCE_MS = 180

    def _request_visible_count(self):
        """Ask for the footer's numbers, coalescing rapid clicks."""
        timer = getattr(self, "_count_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._start_visible_count)
            self._count_timer = timer
        timer.start(self.COUNT_DEBOUNCE_MS)

    def _start_visible_count(self):
        """Run the count in a worker, and show what comes back.

        A QgsTask would be the usual machinery, but this fires on every
        selection change and a task carries a progress entry in QGIS's
        status bar - a stream of them for a number nobody is waiting on.
        A plain thread with a generation counter is the smaller thing that
        fits.
        """
        analysis = self._analysis
        if analysis is None or not self._selected_analytes():
            return
        if not hasattr(analysis, "thread_view"):
            # A ReportTable, which is in memory and fast: no thread needed.
            self._show_visible_count(self._count_visible(), self._count_generation)
            return

        self._count_generation = getattr(self, "_count_generation", 0) + 1
        generation = self._count_generation
        # Read on THIS thread: the widgets must not be touched from the
        # worker, and these are the only inputs it needs.
        wanted = list(self._selected_analytes())
        scope = (self._matrix_mode(), self._fraction_mode(),
                 self._method_mode())

        def work():
            try:
                view = analysis.thread_view()
                locations = results = 0
                for analyte in wanted:
                    found, rows = view.count_for(
                        analyte["chemical"], analyte["cas_rn"],
                        matrix=scope[0], fraction=scope[1], method=scope[2])
                    locations += found
                    results += rows
                answer = (locations, results)
            except Exception:                   # noqa: BLE001
                # A footer is never worth an error: leaving the previous
                # number is better than replacing it with a traceback.
                return
            # Back to the UI thread. A direct setText from here would be a
            # cross-thread widget touch, which Qt does not forgive.
            self._count_ready.emit(answer[0], answer[1], generation)

        thread = threading.Thread(target=work, daemon=True)
        self._count_thread = thread
        thread.start()

    def _show_visible_count(self, answer, generation):
        """Show a count, unless a newer request has overtaken it."""
        if generation != getattr(self, "_count_generation", 0):
            return                              # superseded; drop it
        locations, results = answer
        self.rows_footer.setText(
            "%s location%s · %s result%s would map"
            % (format(locations, ",d"), "" if locations == 1 else "s",
               format(results, ",d"), "" if results == 1 else "s"))

    def _count_visible(self):
        """(locations, results) the current filters leave for the selected
        chemicals, or (0, None) when none is selected.

        Uses rows_for with the same matrix/fraction/method scoping the build
        uses, and no row cap, so the footer counts exactly what Add layer
        would draw. Rows with no usable number are excluded there too, so a
        column of blanks does not inflate the count.
        """
        analytes = self._selected_analytes()
        if not analytes:
            return 0, None
        # COUNTED, not built. This used to call rows_for and measure the
        # list, which materialises one dict per matching row - 115,017 for
        # Arsenic on Facility A - to produce two integers. 1.53 s on the UI
        # thread, on every analyte click; 0.28 s counted in SQL.
        #
        # count_for is still needed per analyte rather than as one query
        # because `seen` is the DISTINCT locations across all of them, and
        # a location carrying two selected chemicals must count once. The
        # store has no multi-analyte count, so several selected chemicals
        # sum their locations - which over-counts a shared location. That
        # was true of the old code too, and is left alone here rather than
        # changed silently under a performance fix.
        analysis = self._analysis
        if hasattr(analysis, "count_for"):
            locations = 0
            total = 0
            for analyte in analytes:
                found, rows = analysis.count_for(
                    analyte["chemical"], analyte["cas_rn"],
                    matrix=self._matrix_mode(),
                    fraction=self._fraction_mode(),
                    method=self._method_mode(),
                )
                locations += found
                total += rows
            return locations, total

        # A ReportTable has no count_for, so it still builds the rows.
        seen = set()
        total = 0
        for analyte in analytes:
            rows, _ = analysis.rows_for(
                analyte["chemical"], analyte["cas_rn"],
                matrix=self._matrix_mode(),
                fraction=self._fraction_mode(),
                method=self._method_mode(),
            )
            total += len(rows)
            seen.update(row["location"] for row in rows)
        return len(seen), total

    def _populate_analytes(self):
        """Offer the chemicals, and say how many results each one returned.

        What is OFFERED is what the facility holds, from the parameter
        service. What was FETCHED only annotates it with counts: the fetch is
        filtered by this very list, so refilling it from the fetched rows
        would shrink it to whatever was last asked for and leave no way to
        broaden the next request.

        A report the plugin did not filter - a saved one, or an agent's stored
        output - is the exception: its rows are all there are, so they define
        the list rather than annotating it.
        """
        counts = {a["cas_rn"]: a["count"] for a in self._analytes}
        # Anything fetched joins the offer rather than replacing it. The offer
        # only ever grows within a facility, so a narrow fetch can never take
        # away a chemical the user could previously have asked for - which is
        # what stranded them on whatever they mapped first.
        known = {cas for cas, _ in self._offered_analytes}
        for analyte in self._analytes:
            if analyte["cas_rn"] not in known:
                known.add(analyte["cas_rn"])
                self._offered_analytes.append(
                    (analyte["cas_rn"], analyte["chemical"]))
        # Named for the reader, not for whatever order the two sources
        # happened to arrive in.
        self._offered_analytes.sort(key=lambda pair: (pair[1] or "").lower())
        self.selection_panel.set_analytes(self._offered_analytes, counts=counts)
        self._update_actions()

    def _analyte_record(self, cas_rn):
        """The full analyte dict for a CAS, as the rest of the panel wants it."""
        for analyte in self._analytes:
            if analyte["cas_rn"] == cas_rn:
                return analyte
        return None

    def _current_analyte(self):
        """The chemical the map tool and the plot act on.

        The first selected, rather than a separate "current" notion: with one
        list there is no longer a distinction between what is highlighted and
        what is chosen.
        """
        chosen = self._selected_analytes()
        return chosen[0] if chosen else None

    def _on_analyte_changed(self):
        self._update_actions()
        self._update_active_label()

    # ------------------------------------------------------ 7. joined layer

    def _storage_mode(self):
        return self.storage_combo.currentData() or GEOPACKAGE

    def _on_storage_changed(self):
        settings_store.save_storage(self._storage_mode())
        self._update_storage_hint()

    def _project_gpkg(self):
        """The GeoPackage this project's layers belong in, or None."""
        return settings_store.geopackage_for_project(
            QgsProject.instance().fileName()
        )

    def _update_storage_hint(self):
        """Say where the next layer will go, before it is built.

        An unsaved project has nothing to sit beside, and silently producing a
        memory layer in that case would reintroduce exactly the surprise this
        setting exists to remove.
        """
        if self._storage_mode() == MEMORY:
            self.storage_hint.setText(
                "Layers are discarded when QGIS closes; a saved project will "
                "reopen with them empty."
            )
            return
        path = self._project_gpkg()
        if path:
            self.storage_hint.setText("Saving to %s" % os.path.basename(path))
        else:
            self.storage_hint.setText(
                "This project has not been saved yet, so layers stay in memory "
                "until it is. Save the project, then add the layer again to "
                "write it alongside."
            )

    def _needs_fetch(self):
        """Whether the rows for the current selection still have to be read.

        A fetch is needed when nothing is loaded, and also when the loaded
        data came from a different selection: the filters are saved onto the
        report, so changing them changes which rows exist rather than merely
        which are shown.
        """
        # A parquet source is never stale and never fetches: the rows are
        # every row there is, and the pickers re-scope them locally. Asking
        # the live question marked 3.45 million loaded rows as needing a
        # fetch that has no button and no meaning, which put the map button
        # down with no way to bring it up.
        if self._parquet_url is not None and self._report_store is not None:
            return False
        if self._report_table is None:
            return True
        if not self._using_own_report():
            # A saved report is run as it is, so what is loaded is what there
            # is; the selection only filters it.
            return False
        if self._fsm.interactive:
            return not self._rows_cover_selection()
        return self._fetched_selection != self._selection_key()

    def _rows_cover_selection(self):
        """Whether the rows in hand include the chemicals now picked.

        INTERACTIVE MODE'S STALENESS, and why it differs. Equality of keys
        is right for a fetch-then-map workflow; in interactive mode it would
        make every pick stale - fetch five chemicals, go single-select, and
        the key {A} no longer equals {A..E}, so nothing could ever be drawn.
        Rows fetched for five chemicals answer a question about one of them.
        Every OTHER parameter must still match exactly: a different matrix
        or date range really is different rows.
        """
        if self._fetched_selection is None:
            return False
        fetched = dict(self._fetched_selection)
        wanted = dict(self._selection_key())
        asked = fetched.pop(PARAM_ANALYTES, None)
        picked = wanted.pop(PARAM_ANALYTES, None)
        if fetched != wanted:
            return False
        if asked is None:
            # The fetch named no chemicals, which means all of them.
            return True
        if picked is None:
            return False
        return (set(picked.split(LIST_SEPARATOR))
                <= set(asked.split(LIST_SEPARATOR)))

    # Filters that re-scope rows already in hand rather than changing which
    # rows exist. They are offered only AFTER a fetch, from what that fetch
    # returned, so making them part of the key would be circular: ticking a
    # method the data just reported would mark that same data stale and
    # demand another fetch to apply a filter the rows can already satisfy.
    # Both are applied locally (_in_method / _in_fraction), so nothing is
    # lost by leaving them out.
    LOCAL_ONLY_PARAMS = (PARAM_METHODS, PARAM_FRACTIONS)

    def _selection_layers(self):
        """The layers a map selection is read from and mirrored across.

        Locations, the scatter's own map, and every result layer mapped (the
        preview among them) - each connected by _add_to_group, or where
        Locations is added. Alive ones only, each once.
        """
        candidates = [self._locations_layer, getattr(self, "_scatter_layer", None)]
        candidates += list(getattr(self, "_result_layers", None) or [])
        layers = []
        for lyr in candidates:
            if (lyr is not None and self._layer_alive(lyr)
                    and all(lyr is not seen for seen in layers)):
                layers.append(lyr)
        return layers

    @staticmethod
    def _selected_codes_on(layer):
        """The location codes of a layer's selected features, as stored."""
        if not layer.selectedFeatureCount():
            return []
        index = layer.fields().indexOf(LOCATION_FIELD)
        if index < 0:
            return []
        # The code and nothing else: a per-result layer holds 250,000
        # features, and a selection of thousands of them must not fetch
        # every geometry and attribute to read one column (US-6: 10,000
        # selected must not stall the UI).
        request = (QgsFeatureRequest()
                   .setFlags(QgsFeatureRequest.Flag.NoGeometry)
                   .setSubsetOfAttributes([index]))
        codes = []
        for feature in layer.getSelectedFeatures(request):
            code = feature.attribute(index)
            if code not in (None, ""):
                codes.append(str(code))
        return codes

    def _ids_by_code(self, layer):
        """Lower-cased location code -> feature ids, for one layer.

        INDEXED ONCE PER LAYER STATE, keyed on the feature count as the
        brush's index is (linked_views._features_for): scanning a layer on
        every selection would make each one O(dataset). Ids only, no
        geometry - this answers "which features are at these stations".
        """
        cache = self.__dict__.setdefault("_code_index", {})
        key = layer.featureCount()
        hit = cache.get(layer.id())
        if hit is not None and hit[0] == key:
            return hit[1]
        index = {}
        field = layer.fields().indexOf(LOCATION_FIELD)
        if field >= 0:
            request = (QgsFeatureRequest()
                       .setFlags(QgsFeatureRequest.Flag.NoGeometry)
                       .setSubsetOfAttributes([field]))
            for feature in layer.getFeatures(request):
                code = str(feature.attribute(field) or "").strip().lower()
                if code:
                    index.setdefault(code, []).append(feature.id())
        cache[layer.id()] = (key, index)
        return index

    def _select_codes(self, layer, codes):
        """Select exactly the features of `layer` at `codes`. Guarded: the
        selectionChanged this causes is not a new selection to mirror."""
        index = self._ids_by_code(layer)
        wanted = {str(c).strip().lower() for c in codes}
        ids = sorted({i for c in wanted for i in index.get(c, ())})
        if set(layer.selectedFeatureIds()) == set(ids):
            return
        self._mirroring = True
        try:
            layer.selectByIds(ids)
        finally:
            self._mirroring = False

    def _mirror_selection(self, source):
        """One selection, by location code, on every layer of ours.

        QGIS draws a selection on the layer it was made on, and ours stack
        at the same stations: a selection on Locations was drawn yellow and
        then covered by the scatter map over it (measured 2026-10-29: 21
        selection-colour pixels with Locations rendered alone, 0 under the
        scatter map). Selecting the same stations on the others puts the
        yellow on whichever layer is on top. The source keeps exactly what
        the user selected; only the others follow.

        IT CROSSES THE FETCH SEAM on purpose: a selection made on the
        scatter's map now selects those Locations too, so the "Only
        locations selected on the map" box counts it. The box still gates
        the fetch, and its label shows the count.
        """
        codes = self._selected_codes_on(source)
        for layer in self._selection_layers():
            if layer is not source:
                self._select_codes(layer, codes)

    def _map_selection_codes(self):
        """Every location selected on the map, whatever the checkbox says.

        DELIBERATELY NOT _selected_location_codes. That one is gated on the
        checkbox because it narrows a FETCH - an incidental selection must
        not silently change what is asked of EQuIS. This one narrows what
        is already in hand, costs nothing, and is undone by clicking empty
        space, so the same caution does not apply.
        US-6 calls those the two sides of the fetch seam.

        Returns None when nothing is selected, which is different from an
        empty list: None means "no location filter", [] would mean "these
        zero locations".
        """
        layers = self._selection_layers()
        if not layers:
            return None
        codes = []
        for layer in layers:
            codes.extend(self._selected_codes_on(layer))
        return sorted(set(codes)) or None

    def _selected_location_codes(self):
        """Location codes ticked for the fetch, or [] for no location filter.

        Read from the Locations layer's QGIS selection, and only when the
        checkbox says to use it: an incidental selection left on the map must
        not silently narrow a fetch.
        """
        if not getattr(self, "map_selection_check", None):
            return []
        if not self.map_selection_check.isChecked():
            return []
        layer = self._locations_layer
        if layer is None or not self._layer_alive(layer):
            return []
        codes = []
        for feature in layer.selectedFeatures():
            try:
                code = feature[LOCATION_FIELD]
            except KeyError:
                continue
            if code not in (None, ""):
                codes.append(str(code))
        return sorted(set(codes))

    def _refresh_map_selection_label(self):
        """Put the live selection count in the checkbox text."""
        if not getattr(self, "map_selection_check", None):
            return
        layer = self._locations_layer
        count = (layer.selectedFeatureCount()
                 if layer is not None and self._layer_alive(layer) else 0)
        self.map_selection_check.setText(
            "Only locations selected on the map (%s)" % format(count, ",d"))
        # Nothing selected means the box can only ever mean "all", so it is
        # not offered as a choice - but a ticked box is left ticked, so a
        # selection made a moment later takes effect without another click.
        self.map_selection_check.setEnabled(
            count > 0 or self.map_selection_check.isChecked())

    def _on_map_selection_changed(self, *_args):
        if getattr(self, "_mirroring", False):
            # Our own _select_codes: the selection that caused it is being
            # handled already, once.
            return
        source = self.sender()
        if source is not None and hasattr(source, "selectedFeatureIds"):
            self._mirror_selection(source)
        self._refresh_map_selection_label()
        # US-6: selecting on the map re-bins the chart, through QGIS's own
        # selection tools - the link is to layer.selectionChanged, so
        # rectangle, polygon, freehand and select-by-expression all drive
        # it without the plugin inventing a tool.
        self._refresh_chart()
        if self.map_selection_check.isChecked():
            # The request changed, so the key and the stale flag follow.
            self._on_selection_changed()

    def _current_selection(self, facility_id=None):
        """The DataSelection the controls describe, locations included.

        The panel owns every picker but the map selection, which needs the
        Locations layer only the dialog holds; this is the one place the two
        are put together, so every caller sees the same request.
        """
        return self.selection_panel.selection(
            facility_id, locations=self._selected_location_codes())

    def _selection_key(self):
        """What makes two fetches the same request.

        Only the filters that decide which rows the SERVER generates. A
        filter applied locally does not change the request, so including it
        would refetch identical data to look at a subset of it.
        """
        facility = self._current_facility() or {}
        selection = self._current_selection(facility.get("id"))
        return tuple(sorted(
            (name, value) for name, value in selection.to_parameters().items()
            if value is not None and name not in self.LOCAL_ONLY_PARAMS
        ))

    def _confirm_fractions(self, analytes, how, ask=True):
        """Refuse to aggregate across dissolved and total. Returns keep-going.

        Dissolved and total are drawn from the same water on the same day and
        can differ by more than an order of magnitude, so a maximum taken
        across both reports whichever fraction happened to be higher - a
        number that looks entirely plausible on the map and is wrong.

        This blocks rather than warns because the existing warning was proved
        not to survive: it is written at report load and overwritten by the
        row-count summary moments later. A filter that silently produces the
        wrong number needs a stop, not a message.

        Only the aggregated paths are gated. Unaggregated mode carries the
        fraction on every feature, so nothing is being mixed and there is
        nothing to refuse.
        """
        if how == UNAGGREGATED:
            return True
        chosen = self._fraction_mode()
        if chosen is not ANY_FRACTION and len(chosen) == 1:
            # Exactly one fraction: nothing can be crossed.
            return True
        if self._analysis is None:
            return True

        matrix = self._matrix_mode()
        picked = set(chosen or ())
        mixed = []
        for analyte in analytes:
            found = self._analysis.fractions_for(
                analyte["chemical"], analyte["cas_rn"], matrix)
            if picked:
                # Only the ticked ones can reach the aggregate, so only they
                # can be crossed.
                found = [f for f in found if f.upper() in
                         {p.upper() for p in picked}]
            if len(found) > 1:
                mixed.append((analyte["chemical"], found))
        if not mixed:
            return True

        names = dict(FRACTIONS)
        detail = "; ".join(
            "%s (%s)" % (chemical, ", ".join(
                "%s - %s" % (code, names.get(code, code)) for code in found))
            for chemical, found in mixed[:4])
        if len(mixed) > 4:
            detail += "; and %d more" % (len(mixed) - 4)
        if not ask:
            # As in _confirm_units: refused without a modal in interactive
            # mode, never waved through.
            self._set_status(
                "Not mapped: %s mixes dissolved and total. Choose one "
                "fraction, or 'Every result'." % detail,
                Qgis.MessageLevel.Warning)
            return False

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Mixed fractions")
        # Lead with the number, not the rule: this stops a build that used to
        # succeed, and without the reason it reads as the plugin breaking.
        box.setText(
            "These results mix dissolved and total: %s.\n\n"
            "The same sample can differ by more than 20x between them, so a "
            "%s taken across both would report whichever fraction happened "
            "to be higher." % (detail, dict(AGGREGATIONS).get(how, how).lower()))
        box.setInformativeText("Map one fraction, or map every result "
                               "separately and keep them apart on the map.")
        dissolved = box.addButton("Map dissolved",
                                  QMessageBox.ButtonRole.AcceptRole)
        total = box.addButton("Map total", QMessageBox.ButtonRole.AcceptRole)
        every = box.addButton("Map every result",
                              QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()

        clicked = box.clickedButton()
        if clicked is dissolved or clicked is total:
            code = FRACTION_DISSOLVED if clicked is dissolved else FRACTION_TOTAL
            self.selection_panel.set_selected_fractions([code])
            self._on_build_layer()
        elif clicked is every:
            index = self.aggregation_combo.findData(UNAGGREGATED)
            if index >= 0:
                self.aggregation_combo.setCurrentIndex(index)
            self._on_build_layer()
        # Either way this attempt is over: the re-entered call has done the
        # work, and carrying on here would build the mixed layer anyway.
        return False

    def _confirm_units(self, analytes, how, ask=True):
        """Refuse to aggregate across units. Returns keep-going.

        THE SAME FAILURE AS MIXED FRACTIONS, and it blocks for the same
        reason. Measured on Facility A 2026-09-24: Arsenic in WS is 68,371 rows
        in ug/L and 22,121 in mg/L - a factor of 1000 - and 237 (chemical,
        matrix) pairs mix units this way. A maximum across them ranks by
        UNIT, not by concentration: 0.57 mg/L is 570 ug/L and loses to
        168 ug/L. The map draws both on one ramp and every number on it
        looks entirely plausible.

        Unaggregated mode is not gated: each feature carries its own unit,
        so nothing is being crossed.

        There is no "convert them" button, deliberately. EQuIS can do the
        conversion at the source - the ARII base takes @reporting_unit - and
        doing it here would mean this plugin owning a unit-conversion table
        and being wrong about it silently. Saying which units are present,
        and which filter would separate them, is the honest stop.
        """
        if how == UNAGGREGATED or self._analysis is None:
            return True
        # No "has it got units_for?" escape: a ReportTable once lacked it and
        # the guard silently passed every live report until its store landed.
        # Both implementations answer it now (tools/test_store_parity.py).

        matrix = self._matrix_mode()
        fraction = self._fraction_mode()
        method = self._method_mode()
        mixed = []
        for analyte in analytes:
            found = self._analysis.units_for(
                analyte["chemical"], analyte["cas_rn"], matrix=matrix,
                fraction=fraction, method=method,
                non_detect=self._non_detect_mode())
            named = [(unit, rows) for unit, rows in found if unit]
            if len(named) > 1:
                mixed.append((analyte["chemical"], named))
        if not mixed:
            return True

        detail = "; ".join(
            "%s (%s)" % (chemical, ", ".join(
                "%s x%s" % (unit, format(rows, ",d"))
                for unit, rows in found[:3]))
            for chemical, found in mixed[:3])
        if len(mixed) > 3:
            detail += "; and %d more" % (len(mixed) - 3)
        if not ask:
            # Interactive mode: a modal on every pick would make browsing
            # impossible, but the refusal stands. Said on the status line,
            # and the caller takes the preview down.
            self._set_status(
                "Not mapped: %s is reported in more than one unit (%s). "
                "Narrow the matrix or method, or choose 'Every result'."
                % (mixed[0][0], detail), Qgis.MessageLevel.Warning)
            return False

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Mixed units")
        box.setText(
            "These results are reported in more than one unit: %s.\n\n"
            "A %s taken across them ranks by unit rather than by "
            "concentration - 0.57 mg/L is 570 ug/L, and would be drawn "
            "below a result of 168 ug/L." % (
                detail, dict(AGGREGATIONS).get(how, how).lower()))
        box.setInformativeText(
            "Narrow the matrix or method until one unit remains, or map "
            "every result so each feature carries its own unit. To fix it "
            "at the source, set the report's Result/Unit parameter "
            "(@reporting_unit) so EQuIS converts before the data arrives.")
        every = box.addButton("Map every result",
                              QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()

        if box.clickedButton() is every:
            index = self.aggregation_combo.findData(UNAGGREGATED)
            if index >= 0:
                self.aggregation_combo.setCurrentIndex(index)
            self._on_build_layer()
        return False

    def _on_build_layer(self, *_args, preview=False):
        """Add one layer per selected chemical; the last becomes active.

        Selecting several chemicals and getting one layer was the old
        behaviour and it discarded the rest of the selection silently. Each
        selected chemical now gets its own joined layer, and the last one added
        is the active layer that the time-series tool and the sparkline
        controls act on.

        `preview` is interactive mode: one chemical, always a memory layer,
        replacing the previous preview rather than anything by view key, and
        without the modals and the message bar a deliberate build earns.
        Returns True when a layer was drawn. (`*_args` absorbs the `checked`
        a button's clicked signal passes, which would otherwise land here.)
        """
        analytes = self._selected_analytes()
        if not analytes:
            # currentItem() can be set with nothing selected, e.g. after the
            # list is repopulated by a filter.
            current = self._current_analyte()
            analytes = [current] if current else []
        if not analytes:
            self._set_status("Select a chemical to map.",
                             Qgis.MessageLevel.Warning)
            return
        if not self._location_pages:
            self._set_status("Location points are not loaded yet.",
                             Qgis.MessageLevel.Warning)
            return

        # Mapping draws what has been fetched; it never fetches. This used
        # to launch a report run whenever the selection had moved on, so
        # picking a different chemical after mapping one silently started a
        # server round-trip: the progress bar restarted, the panel repainted,
        # and the two actions the UI separates into steps 4 and 5 turned out
        # to be one button after all.
        #
        # Refusing here is what makes "Pre-fetch data" mean what it says.
        if self._needs_fetch():
            self._set_status(
                "The chemicals selected are not in the data fetched. Press "
                "'Pre-fetch data' in step 4 to retrieve them, then map.",
                Qgis.MessageLevel.Warning,
            )
            self._update_actions()
            return
        changed = self._source_changed()
        if changed:
            self._set_status(changed, Qgis.MessageLevel.Warning)
            return False
        if not preview and not self._confirm_bulk_add(len(analytes)):
            return

        # De-duplicate and put the current item last, so the chemical the user
        # clicked is the one left active rather than whatever sorts last.
        # Identity is (chemical, cas_rn), not the dict: PyQt marshals item data
        # through QVariant, so the dict from selectedItems() is not the same
        # object as the one from currentItem() even for the same row.
        current = self._current_analyte()
        selected_keys = {_analyte_key(a) for a in analytes}
        # Only promote the current item when it is actually part of the
        # selection; otherwise it would smuggle in an unselected chemical.
        current_key = (_analyte_key(current)
                       if current and _analyte_key(current) in selected_keys
                       else None)
        ordered = []
        seen = set()
        for analyte in analytes:
            key = _analyte_key(analyte)
            if key in seen or key == current_key:
                continue
            seen.add(key)
            ordered.append(analyte)
        if current_key is not None:
            ordered.append(current)
        analytes = ordered

        how = self.aggregation_combo.currentData() or MOST_RECENT
        facility = self._current_facility() or {}
        # The short form: the combo's label for UNAGGREGATED is a sentence,
        # which is right in the picker and unusable in a layer name.
        label = AGGREGATION_SHORT.get(how, dict(AGGREGATIONS).get(how, how))
        from_cache = (self._report_source in (CACHE, CACHE_FALLBACK)
                      or self._locations_source in (CACHE, CACHE_FALLBACK))
        # An unsaved project has no folder to write beside, so fall back to a
        # memory layer for now rather than inventing a location for the file.
        gpkg_path = self._project_gpkg()
        storage = self._storage_mode()
        if storage == GEOPACKAGE and not gpkg_path:
            storage = MEMORY
        if preview:
            # Scratch until kept: a GeoPackage table written per click would
            # litter the project file with every chemical glanced at.
            storage = MEMORY
            gpkg_path = None

        # Checked BEFORE anything is removed below: a refused build must
        # leave the map exactly as it was, not destroy the layer it was
        # about to replace. A PREVIEW is the exception, taken down by the
        # caller on refusal: it would otherwise stay up under a pick it
        # does not show.
        if not self._confirm_units(analytes, how, ask=not preview):
            return False
        if not self._confirm_fractions(analytes, how, ask=not preview):
            return False

        # Replace only the views being re-added. Two layers over the same data
        # with different symbology are two useful views, not a duplicate, so
        # adding circles must not destroy the sparklines beside them.
        wanted = {_view_key(a, how, self._sparkline_mode(),
                            self._matrix_mode(), self._fraction_mode(),
                            self._method_mode())
                  for a in analytes}
        self._stop_roll(restyle=False)
        # Where the outgoing preview sat, so its replacement lands in the
        # same place instead of jumping to the top of the group.
        preview_at = self._drop_preview() if preview else None
        kept = {}
        for key, layer in self._layer_views.items():
            if not self._layer_alive(layer):
                continue
            if key in wanted and not preview:
                self._remove_layer(layer)
                continue
            kept[key] = layer
        self._layer_views = kept
        self._result_layers = list(kept.values())
        if self._result_layer is not None and not self._layer_alive(self._result_layer):
            self._result_layer = None

        if len(analytes) > 1:
            self._set_busy(True)
            self._set_status("Building %d layers..." % len(analytes))
            QgsApplication.processEvents()

        built = []
        warnings = []
        empty = []
        try:
            for analyte in analytes:
                summary = {}
                results = []
                if how == UNAGGREGATED:
                    results, dropped = self._analysis.rows_for(
                        analyte["chemical"], analyte["cas_rn"],
                        non_detect=self._non_detect_mode(),
                        matrix=self._matrix_mode(),
                        fraction=self._fraction_mode(),
                        method=self._method_mode(),
                        limit=UNAGGREGATED_ROW_LIMIT,
                    )
                    if dropped:
                        warnings.append(
                            "%s has more than %s results; %d location%s with "
                            "the least history were left out."
                            % (analyte["chemical"],
                               "{:,}".format(UNAGGREGATED_ROW_LIMIT), dropped,
                               "" if dropped == 1 else "s"))
                else:
                    summary = self._analysis.aggregate(
                        analyte["chemical"], analyte["cas_rn"], how=how,
                        non_detect=self._non_detect_mode(),
                        matrix=self._matrix_mode(),
                        fraction=self._fraction_mode(),
                        method=self._method_mode(),
                    )
                # The symbology is part of the name: two views of one chemical
                # can now coexist, and two identically named layers in the
                # panel would be indistinguishable. So is the matrix, for the
                # same reason - and it is omitted when unfiltered, so an
                # unfiltered report keeps the names it has always had.
                matrix = self._matrix_mode()
                matrix_suffix = (", %s" % "+".join(matrix)) if matrix else ""
                # The fraction selects different rows just as the matrix
                # does, so it belongs in both names. Without it in the table
                # name a dissolved layer and a total layer of one chemical
                # would overwrite each other in the GeoPackage.
                fraction = self._fraction_mode()
                fraction_suffix = (", %s" % "+".join(fraction)) if fraction else ""
                # Method too, for exactly the same reason: two methods can
                # disagree about whether the analyte was detected at all, so
                # the two layers are different data and must not collide -
                # in the panel or, more damagingly, in the GeoPackage.
                method = self._method_mode()
                method_suffix = (", %s" % "+".join(method)) if method else ""
                name = cached_layer_name(
                    "%s - %s (%s%s%s%s%s)" % (
                        facility.get("name", "EQuIS"), analyte["chemical"],
                        label.lower(), matrix_suffix, fraction_suffix,
                        method_suffix,
                        ", sparklines" if self._sparkline_mode() else "",
                    ),
                    from_cache,
                )
                # The table is named for the data, without the symbology, so
                # a chemical drawn two ways shares one table rather than
                # storing the same rows twice. Matrix, fraction and the
                # aggregation all belong here: each selects different rows,
                # not a different drawing of the same ones.
                table_name = "%s - %s (%s%s%s%s)" % (
                    facility.get("name", "EQuIS"), analyte["chemical"],
                    label.lower(), matrix_suffix, fraction_suffix,
                    method_suffix)

                if how == UNAGGREGATED:
                    layer, matched, unmatched, warning = build_result_layer(
                        name, self._location_pages, results,
                        analyte["chemical"],
                        fallback_wkid=facility.get("wkid") or self._service_wkid,
                        location_field=LOCATION_FIELD, storage=storage,
                        gpkg_path=gpkg_path, table_name=table_name,
                    )
                else:
                    layer, matched, unmatched, warning = build_joined_layer(
                        name, self._location_pages, summary,
                        analyte["chemical"],
                        fallback_wkid=facility.get("wkid") or self._service_wkid,
                        location_field=LOCATION_FIELD, storage=storage,
                        gpkg_path=gpkg_path, table_name=table_name,
                    )
                if layer is None:
                    warnings.append("%s could not be built" % analyte["chemical"])
                    continue
                if warning:
                    warnings.append(warning)
                if not matched:
                    empty.append(analyte["chemical"])
                # Written once, here, from the fetch that produced these rows
                # and the filters in force at this moment. Never rewritten:
                # later panel changes describe the NEXT layer, not this one.
                self._write_provenance(layer, name, analyte, how)
                if preview:
                    # Marked on the layer as well as held here, so a preview
                    # found in a project later can be told apart from a kept
                    # layer of the same name.
                    layer.setCustomProperty("equis/preview", True)
                self._add_to_group(layer, index=preview_at)
                drawn, _ = self._apply_symbology(layer, analyte, how=how)
                if how == UNAGGREGATED and not drawn:
                    # Every result for a location sits on the one coordinate,
                    # so without this only the topmost is ever visible and the
                    # layer looks exactly like a maximum.
                    #
                    # Not when profiles were drawn: a profile IS the whole
                    # stack in one picture, so fanning the features apart
                    # would scatter identical copies of it around the point.
                    apply_point_displacement(layer)
                    # Configured but left switched off, so the layer is ready
                    # for the Temporal Controller without vanishing the
                    # moment it is added.
                    enable_temporal(layer)
                if preview:
                    self._preview_layer = layer
                else:
                    self._layer_views[_view_key(
                        analyte, how, self._sparkline_mode(),
                        self._matrix_mode(), self._fraction_mode(),
                        self._method_mode())] = layer
                # For an unaggregated layer `unmatched` is the list of
                # location CODES that had no point to sit on, not a count of
                # locations with no result. An empty layer here is a code
                # mismatch, not a missing chemical, and naming the codes is
                # the difference between a five-second fix and a bug report.
                unplaced = list(unmatched) if how == UNAGGREGATED else []
                # STAMP THE SCOPE ONTO THE LAYER. Clicking a point plots a
                # series, and the series has to be of what THAT layer
                # shows - otherwise clicking a Manganese layer plots
                # whatever the panel's picker happens to say, which is what
                # it did. The panel's picker moves; a layer does not.
                #
                # Custom properties rather than parsing the layer name: the
                # name is for people and carries a "[cached]" suffix, a
                # "+" between several methods and whatever the user renames
                # it to. These survive a project save, so a reopened
                # project still plots the right thing.
                self._stamp_scope(layer, analyte, how)
                built.append({"layer": layer, "analyte": analyte,
                              "matched": matched,
                              "unmatched": len(unmatched)
                              if how == UNAGGREGATED else unmatched,
                              "unplaced": unplaced,
                              "per_result": how == UNAGGREGATED})
        finally:
            self._set_busy(False)

        if not built:
            self._set_status("Could not build the layer.", Qgis.MessageLevel.Critical)
            return False

        self._layer_storage = storage if not warnings else MEMORY
        # Everything still mapped, not just what this press produced.
        self._result_layers = [layer for layer in self._layer_views.values()
                               if self._layer_alive(layer)]
        if preview and self._preview_layer is not None:
            self._result_layers.append(self._preview_layer)
        active = built[-1]
        self._result_layer = active["layer"]
        if self._linked is not None:
            # Index the layer for brushing NOW. It is O(dataset) - 1.3 s at
            # 250,000 features - while every brush afterwards is O(selection)
            # at ~11 ms. Left until the first drag, that whole cost lands on
            # the one interaction US-1 budgets at 100 ms.
            try:
                self._linked.prepare()
            except Exception as exc:  # noqa: BLE001 - a failed index must
                # not cost the user the layer they asked for.
                QgsMessageLog.logMessage(
                    "could not index the layer for brushing: %s" % exc,
                    "equis-web", Qgis.Warning)
        self._active_aggregation = how
        self._mapped_chemical = active["analyte"]["chemical"]
        # _result_layer is set above, so the capabilities now allow the pick
        # tool and the roll; asserting them here as well would be a second
        # opinion that could disagree.
        self._update_actions()
        # Arm the tool once, so the advertised "click a point" works straight
        # away; re-running for another chemical leaves the user's current tool
        # choice alone and just repoints ours at the new layer.
        #
        # NOT for a preview: browsing is where the map's own selection tools
        # get used (US-6 re-bins the chart from them), and taking the cursor
        # away on the first pick would break the other half of the mode.
        if self._map_tool is None:
            if not preview:
                self.set_pick_tool_active(True)
        else:
            self._map_tool.layer = self._result_layer
        if preview:
            if not self._preview_zoomed:
                self._zoom_to(self._result_layer)
                self._preview_zoomed = True
            # The chart follows the ACTIVE layer (_chart_scope), so the new
            # preview is made active: otherwise removing the old one hands
            # "active" to whatever QGIS picks next and the chart follows that.
            try:
                self.iface.setActiveLayer(self._result_layer)
            except Exception:                   # noqa: BLE001
                pass
            self._refresh_chart()
        else:
            self._zoom_to(self._result_layer)

        message = self._describe_built(built, label, storage, gpkg_path,
                                       warnings, empty)
        # SAID, NOT REFUSED. Matrices may aggregate together when their units
        # agree - the unit guard is the one gate (decided 2026-10-01; see
        # CLAUDE.md). Where units do NOT separate water from solid - leachate
        # and dissolved-on-solid results in ug/L or mg/L, pH/ORP/percent - the
        # combination is the analyst's call, so the panel makes sure it was
        # seen rather than modelling matrix families to second-guess it.
        matrices = self._matrix_mode() or ()
        if how != UNAGGREGATED and len(matrices) > 1:
            message += (" Aggregated across %d matrices (%s): each location's "
                        "value may come from any of them."
                        % (len(matrices), "+".join(matrices)))
        level = Qgis.MessageLevel.Success
        if warnings or len(empty) == len(built):
            level = Qgis.MessageLevel.Warning
        self._update_active_label()
        self._update_storage_hint()
        self._set_status(message, level)
        if preview and level == Qgis.MessageLevel.Success:
            # A message bar per pick would stack up while browsing. The
            # status line carries it; warnings still go to the bar, and the
            # layer name still carries "[cached]".
            return True
        self.iface.messageBar().pushMessage(
            "EQuIS", message, level=level, duration=7,
        )
        return True

    # --------------------------------------------------- interactive mode

    def _on_interactive_toggled(self, checked):
        """Arm or disarm interactive mode. See panel_state's docstring."""
        if checked:
            # Before the machine is told, so the roll's own teardown runs
            # (timer, label, renderer); the machine would end the MODE
            # regardless, but not the timer still firing at the layer.
            self._stop_roll(restyle=True)
        self._fsm.set_interactive(checked, "interactive toggled")
        self.selection_panel.set_single_select(checked)
        # The map button builds many layers from a multi-selection; in this
        # mode the layer is already drawn, and keeping it is the question.
        self.map_btn.setVisible(not checked)
        self.keep_btn.setVisible(checked)
        if checked:
            self._preview_zoomed = False
            self._preview_key = None
            self._schedule_preview()
        else:
            self._preview_timer.stop()
            had = self._drop_preview()
            self._refresh_active_after_preview()
            if had is not None:
                self._set_status(
                    "Interactive mode off. The temporary layer was removed; "
                    "'Keep layer' holds one before switching off.")
        self._update_actions()

    def _schedule_preview(self):
        """Ask for the preview to follow the controls, once they settle."""
        if self._fsm.interactive:
            # Who asked, so a change that never reaches the map can be told
            # apart from one that reached it and was decided against.
            # Reported 2026-10-01: WS -> WG did not redraw until the next
            # pick, and did not reproduce headless on the same data.
            import sys
            caller = sys._getframe(1).f_code.co_name
            self._preview_log("scheduled by %s" % caller)
            self._preview_timer.start()

    @staticmethod
    def _preview_log(message):
        # Beside the chart's lines, so one tab shows both in order.
        QgsMessageLog.logMessage("interactive: %s" % message, "equis-web",
                                 Qgis.MessageLevel.Info)

    def _preview_question(self):
        """Everything a preview is drawn from. Equal means nothing to redraw.

        Every control whose change should replace the layer is in here, and
        nothing else: a change that does not alter the question must not
        tear down and rebuild a layer the user is looking at.
        """
        analyte = self._current_analyte()
        if analyte is None:
            return None
        return (
            analyte.get("chemical"), analyte.get("cas_rn"),
            self.aggregation_combo.currentData() or MOST_RECENT,
            self._non_detect_mode(),
            tuple(self._matrix_mode() or ()),
            tuple(self._fraction_mode() or ()),
            tuple(self._method_mode() or ()),
            bool(self._sparkline_mode()),
            self._fetched_selection,
            id(self._analysis),
        )

    def _update_preview(self):
        """Draw the current pick as the preview, replacing the last one.

        The single decision point for what interactive mode shows. When the
        pick cannot be drawn - nothing picked, rows that do not answer it, a
        unit or fraction refusal - the preview comes DOWN rather than
        staying up under a pick it does not show.
        """
        if not self._fsm.interactive:
            return
        self._capabilities()          # brings busy/stale on the machine up to date
        question = self._preview_question()
        alive = (self._preview_layer is not None
                 and self._layer_alive(self._preview_layer))
        # Same question, still answered: by the preview, or - just after
        # Keep - by the kept layer, which a duplicate preview would sit on.
        if (question is not None and question == self._preview_key
                and (alive or self._preview_layer is None)):
            self._preview_log("unchanged, nothing to redraw: %s"
                              % self._describe_question(question))
            return
        reason = None
        if question is None:
            reason = "Pick a chemical to map it."
        elif not self._fsm.may_preview:
            if self._fsm.busy:
                # Try again when the task lands; _enter reschedules.
                self._preview_log("busy (a task is running); waiting for "
                                  "it to land")
                return
            if self._fsm.state != st.LOADED:
                reason = ("Interactive mode draws once rows are loaded. "
                          "Pre-fetch data in step 4.")
            else:
                reason = ("The rows fetched do not answer %s with the "
                          "step 4 filters as they are now. Pre-fetch data "
                          "in step 4 - with no chemicals selected it "
                          "fetches them all, and every pick after that "
                          "draws at once." % question[0])
        elif not (self._location_pages and self._has_geometry):
            reason = "This facility has no location points to map."
        elif self._source_changed() is not None:
            # Asked again for the message: two stats, against a draw.
            reason = self._source_changed()
        if reason is not None:
            self._preview_log("not drawn: %s" % reason)
            self._preview_key = None
            if self._drop_preview() is not None:
                self._refresh_active_after_preview()
            self._set_status(reason, Qgis.MessageLevel.Info)
            self._update_actions()
            return
        # Recorded BEFORE building: the build emits change signals of its
        # own, and they must find the question already answered.
        self._preview_key = question
        self._preview_log("drawing %s" % self._describe_question(question))
        try:
            drawn = self._on_build_layer(preview=True)
        except Exception as exc:                # noqa: BLE001
            # A read that fails mid-browse - a file being rewritten, a
            # network drop on a remote set - is a message on the status
            # line, not a traceback; the preview comes down below like any
            # other failure to draw.
            self._set_busy(False)
            self._set_status("Could not read the data to map %s: %s"
                             % (question[0], exc), Qgis.MessageLevel.Warning)
            drawn = False
        self._preview_log("drawn" if drawn else
                          "refused or failed: %s" % self.status_label.text())
        if not drawn:
            # Refused or failed. The old preview is already gone (a refusal
            # before the drop leaves it, so drop it here too), and the key is
            # cleared so correcting the control that caused it redraws.
            self._preview_key = None
            if self._drop_preview() is not None:
                self._refresh_active_after_preview()
            self._update_actions()

    @staticmethod
    def _describe_question(question):
        """A preview question as one readable log line."""
        names = ("chemical", "cas", "how", "nd", "matrix", "fraction",
                 "method", "sparklines")
        return ", ".join("%s=%s" % (name, value)
                         for name, value in zip(names, question)
                         if value not in (None, "", ()))

    def _source_changed(self):
        """A message if the open parquet files changed on disk, else None.

        See ReportStore.changed_files. Asked before drawing, so a rewritten
        source is refused by name rather than mapped as if it were the data
        the panel's lists and counts describe.
        """
        store = self._report_store
        changed = (store.changed_files()
                   if store is not None and hasattr(store, "changed_files")
                   else [])
        if not changed:
            return None
        return ("%s changed on disk after it was opened, so the chemical "
                "list and counts here no longer describe it. Open the source "
                "again to map the new data."
                # The last three parts: every partition's file is named
                # part-0000.parquet, so the basename alone names nothing.
                % "/".join(changed[0].replace("\\", "/").split("/")[-3:]))

    def _drop_preview(self):
        """Remove the preview layer. Returns its index in the group, or None.

        The index is so a replacement can go back where it was; the layer
        panel should not reshuffle on every pick.
        """
        layer = self._preview_layer
        self._preview_layer = None
        if layer is None or not self._layer_alive(layer):
            return None
        index = None
        root = QgsProject.instance().layerTreeRoot()
        node = root.findLayer(layer.id())
        if node is not None and node.parent() is not None:
            index = node.parent().children().index(node)
        if self._linked is not None:
            # A highlight drawn over a layer about to go belongs to nothing.
            try:
                self._linked.clear_brush()
            except Exception:                   # noqa: BLE001
                pass
        self._result_layers = [l for l in self._result_layers
                               if l is not layer]
        if self._result_layer is layer:
            self._result_layer = None
        self._remove_layer(layer)
        return index

    def _refresh_active_after_preview(self):
        """After the preview goes with nothing replacing it, re-point the rest.

        The pick tool and the roll act on _result_layer; with the preview
        gone they fall back to the last kept layer, as they do when the user
        deletes one.
        """
        if self._result_layer is None:
            alive = [l for l in self._layer_views.values()
                     if self._layer_alive(l)]
            self._result_layer = alive[-1] if alive else None
            if self._map_tool is not None:
                self._map_tool.layer = self._result_layer
            if self._result_layer is None:
                self._mapped_chemical = None
                self._active_aggregation = None
        self._update_active_label()

    def _on_keep_preview(self):
        """Make the preview an ordinary layer; the next pick starts a new one.

        Written to the project's GeoPackage when the storage choice says so
        and the project is saved - the same file and table naming a normal
        build uses - otherwise it stays a memory layer, but a KEPT one: the
        next pick no longer replaces it.
        """
        layer = self._preview_layer
        if layer is None or not self._layer_alive(layer):
            self._set_status("Nothing to keep yet. Pick a chemical first.",
                             Qgis.MessageLevel.Warning)
            return
        scope = self._layer_scope(layer) or {}
        analyte = {"chemical": scope.get("chemical") or "",
                   "cas_rn": scope.get("cas_rn") or ""}
        how = scope.get("aggregation") or MOST_RECENT
        key = _view_key(analyte, how, self._sparkline_mode(),
                        self._matrix_mode(), self._fraction_mode(),
                        self._method_mode())
        # Keeping a view that is already kept replaces it, exactly as
        # mapping it twice would.
        old = self._layer_views.get(key)
        if old is not None and self._layer_alive(old):
            self._remove_layer(old)

        kept = layer
        where = "in memory - save the project to write it beside it"
        gpkg_path = self._project_gpkg()
        if self._storage_mode() == GEOPACKAGE and gpkg_path:
            name = layer.name()
            saved, error = persist_to_geopackage(layer, gpkg_path, name)
            if saved is not None:
                # Carried across: the stamped scope is what the chart and the
                # time-series tool read, and the renderer is the drawing the
                # user was looking at when they pressed Keep.
                for prop in layer.customPropertyKeys():
                    if prop.startswith("equis/") and prop != "equis/preview":
                        saved.setCustomProperty(prop, layer.customProperty(prop))
                saved.setRenderer(layer.renderer().clone())
                saved.setMetadata(layer.metadata())
                index = self._drop_preview()
                self._add_to_group(saved, index=index)
                kept = saved
                where = "in %s" % os.path.basename(gpkg_path)
            else:
                where = "in memory (%s)" % error
        if kept is layer:
            layer.removeCustomProperty("equis/preview")
            self._preview_layer = None
        self._layer_views[key] = kept
        self._result_layers = [l for l in self._layer_views.values()
                               if self._layer_alive(l)]
        self._result_layer = kept
        if self._map_tool is not None:
            self._map_tool.layer = kept
        try:
            self.iface.setActiveLayer(kept)
        except Exception:                       # noqa: BLE001
            pass
        # The kept layer answers the current question, so the key stays:
        # a repeat signal must not draw a preview on top of it. The next
        # pick or change asks something else and draws as usual.
        self._update_actions()
        self._update_active_label()
        self._set_status("Kept %s %s. The next pick draws a new temporary "
                         "layer." % (kept.name(), where),
                         Qgis.MessageLevel.Success)

    def _provenance(self, analyte, how):
        """One sentence naming the query that produced a layer.

        The fetched half comes from the selection the rows were actually
        fetched for, not the live controls - those may already describe the
        next request. The applied half is what the build is about to use.
        The in-house predecessor did this in a layer's abstract and it is the one thing a layer
        left in a project for six months still needs to say about itself.
        """
        facility = (self._current_facility() or {}).get("name", "EQuIS")
        report = (self._active_report or {}).get("name", "")
        fetched = self._fetched_selection_obj
        if fetched is not None and self._using_own_report():
            asked = fetched.describe()
        elif report:
            asked = "report '%s' as saved" % report
        else:
            asked = "all rows"
        bits = ["%s: %s" % (analyte.get("chemical", ""), asked)]
        bits.append("facility %s" % facility)
        applied = []
        for label, values in (("matrix", self._matrix_mode()),
                              ("fraction", self._fraction_mode()),
                              ("method", self._method_mode())):
            if values:
                applied.append("%s %s" % (label, "+".join(values)))
        # From the rows' own scope, not the date controls: that is what the
        # build reads, and before 2026-10-03 a parquet layer was built with
        # the dates ticked and silently ignored, and nothing here said so.
        start, end, codes = (self._analysis.scope if self._analysis is not None
                             else NO_ROW_SCOPE)
        if start is not None or end is not None:
            applied.append("sampled %s to %s" % (
                start.isoformat() if start else "the first sample",
                end.isoformat() if end else "the last"))
        if codes:
            applied.append("sample type %s" % "+".join(sorted(codes)))
        if applied:
            bits.append("filtered to " + ", ".join(applied))
        bits.append("per location: %s" % AGGREGATION_SHORT.get(
            how, dict(AGGREGATIONS).get(how, how)).lower())
        nd = self._non_detect_mode()
        bits.append("non-detects %s" % dict(ND_HANDLING).get(nd, nd).lower())
        if self._report_source in (CACHE, CACHE_FALLBACK):
            when = (self._report_entry.fetched_text()
                    if self._report_entry is not None else "earlier")
            bits.append("from cache, fetched %s" % when)
        elif self._report_entry is not None:
            bits.append("fetched %s" % self._report_entry.fetched_text())
        if self._sparkline_mode():
            bits.append("drawn as sparklines")
        return "; ".join(bits) + "."

    def _write_provenance(self, layer, name, analyte, how):
        """Put the provenance sentence where QGIS will show it.

        Both the layer metadata (Properties > Metadata, saved with the
        project) and the server properties (Properties > Information), since
        users look in either. Failure here is never worth losing the layer
        over, so it is logged and the build carries on.
        """
        sentence = self._provenance(analyte, how)
        try:
            md = layer.metadata()
            md.setTitle(name)
            md.setAbstract(sentence)
            layer.setMetadata(md)
        except Exception as exc:  # noqa: BLE001 - metadata is a courtesy
            QgsMessageLog.logMessage(
                "Could not write layer metadata: %s" % exc, "EQuIS",
                Qgis.MessageLevel.Warning)
        try:
            layer.serverProperties().setAbstract(sentence)
        except Exception:  # noqa: BLE001 - API varies by QGIS version
            pass

    def _group_label(self):
        """The group our layers belong under, named for the active session."""
        facility = (self._current_facility() or {}).get("name", "EQuIS")
        report = (self._active_report or {}).get("name", "")
        return "EQuIS - %s%s" % (facility, " - %s" % report if report else "")

    def _add_to_group(self, layer, index=None):
        """Add a layer to the project inside our own group.

        Several chemicals times two symbologies fills the layer panel quickly,
        so they are collected under one collapsible group the user can also
        delete wholesale in QGIS itself.
        """
        project = QgsProject.instance()
        label = self._group_label()
        root = project.layerTreeRoot()
        group = root.findGroup(label)
        if group is None:
            group = root.insertGroup(0, label)
        self._group_name = label
        # addMapLayer(layer, False) keeps QGIS from also placing it at the
        # tree root, which would show the layer twice.
        project.addMapLayer(layer, False)
        if index is None:
            group.addLayer(layer)
        else:
            group.insertLayer(index, layer)
        # Selecting on ANY layer of ours drives the charts - a result layer,
        # the preview, the scatter or ternary map - not only on Locations:
        # the layer a user selects on is the one they are looking at.
        # Connected here, the one place every one of them passes, and
        # disconnected first so a layer filed twice is not heard twice.
        try:
            layer.selectionChanged.disconnect(self._on_map_selection_changed)
        except (TypeError, RuntimeError):
            pass
        layer.selectionChanged.connect(self._on_map_selection_changed)
        # A new scatter map (or result layer) under an existing selection
        # starts with those stations selected, so it is yellow at once.
        codes = self._map_selection_codes()
        if codes:
            self._select_codes(layer, codes)
        return group

    def _on_export_csv(self):
        """Save the rows behind the mapped layers as one CSV.

        The rows, not the layer's own features: an aggregated layer holds one
        summarised value per location, and the question this answers is what
        that value was computed over. Every column the report returned is
        kept, so the export is recognisably the report it came from.

        All mapped analytes go into ONE file rather than one file each. They
        share the report's columns, so a single table pivots and filters
        without having to be reassembled first.
        """
        if self._analysis is None:
            self._set_status("Nothing has been fetched to export.",
                             Qgis.MessageLevel.Warning)
            return

        # What is on the map, in the order it was added. Falls back to the
        # selection when nothing is mapped yet, so the button is useful
        # before the first layer as well as after it.
        analytes = [entry["analyte"] for entry in self._exported_analytes()]
        if not analytes:
            self._set_status(
                "Select a chemical, or map one, before exporting.",
                Qgis.MessageLevel.Warning)
            return

        # Deliberately the table, not `_analysis`: this returns the REPORT's
        # own columns in the report's own order, which the store's canonical
        # 18 do not carry. The table is always resident when the store is -
        # that is the invariant on `_analysis` - so reading it here is free
        # and needs no second store. Settled as 1b.5 in
        # docs/implementation-plan.md; do not "optimise" this to `_analysis`.
        # A parquet source has no table, and its store CAN answer this - the
        # parquet carries the report's own 69 columns, so `raw` is a view over
        # the same files rather than a copy. Verified: 115,017 Arsenic rows
        # exported with every column. So: the table when there is one, else
        # the store. This line once read `_analysis` for both, which on the
        # live path is the background store once it lands - built without
        # the report's columns, so every export after the first seconds of a
        # report raised StoreError (found 2026-10-28; tools/test_offline.py
        # now exports again after the store lands). Both are row-scoped by
        # `_apply_row_scope`, so either honours the dates and the QC filter.
        source = (self._report_table if self._report_table is not None
                  else self._analysis)
        headers, rows = source.filtered_rows(
            analytes=analytes,
            matrix=self._matrix_mode(),
            fraction=self._fraction_mode(),
            method=self._method_mode(),
        )
        if not rows:
            self._set_status(
                "Nothing to export: no results match the current filters. %s"
                % self._why_empty([a["chemical"] for a in analytes]),
                Qgis.MessageLevel.Warning)
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export mapped results", self._export_default_path(),
            "CSV files (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"

        try:
            # newline="" per csv's own contract: without it every row gains a
            # blank line on Windows. utf-8-sig so Excel opens accented
            # chemical names correctly instead of as mojibake.
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(headers)
                writer.writerows(rows)
        except OSError as exc:
            self._set_status("Could not write %s (%s)."
                             % (os.path.basename(path), exc),
                             Qgis.MessageLevel.Critical)
            return

        self._set_status(
            "Exported %s row(s) for %s to %s."
            % (format(len(rows), ",d"),
               ", ".join(a["chemical"] for a in analytes[:3])
               + (" +%d more" % (len(analytes) - 3) if len(analytes) > 3
                  else ""),
               os.path.basename(path)),
            Qgis.MessageLevel.Success)

    def _exported_analytes(self):
        """What the export covers: the mapped layers, else the selection.

        Returned as the layer entries rather than bare analytes so the caller
        can tell "one layer per chemical" from "one chemical selected twice".
        """
        mapped = []
        seen = set()
        for key, layer in self._layer_views.items():
            if not self._layer_alive(layer):
                continue
            analyte = self._analyte_for_layer(layer)
            if not analyte:
                continue
            marker = _analyte_key(analyte)
            if marker in seen:
                continue
            seen.add(marker)
            mapped.append({"analyte": analyte, "layer": layer})
        if mapped:
            return mapped
        return [{"analyte": a, "layer": None}
                for a in self._selected_analytes()]

    def _export_default_path(self):
        """Where the save dialog opens, and what it suggests calling the file.

        Beside the project when there is one, so the export lands with the
        data it describes rather than in whatever directory QGIS was last
        pointed at.
        """
        facility = (self._current_facility() or {}).get("name") or "EQuIS"
        bits = [facility]
        matrix = self._matrix_mode()
        if matrix is not ALL_MATRICES:
            bits.append("+".join(matrix))
        name = " - ".join(bits) + " results.csv"
        # Strip what Windows will not accept in a filename.
        name = re.sub(r'[<>:"/\\|?*]+', "_", name)
        project = QgsProject.instance().fileName()
        folder = os.path.dirname(project) if project else ""
        return os.path.join(folder, name) if folder else name

    def _on_clear_layers(self):
        """Remove every layer this panel added, and the group holding them."""
        live = [layer for layer in self._layer_views.values()
                if self._layer_alive(layer)]
        if (self._preview_layer is not None
                and self._layer_alive(self._preview_layer)):
            live.append(self._preview_layer)
        if not live and self._group_name is None:
            self._set_status("No EQuIS layers to remove.")
            return
        self._stop_roll(restyle=False)
        for layer in live:
            if layer is not self._preview_layer:
                self._remove_layer(layer)
        self._drop_preview()
        self._layer_views = {}
        self._result_layers = []
        self._result_layer = None
        self._mapped_chemical = None
        self._active_aggregation = None
        self._release_pick_tool()
        self._remove_group()
        self._update_actions()
        self._update_active_label()
        self._set_status("Removed %d EQuIS layer(s)." % len(live))

    def _remove_group(self):
        """Drop our now-empty group from the layer tree."""
        if self._group_name is None:
            return
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(self._group_name)
        if group is not None and not group.findLayers():
            root.removeChildNode(group)
        self._group_name = None

    def _confirm_bulk_add(self, count):
        """Check before adding a lot of layers at once.

        Each one is a full pass over the report plus its own GeoPackage table,
        so a large selection is worth confirming rather than discovering.
        """
        if count <= BULK_LAYER_WARNING:
            return True
        answer = QMessageBox.question(
            self,
            "Add %d layers?" % count,
            "%d chemicals are selected, so %d layers will be added - each one "
            "a separate join over the report%s.\n\nContinue?"
            % (count, count,
               " and its own table in the GeoPackage"
               if self._storage_mode() == GEOPACKAGE else ""),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _why_empty(self, chemicals):
        """Why these chemicals produced nothing, in the user's terms.

        An empty layer used to be blamed on mismatched location codes, which
        is one cause and rarely the real one. Far more often the report
        genuinely holds no such result: the chemical picker offers every
        chemical the FACILITY permits - the parameter service returns the
        same list whatever matrix is asked for, so it cannot be narrowed -
        and a facility that permits xylenes may still have never measured it
        in tailings.

        Reading the fetched rows answers it properly: they can say whether
        the chemical is absent everywhere, or present but not in the matrix
        or fraction currently selected, which is the difference between
        "wrong chemical" and "wrong filter".
        """
        table = self._report_table
        if table is None or not chemicals:
            return ""
        matrix = self._matrix_mode()
        fraction = self._fraction_mode()
        if matrix is ALL_MATRICES and fraction is ANY_FRACTION:
            return ("Check that the report and the location layer use the "
                    "same codes.")

        elsewhere = []
        missing = []
        for chemical in chemicals:
            analyte = next((a for a in self._analytes
                            if a["chemical"] == chemical), None)
            cas = (analyte or {}).get("cas_rn")
            # One pass over the rows: which matrices carry this chemical at
            # all. Cheaper and clearer than aggregating once per matrix.
            where = sorted({
                table.cell(row, "matrix").upper()
                for row in table._candidate_rows(chemical, cas)
                if table._matches(row, chemical, cas)
                and table.cell(row, "matrix")
            })
            if not where and not any(
                    table._matches(row, chemical, cas)
                    for row in table._candidate_rows(chemical, cas)):
                missing.append(chemical)
                continue
            elsewhere.append((chemical, where))

        bits = []
        if missing:
            bits.append(
                "%s %s no results in this report at all."
                % (", ".join(missing), "has" if len(missing) == 1 else "have"))
        for chemical, where in elsewhere:
            scope = []
            if matrix is not ALL_MATRICES:
                shown = ("+".join(matrix)
                         if isinstance(matrix, (list, tuple, set, frozenset))
                         else matrix)
                scope.append("matrix %s" % shown)
            if fraction is not ANY_FRACTION:
                shown = ("+".join(fraction)
                         if isinstance(fraction, (list, tuple, set, frozenset))
                         else fraction)
                scope.append("fraction %s" % shown)
            said = "%s has no results in %s." % (chemical, " + ".join(scope))
            if where:
                said += " It does have results in %s." % ", ".join(where)
            bits.append(said)
        return " ".join(bits)

    def _describe_built(self, built, label, storage, gpkg_path, warnings, empty):
        """One status line covering however many layers were just added."""
        active = built[-1]["analyte"]["chemical"]
        if len(built) == 1:
            entry = built[0]
            total = entry["matched"] + entry["unmatched"]
            if entry.get("per_result"):
                # An unaggregated layer counts results, not locations, so
                # "at 37 of 123 locations" would be meaningless.
                message = ("%s: %s result(s) mapped."
                           % (label, format(entry["matched"], ",d")))
                if entry.get("unplaced"):
                    message += " " + _describe_unplaced(entry["unplaced"])
            else:
                message = ("%s at %s of %s location(s)."
                           % (label, format(entry["matched"], ",d"),
                              format(total, ",d")))
            if not entry["matched"]:
                if entry.get("unplaced"):
                    # Results exist; none of their locations are on the map.
                    codes = entry["unplaced"]
                    message = (
                        "%s has results at %s, but %s in the location layer, "
                        "so there is nothing to draw. The report and the "
                        "location layer are using different codes."
                        % (active, _name_codes(codes),
                           "none of those are"
                           if len(codes) > 1 else "it is not"))
                else:
                    message = ("Nothing to map for %s. %s"
                               % (active, self._why_empty([active]))).strip()
        else:
            message = ("Added %d layers (%s). %s is active."
                       % (len(built),
                          ", ".join(e["analyte"]["chemical"] for e in built),
                          active))
            if empty:
                message += (" Nothing to map for %s. %s"
                            % (", ".join(empty), self._why_empty(empty))).rstrip()
            # Pooled across the layers: the same missing location usually
            # affects every chemical, so naming it once per layer would say
            # the same thing several times. Said at all because this branch
            # used to drop it entirely - a multi-layer build could silently
            # lose results that a single-layer build would have reported.
            together = []
            for entry in built:
                for code in entry.get("unplaced") or []:
                    if code not in together:
                        together.append(code)
            if together:
                message += " " + _describe_unplaced(together)
        # Only when it IS active: an interactive preview deliberately does
        # not arm it, and the sentence claimed it had on every pick.
        try:
            armed = (self._map_tool is not None
                     and self.iface.mapCanvas().mapTool() is self._map_tool)
        except Exception:                       # noqa: BLE001
            armed = self._map_tool is not None
        if armed:
            message += (
                " The time series tool is active - click a point to plot "
                "it, or untoggle 'Time series tool' for the normal cursor."
            )
        if warnings:
            # The joins succeeded but could not be written; the layers work for
            # this session and will not survive the project closing.
            message += (
                " Could not save beside the project (%s), so %s a memory layer%s "
                "and will be empty when the project is reopened."
                % ("; ".join(sorted(set(warnings))[:2]),
                   "these are" if len(built) > 1 else "this is",
                   "s" if len(built) > 1 else "")
            )
        elif storage == GEOPACKAGE and gpkg_path:
            message += " Saved to %s." % os.path.basename(gpkg_path)
        elif self._storage_mode() == GEOPACKAGE:
            message += (
                " The project has not been saved yet, so %s memory layer%s; "
                "save the project and add again to keep %s."
                % ("these are" if len(built) > 1 else "this is a",
                   "s" if len(built) > 1 else "",
                   "them" if len(built) > 1 else "it")
            )
        return message

    # ------------------------------------------------------ 8. time series

    def set_pick_tool_active(self, active):
        """Arm or release the time-series map tool.

        Called from the toolbar action and the in-dialog toggle; both are kept
        in step here so the two controls can never disagree.
        """
        canvas = self.iface.mapCanvas()
        if active:
            if self._result_layer is None:
                self._sync_pick_controls(False)
                self._set_status(
                    "Add a chemical layer to the map first.",
                    Qgis.MessageLevel.Warning,
                )
                return
            self._fsm.enter_mode(st.PICKING, "pick tool armed")
            if self._map_tool is None:
                tool = LocationPickTool(canvas, self._result_layer,
                                        location_field=LOCATION_FIELD)
                tool.locationPicked.connect(self._on_location_picked)
                # QGIS swaps the tool out when the user picks another one;
                # follow that so our buttons untoggle with it.
                tool.deactivated.connect(self._on_tool_deactivated)
                # Held on the instance so Qt does not collect it while active.
                self._map_tool = tool
            else:
                self._map_tool.layer = self._result_layer
            canvas.setMapTool(self._map_tool)
        elif self._map_tool is not None and canvas.mapTool() is self._map_tool:
            canvas.unsetMapTool(self._map_tool)
        self._sync_pick_controls(active)

    def _on_tool_deactivated(self):
        """QGIS switched to a different map tool; reflect that in the UI."""
        self._sync_pick_controls(False)

    def _on_pick_button_toggled(self, checked):
        if self._syncing_pick:
            return
        self.set_pick_tool_active(checked)

    def _sync_pick_controls(self, checked):
        """Set both toggles without re-entering the handlers."""
        self._syncing_pick = True
        try:
            self.pick_btn.setChecked(checked)
            if self.pick_action is not None:
                self.pick_action.setChecked(checked)
        finally:
            self._syncing_pick = False

    # ------------------------------------------------------ sparkline markers

    # The slider works in tenths of a millimetre: QgsRangeSlider is integer
    # only, and whole millimetres are too coarse a step at the small end,
    # where one millimetre is the difference between a readable caption and a
    # smudge.
    SPARK_SIZE_STEPS = 10

    def _build_spark_size_row(self):
        """The two-handle slider bounding how far the markers scale.

        Markers are sized on the ground, so how big they get on screen is a
        function of zoom; these two millimetre clamps are what stops them
        vanishing when zoomed out and swamping the map when zoomed in. The
        defaults are what the plugin always used, so an untouched slider
        draws exactly what it drew before.
        """
        low, high = settings_store.load_spark_range()
        row = QHBoxLayout()
        row.addWidget(QLabel("Marker size:"))

        tip = (
            "How small and how large the sparkline markers may get as you\n"
            "zoom. The markers are sized on the ground, so zooming out\n"
            "shrinks them; these limits stop them disappearing when zoomed\n"
            "right out or covering each other when zoomed right in.\n\n"
            "Applies to sparkline layers already on the map as well as the\n"
            "next one added."
        )

        self.spark_size_slider = None
        self.spark_min_spin = None
        self.spark_max_spin = None
        if QgsRangeSlider is not None:
            self.spark_size_slider = QgsRangeSlider(Qt.Orientation.Horizontal)
            self.spark_size_slider.setMinimum(
                int(round(SPARK_LIMIT_MIN_MM * self.SPARK_SIZE_STEPS)))
            self.spark_size_slider.setMaximum(
                int(round(SPARK_LIMIT_MAX_MM * self.SPARK_SIZE_STEPS)))
            self.spark_size_slider.setRange(
                int(round(low * self.SPARK_SIZE_STEPS)),
                int(round(high * self.SPARK_SIZE_STEPS)))
            self.spark_size_slider.setToolTip(tip)
            self.spark_size_slider.rangeChanged.connect(
                self._on_spark_size_changed)
            row.addWidget(self.spark_size_slider, 1)
        else:
            # Older or trimmed QGIS builds have no range slider; two spin
            # boxes express the same pair and match the 'min pts' control
            # already in this panel.
            self.spark_min_spin = QSpinBox()
            self.spark_max_spin = QSpinBox()
            for spin, value, prefix in (
                (self.spark_min_spin, low, "min "),
                (self.spark_max_spin, high, "max "),
            ):
                spin.setRange(int(round(SPARK_LIMIT_MIN_MM)),
                              int(round(SPARK_LIMIT_MAX_MM)))
                spin.setValue(int(round(value)))
                spin.setPrefix(prefix)
                spin.setSuffix(" mm")
                spin.setToolTip(tip)
                spin.valueChanged.connect(self._on_spark_size_changed)
                row.addWidget(spin)
            row.addStretch(1)

        self.spark_size_label = QLabel("")
        self.spark_size_label.setStyleSheet("color: #5a6b7b;")
        row.addWidget(self.spark_size_label)
        self._update_spark_size_label(low, high)
        return row

    def _spark_size_range(self):
        """The (smallest, largest) millimetre pair the controls currently show."""
        if self.spark_size_slider is not None:
            return (self.spark_size_slider.lowerValue() / float(self.SPARK_SIZE_STEPS),
                    self.spark_size_slider.upperValue() / float(self.SPARK_SIZE_STEPS))
        low = float(self.spark_min_spin.value())
        high = float(self.spark_max_spin.value())
        # Two independent spin boxes can be crossed; the styling code
        # straightens the pair, and showing it straightened keeps the label
        # honest about what will be drawn.
        return (low, high) if low <= high else (high, low)

    def _update_spark_size_label(self, low, high):
        self.spark_size_label.setText("%g-%g mm" % (low, high))

    def _on_spark_size_changed(self, *args):
        """Remember the new range and redraw whatever it applies to.

        Restyling in place is right here where toggling the sparkline mode is
        not: the size range is a property of every sparkline layer at once,
        so changing it does not create a second view of a dataset or leave a
        layer's name describing symbology it no longer has.
        """
        low, high = self._spark_size_range()
        self._update_spark_size_label(low, high)
        settings_store.save_spark_range(low, high)
        if self._roll_timer.isActive():
            # Rolling owns the renderer, so its markers are resized by
            # rebuilding that renderer rather than by restyling each layer.
            self._restyle_roll_markers()
            return
        for key, layer in list(self._layer_views.items()):
            # Only the SVG-marker views carry a size range: sparklines, and
            # the depth profiles an unaggregated layer draws. Graduated
            # circles are sized by concentration and have nothing to resize.
            svg_view = (key[VIEW_SPARKLINES]
                        or key[VIEW_AGGREGATION] == UNAGGREGATED)
            if not svg_view or not self._layer_alive(layer):
                continue
            analyte = self._analyte_for_layer(layer)
            if analyte:
                self._apply_symbology(layer, analyte,
                                      how=key[VIEW_AGGREGATION])
                layer.triggerRepaint()

    def _restyle_roll_markers(self):
        """Re-apply the rolling renderer at the current size range.

        The marker columns are already written, so this only rebuilds the
        symbol and restores the frame being shown - no data is touched and
        the roll does not visibly restart.
        """
        layer = self._result_layer
        if not self._layer_alive(layer) or not self._roll_markers:
            return
        style_with_rolling_sparklines(
            layer, self._roll_markers, location_field=LOCATION_FIELD,
            size_range=self._spark_size_range(),
        )
        self._show_roll_frame()

    def _sparkline_mode(self):
        return self.sparkline_check.isChecked()

    def _apply_depth_profiles(self, layer, analyte):
        """Draw each location's results as a depth profile, where that fits.

        Only for locations with results at more than one depth: a single
        sample has no profile to show, and giving it one would imply a
        vertical structure the data does not have. Those keep the graduated
        marker, so a layer can carry both.

        Returns the number of locations given a profile - zero means the
        caller should style the whole layer the ordinary way.
        """
        if not self.profile_check.isChecked():
            return 0
        field_names = {layer.fields().at(i).name()
                       for i in range(layer.fields().count())}
        if "start_depth" not in field_names:
            return 0

        by_location = {}
        unit = ""
        for feature in layer.getFeatures():
            code = feature[LOCATION_FIELD]
            if not code:
                continue
            start = feature["start_depth"]
            end = feature["end_depth"]
            if start is None and end is None:
                continue
            value = feature["result_value"]
            if value is None:
                continue
            unit = unit or (feature["result_unit"] or "")
            by_location.setdefault(str(code), []).append({
                "start_depth": None if start is None else float(start),
                "end_depth": None if end is None else float(end),
                "value": float(value),
                "detect": feature["result_detect"],
                "at_limit": bool(feature["result_at_limit"]),
                # Drives one series per method on the profile. When more
                # than one fraction is on the layer the fraction joins the
                # series key, so dissolved and total read as two lines
                # rather than as scatter on one.
                # One series per sampling event: a hole re-sampled a year
                # later is a second profile of that hole, not more points on
                # the first, so the two must not be joined into one line.
                "date": (feature["result_date"]
                         if "result_date" in field_names else None),
                "method": (feature["method"]
                           if "method" in field_names else None),
                "fraction": (feature["result_fraction"]
                             if "result_fraction" in field_names else None),
            })
        if not by_location:
            return 0

        markers = embed_depth_profiles(
            by_location, analyte=(analyte or {}).get("chemical"), unit=unit)
        if not markers:
            return 0
        # The same SVG marker machinery the sparklines use, so the size
        # controls and the ground-unit scaling apply unchanged.
        # One profile per location, not per feature: this layer has a
        # feature per RESULT, and the profile already draws all of them.
        return style_with_sparklines(layer, markers,
                                     location_field=LOCATION_FIELD,
                                     size_range=self._spark_size_range(),
                                     once_per_location=True)

    def _apply_symbology(self, layer, analyte, how=None):
        """Style one layer per the current mode. Returns (drawn, too_few).

        Called both as layers are added and when the mode is toggled, so the
        two paths can never disagree about what a layer should look like.

        `how` is the aggregation the layer was built with; None means "ask
        the active one", which is what the restyle paths want since they are
        looking at a layer that already exists. An unaggregated layer never
        gets sparklines: a sparkline is itself an aggregation - one marker
        for a location's whole series - so drawing one per result row would
        paint the same marker on top of itself once per result.
        """
        if how is None:
            how = self._active_aggregation
        if how == UNAGGREGATED:
            # A boring sampled down its length is the one case where the
            # stacked features carry a shape worth drawing: depth turns an
            # unreadable pile of coincident markers into a profile.
            drawn = self._apply_depth_profiles(layer, analyte)
            if drawn:
                return drawn, 0
            style_by_result(layer)
            return 0, 0
        if not self._sparkline_mode() or self._analysis is None:
            style_by_result(layer)
            return 0, 0

        minimum = self.min_points_spin.value()
        mode = self._non_detect_mode()
        # ONE question for every location, not one per location. On the
        # DuckDB store a loop here costs 1.44 s against 52 ms for the same
        # work as a single grouped scan, because every query pays a fixed
        # parse-plan-execute cost however small it is. On ReportTable the two
        # are equivalent, so the call site is written once either way.
        codes = [str(f[LOCATION_FIELD]) for f in layer.getFeatures()
                 if f[LOCATION_FIELD]]
        every_series = self._analysis.series_many(
            codes, analyte["chemical"], analyte["cas_rn"],
            non_detect=mode, matrix=self._matrix_mode(),
            fraction=self._fraction_mode(),
            method=self._method_mode(),
        )
        series_by_location = {}
        too_few = 0
        for code in codes:
            points = every_series.get(code) or []
            if not points:
                continue
            if len(points) < minimum:
                # Too little history to be a trend; a plain marker is more
                # honest than a one-point "sparkline".
                too_few += 1
                continue
            series_by_location[code] = points

        # Embedded rather than written to temp files: a path into the temp
        # directory dangles as soon as the machine sweeps it, so a saved
        # project would reopen with the sparklines gone even though the layer
        # itself persisted.
        markers = embed_sparklines(series_by_location,
                                   analyte=analyte["chemical"])
        drawn = style_with_sparklines(layer, markers,
                                      location_field=LOCATION_FIELD,
                                      size_range=self._spark_size_range())
        if not drawn:
            # Leave the layer coherent rather than showing an empty sparkline
            # renderer.
            style_by_result(layer)
        return drawn, too_few

    def _on_sparkline_mode_changed(self, checked):
        """Choose how the next layer is drawn.

        Deliberately does not restyle what is already mapped. Two views of one
        dataset - the same chemical as circles and as sparklines - are now kept
        side by side, and converting the existing layer in place would both
        destroy one of those views and leave its name describing symbology it
        no longer has. Press 'Add layer to map' to create the other view.
        """
        # Rolling owns the renderer, and it is sparkline drawing by definition,
        # so switching to circles ends it.
        if self._roll_timer.isActive():
            self._stop_roll(restyle=False)
        if self._fsm.interactive:
            # Interactive mode has one view, so the symbology switch is a
            # change like any other: the preview is rebuilt in the new style.
            self._schedule_preview()
            return
        mapped = any(self._layer_alive(layer)
                     for layer in self._layer_views.values())
        drawn_as = ("time-series sparklines" if checked
                    else "circles graduated by concentration")
        if mapped:
            self._set_status(
                "Next layer will be drawn as %s. Press 'Add layer to map' to "
                "add it alongside what is already mapped." % drawn_as
            )
        else:
            self._set_status("Layers will be drawn as %s." % drawn_as)

    def _analyte_for_layer(self, layer):
        """Which chemical a mapped layer holds, read from the layer itself.

        The `analyte` column is written by build_joined_layer, so this survives
        the selection changing after the layer was added.
        """
        index = layer.fields().indexFromName("analyte")
        name = ""
        if index >= 0:
            for feature in layer.getFeatures():
                value = feature["analyte"]
                if value:
                    name = str(value)
                    break
        if not name:
            return None
        for analyte in self._analytes:
            if analyte["chemical"] == name:
                return analyte
        return {"chemical": name, "cas_rn": ""}

    # ---------------------------------------------------- rolling sparklines

    def _selected_analytes(self):
        """Every chemical selected, in list order.

        Read from the selection panel, which holds the single list: chemicals
        are chosen once, and that choice drives both what is fetched and what
        is mapped.
        """
        records = []
        for cas, name in self.selection_panel.selected_analytes():
            record = self._analyte_record(cas)
            if record is not None:
                records.append(record)
            elif name:
                # Selected before a fetch, so there is no record yet; the
                # name and CAS are enough to describe it.
                records.append({"chemical": name, "cas_rn": cas, "count": 0})
        return records

    def _on_roll_toggled(self, checked):
        if not checked:
            self._stop_roll()
            return
        if not self._start_roll():
            # Never leave the button latched on when nothing is rolling.
            self.roll_btn.blockSignals(True)
            self.roll_btn.setChecked(False)
            self.roll_btn.blockSignals(False)

    def _start_roll(self):
        """Build a marker column per selected analyte and start the timer."""
        analytes = self._selected_analytes()
        if self._result_layer is None or self._analysis is None:
            self._set_status("Add a chemical layer to the map first.",
                             Qgis.MessageLevel.Warning)
            return False
        if self._active_aggregation == UNAGGREGATED:
            # Rolling cycles sparkline markers, which an unaggregated layer
            # has no room for: its features are single results, not series.
            self._set_status(
                "Rolling draws a sparkline per location, so it needs an "
                "aggregated layer. Choose 'Most recent' or 'Maximum' and map "
                "again to roll between chemicals.",
                Qgis.MessageLevel.Warning,
            )
            return False
        if len(analytes) < 2:
            self._set_status(
                "Select two or more chemicals to roll between them - "
                "ctrl-click in the list above.",
                Qgis.MessageLevel.Warning,
            )
            return False

        mode = self._non_detect_mode()
        minimum = self.min_points_spin.value()
        codes = [
            f[LOCATION_FIELD] for f in self._result_layer.getFeatures()
            if f[LOCATION_FIELD]
        ]
        self._set_busy(True)
        self._set_status(
            "Rendering %s markers for %d chemicals..."
            % (format(len(codes), ",d"), len(analytes))
        )
        QgsApplication.processEvents()
        try:
            by_analyte = []
            for analyte in analytes:
                # One query per analyte rather than one per analyte per
                # location: this loop used to make len(codes) x len(analytes)
                # calls, which is the worst case the store's fixed per-query
                # cost punishes.
                every_series = self._analysis.series_many(
                    [str(code) for code in codes],
                    analyte["chemical"], analyte["cas_rn"],
                    non_detect=mode, matrix=self._matrix_mode(),
                    fraction=self._fraction_mode(),
                    method=self._method_mode(),
                )
                series_by_location = {}
                for code in codes:
                    points = every_series.get(str(code)) or []
                    if len(points) >= minimum:
                        series_by_location[str(code)] = points
                if not series_by_location:
                    continue
                by_analyte.append((
                    analyte["chemical"],
                    embed_sparklines(series_by_location,
                                     analyte=analyte["chemical"]),
                ))
            if len(by_analyte) < 2:
                self._restore_static_symbology()
                self._set_status(
                    "Only %d of the %d selected chemicals have locations with "
                    "%d or more results, so there is nothing to roll between. "
                    "Lower 'min pts' or select different chemicals."
                    % (len(by_analyte), len(analytes), minimum),
                    Qgis.MessageLevel.Warning,
                )
                return False
            self._roll_markers = by_analyte
            self._roll_columns = style_with_rolling_sparklines(
                self._result_layer, by_analyte, location_field=LOCATION_FIELD,
                size_range=self._spark_size_range(),
            )
            self._roll_analytes = [name for name, _ in by_analyte]
            # Rolling *is* sparkline drawing, so reflect it in the mode rather
            # than leaving the checkbox contradicting the map.
            if not self.sparkline_check.isChecked():
                self.sparkline_check.blockSignals(True)
                self.sparkline_check.setChecked(True)
                self.sparkline_check.blockSignals(False)
        finally:
            self._set_busy(False)

        if not self._roll_columns:
            self._set_status("Could not prepare the rolling markers.",
                             Qgis.MessageLevel.Warning)
            return False

        self._roll_index = 0
        self._show_roll_frame()
        # Rolling owns the renderer, so it is a mode rather than a
        # setting - the machine ends it if the layer goes away.
        self._fsm.enter_mode(st.ROLLING, "roll started")
        self._roll_timer.start(self.roll_interval_spin.value() * 1000)
        self._warn_if_roll_is_slow(len(codes))
        return True

    def _warn_if_roll_is_slow(self, marker_count):
        """Say so when the map cannot redraw as fast as the roll advances.

        Redraw cost is roughly linear in markers drawn - about 200 ms at 300
        and 2.4 s at 3,000 - so a whole facility can take longer to draw than
        the interval, and the roll silently falls behind. Zooming in fixes it,
        because QGIS only draws what is in the viewport.
        """
        interval = self.roll_interval_spin.value()
        # Derived from measurements on this renderer; deliberately rough,
        # since it only decides whether to warn.
        estimate = marker_count * 0.0008
        if estimate <= interval:
            return
        self._set_status(
            "Rolling %d chemicals across %s markers. Redrawing that many takes "
            "roughly %.0f s, longer than the %d s interval, so the roll will "
            "lag - zoom in, or raise 'min pts', to draw fewer at once."
            % (len(self._roll_analytes), format(marker_count, ",d"),
               estimate, interval),
            Qgis.MessageLevel.Warning,
        )

    def _show_roll_frame(self):
        """Point the renderer at the current analyte and say which it is."""
        if not self._roll_columns or self._result_layer is None:
            return
        index = self._roll_index % len(self._roll_columns)
        set_roll_column(self._result_layer, self._roll_columns[index])
        # The marker caption names the chemical, but the panel says it too:
        # the map may be scrolled away from any marker.
        self.roll_label.setText(
            "Showing <b>%s</b>  (%d of %d)"
            % (self._roll_analytes[index], index + 1, len(self._roll_analytes))
        )

    def _advance_roll(self):
        """Timer tick: move to the next analyte.

        Guards against the layer having gone away while the timer was armed,
        which happens when the user removes it from the project.
        """
        if self._result_layer is None or not self._layer_alive(self._result_layer):
            self._stop_roll()
            return
        self._roll_index += 1
        self._show_roll_frame()

    def _on_roll_interval_changed(self, value):
        if self._roll_timer.isActive():
            self._roll_timer.start(value * 1000)

    def _stop_roll(self, restyle=True):
        """Stop the timer and put the map back to a static symbology."""
        was_running = self._roll_timer.isActive()
        if was_running:
            self._fsm.enter_mode(st.IDLE, "roll stopped")
        self._roll_timer.stop()
        self._roll_columns = []
        self._roll_analytes = []
        self._roll_markers = []
        self._roll_index = 0
        self.roll_label.setText("")
        if self.roll_btn.isChecked():
            self.roll_btn.blockSignals(True)
            self.roll_btn.setChecked(False)
            self.roll_btn.blockSignals(False)
        if (was_running and restyle and self._result_layer is not None
                and self._layer_alive(self._result_layer)):
            # Leave the map in a coherent state rather than frozen on
            # whichever analyte happened to be showing.
            self._restore_static_symbology()

    def _restore_static_symbology(self):
        """Put the active layer back to whatever the mode asks for.

        After a roll ends the layer is left on one analyte's marker column, so
        it has to be restyled - to sparklines or to circles depending on the
        checkbox, not unconditionally to circles.
        """
        layer = self._result_layer
        if layer is None or not self._layer_alive(layer):
            return
        analyte = self._analyte_for_layer(layer) or self._current_analyte()
        if analyte is None:
            style_by_result(layer)
            return
        self._apply_symbology(layer, analyte)


    def _advance_to(self, done_box, next_box=None):
        """Fold a finished step away and open the one that follows.

        Keeps the panel short as the user works down it, which matters because
        the plot shares the same column.
        """
        if done_box is not None:
            done_box.setCollapsed(True)
        if next_box is not None:
            next_box.setCollapsed(False)

    def _non_detect_mode(self):
        return self.non_detect_combo.currentData() or ND_HALF

    def _on_non_detect_changed(self):
        """Redraw whatever is showing under the new non-detect convention."""
        if self._picked_location and self._report_table is not None:
            self._on_location_picked(self._picked_location)
        if self._fsm.interactive:
            self._schedule_preview()
            return
        if self._result_layer is not None and self._mapped_chemical:
            self._set_status(
                "Non-detect handling changed. Press 'Add layer to map' to "
                "rebuild the layer with it.",
                Qgis.MessageLevel.Info,
            )

    # ------------------------------------------------- equis:// link actions
    # The surface url_actions.py drives. Public on purpose: a link handler
    # reaching into private methods would couple it to this file's internals,
    # and these four are what a link can ask for.

    def select_analyte(self, analyte, cas_rn=None):
        """Choose one chemical by name or CAS, as if it had been clicked."""
        wanted = [cas_rn] if cas_rn else []
        if analyte:
            wanted.append(analyte)
        picked = self.selection_panel.set_selected_analytes(wanted)
        if not picked:
            self.note_link_problem(
                "%r is not among the chemicals loaded." % (analyte or cas_rn))
        return bool(picked)

    def apply_scope(self, matrix=None, fraction=None, method=None):
        """Set the matrix/fraction/method filters a link named.

        Each is a single code from a link, but the pickers are multi-select,
        so each becomes a one-item selection. Anything not named is left
        alone - a link that mentions only a matrix must not silently widen
        the fraction the user had chosen.
        """
        if matrix:
            self.selection_panel.set_selected_matrices([matrix])
        if fraction:
            self.selection_panel.set_selected_fractions([fraction])
        if method and hasattr(self.selection_panel, "set_selected_methods"):
            self.selection_panel.set_selected_methods([method])

    def select_location(self, code, add=False):
        """Select one location on the map, optionally adding to what is there.

        `add` is what makes "compare these three wells" work from the
        identify panel: each click adds rather than replacing, and the
        panel's existing "only locations selected on the map" filter then
        scopes everything to them.
        """
        layer = self._locations_layer
        if layer is None:
            self.note_link_problem("There is no locations layer to select on.")
            return False
        matched = [feature.id() for feature in layer.getFeatures()
                   if str(feature[LOCATION_FIELD] or "").strip().lower()
                   == str(code or "").strip().lower()]
        if not matched:
            self.note_link_problem("%r is not on the map." % code)
            return False
        if add:
            layer.selectByIds(matched, Qgis.SelectBehavior.AddToSelection)
        else:
            layer.selectByIds(matched)
        return True

    def plot_location(self, code):
        """Plot one location, exactly as clicking it on the map does.

        Straight to _on_location_picked rather than a second implementation:
        it already handles no-analyte, no-data and raising the dock, and a
        link must not reach a state a click cannot.
        """
        self._on_location_picked(code)

    def map_selected(self, how=None):
        """Build the layer for whatever is selected, as the button does."""
        if how:
            index = self.aggregation_combo.findData(how)
            if index >= 0:
                self.aggregation_combo.setCurrentIndex(index)
        self._on_build_layer()

    def note_link_problem(self, message):
        """Say why a link did nothing.

        A link that silently fails is worse than one that errors: the user
        clicked something and has no way to tell whether it worked.
        """
        self._set_status(message, Qgis.MessageLevel.Warning)
        QgsMessageLog.logMessage(message, "EQuIS", Qgis.MessageLevel.Warning)

    def _scope_label(self, scope=None):
        """The filters a plot was drawn under, in the layer names' own words.

        Matrix, fraction and method, joined with "+" for several and ", "
        between them - the same vocabulary _on_map_chemical builds layer
        names from, so a chart and the layer it came from describe
        themselves identically rather than in two dialects.

        The non-detect convention is named too when it is not the default:
        half the detection limit, the full limit and excluding them
        entirely give visibly different series from identical data, and the
        chart cannot show which was used.

        Empty when nothing is filtered, so an unfiltered chart carries no
        subtitle rather than a row of "all".
        """
        # `scope` describes a LAYER when one is given, and the panel's own
        # pickers otherwise. A chart of a layer must be captioned with what
        # that layer holds, not with what the picker has moved on to.
        scope = scope or {
            "matrix": self._matrix_mode(),
            "fraction": self._fraction_mode(),
            "method": self._method_mode(),
            "non_detect": self._non_detect_mode(),
        }
        parts = []
        for key in ("matrix", "fraction", "method"):
            values = scope.get(key)
            if values:
                parts.append("+".join(values))
        mode = scope.get("non_detect") or ND_HALF
        if mode != ND_HALF:
            parts.append(dict(ND_HANDLING).get(mode, str(mode)))
        return " · ".join(parts)

    # ------------------------------------------------- what a layer shows
    # A layer records the selection it was built from, so anything acting on
    # it later - the time series, an equis:// link, a future chart - asks the
    # LAYER rather than the panel. The panel's pickers move on.

    SCOPE_KEYS = ("chemical", "cas_rn", "matrix", "fraction", "method",
                  "non_detect", "aggregation")

    def _stamp_scope(self, layer, analyte, how):
        """Record on `layer` what it was built from."""
        if layer is None:
            return
        def joined(values):
            return "|".join(values) if values else ""
        scope = {
            "chemical": analyte.get("chemical") or "",
            "cas_rn": analyte.get("cas_rn") or "",
            "matrix": joined(self._matrix_mode()),
            "fraction": joined(self._fraction_mode()),
            "method": joined(self._method_mode()),
            "non_detect": self._non_detect_mode() or "",
            "aggregation": how or "",
        }
        for key, value in scope.items():
            layer.setCustomProperty("equis/%s" % key, value)

    def _layer_scope(self, layer):
        """What `layer` was built from, or None if this plugin did not build it.

        `chemical` is the marker: a layer without it is someone else's, and
        acting on it would be guessing.
        """
        if layer is None:
            return None
        chemical = layer.customProperty("equis/chemical", "")
        if not chemical:
            return None
        def split(key):
            raw = layer.customProperty("equis/%s" % key, "")
            return [part for part in str(raw).split("|") if part] or None
        return {
            "chemical": chemical,
            "cas_rn": layer.customProperty("equis/cas_rn", "") or None,
            "matrix": split("matrix"),
            "fraction": split("fraction"),
            "method": split("method"),
            "non_detect": layer.customProperty("equis/non_detect", "")
                          or self._non_detect_mode(),
            "aggregation": layer.customProperty("equis/aggregation", ""),
        }

    def _active_scope(self):
        """The scope of the layer the user has selected in the layer tree.

        Falls back to the panel's own pickers when the active layer is not
        one of ours - a basemap, or nothing selected at all - so clicking
        still does something sensible rather than refusing.
        """
        try:
            active = self.iface.activeLayer()
        except Exception:                       # noqa: BLE001
            active = None
        scope = self._layer_scope(active)
        if scope is not None:
            return scope, True
        analyte = self._current_analyte()
        if analyte is None:
            return None, False
        return {
            "chemical": analyte.get("chemical"),
            "cas_rn": analyte.get("cas_rn"),
            "matrix": self._matrix_mode(),
            "fraction": self._fraction_mode(),
            "method": self._method_mode(),
            "non_detect": self._non_detect_mode(),
            "aggregation": self._active_aggregation or "",
        }, False

    def _on_location_picked(self, code):
        # THE ACTIVE LAYER DECIDES, not the panel's picker. Clicking a point
        # on a Manganese layer used to plot whatever the picker said, because
        # this read _current_analyte(); the picker moves on after a layer is
        # built and the layer does not. Identify works the way the user
        # expects here - the highlighted layer is the subject - so this does
        # too, falling back to the picker when the active layer is not ours.
        scope, from_layer = self._active_scope()
        if scope is None or self._analysis is None:
            # Clicking should never be a no-op with no explanation: this
            # happens if the report was reloaded after the layer was built.
            self._set_status(
                "Select a chemical, or a mapped layer, before clicking a "
                "location.", Qgis.MessageLevel.Warning,
            )
            self.iface.messageBar().pushMessage(
                "EQuIS", "Select a chemical, or a mapped layer, before "
                "clicking a location.",
                level=Qgis.MessageLevel.Warning, duration=5,
            )
            return
        self._picked_location = code
        points = self._analysis.series(
            code, scope["chemical"], scope["cas_rn"],
            non_detect=scope["non_detect"],
            matrix=scope["matrix"],
            fraction=scope["fraction"],
            method=scope["method"],
        )
        # The plot has its own dock now, so clicking a location has to bring
        # it up: it starts hidden to keep an empty chart out of the way, and
        # a click that drew a plot nobody can see would look like a click
        # that did nothing. Only shown - never re-docked or resized, so
        # wherever the user put it is where it stays.
        self.plot_dock.plot_series(code, scope["chemical"], points,
                                   scope=self._scope_label(scope))
        if not self.plot_dock.isVisible():
            self.plot_dock.show()
        self.plot_dock.raise_()

    # ---------------------------------------------------------------- misc

    def _reset_report_state(self):
        """Drop everything tied to a facility or report.

        Also releases the map tool: once the report is gone, clicking a point
        cannot plot anything, so leaving the cursor armed would be misleading.
        """
        self._location_pages = None
        self._has_geometry = False
        self._report_table = None
        # The store describes exactly these rows, so it cannot outlive them.
        self._discard_store()
        self._analytes = []
        self._matrices = []
        # What the outgoing facility held, which says nothing about the next.
        self._offered_analytes = []
        self._fetched_selection = None
        # The DataSelection those rows were fetched for, kept whole so a
        # layer's provenance can describe the request that produced its rows
        # rather than whatever the controls say at the moment of mapping.
        self._fetched_selection_obj = None
        # Scoped by its own @facility_id, so it must not survive a facility
        # change: reusing it would fetch the previous site's data.
        self._live_report = None
        self._active_report = None
        self._active_aggregation = None
        self._mapped_chemical = None
        self._picked_location = None
        self._report_source = None
        self._report_entry = None
        self._locations_source = None
        self.selection_panel.clear()
        self.report_combo.clear()
        # Cleared AND hidden: the chart described the outgoing facility, and
        # leaving it on screen would attach the last location's history to
        # whatever is loaded next.
        self.plot_dock.clear(
            "Run a report and pick a chemical to see time series.")
        self.plot_dock.hide()
        self._release_pick_tool()
        # The layers describe the outgoing facility/report, so they go too;
        # leaving them behind is what made it ambiguous which was "active".
        for layer in list(self._layer_views.values()):
            self._remove_layer(layer)
        for layer in self._result_layers or []:
            self._remove_layer(layer)
        self._remove_layer(self._result_layer)
        self._remove_layer(self._locations_layer)
        self._layer_views = {}
        self._result_layers = []
        self._result_layer = None
        self._locations_layer = None
        self._remove_group()
        self._update_actions()
        self._update_active_label()

    def _release_pick_tool(self):
        """Unset our map tool and disable both toggles."""
        if self._map_tool is not None:
            canvas = self.iface.mapCanvas()
            if canvas.mapTool() is self._map_tool:
                canvas.unsetMapTool(self._map_tool)
            self._map_tool = None
        self._sync_pick_controls(False)
        # The roll drives the layer that is going away, so it stops with it.
        self._stop_roll(restyle=False)
        self._fsm.enter_mode(st.IDLE, "pick tool released")
        # The layer is gone, so the capabilities already withdraw the pick
        # tool and the roll.
        self._update_actions()

    def _zoom_to(self, layer):
        if layer.featureCount() and not layer.extent().isEmpty():
            canvas = self.iface.mapCanvas()
            canvas.setExtent(
                canvas.mapSettings().layerExtentToOutputExtent(layer, layer.extent())
            )
            canvas.refresh()

    def _start(self, task, replace=False):
        """Run one background task, refusing to run two at once.

        There is no state machine here, and this is the one place that
        matters: `self._task` is the whole notion of "something is running",
        and it used to be overwritten by whoever started next. The first task
        was not cancelled - it stayed on the task manager, finished later,
        and fired its callback into a dialog that had moved on, while the
        progress bar showed whichever task most recently set it. A fetch and
        a report re-run could both be in flight against one panel.

        So a second start is refused rather than silently queued. `replace`
        is for the deliberate case - the user asking for a different fetch -
        which cancels the running task first so exactly one is ever live.

        Returns True when the task was started.
        """
        if self._is_busy():
            if not replace:
                self._set_status(
                    "Already working on something - wait for it to finish, "
                    "or press Cancel.", Qgis.MessageLevel.Warning)
                return False
            # Remembered by identity so its late callback can be ignored:
            # a cancelled task still runs to completion and still emits.
            self._superseded.add(id(self._task))
            self._task.cancel()
            self._task = None
        self._task = task
        # What _start actually handed to the task manager. `_task` alone is
        # not enough: a task constructed and never queued reports the same
        # Queued status as one genuinely waiting to run, so results delivered
        # directly - the offline path, and the tests - would look like they
        # belonged to something in flight.
        self._queued_task = task
        QgsApplication.taskManager().addTask(task)
        return True

    @staticmethod
    def _task_finished(task):
        """Whether a task is done, whatever the QGIS build calls it.

        A cancelled or finished task still sits in `self._task` until its
        callback clears it, and treating that as "busy" would lock the panel
        out of ever starting another.

        Note this cannot tell "queued, about to run" from "constructed and
        never handed to the task manager" - both report Queued - which is
        why _is_busy also requires that _start actually queued this task.
        """
        try:
            status = task.status()
        except Exception:  # noqa: BLE001 - a stub task in the tests
            return False
        return status in (QgsTask.TaskStatus.Complete,
                          QgsTask.TaskStatus.Terminated)

    def _is_busy(self):
        """Whether a background task is actually in flight."""
        return (self._task is not None
                and self._task is self._queued_task
                and not self._task_finished(self._task))

    def _stale(self, task):
        """Whether this callback belongs to a task the panel has moved on from.

        A cancelled or superseded task still runs to completion and still
        emits, so every callback has to ask whether it is still the one being
        waited for. Without this a replaced fetch could overwrite the results
        of the fetch that replaced it.

        Only a task this panel actually cancelled is stale. Inferring it
        from "some other task is in _task" was wrong twice over: a task that
        is queued but not yet running is indistinguishable from a stalled
        one, and results handed straight to a callback - the offline path,
        and the tests - were never in _task at all. Both cases looked stale
        and had their rows silently discarded.

        So supersession is RECORDED at the point it happens, in _start, and
        nowhere else has to guess.
        """
        return id(task) in self._superseded

    def _on_progress(self, value):
        if self.progress.maximum() != 0:
            self.progress.setValue(int(value))

    def _on_task_failed(self, message):
        self._task = None
        # Whatever failed, the panel must land somewhere with controls rather
        # than staying in the state the task was driving.
        if self._fsm.fetching:
            self._enter(st.LOADED if self._report_table is not None
                        else st.PREPARED, "task failed")
        self._set_busy(False)
        self._set_status(message, Qgis.MessageLevel.Critical)

    def _cancel_task(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None
        # A cancelled fetch leaves the panel in FETCHING otherwise, which has
        # no controls of its own - the user would be stuck watching a stopped
        # progress bar.
        if self._fsm.fetching:
            self._enter(st.LOADED if self._report_table is not None
                        else st.PREPARED, "fetch cancelled")
        self._set_busy(False)
        self._set_status("Cancelled.", Qgis.MessageLevel.Warning)

    def reopen(self):
        """Bring the dialog back showing whatever session is already active.

        Reloading a report costs minutes and the active one describes what is
        already on the map, so reopening restores rather than restarts. Only
        connect automatically when there is nothing to come back to.
        """
        # `_analysis` for the same reason as _auto_connect: a parquet session
        # has rows and no ReportTable, and must be restored, not replaced.
        if self._analysis is not None or self._facilities:
            self._describe_session()
            # The map tool follows the layer, and the layer may have been
            # removed from the project while the dialog was closed.
            # Any of our layers may have been deleted while we were closed.
            self._layer_views = {
                key: layer for key, layer in self._layer_views.items()
                if self._layer_alive(layer)
            }
            self._result_layers = [
                layer for layer in self._result_layers
                if self._layer_alive(layer)
            ]
            if self._result_layer is not None and not self._layer_alive(self._result_layer):
                # The active layer is gone. Fall back to another of ours if one
                # survived, so the tool and the sparkline buttons keep working.
                self._result_layer = (self._result_layers[-1]
                                      if self._result_layers else None)
                if self._result_layer is None:
                    self._active_aggregation = None
                    self._mapped_chemical = None
                    self._release_pick_tool()
                elif self._map_tool is not None:
                    self._map_tool.layer = self._result_layer
                self._update_active_label()
            # Whatever survived the close is reflected by the capabilities;
            # this used to re-enable the pick button on its own and could
            # arm it for a layer that had been removed.
            self._update_actions()
            return
        QTimer.singleShot(0, self._auto_connect)

    def _remove_layer(self, layer):
        """Drop a layer we previously added, if it is still in the project."""
        if layer is None:
            return
        if self._map_tool is not None and getattr(self._map_tool, "layer", None) is layer:
            self._map_tool.layer = None
        if self._layer_alive(layer):
            QgsProject.instance().removeMapLayer(layer.id())

    @staticmethod
    def _layer_alive(layer):
        """True while the layer is still in the project (not deleted by user)."""
        try:
            return QgsProject.instance().mapLayer(layer.id()) is not None
        except RuntimeError:
            # The underlying C++ object is already gone.
            return False

    def _update_active_label(self):
        """Refresh the banner, and the overlay's Data panel that repeats it."""
        self._update_active_banner()
        self._push_overlay_status()

    def _update_active_banner(self):
        """Refresh the banner describing the one active facility/report."""
        facility = self._current_facility()
        cached_session = self._offline or self._report_source in (
            CACHE, CACHE_FALLBACK
        )
        # Recolouring the whole banner is the point: a "(cached)" suffix is
        # easy to read past, a panel that changes colour is not.
        self.active_label.setStyleSheet(
            BANNER_CACHED if cached_session else BANNER_LIVE
        )
        # SHOWN ONLY TO WARN (asked 2026-10-12: the routine summary cost
        # more room than it earned). It still appears for what must never
        # be read past - cached or offline data (CLAUDE.md: marked
        # everywhere it appears) and a report a row limit cut short.
        truncated = (self._report_entry is not None
                     and getattr(self._report_entry, "truncated", False)
                     and self._active_report is not None)
        self.active_label.setVisible(bool(cached_session or truncated))
        if facility is None:
            self.active_label.setText(
                "<b>⚠ Offline</b> — select a cached facility."
                if self._offline else "Nothing loaded yet."
            )
            return

        lines = []
        if self._offline:
            lines.append(
                "<b style='color:#8a6d3b'>⚠ Offline — showing cached data</b>"
            )
        lines.append("<b>Facility:</b> %s%s" % (
            facility["name"],
            " <span style='color:#8a6d3b'>(cached)</span>" if self._offline else "",
        ))
        if self._active_report is not None:
            rows = self._row_count()
            note = ""
            if self._report_source in (CACHE, CACHE_FALLBACK):
                note = " <span style='color:#8a6d3b'>cached %s</span>" % (
                    self._report_entry.age_text if self._report_entry else "earlier"
                )
            lines.append(
                "<b>Report:</b> %s <span style='color:#5a6b7b'>(%s rows, "
                "%d chemicals)</span>%s"
                % (self._active_report["name"], format(rows, ",d"),
                   len(self._analytes), note)
            )
            if self._report_entry is not None and self._report_entry.truncated:
                lines.append(
                    "<b style='color:#b36b00'>Partial report — a row limit cut "
                    "it short, so later sample dates are missing.</b>"
                )
        elif self._using_own_report():
            # The report is already prepared and named above; what is missing
            # is the fetch, not a choice of report.
            ready = bool(self.selection_panel.selected_analytes())
            lines.append(
                "<span style='color:#5a6b7b'>Nothing fetched yet — choose "
                "chemicals in step 4, then press <b>Pre-fetch data</b>.</span>"
                if not ready else
                "<span style='color:#5a6b7b'>Ready to fetch: %s. Press "
                "<b>Pre-fetch data</b>.</span>"
                % self._current_selection(facility.get("id")).describe()
            )
        elif self._parquet_url is not None:
            # There is no report here and none to pick. Say what IS loaded.
            ready = bool(self.selection_panel.selected_analytes())
            lines.append(
                "<span style='color:#5a6b7b'>%s rows from the published "
                "parquet. %s</span>"
                % (format(self._row_count(), ",d"),
                   "Choose a chemical to map."
                   if not ready else "Press <b>Map chemical</b>."))
        else:
            lines.append("<span style='color:#8a6d3b'>No report loaded — pick "
                         "one and press Run report.</span>")

        analyte = self._current_analyte()
        if self._result_layer is not None and self._active_aggregation:
            label = dict(AGGREGATIONS).get(self._active_aggregation, "")
            lines.append(
                "<b>Mapped:</b> %s <span style='color:#5a6b7b'>(%s)</span>"
                % (self._mapped_chemical or "", label.lower())
            )
            # Read back from the layer rather than recomputed, so the banner
            # shows what that layer IS, not what the panel would build now.
            try:
                abstract = self._result_layer.metadata().abstract()
            except Exception:  # noqa: BLE001 - a dead layer has no metadata
                abstract = ""
            if abstract:
                lines.append(
                    "<span style='color:#5a6b7b'><b>Last layer:</b> %s</span>"
                    % abstract)
        elif analyte is not None:
            lines.append(
                "<span style='color:#5a6b7b'>Selected: %s — press 'Add layer "
                "to map'.</span>" % analyte["chemical"]
            )
        self.active_label.setText("<br>".join(lines))

    def _describe_session(self):
        """Restate what is currently active, for someone returning later."""
        facility = self._current_facility()
        if facility is None:
            self._set_status("Connected. Select a facility.")
            return
        parts = ["Facility: %s" % facility["name"]]
        if self._active_report:
            parts.append("Report: %s" % self._active_report["name"])
        if self._result_layer is not None and self._active_aggregation:
            label = dict(AGGREGATIONS).get(self._active_aggregation, "")
            parts.append("Mapped: %s (%s)"
                         % (self._mapped_chemical or "", label.lower()))
        elif self._report_table is not None:
            parts.append("Select a chemical to map.")
        self._set_status("   |   ".join(parts))

    def closeEvent(self, event):
        # Hiding, not tearing down: the session stays live so reopening shows
        # the active facility/report instead of a blank dialog.
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._map_tool is not None:
            # Leave the user with a working cursor, not our tool.
            self.iface.mapCanvas().unsetMapTool(self._map_tool)
        self._sync_pick_controls(False)
        # A hidden panel that keeps repainting the map every few seconds is
        # both baffling and expensive, so the roll stops with the dialog.
        self._stop_roll()
        # A Chromium view left running behind a hidden panel keeps a process
        # and a channel alive for nothing.
        self._close_linked_views()
        if self.pick_action is not None:
            self.pick_action.setEnabled(False)
        super().closeEvent(event)

    def shutdown(self):
        """Release everything; called when the plugin itself is unloaded."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
        # An open DuckDB file handle would outlive the plugin otherwise, and
        # on Windows that is the difference between the next session
        # rebuilding cleanly and failing to unlink the file.
        # Before the store: the web view queries it, and a page mid-query
        # against a closed connection is a crash rather than an error.
        self._close_linked_views()
        self._discard_store()
        self._release_pick_tool()
