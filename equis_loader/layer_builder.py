"""Turn EQuIS/Esri JSON and report CSV rows into QGIS layers.

Layers are built in memory and then, depending on the chosen storage, either
handed back as-is or written to a GeoPackage and reopened from it. A memory
layer is not saved with the project - QGIS keeps the reference but not the
data, so the project reopens with an empty layer - which is what GeoPackage
storage exists to fix.
"""

import hashlib
import os
import re

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransformContext,
    QgsCategorizedSymbolRenderer,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsMapUnitScale,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsGradientColorRamp,
    QgsGraduatedSymbolRenderer,
    QgsMarkerSymbol,
    QgsPointDisplacementRenderer,
    QgsPointXY,
    QgsProperty,
    QgsRendererCategory,
    QgsRendererRange,
    QgsRuleBasedRenderer,
    QgsSingleSymbolRenderer,
    QgsSvgMarkerSymbolLayer,
    QgsSymbolLayer,
    QgsUnitTypes,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsVectorLayerTemporalProperties,
)
from qgis.PyQt.QtCore import QDate, QDateTime, QMetaType, QTime
from qgis.PyQt.QtGui import QColor

# Storage modes. MEMORY is what the plugin has always done; GEOPACKAGE writes
# beside the project so the layer survives being reopened.
MEMORY = "memory"
GEOPACKAGE = "geopackage"

# Esri geometry type -> the memory-provider layer type we create for it.
GEOMETRY_TYPES = {
    "esriGeometryPoint": "Point",
    "esriGeometryMultipoint": "MultiPoint",
    "esriGeometryPolyline": "MultiLineString",
    "esriGeometryPolygon": "MultiPolygon",
}

# Esri field type -> Qt type. Anything unlisted falls back to string, which is
# always safe to display.
FIELD_TYPES = {
    "esriFieldTypeOID": QMetaType.Type.LongLong,
    "esriFieldTypeInteger": QMetaType.Type.Int,
    "esriFieldTypeSmallInteger": QMetaType.Type.Int,
    "esriFieldTypeSingle": QMetaType.Type.Double,
    "esriFieldTypeDouble": QMetaType.Type.Double,
    "esriFieldTypeString": QMetaType.Type.QString,
    "esriFieldTypeDate": QMetaType.Type.QString,
    "esriFieldTypeGUID": QMetaType.Type.QString,
    "esriFieldTypeGlobalID": QMetaType.Type.QString,
}

# Column names reports use for coordinates, in the order we prefer them.
X_COLUMNS = ("X_COORD", "LONGITUDE", "LON", "LONG", "EASTING", "X")
Y_COLUMNS = ("Y_COORD", "LATITUDE", "LAT", "NORTHING", "Y")


def crs_from_wkid(wkid):
    """Build a CRS from an EPSG code, falling back to WGS 84."""
    if wkid:
        crs = QgsCoordinateReferenceSystem.fromEpsgId(int(wkid))
        if crs.isValid():
            return crs
    return QgsCoordinateReferenceSystem.fromEpsgId(4326)


def resolve_wkid(feature_set, fallback_wkid=0):
    """Pick the most trustworthy EPSG code available.

    The service reports `wkid: 0` with the real code in `latestWkid` on some
    responses, so prefer whichever is non-zero.
    """
    spatial_ref = (feature_set or {}).get("spatialReference") or {}
    for key in ("latestWkid", "wkid"):
        value = spatial_ref.get(key)
        if value:
            return value
    return fallback_wkid


def _qgs_fields(esri_fields):
    fields = QgsFields()
    for fld in esri_fields or []:
        name = fld.get("name")
        if not name:
            continue
        qtype = FIELD_TYPES.get(fld.get("type"), QMetaType.Type.QString)
        fields.append(QgsField(name, qtype))
    return fields


def _point(geom):
    x, y = geom.get("x"), geom.get("y")
    if x is None or y is None:
        return None
    # Esri encodes null geometry as NaN.
    if x != x or y != y:
        return None
    return QgsGeometry.fromPointXY(QgsPointXY(float(x), float(y)))


def _rings_to_polygon(rings):
    parts = []
    for ring in rings or []:
        pts = [QgsPointXY(float(p[0]), float(p[1])) for p in ring if len(p) >= 2]
        if len(pts) >= 3:
            parts.append([pts])
    if not parts:
        return None
    return QgsGeometry.fromMultiPolygonXY(parts)


def _paths_to_line(paths):
    parts = []
    for path in paths or []:
        pts = [QgsPointXY(float(p[0]), float(p[1])) for p in path if len(p) >= 2]
        if len(pts) >= 2:
            parts.append(pts)
    if not parts:
        return None
    return QgsGeometry.fromMultiPolylineXY(parts)


def esri_geometry(geom):
    """Convert one Esri geometry object to a QgsGeometry, or None."""
    if not geom:
        return None
    if "x" in geom:
        return _point(geom)
    if "rings" in geom:
        return _rings_to_polygon(geom["rings"])
    if "paths" in geom:
        return _paths_to_line(geom["paths"])
    if "points" in geom:
        pts = [QgsPointXY(float(p[0]), float(p[1])) for p in geom["points"] if len(p) >= 2]
        return QgsGeometry.fromMultiPointXY(pts) if pts else None
    return None


# Longest table name written. GeoPackage itself allows far more, but long
# identifiers are awkward downstream, so names are shortened - carefully.
GPKG_NAME_LIMIT = 60
# Room kept at the end for the disambiguating suffix on a shortened name.
GPKG_HASH_LENGTH = 7


def gpkg_layer_name(name):
    """Reduce a display name to something GeoPackage accepts as a table name.

    Layer names carry chemical names and aggregation labels, which contain
    spaces, commas, brackets and the occasional slash.

    A name too long to keep is truncated with a hash of the WHOLE original
    appended, because plain truncation silently merged layers that differ
    only at the end - which is exactly where the filters live. On a facility
    whose name is long enough, "(unaggregated, SE+TAILS+WS, D)" and the same
    with T both cut to the same 60 characters, so mapping total overwrote
    dissolved in the GeoPackage with no warning. The hash is of the full
    name, so any difference anywhere still yields a different table.
    """
    cleaned = re.sub(r"[^0-9A-Za-z]+", "_", str(name or "layer")).strip("_")
    if not cleaned:
        cleaned = "layer"
    # A leading digit is legal in GeoPackage but awkward in SQL elsewhere.
    if cleaned[0].isdigit():
        cleaned = "l_" + cleaned
    cleaned = cleaned.lower()
    if len(cleaned) <= GPKG_NAME_LIMIT:
        return cleaned
    digest = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:GPKG_HASH_LENGTH]
    keep = GPKG_NAME_LIMIT - GPKG_HASH_LENGTH - 1
    return "%s_%s" % (cleaned[:keep].rstrip("_"), digest)


def persist_to_geopackage(layer, gpkg_path, layer_name=None):
    """Write a memory layer into a GeoPackage and return a layer reading it.

    Several layers share one file - one per mapped chemical - so this
    overwrites just its own table rather than the whole file. Returns
    (layer, error): on failure the error is a message fit to show a user and
    the caller should keep the in-memory layer rather than losing the work.
    """
    if layer is None:
        return None, "There is no layer to save."
    table = gpkg_layer_name(layer_name or layer.name())

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = table
    options.fileEncoding = "UTF-8"
    # Add to the existing file when there is one; creating from scratch each
    # time would drop the other chemicals already written beside it.
    if os.path.exists(gpkg_path):
        options.actionOnExistingFile = (
            QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
        )

    try:
        directory = os.path.dirname(gpkg_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, gpkg_path, QgsCoordinateTransformContext(), options
        )
    except Exception as exc:  # noqa: BLE001 - reported, never raised on
        return None, "Could not write %s (%s)." % (os.path.basename(gpkg_path), exc)

    code, message = result[0], result[1]
    if code != QgsVectorFileWriter.WriterError.NoError:
        return None, message or "Could not write %s." % os.path.basename(gpkg_path)

    saved = QgsVectorLayer(
        "%s|layername=%s" % (gpkg_path, table), layer.name(), "ogr"
    )
    if not saved.isValid():
        return None, "Saved %s but could not read it back." % os.path.basename(gpkg_path)
    return saved, ""


