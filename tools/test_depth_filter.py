r"""The depth filter's controls: a range slider and two spin boxes. Needs QGIS.

    "C:\Program Files\QGIS 4.2.3\bin\python-qgis.bat" tools\test_depth_filter.py

The rules the filter applies are in report_data (in_depth, row_scope) and
held to the store by tools/test_store_parity.py. This is the panel's half:
the row appears only when the rows in hand have a depth, the slider and the
spin boxes stay in step, a drag is applied once it settles rather than per
pixel, and a new range (another matrix) keeps, clamps or resets the bounds
as set_depth_extent says. Offscreen; the slider is dragged with the mouse.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

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
from qgis.PyQt.QtWidgets import QStyle, QStyleOptionSlider    # noqa: E402
from equis_loader import report_data as rd                    # noqa: E402
from equis_loader.selection_panel import (                    # noqa: E402
    DEPTH_SETTLE_MS, DEPTH_SLIDER_STEPS, SelectionPanel, depth_ticks)

# The sediment of tools/test_store_parity.py's depth_rows, as its extent.
SEDIMENT = rd.depth_extent_from(
    [("cm", 5, 0.0, 40.0), ("m", 1, 0.3, 0.5), ("ft", 1, 1.0, 2.0),
     ("fathom", 1, 1.0, 2.0)],
    [("cm", d) for d in (0, 2, 10, 15, 20, 30, 40)]
    + [("m", 0.3), ("m", 0.5), ("ft", 1), ("ft", 2), ("fathom", 1)])
STOPS = [0.0, 2.0, 10.0, 15.0, 20.0, 30.0, 30.48, 40.0, 50.0, 60.96]

panel = SelectionPanel()
panel.resize(420, 600)
panel.show()
told = []
panel.changed.connect(lambda: told.append(panel.selection().depth))


def settle():
    QTest.qWait(DEPTH_SETTLE_MS + 150)


def depth():
    return panel.selection().depth


check("hidden while the rows in hand have no depth",
      not panel.depth_check.isVisible() and depth() is None)

panel.set_depth_extent(SEDIMENT)
check("shown once a depth is offered",
      panel.depth_check.isVisible() and panel.depth_box.isVisible())
check("offered in the unit most rows use, over the full range",
      (panel.depth_unit_label.text(), panel.depth_top.value(),
       panel.depth_bottom.value()) == ("cm", 0.0, 60.96),
      repr((panel.depth_unit_label.text(), panel.depth_top.value(),
            panel.depth_bottom.value())))
check("the unconvertible rows are named where the filter is",
      "1 result(s) in fathom" in panel.depth_unit_label.toolTip())
check("unticked, it filters nothing", depth() is None)
check("unticked, the handles cannot be moved",
      not panel.depth_slider.isEnabled() and not panel.depth_top.isEnabled())

del told[:]
panel.depth_check.setChecked(True)
check("ticked: the full range, which leaves out rows with no depth",
      depth() == (0.0, 60.96, "cm") and told == [(0.0, 60.96, "cm")],
      repr((depth(), told)))
check("ticked: the warning says rows with no depth are excluded",
      "no depth" in panel.warning_label.text())

def handle_centre(slider, value):
    """Where the style draws a handle for `value`, as QgsRangeSlider does."""
    option = QStyleOptionSlider()
    option.initFrom(slider)
    option.orientation = Qt.Orientation.Horizontal
    option.minimum, option.maximum = slider.minimum(), slider.maximum()
    option.sliderPosition = option.sliderValue = value
    return slider.style().subControlRect(
        QStyle.ComplexControl.CC_Slider, option,
        QStyle.SubControl.SC_SliderHandle, slider).center()


# Drag the top handle (at the left end) a third of the way along. The
# layout gives the slider its width only once events have run.
QTest.qWait(50)
del told[:]
slider = panel.depth_slider
start = handle_centre(slider, slider.lowerValue())
y = start.y()
QTest.mousePress(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                 start)
for x in range(start.x(), slider.width() // 3, 8):
    QTest.mouseMove(slider, QPoint(x, y))
QTest.mouseRelease(slider, Qt.MouseButton.LeftButton,
                   Qt.KeyboardModifier.NoModifier, QPoint(slider.width() // 3, y))
moved = panel.depth_top.value()
check("a mouse drag moves the top handle, and the spin box follows",
      slider.lowerValue() > 0 and moved > 0,
      "tick %d, %r cm, slider %d px wide, pressed at %r"
      % (slider.lowerValue(), moved, slider.width(), (start.x(), start.y())))
check("the handle lands on a depth where a sample starts or ends",
      moved in STOPS, "%r cm" % moved)
check("not applied mid-drag", told == [], repr(told))
settle()
check("applied once it settles, once", told == [(moved, 60.96, "cm")],
      repr(told))

# Type a bottom: the slider follows, and the top cannot pass it.
del told[:]
panel.depth_bottom.setValue(12.5)
check("a typed bound moves the slider's handle",
      slider.upperValue() == panel._depth_tick(12.5), repr(slider.upperValue()))
panel.depth_top.setValue(20.0)
check("a top typed below the bottom carries the bottom with it",
      (panel.depth_top.value(), panel.depth_bottom.value()) == (20.0, 20.0),
      repr((panel.depth_top.value(), panel.depth_bottom.value())))
settle()
check("typing settles to one change too", told == [(20.0, 20.0, "cm")],
      repr(told))

# Another matrix in the same unit: bounds kept where they still fit.
panel.depth_top.setValue(5.0)
panel.depth_bottom.setValue(30.0)
settle()
del told[:]
panel.set_depth_extent(rd.depth_extent_from([("cm", 3, 0.0, 25.0)]))
check("same unit, narrower range: the bounds are clamped into it",
      depth() == (5.0, 25.0, "cm") and told == [(5.0, 25.0, "cm")],
      repr((depth(), told)))
del told[:]
panel.set_depth_extent(rd.depth_extent_from([("cm", 3, 0.0, 25.0)]))
check("the same range again changes nothing and says nothing", told == [])
# The bottom now sits at its end (25), the top does not (5): a wider range
# carries the bottom to its new end and leaves the top where it was put.
panel.set_depth_extent(rd.depth_extent_from([("cm", 3, 0.0, 40.0)]))
check("a bound at its end follows the end; one moved inside stays",
      depth() == (5.0, 40.0, "cm"), repr(depth()))
panel.depth_top.setValue(30.0)
settle()
panel.set_depth_extent(rd.depth_extent_from([("cm", 3, 0.0, 25.0)]))
check("a kept range wholly outside the new one gives way to all of it",
      depth() == (0.0, 25.0, "cm"), repr(depth()))

# Another unit: a bound typed in cm says nothing in m, so start over.
panel.set_depth_extent(rd.depth_extent_from([("m", 9, 0.1, 38.93)]))
check("a new unit starts again at its full range",
      depth() == (0.1, 38.93, "m"), repr(depth()))
check("the slider spans it", slider.lowerValue() == 0
      and slider.upperValue() == slider.maximum(),
      repr((slider.lowerValue(), slider.upperValue(), slider.maximum())))
slider.setRange(0, slider.maximum())
check("at the ends, the handles reach the range itself",
      (panel.depth_top.value(), panel.depth_bottom.value()) == (0.1, 38.93),
      repr((panel.depth_top.value(), panel.depth_bottom.value())))
settle()

# Soil in feet, as reported 2026-10-30: a few borings to 200 ft beside
# hundreds of shallow samples. Evenly spaced, the slider moved a foot at a
# time; at the boundaries, 0.33 ft is a stop of its own.
SOIL = [0, 0.33, 0.5, 1, 2, 5, 10, 50, 120, 200]
panel.set_depth_extent(rd.depth_extent_from(
    [("ft", 40, 0.0, 200.0)], [("ft", d) for d in SOIL]))
slider.setRange(0, 1)
check("feet: the second stop is 0.33 ft, not a whole foot",
      depth() == (0.0, 0.33, "ft"), repr(depth()))
check("feet: the slider's middle is 2 ft, not 100",
      panel._depth_value(slider.maximum() // 2) == 2.0, repr(
          panel._depth_value(slider.maximum() // 2)))
# The spin boxes' arrows walk the same boundaries, and hold a depth to
# the precision it is compared at.
panel.depth_bottom.stepBy(1)
check("an arrow goes to the next boundary", depth()[1] == 0.5, repr(depth()))
panel.depth_bottom.stepBy(-2)
check("and back past one", depth()[1] == 0.0, repr(depth()))
panel.depth_bottom.setValue(0.333)
check("a typed 0.333 is kept as typed, not rounded to 0.33",
      depth()[1] == 0.333 and panel.depth_bottom.text() == "0.333",
      repr((depth(), panel.depth_bottom.text())))
panel.depth_bottom.stepBy(1)
check("an arrow from between two boundaries goes to the next",
      depth()[1] == 0.5, repr(depth()))
settle()

# More boundaries than the slider has positions (Facility A's water has
# 468): every one stays an arrow stop, a ranked subset is the slider's.
many = [round(0.1 * i, 1) for i in range(468)]
panel.set_depth_extent(rd.depth_extent_from(
    [("m", 500, 0.0, many[-1])], [("m", d) for d in many]))
check("thinned to the slider's width, the ends kept",
      slider.maximum() + 1 == DEPTH_SLIDER_STEPS
      and panel._depth_value(0) == 0.0
      and panel._depth_value(slider.maximum()) == many[-1],
      repr((slider.maximum(), panel._depth_value(slider.maximum()))))
check("the arrows still stop at every boundary",
      panel.depth_bottom.stops == many)
check("depth_ticks keeps every stop when there are few",
      depth_ticks([0, 1, 2], 5) == [0, 1, 2])
settle()

# Untouched since then: a wider range is simply taken.
panel.set_depth_extent(rd.depth_extent_from([("m", 9, 0.0, 84.55)]))
check("untouched, a new range replaces the old one entirely",
      depth() == (0.0, 84.55, "m"), repr(depth()))

# A range with no thickness at all: one handle position, no division by 0.
panel.set_depth_extent(rd.depth_extent_from([("m", 4, 2.0, 2.0)]))
check("a single depth is a range of one point",
      depth() == (2.0, 2.0, "m") and slider.maximum() == 0, repr(depth()))

panel.set_depth_extent(None)
check("no depth in the rows: hidden, and no filter",
      not panel.depth_check.isVisible() and depth() is None)
panel.set_depth_extent(SEDIMENT)
panel.clear_result_filters()
check("a new report forgets the range", not panel.depth_box.isVisible()
      and depth() is None)

# --- the unit row ------------------------------------------------------------
units_told = []
panel.changed.connect(lambda: units_told.append(panel.selection().units))
model = panel.unit_combo.model()
check("no units offered: the row is hidden", not panel.unit_combo.isVisible())
panel.set_units([("mg/kg", 40210), ("mg/L", 87), ("", 3)])
check("offered with counts, so the stray unit shows itself",
      panel.unit_combo.isVisible() and panel.unit_combo.isEnabled()
      and [model.item(i).text() for i in range(model.rowCount())]
      == ["mg/kg  (40,210)", "mg/L  (87)", "(no unit)  (3)"],
      repr([model.item(i).text() for i in range(model.rowCount())]))
check("nothing ticked keeps every unit", panel.selection().units == [])
del units_told[:]
panel._on_unit_item_pressed(model.index(0, 0))
check("ticking one keeps only it, and says so",
      units_told == [["mg/kg"]]
      and "Only results in mg/kg" in panel.warning_label.text(),
      repr((units_told, panel.warning_label.text())))
panel._on_unit_item_pressed(model.index(2, 0))
check("no unit is a choice of its own",
      panel.selection().units == ["mg/kg", ""]
      and panel.unit_combo.lineEdit().text() == "mg/kg, (no unit)",
      repr((panel.selection().units, panel.unit_combo.lineEdit().text())))
del units_told[:]
panel.set_units([("mg/kg", 40210), ("mg/L", 87), ("", 3)])
check("the same offer again changes nothing and says nothing",
      units_told == [] and panel.selection().units == ["mg/kg", ""])
panel.set_units([("mg/kg", 120), ("ug/kg", 9)])
check("another matrix: a ticked unit it has stays ticked, the rest go",
      panel.selection().units == ["mg/kg"] and units_told == [["mg/kg"]],
      repr((panel.selection().units, units_told)))
panel.set_units([("ug/L", 500)])
check("a matrix without the ticked unit unticks it, rather than hide a "
      "filter that keeps nothing",
      panel.selection().units == [] and not panel.unit_combo.isEnabled()
      and panel.unit_combo.lineEdit().text() == "ug/L",
      repr((panel.selection().units, panel.unit_combo.lineEdit().text())))
panel.set_units([("mg/kg", 1), ("mg/L", 1)])
panel.set_selected_units(["mg/L"])
panel.clear_result_filters()
check("a new report forgets the units", not panel.unit_combo.isVisible()
      and panel.selection().units == [])

panel.close()

# --- the dialog: rows in, range offered, rows out ---------------------------
# The panel's half alone cannot show that the dialog offers the range for the
# chosen matrix, or that a tick reaches the rows. A report with the parity
# fixture's rows - water with no depth, sediment cores in cm, m, ft and two
# units that cannot be converted - goes through the same calls a fetch makes.
from test_offline import FakeIface                            # noqa: E402
from test_store_parity import FULL_HEADERS, awkward_rows      # noqa: E402
from equis_loader.dialog import EquisDialog                   # noqa: E402

dialog = EquisDialog(FakeIface())
table = rd.ReportTable(FULL_HEADERS, awkward_rows())
dialog._report_table = table
dialog._apply_row_scope()
dialog._sync_selection_panel(table)
sel = dialog.selection_panel
check("dialog: the depth row is offered for a report with depths",
      not sel.depth_check.isHidden() and sel._depth is not None)
check("dialog: over every matrix, the range is the cores' range, in cm",
      (sel._depth or {}).get("unit") == "cm"
      and (sel.depth_top.value(), sel.depth_bottom.value()) == (0.0, 60.96),
      repr((sel._depth, sel.depth_top.value(), sel.depth_bottom.value())))


def analytes():
    return sorted(a["chemical"] for a in dialog._analysis.analytes())


every = analytes()
sel.depth_check.setChecked(True)
check("dialog: ticking it re-scopes the rows the dialog asks",
      table.scope[3] == (0.0, 60.96, "cm"), repr(table.scope))
check("dialog: only chemicals sampled at a depth are left",
      analytes() == ["Lead"] and len(every) > 1, repr((analytes(), every)))
sel.depth_top.setValue(30.0)
sel.depth_bottom.setValue(50.0)
QTest.qWait(DEPTH_SETTLE_MS + 150)
rows, _ = table.rows_for("Lead", "7439-92-1")
check("dialog: typed bounds reach the rows once they settle",
      sorted(r["value"] for r in rows) == [13.0], repr(rows))
provenance = dialog._provenance({"chemical": "Lead", "cas_rn": "7439-92-1"},
                                rd.MAXIMUM)
check("dialog: a layer's provenance names the depth range",
      "depth 30 to 50 cm (whole interval inside)" in provenance, provenance)
sel.depth_check.setChecked(False)
check("dialog: unticking gives every row back",
      table.scope[3] is None and analytes() == every, repr(table.scope))

sel.set_selected_matrices(["WG"])
check("dialog: a matrix with no depth hides the row",
      sel.depth_check.isHidden() and sel.selection().depth is None)
sel.set_selected_matrices(["SE"])
check("dialog: a matrix with depths brings it back",
      not sel.depth_check.isHidden())

# The reported case: a solid's few leachate results in mg/L make the unit
# guard refuse the map, and ticking the solid's unit is the way through.
LEAD = [{"chemical": "Lead", "cas_rn": "7439-92-1"}]
check("dialog: the sediment's units are offered, commonest first",
      sel._unit_offer == [("mg/kg", 10), ("mg/L", 2), ("", 1), ("MG/KG", 1)],
      repr(sel._unit_offer))
check("dialog: with the leachate in, the guard refuses a maximum",
      dialog._confirm_units(LEAD, rd.MAXIMUM, ask=False) is False)
sel.set_selected_units(["mg/kg"])
check("dialog: ticking mg/kg re-scopes the rows",
      table.scope[4] == ("mg/kg",), repr(table.scope))
check("dialog: and the guard lets the maximum through",
      dialog._confirm_units(LEAD, rd.MAXIMUM, ask=False) is True)
provenance = dialog._provenance(LEAD[0], rd.MAXIMUM)
check("dialog: a layer's provenance names the unit kept",
      "unit mg/kg" in provenance, provenance)
sel.set_selected_matrices(["WG"])
check("dialog: water's units replace them, and the tick goes",
      [u for u, _ in sel._unit_offer] == ["ug/L", "mg/L"]
      and table.scope[4] == (), repr((sel._unit_offer, table.scope)))
dialog.close()

# Soil and groundwater in feet, as the depth row was reported: borings to
# 200 ft in the water, soil no deeper than 2 ft. The range follows the
# matrix, and the soil's 0.33 ft boundary is a stop of its own.
from test_store_parity import row                             # noqa: E402


def sampled(loc, matrix, d0, d1, value, unit="mg/kg"):
    return row(loc, "Lead", "7439-92-1", "06/15/2020", value, value, "0.5",
               unit, "Y", matrix=matrix, d0=d0, d1=d1, dunit="ft")


feet = rd.ReportTable(FULL_HEADERS, [
    sampled("SB-01", "SO", "0", "0.33", "5"),
    sampled("SB-01", "SO", "0.33", "1", "6"),
    sampled("SB-02", "SO", "1", "2", "7"),
    sampled("SB-02", "SO", "0", "0.33", "0.01", unit="mg/L"),
    sampled("MW-01", "WG", "180", "200", "3", unit="ug/L"),
    sampled("MW-02", "WG", "40", "50", "4", unit="ug/L"),
])
dialog = EquisDialog(FakeIface())
dialog._report_table = feet
dialog._apply_row_scope()
dialog._sync_selection_panel(feet)
sel = dialog.selection_panel
check("feet: over both matrices the range reaches the wells' 200 ft",
      (sel.depth_top.value(), sel.depth_bottom.value()) == (0.0, 200.0),
      repr((sel.depth_top.value(), sel.depth_bottom.value())))
sel.set_selected_matrices(["SO"])
check("feet: soil alone is 0 to 2 ft",
      (sel.depth_top.value(), sel.depth_bottom.value(),
       sel.depth_unit_label.text()) == (0.0, 2.0, "ft"),
      repr((sel.depth_top.value(), sel.depth_bottom.value())))
check("feet: and its slider stops at 0, 0.33, 1 and 2 ft",
      sel._depth_ticks == [0.0, 0.33, 1.0, 2.0], repr(sel._depth_ticks))
check("feet: the soil's units, leachate included",
      sel._unit_offer == [("mg/kg", 3), ("mg/L", 1)], repr(sel._unit_offer))
sel.depth_check.setChecked(True)
sel.depth_slider.setRange(0, 1)
QTest.qWait(DEPTH_SETTLE_MS + 150)
sel.set_selected_units(["mg/kg"])
rows, _ = feet.rows_for("Lead", "7439-92-1", matrix=("SO",))
check("feet: the top 0.33 ft in mg/kg is one sample",
      sorted(r["value"] for r in rows) == [5.0], repr(rows))
sel.set_selected_matrices(["WG"])
check("feet: back to the wells, back to 200 ft",
      (sel.depth_top.minimum(), sel.depth_bottom.maximum()) == (40.0, 200.0),
      repr((sel.depth_top.minimum(), sel.depth_bottom.maximum())))
dialog.close()

print("")
if FAILURES:
    print("%d failure(s):" % len(FAILURES))
    for name in FAILURES:
        print("  %s" % name)
    app.exitQgis()
    raise SystemExit(1)
print("The depth filter's controls behave.")
app.exitQgis()
