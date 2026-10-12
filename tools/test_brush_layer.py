"""The highlight layer that follows a brush.

Needs QGIS for real layers, but no web view and no browser:

    "C:\\Program Files\\QGIS 4.2.1\\bin\\python-qgis.bat" tools\\test_brush_layer.py

The arrangement under test was chosen by measurement, not taste
(tools/mosaic_poc/README.md): two layers cost 49.8 ms at 250,000 points where
selectByIds costs 2.1 s, because the unchanged base is served from the canvas
cache. What this suite checks is that the implementation keeps the properties
that makes true - chiefly that the BASE IS NEVER TOUCHED.
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

from equis_loader.brush_layer import BrushLayer, READABLE_HIGHLIGHT  # noqa: E402


def base_layer(count=500):
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=fid:integer"
                           "&field=value:double", "Nickel", "memory")
    provider = layer.dataProvider()
    features = []
    for index in range(count):
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(
            QgsPointXY(index * 0.001, index * 0.001)))
        feature.setAttributes([index, index * 1.5])
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer, False)
    return layer


base = base_layer()
brush = BrushLayer(base)

# --- the base must never be touched -----------------------------------------
# This is the whole reason the arrangement is fast: an unchanged base is
# served from the canvas cache. A highlight that edited, filtered or
# selected on the base would dirty it and cost 597 ms a frame at scale.
before = base.featureCount()
subset_before = base.subsetString()
selected_before = base.selectedFeatureCount()

wanted = [f for f in base.getFeatures() if f["value"] < 100]
shown = brush.show(wanted)
check("the highlight holds what it was given", shown == len(wanted),
      "%d of %d" % (shown, len(wanted)))
check("the base keeps every feature", base.featureCount() == before,
      "%d vs %d" % (base.featureCount(), before))
check("the base is not filtered", base.subsetString() == subset_before,
      repr(base.subsetString()))
check("the base has no selection", base.selectedFeatureCount() == selected_before,
      "%d selected" % base.selectedFeatureCount())

# --- it is a separate layer, above the base ---------------------------------
check("a highlight layer exists", brush.layer is not None
      and brush.layer.isValid())
check("it is not the base", brush.layer.id() != base.id())
check("it carries the base's fields",
      [f.name() for f in brush.layer.fields()]
      == [f.name() for f in base.fields()],
      str([f.name() for f in brush.layer.fields()]))
check("it is named so the legend explains itself",
      "brushed" in (brush.layer.name() or "").lower(), brush.layer.name())

# --- moving the brush replaces, never accumulates ---------------------------
narrower = [f for f in base.getFeatures() if f["value"] < 30]
brush.show(narrower)
check("a new brush replaces the old highlight",
      brush.layer.featureCount() == len(narrower),
      "%d, expected %d" % (brush.layer.featureCount(), len(narrower)))

# --- clearing returns the map to the full layer -----------------------------
brush.clear()
check("clearing empties the highlight", brush.layer.featureCount() == 0,
      str(brush.layer.featureCount()))
check("clearing leaves the base whole", base.featureCount() == before)
check("the base is STILL unfiltered after a clear",
      base.subsetString() == subset_before)

# --- an unreadable brush says so --------------------------------------------
# At 20% of 250,000 the highlight costs 397 ms and 50,000 markers is not a
# picture anyone reads. The honest response is a message, not a slow frame.
check("a normal brush offers no advice", brush.advice() is None)

big = base_layer(READABLE_HIGHLIGHT + 50)
wide = BrushLayer(big)
wide.show(list(big.getFeatures()))
check("an over-wide brush is flagged", wide.overfull is True,
      "%d features" % wide.last_count)
advice = wide.advice()
check("the advice names the number and what to do",
      advice and "narrow" in advice.lower()
      and "{:,}".format(wide.last_count) in advice,
      str(advice))
wide.remove()

# --- teardown ---------------------------------------------------------------
brush.remove()
check("removing takes the layer out of the project",
      brush.layer is None
      and all("brushed" not in (lyr.name() or "").lower()
              for lyr in QgsProject.instance().mapLayers().values()))

print()
if FAILURES:
    print("FAILED: %s" % ", ".join(FAILURES))
    raise SystemExit(1)
print("The brush highlights without ever touching the base layer.")