def build_feature_layer(layer_name, feature_sets, geometry_type=None, fallback_wkid=0):
    """Assemble a memory layer from one or more Esri FeatureSet pages.

    `feature_sets` is a list of (feature_set, features) pairs as produced by
    EquisClient.iter_layer_features.
    """
    if not feature_sets:
        return None, 0

    first = feature_sets[0][0]
    geom_key = geometry_type or first.get("geometryType") or "esriGeometryPoint"
    geom_name = GEOMETRY_TYPES.get(geom_key, "Point")
    wkid = resolve_wkid(first, fallback_wkid)
    crs = crs_from_wkid(wkid)

    layer = QgsVectorLayer(
        "%s?crs=%s" % (geom_name, crs.authid() or "EPSG:4326"), layer_name, "memory"
    )
    if not layer.isValid():
        return None, 0

    fields = _qgs_fields(first.get("fields"))
    provider = layer.dataProvider()
    provider.addAttributes(fields.toList())
    layer.updateFields()

    field_names = [fields.at(i).name() for i in range(fields.count())]
    buffer = []
    skipped = 0
    for _, features in feature_sets:
        for item in features:
            attrs = item.get("attributes") or {}
            feature = QgsFeature(layer.fields())
            feature.setAttributes([attrs.get(name) for name in field_names])
            geom = esri_geometry(item.get("geometry"))
            if geom is None:
                skipped += 1
            else:
                feature.setGeometry(geom)
            buffer.append(feature)

    provider.addFeatures(buffer)
    layer.updateExtents()
    return layer, skipped


def build_joined_layer(layer_name, feature_sets, summary, chemical, unit_hint="",
                       fallback_wkid=0, location_field="code",
                       storage=MEMORY, gpkg_path=None, spark_field="spark_svg",
                       table_name=None):
    """Location points carrying one analyte's aggregated result.

    `summary` is ReportTable.aggregate() output keyed by location code, and
    only locations IN it become features. An entry means the location has a
    row matching the analyte and the current matrix/fraction/method scope;
    its value may still be NULL, and that is kept - "sampled, no usable
    number" is a real and visible state.

    What is dropped is a location with no entry at all, which was never a
    candidate for this layer. That is a change from the left join this used
    to do: keeping every location made sense for one analyte over one
    monitoring network, and stopped making sense over a whole-facility
    parquet, where a "Chloride (maximum, WS, D)" layer drew all 906
    locations - tailings, soil and sediment included - every one NULL,
    because Chloride here is WS/T and WS/N and has no WS/D data at all.

    With `storage=GEOPACKAGE` the finished layer is written to `gpkg_path` and
    the returned layer reads from that file, so it survives the project being
    closed. A write failure falls back to the memory layer rather than losing
    the join, and is reported through the fourth return value.

    Returns (layer, matched, unmatched, warning).
    """
    if not feature_sets:
        return None, 0, 0, ""

    first = feature_sets[0][0]
    wkid = resolve_wkid(first, fallback_wkid)
    crs = crs_from_wkid(wkid)
    layer = QgsVectorLayer(
        "Point?crs=%s" % (crs.authid() or "EPSG:4326"), layer_name, "memory"
    )
    if not layer.isValid():
        return None, 0, [], ""

    source_fields = _qgs_fields(first.get("fields"))
    source_names = [source_fields.at(i).name() for i in range(source_fields.count())]

    # Result columns are prefixed so they cannot collide with location fields.
    result_fields = [
        QgsField("result_value", QMetaType.Type.Double),
        QgsField("result_unit", QMetaType.Type.QString),
        QgsField("result_date", QMetaType.Type.QString),
        QgsField("result_detect", QMetaType.Type.QString),
        # Whether the plotted number is a detection limit rather than a
        # measurement. DETECT_FLAG alone does not say this: a row can be
        # flagged undetected and still be dropped for want of any limit, and
        # the aggregate only marks at_limit on the row that actually won.
        QgsField("result_at_limit", QMetaType.Type.Int),
        QgsField("result_count", QMetaType.Type.Int),
        QgsField("detect_count", QMetaType.Type.Int),
        QgsField("analyte", QMetaType.Type.QString),
    ]
    # Created up front so sparklines can be applied to a GeoPackage layer
    # without an ALTER TABLE: the OGR provider will add a column, but doing it
    # once here keeps both storage modes on the same schema.
    if spark_field and spark_field not in source_names:
        result_fields.append(QgsField(spark_field, QMetaType.Type.QString))

    provider = layer.dataProvider()
    provider.addAttributes(source_fields.toList() + result_fields)
    layer.updateFields()

    # Match location codes case-insensitively; the report and the feature
    # service do not always agree on case.
    lookup = {str(k).strip().lower(): v for k, v in (summary or {}).items()}

    buffer = []
    matched = 0
    for _, features in feature_sets:
        for item in features:
            attrs = item.get("attributes") or {}
            feature = QgsFeature(layer.fields())
            values = [attrs.get(name) for name in source_names]

            code = str(attrs.get(location_field) or "").strip().lower()
            entry = lookup.get(code)
            if entry and entry.get("value") is not None:
                matched += 1
                values.extend([
                    entry.get("value"),
                    entry.get("unit") or unit_hint,
                    entry["date"].strftime("%Y-%m-%d") if entry.get("date") else None,
                    entry.get("detect") or None,
                    1 if entry.get("at_limit") else 0,
                    entry.get("count"),
                    entry.get("detect_count"),
                    chemical,
                ])
            elif entry:
                # Location was sampled for this analyte but has no usable
                # number; keep the counts so that is distinguishable from
                # "never sampled".
                values.extend([None, entry.get("unit") or unit_hint, None,
                               entry.get("detect") or None,
                               1 if entry.get("at_limit") else 0,
                               entry.get("count"),
                               entry.get("detect_count"), chemical])
            else:
                # NOT IN THE LAYER AT ALL. `summary` holds one entry per
                # location that has a row matching the analyte AND the
                # matrix/fraction/method scope - so no entry means this
                # location was never a candidate, not that it was sampled
                # and came back clean.
                #
                # Drawing it anyway put all 906 of a facility's locations on
                # a "Chloride (maximum, WS, D)" layer, every one of them
                # NULL, because Chloride at this facility is WS/T and WS/N
                # and has no WS/D data whatsoever. A tailings location on a
                # dissolved-water layer is not a visible gap; it is a point
                # that had no business being asked.
                #
                # The left join it replaces was a considered choice and the
                # reasoning still holds for ONE analyte over one monitoring
                # network - a well sampled but never detecting chloride is
                # worth seeing. It stops holding over a whole-facility
                # parquet, where "every location" spans four matrices.
                # Those locations are counted as `unmatched` so the caller
                # can say how many were left out.
                continue
            if spark_field and spark_field not in source_names:
                # Filled in later by style_with_sparklines, if asked for.
                values.append(None)

            feature.setAttributes(values)
            geom = esri_geometry(item.get("geometry"))
            if geom is not None:
                feature.setGeometry(geom)
            buffer.append(feature)

    provider.addFeatures(buffer)
    layer.updateExtents()

    # Features WITHOUT a usable number, not locations left out entirely -
    # those never entered the buffer. Both are worth distinguishing: one is
    # "sampled and came back with nothing usable", the other is "not part of
    # this layer's question".
    unmatched = len(buffer) - matched
    if storage == GEOPACKAGE and gpkg_path:
        # `table_name` lets the caller key the table on the data alone, so two
        # layers that differ only in symbology share one table instead of
        # duplicating identical geometry and attributes in the file.
        saved, error = persist_to_geopackage(layer, gpkg_path,
                                            table_name or layer_name)
        if saved is not None:
            return saved, matched, unmatched, ""
        # Losing the join because the disk is full or the file is locked would
        # be far worse than falling back to a layer that works for this
        # session; say so and carry on.
        return layer, matched, unmatched, error
    return layer, matched, unmatched, ""


