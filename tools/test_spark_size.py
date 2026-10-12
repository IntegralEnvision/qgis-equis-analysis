"""The sparkline marker size range: clamping, proportion, and what reaches QGIS.

Needs QGIS but no network:

  "C:\\Program Files\\QGIS 4.0.2\\bin\\python-qgis.bat" tools\\test_spark_size.py

The markers are sized on the ground and clamped in millimetres, so these two
clamps are the whole of how big a marker can get on screen. An inverted or
zeroed pair does not raise - QGIS just draws nothing - so the clamping is
worth asserting rather than eyeballing on a map.
"""

import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", ""), True)
app = QgsApplication([], False)
app.initQgis()

from equis_loader import layer_builder as lb  # noqa: E402
from equis_loader import sparkline as sp  # noqa: E402


def series(base, n=5):
    return [
        {"date": datetime.datetime(2019 + j, 1, 1), "value": base + j * 2.0,
         "unit": "ug/L", "detect": "Y", "at_limit": False}
        for j in range(n)
    ]


def build_layer(count=12):
    layer = QgsVectorLayer("Point?crs=EPSG:3857", "size", "memory")
    provider = layer.dataProvider()
    provider.addAttributes([QgsField("code", QMetaType.Type.QString)])
    layer.updateFields()
    feats = []
    for i in range(count):
        f = QgsFeature(layer.fields())
        f.setAttributes(["MW-%03d" % i])
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(i * 120, i * 90)))
        feats.append(f)
    provider.addFeatures(feats)
    layer.updateExtents()
    return layer


def applied_clamps(layer):
    """The (min, max) millimetre clamps the layer's sparkline symbol carries."""
    renderer = layer.renderer()
    for rule in renderer.rootRule().children():
        symbol = rule.symbol()
        if symbol is None:
            continue
        sl = symbol.symbolLayer(0)
        scale = getattr(sl, "sizeMapUnitScale", None)
        if scale is None:
            continue
        unit_scale = sl.sizeMapUnitScale()
        if unit_scale.minSizeMMEnabled and unit_scale.maxSizeMMEnabled:
            return unit_scale.minSizeMM, unit_scale.maxSizeMM
    return None


def main():
    # --- the clamp helper, which every path funnels through -----------
    check("clamp: defaults survive untouched",
          lb.clamp_spark_range(4.0, 16.0) == (4.0, 16.0))
    check("clamp: a crossed pair is straightened, not rejected",
          lb.clamp_spark_range(16.0, 4.0) == (4.0, 16.0))
    check("clamp: zero is lifted to the floor",
          lb.clamp_spark_range(0.0, 16.0)[0] == lb.SPARK_LIMIT_MIN_MM)
    check("clamp: above the ceiling is pulled back",
          lb.clamp_spark_range(4.0, 999.0)[1] == lb.SPARK_LIMIT_MAX_MM)
    check("clamp: garbage falls back to the defaults",
          lb.clamp_spark_range("wide", None) == (lb.SPARK_MIN_MM,
                                                 lb.SPARK_MAX_MM))
    check("limit: the ceiling is half again the old maximum",
          abs(lb.SPARK_LIMIT_MAX_MM - 24.0) < 1e-9,
          "%g mm" % lb.SPARK_LIMIT_MAX_MM)

    # --- the range as the renderer actually receives it ---------------
    layer = build_layer()
    markers = sp.embed_sparklines(
        {"MW-%03d" % i: series(float(i % 4) + 1.0) for i in range(12)},
        analyte="Benzene")

    lb.style_with_sparklines(layer, markers, location_field="code")
    default_clamps = applied_clamps(layer)
    check("default: an unset range draws what it always drew",
          default_clamps is not None
          and abs(default_clamps[0] - lb.SPARK_MIN_MM) < 1e-6
          and abs(default_clamps[1] - lb.STATIC_SPARK_NOMINAL_MM) < 1e-6,
          "%s" % (default_clamps,))

    lb.style_with_sparklines(layer, markers, location_field="code",
                             size_range=(6.0, 24.0))
    wide = applied_clamps(layer)
    check("range: the chosen minimum reaches the symbol",
          wide is not None and abs(wide[0] - 6.0) < 1e-6, "%s" % (wide,))
    check("range: the maximum scales up and stays inside the ceiling",
          wide is not None and default_clamps[1] < wide[1] <= lb.SPARK_LIMIT_MAX_MM,
          "%g -> %g mm" % (default_clamps[1], wide[1]))

    lb.style_with_sparklines(layer, markers, location_field="code",
                             size_range=(24.0, 6.0))
    crossed = applied_clamps(layer)
    check("range: a crossed pair still yields a drawable symbol",
          crossed is not None and crossed[0] < crossed[1], "%s" % (crossed,))

    # --- the rolling marker keeps its edge over the static one --------
    roll_layer = build_layer()
    by_analyte = [(name, markers) for name in ("Benzene", "Toluene")]
    lb.style_with_rolling_sparklines(roll_layer, by_analyte,
                                     location_field="code",
                                     size_range=(6.0, 24.0))
    rolled = applied_clamps(roll_layer)
    check("roll: the captioned marker stays the larger of the two",
          rolled is not None and rolled[1] > wide[1],
          "roll %g mm vs static %g mm" % (rolled[1], wide[1]))
    check("roll: and is still inside the ceiling",
          rolled is not None and rolled[1] <= lb.SPARK_LIMIT_MAX_MM,
          "%g mm" % rolled[1])

    print("\n%s" % ("ALL SIZE TESTS PASSED" if not FAILURES
                    else "FAILURES: %s" % ", ".join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    code = main()
    app.exitQgis()
    sys.exit(code)
