"""Tests for the overlay's data shaping.

No QGIS and no network: `series_payload` is deliberately free of QGIS
imports so the part that decides what a point LOOKS like on screen can be
exercised with a plain interpreter, the way report_data and cache already are.

    python tools\\test_overlay_payload.py

The rule under test is the one that is easy to get wrong and impossible to
see: a value plotted at a detection limit is a non-detect whatever DETECT_FLAG
says. timeseries._is_detected encodes it for the dock plot; if the overlay
disagreed, one point would be drawn solid in one view and hollow in the other
and the plugin would be contradicting itself on screen.
"""

import datetime
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


def _load_overlay():
    """Import overlay.py without its QGIS dependencies.

    The module imports qgis at the top, so stub just enough for the import to
    succeed. Only the QGIS-free functions are then touched.
    """
    import types

    for name in ("qgis", "qgis.core", "qgis.PyQt", "qgis.PyQt.QtCore",
                 "qgis.PyQt.QtGui", "qgis.PyQt.QtWebChannel",
                 "qgis.PyQt.QtWebEngineWidgets"):
        module = types.ModuleType(name)
        module.__path__ = []
        sys.modules.setdefault(name, module)

    core = sys.modules["qgis.core"]
    for attr in ("Qgis", "QgsMessageLog", "QgsPointXY"):
        setattr(core, attr, type(attr, (), {}))
    qtcore = sys.modules["qgis.PyQt.QtCore"]
    for attr in ("QEvent", "QObject", "QTimer", "QUrl", "Qt"):
        setattr(qtcore, attr, type(attr, (), {}))
    qtcore.pyqtSignal = lambda *a, **k: None
    qtcore.pyqtSlot = lambda *a, **k: (lambda fn: fn)
    sys.modules["qgis.PyQt.QtGui"].QColor = type("QColor", (), {})
    sys.modules["qgis.PyQt.QtWebChannel"].QWebChannel = type("QWebChannel", (), {})
    sys.modules["qgis.PyQt.QtWebEngineWidgets"].QWebEngineView = type(
        "QWebEngineView", (), {})

    path = os.path.join(ROOT, "tools", "mosaic_poc", "overlay.py")
    spec = importlib.util.spec_from_file_location("overlay_poc", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def point(value, detect, at_limit, when=None):
    return {
        "date": when or datetime.date(2020, 1, 1),
        "value": value,
        "unit": "ug/L",
        "detect": detect,
        "at_limit": at_limit,
    }


def main():
    overlay = _load_overlay()
    shape = overlay.series_payload

    # --- the detect rule, which is the whole reason this file exists -----
    cases = [
        ("detected Y",            point(5.0, "Y", False),  True),
        ("detected lowercase y",  point(5.0, "y", False),  True),
        ("detected YES",          point(5.0, "YES", False), True),
        ("non-detect N",          point(1.0, "N", True),   False),
        # The trap: the flag says detected, but the value IS the limit.
        ("at_limit beats flag",   point(1.0, "Y", True),   False),
        ("blank flag",            point(5.0, "", False),   False),
        ("missing flag",          {"date": datetime.date(2020, 1, 1),
                                   "value": 5.0},          False),
    ]
    for label, raw, expected in cases:
        got = shape("L1", "Benzene", [raw])["points"][0]["detected"]
        check("detect: %s" % label, got is expected,
              "expected %s, got %s" % (expected, got))

    # --- structure -------------------------------------------------------
    when = datetime.datetime(2019, 5, 4, 12, 30)
    out = shape("MW-01", "Toluene", [point(3.0, "Y", False, when)])
    check("id and name carried", out["id"] == "MW-01" and out["name"] == "MW-01")
    check("chemical carried", out["chemical"] == "Toluene")
    check("date is ISO text", out["points"][0]["t"].startswith("2019-05-04"),
          out["points"][0]["t"])
    check("value carried", out["points"][0]["v"] == 3.0)

    # Unit falls back to the first point that has one, matching what the dock
    # plot does, so a series whose first row lost its unit is still labelled.
    mixed = [dict(point(1.0, "Y", False), unit=""), point(2.0, "Y", False)]
    check("unit falls back to first present",
          shape("L", "C", mixed)["unit"] == "ug/L")
    check("explicit unit wins",
          shape("L", "C", mixed, unit="mg/kg")["unit"] == "mg/kg")

    # --- degenerate input, which the panel really does produce -----------
    empty = shape("L", "C", [])
    check("empty series is a valid payload",
          empty["points"] == [] and empty["unit"] == "")
    check("None series is a valid payload", shape("L", "C", None)["points"] == [])

    # --- the payload must survive the channel ----------------------------
    import json
    demo = overlay.make_demo_series(n_locations=3, n_points=5)
    text = json.dumps([d["payload"] for d in demo])
    back = json.loads(text)
    check("demo data is JSON round-trippable", len(back) == 3)
    check("demo carries both detects and non-detects",
          any(p["detected"] for s in back for p in s["points"])
          and any(not p["detected"] for s in back for p in s["points"]),
          "a demo with no non-detects would not exercise the drawing")
    check("demo is deterministic",
          json.dumps([d["payload"] for d in
                      overlay.make_demo_series(n_locations=3, n_points=5)]) == text)

    print("")
    if FAILURES:
        print("%d failure(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all overlay payload tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
