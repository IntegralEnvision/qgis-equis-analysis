"""The Analysis tab's scatter chooser: what the page is offered, what a choice draws.

NO QGIS, like store.py and pivot.py, for the same reason: the decisions are
tested under a plain interpreter, and tools/analysis_preview.py serves the
real page from this module in an ordinary browser, so the surface can be
seen and screenshotted without QGIS at all. linked_views.py is the Qt shell
around it - busy messages, the map layer, the draw - and nothing more.

THE SHAPE (asked 2026-10-10: "assume the pivot for all the analytes exists;
an analyte chooser for both X and Y"). A matrix is pivoted ONCE for every
analyte it holds, and any two of its columns are a scatter. That is only
reasonable because the pivot got fast: every analyte in tailings (628
columns) builds in 0.4 s, water (215 columns, 76,972 samples) in 3.3 s,
and a pair's rows, breaks and stations then cost ~0.2 s each against the
same pivot (2026-10-10, this laptop). One pivot per (matrix, row scope),
kept for the store's lifetime or until pivot.KEEP_TABLES retires it.

ONE MATRIX AT A TIME. The units guard is per column, so a multi-matrix
pivot is legitimate when units agree (SE+TAILS); the chooser does not offer
it yet. open_scatter on the console still takes a list.

THE PROTOCOL, both directions JSON:

  page -> host   chosen {matrix, x, y}      x and y are column prefixes
  host -> page   offer  {matrices, matrix, variables, skipped, ...}
                 paired {x, counts}         per column, pairs with x
                 scatter {...}              scatter_options(), as before

The page holds the selection; the host decides everything that has a rule
in it (which samples, which cells, which breaks), so neither the pivot's
rules nor bivariate.py's ever get a second copy in JavaScript.
"""

from . import bivariate
from . import pivot as pv
from . import ternary
from .store import DETECT_TRUE

FRACTION_NAMES = {"T": "total", "D": "dissolved"}


class AnalysisError(ValueError):
    """A choice that cannot be drawn. The message says why, for a person."""


def column_meta(column):
    """What the page needs to label one pivot column group."""
    return {"prefix": column["prefix"], "chemical": column["chemical"],
            "fraction": column["fraction"] or "", "unit": column["unit"] or ""}


def window_from_range(begin, end, include_end=False):
    """A map time range as the whole-day (start, end) window the scatter uses.

    `begin` and `end` are datetimes, either None for an open end. QGIS's
    Temporal Controller frames run [begin, end) by default, so a one-day
    frame is 2020-01-01T00:00 to 2020-01-02T00:00 and must mean 2020-01-01
    only: an exclusive end at midnight ends the day before. An end later in
    a day keeps that day, as the panel's date filter would. Both None is
    "no window".
    """
    import datetime
    if begin is None and end is None:
        return None
    start = begin.date() if begin is not None else None
    last = None
    if end is not None:
        last = end.date()
        midnight = (end.hour, end.minute, end.second, end.microsecond) == (0, 0, 0, 0)
        if midnight and not include_end:
            last = last - datetime.timedelta(days=1)
    return (start, last)


def solid_table(store, table, columns, mode="all", window=None):
    """The samples a points mode draws solid, as a table. None for "every
    sample, all time" - the page then draws them all solid, as before.

    Picked by bivariate.picks_sql: one real sample per location by a
    stated rank, or every sample, within `window`. A table, so the page's
    views filter by it rather than re-deriving the rule in JavaScript.
    """
    if mode == "all" and not window:
        return None
    name = "ps_%d" % next(pv._counter)
    store.con.execute("CREATE TABLE %s AS SELECT sample FROM (%s)" % (
        name, bivariate.picks_sql(table, [c["prefix"] for c in columns],
                                  list(DETECT_TRUE), mode, window)))
    pv.keep_last(store, "_solid_tables", name)
    return name


def map_phrase(mode, columns, window=None):
    """What one station on the scatter map is: for its layer name and key.

    "all" cannot put every sample on one station, so its map is the most
    recent, as stations() says. One phrase, used by both the page and the
    layer, so the two never describe the map differently.
    """
    if mode in bivariate._AXIS:
        axis = columns[bivariate._AXIS[mode]]
        text = "sample with the highest %s" % axis["chemical"]
    else:
        text = "most recent sample"
    if window:
        start, end = (None if w is None else bivariate._day(w).isoformat()
                      for w in window)
        text += " in %s to %s" % (start or "…", end or "…")
    return text