# The datetime column QGIS's Temporal Controller is pointed at. Named for
# what it is rather than result_*, since it is a sample property rather than
# a property of the result.
SAMPLE_TIME_FIELD = "sample_time"


def _as_datetime(when):
    """A date as something QGIS will store in a QDateTime column.

    Report dates carry no time of day, so midnight is the only honest
    reading; the Temporal Controller then steps whole days, which is the
    resolution the data actually has.
    """
    if when is None:
        return None
    return QDateTime(QDate(when.year, when.month, when.day), QTime(0, 0))


def enable_temporal(layer, field=SAMPLE_TIME_FIELD):
    """Point QGIS's Temporal Controller at the sample date.

    Without this the field is just another column: the controller has to be
    told which one carries the time and switched on, and doing it here means
    an unaggregated layer arrives ready to animate rather than needing six
    clicks in the layer properties first.

    Left INACTIVE despite being configured. An active temporal layer is
    filtered by the canvas's current time range, so switching it on here
    would hide every feature the moment the layer is added unless the
    controller happens to be showing the right window - the layer would look
    empty and broken. Turning the controller on is the user's move; this just
    means it works when they do.

    Returns True when the layer was configured.
    """
    if layer is None or layer.fields().indexFromName(field) < 0:
        return False
    props = layer.temporalProperties()
    props.setMode(
        QgsVectorLayerTemporalProperties.TemporalMode.ModeFeatureDateTimeInstantFromField)
    props.setStartField(field)
    props.setIsActive(False)
    return True


def build_result_layer(layer_name, feature_sets, results, chemical,
                       unit_hint="", fallback_wkid=0, location_field="code",
                       storage=MEMORY, gpkg_path=None, table_name=None):
    """One point per RESULT, rather than one per location.

    `results` is ReportTable.rows_for() output: a flat list with one dict per
    result row, several of which share a location. build_joined_layer cannot
    express this - it walks the location feature_sets and looks each code up
    once, so a location can only ever produce a single feature - so this
    inverts the loop and walks the results, looking the geometry up by code.
    That is the direction which allows repeats.

    Features are left at their true coordinates, exactly stacked where a
    location has several results. Nothing is offset: a point in a GIS is a
    survey claim, and moving one to make a map legible would make the layer
    lie about where the sample was taken. Use apply_point_displacement() to
    fan them apart at draw time instead.

    Unlike the aggregated layer this is NOT a left join: a location with no
    result for the analyte produces no feature at all, because there is no
    result to hang it on. Those are counted and returned so the caller can
    say so rather than losing them silently.

    Returns (layer, placed, unplaced, warning). `unplaced` is the list of
    distinct location codes that could not be placed, in first-seen order -
    not a count. A bare number ("8 could not be placed") tells the user that
    something is wrong but not which sites to look at, and the codes are the
    whole substance of the problem: they are what differs between the report
    and the location layer.
    """
    if not feature_sets:
        return None, 0, [], ""

    first = feature_sets[0][0]
    wkid = resolve_wkid(first, fallback_wkid)
    crs = crs_from_wkid(wkid)
    layer = QgsVectorLayer(
        "Point?crs=%s" % (crs.authid() or "EPSG:4326"), layer_name, "memory"
    )
    if not layer.isValid():
        return None, 0, [], ""

    source_fields = _qgs_fields(first.get("fields"))
    source_names = [source_fields.at(i).name() for i in range(source_fields.count())]

    # The first eight names and types match build_joined_layer exactly, so
    # style_by_result() and any saved QML work on both layer kinds without
    # being taught the difference.
    result_fields = [
        QgsField("result_value", QMetaType.Type.Double),
        QgsField("result_unit", QMetaType.Type.QString),
        QgsField("result_date", QMetaType.Type.QString),
        QgsField("result_detect", QMetaType.Type.QString),
        QgsField("result_at_limit", QMetaType.Type.Int),
        QgsField("result_count", QMetaType.Type.Int),
        QgsField("detect_count", QMetaType.Type.Int),
        QgsField("analyte", QMetaType.Type.QString),
        # What the aggregate throws away, and the reason this layer exists.
        QgsField("result_matrix", QMetaType.Type.QString),
        QgsField("result_fraction", QMetaType.Type.QString),
        QgsField("sample_type", QMetaType.Type.QString),
        # Which result this is within its location, oldest first. The only
        # thing telling two otherwise identical stacked features apart.
        QgsField("result_seq", QMetaType.Type.Int),
        # A REAL datetime, not the ISO string in result_date: QGIS's Temporal
        # Controller can only animate a date/datetime field, and a string
        # that merely looks like a date is invisible to it. Both are kept -
        # the string is what a label expression and the attribute table read
        # comfortably, the datetime is what the controller needs.
        QgsField(SAMPLE_TIME_FIELD, QMetaType.Type.QDateTime),
        # The sampled interval. A soil boring is sampled every few metres and
        # every one of those results shares the borehole's single coordinate,
        # so without depth the stack is not just crowded, it is unreadable:
        # nothing on the map distinguishes 2 m from 40 m.
        QgsField("start_depth", QMetaType.Type.Double),
        QgsField("end_depth", QMetaType.Type.Double),
        QgsField("depth_unit", QMetaType.Type.QString),
        # Site-specific free text carried on the sample.
        QgsField("custom_1", QMetaType.Type.QString),
        # Which laboratory method produced this result. Two methods on one
        # sample look like duplicate rows without it.
        QgsField("method", QMetaType.Type.QString),
    ]

    provider = layer.dataProvider()
    provider.addAttributes(source_fields.toList() + result_fields)
    layer.updateFields()

    # Location geometry and attributes, keyed the same case-insensitive way
    # build_joined_layer keys its lookup.
    geometry_by_code = {}
    attrs_by_code = {}
    for _, features in feature_sets:
        for item in features:
            attrs = item.get("attributes") or {}
            code = str(attrs.get(location_field) or "").strip().lower()
            if not code or code in geometry_by_code:
                continue
            geom = esri_geometry(item.get("geometry"))
            if geom is None:
                continue
            geometry_by_code[code] = geom
            attrs_by_code[code] = attrs

    buffer = []
    # Distinct codes rather than a count, keeping first-seen order: several
    # results usually share one missing location, and naming that location
    # once is what lets the user go and fix it. The ORIGINAL spelling is
    # kept, not the lowercased key used for matching - the user has to
    # recognise it in the report.
    unplaced = []
    seen_unplaced = set()
    for entry in results or []:
        raw = str(entry.get("location") or "").strip()
        code = raw.lower()
        geom = geometry_by_code.get(code)
        if geom is None:
            # No point to put it on, and no location feature to hang a NULL
            # from either - unlike the joined layer, there is nowhere for an
            # unplaceable result to go.
            if code not in seen_unplaced:
                seen_unplaced.add(code)
                unplaced.append(raw or "(blank)")
            continue
        attrs = attrs_by_code.get(code) or {}
        feature = QgsFeature(layer.fields())
        values = [attrs.get(name) for name in source_names]
        when = entry.get("date")
        detected = 1 if entry.get("detect") in ("Y", "YES", "TRUE", "1") else 0
        values.extend([
            entry.get("value"),
            entry.get("unit") or unit_hint,
            when.strftime("%Y-%m-%d") if when else None,
            entry.get("detect") or None,
            1 if entry.get("at_limit") else 0,
            # One row is one result, so these are per-feature constants. Kept
            # rather than dropped so one symbology serves both layer kinds.
            1,
            detected,
            chemical,
            entry.get("matrix") or None,
            entry.get("fraction") or None,
            entry.get("sample_type") or None,
            entry.get("seq"),
            _as_datetime(when),
            entry.get("start_depth"),
            entry.get("end_depth"),
            entry.get("depth_unit") or None,
            entry.get("custom_1") or None,
            entry.get("method") or None,
        ])
        feature.setAttributes(values)
        feature.setGeometry(QgsGeometry(geom))
        buffer.append(feature)

    provider.addFeatures(buffer)
    layer.updateExtents()
    placed = len(buffer)

    if storage == GEOPACKAGE and gpkg_path:
        saved, error = persist_to_geopackage(layer, gpkg_path,
                                             table_name or layer_name)
        if saved is not None:
            return saved, placed, unplaced, ""
        return layer, placed, unplaced, error
    return layer, placed, unplaced, ""


