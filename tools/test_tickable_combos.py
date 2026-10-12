r"""A tick is a tick, wherever the row is clicked. Needs QGIS.

    "C:\Program Files\QGIS 4.2.1\bin\python-qgis.bat" tools\test_tickable_combos.py

The location group, matrix, method and fraction combos are checkable lists.
Each one's `pressed` handler toggles the row and tells the panel. While the
items were also ItemIsUserCheckable, Qt toggled a row ITSELF when its
checkbox was clicked, consuming the click, so the handler never ran and the
panel was never told. Clicking the text worked. Reported 2026-10-01 from
interactive mode: a matrix ticked by its box did not redraw until the next
chemical was picked.

Real mouse clicks through QTest on the open popup, offscreen, on the
checkbox and on the text. Each must toggle the row exactly once, and the
panel must be told the state the row was left in.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

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
app = QgsApplication([], True)
app.initQgis()

from qgis.PyQt.QtCore import QPoint, Qt                       # noqa: E402
from qgis.PyQt.QtTest import QTest                            # noqa: E402
from qgis.PyQt.QtWidgets import QStyle, QStyleOptionViewItem  # noqa: E402
from equis_loader.selection_panel import SelectionPanel       # noqa: E402

panel = SelectionPanel()
panel.set_location_groups([("G1", "Group one"), ("G2", "Group two")])
panel.set_matrices([("WS", "Surface water"), ("WG", "Groundwater")],
                   replace=True)
panel.set_methods(["SW6020", "E200.8"], replace=True)
panel.show()

told = []
panel.changed.connect(lambda: told.append(True))


def checkbox_point(view, index):
    """The centre of the row's check indicator, as the style draws it."""
    option = QStyleOptionViewItem()
    option.initFrom(view)
    option.rect = view.visualRect(index)
    option.features |= QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
    rect = view.style().subElementRect(
        QStyle.SubElement.SE_ItemViewItemCheckIndicator, option, view)
    return rect.center()


COMBOS = (
    ("location group", panel.group_combo, panel.selected_location_groups),
    ("matrix", panel.matrix_combo, panel.selected_matrices),
    ("method", panel.method_combo, panel.selected_methods),
    ("fraction", panel.fraction_combo, panel.selected_fractions),
)

for name, combo, selected in COMBOS:
    combo.setVisible(True)
    combo.setEnabled(True)
    model = combo.model()
    check("%s: rows are not user-checkable" % name,
          all(not (model.item(r).flags() & Qt.ItemFlag.ItemIsUserCheckable)
              for r in range(model.rowCount())) and model.rowCount() > 0,
          "%d rows" % model.rowCount())
    for where in ("checkbox", "text"):
        combo.showPopup()
        app.processEvents()
        view = combo.view()
        index = model.index(model.rowCount() - 1, 0)
        item = model.item(index.row())
        code = item.data(Qt.ItemDataRole.UserRole)
        was = item.checkState() == Qt.CheckState.Checked
        point = (checkbox_point(view, index) if where == "checkbox"
                 else view.visualRect(index).center() + QPoint(30, 0))
        del told[:]
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, point)
        app.processEvents()
        now = item.checkState() == Qt.CheckState.Checked
        check("%s: a click on the %s toggles the row once" % (name, where),
              now != was, "%s -> %s" % (was, now))
        check("%s: and the panel is told" % name + " (%s)" % where,
              len(told) >= 1, "%d change signal(s)" % len(told))
        check("%s: and reads the state the row was left in (%s)"
              % (name, where),
              (code in [str(c) for c in selected()]) == now
              or (code.upper() in [str(c).upper() for c in selected()]) == now,
              "%s in %s" % (code, selected()))
        combo.hidePopup()
        app.processEvents()

print()
if FAILURES:
    print("FAILED: %s" % ", ".join(FAILURES))
    app.exitQgis()
    raise SystemExit(1)
print("A tick is a tick, wherever the row is clicked.")
app.exitQgis()