def points_options(store, table, columns, mode="all", window=None):
    """The `solid` and `points` options for a mode and window."""
    names = [c["chemical"] for c in columns] + ["", ""]
    label = bivariate.MODE_LABELS[mode].format(x=names[0], y=names[1], z=names[2])
    return {
        "solid": solid_table(store, table, columns, mode, window),
        "points": {"mode": mode, "label": label,
                   "map": map_phrase(mode, columns, window),
                   "window": [None if w is None else bivariate._day(w).isoformat()
                              for w in window] if window else None},
    }


def scatter_options(store, pivot, first, second, matrix, locations=None,
                    mode="all", window=None):
    """The page's scatter options for two column groups of `pivot`.

    Returns (options, breaks). One authority for classes and colours
    (bivariate.py): the page colours points with these breaks and this
    palette, and the map layer is built from the same answers.

    options["pivot"] is the pair's own table (pivot.pair_table), not the
    pivot: everything after this - breaks, stations, every page query -
    reads the same two columns again and again.
    """
    truth = list(DETECT_TRUE)
    table = pv.pair_table(store, pivot, first, second)
    breaks = bivariate.axis_breaks(store.con, table,
                                   first["prefix"], second["prefix"], truth)
    options = {
        "pivot": table,
        "x": column_meta(first), "y": column_meta(second),
        "matrix": "+".join(matrix) if isinstance(matrix, (list, tuple))
                  else (matrix or ""),
        # Which flag values mean "detected": the store's own list, so the
        # page never carries a second copy of it.
        "detectTrue": truth,
        "resolved": first["resolved"] + second["resolved"],
        # What the pivot left out, for the caption to say.
        "excluded": dict(pivot.excluded),
        "bivariate": {
            "x": breaks["x"], "y": breaks["y"],
            "palette": bivariate.PALETTE, "grey": bivariate.GREY,
            "paletteName": bivariate.PALETTE_NAME,
        },
    }
    if locations is not None:
        options["locations"] = list(locations)
    # The breaks above are over EVERY sample, whatever the mode or window:
    # colours keep their meaning as the window moves, as the axes do.
    options.update(points_options(store, table, [first, second], mode, window))
    return options, breaks


def ternary_options(store, pivot, columns, matrix, locations=None,
                    mode="all", window=None):
    """The page's ternary options for three column groups of `pivot`.

    Shares and colours are ternary.py's; the page's SQL is checked against
    it. No breaks: a ternary's colour is its composition, not a class.
    """
    table = pv.chart_table(store, pivot, columns)
    options = {
        "kind": "ternary",
        "pivot": table,
        "x": column_meta(columns[0]), "y": column_meta(columns[1]),
        "z": column_meta(columns[2]),
        "matrix": "+".join(matrix) if isinstance(matrix, (list, tuple))
                  else (matrix or ""),
        "detectTrue": list(DETECT_TRUE),
        "resolved": sum(c["resolved"] for c in columns),
        "excluded": dict(pivot.excluded),
        "ternary": {"corners": list(ternary.CORNERS), "grey": bivariate.GREY},
    }
    if locations is not None:
        options["locations"] = list(locations)
    options.update(points_options(store, table, columns, mode, window))
    return options


class Scatter(object):
    """A choice resolved to something drawable: what the host then shows.

    `kind` is "scatter" or "ternary"; `columns` its two or three analytes.
    """

    def __init__(self, pivot, columns, options, breaks, stations,
                 kind="scatter"):
        self.pivot = pivot
        self.columns = list(columns)
        self.first, self.second = self.columns[0], self.columns[1]
        self.kind = kind
        self.options, self.breaks, self.stations = options, breaks, stations