def apply_point_displacement(layer, tolerance_mm=2.0):
    """Fan coincident results around their shared location, at draw time only.

    An unaggregated layer stacks every result for a location on one point, so
    all but the topmost are hidden and the map looks exactly like a maximum -
    measured at 500 painted pixels for six stacked results against 476 for
    the top marker alone. This wraps whatever renderer the layer already has
    in a displacement renderer, which spreads coincident points into a ring
    and marks the true position with a centre dot.

    Nothing is moved: the geometry still holds the surveyed coordinate, and
    turning this off restores the stack. The existing renderer is embedded
    rather than replaced, so the graduated ramp, the hollow-square
    non-detects and the symbol levels all survive.

    Returns True when a renderer was wrapped.
    """
    inner = layer.renderer()
    if inner is None:
        return False
    if isinstance(inner, QgsPointDisplacementRenderer):
        return True
    renderer = QgsPointDisplacementRenderer()
    renderer.setEmbeddedRenderer(inner.clone())
    renderer.setTolerance(tolerance_mm)
    renderer.setToleranceUnit(QgsUnitTypes.RenderUnit.RenderMillimeters)
    # The ring needs to be visibly a ring rather than a smear, and the centre
    # dot is what tells the reader where the location actually is.
    renderer.setCircleRadiusAddition(0.6)
    layer.setRenderer(renderer)
    layer.triggerRepaint()
    return True


# A non-detect is not a small measurement, it is the absence of one, so it is
# drawn as an outline rather than as a filled marker at the bottom of the
# ramp. The size is fixed: the number behind it is the laboratory's detection
# limit, and graduating on that would read as a concentration gradient where
# there is none.
ND_SIZE_MM = 2.8
ND_OUTLINE = "90,90,90"


def non_detect_symbol(size_mm=ND_SIZE_MM):
    """The hollow square standing for a location whose result was a non-detect."""
    return QgsMarkerSymbol.createSimple(
        {"name": "square", "size": "%g" % size_mm,
         "color": "0,0,0,0",              # hollow: no fill at all
         "outline_color": ND_OUTLINE, "outline_width": "0.4"}
    )


def non_detect_expression(detect_field="result_detect",
                          at_limit_field="result_at_limit"):
    """QGIS expression selecting the features that are non-detects.

    Prefers `result_at_limit`, which the aggregate sets on the row that
    actually won and so is the truer statement of "the number shown is a
    limit". Layers built before that column existed - a reopened GeoPackage,
    for one - still carry the raw flag, so fall back to it rather than
    silently drawing every location as quantified.

    Either field name may be None, meaning the layer does not have it. Naming
    an absent column would make the expression evaluate to NULL rather than
    false, and the negated "detected" rule would then match nothing at all -
    every quantified location would vanish from the map.
    """
    terms = []
    if at_limit_field:
        terms.append('"%s" = 1' % at_limit_field)
    if detect_field:
        terms.append(('("%s" is not null and upper("%s") not in '
                      "('Y','YES','TRUE','1'))") % (detect_field, detect_field))
    if not terms:
        return None
    return " or ".join(terms)


def _has_field(layer, name):
    return layer.fields().indexFromName(name) >= 0


def _non_detect_filter(layer, detect_field="result_detect",
                       at_limit_field="result_at_limit"):
    """The expression matching this layer's non-detects, or None.

    None means the layer carries no detection information at all - a plain
    feature layer, say - in which case every feature is treated as quantified
    and the layer is styled as it always was.
    """
    return non_detect_expression(
        detect_field if _has_field(layer, detect_field) else None,
        at_limit_field if _has_field(layer, at_limit_field) else None,
    )


def _set_render_pass(symbol, pass_index):
    """Put every layer of `symbol` into one symbol-level render pass.

    QGIS draws all symbols in pass 0, then all in pass 1, and so on, rather
    than drawing each feature complete in turn. A symbol whose layers
    disagree about their pass would be split across passes, so they are set
    together.
    """
    if symbol is None:
        return
    for index in range(symbol.symbolLayerCount()):
        symbol.symbolLayer(index).setRenderingPass(pass_index)


def _apply_plain_symbol(layer, nd_expression=None):
    """A single plain marker, with non-detects still hollow.

    Used when there is too little variation to graduate: without it, calling
    style_by_result() to undo sparklines would silently leave the sparkline
    renderer in place. Non-detects keep their own symbol here too, so the
    distinction does not come and go with the number of classes.
    """
    symbol = QgsMarkerSymbol.createSimple(
        {"name": "circle", "size": "3", "color": "254,178,76",
         "outline_color": "120,80,20"}
    )
    if nd_expression is None:
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    else:
        renderer = QgsRuleBasedRenderer(QgsRuleBasedRenderer.Rule(None))
        root = renderer.rootRule()
        # Nothing is graduated here, but non-detects still belong underneath
        # the measurements rather than on top of them.
        nd_symbol = non_detect_symbol()
        _set_render_pass(nd_symbol, 0)
        _set_render_pass(symbol, 1)
        root.appendChild(QgsRuleBasedRenderer.Rule(
            nd_symbol, 0, 0, nd_expression, "Non-detect"))
        root.appendChild(QgsRuleBasedRenderer.Rule(
            symbol, 0, 0, "NOT (%s)" % nd_expression, "Detected"))
        renderer.setUsingSymbolLevels(True)
        layer.setRenderer(renderer)
    layer.triggerRepaint()


def _graduated_ranges(values, classes):
    """Quantile ranges over `values`, or None if it will not graduate.

    Built directly rather than via the deprecated createRenderer(), whose
    returned range/symbol temporaries crash the interpreter when cloned under
    QGIS 4.
    """
    if len(values) < 2 or min(values) == max(values):
        return None
    count = max(2, min(classes, len(set(values))))
    ordered = sorted(values)
    # Quantile breaks, de-duplicated so no two classes share a bound.
    breaks = []
    for i in range(1, count):
        breaks.append(ordered[int(round(i * (len(ordered) - 1) / float(count)))])
    bounds = [ordered[0]] + breaks + [ordered[-1]]
    unique = []
    for bound in bounds:
        if not unique or bound > unique[-1]:
            unique.append(bound)
    if len(unique) < 3:
        return None

    ramp = QgsGradientColorRamp(QColor(255, 255, 178), QColor(189, 0, 38))
    ranges = []
    steps = len(unique) - 1
    for index in range(steps):
        symbol = QgsMarkerSymbol.createSimple({"name": "circle"})
        symbol.setColor(ramp.color(index / float(max(1, steps - 1))))
        symbol.setSize(2.6 + index * 0.9)
        label = "%g - %g" % (unique[index], unique[index + 1])
        ranges.append(
            QgsRendererRange(unique[index], unique[index + 1], symbol, label)
        )
    return ranges


