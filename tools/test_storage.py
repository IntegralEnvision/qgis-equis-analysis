"""Layer storage: GeoPackage persistence and embedded sparkline markers.

Needs QGIS but no network:

  "C:\\Program Files\\QGIS 4.0.2\\bin\\python-qgis.bat" tools\\test_storage.py

The point of the feature is that a saved project reopens with its data, so
these tests write a project, clear it, and read it back cold rather than only
checking that a file appeared.
"""

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QSize  # noqa: E402
from qgis.PyQt.QtGui import QColor  # noqa: E402

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
from equis_loader import settings as settings_store  # noqa: E402
from equis_loader import sparkline as sp  # noqa: E402


def sample_pages(count=5):
    """Location pages in the shape the builders consume."""
    features = []
    for i in range(count):
        features.append({
            "attributes": {"code": "MW-%d" % i, "type": "DOMW"},
            "geometry": {"x": 500000.0 + i * 25, "y": 4900000.0 + i * 25},
        })
    return [({
        "fields": [{"name": "code", "type": "esriFieldTypeString"},
                   {"name": "type", "type": "esriFieldTypeString"}],
        "spatialReference": {"wkid": 0, "latestWkid": 26917},
        "geometryType": "esriGeometryPoint",
    }, features)]


def sample_summary(count=5):
    import datetime
    return {
        "MW-%d" % i: {
            "location": "MW-%d" % i, "value": 1.5 + i, "unit": "ug/L",
            "date": datetime.datetime(2020, 1, 1 + i), "detect": "Y",
            "at_limit": False, "count": 3, "detect_count": 3,
        }
        for i in range(count)
    }


def painted_pixels(layer, extent):
    """How much ink a layer puts on a canvas, to prove symbols really render."""
    settings = QgsMapSettings()
    settings.setLayers([layer])
    settings.setBackgroundColor(QColor(255, 255, 255))
    settings.setOutputSize(QSize(400, 300))
    settings.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:26917"))
    settings.setExtent(extent)
    job = QgsMapRendererParallelJob(settings)
    job.start()
    job.waitForFinished()
    image = job.renderedImage()
    count = 0
    for x in range(0, image.width(), 2):
        for y in range(0, image.height(), 2):
            colour = QColor(image.pixel(x, y))
            if (colour.red(), colour.green(), colour.blue()) != (255, 255, 255):
                count += 1
    return count


