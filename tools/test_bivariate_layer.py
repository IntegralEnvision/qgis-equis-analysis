r"""The scatter's map layer (layer_builder.build_bivariate_layer). Needs QGIS.

    "C:\Program Files\QGIS 4.2.1\bin\python-qgis.bat" tools\test_bivariate_layer.py

Memory layers need no GUI, so this runs headless. It checks that stations
land on their location points, that each carries the class bivariate.py
gives it, and that the legend is the nine palette colours (high-high first)
plus the three grey non-detect classes in the scatter's symbols.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from qgis.core import QgsApplication  # noqa: E402

FAILED = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - %s" % (detail,)) if detail != "" else ""))
    if not condition:
        FAILED.append(label)


def feature_sets(codes):
    fields = [{"name": "code", "type": "esriFieldTypeString"},
              {"name": "name", "type": "esriFieldTypeString"}]
    feats = [{"attributes": {"code": c, "name": "Station " + c},
              "geometry": {"x": -43.0 + i * 0.01, "y": -20.0}}
             for i, c in enumerate(codes)]
    return [({"fields": fields, "spatialReference": {"wkid": 4326}}, feats)]


def main():
    from equis_loader import bivariate as bv
    from equis_loader.layer_builder import build_bivariate_layer

    breaks = {"x": {"breaks": [10.0, 100.0], "scale": "log10"},
              "y": {"breaks": [1.0, 5.0], "scale": "log10"}}
    stations = [
        # Case-insensitive join: the report and the service disagree on case.
        {"location": "mw-1", "sample": "S1", "date": None, "x": 150.0, "y": 0.5,
         "x_detected": True, "y_detected": True, "samples_here": 3},
        {"location": "MW-2", "sample": "S2", "date": None, "x": 10.0, "y": 5.0,
         "x_detected": True, "y_detected": True, "samples_here": 1},
        {"location": "MW-3", "sample": "S3", "date": None, "x": 4.0, "y": 9.0,
         "x_detected": True, "y_detected": False, "samples_here": 1},
        {"location": "MW-9", "sample": "S9", "date": None, "x": 1.0, "y": 1.0,
         "x_detected": False, "y_detected": False, "samples_here": 1},
    ]
    layer, placed, unplaced = build_bivariate_layer(
        "test", feature_sets(["MW-1", "MW-2", "MW-3", "MW-4"]), stations,
        {"chemical": "Mn"}, {"chemical": "Pb"}, breaks)
    check("layer built", layer is not None and layer.isValid())
    check("three stations placed; MW-9 had no point", (placed, unplaced)
          == (3, ["MW-9"]), (placed, unplaced))
    check("a location with no station is not drawn (MW-4)",
          layer.featureCount() == 3, layer.featureCount())
    keys = {f["code"]: f["bv_key"] for f in layer.getFeatures()}
    # MW-1: x 150 > 100 -> 3; y 0.5 <= 1 -> 1. MW-2 sits ON both breaks:
    # 10 <= 10 -> 1, 5 <= 5 -> 2. MW-3: Pb not detected -> grey class.
    check("classes match bivariate.py, breaks inclusive",
          keys == {"MW-1": "3-1", "MW-2": "1-2", "MW-3": "x_only"}, keys)
    renderer = layer.renderer()
    cats = renderer.categories()
    check("twelve categories: nine colours then three grey classes",
          [c.value() for c in cats][-3:] == ["x_only", "y_only", "neither"]
          and len(cats) == 12, [c.value() for c in cats])
    check("first category is high-high", cats[0].value() == "3-3", cats[0].value())
    colours = {c.value(): c.symbol().color().name() for c in cats}
    check("the nine colours are the palette's",
          all(colours[k] == bv.PALETTE[k] for k in bv.PALETTE), colours)
    shapes = {c.value(): c.symbol().symbolLayer(0).shape().name
              if hasattr(c.symbol().symbolLayer(0).shape(), "name")
              else c.symbol().symbolLayer(0).shape() for c in cats[-3:]}
    check("non-detect classes keep distinct shapes", len(set(map(str, shapes.values()))) == 3,
          shapes)
    outlines = {c.value(): c.symbol().symbolLayer(0).strokeColor().name()
                for c in cats[-3:]}
    check("non-detect classes are hollow and outlined grey",
          all(v == bv.GREY for v in outlines.values())
          and all(c.symbol().symbolLayer(0).fillColor().alpha() == 0
                  for c in cats[-3:-1]), outlines)
    labels = [c.label() for c in cats]
    check("labels name analytes and break ranges",
          labels[0] == "Mn > 100, Pb > 5" and "Mn detected, Pb not" in labels, labels[:2])
    attrs = next(f for f in layer.getFeatures() if f["code"] == "MW-1")
    check("the feature says which sample it is, and how many were there",
          attrs["bv_sample"] == "S1" and attrs["bv_samples_here"] == 3
          and attrs["bv_x_analyte"] == "Mn", (attrs["bv_sample"], attrs["bv_samples_here"]))


if __name__ == "__main__":
    app = QgsApplication([], False)
    app.initQgis()
    try:
        main()
    finally:
        app.exitQgis()
    print("\n%s" % ("ALL BIVARIATE LAYER TESTS PASSED" if not FAILED
                    else "%d FAILED" % len(FAILED)))
    sys.exit(1 if FAILED else 0)