def style_by_result(layer, field="result_value", classes=5,
                    detect_field="result_detect",
                    at_limit_field="result_at_limit"):
    """Graduate quantified results; draw non-detects as hollow squares.

    Non-detects are excluded from the classification as well as from the
    ramp - they are plotted at (half) a detection limit, and letting those
    numbers set the quantile breaks would pull the whole scale toward the
    laboratory's sensitivity instead of the contamination actually measured.

    Returns True when a graduated renderer was applied.
    """
    nd_expression = _non_detect_filter(layer, detect_field, at_limit_field)

    values = []
    nd_count = 0
    context = QgsExpressionContext()
    context.appendScopes(
        QgsExpressionContextUtils.globalProjectLayerScopes(layer))
    matcher = None
    if nd_expression is not None:
        matcher = QgsExpression(nd_expression)
        matcher.prepare(context)
    for feature in layer.getFeatures():
        if matcher is not None:
            context.setFeature(feature)
            if matcher.evaluate(context):
                nd_count += 1
                continue
        value = feature[field]
        if value is not None and str(value) != "NULL":
            values.append(value)

    ranges = _graduated_ranges(values, classes)
    if ranges is None:
        # Too little spread among the quantified results to graduate. The
        # non-detects are still worth distinguishing, so keep the split.
        _apply_plain_symbol(layer, nd_expression if nd_count else None)
        return False

    graduated = QgsGraduatedSymbolRenderer(field, ranges)
    if nd_expression is None or not nd_count:
        # Ascending passes: the largest, darkest markers are drawn last and so
        # sit on top. Without this the draw order is feature order, and a hot
        # spot can disappear under a neighbouring low result.
        for index, entry in enumerate(graduated.ranges()):
            _set_render_pass(entry.symbol(), index)
        graduated.setUsingSymbolLevels(True)
        layer.setRenderer(graduated)
        layer.triggerRepaint()
        return True

    # Rule-based, because a graduated renderer classifies on one field and
    # cannot also branch on the detect flag. Each range is re-expressed as a
    # child rule under a "detected" parent that carries the NOT(non-detect)
    # filter, so the ramp and the class breaks survive but only quantified
    # results can reach them.
    renderer = QgsRuleBasedRenderer(QgsRuleBasedRenderer.Rule(None))
    root = renderer.rootRule()
    # Pass 0: non-detects underneath everything. They are the least
    # interesting thing on the map and should never occlude a measurement.
    nd_symbol = non_detect_symbol()
    _set_render_pass(nd_symbol, 0)
    root.appendChild(QgsRuleBasedRenderer.Rule(
        nd_symbol, 0, 0, nd_expression, "Non-detect"))
    detected = QgsRuleBasedRenderer.Rule(
        None, 0, 0, "NOT (%s)" % nd_expression, "Detected")
    for index, entry in enumerate(ranges):
        # The lowest class takes its lower bound inclusively, matching how a
        # graduated renderer classifies; the rest are (lower, upper].
        operator = ">=" if index == 0 else ">"
        symbol = entry.symbol().clone()
        # Passes 1..n ascending, so the highest class is drawn last and ends
        # up on top; non-detects hold pass 0 beneath them all.
        _set_render_pass(symbol, index + 1)
        rule = QgsRuleBasedRenderer.Rule(
            symbol, 0, 0,
            '"%s" %s %.17g and "%s" <= %.17g' % (
                field, operator, entry.lowerValue(), field, entry.upperValue()),
            entry.label())
        detected.appendChild(rule)
    root.appendChild(detected)
    renderer.setUsingSymbolLevels(True)
    layer.setRenderer(renderer)
    layer.triggerRepaint()
    return True


def spark_ground_size(layer, across=26.0):
    """A sensible on-the-ground marker size for this layer's extent.

    A fixed distance suits one site and not the next: 260 m of marker is
    reasonable across a few kilometres and absurd across a hundred. Sizing it
    as a fraction of the layer's own extent means the markers start out
    proportionate whatever the site's scale, and the millimetre clamps handle
    the rest.
    """
    try:
        extent = layer.extent()
    except (AttributeError, RuntimeError):
        return SPARK_GROUND_SIZE
    if extent is None or extent.isEmpty():
        return SPARK_GROUND_SIZE
    span = max(extent.width(), extent.height())
    if span <= 0:
        return SPARK_GROUND_SIZE
    return span / float(across)


def style_with_sparklines(layer, markers_by_location, location_field="code",
                          path_field="spark_svg", ground_size=None,
                          size_range=None, once_per_location=False):
    """Draw each location as its own time-series sparkline.

    `markers_by_location` maps a location code to whatever QGIS should render:
    either a path to an SVG file or an embedded "base64:..." image. The value
    is stored per feature and bound to the symbol with a data-defined
    property, so one symbol layer serves every location and this function does
    not care which form it was given.

    `size_range` is the (smallest, largest) millimetre pair the markers may
    scale between; None keeps the built-in defaults.

    `once_per_location` gives the marker to a single feature per location and
    leaves the rest without one. An aggregated layer has one feature per
    location and does not need it, but an unaggregated layer has one per
    RESULT - so a boring with twelve samples would otherwise get twelve
    identical copies of its depth profile stacked on the same coordinate,
    which renders as a smear rather than one clean marker.

    Returns the number of features given a sparkline.
    """
    if ground_size is None:
        ground_size = spark_ground_size(layer)
    provider = layer.dataProvider()
    if not markers_by_location:
        # Nothing qualifies: clear any markers a previous run left behind,
        # otherwise stale sparklines stay on the map.
        index = layer.fields().indexFromName(path_field)
        if index >= 0:
            provider.changeAttributeValues(
                {f.id(): {index: None} for f in layer.getFeatures()}
            )
            layer.triggerRepaint()
        return 0
    if layer.fields().indexFromName(path_field) < 0:
        provider.addAttributes([QgsField(path_field, QMetaType.Type.QString)])
        layer.updateFields()

    index = layer.fields().indexFromName(path_field)
    if index < 0:
        # The provider refused the new column, which a read-only GeoPackage
        # would. Nothing can be drawn, and silently returning 0 would look
        # like "no location qualified".
        return 0
    lookup = {str(k).strip().lower(): v for k, v in markers_by_location.items()}
    # Rewrite every feature, so a location that no longer qualifies loses the
    # marker a previous run gave it instead of keeping a stale sparkline.
    updates = {}
    drawn = 0
    claimed = set()
    for feature in layer.getFeatures():
        code = str(feature[location_field] or "").strip().lower()
        marker = lookup.get(code)
        if marker and once_per_location:
            # First feature seen for this location carries the marker; the
            # others are deliberately left empty so the "no marker" rule
            # draws them instead of stacking a duplicate on top.
            if code in claimed:
                marker = None
            else:
                claimed.add(code)
        updates[feature.id()] = {index: marker or None}
        if marker:
            drawn += 1
    if updates:
        provider.changeAttributeValues(updates)

    symbol = QgsMarkerSymbol()
    svg_layer = QgsSvgMarkerSymbolLayer("")
    # Sized so the location label inside the SVG stays readable, and tied to
    # the ground so zooming out shrinks the markers rather than leaving them
    # overlapping at full size.
    low, high = _spark_range(size_range, STATIC_SPARK_NOMINAL_MM)
    _apply_spark_sizing(svg_layer, nominal_mm=high, ground=ground_size,
                        min_mm=low)
    svg_layer.setDataDefinedProperty(
        QgsSymbolLayer.PropertyName,
        QgsProperty.fromExpression('"%s"' % path_field),
    )
    symbol.changeSymbolLayer(0, svg_layer)

    # Locations without a sparkline keep a plain dot rather than vanishing.
    # When one marker stands for a whole location, the other features at that
    # location are not "no data" - they are the very results the marker is
    # drawing - so they get nothing at all rather than a dot under the plot.
    if once_per_location:
        fallback = QgsMarkerSymbol.createSimple(
            {"name": "circle", "size": "0", "color": "0,0,0,0",
             "outline_color": "0,0,0,0"}
        )
    else:
        fallback = QgsMarkerSymbol.createSimple(
            {"name": "circle", "size": "2", "color": "180,180,180,180",
             "outline_color": "110,110,110,200"}
        )
    renderer = QgsRuleBasedRenderer(fallback.clone())
    root = renderer.rootRule()
    has_spark = QgsRuleBasedRenderer.Rule(
        symbol, 0, 0, '"%s" is not null' % path_field, "Time series")
    no_spark = QgsRuleBasedRenderer.Rule(
        fallback, 0, 0, '"%s" is null' % path_field, "No data")
    root.appendChild(has_spark)
    root.appendChild(no_spark)
    layer.setRenderer(renderer)
    layer.triggerRepaint()
    return drawn


