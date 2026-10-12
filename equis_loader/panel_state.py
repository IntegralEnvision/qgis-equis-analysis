"""What the panel is doing, as an explicit machine rather than eight flags.

The panel used to answer "can I do this yet?" by re-deriving it from
`_client`, `_offline`, `_task`, `_report_table`, `_location_pages` and
friends, in whichever function happened to be asking. Three bugs came out of
that in one week: a fetch that overwrote the task in flight and orphaned it,
a chemical list that stayed live during a fetch so a second one could be
launched on top, and a map button that silently started a server round-trip
whenever the selection had moved on. None of them was wrong in any single
place - the panel simply had several opinions and they drifted.

So the sequence is written down once, here, with no QGIS imports so it can be
exercised with a plain interpreter.

Two deliberate limits on how much this models:

  Offline is a FLAG, not a state. It is orthogonal to the sequence - a
  facility can be picked, a report fetched and a layer mapped entirely from
  cache - and it can flip underneath any of them, since a failed live read
  falls back to the cache rather than failing. Duplicating every state into
  online and offline variants would double the machine to say the same thing
  twice.

  The pipeline is one axis; what you do with a DRAWN layer is another. Pick
  and roll are sub-states of LOADED rather than peers of it, because they
  cannot exist without a layer and do not interrupt the pipeline.

A PARQUET SOURCE SHORT-CIRCUITS THE MIDDLE. The sequence above describes
fetching rows out of EQuIS: connect, pick a facility, prepare a report, run
it, get rows. A published parquet set has the rows already, so it enters at
FACILITY - which it can name from the data itself - and goes straight to
LOADED. PREPARED and FETCHING are not skipped as a shortcut; they describe
work that does not exist on that path. There is no report to prepare and
nothing to run.

That is also why the source is NOT a state. Like offline, it is orthogonal:
it changes which arrows get used, not what the states mean. LOADED means
rows are in hand and can be mapped, whichever way they arrived.

INTERACTIVE IS A FLAG, AND IT HAS CONSEQUENCES. The toggle turns the
chemical list into a single-select browser: each pick replaces ONE scratch
memory layer - the preview - and re-bins the chart, and so does every
dropdown change after that. Like offline it is orthogonal to the pipeline
(it can be armed before any rows exist, and survives a new fetch), so it is
not a state. It is also not a mode beside PICKING and ROLLING, because it
coexists with one of them and excludes the other:

  - PICKING is compatible. Clicking a point on the preview plots its series,
    which is half of what browsing is for.
  - ROLLING is not. A roll cycles the renderer through a MULTI-selection,
    which interactive mode forbids, and it owns the renderer of a layer
    that interactive mode replaces on every click - the exact "a roll kept
    repainting a layer that had just been replaced" failure. Arming
    interactive ends a roll; a roll is refused while it is armed.

What it may DRAW is narrower than what it may be: a preview needs rows that
answer the selection, so `may_preview` is LOADED, not busy and not stale.
Anywhere else the dialog removes the preview rather than leaving it up - a
layer describing a pick the rows can no longer answer is a stale map that
looks live, which is the one outcome this plugin engineers against.

    interactive (armed in any state)
        |-- ROLLING: ended on arming, refused while armed
        |-- PICKING: allowed
        `-- may_preview = LOADED and not busy and not stale
              |-- chemical picked  -> preview removed, new one added
              |-- dropdown changed -> preview removed, new one added
              |-- Keep layer       -> preview becomes an ordinary layer
              `-- may_preview lost -> preview removed
"""

# --- the pipeline ---------------------------------------------------------
# One axis, in the order the panel's numbered steps already imply.

DISCONNECTED = "disconnected"   # no client and not working offline
CONNECTED = "connected"         # a site answered; no facility chosen yet
FACILITY = "facility"           # a facility is chosen, locations loading/loaded
PREPARED = "prepared"           # a report exists and the pickers are filled
FETCHING = "fetching"           # a report run is in flight
LOADED = "loaded"               # rows are in hand and can be mapped

PIPELINE = (DISCONNECTED, CONNECTED, FACILITY, PREPARED, FETCHING, LOADED)

