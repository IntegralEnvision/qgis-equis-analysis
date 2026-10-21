r"""What is that thing in the corner? A console probe for stray widgets.

    exec(open(r"<path-to>\equis_qgis_public\tools\diagnose_windows.py").read())

RUN IT IN THE QGIS PYTHON CONSOLE while the problem is on screen, then
move the mouse onto the misbehaving widget within five seconds. It prints,
and copies to the clipboard:

  * the widget under the cursor and every parent up to its window, with
    class, object name, geometry and the attributes that decide whether a
    widget paints a background or sits in a layout;
  * every visible top-level window - a "panel with no background and no
    way to close it" is very often a widget that became a window, or a
    dock the main window no longer lays out;
  * every dock widget: floating, area, visible, geometry;
  * the overlay's state, if the plugin is loaded.

Written 2026-10-21: a dock was reported drawn at the main window's
top-left, without background or title bar, after re-selecting data with
the overlay on, and could not be reproduced in a scratch profile.

To get rid of a stray widget once it is identified, the last line printed
names it; `_STRAY.hide()` hides it.
"""


def _diagnose_windows(delay_ms=5000):
    from qgis.PyQt.QtCore import QTimer, Qt
    from qgis.PyQt.QtGui import QCursor, QGuiApplication
    from qgis.PyQt.QtWidgets import QApplication, QDockWidget
    from qgis.utils import iface, plugins

    attrs = ("WA_TranslucentBackground", "WA_NoSystemBackground",
             "WA_OpaquePaintEvent", "WA_NativeWindow", "WA_AlwaysStackOnTop",
             "WA_TransparentForMouseEvents", "WA_WState_Hidden")

    def describe(w):
        flags = []
        for name in attrs:
            try:
                if w.testAttribute(getattr(Qt.WidgetAttribute, name)):
                    flags.append(name[3:])
            except (AttributeError, RuntimeError):
                pass
        g = w.geometry()
        return "%s '%s' at (%d,%d %dx%d)%s%s autoFill=%s %s" % (
            w.metaObject().className(), w.objectName(), g.x(), g.y(),
            g.width(), g.height(), " window" if w.isWindow() else "",
            " visible" if w.isVisible() else " hidden",
            w.autoFillBackground(), " ".join(flags))

    def run():
        lines = []
        pos = QCursor.pos()
        under = QApplication.widgetAt(pos)
        lines.append("== under the cursor at %d,%d" % (pos.x(), pos.y()))
        w = under
        while w is not None:
            lines.append("  " + describe(w))
            w = w.parentWidget()
        lines.append("== visible top-level windows")
        for w in QApplication.topLevelWidgets():
            if w.isVisible():
                lines.append("  " + describe(w) + "  flags=%s" % int(w.windowFlags().value))
        lines.append("== dock widgets")
        win = iface.mainWindow()
        for d in win.findChildren(QDockWidget):
            lines.append("  '%s' floating=%s area=%s parent=%s %s" % (
                d.windowTitle(), d.isFloating(), win.dockWidgetArea(d),
                type(d.parentWidget()).__name__ if d.parentWidget() else None,
                describe(d)))
        plugin = plugins.get("equis_loader")
        panel = getattr(plugin, "dialog", None) if plugin else None
        if panel is not None:
            linked = getattr(panel, "_linked", None)
            lines.append("== the plugin")
            lines.append("  dock: " + describe(panel))
            if linked is not None and linked.views is not None:
                view = linked.views.view
                lines.append("  web view: " + describe(view))
                lines.append("  web view parent chain: " + " < ".join(
                    p.metaObject().className() for p in _parents(view)))
                lines.append("  overlay: %s" % ("on, visible=%s" % linked.overlay_visible
                                                if linked.overlay is not None else "off"))
        lines.append("== screens")
        for s in QGuiApplication.screens():
            g = s.geometry()
            lines.append("  %s %dx%d at %d,%d scale %.2f" % (
                s.name(), g.width(), g.height(), g.x(), g.y(), s.devicePixelRatio()))
        text = "\n".join(lines)
        print(text)
        QApplication.clipboard().setText(text)
        globals()["_STRAY"] = under.window() if under is not None else None
        print("\n(copied to the clipboard; _STRAY is %s - _STRAY.hide() hides it)"
              % (describe(_STRAY) if _STRAY is not None else None))

    def _parents(w):
        out = []
        while w is not None:
            out.append(w)
            w = w.parentWidget()
        return out

    print("Move the mouse onto the misbehaving widget; reading in %d s..." % (delay_ms // 1000))
    QTimer.singleShot(delay_ms, run)


_diagnose_windows()