# The layer variable the rolling renderer reads to decide which analyte's
# marker column to draw. Changing it and repainting is the whole animation:
# no data is rewritten, so nothing is dirtied and nothing hits the disk.
ROLL_VARIABLE = "equis_analyte_column"

# Sparkline markers are sized as a real distance on the ground rather than a
# fixed number of millimetres, so zooming out shrinks them instead of leaving
# a solid mat of overlapping markers. The millimetre clamps keep them legible
# when zoomed right in and visible when zoomed right out.
SPARK_GROUND_SIZE = 260.0     # metres across, at the layer's CRS
SPARK_MIN_MM = 4.0
SPARK_MAX_MM = 16.0
# What the marker measures when neither clamp is binding. Sparklines carry two
# captions and a trend, so they need more room than a plain symbol.
SPARK_NOMINAL_MM = 16.0

# How far the size range can be opened up beyond the defaults above. The
# floor is where a captioned sparkline stops being readable at all, and the
# ceiling is half again the old fixed maximum, which is as large as a marker
# can get before it covers its neighbours at any useful zoom.
SPARK_LIMIT_MIN_MM = 1.0
SPARK_LIMIT_MAX_MM = SPARK_MAX_MM * 1.5

# The two markers are not the same size: the rolling one carries an analyte
# caption as well as a location code, so it needs more room. Kept as their
# share of SPARK_NOMINAL_MM rather than as absolute millimetres, so a user
# who widens the range keeps that difference instead of flattening the two
# markers to one size.
STATIC_SPARK_NOMINAL_MM = 13.2
ROLL_SPARK_NOMINAL_MM = 15.6


def clamp_spark_range(min_mm, max_mm):
    """Coerce a (min, max) millimetre pair into a usable size range.

    The UI is not the only caller - a saved setting can be stale or hand-
    edited - so the bounds are enforced here rather than trusted from
    outside. A crossed pair is straightened rather than rejected: an
    inverted range would make QGIS clamp every marker to nothing.
    """
    try:
        low = float(min_mm)
        high = float(max_mm)
    except (TypeError, ValueError):
        return SPARK_MIN_MM, SPARK_MAX_MM
    low = min(max(low, SPARK_LIMIT_MIN_MM), SPARK_LIMIT_MAX_MM)
    high = min(max(high, SPARK_LIMIT_MIN_MM), SPARK_LIMIT_MAX_MM)
    if high < low:
        low, high = high, low
    return low, high


def _spark_range(size_range, nominal_mm):
    """The (min, max) millimetres a marker of this kind should scale between.

    `nominal_mm` is what this marker measured before the range was
    adjustable. A chosen maximum is applied in proportion to it rather than
    verbatim, so the rolling marker stays the larger of the two at every
    setting instead of the two collapsing to the same size.
    """
    if not size_range:
        return SPARK_MIN_MM, nominal_mm
    low, high = clamp_spark_range(size_range[0], size_range[1])
    scaled = high * nominal_mm / SPARK_NOMINAL_MM
    return clamp_spark_range(low, scaled)


def _apply_spark_sizing(svg_layer, nominal_mm=SPARK_NOMINAL_MM, ground=None,
                        min_mm=SPARK_MIN_MM):
    """Size an SVG marker so it scales with the view, within limits.

    A marker fixed in millimetres stays the same on screen however far out you
    zoom, so a whole facility becomes an unreadable overlap. Sizing in map
    units ties the marker to the ground instead; the min/max clamps stop it
    vanishing when zoomed out or swamping the map when zoomed in.

    `ground` is only a starting point - the clamps decide the outcome at most
    zooms - so a caller that knows its site's extent can pass its own.

    `min_mm` and `nominal_mm` are the two ends of the size range, which the
    panel's slider drives; they are clamped here so no caller can invert them.
    """
    low, high = clamp_spark_range(min_mm, nominal_mm)
    svg_layer.setSizeUnit(QgsUnitTypes.RenderUnit.RenderMapUnits)
    svg_layer.setSize(ground if ground else SPARK_GROUND_SIZE)
    scale = QgsMapUnitScale()
    scale.minSizeMMEnabled = True
    scale.minSizeMM = low
    scale.maxSizeMMEnabled = True
    scale.maxSizeMM = high
    svg_layer.setSizeMapUnitScale(scale)


def roll_column_name(index):
    """Attribute holding the marker for the nth analyte in the roll."""
    return "spark_%d" % index


def style_with_rolling_sparklines(layer, markers_by_analyte,
                                  location_field="code", size_range=None):
    """Prepare a layer to cycle its markers between several analytes.

    `markers_by_analyte` is an ordered sequence of (analyte, {location: uri})
    pairs. Each analyte gets its own attribute column, and the renderer reads
    whichever column the ROLL_VARIABLE names, so advancing the roll is one
    variable assignment plus a repaint - about a millisecond, with no writes
    to the layer at all.

    `size_range` is the (smallest, largest) millimetre pair the markers may
    scale between; None keeps the built-in defaults.

    Returns the list of column names, in roll order.
    """
    provider = layer.dataProvider()
    columns = []
    missing = []
    for index in range(len(markers_by_analyte)):
        name = roll_column_name(index)
        columns.append(name)
        if layer.fields().indexFromName(name) < 0:
            missing.append(QgsField(name, QMetaType.Type.QString))
    if missing:
        provider.addAttributes(missing)
        layer.updateFields()

    # One pass over the features, filling every analyte's column at once:
    # re-reading the layer per analyte is what would make this slow.
    indexes = [layer.fields().indexFromName(name) for name in columns]
    if any(i < 0 for i in indexes):
        return []
    lookups = [
        {str(k).strip().lower(): v for k, v in markers.items()}
        for _, markers in markers_by_analyte
    ]
    updates = {}
    for feature in layer.getFeatures():
        code = str(feature[location_field] or "").strip().lower()
        updates[feature.id()] = {
            index: lookup.get(code) or None
            for index, lookup in zip(indexes, lookups)
        }
    if updates:
        provider.changeAttributeValues(updates)

    symbol = QgsMarkerSymbol()
    svg_layer = QgsSvgMarkerSymbolLayer("")
    # Larger than the plain sparkline marker because it carries the analyte
    # caption as well as the location code, and tied to the ground so zooming
    # out shrinks it.
    low, high = _spark_range(size_range, ROLL_SPARK_NOMINAL_MM)
    _apply_spark_sizing(svg_layer, nominal_mm=high,
                        ground=spark_ground_size(layer), min_mm=low)
    svg_layer.setDataDefinedProperty(
        QgsSymbolLayer.PropertyName,
        QgsProperty.fromExpression("attribute(@%s)" % ROLL_VARIABLE),
    )
    symbol.changeSymbolLayer(0, svg_layer)

    fallback = QgsMarkerSymbol.createSimple(
        {"name": "circle", "size": "2", "color": "180,180,180,180",
         "outline_color": "110,110,110,200"}
    )
    renderer = QgsRuleBasedRenderer(fallback.clone())
    root = renderer.rootRule()
    root.appendChild(QgsRuleBasedRenderer.Rule(
        symbol, 0, 0, "attribute(@%s) is not null" % ROLL_VARIABLE,
        "Time series"))
    root.appendChild(QgsRuleBasedRenderer.Rule(
        fallback, 0, 0, "attribute(@%s) is null" % ROLL_VARIABLE, "No data"))
    layer.setRenderer(renderer)

    set_roll_column(layer, columns[0] if columns else "")
    return columns


