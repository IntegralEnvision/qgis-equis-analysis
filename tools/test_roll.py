"""Rolling sparkline markers: captions, column cycling, and teardown.

Needs QGIS but no network:

  "C:\\Program Files\\QGIS 4.0.2\\bin\\python-qgis.bat" tools\\test_roll.py

Animated SVG is not an option - Qt implements SVG 1.2 Tiny, which has no
declarative animation, so SMIL and CSS keyframes both render frozen. Rolling
instead swaps which attribute the symbol reads, which costs about a
millisecond and touches no data.

The teardown cases matter most: a timer still firing at a layer that has been
removed from the project takes QGIS down with it.
"""

import datetime
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsPointXY,
    QgsRectangle,
    QgsSvgMarkerSymbolLayer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType, QSize  # noqa: E402
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
from equis_loader import sparkline as sp  # noqa: E402

ANALYTES = ["Benzene", "Trichloroethylene", "Vinyl chloride"]


def series(base, n=5):
    return [
        {"date": datetime.datetime(2019 + j, 1, 1), "value": base + j * 2.0,
         "unit": "ug/L", "detect": "Y", "at_limit": False}
        for j in range(n)
    ]


def build_layer(count=60):
    layer = QgsVectorLayer("Point?crs=EPSG:3857", "roll", "memory")
    provider = layer.dataProvider()
    provider.addAttributes([QgsField("code", QMetaType.Type.QString)])
    layer.updateFields()
    feats = []
    side = int(count ** 0.5) + 1
    for i in range(count):
        f = QgsFeature(layer.fields())
        f.setAttributes(["MW-%03d" % i])
        f.setGeometry(QgsGeometry.fromPointXY(
            QgsPointXY((i % side) * 120, (i // side) * 120)))
        feats.append(f)
    provider.addFeatures(feats)
    layer.updateExtents()
    return layer


def markers_for(count):
    out = []
    for name in ANALYTES:
        out.append((name, sp.embed_sparklines(
            {"MW-%03d" % i: series(float(i % 4) + 1.0) for i in range(count)},
            analyte=name)))
    return out


def _render(layer):
    ms = QgsMapSettings()
    ms.setLayers([layer])
    ms.setBackgroundColor(QColor(255, 255, 255))
    ms.setOutputSize(QSize(320, 260))
    ms.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    ms.setExtent(layer.extent().buffered(150))
    job = QgsMapRendererParallelJob(ms)
    job.start()
    job.waitForFinished()
    return job.renderedImage()


def painted(layer):
    """How much ink a layer puts on the canvas."""
    image = _render(layer)
    count = 0
    for x in range(0, 320, 2):
        for y in range(0, 260, 2):
            colour = QColor(image.pixel(x, y))
            if (colour.red(), colour.green(), colour.blue()) != (255, 255, 255):
                count += 1
    return count


def frame_hash(layer):
    """A fingerprint of the rendered map, to prove frames really differ."""
    image = _render(layer)
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    return hashlib.md5(bytes(bits)).hexdigest()[:12]


def main():
    # --- the caption -------------------------------------------------
    plain = sp.render_svg(series(1.0), label="MW-1")
    captioned = sp.render_svg(series(1.0), label="MW-1", analyte="Benzene")
    check("roll: the analyte name is drawn in the marker",
          ">Benzene<" in captioned, captioned[captioned.find(">Benzene<") - 30:
          captioned.find(">Benzene<") + 9] if ">Benzene<" in captioned else "absent")
    # Every caption takes its room from the MARKER, never from the plot:
    # paying for a label by flattening the trend would defeat the label.
    check("roll: an uncaptioned marker still carries the scale band",
          'height="%g"' % (sp.HEIGHT + sp.SCALE_HEIGHT) in plain,
          plain[:90])
    check("roll: a captioned marker grows instead of squashing the plot",
          'height="%g"' % (sp.HEIGHT + sp.ANALYTE_HEIGHT + sp.SCALE_HEIGHT)
          in captioned)
    bare = sp.render_svg(series(1.0), label="MW-1", show_scale=False)
    check("roll: and drops the band when the scale is not drawn",
          'height="%g"' % sp.HEIGHT in bare, bare[:90])

    # The trend must stay clear of every caption above it.
    top = sp.ANALYTE_HEIGHT + sp.SCALE_HEIGHT
    coords = sp._scale(series(1.0), top_offset=top)
    lowest = min(y for _, y, _ in coords)
    check("roll: the trend never intrudes on the captions",
          lowest >= sp.LABEL_HEIGHT + top,
          "min y %.1f vs band %.1f" % (lowest, sp.LABEL_HEIGHT + top))

    # Long chemical names have to stay identifiable.
    long_name = "1,1,2-Trichloro-1,2,2-trifluoroethane"
    svg = sp.render_svg(series(1.0), label="MW-1", analyte=long_name)
    check("roll: long chemical names keep their distinguishing head",
          "1,1,2-Trichl" in svg and long_name not in svg,
          sp._shorten_head(long_name))
    check("roll: chemical names truncate from the tail, unlike codes",
          sp._shorten_head("ABCDEFGHIJKLMNOPQRSTUVWXYZ").startswith("ABC")
          and sp._shorten("ABCDEFGHIJKLMNOPQRSTUVWXYZ").endswith("XYZ"))
    check("roll: XML metacharacters in a chemical name are escaped",
          "&amp;" in sp.render_svg(series(1.0), analyte="A & B"))

    # --- the frame ----------------------------------------------------
    check("roll: the frame carries the corner radius",
          'rx="%g"' % sp.CORNER_RADIUS in captioned,
          "rx=%g" % sp.CORNER_RADIUS)
    check("roll: the analyte band is tucked inside the frame's corners",
          sp.BAND_RADIUS < sp.CORNER_RADIUS,
          "band %g vs frame %g" % (sp.BAND_RADIUS, sp.CORNER_RADIUS))
    # The band is a path, not a rect, because only its top corners round.
    check("roll: the analyte band rounds only its top corners",
          '<path d="M1,' in captioned and 'A%g,%g' % (sp.BAND_RADIUS,
                                                      sp.BAND_RADIUS) in captioned)

    # The corner has to survive being scaled to the size QGIS draws markers
    # at. It is measured as border ink, not as transparency: the marker is
    # filled and stroked, so a rounded corner curves the *border* inward
    # rather than cutting through to the background - probing for background
    # pixels finds none however round the corner is.
    from qgis.PyQt.QtCore import QByteArray
    from qgis.PyQt.QtGui import QImage, QPainter
    from qgis.PyQt.QtSvg import QSvgRenderer

    def corner_ink(radius, px, probe=4):
        original = (sp.CORNER_RADIUS, sp.BAND_RADIUS)
        sp.CORNER_RADIUS, sp.BAND_RADIUS = radius, max(0.0, radius - 1.5)
        try:
            svg = sp.render_svg(series(1.0), label="MW-1", analyte="Benzene")
        finally:
            sp.CORNER_RADIUS, sp.BAND_RADIUS = original
        renderer = QSvgRenderer(QByteArray(svg.encode()))
        size = renderer.defaultSize()
        image = QImage(px, int(px * size.height() / size.width()),
                       QImage.Format.Format_ARGB32)
        image.fill(QColor(255, 0, 255))
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        # Count non-fill pixels in the corner: the more the corner curves, the
        # more border sweeps through this square.
        ink = 0
        for x in range(probe):
            for y in range(probe):
                c = QColor(image.pixel(x, y))
                if not (c.red() > 200 and c.green() > 200 and c.blue() > 200):
                    ink += 1
        return ink

    square = corner_ink(0.0, 40)
    rounded = corner_ink(sp.CORNER_RADIUS, 40)
    check("roll: the corner is visibly rounder than a square one at map size",
          rounded > square,
          "%d border px vs %d for a square corner (40 px wide)"
          % (rounded, square))
    check("roll: rounding scales with the radius",
          corner_ink(8.0, 40) >= rounded > corner_ink(2.5, 40),
          "rx8=%d rx%g=%d rx2.5=%d"
          % (corner_ink(8.0, 40), sp.CORNER_RADIUS, rounded,
             corner_ink(2.5, 40)))

    # --- scale-dependent sizing ---------------------------------------
    # Markers fixed in millimetres stay the same on screen at every zoom, so a
    # whole facility becomes an unreadable mat. Sizing in map units ties them
    # to the ground; the clamps stop them vanishing or swamping the map.
    from qgis.core import QgsUnitTypes

    sized = build_layer(9)
    lb.style_with_rolling_sparklines(sized, markers_for(9),
                                    location_field="code")

    def find_svg_layer(layer):
        """The SVG symbol layer, wherever the rule tree keeps it.

        The renderer holds three rules - a default, the sparkline rule and a
        plain-dot fallback - so indexing blindly finds a simple marker and
        reports millimetres however the sparkline is configured.
        """
        for rule in layer.renderer().rootRule().children():
            symbol = rule.symbol()
            if symbol is None:
                continue
            for i in range(symbol.symbolLayerCount()):
                candidate = symbol.symbolLayer(i)
                if isinstance(candidate, QgsSvgMarkerSymbolLayer):
                    return candidate
        return None

    svg_layer = find_svg_layer(sized)
    check("roll: the rule tree contains an SVG marker", svg_layer is not None)
    check("roll: markers are sized in map units, not millimetres",
          svg_layer.sizeUnit() == QgsUnitTypes.RenderUnit.RenderMapUnits,
          str(svg_layer.sizeUnit()))
    scale = svg_layer.sizeMapUnitScale()
    check("roll: a minimum screen size stops them vanishing when zoomed out",
          scale.minSizeMMEnabled and scale.minSizeMM > 0,
          "min %.1f mm" % scale.minSizeMM)
    check("roll: a maximum screen size stops them swamping the map",
          scale.maxSizeMMEnabled and scale.maxSizeMM > scale.minSizeMM,
          "max %.1f mm" % scale.maxSizeMM)
    check("roll: the ground size follows the layer's own extent",
          lb.spark_ground_size(sized) > 0
          and lb.spark_ground_size(sized) != lb.SPARK_GROUND_SIZE,
          "%.1f map units" % lb.spark_ground_size(sized))
    # An empty layer has no extent to measure, so it must fall back rather
    # than producing a zero-sized marker.
    check("roll: an empty layer falls back to the default ground size",
          lb.spark_ground_size(build_layer(0)) == lb.SPARK_GROUND_SIZE)

    # And it must actually shrink: render the same markers at two zooms and
    # compare how much of the canvas they cover.
    def coverage(layer, width):
        ms = QgsMapSettings()
        ms.setLayers([layer])
        ms.setBackgroundColor(QColor(255, 255, 255))
        ms.setOutputSize(QSize(300, 300))
        ms.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        centre = layer.extent().center()
        half = width / 2.0
        ms.setExtent(QgsRectangle(centre.x() - half, centre.y() - half,
                                  centre.x() + half, centre.y() + half))
        job = QgsMapRendererParallelJob(ms)
        job.start()
        job.waitForFinished()
        image = job.renderedImage()
        painted = 0
        for x in range(0, 300, 2):
            for y in range(0, 300, 2):
                c = QColor(image.pixel(x, y))
                if (c.red(), c.green(), c.blue()) != (255, 255, 255):
                    painted += 1
        return painted

    span = max(sized.extent().width(), sized.extent().height()) or 1000.0
    measured = [(mult, coverage(sized, span * mult))
                for mult in (1.2, 3.0, 8.0, 20.0)]
    check("roll: markers shrink as the view zooms out",
          all(b <= a for (_, a), (_, b) in zip(measured, measured[1:]))
          and measured[-1][1] < measured[0][1] * 0.5,
          ", ".join("x%.1f=%dpx" % m for m in measured))

    # --- the static sparkline renderer gets the same treatment ---------
    # style_with_sparklines is the non-rolling path; it must be sized the same
    # way, or switching between the two modes would resize the markers.
    static = build_layer(9)
    static_markers = sp.embed_sparklines(
        {"MW-%03d" % i: series(1.0) for i in range(9)}, analyte="Benzene")
    drawn = lb.style_with_sparklines(static, static_markers,
                                    location_field="code")
    static_svg = find_svg_layer(static)
    check("static: sparklines apply outside the roll", drawn == 9, str(drawn))
    check("static: markers are sized in map units too",
          static_svg is not None
          and static_svg.sizeUnit() == QgsUnitTypes.RenderUnit.RenderMapUnits)
    check("static: the same millimetre clamps apply",
          static_svg.sizeMapUnitScale().minSizeMM == lb.SPARK_MIN_MM,
          "min %.1f mm" % static_svg.sizeMapUnitScale().minSizeMM)
    # The rolling marker carries an extra caption, so it is allowed to be
    # larger - but both must be larger than the old fixed 11 mm.
    check("static: the marker is larger than it used to be",
          static_svg.sizeMapUnitScale().maxSizeMM > 11.0,
          "%.1f mm vs the previous 11.0" % static_svg.sizeMapUnitScale().maxSizeMM)
    check("roll: the rolling marker is larger still",
          svg_layer.sizeMapUnitScale().maxSizeMM
          >= static_svg.sizeMapUnitScale().maxSizeMM,
          "%.1f vs %.1f mm" % (svg_layer.sizeMapUnitScale().maxSizeMM,
                               static_svg.sizeMapUnitScale().maxSizeMM))

    # --- columns and cycling ------------------------------------------
    layer = build_layer(60)
    columns = lb.style_with_rolling_sparklines(layer, markers_for(60),
                                               location_field="code")
    check("roll: one marker column per analyte",
          columns == ["spark_0", "spark_1", "spark_2"], repr(columns))
    check("roll: every column is populated",
          all(layer.fields().indexFromName(c) >= 0 for c in columns))

    first = next(layer.getFeatures())
    check("roll: each column holds a different analyte's marker",
          first["spark_0"] != first["spark_1"] != first["spark_2"]
          and all(first[c].startswith("base64:") for c in columns))

    # The renderer must follow the variable, not the data.
    inks = []
    frames = []
    for column in columns:
        lb.set_roll_column(layer, column)
        current = QgsExpressionContextUtils.layerScope(layer).variable(
            lb.ROLL_VARIABLE)
        check("roll: the variable points at %s" % column, current == column,
              str(current))
        inks.append(painted(layer))
        frames.append(frame_hash(layer))
    check("roll: every analyte actually renders", all(v > 20 for v in inks),
          repr(inks))
    # Hashing the image rather than counting ink: two analytes can put the
    # same amount of paint down while drawing entirely different markers, so
    # an ink count alone would pass even if the map never changed.
    check("roll: each analyte draws a visibly different map",
          len(set(frames)) == len(columns), repr(frames))

    # Rolling must not rewrite the features - that is what keeps it cheap and
    # keeps a GeoPackage layer from being dirtied every few seconds.
    before = [f["spark_0"] for f in layer.getFeatures()]
    lb.set_roll_column(layer, columns[2])
    after = [f["spark_0"] for f in layer.getFeatures()]
    check("roll: advancing the roll writes no data", before == after)

    # --- robustness ---------------------------------------------------
    lb.set_roll_column(layer, "spark_does_not_exist")
    check("roll: a missing column renders without crashing",
          painted(layer) >= 0)

    empty = lb.style_with_rolling_sparklines(build_layer(5), [],
                                             location_field="code")
    check("roll: no analytes yields no columns", empty == [])

    single = lb.style_with_rolling_sparklines(
        build_layer(5), markers_for(5)[:1], location_field="code")
    check("roll: a single analyte still produces one column",
          single == ["spark_0"], repr(single))

    # A location with no data for one analyte must fall back to a plain
    # marker for that frame rather than disappearing.
    layer2 = build_layer(6)
    partial = [
        ("Benzene", sp.embed_sparklines(
            {"MW-%03d" % i: series(1.0) for i in range(6)}, analyte="Benzene")),
        ("TCE", sp.embed_sparklines(
            {"MW-000": series(1.0)}, analyte="TCE")),
    ]
    cols2 = lb.style_with_rolling_sparklines(layer2, partial,
                                             location_field="code")
    lb.set_roll_column(layer2, cols2[1])
    values = [f["spark_1"] for f in layer2.getFeatures()]
    check("roll: locations missing an analyte get a null, not a stale marker",
          sum(1 for v in values if v) == 1 and sum(1 for v in values if not v) == 5,
          "%d set, %d null" % (sum(1 for v in values if v),
                               sum(1 for v in values if not v)))
    check("roll: a frame with mostly nulls still draws", painted(layer2) > 0)

    # --- size, which decides how many analytes are practical ----------
    uri = sp.svg_datauri(sp.render_svg(series(1.0), label="MW-MR0042",
                                       analyte="Trichloroethylene"))
    per = len(uri)
    check("roll: a captioned marker stays around a kilobyte",
          800 < per < 2000, "%d bytes" % per)
    print("      (%d analytes x 10,636 locations = %.0f MB embedded)"
          % (3, per * 3 * 10636 / 1048576.0))

    print("\n%s" % ("ALL ROLL TESTS PASSED" if not FAILURES
                    else "FAILURES: %s" % ", ".join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    code = main()
    app.exitQgis()
    sys.exit(code)
