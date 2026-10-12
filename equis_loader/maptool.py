"""Map tool that opens a time series for the location under the cursor."""

from qgis.core import QgsGeometry, QgsPointXY, QgsRectangle
from qgis.gui import QgsMapTool
from qgis.PyQt.QtCore import pyqtSignal

# Click tolerance in screen pixels, converted to map units per canvas scale.
TOLERANCE_PX = 8


class LocationPickTool(QgsMapTool):
    """Emits the location code of the clicked feature."""

    locationPicked = pyqtSignal(str)

    def __init__(self, canvas, layer, location_field="code"):
        super().__init__(canvas)
        self.canvas = canvas
        self.layer = layer
        self.location_field = location_field

    def canvasReleaseEvent(self, event):
        if self.layer is None:
            return
        point = self.toMapCoordinates(event.pos())
        tolerance = self.canvas.mapUnitsPerPixel() * TOLERANCE_PX
        box = QgsRectangle(
            point.x() - tolerance, point.y() - tolerance,
            point.x() + tolerance, point.y() + tolerance,
        )

        # The layer may be in a different CRS than the canvas.
        transform = self.canvas.mapSettings().layerTransform(self.layer)
        if transform is not None and transform.isValid() and not transform.isShortCircuited():
            try:
                box = transform.transformBoundingBox(box, transform.ReverseTransform)
            except Exception:  # noqa: BLE001 - fall back to untransformed box
                pass

        # Several points can fall inside the tolerance box; take the closest.
        target = QgsGeometry.fromPointXY(QgsPointXY(box.center()))
        nearest = None
        nearest_distance = None
        for feature in self.layer.getFeatures(box):
            if not feature.hasGeometry():
                continue
            distance = feature.geometry().distance(target)
            if nearest_distance is None or distance < nearest_distance:
                nearest, nearest_distance = feature, distance

        if nearest is None:
            return
        if self.location_field not in [f.name() for f in self.layer.fields()]:
            return
        code = nearest[self.location_field]
        if code:
            self.locationPicked.emit(str(code))