def main():
    tmp = tempfile.mkdtemp(prefix="equis_storage_")
    try:
        pages = sample_pages()
        summary = sample_summary()
        extent = QgsRectangle(499950, 4899950, 500150, 4900150)

        # --- memory storage keeps today's behaviour ---------------------
        mem_layer, matched, unmatched, warning = lb.build_joined_layer(
            "Site - Benzene", pages, summary, "Benzene",
            fallback_wkid=26917, storage=lb.MEMORY,
        )
        check("storage: memory layers still build",
              mem_layer is not None and mem_layer.featureCount() == 5
              and matched == 5 and not warning)
        check("storage: memory layers use the memory provider",
              mem_layer.dataProvider().name() == "memory",
              mem_layer.dataProvider().name())

        # --- geopackage storage ------------------------------------------
        gpkg = os.path.join(tmp, "project_equis.gpkg")
        gpkg_layer, matched, unmatched, warning = lb.build_joined_layer(
            "Site - Benzene (most recent)", pages, summary, "Benzene",
            fallback_wkid=26917, storage=lb.GEOPACKAGE, gpkg_path=gpkg,
        )
        check("storage: geopackage write reports no warning",
              not warning, warning)
        check("storage: the file is created", os.path.exists(gpkg))
        check("storage: the layer reads from ogr, not memory",
              gpkg_layer is not None
              and gpkg_layer.dataProvider().name() == "ogr",
              gpkg_layer.dataProvider().name() if gpkg_layer else "none")
        check("storage: features and join survive the write",
              gpkg_layer.featureCount() == 5 and matched == 5)
        check("storage: the CRS survives the write",
              gpkg_layer.crs().authid() == "EPSG:26917",
              gpkg_layer.crs().authid())
        values = sorted(f["result_value"] for f in gpkg_layer.getFeatures())
        check("storage: result values survive the write",
              values == [1.5, 2.5, 3.5, 4.5, 5.5], repr(values))

        # --- a second chemical shares the same file -----------------------
        second, _, _, warning2 = lb.build_joined_layer(
            "Site - Toluene (maximum)", pages, summary, "Toluene",
            fallback_wkid=26917, storage=lb.GEOPACKAGE, gpkg_path=gpkg,
        )
        check("storage: a second chemical writes to the same file",
              second is not None and not warning2)
        import sqlite3
        con = sqlite3.connect(gpkg)
        tables = sorted(r[0] for r in
                        con.execute("select table_name from gpkg_contents"))
        con.close()
        check("storage: both chemicals coexist in one geopackage",
              len(tables) == 2, repr(tables))
        check("storage: layer names are reduced to safe table names",
              all(t.replace("_", "").isalnum() for t in tables), repr(tables))

        # Re-mapping the same chemical must replace its table, not add one.
        again, _, _, _ = lb.build_joined_layer(
            "Site - Benzene (most recent)", pages, summary, "Benzene",
            fallback_wkid=26917, storage=lb.GEOPACKAGE, gpkg_path=gpkg,
        )
        con = sqlite3.connect(gpkg)
        after = sorted(r[0] for r in
                       con.execute("select table_name from gpkg_contents"))
        con.close()
        check("storage: re-mapping a chemical overwrites its own table",
              after == tables, repr(after))

        # --- embedded sparklines ------------------------------------------
        import datetime
        series = {
            "MW-%d" % i: [
                {"date": datetime.datetime(2019 + j, 1, 1), "value": 1.0 + j + i,
                 "unit": "ug/L", "detect": "Y", "at_limit": False}
                for j in range(4)
            ]
            for i in range(5)
        }
        markers = sp.embed_sparklines(series)
        check("storage: sparklines embed as base64 data",
              len(markers) == 5
              and all(v.startswith("base64:") for v in markers.values()))
        check("storage: no sparkline file is written anywhere",
              not any(name.endswith(".svg") for name in os.listdir(tmp)),
              repr(os.listdir(tmp)))

        drawn = lb.style_with_sparklines(gpkg_layer, markers,
                                         location_field="code")
        check("storage: sparklines apply to a geopackage layer",
              drawn == 5, str(drawn))
        ink = painted_pixels(gpkg_layer, extent)
        check("storage: embedded sparklines actually render", ink > 50,
              "%d painted px" % ink)

        # --- the real test: save, close, reopen cold ----------------------
        project_path = os.path.join(tmp, "project.qgz")
        project = QgsProject.instance()
        project.clear()
        project.addMapLayer(gpkg_layer)
        project.write(project_path)
        project.clear()

        reopened = QgsProject.instance()
        reopened.read(project_path)
        layers = list(reopened.mapLayers().values())
        check("storage: the project reopens with its layer",
              len(layers) == 1 and layers[0].isValid(),
              "%d layers" % len(layers))
        restored = layers[0]
        check("storage: the reopened layer still has its features",
              restored.featureCount() == 5, str(restored.featureCount()))
        restored_values = sorted(f["result_value"] for f in restored.getFeatures())
        check("storage: the reopened layer still has its results",
              restored_values == [1.5, 2.5, 3.5, 4.5, 5.5], repr(restored_values))
        ink_after = painted_pixels(restored, extent)
        check("storage: sparklines still render after reopening",
              ink_after > 50, "%d painted px (was %d)" % (ink_after, ink))
        reopened.clear()

        # --- a memory layer does NOT survive, which is the whole point ----
        project = QgsProject.instance()
        project.clear()
        mem_path = os.path.join(tmp, "memory.qgz")
        project.addMapLayer(mem_layer)
        project.write(mem_path)
        project.clear()
        project.read(mem_path)
        mem_layers = list(project.mapLayers().values())
        empty = (not mem_layers) or mem_layers[0].featureCount() == 0
        check("storage: a memory layer reopens empty (the problem being fixed)",
              empty,
              "%d features" % (mem_layers[0].featureCount() if mem_layers else 0))
        project.clear()

        # --- failure falls back rather than losing the join ---------------
        blocked = os.path.join(tmp, "blocked")
        with open(blocked, "w", encoding="utf-8") as handle:
            handle.write("a file where a directory is needed")
        fallback, matched_fb, _, warn_fb = lb.build_joined_layer(
            "Site - Xylene", pages, summary, "Xylene", fallback_wkid=26917,
            storage=lb.GEOPACKAGE,
            gpkg_path=os.path.join(blocked, "nested", "x.gpkg"),
        )
        check("storage: an unwritable path still returns a usable layer",
              fallback is not None and fallback.featureCount() == 5
              and matched_fb == 5)
        check("storage: the failure is reported, not swallowed",
              bool(warn_fb), warn_fb)

        # --- project path helper -------------------------------------------
        check("storage: the geopackage sits beside the project",
              settings_store.geopackage_for_project(
                  os.path.join(tmp, "sites.qgz")
              ) == os.path.join(tmp, "sites_equis.gpkg"))
        check("storage: an unsaved project has nowhere to write",
              settings_store.geopackage_for_project("") is None)

        # --- table-name sanitising ------------------------------------------
        check("storage: awkward chemical names become safe table names",
              lb.gpkg_layer_name("Site - Xylenes, m,p- (max)")
              == "site_xylenes_m_p_max",
              lb.gpkg_layer_name("Site - Xylenes, m,p- (max)"))
        check("storage: a leading digit is prefixed",
              lb.gpkg_layer_name("1,4-Dioxane").startswith("l_"),
              lb.gpkg_layer_name("1,4-Dioxane"))
        check("storage: an empty name still yields something valid",
              lb.gpkg_layer_name("") == "layer")

        # --- one marker per LOCATION, not per feature -------------------
        # An unaggregated layer has a feature per RESULT. Without
        # once_per_location every one of a boring's samples gets an
        # identical copy of the same depth profile, stacked on one
        # coordinate - which renders as a smear of repeated marks rather
        # than one clean profile.
        from qgis.PyQt.QtCore import QMetaType
        stack = QgsVectorLayer("Point?crs=EPSG:4326", "stack", "memory")
        stack.dataProvider().addAttributes([
            QgsField("code", QMetaType.Type.QString),
            QgsField("spark_svg", QMetaType.Type.QString),
        ])
        stack.updateFields()
        feats = []
        for i in range(12):
            feat = QgsFeature(stack.fields())
            feat.setAttributes(["RDO-16", None])
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(0.0, 0.0)))
            feats.append(feat)
        for i in range(3):
            feat = QgsFeature(stack.fields())
            feat.setAttributes(["OTHER", None])
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(1.0, 0.0)))
            feats.append(feat)
        stack.dataProvider().addFeatures(feats)
        stack.updateExtents()

        markers = {"RDO-16": "base64:AAAA", "OTHER": "base64:BBBB"}
        drawn = lb.style_with_sparklines(stack, markers,
                                         once_per_location=True)
        check("stack: one marker per location, not per feature",
              drawn == 2, str(drawn))
        carried = [f["code"] for f in stack.getFeatures() if f["spark_svg"]]
        check("stack: exactly one feature per location carries it",
              sorted(carried) == ["OTHER", "RDO-16"], str(sorted(carried)))
        check("stack: the other 13 features carry nothing",
              sum(1 for f in stack.getFeatures() if not f["spark_svg"]) == 13,
              str(sum(1 for f in stack.getFeatures() if not f["spark_svg"])))

        # The aggregated path is unchanged: one feature per location already,
        # so every one of them keeps its marker.
        plain = QgsVectorLayer("Point?crs=EPSG:4326", "plain", "memory")
        plain.dataProvider().addAttributes([
            QgsField("code", QMetaType.Type.QString),
            QgsField("spark_svg", QMetaType.Type.QString),
        ])
        plain.updateFields()
        rows = []
        for code in ("A", "B", "C"):
            feat = QgsFeature(plain.fields())
            feat.setAttributes([code, None])
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(0.0, 0.0)))
            rows.append(feat)
        plain.dataProvider().addFeatures(rows)
        plain.updateExtents()
        drawn2 = lb.style_with_sparklines(
            plain, {"A": "base64:A", "B": "base64:B", "C": "base64:C"})
        check("stack: the aggregated path still marks every feature",
              drawn2 == 3, str(drawn2))

        # --- long names must not collapse onto one table -------------------
        # Truncation used to cut at 60 characters flat, so on a facility with
        # a long name the dissolved and total layers of one chemical reduced
        # to the SAME table and the second silently overwrote the first in
        # the GeoPackage. The filters live at the END of the name, which is
        # exactly what plain truncation throws away.
        long_site = "Example River Tailings Remediation Area North"
        d_name = "%s - Arsenic (unaggregated, SE+TAILS+WS, D)" % long_site
        t_name = "%s - Arsenic (unaggregated, SE+TAILS+WS, T)" % long_site
        check("gpkg: layers differing only at the end get distinct tables",
              lb.gpkg_layer_name(d_name) != lb.gpkg_layer_name(t_name),
              lb.gpkg_layer_name(d_name))
        check("gpkg: shortened names still respect the limit",
              len(lb.gpkg_layer_name(d_name)) <= lb.GPKG_NAME_LIMIT,
              str(len(lb.gpkg_layer_name(d_name))))
        check("gpkg: the same name is always the same table",
              lb.gpkg_layer_name(d_name) == lb.gpkg_layer_name(d_name))
        # Short names must be untouched, or every table in an existing
        # GeoPackage would be orphaned by this change.
        check("gpkg: short names are unchanged",
              lb.gpkg_layer_name("Site - Benzene (maximum)")
              == "site_benzene_maximum",
              lb.gpkg_layer_name("Site - Benzene (maximum)"))

        # --- unplaceable results are NAMED, not just counted ---------------
        # A bare "8 could not be placed" says something is wrong but not
        # which sites to look at, and the codes are the whole substance of
        # the problem: they are exactly what differs between the report and
        # the location layer.
        import datetime
        results = [
            {"location": "MW-1", "value": 1.0, "unit": "ug/L",
             "date": datetime.datetime(2020, 1, 1), "detect": "Y"},
            {"location": "RDO-16", "value": 2.0, "unit": "ug/L",
             "date": datetime.datetime(2020, 1, 2), "detect": "Y"},
            {"location": "BH-7", "value": 3.0, "unit": "ug/L",
             "date": datetime.datetime(2020, 1, 3), "detect": "Y"},
            # Same missing location twice: it must be named ONCE, or a
            # thousand results at one bad site would print it a thousand
            # times.
            {"location": "RDO-16", "value": 4.0, "unit": "ug/L",
             "date": datetime.datetime(2020, 1, 4), "detect": "Y"},
        ]
        layer3, placed, unplaced, _w = lb.build_result_layer(
            "Site - Benzene (every result)", sample_pages(), results,
            "Benzene", fallback_wkid=26917, storage=lb.MEMORY,
        )
        check("unplaced: placeable results are still mapped",
              placed == 1, str(placed))
        check("unplaced: the codes are returned, not a count",
              sorted(unplaced) == ["BH-7", "RDO-16"], str(unplaced))
        check("unplaced: a repeated missing location is named once",
              unplaced.count("RDO-16") == 1, str(unplaced))
        check("unplaced: the original spelling is kept",
              "RDO-16" in unplaced, str(unplaced))
    finally:
        QgsProject.instance().clear()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%s" % ("ALL STORAGE TESTS PASSED" if not FAILURES
                    else "FAILURES: %s" % ", ".join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    code = main()
    app.exitQgis()
    sys.exit(code)
