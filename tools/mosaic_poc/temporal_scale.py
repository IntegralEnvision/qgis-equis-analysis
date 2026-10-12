"""Does QGIS's Temporal Controller make an unaggregated layer affordable?

R9 of docs/vision-linked-views.md found that drawn marks cost ~1,100x what
scanned rows cost, so anything that reduces marks is worth more than anything
that reduces rows. Temporal filtering reduces marks, which puts it on the
right side of that ledger - but phase 1.5 also found that a provider filter is
re-evaluated on EVERY render pass, which is why case A (`subsetString`) took
360 ms to draw the same ~5,000 marks case C drew in 50 ms.

Temporal filtering has case A's shape. This measures what it actually costs,
and whether an index changes the answer:

    all             every feature, no temporal filter - the thing to avoid
    temporal        temporal window active, unindexed time column
    temporal+index  the same, with an attribute index on the time column
    direct          a separate layer holding exactly the visible features,
                    which is the floor: drawing k marks and filtering nothing

    filter overhead = temporal - direct

If `temporal+index` is much faster than `temporal`, the filter reached the
provider and the index was used - which is the question that decides whether
an unaggregated layer scales. If the two are the same, either QGIS is
evaluating the expression per feature rather than pushing it down to OGR, or
OGR is ignoring the index; both mean the cost grows with the whole layer
rather than with the window.

Run from the QGIS Python console:

    exec(open(r"C:\\path\\to\\equis_qgis\\tools\\mosaic_poc\\temporal_scale.py").read())

    t = TemporalScale(iface)                       # 50k and 250k
    t = TemporalScale(iface, sizes=[50000])        # quicker
    t.run()
    t.animate()      # stepping windows, which is the demanding case
    t.close()
"""

import os
import random
import tempfile
import time

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsDateTimeRange,
    QgsFeature,
    QgsGeometry,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsMarkerSymbol,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSingleSymbolRenderer,
    QgsVectorDataProvider,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsVectorLayerTemporalProperties,
)
from qgis.PyQt.QtCore import QDate, QDateTime, QSize, QTime
from qgis.PyQt.QtGui import QColor

BUDGET_MS = 100.0
OUTPUT_SIZE = QSize(1400, 900)
CRS = "EPSG:3857"
TIME_FIELD = "sample_time"

# Fractions of the record visible in one temporal window. 2% of a decade is
# roughly a quarter; 10% is about a year.
WINDOWS = (0.02, 0.10)

YEARS = 18
START = QDate(2008, 1, 1)


def _ms(seconds):
    return seconds * 1000.0


def _temporal_mode():
    """The enum moved between QGIS versions; ask for whichever exists."""
    mode = getattr(Qgis, "VectorTemporalMode", None)
    if mode is not None and hasattr(mode, "FeatureDateTimeInstantFromField"):
        return mode.FeatureDateTimeInstantFromField
    return QgsVectorLayerTemporalProperties.ModeFeatureDateTimeInstantFromField


