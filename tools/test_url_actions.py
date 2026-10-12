r"""`equis://` links, dispatched to a stand-in panel.

A link can be typed, pasted, or come from a project file someone else
wrote, so the parsing is exercised with the malformed cases as well as the
good ones. The panel is a recorder rather than the real dialog: what
matters here is which call a URL turns into, and with what.

    "C:\Program Files\QGIS 4.2.1\bin\python-qgis.bat" tools\test_url_actions.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


try:
    from qgis.core import QgsApplication
except ImportError as exc:
    print("needs QGIS: %s" % exc)
    print("SKIPPED")
    raise SystemExit(0)

QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", ""), True)
app = QgsApplication([], False)
app.initQgis()

from qgis.PyQt.QtCore import QUrl                      # noqa: E402
from qgis.PyQt.QtGui import QDesktopServices           # noqa: E402

from equis_loader import url_actions                   # noqa: E402


class FakePanel(object):
    """Records what a link asked for, and nothing else."""

    def __init__(self):
        self.calls = []
        self.problems = []

    def select_analyte(self, analyte, cas_rn=None):
        self.calls.append(("select_analyte", analyte, cas_rn))
        return True

    def apply_scope(self, **scope):
        self.calls.append(("apply_scope", scope))

    def plot_location(self, code):
        self.calls.append(("plot_location", code))

    def map_selected(self, how=None):
        self.calls.append(("map_selected", how))

    def note_link_problem(self, message):
        self.problems.append(message)


panel = FakePanel()
actions = url_actions.UrlActions(panel)

# --- the two actions a map tip offers ----------------------------------------
actions.dispatch("equis://series?location=MW-1&analyte=Arsenic")
check("a series link plots the location it names",
      ("plot_location", "MW-1") in panel.calls, str(panel.calls))
check("and selects the chemical it names",
      ("select_analyte", "Arsenic", None) in panel.calls, str(panel.calls))

panel.calls[:] = []
actions.dispatch("equis://map?analyte=Lead&matrix=WS&fraction=D")
check("a map link builds the layer",
      any(c[0] == "map_selected" for c in panel.calls), str(panel.calls))
check("and applies the scope the link named",
      ("apply_scope", {"matrix": "WS", "fraction": "D"}) in panel.calls,
      str(panel.calls))

# --- a link that names no scope must not widen one ---------------------------
# CLAUDE.md forbids crossing matrices and fractions. A link mentioning only
# a location must leave the user's own choice alone rather than clearing it.
panel.calls[:] = []
actions.dispatch("equis://series?location=MW-2")
check("a link with no scope does not touch the filters",
      not any(c[0] == "apply_scope" for c in panel.calls), str(panel.calls))
check("and plots without requiring a chemical in the link",
      ("plot_location", "MW-2") in panel.calls, str(panel.calls))

# --- CAS wins over the name, as report_data's rule says ----------------------
panel.calls[:] = []
actions.dispatch("equis://select?analyte=Arsenic&cas_rn=7440-38-2")
check("cas_rn is passed alongside the name",
      ("select_analyte", "Arsenic", "7440-38-2") in panel.calls,
      str(panel.calls))

# --- malformed and hostile input ---------------------------------------------
panel.calls[:] = []
panel.problems[:] = []
check("an unknown action is refused, not guessed at",
      actions.dispatch("equis://destroy?location=MW-1") is None
      and not panel.calls, str(panel.calls))
check("and says so", bool(panel.problems), str(panel.problems))

panel.problems[:] = []
actions.dispatch("equis://series")
check("a series link with no location is refused",
      bool(panel.problems), str(panel.problems))

panel.problems[:] = []
actions.dispatch("equis://map")
check("a map link with no chemical is refused",
      bool(panel.problems), str(panel.problems))

panel.calls[:] = []
check("another scheme is not ours to handle",
      actions.dispatch("https://example.com/series?location=MW-1") is None
      and not panel.calls, str(panel.calls))

# A value long enough to fill a status line is capped rather than passed on.
panel.calls[:] = []
actions.dispatch("equis://series?location=" + "A" * 5000)
plotted = [c for c in panel.calls if c[0] == "plot_location"]
check("an absurdly long value is capped",
      plotted and len(plotted[0][1]) <= 200,
      str(len(plotted[0][1]) if plotted else None))

# handle() is called from Qt's event loop, where an exception is a crash.
class Exploding(FakePanel):
    def plot_location(self, code):
        raise RuntimeError("boom")


exploding = Exploding()
safe = url_actions.UrlActions(exploding)
try:
    safe.handle(QUrl("equis://series?location=MW-1"))
    survived = True
except Exception:                               # noqa: BLE001
    survived = False
check("a failing action is reported, never raised into Qt", survived)
check("and the failure is explained", bool(exploding.problems),
      str(exploding.problems))

# --- with no panel at all ----------------------------------------------------
orphan = url_actions.UrlActions(None)
check("a link with no panel does nothing rather than raising",
      orphan.dispatch("equis://series?location=MW-1") is None)

# --- registration survives a reload ------------------------------------------
first = url_actions.register(panel)
second = url_actions.register(panel)
check("registering twice keeps ONE handler", first is second)
check("and repoints it at the current panel", second.panel is panel)

other = FakePanel()
url_actions.register(other)
check("a reload repoints rather than orphaning",
      url_actions._REGISTRY["handler"].panel is other)

# The real proof: Qt routes the scheme to us.
other.calls[:] = []
QDesktopServices.openUrl(QUrl("equis://series?location=MW-7"))
app.processEvents()
check("Qt delivers the scheme to the handler",
      ("plot_location", "MW-7") in other.calls, str(other.calls))

check("unregistering reports that it did something",
      url_actions.unregister() is True)
check("and a second unregister is harmless",
      url_actions.unregister() is False)

# --- QgsAction: the identify panel, with no mode to enter --------------------
# A QgsMapToolIdentify subclass would need the user IN that tool - a mode,
# and a second opinion about what a map click means beside the panel's
# existing pick tool. An action appears in the identify results the user is
# already looking at.
from qgis.core import (QgsAction, QgsExpressionContext,              # noqa: E402
                       QgsExpressionContextUtils, QgsFeature,
                       QgsGeometry, QgsPointXY, QgsVectorLayer)

layer = QgsVectorLayer("Point?crs=EPSG:4326&field=code:string"
                       "&field=results:integer", "Locations", "memory")
feature = QgsFeature(layer.fields())
feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(1.0, 2.0)))
feature.setAttributes(["MW-1", 42])
layer.dataProvider().addFeatures([feature])

added = url_actions.install_actions(layer)
check("actions are installed on the layer", added > 0, str(added))
names = [a.name() for a in layer.actions().actions()]
check("including one that plots the location",
      "Plot this location" in names, str(names))
check("every action is an OpenUrl, never stored python source",
      all(a.type() == QgsAction.ActionType.OpenUrl
          for a in layer.actions().actions()), str(names))
check("they are scoped to a feature, not the whole layer",
      all("Feature" in a.actionScopes()
          for a in layer.actions().actions()))

# A layer is rebuilt on every redraw; actions must not stack up.
again = url_actions.install_actions(layer)
check("installing twice adds nothing", again == 0 and
      len(layer.actions().actions()) == len(names), str(again))

# THE POINT: firing one reaches the handler with this feature's code.
url_actions.register(panel)
panel.calls[:] = []
context = QgsExpressionContext()
context.appendScopes(
    QgsExpressionContextUtils.globalProjectLayerScopes(layer))
context.setFeature(feature)
layer.actions().actions()[0].run(context)
app.processEvents()
check("firing an action reaches the panel with the feature's code",
      ("plot_location", "MW-1") in panel.calls, str(panel.calls))
url_actions.unregister()

# --- the map tip names the parameters this file parses -----------------------
# A tip written by hand with `loc=` instead of `location=` fails silently at
# click time, which is a poor way to find a typo.
tip = url_actions.map_tip(analyte_field='"chemical"')
check("the generated map tip uses the parsed parameter names",
      "location=" in tip and "analyte=" in tip, tip[:90])
check("and carries the feature's own code",
      '[% "code" %]' in tip, tip[:90])

print()
if FAILURES:
    print("FAILED: %s" % ", ".join(FAILURES))
    raise SystemExit(1)
print("equis:// links reach the panel, and bad ones are refused.")