def set_roll_column(layer, column):
    """Point the rolling renderer at one analyte's markers and repaint.

    This is the per-tick operation, so it does no work beyond setting a
    variable: the renderer resolves the column name on the next draw.
    """
    QgsExpressionContextUtils.setLayerVariable(layer, ROLL_VARIABLE, column)
    layer.triggerRepaint()


def _find_column(headers, candidates):
    upper = [h.strip().upper() for h in headers]
    for want in candidates:
        if want in upper:
            return upper.index(want)
    return -1


def find_coordinate_columns(headers):
    """Locate the X/Y columns in a report's header row, or (-1, -1)."""
    return _find_column(headers, X_COLUMNS), _find_column(headers, Y_COLUMNS)


def _looks_numeric(values):
    seen = False
    for value in values:
        text = (value or "").strip()
        if not text:
            continue
        try:
            float(text)
        except ValueError:
            return False
        seen = True
    return seen


def build_report_layer(layer_name, headers, rows, wkid, x_index=None, y_index=None):
    """Build a point layer from report rows using their coordinate columns.

    Rows without usable coordinates are still added, without geometry, so the
    attribute data is never silently dropped.
    """
    if not headers:
        return None, 0, 0

    if x_index is None or y_index is None:
        x_index, y_index = find_coordinate_columns(headers)
    has_geometry = x_index >= 0 and y_index >= 0

    crs = crs_from_wkid(wkid)
    if has_geometry:
        uri = "Point?crs=%s" % (crs.authid() or "EPSG:4326")
    else:
        uri = "None"
    layer = QgsVectorLayer(uri, layer_name, "memory")
    if not layer.isValid():
        return None, 0, 0

    # Type each column by sampling: numeric-looking columns become doubles so
    # they can drive graduated styling, everything else stays text.
    sample = rows[:200]
    column_types = []
    for index in range(len(headers)):
        values = [r[index] if index < len(r) else "" for r in sample]
        column_types.append(
            QMetaType.Type.Double if _looks_numeric(values) else QMetaType.Type.QString
        )

    # Crosstab reports can repeat a column name; QGIS silently drops duplicate
    # fields, which would misalign every attribute after the first clash.
    unique_names = []
    used = set()
    for index, name in enumerate(headers):
        candidate = (name or "").strip() or "field_%d" % index
        base = candidate
        suffix = 2
        while candidate.lower() in used:
            candidate = "%s_%d" % (base, suffix)
            suffix += 1
        used.add(candidate.lower())
        unique_names.append(candidate)

    provider = layer.dataProvider()
    provider.addAttributes(
        [QgsField(unique_names[i], column_types[i]) for i in range(len(headers))]
    )
    layer.updateFields()

    buffer = []
    placed = 0
    for row in rows:
        feature = QgsFeature(layer.fields())
        attrs = []
        for index in range(len(headers)):
            raw = row[index] if index < len(row) else ""
            text = (raw or "").strip()
            if column_types[index] == QMetaType.Type.Double:
                try:
                    attrs.append(float(text) if text else None)
                except ValueError:
                    attrs.append(None)
            else:
                attrs.append(text if text else None)
        feature.setAttributes(attrs)

        if has_geometry:
            try:
                x = float((row[x_index] or "").strip())
                y = float((row[y_index] or "").strip())
                if (x or y) and x == x and y == y:
                    feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
                    placed += 1
            except (ValueError, IndexError):
                pass
        buffer.append(feature)

    provider.addFeatures(buffer)
    layer.updateExtents()
    return layer, placed, len(rows) - placed


# ------------------------------------------------------------- bivariate map

# The scatter's symbols on the map, so a triangle means the same thing in
# both places. QGIS's simple-marker names for scatter.js's CLASSES.
_BIVARIATE_SHAPES = {"both": "circle", "x_only": "triangle",
                     "y_only": "square", "neither": "cross2"}


def build_bivariate_layer(layer_name, feature_sets, stations, x_meta, y_meta,
                          breaks, fallback_wkid=0, location_field="code"):
    """One point per station, coloured by a two-analyte class (US-24).

    `stations` is bivariate.stations(): one real sample per location, its
    most recent carrying both analytes. `breaks` is bivariate.axis_breaks().
    Both-detected stations take a 3x3 colour; the three classes involving a
    non-detect are grey, in the scatter's symbol for that class, so the map
    and the chart share one key (decided 2026-10-09). Palette, breaks and
    keys all come from bivariate.py - nothing here decides a class.

    Returns (layer, placed, unplaced): stations matched to a location point,
    and codes that had no point to draw on.
    """
    from . import bivariate as bv

    if not feature_sets:
        return None, 0, []
    first = feature_sets[0][0]
    crs = crs_from_wkid(resolve_wkid(first, fallback_wkid))
    layer = QgsVectorLayer("Point?crs=%s" % (crs.authid() or "EPSG:4326"),
                           layer_name, "memory")
    if not layer.isValid():
        return None, 0, []
    source_fields = _qgs_fields(first.get("fields"))
    source_names = [source_fields.at(i).name()
                    for i in range(source_fields.count())]
    extra = [
        QgsField("bv_key", QMetaType.Type.QString),
        QgsField("bv_sample", QMetaType.Type.QString),
        QgsField("bv_date", QMetaType.Type.QString),
        QgsField("bv_x_analyte", QMetaType.Type.QString),
        QgsField("bv_x", QMetaType.Type.Double),
        QgsField("bv_x_detected", QMetaType.Type.Int),
        QgsField("bv_x_qualifier", QMetaType.Type.QString),
        QgsField("bv_y_analyte", QMetaType.Type.QString),
        QgsField("bv_y", QMetaType.Type.Double),
        QgsField("bv_y_detected", QMetaType.Type.Int),
        QgsField("bv_y_qualifier", QMetaType.Type.QString),
        QgsField("bv_samples_here", QMetaType.Type.Int),
        # How many samples share the chosen sample's date here. >1 means the
        # pick among them was by sample code, i.e. arbitrary (replicates).
        QgsField("bv_tied", QMetaType.Type.Int),
    ]
    provider = layer.dataProvider()
    provider.addAttributes(source_fields.toList() + extra)
    layer.updateFields()

    xb = (breaks.get("x") or {}).get("breaks") or []
    yb = (breaks.get("y") or {}).get("breaks") or []
    by_code = {str(s["location"]).strip().lower(): s for s in stations}
    placed, seen, buffer = 0, set(), []
    for _, features in feature_sets:
        for item in features:
            attrs = item.get("attributes") or {}
            code = str(attrs.get(location_field) or "").strip().lower()
            station = by_code.get(code)
            if station is None or code in seen:
                continue
            seen.add(code)
            key = (bv.key_for(station["x"], station["y"], station["x_detected"],
                              station["y_detected"], xb, yb)
                   if xb and yb else
                   bv.detection_class(station["x_detected"],
                                      station["y_detected"]))
            when = station.get("date")
            feature = QgsFeature(layer.fields())
            feature.setAttributes([attrs.get(n) for n in source_names] + [
                key, station["sample"],
                when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else when,
                x_meta.get("chemical"), station["x"], int(station["x_detected"]),
                station.get("x_qualifier"),
                y_meta.get("chemical"), station["y"], int(station["y_detected"]),
                station.get("y_qualifier"), station.get("samples_here"),
                station.get("tied") or 1,
            ])
            geom = esri_geometry(item.get("geometry"))
            if geom is not None:
                feature.setGeometry(geom)
            buffer.append(feature)
            placed += 1
    provider.addFeatures(buffer)
    layer.updateExtents()
    layer.setRenderer(_bivariate_renderer(x_meta, y_meta, xb, yb))
    unplaced = sorted(s["location"] for c, s in by_code.items() if c not in seen)
    return layer, placed, unplaced