class Session(object):
    """The chooser's state for ONE store. A new store means a new Session.

    Holds the every-analyte pivot per (matrix, row scope), and what the
    page was last offered, so a re-offer is sent only when it would say
    something different - a catalogue for tailings is ~100 KB of JSON.

    ITS SQL RUNS ON ITS OWN CURSOR (`db`, store.thread_view()), because
    respond() runs in a worker: water's pivot is 3-4 s, and on the UI
    thread that froze all of QGIS. A cursor shares the database, so the
    tables it makes are the page's to query from the main connection.
    The row scope is still read from `store`: the cursor's copy of it is
    taken when the cursor is made and would go stale.
    """

    def __init__(self, store):
        self.store = store
        thread_view = getattr(store, "thread_view", None)
        self.db = thread_view() if thread_view is not None else store
        self._pivots = {}
        self._matrices = {}
        self.window = None           # the map's time window, set by the host
        self.current = None          # the Scatter last drawn, for repoint()
        self.offered = None          # the catalogue key the page last got
        self._paired_for = None      # (that key, x) the page has counts for
        self.choice = {}             # what the page last chose

    def needs_offer(self):
        """True when the page's catalogue is not the current pivot's.

        So a host can call for an offer on every tab show and data change,
        and pay for one only when the data or its row scope moved.
        """
        if self.offered is None:
            return True
        matrix, table = self.offered
        found = self._pivots.get((matrix, self._scope_key()))
        return found is None or not self._alive(found) or found.table != table

    def page_reloaded(self):
        """A new page knows nothing: offer it everything again."""
        self.offered = self._paired_for = None

    # ---------------------------------------------------------------- reads

    def _scope_key(self):
        return tuple(str(part) for part in self.store.scope)

    def matrices(self):
        """[{code, samples}] for every matrix with normal samples, largest first."""
        key = self._scope_key()
        if key not in self._matrices:
            store = self.db
            normal = ("upper(coalesce(trim(sample_type), '')) = 'N'"
                      if "sample_type" in store.present else "TRUE")
            has_sample = ("coalesce(trim(sample), '') <> ''"
                          if "sample" in store.present else "FALSE")
            rows = store.con.execute("""
                SELECT matrix, count(DISTINCT sample) AS n FROM v_results
                WHERE coalesce(trim(matrix), '') <> '' AND ({normal})
                  AND ({has_sample}) AND {primary}
                GROUP BY 1 ORDER BY 2 DESC, 1""".format(
                    normal=normal, has_sample=has_sample,
                    # Counted as the pivot will count them: without field
                    # duplicates and replicates (SO: 112, not 839).
                    primary=pv.not_field_replicate(store)
                    if "sample" in store.present else "TRUE")).fetchall()
            self._matrices[key] = [{"code": code, "samples": int(n)}
                                   for code, n in rows]
        return self._matrices[key]

    def built(self, matrix):
        """True when `matrix`'s pivot is in hand, so asking for it is cheap."""
        found = self._pivots.get((matrix, self._scope_key()))
        return found is not None and self._alive(found)

    def _alive(self, pivot):
        # pivot.KEEP_TABLES retires old pivots, including this one if enough
        # console scatters were built since. A retired one is rebuilt.
        return pivot.table in (getattr(self.db, "_pivot_tables", None) or ())

    def pivot(self, matrix):
        """Every analyte in `matrix`, one row per sample. Built once per scope."""
        key = (matrix, self._scope_key())
        found = self._pivots.get(key)
        if found is not None and self._alive(found):
            return found
        # The shared view already holds the current scope; the cursor is
        # told so only so the new table's name hashes the right one.
        self.db._scope = self.store.scope
        variables = pv.all_variables(self.db, [matrix])
        if not variables:
            raise AnalysisError("No normal samples carry an analyte in %s."
                                % matrix)
        # on_mixed="skip": one analyte reported in two units must not cost
        # the other two hundred. Skipped ones are listed, with the reason.
        found = pv.build_pivot(self.db, variables, matrix=[matrix],
                               on_mixed="skip")
        self._pivots[key] = found
        return found

    def catalogue(self, matrix):
        """What the page's choosers list for `matrix`."""
        pivot = self.pivot(matrix)
        variables = []
        for c in pivot.columns:
            if not c["cells"]:
                continue
            variables.append(dict(
                column_meta(c),
                fractionName=FRACTION_NAMES.get(c["fraction"] or "",
                                                c["fraction"] or ""),
                samples=c["cells"],
                # A blank flag is unknown, neither detected nor not.
                detected=c["cells"] - c["nondetects"] - c["blank_detect"],
            ))
        return {
            "matrices": self.matrices(),
            "matrix": matrix,
            "pivot": pivot.table,
            "samples": pivot.rows,
            "variables": variables,
            "skipped": [dict(detail, chemical=v.chemical,
                             fraction=v.fraction if isinstance(v.fraction, str)
                             else "", reason=reason)
                        for (v, reason), detail in zip(
                            pivot.skipped, getattr(pivot, "skipped_detail", None)
                            or [{} for _ in pivot.skipped])],
            "excluded": dict(pivot.excluded),
        }

    def column(self, matrix, prefix):
        for c in self.pivot(matrix).columns:
            if c["prefix"] == prefix:
                return c
        return None

    def find(self, matrix, name):
        """The prefix of the first column for a chemical name or CAS, or None.

        For a caller holding names (the console, the preview's --x/--y);
        the page itself always sends prefixes.
        """
        text = str(name or "").strip().lower()
        found = [c for c in self.pivot(matrix).columns
                 if text in ((c["chemical"] or "").lower(),
                             (c["cas_rn"] or "").lower(), c["prefix"])]
        # Total before any other fraction: a name alone means the whole
        # sample, as it does on the panel's pickers.
        found.sort(key=lambda c: (c["fraction"] or "") != "T")
        return found[0]["prefix"] if found else None

    def _carried(self, matrix, prefix, previous):
        """`prefix` in `matrix`, or the same analyte under another name.

        Column names depend on the matrix - tailings holds two fractions,
        so its Arsenic is `arsenic_t` where soil's is `arsenic` - and a
        choice should survive a change of matrix when the analyte is there.
        Same chemical and fraction first; then the same chemical, total
        first. Never a different chemical.
        """
        columns = self.pivot(matrix).columns
        if any(c["prefix"] == prefix for c in columns):
            return prefix
        if not previous:
            return None
        same = [c for c in columns if c["chemical"] == previous["chemical"]
                and c["cells"]]
        exact = [c for c in same if (c["fraction"] or "") == previous["fraction"]]
        same.sort(key=lambda c: (c["fraction"] or "") != "T")
        pick = (exact or same or [None])[0]
        return pick["prefix"] if pick else None

    # -------------------------------------------------------------- respond

    def respond(self, choice, say, send, locations=None):
        """Answer a choice from the page. Returns a Scatter to draw, or None.

        `say(message, where)` announces a slow step BEFORE it starts (the
        host shows it and repaints). `where` is "chooser" for the pivot -
        it is the chooser's own work, and a spinner put in the chart area
        would replace a histogram with a wait that no chart then ends - or
        "chart" when a scatter will follow. `send(name, payload)` calls
        window.equis[name] on the page. Every path sends something, so a page that asked is never
        left waiting on silence.
        """
        choice = dict(choice or {})
        matrix = choice.get("matrix") or None
        known = {m["code"] for m in self.matrices()}
        if matrix not in known:
            matrix = None
        if matrix is None:
            send("offer", {"matrices": self.matrices(), "matrix": None,
                           "variables": [], "skipped": []})
            self.offered = self._paired_for = None
            self.choice = {}
            return None
        if not self.built(matrix):
            say("Pivoting every analyte in %s: one row per sample..." % matrix,
                "chooser")
        catalogue = self.catalogue(matrix)
        prefixes = {v["prefix"] for v in catalogue["variables"]}
        if matrix != self.choice.get("matrix"):
            # A new matrix: the same analytes, if it has them.
            choice["x"] = self._carried(matrix, choice.get("x"), self.choice.get("x_meta"))
            choice["y"] = self._carried(matrix, choice.get("y"), self.choice.get("y_meta"))
            choice["z"] = self._carried(matrix, choice.get("z"), self.choice.get("z_meta"))
        x = choice.get("x") if choice.get("x") in prefixes else None
        y = choice.get("y") if choice.get("y") in prefixes else None
        z = choice.get("z") if choice.get("z") in prefixes else None
        chart = "ternary" if choice.get("chart") == "ternary" else "scatter"
        offered_key = (matrix, catalogue["pivot"])
        if self.offered != offered_key:
            send("offer", dict(catalogue, x=x, y=y, z=z, chart=chart))
            self.offered = offered_key
        pivot = self.pivot(matrix)
        # Keyed on the catalogue too: a fresh offer rebuilds the page's
        # lists, which drops the pair counts written into them.
        if x and self._paired_for != (offered_key, x):
            send("paired", {"x": x, "counts": pv.pair_counts(
                self.db.con, pivot, x)})
            self._paired_for = (offered_key, x)
        # A ternary's third list counts the samples carrying X AND Y too.
        if (chart == "ternary" and x and y and x != y
                and getattr(self, "_paired_z_for", None) != (offered_key, x, y)):
            send("pairedZ", {"x": x, "y": y, "counts": pv.pair_counts(
                self.db.con, pivot, [x, y])})
            self._paired_z_for = (offered_key, x, y)
        meta = lambda p: ({"chemical": self.column(matrix, p)["chemical"],
                           "fraction": self.column(matrix, p)["fraction"] or ""}
                          if p else None)
        self.choice = {"matrix": matrix, "x": x, "y": y, "z": z, "chart": chart,
                       "mode": self.choice.get("mode", "all"),
                       "x_meta": meta(x), "y_meta": meta(y), "z_meta": meta(z)}
        if chart == "ternary":
            return self._ternary(matrix, pivot, x, y, z, choice, say, send,
                                 locations)
        if not (x and y) or x == y:
            send("note", "Choose %s to scatter against %s." % (
                "a different analyte" if x and x == y else "two analytes",
                "it" if x == y else "each other"))
            return None
        mode = choice.get("mode") if choice.get("mode") in bivariate.MODES else "all"
        self.choice["mode"] = mode
        first, second = self.column(matrix, x), self.column(matrix, y)
        say("Classing %s x %s (Jenks breaks)..." % (first["chemical"],
                                                    second["chemical"]), "chart")
        options, breaks = scatter_options(self.db, pivot, first, second,
                                          matrix, locations, mode, self.window)
        stations = bivariate.stations(self.db.con, options["pivot"],
                                      first["prefix"], second["prefix"],
                                      list(DETECT_TRUE), mode, self.window)
        self.current = Scatter(pivot, [first, second], options, breaks, stations)
        return self.current

    def _ternary(self, matrix, pivot, x, y, z, choice, say, send, locations):
        """Three analytes as shares of their sum (ternary.py). Or a note why not."""
        chosen = [p for p in (x, y, z) if p]
        if len(chosen) < 3 or len(set(chosen)) < 3:
            send("note", "Choose three different analytes for a ternary.")
            return None
        columns = [self.column(matrix, p) for p in (x, y, z)]
        refusal = ternary.unit_refusal(columns)
        if refusal:
            send("note", refusal)
            return None
        mode = choice.get("mode") if choice.get("mode") in bivariate.MODES else "all"
        self.choice["mode"] = mode
        say("Placing %s : %s : %s..." % tuple(c["chemical"] for c in columns),
            "chart")
        options = ternary_options(self.db, pivot, columns, matrix, locations,
                                  mode, self.window)
        stations = ternary.stations(self.db.con, options["pivot"],
                                    [c["prefix"] for c in columns],
                                    list(DETECT_TRUE), mode, self.window)
        self.current = Scatter(pivot, columns, options, None, stations,
                               kind="ternary")
        return self.current

    def repoint(self, mode=None, window=False):
        """The current scatter with a new points mode and/or time window.

        Re-picks the solid samples and the map's stations only: the pair,
        the axes and the breaks stay - the axes because points dimming is
        the design (decided 2026-10-12), the breaks so a colour means the
        same thing in every window. `window=False` keeps the current one.
        Returns the new Scatter, or None when nothing is drawn.
        """
        if self.current is None:
            return None
        if window is not False:
            self.window = window
        if mode in bivariate.MODES:
            self.choice["mode"] = mode
        mode = self.choice.get("mode") or "all"
        found = self.current
        if mode in bivariate._AXIS and bivariate._AXIS[mode] >= len(found.columns):
            mode = self.choice["mode"] = "all"   # "highest Z" on a scatter
        options = dict(found.options)
        options.update(points_options(self.db, options["pivot"], found.columns,
                                      mode, self.window))
        prefixes = [c["prefix"] for c in found.columns]
        if found.kind == "ternary":
            stations = ternary.stations(self.db.con, options["pivot"], prefixes,
                                        list(DETECT_TRUE), mode, self.window)
        else:
            stations = bivariate.stations(self.db.con, options["pivot"],
                                          prefixes[0], prefixes[1],
                                          list(DETECT_TRUE), mode, self.window)
        self.current = Scatter(found.pivot, found.columns, options, found.breaks,
                               stations, kind=found.kind)
        return self.current
