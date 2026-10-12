"""The brush-to-map policy, without a browser.

The controller is the twenty lines that turn tested parts into a feature, so
it is worth testing on its own: the page is replaced by calling `_on_brush`
directly with the payload the bridge would deliver.

Needs QGIS for real layers:

    "C:\\Program Files\\QGIS 4.2.1\\bin\\python-qgis.bat" tools\\test_linked_views.py
"""
import os
import sys

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
    from qgis.core import (QgsApplication, QgsFeature, QgsGeometry,
                           QgsPointXY, QgsProject, QgsVectorLayer)
except ImportError as exc:
    print("needs QGIS: %s" % exc)
    print("SKIPPED")
    raise SystemExit(0)

QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", ""), True)
app = QgsApplication([], False)
app.initQgis()

from equis_loader.linked_views import LinkedViewsController  # noqa: E402


def make_layer(name="Nickel", codes=None):
    codes = codes or ["MW-%d" % i for i in range(1, 21)]
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=code:string"
                           "&field=value:double", name, "memory")
    provider = layer.dataProvider()
    features = []
    for index, code in enumerate(codes):
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(
            QgsPointXY(index * 0.01, index * 0.01)))
        feature.setAttributes([code, index * 1.5])
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer, False)
    return layer


layer = make_layer()
status = []
controller = LinkedViewsController(lambda: None, lambda: layer,
                                   on_status=status.append)

# --- a brush highlights exactly what it named --------------------------------
controller._on_brush({"from": 0, "to": 10, "label": "0 - 10 mg/kg",
                      "locations": ["MW-1", "MW-3", "MW-5"]})
check("the highlight holds the brushed locations",
      controller.brush is not None
      and controller.brush.last_count == 3,
      str(controller.brush.last_count if controller.brush else None))
check("the brush's own words reach the status line",
      status and "0 - 10 mg/kg" in status[-1], str(status[-1:]))

# The base must be untouched - that is what makes the arrangement fast.
check("the base layer keeps every feature", layer.featureCount() == 20)
check("the base layer is not filtered", layer.subsetString() == "")
check("the base layer has no selection", layer.selectedFeatureCount() == 0)

# --- a location the layer does not have is simply not drawn ------------------
# The store can know a location the map cannot place - see the unplaced
# locations the panel already reports. That must not raise.
controller._on_brush({"from": 0, "to": 5, "label": "narrow",
                      "locations": ["MW-2", "NOT-ON-THE-MAP"]})
check("an unmappable location is skipped, not fatal",
      controller.brush.last_count == 1,
      str(controller.brush.last_count))

# --- clearing puts the map back ----------------------------------------------
controller._on_brush(None)
check("clearing empties the highlight", controller.brush.last_count == 0)
check("clearing says so rather than leaving stale words",
      status[-1] == "", repr(status[-1]))

# --- an empty selection is a cleared brush, not an error ---------------------
controller._on_brush({"from": 99, "to": 100, "label": "nothing here",
                      "locations": []})
check("a brush matching nothing clears rather than raising",
      controller.brush.last_count == 0)
check("and still says what the brush was",
      "nothing here" in status[-1], repr(status[-1]))

# --- the highlight follows the CURRENT layer ---------------------------------
# The panel rebuilds its layer whenever the map is redrawn. A highlight
# belonging to a removed layer would draw over whatever replaced it, which
# reads as the brush selecting the wrong points.
first_brush_layer = controller.brush.layer.id()
replacement = make_layer("Nickel (rebuilt)")
current = {"layer": replacement}
controller._layer_for = lambda: current["layer"]
controller._on_brush({"from": 0, "to": 10, "label": "after rebuild",
                      "locations": ["MW-1", "MW-2"]})
check("a new base layer gets a new highlight",
      controller.brush.layer.id() != first_brush_layer)
check("the new highlight holds the new brush",
      controller.brush.last_count == 2,
      str(controller.brush.last_count))

# --- no layer at all is a no-op, not a crash ---------------------------------
current["layer"] = None
try:
    controller._on_brush({"from": 0, "to": 1, "locations": ["MW-1"]})
    survived = True
except Exception as exc:  # noqa: BLE001
    survived = False
    print("   raised: %r" % (exc,))
check("brushing with no layer is harmless", survived)

# --- a wide brush carries the advice with it ---------------------------------
big_codes = ["L%05d" % i for i in range(25000)]
big = make_layer("big", big_codes)
current["layer"] = big
controller._on_brush({"from": 0, "to": 1e9, "label": "everything",
                      "locations": big_codes})
check("an over-wide brush is drawn but flagged",
      controller.brush.last_count == 25000 and controller.brush.overfull,
      str(controller.brush.last_count))
check("the advice reaches the user with the brush's words",
      "everything" in status[-1] and "narrow" in status[-1].lower(),
      str(status[-1])[:110])

# --- prepare() moves the index cost off the first drag -----------------------
# The index is O(dataset) and every brush after it is O(selection): 1.3 s
# against 11 ms at 250,000 features. Lazily, that whole cost lands on the
# user's first drag - the one moment US-1 budgets at 100 ms.
current["layer"] = make_layer("prepared", ["P-%d" % i for i in range(50)])
fresh = LinkedViewsController(lambda: None, lambda: current["layer"])
check("the index is empty before prepare", not fresh._by_code)
fresh.prepare()
check("prepare builds the index", len(fresh._by_code) == 50,
      str(len(fresh._by_code)))
fresh.prepare()
check("prepare twice is a no-op, not a rebuild", len(fresh._by_code) == 50)
fresh.close()

controller.close()
check("closing removes the highlight layer",
      controller.brush is None and controller.views is None)

print()
if FAILURES:
    print("FAILED: %s" % ", ".join(FAILURES))
    raise SystemExit(1)
print("A brush reaches the map, and the map keeps its base intact.")