def _bivariate_renderer(x_meta, y_meta, xb, yb):
    """Nine colours (high-high first) then the three grey non-detect classes."""
    from . import bivariate as bv

    def span(breaks, cls):
        fmt = lambda v: ("%.3g" % v)
        if cls == 1:
            return "<= %s" % fmt(breaks[0])
        if cls == len(breaks) + 1:
            return "> %s" % fmt(breaks[-1])
        return "%s-%s" % (fmt(breaks[cls - 2]), fmt(breaks[cls - 1]))

    xn, yn = x_meta.get("chemical", "x"), y_meta.get("chemical", "y")
    categories = []
    if xb and yb:
        for yc in (3, 2, 1):
            for xc in (3, 2, 1):
                key = "%d-%d" % (xc, yc)
                symbol = QgsMarkerSymbol.createSimple({
                    "name": "circle", "color": bv.PALETTE[key],
                    "outline_color": "#4d4d4d", "outline_width": "0.2",
                    "size": "3"})
                categories.append(QgsRendererCategory(
                    key, symbol, "%s %s, %s %s" % (xn, span(xb, xc),
                                                   yn, span(yb, yc))))
    else:
        symbol = QgsMarkerSymbol.createSimple({
            "name": "circle", "color": "#4d4d4d", "size": "3"})
        categories.append(QgsRendererCategory("both", symbol, "both detected"))
    labels = {"x_only": "%s detected, %s not" % (xn, yn),
              "y_only": "%s detected, %s not" % (yn, xn),
              "neither": "neither detected (both at limits)"}
    for key in ("x_only", "y_only", "neither"):
        symbol = QgsMarkerSymbol.createSimple({
            "name": _BIVARIATE_SHAPES[key], "color": "0,0,0,0",
            "outline_color": bv.GREY, "outline_width": "0.5", "size": "3.2"})
        categories.append(QgsRendererCategory(key, symbol, labels[key]))
    return QgsCategorizedSymbolRenderer("bv_key", categories)


def build_ternary_layer(layer_name, feature_sets, stations, metas,
                        fallback_wkid=0, location_field="code"):
    """One point per station, coloured by a three-analyte composition.

    `stations` is ternary.stations(): one real sample per location (the
    Points mode's pick), each with its shares and its colour - decided in
    ternary.py, which the page's SQL is also checked against, so a station
    here and its point in the triangle are one colour. A station with any
    non-detect is grey and hollow, as in the triangle (decided 2026-10-12).

    Returns (layer, placed, unplaced), as build_bivariate_layer does.
    """
    if not feature_sets:
        return None, 0, []
    first = feature_sets[0][0]
    crs = crs_from_wkid(resolve_wkid(first, fallback_wkid))
    layer = QgsVectorLayer("Point?crs=%s" % (crs.authid() or "EPSG:4326"),
                           layer_name, "memory")
    if not layer.isValid():
        return None, 0, []
    source_fields = _qgs_fields(first.get("fields"))
    source_names = [source_fields.at(i).name()
                    for i in range(source_fields.count())]
    extra = [QgsField("tn_class", QMetaType.Type.QString),
             QgsField("tn_colour", QMetaType.Type.QString),
             QgsField("tn_sample", QMetaType.Type.QString),
             QgsField("tn_date", QMetaType.Type.QString)]
    for tag in ("a", "b", "c"):
        extra += [QgsField("tn_%s_analyte" % tag, QMetaType.Type.QString),
                  QgsField("tn_%s" % tag, QMetaType.Type.Double),
                  QgsField("tn_%s_share" % tag, QMetaType.Type.Double),
                  QgsField("tn_%s_detected" % tag, QMetaType.Type.Int)]
    extra += [QgsField("tn_samples_here", QMetaType.Type.Int),
              QgsField("tn_tied", QMetaType.Type.Int)]
    provider = layer.dataProvider()
    provider.addAttributes(source_fields.toList() + extra)
    layer.updateFields()

    by_code = {str(s["location"]).strip().lower(): s for s in stations}
    placed, seen, buffer = 0, set(), []
    for _, features in feature_sets:
        for item in features:
            attrs = item.get("attributes") or {}
            code = str(attrs.get(location_field) or "").strip().lower()
            station = by_code.get(code)
            if station is None or code in seen:
                continue
            seen.add(code)
            when = station.get("date")
            values = [attrs.get(n) for n in source_names] + [
                "all_detected" if all(station["detected"]) else "non_detect",
                station["colour"], station["sample"],
                when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else when]
            for meta, value, share, detected in zip(
                    metas, station["values"], station["shares"],
                    station["detected"]):
                values += [meta.get("chemical"), value, share, int(detected)]
            values += [station.get("samples_here"), station.get("tied") or 1]
            feature = QgsFeature(layer.fields())
            feature.setAttributes(values)
            geom = esri_geometry(item.get("geometry"))
            if geom is not None:
                feature.setGeometry(geom)
            buffer.append(feature)
            placed += 1
    provider.addFeatures(buffer)
    layer.updateExtents()
    layer.setRenderer(_ternary_renderer(metas))
    unplaced = sorted(s["location"] for c, s in by_code.items() if c not in seen)
    return layer, placed, unplaced


def _ternary_renderer(metas):
    """Detected on all three: the station's own colour; otherwise grey, hollow."""
    from . import bivariate as bv
    names = " : ".join(m.get("chemical", "?") for m in metas)
    solid = QgsMarkerSymbol.createSimple({
        "name": "circle", "color": "#4d4d4d", "outline_color": "#4d4d4d",
        "outline_width": "0.2", "size": "3"})
    # Each station's colour is its composition: a field, not a category.
    solid.symbolLayer(0).setDataDefinedProperty(
        QgsSymbolLayer.PropertyFillColor,
        QgsProperty.fromField("tn_colour"))
    hollow = QgsMarkerSymbol.createSimple({
        "name": "circle", "color": "0,0,0,0", "outline_color": bv.GREY,
        "outline_width": "0.5", "size": "3.2"})
    return QgsCategorizedSymbolRenderer("tn_class", [
        QgsRendererCategory("all_detected", solid,
                            "%s, all detected (colour = composition)" % names),
        QgsRendererCategory("non_detect", hollow,
                            "a non-detect: placed at its limit"),
    ])
