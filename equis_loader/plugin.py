"""Plugin entry point: menu/toolbar wiring."""

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .dialog import EquisDialog

# Named for what it does, not for EQuIS: the plugin is not an EarthSoft
# product, and a menu called EQuIS reads as though it were.
MENU = "&Chemistry Explorer"


class EquisLoaderPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.pick_action = None
        self.dialog = None

    def initGui(self):
        here = os.path.dirname(__file__)
        icon = self._icon(os.path.join(here, "icon.svg"))
        self.action = QAction(icon, "QGIS Chemistry Explorer...",
                              self.iface.mainWindow())
        self.action.setObjectName("equisLoaderAction")
        self.action.triggered.connect(self.run)

        # A checkable map tool, so it behaves like Identify or Pan: click to
        # arm, click again (or pick another tool) to release the cursor.
        self.pick_action = QAction(
            self._icon(os.path.join(here, "icon_timeseries.svg")),
            "Chemistry Time Series", self.iface.mainWindow()
        )
        self.pick_action.setObjectName("equisTimeSeriesAction")
        self.pick_action.setCheckable(True)
        self.pick_action.setToolTip(
            "Chemistry Time Series\n"
            "Click a location on a result layer to plot the selected\n"
            "chemical's history there."
        )
        self.pick_action.setEnabled(False)
        self.pick_action.triggered.connect(self._on_pick_toggled)

        for action in (self.action, self.pick_action):
            self.iface.addToolBarIcon(action)
            self.iface.addPluginToWebMenu(MENU, action)

    @staticmethod
    def _icon(path):
        return QIcon(path) if os.path.exists(path) else QIcon()

    def unload(self):
        # Stop answering equis:// first. A handler left pointing at a panel
        # that is about to be deleted would swallow the link and do nothing,
        # which is worse than the link failing visibly.
        try:
            from . import url_actions
            url_actions.unregister()
        except Exception:                      # noqa: BLE001
            pass
        for attr in ("action", "pick_action"):
            action = getattr(self, attr, None)
            if action is not None:
                self.iface.removeToolBarIcon(action)
                self.iface.removePluginWebMenu(MENU, action)
                setattr(self, attr, None)
        if self.dialog is not None:
            self.dialog.shutdown()
            # The plot dock is a sibling in the main window, not a child of
            # the panel, so removing the panel alone would strand it.
            plot_dock = getattr(self.dialog, "plot_dock", None)
            if plot_dock is not None:
                self.iface.removeDockWidget(plot_dock)
                plot_dock.deleteLater()
            self.iface.removeDockWidget(self.dialog)
            self.dialog.deleteLater()
            self.dialog = None

    def run(self):
        # The panel is kept alive between openings: a loaded report is
        # expensive (minutes) and describes what is already on the map, so
        # reopening must show the active session rather than start over.
        if self.dialog is None:
            self.dialog = EquisDialog(self.iface, self.iface.mainWindow(),
                                      pick_action=self.pick_action)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea,
                                     self.dialog)
            # The plot is its own dock so it can be floated and made large -
            # a chart worth zooming into needs room the control panel cannot
            # spare. Bottom by default: a time series is wide and short, so
            # the map window's full width suits it better than a column.
            # Hidden until there is something to plot, so the workspace is
            # not cluttered by an empty chart before a location is clicked.
            self.iface.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea,
                                     self.dialog.plot_dock)
            self.dialog.plot_dock.hide()
            # `equis://` links - from a map tip, an attribute form, anywhere
            # Qt opens a URL - drive this panel. Registered once the panel
            # exists, because a link has nothing to act on without it.
            from . import url_actions
            url_actions.register(self.dialog)
        self.dialog.reopen()
        self.dialog.show()
        self.dialog.raise_()

    def _on_pick_toggled(self, checked):
        if self.dialog is not None:
            self.dialog.set_pick_tool_active(checked)
        elif checked and self.pick_action is not None:
            # Nothing to pick from until a result layer exists.
            self.pick_action.setChecked(False)
