"""The map half of a brush: a small highlight layer over a static base.

US-1 / US-3 / US-24. The arrangement is not a style choice - it is the one
that was measured, in tools/mosaic_poc/README.md, against the two obvious
alternatives:

| approach | 250,000 points, 2% brushed | why |
|---|---|---|
| `selectByIds` | 2.1 s (memory), 3.9 s (gpkg) | QGIS repaints all 250,000 |
| `setSubsetString` | 0.36 s | the provider re-filters every row |
| **two layers** | **49.8 ms** | the base is unchanged, so it is cached |

The base layer never changes while a brush moves, so QGIS serves it from the
canvas cache - measured at 597 ms saved of a 764 ms frame at 250,000 points.
Only the small highlight layer is rebuilt, and its cost scales with the
SELECTION rather than the dataset. That is the whole idea.

DIM, DO NOT REMOVE (US-24 criterion 6, superseding US-3). The unbrushed
points stay on the map, faded. Removing them answers "where are the high
values" while destroying the context that makes the answer mean anything -
a cluster of three exceedances reads completely differently when you can
see whether it sits among thirty measurements or among three.

WIDE BRUSHES ARE A CONSTRAINT, NOT A BUG. At 20% of 250,000 points the
highlight costs 397 ms, and nothing rescues that. 50,000 solid markers is
not a picture anyone reads, so the honest response is to say so rather than
to optimise toward an unreadable frame.
"""

from qgis.core import (QgsFeature, QgsFillSymbol, QgsMarkerSymbol,
                       QgsProject, QgsVectorLayer)

# Past this many highlighted features the picture stops being readable, and
# the render cost climbs steeply (397 ms at 50,000). The caller is told
# rather than silently given a slow, unreadable frame.
READABLE_HIGHLIGHT = 20000

HIGHLIGHT_SUFFIX = " (brushed)"


class BrushLayer(object):
    """A highlight layer that follows a brush over a base layer.

    Holds no reference to the web view or the store: it is given feature
    ids and rebuilds itself. That keeps it testable without a browser and
    means the same object can serve a brush from anywhere - a chart, a
    selection, a filter statement.
    """

    def __init__(self, base_layer, project=None):
        self.base = base_layer
        self.project = project or QgsProject.instance()
        self.layer = None
        self.last_count = 0
        self.overfull = False

    # ------------------------------------------------------------- building

    def _ensure_layer(self):
        if self.layer is not None and self.layer.isValid():
            return self.layer
        provider = self.base.dataProvider()
        geometry = {1: "Point", 2: "LineString", 3: "Polygon"}.get(
            self.base.geometryType(), "Point")
        crs = self.base.crs().authid() or "EPSG:4326"
        self.layer = QgsVectorLayer("%s?crs=%s" % (geometry, crs),
                                    (self.base.name() or "layer")
                                    + HIGHLIGHT_SUFFIX,
                                    "memory")
        self.layer.dataProvider().addAttributes(provider.fields())
        self.layer.updateFields()
        self._style()
        self.project.addMapLayer(self.layer, False)
        # Above the base, which the caller has already placed. The False
        # above keeps it out of the legend; insertLayer puts it back, which
        # is the only way to control draw order - so the NODE is hidden
        # instead. The layer is machinery, not something the user added:
        # it appears and disappears as a brush moves, it cannot be styled
        # meaningfully on its own, and deleting it would just break the
        # brush until the next drag rebuilt it.
        root = self.project.layerTreeRoot()
        node = root.insertLayer(0, self.layer)
        if node is not None:
            node.setCustomProperty("nodeHidden", "true")
            # The legend widget caches its model, so a node hidden after
            # the tree was built stays visible until something invalidates
            # it. Toggling visibility is the cheapest nudge that does.
            node.setItemVisibilityChecked(True)
        return self.layer

    def _style(self):
        """Solid and slightly larger than the base, so it reads as emphasis.

        Deliberately NOT a different hue: the base layer is usually already
        graduated by concentration, and recolouring the brushed subset would
        destroy the reading the user came for.
        """
        if self.base.geometryType() == 3:
            symbol = QgsFillSymbol.createSimple({
                "color": "255,255,255,0",
                "outline_color": "20,20,20,255",
                "outline_width": "0.8",
            })
        else:
            symbol = QgsMarkerSymbol.createSimple({
                "name": "circle",
                "color": "255,255,255,0",
                "outline_color": "20,20,20,255",
                "outline_width": "0.5",
                "size": "3.2",
            })
        self.layer.renderer().setSymbol(symbol)

    # -------------------------------------------------------------- updating

    def show(self, features):
        """Replace the highlight with `features` (QgsFeature or (id, geom)).

        Rebuilt rather than edited: a memory layer of a few thousand
        features is faster to recreate than to diff, and diffing is where
        stale highlights come from.
        """
        layer = self._ensure_layer()
        provider = layer.dataProvider()
        layer.startEditing()
        provider.truncate()
        prepared = []
        for item in features:
            if isinstance(item, QgsFeature):
                prepared.append(item)
            else:
                feature = QgsFeature(layer.fields())
                feature.setGeometry(item[1])
                if len(item) > 2:
                    feature.setAttributes(list(item[2]))
                prepared.append(feature)
        if prepared:
            provider.addFeatures(prepared)
        layer.commitChanges()
        layer.updateExtents()
        layer.triggerRepaint()
        self.last_count = len(prepared)
        self.overfull = self.last_count > READABLE_HIGHLIGHT
        return self.last_count

    def clear(self):
        """No brush: an empty highlight, with the base still fully drawn."""
        if self.layer is None:
            return 0
        self.layer.startEditing()
        self.layer.dataProvider().truncate()
        self.layer.commitChanges()
        self.layer.triggerRepaint()
        self.last_count = 0
        self.overfull = False
        return 0

    def advice(self):
        """What to tell the user when a brush is too wide to read.

        Returns None when there is nothing worth saying. A message rather
        than a silent slow frame: the cost is inherent to drawing tens of
        thousands of markers, so the useful response is to narrow the brush.
        """
        if not self.overfull:
            return None
        return ("%s points are brushed. Narrow the range: past about %s the "
                "markers overlap into a solid mass and the map stops being "
                "readable." % ("{:,}".format(self.last_count),
                               "{:,}".format(READABLE_HIGHLIGHT)))

    def remove(self):
        if self.layer is not None:
            self.project.removeMapLayer(self.layer.id())
            self.layer = None
        self.last_count = 0
        self.overfull = False
