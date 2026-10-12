"""The "Data selection" step: what subset of a facility's chemistry to fetch.

The same controls serve both data sources, but they mean different things and
the panel says which:

  live  - the values are saved onto a report before it runs, so they decide
          what EQuIS generates. This is what keeps a large facility inside the
          ~230 s ceiling.
  agent - an information agent's stored output is frozen from when it ran and
          cannot be re-scoped, so the values filter rows after they arrive.

Kept out of dialog.py, which is long enough already.
"""

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .data_selection import (
    DataSelection,
    FRACTIONS,
    QC_SAMPLE_TYPES,
    SAMPLE_TYPE_NORMAL,
)

# What the panel calls the two sources, and what it explains about each.
LIVE = "live"
AGENT = "agent"
# A published parquet set: every row is already here, so nothing below is
# sent anywhere. Distinct from AGENT, which is also frozen but is one
# report's stored output rather than the whole facility.
PARQUET = "parquet"

SOURCE_HINTS = {
    LIVE: (
        "These are saved onto your own EQuIS report, so the server returns "
        "only these rows - narrowing here is what keeps a large facility "
        "inside the time limit. Everything below the fetch filters the rows "
        "it returns, and costs nothing to change."
    ),
    AGENT: (
        "An information agent's stored output is frozen from when it last ran "
        "and cannot be re-scoped, so these filters are applied after the data "
        "arrives. Use this when the report is too large to run live."
    ),
    PARQUET: (
        "Every result for this facility is already here, so nothing below is "
        "sent anywhere - each filter re-scopes rows in hand and takes effect "
        "at once. There is no fetch and no time limit to stay inside."
    ),
}


FRACTION_TOOLTIP = (
    "Dissolved, total, or N where the distinction does not apply.\n"
    "Tick nothing for any of them.\n\n"
    "Water typically carries D and T, sediment only N. Ticking\n"
    "several draws a line per fraction on a depth profile; an\n"
    "aggregate across them is refused, since a maximum would report\n"
    "whichever fraction happened to be higher."
)

METHOD_TOOLTIP = (
    "Laboratory analytic method.\n"
    "One sample is often analysed by more than one, and they can\n"
    "disagree about whether the analyte was detected at all - so\n"
    "two results at one depth are usually two methods, not duplicates.\n\n"
    "Tick nothing for all of them. A depth profile draws one line\n"
    "per method, so several can be compared at once."
)

MATRIX_TOOLTIP = (
    "Sample matrix. Aggregating across matrices compares\n"
    "concentrations that are not comparable."
)


# The flags for a row in the checkable combos (location group, matrix,
# method, fraction). ENABLED, NOT USER-CHECKABLE, deliberately. The tick is
# still drawn - the check state is set on every item - but Qt must not
# toggle it. Each combo's `pressed` handler toggles the row and tells the
# panel. With ItemIsUserCheckable as well there were two authorities: a
# click on the CHECKBOX was consumed by Qt's own toggle, the handler never
# ran, and the panel was never told. Clicking the text worked, so the bug
# looked like a delay. Reported 2026-10-01 from interactive mode, where a
# matrix ticked by its box did not redraw until the next chemical was picked.
TICKABLE_ROW = Qt.ItemFlag.ItemIsEnabled