# Legal moves. A transition absent from here is a bug in the caller, not a
# state to invent: every arrow was written by asking "what does the user do
# next", so an unlisted one means the panel got somewhere it has no path to.
#
# Notice FETCHING returns to whatever it came from as well as advancing:
# a cancelled or failed fetch must not strand the panel in a state that has
# no controls, which is what "the progress bar never stopped" looked like.
TRANSITIONS = {
    # FACILITY direct from DISCONNECTED is the parquet source: a published
    # parquet set names its own facility (FACILITY_CODE is on every row) and
    # arrives with the rows already extracted, so there is no site to connect
    # to and nothing to authenticate against. CONNECTED would be a lie - no
    # EQuIS site answered, and none will.
    DISCONNECTED: {CONNECTED, FACILITY},
    CONNECTED: {DISCONNECTED, FACILITY},
    # Back to CONNECTED when the facility is changed or cleared. LOADED is
    # reachable directly: a saved report or a cached copy is run as it is,
    # with no per-user report to prepare, so PREPARED is skipped entirely on
    # those paths. Found by driving the real offline workflow rather than by
    # imagining the arrows in advance.
    FACILITY: {DISCONNECTED, CONNECTED, FACILITY, PREPARED, FETCHING, LOADED},
    # PREPARED -> PREPARED is a re-prepare: a different facility's report, or
    # the pickers being read again.
    PREPARED: {DISCONNECTED, CONNECTED, FACILITY, PREPARED, FETCHING},
    # A fetch either lands (LOADED) or does not (back where it started).
    FETCHING: {DISCONNECTED, CONNECTED, FACILITY, PREPARED, FETCHING, LOADED},
    # From LOADED a new fetch is the only way to different rows; the rest is
    # tearing down or moving on.
    LOADED: {DISCONNECTED, CONNECTED, FACILITY, PREPARED, FETCHING, LOADED},
}

# --- what you can do with a drawn layer -----------------------------------
# Sub-states of LOADED. IDLE means "rows are loaded", with or without a layer
# on the map; the other two are mutually exclusive because both take over the
# map canvas and the layer's renderer.

IDLE = "idle"
PICKING = "picking"   # the time-series tool is armed on the canvas
ROLLING = "rolling"   # markers are cycling through the selected chemicals

MODES = (IDLE, PICKING, ROLLING)

# Rolling owns the renderer and picking owns the map tool, so one must end
# before the other begins rather than both being half-active - which is what
# let a roll keep repainting a layer that had just been replaced.
MODE_TRANSITIONS = {
    IDLE: {IDLE, PICKING, ROLLING},
    PICKING: {IDLE, PICKING, ROLLING},
    ROLLING: {IDLE, PICKING, ROLLING},
}