class TemporalScale(object):
    def __init__(self, iface=None, sizes=(50000, 250000), seed=17):
        self.iface = iface
        self.sizes = list(sizes)
        self.seed = seed
        self.rows = []
        self._temp = tempfile.mkdtemp(prefix="equis_temporal_")
        self._layers = []
        self._cases = {}
        self.extent = QgsRectangle(-100000, -100000, 100000, 100000)
        self.days = YEARS * 365

    # ------------------------------------------------------------ fixtures

    def _make_points(self, count):
        rng = random.Random(self.seed)
        out = []
        for index in range(count):
            out.append((
                index,
                rng.uniform(-100000, 100000),
                rng.uniform(-100000, 100000),
                # Uniform over the record, so a window of x% really holds
                # about x% of the features and the arithmetic is honest.
                rng.randrange(self.days),
            ))
        return out

    def _memory_layer(self, name, points):
        layer = QgsVectorLayer(
            "Point?crs=%s&field=fid:integer&field=%s:datetime"
            % (CRS, TIME_FIELD), name, "memory")
        provider = layer.dataProvider()
        batch = []
        for fid, x, y, day in points:
            feature = QgsFeature(layer.fields())
            feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
            feature.setAttributes([fid, self._stamp(day)])
            batch.append(feature)
        provider.addFeatures(batch)
        layer.updateExtents()
        self._layers.append(layer)
        return layer

    def _stamp(self, day):
        return QDateTime(START.addDays(int(day)), QTime(12, 0))

    def _geopackage_layer(self, name, points):
        source = self._memory_layer(name + "_src", points)
        path = os.path.join(self._temp, "%s.gpkg" % name)
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = name
        QgsVectorFileWriter.writeAsVectorFormatV3(
            source, path, QgsProject.instance().transformContext(), options)
        layer = QgsVectorLayer("%s|layername=%s" % (path, name), name, "ogr")
        # The staging memory layer has served its purpose; at 250,000
        # features keeping it costs real memory for nothing.
        if source in self._layers:
            self._layers.remove(source)
        self._layers.append(layer)
        return layer

    @staticmethod
    def _style(layer):
        symbol = QgsMarkerSymbol.createSimple({
            "name": "circle", "color": "27,110,194", "outline_style": "no",
            "size": "2.0", "size_unit": "MM"})
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))

    def _enable_temporal(self, layer):
        props = layer.temporalProperties()
        props.setMode(_temporal_mode())
        props.setStartField(TIME_FIELD)
        props.setIsActive(True)
        return props

    def _index(self, layer):
        """Attribute index on the time column, if the provider allows it."""
        provider = layer.dataProvider()
        caps = provider.capabilities()
        flag = getattr(QgsVectorDataProvider, "CreateAttributeIndex", None)
        if flag is None:
            flag = QgsVectorDataProvider.Capability.CreateAttributeIndex
        if not (caps & flag):
            return False, "provider cannot create attribute indexes"
        field = layer.fields().indexOf(TIME_FIELD)
        if field < 0:
            return False, "no %s field" % TIME_FIELD
        start = time.time()
        ok = provider.createAttributeIndex(field)
        return bool(ok), "%.0f ms to build" % _ms(time.time() - start)

    # ------------------------------------------------------------ measuring

    def _render(self, layers, window=None):
        settings = QgsMapSettings()
        settings.setLayers(layers)
        settings.setBackgroundColor(QColor(255, 255, 255))
        settings.setOutputSize(OUTPUT_SIZE)
        settings.setDestinationCrs(QgsCoordinateReferenceSystem(CRS))
        settings.setExtent(self.extent)
        if window is not None:
            settings.setIsTemporal(True)
            settings.setTemporalRange(window)
        job = QgsMapRendererParallelJob(settings)
        start = time.time()
        job.start()
        job.waitForFinished()
        return time.time() - start

    def _timed(self, layers, window=None, repeats=3):
        self._render(layers, window)
        return min(self._render(layers, window) for _ in range(repeats))

    def _verify(self, layer):
        """Prove the temporal filter is doing something before trusting it.

        A window containing no features must render blank. If it does not,
        temporal filtering is not taking effect and every number below is
        just 'draw the whole layer' wearing a different label - which is
        precisely the failure that would look like a good result.
        """
        empty = QgsDateTimeRange(
            QDateTime(QDate(1990, 1, 1), QTime(0, 0)),
            QDateTime(QDate(1990, 1, 2), QTime(0, 0)))
        settings = QgsMapSettings()
        settings.setLayers([layer])
        settings.setBackgroundColor(QColor(255, 255, 255))
        settings.setOutputSize(QSize(400, 300))
        settings.setDestinationCrs(QgsCoordinateReferenceSystem(CRS))
        settings.setExtent(self.extent)
        settings.setIsTemporal(True)
        settings.setTemporalRange(empty)
        job = QgsMapRendererParallelJob(settings)
        job.start()
        job.waitForFinished()
        image = job.renderedImage()
        ink = 0
        for x in range(0, 400, 3):
            for y in range(0, 300, 3):
                if QColor(image.pixel(x, y)) != QColor(255, 255, 255):
                    ink += 1
        return ink

    def _window(self, fraction, offset=0.0):
        span = int(self.days * fraction)
        first = int(self.days * offset)
        return QgsDateTimeRange(self._stamp(first), self._stamp(first + span))

    # ------------------------------------------------------------- the run

    def run(self):
        print("")
        print("Temporal filtering cost, budget %.0f ms" % BUDGET_MS)
        print("  %d years of record, output %dx%d"
              % (YEARS, OUTPUT_SIZE.width(), OUTPUT_SIZE.height()))

        for size in self.sizes:
            print("")
            print("  GeoPackage, %s results" % format(size, ",d"))
            points = self._make_points(size)
            built = time.time()
            layer = self._geopackage_layer("temporal_%d" % size, points)
            self._style(layer)
            print("    built in %.1f s" % (time.time() - built))

            everything = self._timed([layer])
            print("    all %s features, no filter      : %8.1f ms"
                  % (format(size, ",d"), _ms(everything)))

            self._enable_temporal(layer)
            ink = self._verify(layer)
            if ink:
                print("    !! temporal filter NOT taking effect: a window with"
                      " no features still painted %d sample pixels." % ink)
                print("    Every number below would be 'draw everything' in"
                      " disguise. Stopping.")
                return
            print("    temporal filter verified (empty window renders blank)")
            self._cases[size] = {"layer": layer, "points": points}

            for fraction in WINDOWS:
                window = self._window(fraction)
                span = int(self.days * fraction)
                visible = sum(1 for p in points if 0 <= p[3] <= span)

                unindexed = self._timed([layer], window)

                # The floor: exactly the visible features, no filtering.
                # GeoPackage, like the layer it is the floor for. A memory
                # layer here would draw 6-8x faster per point for reasons
                # that have nothing to do with filtering, and the "overhead"
                # would mostly be the provider difference.
                direct = self._geopackage_layer(
                    "direct_%d_%d" % (size, int(fraction * 100)),
                    [p for p in points if 0 <= p[3] <= span])
                self._style(direct)
                floor = self._timed([direct])

                print("    window %.0f%% - %s visible:"
                      % (fraction * 100, format(visible, ",d")))
                print("      temporal, unindexed          : %8.1f ms  %s"
                      % (_ms(unindexed),
                         "OK" if _ms(unindexed) < BUDGET_MS else "OVER"))
                print("      direct layer (drawing floor) : %8.1f ms  %s"
                      % (_ms(floor),
                         "OK" if _ms(floor) < BUDGET_MS else "OVER"))
                print("      filter overhead              : %8.1f ms"
                      % (_ms(unindexed) - _ms(floor)))
                self.rows.append({"size": size, "fraction": fraction,
                                  "visible": visible, "all": _ms(everything),
                                  "unindexed": _ms(unindexed),
                                  "floor": _ms(floor)})

            # Index once, then re-measure every window.
            ok, detail = self._index(layer)
            print("    attribute index on %s: %s (%s)"
                  % (TIME_FIELD, "created" if ok else "NOT created", detail))
            if ok:
                layer.reload()
                for row in [r for r in self.rows if r["size"] == size]:
                    window = self._window(row["fraction"])
                    indexed = self._timed([layer], window)
                    row["indexed"] = _ms(indexed)
                    change = row["unindexed"] - row["indexed"]
                    print("      window %.0f%% indexed          : %8.1f ms  "
                          "(%+.1f ms vs unindexed)"
                          % (row["fraction"] * 100, row["indexed"], -change))

        self._summary()

    def _summary(self):
        print("")
        print("=" * 70)
        print("TEMPORAL VERDICT")
        print("=" * 70)
        for row in self.rows:
            tag = "%s results, %.0f%% window (%s visible)" % (
                format(row["size"], ",d"), row["fraction"] * 100,
                format(row["visible"], ",d"))
            best = row.get("indexed", row["unindexed"])
            print("  %-46s %8.1f ms  %s"
                  % (tag, best, "OK" if best < BUDGET_MS else "OVER"))

        # Two windows per size is exactly enough to separate the cost that
        # scales with the LAYER from the cost that scales with the WINDOW.
        # The first version of this averaged the index's effect across
        # selectivities and reported "no change", which hid both the model
        # and a textbook index result. Averaging is how a finding is lost.
        fits = []
        for size in sorted({r["size"] for r in self.rows}):
            pair = sorted((r for r in self.rows if r["size"] == size),
                          key=lambda r: r["visible"])
            if len(pair) < 2:
                continue
            low, high = pair[0], pair[-1]
            if high["visible"] == low["visible"]:
                continue
            draw = ((high["unindexed"] - low["unindexed"])
                    / (high["visible"] - low["visible"]))
            scan = (low["unindexed"] - draw * low["visible"]) / size
            fits.append((size, scan, draw))
            print("")
            print("  %s results decomposes as:" % format(size, ",d"))
            print("    filter  %5.2f us per row in the layer  "
                  "(floor: %.1f ms before anything is drawn)"
                  % (scan * 1000, scan * size))
            print("    draw    %5.1f us per mark actually shown" % (draw * 1000))

        if fits:
            scan = sum(f[1] for f in fits) / len(fits)
            draw = sum(f[2] for f in fits) / len(fits)
            print("")
            print("  MODEL: ms = %.4f x layer_rows + %.4f x visible_marks"
                  % (scan, draw))
            print("  Filtering is ~%.0fx cheaper per row than drawing is per"
                  " mark," % (draw / scan))
            print("  so a temporal window is worth having - but the scan is")
            print("  paid over the WHOLE layer every frame, and that is what")
            print("  sets the ceiling:")
            print("")
            print("    %-14s %-14s %s"
                  % ("layer rows", "filter floor", "marks left in budget"))
            for size in (25000, 50000, 100000, 150000, 250000):
                floor = scan * size
                left = BUDGET_MS - floor
                room = int(left / draw) if left > 0 else 0
                print("    %-14s %8.1f ms    %s"
                      % (format(size, ",d"), floor,
                         format(room, ",d") if room > 0
                         else "none - the scan alone exceeds the budget"))

        indexed = [r for r in self.rows if "indexed" in r]
        if indexed:
            print("")
            print("  THE INDEX, BY SELECTIVITY - it is not one answer:")
            for row in sorted(indexed, key=lambda r: r["fraction"]):
                change = 100.0 * (row["indexed"] - row["unindexed"]) / row["unindexed"]
                print("    %8s rows, %2.0f%% window: %6.1f -> %6.1f ms  %+5.0f%%"
                      % (format(row["size"], ",d"), row["fraction"] * 100,
                         row["unindexed"], row["indexed"], change))
            narrow = [r for r in indexed if r["fraction"] <= 0.05]
            wide = [r for r in indexed if r["fraction"] > 0.05]
            if narrow and wide:
                print("")
                print("  Textbook index behaviour: a win when the window is")
                print("  narrow, a LOSS when it is wide, because past some")
                print("  selectivity a sequential scan beats many seeks.")
                print("  Index the time column if windows are narrow; do not")
                print("  assume it is free either way.")
        print("")
        print("  Then:  t.animate()   # stepping windows, the demanding case")

    # ------------------------------------------------------------ animation

    def animate(self, size=None, fraction=WINDOWS[0], frames=20):
        """What the Temporal Controller actually does: step the window."""
        size = size or max(self._cases)
        case = self._cases.get(size)
        if case is None:
            print("  run() first")
            return
        layer = case["layer"]
        times = []
        for step in range(frames):
            offset = (step / float(frames)) * (1.0 - fraction)
            window = self._window(fraction, offset)
            times.append(self._render([layer], window))
        times_ms = sorted(_ms(t) for t in times)
        mean = sum(times_ms) / len(times_ms)
        print("")
        print("  animation, %s results, %.0f%% window, %d frames"
              % (format(size, ",d"), fraction * 100, frames))
        print("    median %.1f ms   mean %.1f ms   worst %.1f ms"
              % (times_ms[len(times_ms) // 2], mean, times_ms[-1]))
        print("    -> %.1f frames/second sustained" % (1000.0 / mean))
        if mean < BUDGET_MS:
            print("    Smooth enough to animate.")
        else:
            print("    Steppable by hand, not smooth. Each frame re-filters.")

    def close(self):
        project = QgsProject.instance()
        for layer in self._layers:
            try:
                if project.mapLayer(layer.id()):
                    project.removeMapLayer(layer.id())
            except Exception:
                pass
        self._layers = []
        self._cases = {}
        print("  cleaned up (temp files in %s)" % self._temp)


print("temporal_scale.py loaded. Try:")
print("  t = TemporalScale(iface, sizes=[50000])   # quicker")
print("  t = TemporalScale(iface)                  # 50k and 250k")
print("  t.run()")
print("  t.animate()")
