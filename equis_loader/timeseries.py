"""Time series plot shown when a location is clicked on the map.

Two things live here: the plot widget itself, and a dock that carries it.

The plot began as a strip at the bottom of the main panel, sharing a ~380 px
dock with six steps of controls. That is the right size for confirming a
click landed on the intended location, and the wrong size for reading a
chart: the matplotlib pan/zoom toolbar was there the whole time, but zooming
into a 200 px-tall strip only makes the crowding worse. The sparklines are
already the small view of a series; this is meant to be the large one.

So the plot can be undocked - floated, resized, dragged to another screen -
independently of the panel that drives it.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QLabel,
    QVBoxLayout,
    QWidget,
)

# matplotlib ships with QGIS, but a missing/short install should degrade to a
# message rather than break the click handler.
try:
    import matplotlib
    import matplotlib.dates as mdates
    from matplotlib.backends.backend_qtagg import (
        FigureCanvasQTAgg,
        NavigationToolbar2QT,
    )
    from matplotlib.figure import Figure

    HAVE_MATPLOTLIB = True
except ImportError:  # pragma: no cover - depends on the QGIS install
    HAVE_MATPLOTLIB = False


def _is_detected(point):
    """True for a measured detection, false for a non-detect.

    A point plotted at its detection limit is a non-detect even if the flag is
    missing, since the value is a limit rather than a measurement.
    """
    if point.get("at_limit"):
        return False
    return str(point.get("detect", "")).upper() in ("Y", "YES", "TRUE", "1")


class TimeSeriesPlot(QWidget):
    """The plot area, redrawn as the user clicks different locations."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        if not HAVE_MATPLOTLIB:
            layout.addWidget(
                QLabel(
                    "matplotlib is not available in this QGIS install, so time "
                    "series cannot be plotted."
                )
            )
            self.canvas = None
            self.figure = None
            self.status = QLabel("")
            return

        self.figure = Figure(figsize=(4.4, 2.8), tight_layout=True)
        self.canvas = FigureCanvasQTAgg(self.figure)
        toolbar = NavigationToolbar2QT(self.canvas, self)
        toolbar.setIconSize(toolbar.iconSize() * 0.8)
        layout.addWidget(toolbar)
        layout.addWidget(self.canvas, 1)
        self.status = QLabel("Click a mapped location to plot its history.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def clear(self, message="Click a mapped location to plot its history."):
        """Blank the plot, e.g. when the report or facility changes."""
        if self.canvas is None:
            return
        self.figure.clear()
        self.canvas.draw_idle()
        self.status.setText(message)

    def plot(self, location, chemical, points, unit="", scope=None):
        """Draw one location's results for the selected analyte.

        `scope` names the filters the points were drawn under - matrix,
        fraction and method. It is not decoration. CLAUDE.md forbids
        aggregating across matrices or fractions because a soil maximum
        beside groundwater criteria is meaningless, and two methods on one
        sample are real data that disagree about detection. A chart that
        honours those rules but does not SAY which matrix or method it
        honoured invites exactly the reading the rules exist to prevent -
        and once a chart is exported or pasted into a report, nothing else
        carries that context.
        """
        if self.canvas is None:
            return

        self.figure.clear()
        axes = self.figure.add_subplot(111)

        if not points:
            axes.text(
                0.5, 0.5,
                "No results for %s\nat %s%s"
                % (chemical, location, ("\n(%s)" % scope) if scope else ""),
                ha="center", va="center", transform=axes.transAxes,
                color="#666666",
            )
            axes.set_xticks([])
            axes.set_yticks([])
            self.status.setText("")
            self.canvas.draw_idle()
            return

        dates = [p["date"] for p in points]
        values = [p["value"] for p in points]
        unit = unit or next((p["unit"] for p in points if p.get("unit")), "")

        axes.plot(dates, values, "-", color="#1b6ec2", linewidth=1.4, zorder=1)

        # Distinguish non-detects: they are reported at the detection limit, so
        # plotting them identically to detections would overstate the data.
        # Detected results are solid; non-detects are open circles plotted at
        # the detection limit, which is all the lab reports for them.
        detected = [(d, v) for d, v, p in zip(dates, values, points)
                    if _is_detected(p)]
        non_detect = [(d, v) for d, v, p in zip(dates, values, points)
                      if not _is_detected(p)]
        if detected:
            axes.scatter([d for d, _ in detected], [v for _, v in detected],
                         s=36, facecolors="#1b6ec2", edgecolors="#1b6ec2",
                         linewidths=1.2, zorder=3, label="Detected")
        if non_detect:
            axes.scatter([d for d, _ in non_detect], [v for _, v in non_detect],
                         s=40, facecolors="none", edgecolors="#1b6ec2",
                         linewidths=1.2, zorder=3,
                         label="Non-detect (at detection limit)")
        if detected and non_detect:
            axes.legend(loc="best", fontsize=8, frameon=False)

        # The scope goes UNDER the title rather than in it: the title is what
        # was plotted, the subtitle is what it was filtered to, and running
        # them together makes a long line matplotlib clips.
        #
        # The title is padded to reserve the line, and the scope drawn into
        # it smaller and quieter. Two things a rendered PNG caught that the
        # calls themselves could not: a text at y=1.005 lands exactly on
        # the title and prints THROUGH it, and putting both lines in one
        # set_title makes them share a font size, so the filter reads as a
        # second title rather than as the context it is.
        title = "%s at %s" % (chemical, location)
        if scope:
            axes.set_title(title, fontsize=11, pad=16)
            axes.text(0.5, 1.02, scope, transform=axes.transAxes,
                      ha="center", va="bottom", fontsize=8.5,
                      color="#5a6b7b")
        else:
            axes.set_title(title, fontsize=11)
        axes.set_ylabel(unit or "Result")
        axes.grid(True, alpha=0.25, linewidth=0.6)
        axes.spines["top"].set_visible(False)
        axes.spines["right"].set_visible(False)

        # A single point has no range for the locator to divide.
        if len(set(dates)) > 1:
            axes.xaxis.set_major_locator(mdates.AutoDateLocator())
            axes.xaxis.set_major_formatter(mdates.ConciseDateFormatter(
                mdates.AutoDateLocator()))
        else:
            axes.set_xticks(dates)
            axes.set_xticklabels([d.strftime("%Y-%m-%d") for d in dates])
        self.figure.autofmt_xdate()

        span = ""
        if len(dates) > 1:
            span = " from %s to %s" % (dates[0].strftime("%Y-%m-%d"),
                                       dates[-1].strftime("%Y-%m-%d"))
        note = ""
        if non_detect:
            note = (" %d non-detect(s), shown as open circles at their "
                    "detection limit." % len(non_detect))
        self.status.setText("%d result(s)%s.%s" % (len(points), span, note))
        self.canvas.draw_idle()


class TimeSeriesDock(QDockWidget):
    """A dock carrying the plot, so it can be floated and made large.

    Separate from the main panel rather than a section of it. A chart worth
    zooming into needs room the control panel cannot spare, and QGIS already
    knows how to float, resize and remember a dock - so this borrows all of
    that rather than inventing a window.

    Bottom by default: a time series is wide and short, so the full width of
    the map window suits it better than a column beside the panel. Every area
    is allowed, and floating is what the zoom is really for.
    """

    def __init__(self, parent=None):
        super().__init__("Chemistry Time Series", parent)
        # Stable, because QGIS stores dock geometry against it: without one
        # the plot would forget its size and position every session.
        self.setObjectName("EquisTimeSeriesPanel")
        self.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )
        self.plot = TimeSeriesPlot(self)
        # Tall enough that a chart is readable the moment it opens; the user
        # can shrink it, but a dock that opens too small to read is one more
        # thing to fix before the data can be looked at.
        self.plot.setMinimumHeight(220)
        self.setWidget(self.plot)

    # The dock forwards to the plot, so callers do not have to know which of
    # the two they are holding.

    def plot_series(self, location, chemical, points, unit="", scope=None):
        self.plot.plot(location, chemical, points, unit=unit, scope=scope)

    def clear(self, message="Click a mapped location to plot its history."):
        self.plot.clear(message)