class SelectionPanel(QWidget):
    """Pickers for analyte, matrix, fraction, sample type and date."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._analytes = []      # (cas_rn, chemical_name)
        self._source = LIVE
        # Whether the matrix combo offers a real choice. False until a report
        # says otherwise, so selection() cannot report a filter that is not
        # on screen.
        self._matrix_useful = False
        # Every matrix the facility has offered so far, in offer order. Held
        # because the offer arrives from two places - the report's
        # @matrix_codes, and whatever the fetched rows turn out to contain -
        # and the second is a SUBSET of the first. Replacing rather than
        # merging let a fetch of one matrix collapse the combo below the
        # "more than one choice" threshold, hiding it with no way back to
        # "All matrices" short of re-preparing the report.
        self._matrix_offer = []
        # Location groups the facility defines, from the parameter service.
        # Replaced, not merged: they are facility-level and arrive once, at
        # prepare time, and a fetch does not report them.
        self._group_offer = []
        # Every analytic method the facility offers, in offer order. Merged
        # for the same reason the matrix offer is: it arrives both from the
        # parameter service (all of them) and from fetched rows (a subset).
        self._method_offer = []
        # The subset the rows in hand actually carried, upper-cased. Empty
        # until a fetch says otherwise, which is why nothing is annotated
        # before one - "none in this data" against an unfetched report would
        # be a claim the panel cannot yet make.
        self._methods_present = set()
        # Whether a fetch has said which fractions exist. Until it has, the
        # fraction row stays hidden: D/T/N are a property of the analysis,
        # and offering them against unknown data invites a filter that
        # silently returns nothing (sediment carries only N, so a dissolved
        # tick empties a sediment map).
        self._fractions_known = False
        # Chosen CAS numbers, held here rather than read off the widget: the
        # filter box hides rows, and a hidden row is still chosen. Reading the
        # widget alone would silently drop it from the query.
        self._chosen = set()
        # Why the pickers are empty, when they are. Cleared as soon as real
        # values arrive.
        self._unavailable = ""
        # Whether that state is "working on it" rather than "cannot".
        self._busy = False
        # Result counts per CAS, known only after a fetch.
        self._counts = {}

        # Two sections, so the dialog can put them in different steps. What
        # separates them is not tidiness but WHEN each takes effect: the
        # prefetch controls decide which rows EQuIS generates and are spent
        # the moment the fetch runs, while the filter controls re-scope rows
        # already in hand and can be changed freely afterwards. Mixing them
        # in one box is what made "Fetch" and "Add layer" look like two
        # buttons for the same thing.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.prefetch_section = QWidget()
        prefetch = QVBoxLayout(self.prefetch_section)
        prefetch.setContentsMargins(0, 0, 0, 0)
        prefetch.setSpacing(4)
        layout.addWidget(self.prefetch_section)

        self.filter_section = QWidget()
        filters = QVBoxLayout(self.filter_section)
        filters.setContentsMargins(0, 0, 0, 0)
        filters.setSpacing(4)
        layout.addWidget(self.filter_section)

        # --- where ---------------------------------------------------------
        # Where comes first, as it does in every tool surveyed and in the
        # ARII parameter tree itself (Location > Group(s), Individual(s)).
        # Groups are expanded to locations by the server. Checkable like the
        # matrix combo; hidden unless the facility defines any, which on
        # this tenant is 3 facilities in 16 - a picker that is empty most
        # of the time reads as broken, so it is not shown empty. Its own form
        # so it can sit above the chemical list, which keeps the most room.
        self.where_form = QFormLayout()
        self.where_form.setContentsMargins(0, 0, 0, 0)
        self.group_combo = QComboBox()
        self.group_combo.setModel(QStandardItemModel(self.group_combo))
        self.group_combo.setToolTip(
            "Location groups defined for this facility in EQuIS.\n"
            "Tick one or more to fetch only their member locations.\n"
            "Tick nothing for every location."
        )
        self.group_combo.view().pressed.connect(self._on_group_item_pressed)
        self.group_combo.setEditable(True)
        self.group_combo.lineEdit().setReadOnly(True)
        self.group_combo.lineEdit().setPlaceholderText("All locations")
        self.group_label = QLabel("Location group · any of:")
        self.where_form.addRow(self.group_label, self.group_combo)
        self.group_combo.setVisible(False)
        self.group_label.setVisible(False)
        prefetch.addLayout(self.where_form)

        # --- analytes -----------------------------------------------------
        # The strongest lever by far: one analyte across nine years took 29 s
        # where three years of everything took 174 s. It gets the most room.
        self.analyte_filter = QLineEdit()
        self.analyte_filter.setPlaceholderText("Filter chemicals...")
        self.analyte_filter.textChanged.connect(self._populate_analytes)
        prefetch.addWidget(self.analyte_filter)

        self.analyte_list = QListWidget()
        self.analyte_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.analyte_list.setMinimumHeight(120)
        # Capped, or the list absorbs every spare pixel in the dock and
        # pushes the fetch button below the fold - the one control in this
        # step the user is looking for. Tall enough to scan a dozen
        # chemicals; the filter box handles the other four hundred.
        self.analyte_list.setMaximumHeight(220)
        self.analyte_list.setToolTip(
            "Which chemicals to fetch. Ctrl-click or shift-click for several.\n"
            "Selecting nothing fetches them all, which on a large facility is\n"
            "what makes a report time out."
        )
        self.analyte_list.itemSelectionChanged.connect(
            self._on_analyte_selection_changed)
        prefetch.addWidget(self.analyte_list, 1)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.analyte_count = QLabel("")
        row.addWidget(self.analyte_count, 1)
        self.select_none_btn = QPushButton("Select none")
        self.select_none_btn.setToolTip("Fetch every chemical in the report.")
        self.select_none_btn.clicked.connect(self._clear_analytes)
        row.addWidget(self.select_none_btn)
        prefetch.addLayout(row)

        # --- matrix, which is also a prefetch filter ----------------------
        # Saved onto the report like the analytes, so it belongs above the
        # fetch button with them rather than among the local filters.
        prefetch_form = QFormLayout()
        prefetch_form.setContentsMargins(0, 0, 0, 0)

        # --- filters applied to the rows already fetched -------------------
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)

        # Checkable rather than a plain picker: sediment and tailings are
        # both solids and belong on one map, while water never belongs beside
        # either, so which combinations are sensible is the user's call.
        self.matrix_combo = QComboBox()
        self.matrix_combo.setModel(QStandardItemModel(self.matrix_combo))
        self.matrix_combo.setToolTip(MATRIX_TOOLTIP)
        self.matrix_combo.view().pressed.connect(self._on_matrix_item_pressed)
        # The line edit is the summary of what is ticked; it is never typed
        # into, so it is read-only rather than disabled - a disabled field
        # greys its text and the summary becomes hard to read.
        self.matrix_combo.setEditable(True)
        self.matrix_combo.lineEdit().setReadOnly(True)
        self.matrix_combo.lineEdit().setPlaceholderText("All matrices")
        # Held so the row can be hidden as a unit; a stray "Matrix:" label
        # above nothing is worse than no row at all.
        # Labels read as field · operator, so a row states what it does:
        # server-side lists are always "any of", dates always "between".
        # Nothing else the parameters could express, so nothing else offered.
        self.matrix_label = QLabel("Matrix · any of:")
        prefetch_form.addRow(self.matrix_label, self.matrix_combo)

        # Multi-select for the same reason the matrix is: one sample is
        # routinely analysed by two methods, and they are two genuine
        # analyses rather than duplicate data. Ticking several draws a line
        # per method on the depth profile instead of merging them.
        self.method_combo = QComboBox()
        self.method_combo.setModel(QStandardItemModel(self.method_combo))
        self.method_combo.setToolTip(METHOD_TOOLTIP)
        self.method_combo.view().pressed.connect(self._on_method_item_pressed)
        self.method_combo.setEditable(True)
        self.method_combo.lineEdit().setReadOnly(True)
        self.method_combo.lineEdit().setPlaceholderText("All methods")
        self.method_label = QLabel("Method · any of:")
        form.addRow(self.method_label, self.method_combo)
        self.method_combo.setVisible(False)
        self.method_label.setVisible(False)
        # Hidden until a report says there is more than one matrix. Without
        # this the panel opens showing an empty dropdown, which reads as a
        # filter that has somehow been emptied rather than one with nothing
        # to offer yet.
        self.matrix_combo.setVisible(False)
        self.matrix_label.setVisible(False)

        # Dissolved and total interleave roughly 50/50 and differ by up to
        # ~29x for the same sample, so one has to be chosen. Dissolved is the
        # default; the warning below says so rather than leaving it implicit.
        # Multi-select for the same reason matrix and method are: dissolved
        # and total are two genuine analyses of one sample, and comparing
        # them is legitimate. What must not happen is AGGREGATING across
        # them - a maximum would report whichever fraction ran higher - so
        # the gate still refuses a mixed selection on an aggregated layer,
        # while a depth profile draws a line per fraction.
        self.fraction_combo = QComboBox()
        self.fraction_combo.setModel(QStandardItemModel(self.fraction_combo))
        for code, label in FRACTIONS:
            item = QStandardItem(label)
            item.setData(code, Qt.ItemDataRole.UserRole)
            item.setFlags(TICKABLE_ROW)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.fraction_combo.model().appendRow(item)
        self.fraction_combo.setToolTip(FRACTION_TOOLTIP)
        self.fraction_combo.view().pressed.connect(
            self._on_fraction_item_pressed)
        self.fraction_combo.setEditable(True)
        self.fraction_combo.lineEdit().setReadOnly(True)
        self.fraction_combo.lineEdit().setPlaceholderText("Any (warn if mixed)")
        # A QLabel rather than a bare string, so the row can be hidden as a
        # unit: a stray "Fraction:" above nothing reads as a broken control.
        self.fraction_label = QLabel("Fraction · any of:")
        form.addRow(self.fraction_label, self.fraction_combo)
        # Hidden until a fetch says which fractions exist, for the same
        # reason the method row is - see clear_result_filters().
        self.fraction_combo.setVisible(False)
        self.fraction_label.setVisible(False)

        # Dates and QC are PREFETCH filters: @start_date, @end_date and
        # @sample_types are saved onto the report and decide which rows come
        # back, and both are in the dialog's selection key. They used to sit
        # in the step-5 form beside method and fraction, which made changing
        # them look free when it silently demanded another fetch. A control's
        # box is decided by when it takes effect, not by what it resembles.
        self.date_check = QCheckBox("Dates · between:")
        self.date_check.toggled.connect(self._on_date_toggled)
        dates = QHBoxLayout()
        dates.setContentsMargins(0, 0, 0, 0)
        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setDisplayFormat("yyyy-MM-dd")
        self.start_date.setEnabled(False)
        self.start_date.dateChanged.connect(self._on_changed)
        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDisplayFormat("yyyy-MM-dd")
        self.end_date.setEnabled(False)
        self.end_date.dateChanged.connect(self._on_changed)
        dates.addWidget(self.start_date)
        dates.addWidget(QLabel("and"))
        dates.addWidget(self.end_date)
        prefetch_form.addRow(self.date_check, dates)

        self.qc_check = QCheckBox("Include blanks and duplicates")
        self.qc_check.setToolTip(
            "Equipment blanks and field duplicates were half the rows in one\n"
            "report. Blanks are near-zero by construction, so including them\n"
            "drags any aggregate down. Saved onto the report, so changing\n"
            "this needs another fetch."
        )
        self.qc_check.toggled.connect(self._on_changed)
        prefetch_form.addRow("QC samples:", self.qc_check)

        for layout_ in (self.where_form, prefetch_form, form):
            layout_.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        prefetch.addLayout(prefetch_form)
        filters.addLayout(form)

        # --- what this selection hides ------------------------------------
        # Beside the controls that caused it, not in a log nobody reads.
        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet(
            "QLabel { color: #7a5a00; background: #fdf6e3;"
            " border: 1px solid #d9c48a; border-radius: 3px; padding: 4px; }"
        )
        filters.addWidget(self.warning_label)

        self.source_label = QLabel("")
        self.source_label.setWordWrap(True)
        self.source_label.setStyleSheet("QLabel { color: #555; }")
        prefetch.addWidget(self.source_label)

        self._refresh_warnings()

    # ------------------------------------------------------------ populating

    def set_analytes(self, analytes, counts=None):
        """Offer these (cas_rn, name) pairs, keeping any that stay selected.

        `counts` maps CAS to a result count, shown once a fetch has happened:
        before then the list comes from the parameter service and says only
        what exists, which is not the same as how much of it there is.
        """
        self._analytes = list(analytes or [])
        self._counts = dict(counts or {})
        if self._analytes:
            self._unavailable = ""
            self._busy = False
            for widget in (self.analyte_filter, self.select_none_btn):
                widget.setEnabled(True)
            self.matrix_combo.setEnabled(self._matrix_useful)
            # Method and fraction only once a fetch has said what exists;
            # enabling them here would bring back a row deliberately hidden
            # until then.
            self.method_combo.setEnabled(len(self._method_offer) > 1)
            self.fraction_combo.setEnabled(self._fractions_known)
        # Drop anything the new report does not offer, so a stale choice from
        # a previous report cannot filter this one to nothing.
        self._chosen &= {cas for cas, _ in self._analytes}
        self._populate_analytes()

    def _clear_analytes(self):
        """Select nothing, including what the filter box is hiding."""
        self._chosen = set()
        self._populate_analytes()

    def set_matrices(self, matrices, replace=False):
        """Offer these (code, label) pairs, with an "any" entry first.

        Merged into what has already been offered rather than replacing it.
        The report's @matrix_codes lists what the facility holds; a later
        fetch reports only the matrices its own rows happened to contain,
        which is necessarily a subset. Letting that subset win would remove
        choices the facility really does have - and on a two-matrix facility
        it removes the combo altogether.

        A label seen later wins, so the fetch's bare codes never overwrite a
        descriptive label the report supplied. `replace=True` starts the
        offer over, for a genuine change of facility or report.
        """
        if replace:
            self._matrix_offer = []
        labels = dict(self._matrix_offer)
        for code, label in matrices or []:
            if not code:
                continue
            if code not in labels:
                self._matrix_offer.append((code, label))
                labels[code] = label
            elif label and not labels[code]:
                # Fill in a label we did not have before, keeping offer order.
                self._matrix_offer = [
                    (c, label if c == code else l)
                    for c, l in self._matrix_offer
                ]
                labels[code] = label

        # Ticked codes survive the rebuild: a fetch that re-offers the same
        # matrices should not silently clear what the user chose.
        current = set(self.selected_matrices())
        self.matrix_combo.blockSignals(True)
        self.matrix_combo.clear()
        model = self.matrix_combo.model()
        model.clear()
        for code, label in self._matrix_offer:
            item = QStandardItem(
                "%s - %s" % (code, label) if label and label != code else code)
            item.setData(code, Qt.ItemDataRole.UserRole)
            item.setFlags(TICKABLE_ROW)
            item.setCheckState(Qt.CheckState.Checked if code in current
                               else Qt.CheckState.Unchecked)
            model.appendRow(item)
        self.matrix_combo.blockSignals(False)
        self._refresh_matrix_text()
        # One real matrix plus "all" is not a choice, so the combo cannot be
        # used to filter. It stays on screen naming that matrix instead of
        # vanishing: a control that silently disappears is indistinguishable
        # from one that is broken, and "why is the matrix picker missing?" is
        # a question this panel has actually prompted. selection() reads
        # _matrix_useful, so a disabled combo still contributes no filter.
        self._matrix_useful = len(self._matrix_offer) > 1
        only = self._matrix_offer[0] if len(self._matrix_offer) == 1 else None
        showing = self._matrix_useful or only is not None
        self.matrix_combo.setEnabled(
            self._matrix_useful and not self._unavailable)
        self.matrix_combo.setVisible(showing)
        self.matrix_label.setVisible(showing)
        if only is not None:
            shown = ("%s - %s" % only) if only[1] and only[1] != only[0] else only[0]
            self.matrix_combo.setToolTip(
                "This facility reports only one matrix (%s), so there is\n"
                "nothing to choose between. Results are not filtered by matrix."
                % shown)
        else:
            self.matrix_combo.setToolTip(MATRIX_TOOLTIP)
        self._on_changed()

    def clear_result_filters(self):
        """Hide the pickers that only fetched rows can answer.

        Method and fraction are properties of the ANALYSIS, not of the
        facility. The parameter service can only answer per facility - all 54
        methods and all 7 fractions, whatever analyte and matrix are saved -
        so offering them before a fetch shows a list mostly irrelevant to the
        data in question and invites a filter that matches nothing.

        Called when a report is prepared or the facility changes, so a
        previous fetch's methods cannot linger beside a new one's data.
        """
        self._method_offer = []
        self._methods_present = set()
        self.method_combo.model().clear()
        self.method_combo.setVisible(False)
        self.method_label.setVisible(False)
        self.set_selected_fractions([])
        self._fractions_known = False
        self.fraction_combo.setVisible(False)
        self.fraction_label.setVisible(False)
        self._on_changed()

    def set_fractions(self, fractions):
        """Offer the fractions the fetched rows actually carry.

        Reports carry codes beyond D/T (PRT, SPLP, TCLP...), but those are
        leaching procedures rather than the dissolved/total split this is
        about, so they are not offered.

        Shown only once this has been called with something: which fractions
        exist is analyte- and matrix-specific, so before a fetch there is
        nothing honest to show. A row that appears with the data is clearer
        than one that sits there all along offering choices that return
        nothing.
        """
        available = {str(code).upper() for code, _ in (fractions or [])}
        if not available:
            return
        # Only D/T/N are offered, so a report carrying nothing but leaching
        # codes still has no fraction choice worth showing.
        if not available & {code for code, _ in FRACTIONS}:
            return
        self._fractions_known = True
        self.fraction_combo.setVisible(True)
        self.fraction_label.setVisible(True)
        for index in range(self.fraction_combo.model().rowCount()):
            code = self.fraction_combo.model().item(index).data(
                Qt.ItemDataRole.UserRole)
            if code is None:
                continue
            # Qt has no per-item disable, so an absent fraction is removed
            # from the label rather than silently offered.
            label = dict(FRACTIONS).get(code, code)
            if code.upper() not in available:
                label += " (none in this report)"
            self.fraction_combo.model().item(index).setText(label)

    def set_unavailable(self, message, busy=False):
        """Say why the pickers are empty, rather than showing bare lists.

        An empty chemical list is indistinguishable from a facility with no
        chemistry, which is a much more alarming reading than the truth. The
        message goes INTO the list rather than under it: a caption below an
        empty box is exactly where someone waiting for the box to fill is not
        looking. Held rather than just written, because every later refresh
        would otherwise overwrite it with a count of nothing.
        """
        self._unavailable = message or ""
        self._busy = bool(busy)
        # Whatever was chosen belonged to the previous report; keeping it
        # would filter the next fetch by chemicals the user cannot see and
        # did not pick for it.
        self._analytes = []
        self._chosen = set()
        self._counts = {}
        self.analyte_list.blockSignals(True)
        self.analyte_list.clear()
        if self._unavailable:
            item = QListWidgetItem(
                ("⏳  " if busy else "") + self._unavailable)
            # Not selectable: it is a status line, not a chemical.
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.analyte_list.addItem(item)
        self.analyte_list.blockSignals(False)
        # A picker that cannot answer yet should not look ready to be used.
        for widget in (self.analyte_filter, self.select_none_btn,
                       self.matrix_combo, self.method_combo):
            widget.setEnabled(not self._unavailable)
        self.fraction_combo.setEnabled(
            self._fractions_known and not self._unavailable)
        self._refresh_warnings()

    def set_source(self, source):
        """Say whether these filters shape the request or the view."""
        self._source = source if source in SOURCE_HINTS else LIVE
        self._refresh_source()

    def _populate_analytes(self):
        if self._unavailable and not self._analytes:
            # Still waiting; leave the status line in place rather than
            # replacing it with an empty list.
            return
        needle = self.analyte_filter.text().strip().lower()
        self.analyte_list.blockSignals(True)
        self.analyte_list.clear()
        for cas, name in self._analytes:
            haystack = "%s %s" % (name, cas)
            if needle and needle not in haystack.lower():
                continue
            label = name if not cas else "%s   (%s)" % (name, cas)
            if cas in self._counts:
                label += "   %s result(s)" % format(self._counts[cas], ",d")
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, (cas, name))
            self.analyte_list.addItem(item)
            # Re-show what was already chosen; the rest stays remembered even
            # while the filter hides it.
            if cas in self._chosen:
                item.setSelected(True)
        self.analyte_list.blockSignals(False)
        self._refresh_warnings()
        self.changed.emit()

    def _on_analyte_selection_changed(self):
        """Fold the visible rows' state into what is remembered.

        Only the rows on screen can have changed, so the hidden ones keep
        whatever they had - which is the whole point of remembering them.
        """
        visible = set()
        selected = set()
        for index in range(self.analyte_list.count()):
            item = self.analyte_list.item(index)
            value = item.data(Qt.ItemDataRole.UserRole)
            if not value:
                continue
            visible.add(value[0])
            if item.isSelected():
                selected.add(value[0])
        if (self.analyte_list.selectionMode()
                == QAbstractItemView.SelectionMode.SingleSelection
                and selected):
            # One chemical, whatever the filter is hiding: a hidden choice
            # kept alongside the clicked one would make "single" two.
            self._chosen = selected
        else:
            self._chosen = (self._chosen - visible) | selected
        self._on_changed()

    def set_single_select(self, single):
        """Switch the list between one chemical and many.

        Interactive mode maps each pick as it is made, so a pick is one
        chemical. Going single keeps the chemical the user is on - the
        current row if it is chosen, else the first chosen in list order -
        rather than clearing the choice and leaving nothing drawn.
        """
        if single and len(self._chosen) > 1:
            keep = None
            current = self.analyte_list.currentItem()
            if current is not None and current.isSelected():
                value = current.data(Qt.ItemDataRole.UserRole)
                keep = value[0] if value else None
            if keep is None:
                keep = next((cas for cas, _ in self._analytes
                             if cas in self._chosen), None)
            self._chosen = {keep} if keep is not None else set()
        self.analyte_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection if single
            else QAbstractItemView.SelectionMode.ExtendedSelection)
        self.analyte_list.setToolTip(
            "Interactive: each chemical is mapped as it is picked, one at a\n"
            "time. Arrow keys step through the list." if single else
            "Which chemicals to fetch. Ctrl-click or shift-click for several.\n"
            "Selecting nothing fetches them all, which on a large facility is\n"
            "what makes a report time out.")
        self._populate_analytes()

    # -------------------------------------------------------------- reading

    def selected_analytes(self):
        """The (cas_rn, name) pairs chosen, or [] for "everything".

        Includes chemicals the filter box is currently hiding: they are still
        part of the query, and dropping them would quietly change what is
        fetched based on what happens to be typed in a search box.
        """
        return [(cas, name) for cas, name in self._analytes
                if cas in self._chosen]

    def set_selected_analytes(self, wanted):
        """Choose exactly these chemicals, by CAS RN or by name.

        The counterpart to set_selected_fractions, and it exists for the
        same reason: something other than a click needs to drive the
        selection - an `equis://` link from a map tip, in this case.

        CAS wins over the name where both match, which is the rule
        report_data applies everywhere else: a lookup by CAS is
        authoritative rather than merely preferred.
        """
        names = {str(n or "").strip().lower(): cas
                 for cas, n in self._analytes}
        by_cas = {str(cas or "").strip(): cas for cas, _ in self._analytes}
        chosen = set()
        for value in (wanted or []):
            key = str(value or "").strip()
            if not key:
                continue
            if key in by_cas:
                chosen.add(by_cas[key])
            elif key.lower() in names:
                chosen.add(names[key.lower()])
        self._chosen = chosen
        self._populate_analytes()
        return len(chosen)

    def selected_fractions(self):
        """Every ticked fraction code. Empty means no filter."""
        model = self.fraction_combo.model()
        chosen = []
        for row in range(model.rowCount()):
            item = model.item(row)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                code = item.data(Qt.ItemDataRole.UserRole)
                if code:
                    chosen.append(code)
        return chosen

    def set_selected_fractions(self, codes):
        """Tick exactly these fraction codes."""
        wanted = {str(c).upper() for c in (codes or []) if str(c or "").strip()}
        model = self.fraction_combo.model()
        self.fraction_combo.blockSignals(True)
        for row in range(model.rowCount()):
            item = model.item(row)
            if item is None:
                continue
            code = str(item.data(Qt.ItemDataRole.UserRole) or "").upper()
            item.setCheckState(Qt.CheckState.Checked if code in wanted
                               else Qt.CheckState.Unchecked)
        self.fraction_combo.blockSignals(False)
        self._refresh_fraction_text()
        self._on_changed()

    def _on_fraction_item_pressed(self, index):
        """Toggle rather than select-and-close."""
        model = self.fraction_combo.model()
        item = model.itemFromIndex(index)
        if item is None:
            return
        item.setCheckState(
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked)
        self._refresh_fraction_text()
        self._on_changed()

    def _refresh_fraction_text(self):
        chosen = self.selected_fractions()
        edit = self.fraction_combo.lineEdit()
        if edit is not None:
            edit.setText(", ".join(chosen) if chosen else "")

    def set_methods(self, methods, replace=False, present=None):
        """Offer these analytic method codes.

        Merged rather than replaced, exactly as the matrix offer is, and for
        the same reason. The offer arrives from two places now: the parameter
        service names every method the FACILITY has ever used, while a fetch
        reports only those its own rows carried - necessarily a subset. This
        used to replace, on the belief that the service had no method
        equivalent, so a fetch of one analyte narrowed the combo to that
        analyte's methods with no way back to the others short of re-preparing
        the report.

        `present` is the subset the rows in hand actually contained. Those are
        the useful ones, so they are marked - but the rest stay selectable,
        because removing them is what strands the next request.
        """
        if replace:
            self._method_offer = []
        for code in methods or []:
            code = str(code).strip()
            if code and code not in self._method_offer:
                self._method_offer.append(code)
        if present is not None:
            self._methods_present = {str(c).strip().upper()
                                     for c in present if str(c or "").strip()}

        current = set(self.selected_methods())
        model = self.method_combo.model()
        self.method_combo.blockSignals(True)
        model.clear()
        for code in self._method_offer:
            # Annotated, never omitted: a method absent from this fetch is
            # still a method the facility runs, and the user may be about to
            # widen the request to it.
            label = code
            if self._methods_present and code.upper() not in self._methods_present:
                label += "  (none in this data)"
            item = QStandardItem(label)
            item.setData(code, Qt.ItemDataRole.UserRole)
            item.setFlags(TICKABLE_ROW)
            item.setCheckState(Qt.CheckState.Checked if code in current
                               else Qt.CheckState.Unchecked)
            model.appendRow(item)
        self.method_combo.blockSignals(False)
        # A single method is shown, disabled, naming itself - hiding the row
        # is indistinguishable from the feature being missing, which is
        # exactly the confusion the matrix combo used to cause.
        useful = model.rowCount() > 1
        showing = model.rowCount() > 0
        self.method_combo.setEnabled(useful and not self._unavailable)
        self.method_combo.setVisible(showing)
        self.method_label.setVisible(showing)
        if showing and not useful:
            only = model.item(0).data(Qt.ItemDataRole.UserRole)
            self.method_combo.setToolTip(
                "This facility reports only one analytic method (%s), so "
                "there is nothing to choose between." % only)
        else:
            self.method_combo.setToolTip(METHOD_TOOLTIP)
        self._refresh_method_text()
        self._on_changed()

    def selected_methods(self):
        """Every ticked method code. Empty means all of them."""
        model = self.method_combo.model()
        chosen = []
        for row in range(model.rowCount()):
            item = model.item(row)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                code = item.data(Qt.ItemDataRole.UserRole)
                if code:
                    chosen.append(code)
        return chosen

    def _on_method_item_pressed(self, index):
        """Toggle rather than select-and-close, as the matrix combo does."""
        model = self.method_combo.model()
        item = model.itemFromIndex(index)
        if item is None:
            return
        item.setCheckState(
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked)
        self._refresh_method_text()
        self._on_changed()

    def _refresh_method_text(self):
        chosen = self.selected_methods()
        model = self.method_combo.model()
        if not chosen and model.rowCount() == 1:
            # Nothing to pick, so say what the one method is.
            text = str(model.item(0).data(Qt.ItemDataRole.UserRole) or "")
        elif not chosen:
            text = ""
        elif len(chosen) <= 2:
            text = ", ".join(chosen)
        else:
            text = "%s +%d more" % (chosen[0], len(chosen) - 1)
        edit = self.method_combo.lineEdit()
        if edit is not None:
            edit.setText(text)

    def set_location_groups(self, groups):
        """Offer these (code, label) pairs; show the row only if there are any.

        Replaces the offer: groups are a property of the facility, read once
        when the report is prepared, so there is no second source to merge.
        Whatever was ticked survives if the new offer still has it.
        """
        current = set(self.selected_location_groups())
        self._group_offer = [(str(code), label or str(code))
                             for code, label in (groups or []) if code]
        model = self.group_combo.model()
        self.group_combo.blockSignals(True)
        model.clear()
        for code, label in self._group_offer:
            item = QStandardItem(
                "%s - %s" % (code, label) if label and label != code else code)
            item.setData(code, Qt.ItemDataRole.UserRole)
            item.setFlags(TICKABLE_ROW)
            item.setCheckState(Qt.CheckState.Checked if code in current
                               else Qt.CheckState.Unchecked)
            model.appendRow(item)
        self.group_combo.blockSignals(False)
        showing = bool(self._group_offer)
        self.group_combo.setVisible(showing)
        self.group_label.setVisible(showing)
        self.group_combo.setEnabled(showing and not self._unavailable)
        self._refresh_group_text()
        self._on_changed()

    def selected_location_groups(self):
        """Every ticked group code, in offer order. Empty means all."""
        model = self.group_combo.model()
        chosen = []
        for row in range(model.rowCount()):
            item = model.item(row)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                code = item.data(Qt.ItemDataRole.UserRole)
                if code:
                    chosen.append(code)
        return chosen

    def _on_group_item_pressed(self, index):
        """Toggle rather than select-and-close, as the matrix combo does."""
        model = self.group_combo.model()
        item = model.itemFromIndex(index)
        if item is None:
            return
        item.setCheckState(
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked)
        self._refresh_group_text()
        self._on_changed()

    def _refresh_group_text(self):
        chosen = self.selected_location_groups()
        if not chosen:
            text = ""          # the placeholder reads "All locations"
        elif len(chosen) <= 2:
            text = ", ".join(chosen)
        else:
            text = "%s +%d more" % (chosen[0], len(chosen) - 1)
        edit = self.group_combo.lineEdit()
        if edit is not None:
            edit.setText(text)

    def selected_matrices(self):
        """Every ticked matrix code, in offer order. Empty means all."""
        model = self.matrix_combo.model()
        chosen = []
        for row in range(model.rowCount()):
            item = model.item(row)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                code = item.data(Qt.ItemDataRole.UserRole)
                if code:
                    chosen.append(code)
        return chosen

    def set_selected_matrices(self, codes):
        """Tick exactly these matrix codes."""
        wanted = {str(c).upper() for c in (codes or []) if str(c or "").strip()}
        model = self.matrix_combo.model()
        self.matrix_combo.blockSignals(True)
        for row in range(model.rowCount()):
            item = model.item(row)
            if item is None:
                continue
            code = str(item.data(Qt.ItemDataRole.UserRole) or "").upper()
            item.setCheckState(Qt.CheckState.Checked if code in wanted
                               else Qt.CheckState.Unchecked)
        self.matrix_combo.blockSignals(False)
        self._refresh_matrix_text()
        self._on_changed()

    def _on_matrix_item_pressed(self, index):
        """Toggle the pressed row instead of selecting and closing.

        A checkable combo still behaves like a picker unless this is done:
        clicking a row would choose it and shut the popup, so ticking a
        second matrix would be impossible.
        """
        model = self.matrix_combo.model()
        item = model.itemFromIndex(index)
        if item is None:
            return
        item.setCheckState(
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked)
        self._refresh_matrix_text()
        self._on_changed()

    def _refresh_matrix_text(self):
        """Summarise the ticked matrices in the closed combo."""
        chosen = self.selected_matrices()
        if not chosen:
            text = ""          # the placeholder reads "All matrices"
        elif len(chosen) <= 3:
            text = ", ".join(chosen)
        else:
            text = "%s +%d more" % (", ".join(chosen[:2]), len(chosen) - 2)
        edit = self.matrix_combo.lineEdit()
        if edit is not None:
            edit.setText(text)

    def selection(self, facility_id=None, locations=None):
        """The DataSelection these controls describe.

        `locations` is supplied by the dialog, which owns the Locations layer
        the codes are picked from; the panel has no view of the map.
        """
        matrices = self.selected_matrices() if self._matrix_useful else []
        sample_types = [] if self.qc_check.isChecked() else [SAMPLE_TYPE_NORMAL]
        start = end = None
        if self.date_check.isChecked():
            start = self.start_date.date().toPyDate()
            end = self.end_date.date().toPyDate()
        return DataSelection(
            facility_id=facility_id,
            analytes=[cas for cas, _ in self.selected_analytes() if cas],
            locations=list(locations or []),
            location_groups=self.selected_location_groups(),
            matrices=matrices,
            methods=self.selected_methods(),
            fractions=self.selected_fractions(),
            sample_types=sample_types,
            start=start,
            end=end,
        )

    # -------------------------------------------------------------- display

    def _on_date_toggled(self, on):
        self.start_date.setEnabled(on)
        self.end_date.setEnabled(on)
        self._on_changed()

    def _on_changed(self):
        self._refresh_warnings()
        self.changed.emit()

    def _refresh_warnings(self):
        total = len(self._analytes)
        chosen = len(self.selected_analytes())
        if not total and self._unavailable:
            self.analyte_count.setText(self._unavailable)
        elif not total:
            self.analyte_count.setText("")
        elif chosen:
            self.analyte_count.setText(
                "%d of %d chemical(s) selected" % (chosen, total))
        else:
            self.analyte_count.setText(
                "nothing selected - all %d chemicals" % total)

        notes = self.selection().warnings()
        self.warning_label.setText("  ".join(notes))
        self.warning_label.setVisible(bool(notes))
        self._refresh_source()

    def _refresh_source(self):
        self.source_label.setText(SOURCE_HINTS.get(self._source, ""))

    # ---------------------------------------------------------------- state

    def set_enabled(self, enabled):
        for widget in (self.analyte_filter, self.analyte_list,
                       self.select_none_btn,
                       self.qc_check, self.date_check):
            widget.setEnabled(enabled)
        # Only ever enabled when it is a real choice, so a busy panel cannot
        # bring back a combo that was hidden for having nothing to offer.
        self.matrix_combo.setEnabled(enabled and self._matrix_useful)
        self.group_combo.setEnabled(enabled and bool(self._group_offer))
        self.method_combo.setEnabled(enabled and len(self._method_offer) > 1)
        self.fraction_combo.setEnabled(enabled and self._fractions_known)
        if enabled:
            self.start_date.setEnabled(self.date_check.isChecked())
            self.end_date.setEnabled(self.date_check.isChecked())
        else:
            self.start_date.setEnabled(False)
            self.end_date.setEnabled(False)

    def clear(self):
        self._analytes = []
        self._chosen = set()
        self._counts = {}
        self._unavailable = ""
        self.analyte_list.clear()
        self.analyte_filter.clear()
        self.matrix_combo.model().clear()
        self._group_offer = []
        self.group_combo.model().clear()
        self.group_combo.setVisible(False)
        self.group_label.setVisible(False)
        self.method_combo.model().clear()
        self.method_combo.setVisible(False)
        self.method_label.setVisible(False)
        self._method_offer = []
        self._methods_present = set()
        self._fractions_known = False
        self.set_selected_fractions([])
        self.fraction_combo.setVisible(False)
        self.fraction_label.setVisible(False)
        self._matrix_offer = []
        self._matrix_useful = False
        self.matrix_combo.setVisible(False)
        self.matrix_label.setVisible(False)
        self._refresh_warnings()
