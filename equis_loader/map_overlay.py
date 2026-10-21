"""The Analysis view laid over the map canvas, and who gets the mouse.

Asked for 2026-10-20: "we break the rules and overlay things on the map
with transparency... we would need a new gesture like ctrl-mouse to pan
the map instead of interact with the overlay. It may be wrong but I would
like to see it." This is that, kept small enough to throw away.

THE VIEW IS MOVED, NOT COPIED. The tab's one long-lived web view is
reparented onto the canvas viewport and back, so the overlay costs no
second Chromium (~1.4 s to start) and the chart keeps its state. The
compositing - map visible through a transparent page - was proven by the
overlay spike on QGIS 4.2.1 (tools/mosaic_poc/README.md, question 1).

ONE AUTHORITY FOR THE MOUSE. The spike's lesson was two gates on one
mouse - Qt's WA_TransparentForMouseEvents and CSS pointer-events - that
desynced, so the overlay swallowed clicks and did nothing with them. Here
Qt alone decides, per mouse move, from three facts:

  * is the cursor over a CARD - a rectangle the page reports (`cards`);
    everywhere else the page is transparent, so the map gets the mouse;
  * is Ctrl held - then the map gets it even over a card ("hold Ctrl to
    reach the map");
  * is a button down - then nothing changes, so a brush dragged off its
    card, or a pan dragged across one, is not cut in half.

The page never sees an event it was not meant to, so it needs no gate of
its own. The attribute goes on the view AND its focusProxy(), Chromium's
child that really receives input (the spike's question 3).
"""

try:
    from qgis.PyQt.QtCore import QEvent, QObject, QRect, Qt
    from qgis.PyQt.QtGui import QCursor
except ImportError:  # pragma: no cover - imported only inside QGIS
    QObject = object

# Events whose position or modifiers can change the gate's answer.
_MOVES = None


def _moves():
    global _MOVES
    if _MOVES is None:
        _MOVES = {QEvent.Type.MouseMove, QEvent.Type.HoverMove,
                  QEvent.Type.Enter, QEvent.Type.Leave,
                  QEvent.Type.KeyPress, QEvent.Type.KeyRelease,
                  QEvent.Type.MouseButtonRelease}
    return _MOVES


class MapOverlay(QObject):
    """Hold `view` over `canvas`'s viewport until detach()."""

    def __init__(self, view, canvas, on_change=None):
        super().__init__()
        self.view = view
        self.canvas = canvas
        self.viewport = canvas.viewport()
        self.cards = []            # QRects, in viewport pixels
        self.open = None           # True: the page has the mouse
        self.on_change = on_change
        self._watched = []

    # ------------------------------------------------------------ lifecycle

    def attach(self):
        view = self.view
        view.setParent(self.viewport)
        view.setGeometry(self.viewport.rect())
        view.show()
        view.raise_()
        self._watch(self.viewport)
        self._watch(view)
        self._watch_proxy()
        self._gate(False)

    def detach(self):
        for obj in self._watched:
            try:
                obj.removeEventFilter(self)
            except RuntimeError:   # already deleted with the view
                pass
        self._watched = []
        self._set_transparent(False)
        self.open = None

    def _watch(self, obj):
        if obj is not None and obj not in self._watched:
            obj.installEventFilter(self)
            self._watched.append(obj)

    def _watch_proxy(self):
        # Chromium's child exists only once a page is loaded, and has been
        # seen replaced on reparenting; looked up again whenever it matters.
        self._watch(self.view.focusProxy())

    # ---------------------------------------------------------------- cards

    def set_cards(self, rects):
        """The page's cards: [{x, y, w, h}] in CSS pixels, which at zoom 1
        are the view's own pixels - and the view fills the viewport."""
        cards = []
        for r in rects or []:
            try:
                cards.append(QRect(int(r["x"]), int(r["y"]),
                                   int(r["w"]) + 1, int(r["h"]) + 1))
            except (KeyError, TypeError, ValueError):
                continue
        self.cards = cards
        self._decide(None)

    def over_card(self, point):
        return any(card.contains(point) for card in self.cards)

    # ---------------------------------------------------------------- mouse

    def eventFilter(self, obj, event):
        kind = event.type()
        if obj is self.viewport and kind == QEvent.Type.Resize:
            self.view.setGeometry(self.viewport.rect())
        elif kind in _moves():
            self._decide(event)
        return False

    def _decide(self, event):
        """From the EVENT where it has the facts - its position, buttons and
        modifiers are what Qt is about to route - and from the application
        state only where it does not (a key press, a new set of cards)."""
        from qgis.PyQt.QtWidgets import QApplication
        buttons = (event.buttons() if hasattr(event, "buttons")
                   else QApplication.mouseButtons())
        if buttons != Qt.MouseButton.NoButton:
            return          # mid-drag: whoever started it finishes it
        if hasattr(event, "globalPosition"):
            point = self.viewport.mapFromGlobal(event.globalPosition().toPoint())
        else:
            point = self.viewport.mapFromGlobal(QCursor.pos())
        mods = (event.modifiers() if hasattr(event, "modifiers")
                else QApplication.queryKeyboardModifiers())
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        self._gate(self.over_card(point) and not ctrl)

    def _gate(self, open_):
        if open_ == self.open:
            return
        self.open = open_
        self._set_transparent(not open_)
        if self.on_change is not None:
            self.on_change(open_)

    def _set_transparent(self, transparent):
        attr = Qt.WidgetAttribute.WA_TransparentForMouseEvents
        self.view.setAttribute(attr, transparent)
        proxy = self.view.focusProxy()
        if proxy is not None:
            proxy.setAttribute(attr, transparent)
            self._watch(proxy)
