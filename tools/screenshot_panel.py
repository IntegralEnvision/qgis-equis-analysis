"""Render the plugin panel to a PNG so its layout can be looked at.

    "C:\\Program Files\\QGIS 4.2.1\\bin\\python-qgis.bat" tools\\screenshot_panel.py
        [--state STATE] [--out PATH] [--width N] [--height N]

Qt paints any widget straight to an image, so this needs no desktop
automation and no running QGIS window - it builds the panel, drives it into a
named state, and grabs it. Assertions answer "is this value right"; a picture
answers "does this read correctly", which is where the layout bugs have been:
two chemical lists that each looked reasonable alone, a Matrix row that was
visible but empty, a loading caption nobody would look at.

States:
  empty       nothing loaded - the panel as it opens
  prepared    a facility picked and its chemical list read from EQuIS
  fetched     one chemical fetched and mapped (the rest must stay listed)
  loading     waiting for the chemical list
  one-matrix  a report with a single matrix, where that row hides itself
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from qgis.core import QgsApplication  # noqa: E402

FACILITY_CHEMICALS = [
    ("7440-38-2", "Arsenic"), ("7439-89-6", "Iron"),
    ("7440-66-6", "Zinc"), ("71-43-2", "Benzene"),
]
MATRICES = [("WS", "Surface Water"), ("SE", "Sediment")]
FRACTIONS = [("D", "D"), ("T", "T"), ("N", "N")]
# What the parameter service answers for @analytic_methods: every method
# the FACILITY runs, not just those the fetched rows carried.
METHODS = [(c, c) for c in
           ("SW6010", "ICM14B", "ICM40B", "SW6020", "EPA_300.1")]

HEADERS = ["SYS_LOC_CODE", "SAMPLE_DATE", "CHEMICAL_NAME", "CAS_RN",
           "RESULT_NUMERIC", "RESULT_UNIT", "DETECT_FLAG", "MATRIX_CODE",
           "ANALYTIC_METHOD", "FRACTION"]


class _Iface:
    """The slice of QgisInterface the panel touches."""

    def mapCanvas(self):
        from qgis.gui import QgsMapCanvas
        if not hasattr(self, "_canvas"):
            self._canvas = QgsMapCanvas()
        return self._canvas

    def messageBar(self):
        class _Bar:
            def pushMessage(self, *a, **k):
                pass
        return _Bar()

    def addDockWidget(self, *a, **k):
        pass

    def removeDockWidget(self, *a, **k):
        pass


class _PrepareTask:
    """Stands in for PrepareLiveReportTask having just succeeded."""

    class live_report:
        created = True
        notes = []

    choices = {"@cas_rns": FACILITY_CHEMICALS,
               "@matrix_codes": MATRICES,
               "@analytic_methods": METHODS,
               "@fractions": FRACTIONS,
               # A facility that defines groups, so the Where row renders;
               # most facilities return none and the row stays hidden.
               "@location_groups": [("RI_WP", "RI_WP"),
                                    ("RI_WS", "Wetland surface")]}


def _report_task(rows, matrices=("WS",)):
    """Stands in for RunReportTask having just returned `rows`."""

    class _Task:
        _result = (HEADERS, rows)
        report = {"id": -1, "name": "Analytical Results II (filtered)"}
        source = "live"
        entry = None
        from_cache = False
        truncated = False
        eia = None
        selection_key = (("@cas_rns", "7440-38-2"),)

    return _Task()


def _rows(chemical="Arsenic", cas="7440-38-2", matrix="WS", count=50,
          method="SW6010", fraction="D"):
    return [["MW-%02d" % i, "0%d/01/2020" % (i % 9 + 1), chemical, cas,
             "%.2f" % (i * 0.5 + 1), "ug/L", "Y", matrix, method,
             fraction]
            for i in range(count)]


def build(state):
    from equis_loader import dialog as dlg

    panel = dlg.EquisDialog(_Iface())

    if state == "empty":
        pass
    elif state == "loading":
        panel.selection_panel.set_unavailable(
            "Loading the chemical list from EQuIS...", busy=True)
    elif state in ("prepared", "fetched", "one-matrix"):
        panel._on_live_report_ready(_PrepareTask())
        if state != "prepared":
            panel.selection_panel.analyte_list.item(0).setSelected(True)
            panel.selection_panel._on_analyte_selection_changed()
            rows = _rows(matrix="WS" if state == "one-matrix" else "WS")
            if state == "fetched":
                rows += _rows(matrix="SE", count=20, fraction="N",
                              method="ICM14B")
            panel._on_report_ready(_report_task(rows))
    else:
        raise SystemExit("unknown state: %s" % state)

    for box in (panel.connect_box, panel.facility_box, panel.report_box,
                panel.selection_box, panel.filter_box, panel.chem_box):
        box.setCollapsed(False)
    return panel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default="fetched")
    parser.add_argument("--out", default=os.path.join(ROOT, "panel.png"))
    parser.add_argument("--width", type=int, default=430)
    parser.add_argument("--height", type=int, default=1100)
    args = parser.parse_args()

    # True, not False: the panel has to be painted, which needs a GUI-capable
    # application even though no window is ever shown.
    app = QgsApplication([], True)
    app.initQgis()
    try:
        panel = build(args.state)
        # The plot is its own dock now and is never inside this panel, so
        # there is nothing to hide to see the whole step sequence.
        panel.resize(args.width, args.height)
        panel.show()
        app.processEvents()
        if not panel.grab().save(args.out):
            raise SystemExit("could not write %s" % args.out)
        print("%s  (%s)" % (args.out, args.state))

        # Print what the picture shows, so a diff of two runs is readable
        # without opening either image.
        lst = panel.selection_panel.analyte_list
        print("  chemicals : %d - %s"
              % (lst.count(), [lst.item(i).text().split("  ")[0]
                               for i in range(lst.count())]))
        print("  matrix row: %s"
              % ("shown" if panel.selection_panel.matrix_combo.isVisibleTo(
                  panel.selection_panel) else "hidden"))
        mc = panel.selection_panel.method_combo
        print("  method row: %s - %d offered %s"
              % ("shown" if mc.isVisibleTo(panel.selection_panel)
                 else "hidden", mc.model().rowCount(),
                 [mc.model().item(i).text()
                  for i in range(mc.model().rowCount())]))
        fc = panel.selection_panel.fraction_combo
        print("  fraction rw: %s"
              % ("shown" if fc.isVisibleTo(panel.selection_panel)
                 else "hidden"))
        print("  map button: %r" % panel.map_btn.text())
        print("  boxes     : %s"
              % [b.title() for b in (panel.connect_box, panel.facility_box,
                                     panel.report_box, panel.selection_box,
                                     panel.filter_box, panel.chem_box)])
        print("  fetch btn : %r" % panel.run_report_btn.text())
    finally:
        app.exitQgis()


if __name__ == "__main__":
    main()