class PanelState(object):
    """The panel's position in the pipeline, and what it may do from there.

    Deliberately dumb: it holds a state, refuses illegal moves, and answers
    questions about what is allowed. It never touches a widget and never
    starts work - the dialog does that, and asks here whether it may.

    Violations are LOGGED AND IGNORED rather than raised. In the field a
    missed edge case should degrade to the old behaviour, not break the panel
    mid-session; `violations` records them so a test can be strict where a
    user's afternoon should not be.
    """

    def __init__(self, on_violation=None):
        self.state = DISCONNECTED
        self.mode = IDLE
        # True when working from cache with no live client. Orthogonal to the
        # pipeline: every state above can be reached offline.
        self.offline = False
        # Set while a background task is in flight. Not the same as the
        # FETCHING state: loading facilities and preparing a report are also
        # tasks, and they happen in CONNECTED and FACILITY.
        self.busy = False
        # True when the rows in hand were fetched for a different request
        # than the one the controls now describe. A flag rather than a
        # seventh state: a stale panel can still filter the old rows, still
        # pick and roll on an existing layer - it differs from LOADED only in
        # whether it may map, and making that a state would double the LOADED
        # transitions and sub-modes to say one boolean. Meaningful only in
        # LOADED; the dialog sets it, since only the dialog knows what a
        # selection key is.
        self.stale = False
        # The single-select browsing mode (see the module docstring). A
        # flag, not a state or a mode: it is armed across the pipeline and
        # coexists with PICKING.
        self.interactive = False
        # Every refused transition, for tests and for the log.
        self.violations = []
        self._on_violation = on_violation

    # ------------------------------------------------------------ moving

    def can_enter(self, state):
        """Whether `state` is reachable from where the panel is now."""
        if state not in PIPELINE:
            return False
        return state in TRANSITIONS.get(self.state, set())

    def enter(self, state, note=""):
        """Move to `state`. Returns True when the move was legal.

        An illegal move leaves the state ALONE and records the attempt. The
        caller is expected to check the return value when it matters; the
        point is that a wrong move cannot silently corrupt the state, not
        that every caller must handle failure.
        """
        if state == self.state:
            return True
        if not self.can_enter(state):
            self._refuse("%s -> %s" % (self.state, state), note)
            return False
        self.state = state
        # Leaving LOADED means there is no layer to act on, so the modes that
        # need one end with it. Kept here rather than in the dialog so the
        # invariant holds even if a caller forgets. Staleness goes with it:
        # without rows there is nothing to be stale.
        if state != LOADED:
            self.mode = IDLE
            self.stale = False
        return True

    def set_stale(self, flag, note=""):
        """Say whether the rows in hand still answer the current selection.

        Only LOADED has rows, so marking any other state stale is refused and
        recorded, the way an illegal transition is. Clearing is always
        allowed: it can never claim more than the state supports.
        """
        flag = bool(flag)
        if flag and self.state != LOADED:
            self._refuse("stale in state %s" % self.state, note)
            return False
        self.stale = flag
        return True

    def set_interactive(self, flag, note=""):
        """Arm or disarm interactive browsing. Always allowed.

        Arming ends a roll here rather than trusting the dialog to have
        stopped it first: the invariant "never rolling while interactive"
        holds even for a caller that forgets, the way leaving LOADED ends
        the modes.
        """
        flag = bool(flag)
        if flag and self.mode == ROLLING:
            self.mode = IDLE
        self.interactive = flag
        return True

    @property
    def may_preview(self):
        """Whether interactive mode may draw a preview right now."""
        return (self.interactive and self.state == LOADED
                and not self.busy and not self.stale)

    def enter_mode(self, mode, note=""):
        """Switch what is being done with the drawn layer."""
        if mode not in MODES:
            self._refuse("mode -> %s" % mode, note)
            return False
        if self.state != LOADED and mode != IDLE:
            # Picking and rolling both need a layer to act on.
            self._refuse("%s in state %s" % (mode, self.state), note)
            return False
        if mode == ROLLING and self.interactive:
            # A roll needs a multi-selection and owns a renderer that
            # interactive mode replaces on every pick.
            self._refuse("rolling while interactive", note)
            return False
        if mode == self.mode:
            return True
        if mode not in MODE_TRANSITIONS.get(self.mode, set()):
            self._refuse("mode %s -> %s" % (self.mode, mode), note)
            return False
        self.mode = mode
        return True

    def _refuse(self, what, note):
        message = "illegal transition: %s%s" % (what,
                                                (" (%s)" % note) if note else "")
        self.violations.append(message)
        if self._on_violation is not None:
            self._on_violation(message)

    # --------------------------------------------------------- questions

    @property
    def connected(self):
        """Whether the panel has a data source at all, live or cached."""
        return self.state != DISCONNECTED

    @property
    def has_rows(self):
        return self.state == LOADED

    @property
    def fetching(self):
        return self.state == FETCHING

    def at_least(self, state):
        """Whether the pipeline has reached `state` or gone past it.

        FETCHING is not counted as past PREPARED: a fetch in flight has not
        produced anything yet, and treating it as progress is how a panel
        ends up offering to map rows that do not exist.
        """
        if state not in PIPELINE:
            return False
        order = {name: index for index, name in enumerate(PIPELINE)}
        return order[self.state] >= order[state]

    def describe(self):
        """One line for a log or a status bar."""
        bits = [self.state]
        if self.state == LOADED and self.mode != IDLE:
            bits.append(self.mode)
        if self.stale:
            bits.append("stale")
        if self.interactive:
            bits.append("interactive")
        if self.offline:
            bits.append("offline")
        if self.busy:
            bits.append("busy")
        return ", ".join(bits)
