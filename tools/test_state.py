"""Tests for the panel's state machine.

No QGIS and no network: panel_state.py imports nothing, which is the point of
keeping it separate from dialog.py. Run with a plain interpreter:

    python tools\\test_state.py
"""

import importlib.util
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


_pkg = type(sys)("equis_loader")
_pkg.__path__ = [os.path.join(ROOT, "equis_loader")]
sys.modules.setdefault("equis_loader", _pkg)


def _load(name):
    full = "equis_loader.%s" % name
    spec = importlib.util.spec_from_file_location(
        full, os.path.join(ROOT, "equis_loader", "%s.py" % name)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


ps = _load("panel_state")


def test_pipeline():
    """The happy path, in the order the panel's numbered steps imply."""
    s = ps.PanelState()
    check("state: starts disconnected", s.state == ps.DISCONNECTED)
    check("state: and knows it has no source", not s.connected)

    for step in (ps.CONNECTED, ps.FACILITY, ps.PREPARED, ps.FETCHING,
                 ps.LOADED):
        check("state: reaches %s" % step, s.enter(step), s.state)
    check("state: no violations on the happy path", not s.violations,
          str(s.violations))
    check("state: rows are in hand at the end", s.has_rows)


def test_illegal_moves_are_refused():
    """A skipped step is refused, and leaves the state alone."""
    s = ps.PanelState()
    check("illegal: cannot fetch before connecting",
          not s.enter(ps.FETCHING), s.state)
    check("illegal: the state is unchanged after a refusal",
          s.state == ps.DISCONNECTED, s.state)
    check("illegal: the attempt is recorded", len(s.violations) == 1,
          str(s.violations))

    # The invariant that matters in the field: a refusal must not break the
    # panel, so the machine keeps working afterwards.
    check("illegal: the machine still works after a refusal",
          s.enter(ps.CONNECTED) and s.state == ps.CONNECTED)

    s2 = ps.PanelState()
    s2.enter(ps.CONNECTED)
    check("illegal: cannot jump connected -> loaded",
          not s2.enter(ps.LOADED), s2.state)
    check("illegal: an unknown state is refused too",
          not s2.enter("wishful"), s2.state)


def test_violations_are_reported_not_raised():
    """Violations log and carry on; they never raise in the field."""
    seen = []
    s = ps.PanelState(on_violation=seen.append)
    s.enter(ps.LOADED)          # illegal from DISCONNECTED
    check("violations: the callback is told", len(seen) == 1, str(seen))
    check("violations: the message names the attempt",
          "disconnected" in seen[0] and "loaded" in seen[0], seen[0])
    check("violations: nothing was raised", s.state == ps.DISCONNECTED)


def test_fetch_can_fail_back():
    """A fetch that does not land must not strand the panel.

    This is the shape of "the progress bar never stopped": the panel went
    into a fetching state and had no way back to one with controls.
    """
    s = ps.PanelState()
    for step in (ps.CONNECTED, ps.FACILITY, ps.PREPARED):
        s.enter(step)
    s.enter(ps.FETCHING)
    check("fetch: a failed fetch returns to prepared",
          s.enter(ps.PREPARED), s.state)
    check("fetch: and can be retried", s.enter(ps.FETCHING))
    check("fetch: a second fetch from loaded is allowed",
          s.enter(ps.LOADED) and s.enter(ps.FETCHING), s.state)
    check("fetch: fetching is not counted as progress past prepared",
          not s.at_least(ps.LOADED), s.state)


def test_modes_need_a_layer():
    """Picking and rolling act on a drawn layer, so they need one."""
    s = ps.PanelState()
    check("mode: cannot pick before anything is loaded",
          not s.enter_mode(ps.PICKING), s.mode)
    for step in (ps.CONNECTED, ps.FACILITY, ps.PREPARED, ps.FETCHING,
                 ps.LOADED):
        s.enter(step)
    check("mode: picking is allowed once loaded", s.enter_mode(ps.PICKING))
    # Both take over the canvas, so they are mutually exclusive rather than
    # both being half-active.
    check("mode: rolling replaces picking", s.enter_mode(ps.ROLLING)
          and s.mode == ps.ROLLING, s.mode)
    check("mode: and back to idle", s.enter_mode(ps.IDLE))


def test_leaving_loaded_ends_the_modes():
    """A mode cannot outlive the layer it acts on.

    Rolling kept repainting a layer that had been replaced, because stopping
    it was the caller's job and one path forgot. The machine enforces it now.
    """
    s = ps.PanelState()
    for step in (ps.CONNECTED, ps.FACILITY, ps.PREPARED, ps.FETCHING,
                 ps.LOADED):
        s.enter(step)
    s.enter_mode(ps.ROLLING)
    s.enter(ps.FETCHING)
    check("mode: a new fetch ends the roll", s.mode == ps.IDLE, s.mode)


def test_offline_is_orthogonal():
    """Offline is a flag, not a state: every stage is reachable from cache."""
    s = ps.PanelState()
    s.offline = True
    for step in (ps.CONNECTED, ps.FACILITY, ps.PREPARED, ps.FETCHING,
                 ps.LOADED):
        check("offline: %s is reachable offline" % step, s.enter(step))
    check("offline: the flag survives the pipeline", s.offline)
    check("offline: and is not a state", s.state == ps.LOADED, s.state)
    check("offline: describe names both", "offline" in s.describe(),
          s.describe())


def test_busy_is_not_fetching():
    """Loading facilities is a task too, and does not mean a fetch is running.

    Conflating them is why "busy" was read off a progress bar: there was no
    other place that knew the difference.
    """
    s = ps.PanelState()
    s.enter(ps.CONNECTED)
    s.busy = True
    check("busy: a task can run outside a fetch", s.busy and not s.fetching)
    check("busy: describe says so", "busy" in s.describe(), s.describe())


def test_stale_is_a_flag_on_loaded():
    """Stale rows are a property of LOADED, not a state of their own.

    A stale panel can still filter the old rows and act on a drawn layer; it
    only may not map. So it is a flag beside offline and busy, meaningful
    only where there are rows to be stale.
    """
    s = ps.PanelState()
    for step in (ps.CONNECTED, ps.FACILITY, ps.PREPARED):
        s.enter(step)
    check("stale: refused before rows exist", not s.set_stale(True), s.state)
    check("stale: the refusal leaves the flag clear", not s.stale)
    check("stale: and is recorded", len(s.violations) == 1,
          str(s.violations))
    check("stale: clearing outside LOADED is not a violation",
          s.set_stale(False) and len(s.violations) == 1)

    s.enter(ps.FETCHING)
    s.enter(ps.LOADED)
    check("stale: set once loaded", s.set_stale(True) and s.stale)
    check("stale: describe says so", "stale" in s.describe(), s.describe())
    check("stale: the state is still loaded", s.state == ps.LOADED)
    check("stale: a layer can still be picked while stale",
          s.enter_mode(ps.PICKING))

    # A new fetch is the only way to unstale rows, and leaving LOADED means
    # there are no rows to be stale about.
    s.enter(ps.FETCHING)
    check("stale: leaving LOADED clears it", not s.stale, s.describe())
    s.enter(ps.LOADED)
    check("stale: fresh rows arrive unstale", not s.stale)


def test_interactive_is_a_flag_with_consequences():
    """Interactive browsing: armed anywhere, draws only on answerable rows."""
    s = ps.PanelState()
    check("interactive: can be armed before any rows exist",
          s.set_interactive(True) and s.interactive)
    check("interactive: but cannot draw without rows", not s.may_preview)
    for step in (ps.CONNECTED, ps.FACILITY, ps.PREPARED, ps.FETCHING):
        s.enter(step)
        check("interactive: survives %s" % step, s.interactive)
        check("interactive: no preview in %s" % step, not s.may_preview)
    s.enter(ps.LOADED)
    check("interactive: previews once LOADED", s.may_preview, s.describe())
    check("interactive: describe says so", "interactive" in s.describe())

    s.busy = True
    check("interactive: no preview while busy", not s.may_preview)
    s.busy = False
    s.set_stale(True)
    check("interactive: no preview on stale rows", not s.may_preview)
    s.set_stale(False)

    check("interactive: picking is allowed", s.enter_mode(ps.PICKING))
    check("interactive: rolling is refused",
          not s.enter_mode(ps.ROLLING) and s.mode == ps.PICKING, s.mode)
    check("interactive: and the refusal is recorded",
          any("rolling while interactive" in v for v in s.violations),
          str(s.violations))

    # Arming ends a roll already running, rather than letting both own the
    # renderer.
    s = ps.PanelState()
    for step in (ps.CONNECTED, ps.FACILITY, ps.LOADED):
        s.enter(step)
    s.enter_mode(ps.ROLLING)
    s.set_interactive(True)
    check("interactive: arming ends a roll", s.mode == ps.IDLE, s.mode)
    s.set_interactive(False)
    check("interactive: disarmed, rolling is allowed again",
          s.enter_mode(ps.ROLLING))
    check("interactive: disarmed means no preview", not s.may_preview)

    # A new fetch keeps the choice; the preview waits for the rows.
    s = ps.PanelState()
    for step in (ps.CONNECTED, ps.FACILITY, ps.LOADED):
        s.enter(step)
    s.set_interactive(True)
    s.enter(ps.FETCHING)
    check("interactive: armed through a fetch", s.interactive)
    check("interactive: nothing to preview mid-fetch", not s.may_preview)
    s.enter(ps.LOADED)
    check("interactive: previews again after it lands", s.may_preview)


def main():
    test_pipeline()
    test_interactive_is_a_flag_with_consequences()
    test_stale_is_a_flag_on_loaded()
    test_illegal_moves_are_refused()
    test_violations_are_reported_not_raised()
    test_fetch_can_fail_back()
    test_modes_need_a_layer()
    test_leaving_loaded_ends_the_modes()
    test_offline_is_orthogonal()
    test_busy_is_not_fetching()
    print("\n%s" % ("ALL STATE TESTS PASSED" if not FAILURES
                    else "FAILURES: %s" % ", ".join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
