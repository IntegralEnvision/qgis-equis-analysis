"""Why the adapter's map and the store's differ, cell by cell.

Phase 1 of docs/chemduck-adoption-plan.md. The panel maps ReportStore's
answer; chem.py asks ChemDuck the same question. They are meant to differ -
ChemDuck keeps depths and method classes apart, ranks the samples of one
date, chooses among a sample's re-runs, reaches the quantitation limit,
leaves out what EQuIS marks not reportable and QC samples - so nothing here
demands that they agree. It names, for each location, which of INTENDED
accounts for the difference, and checks each claimed cause against the
rows, so a broken rule cannot hide behind a listed category: a non-detect
drawn at the wrong factor is not "a different limit", because the limit is
checked. A difference no cause accounts for comes back as "unexplained: ...".

Two readers, one rule set (CLAUDE.md, "one authority per decision"):
tools/test_chemduck_parity.py fails on any unexplained difference, and the
panel's debug shadow (`shadow_map`, below) logs each map built. Until
2026-11-25 this lived in the test alone.

Imports no QGIS, like store.py and chem.py.
"""
import collections
import datetime
import math
import time

from . import chem as ch
from . import report_data as rd
from .store import ND_COLUMN, duckdb_module

# Every way the two maps may differ, and why it is meant. A difference that
# is none of these is unexplained. Each is checked against the rows in
# Comparison.explain, never assumed from the shape of the answer alone.
INTENDED = {
    "points": "positions and depths kept apart: ChemDuck answers per "
              "measurement point, and a location with several is rolled up "
              "here by the plugin's rule before comparing (shared rules D1; "
              "the plan's decision 2)",
    "method_classes": "methods of different classes are different variables, "
                      "a field and a lab method (shared rules D3)",
    "same_date": "several samples of one date: ChemDuck ranks them "
                 "(rank_across); the plugin takes the first row",
    "reruns": "several results of one sample: ChemDuck chooses by the "
              "reportable flag, the test type, then rank_reruns; the plugin "
              "takes the first row, or the highest",
    "tie_later_date": "equal values on two dates: ChemDuck reports the later "
                      "date, the plugin the first row",
    "quantitation_limit": "a non-detect with only a quantitation limit: "
                          "ChemDuck's limit chain reaches it, the plugin's "
                          "value ladder does not",
    "nd_limit": "a non-detect valued from a different limit: ChemDuck's chain "
                "starts at REPORT_RESULT_LIMIT, the plugin's ladder at "
                "REPORT_RESULT_VALUE (equal at every one of Facility A's "
                "1,615,175 non-detects, 2026-10-29)",
    "non_reportable": "results EQuIS marks not reportable are left out by "
                      "default (the plan's decision 6)",
    "qc_samples": "only normal samples by default: field duplicates, blanks "
                  "and other QC types left out (the plan's decision 5)",
    "units_refused": "a variable in more than one unit: ChemDuck refuses the "
                     "call (p_units := 'refuse'), and the plugin's unit guard "
                     "(the panel's _confirm_units, from units_for) stops it "
                     "too, though as a question the user can answer yes to",
    "units_converted": "a variable in two units ChemDuck can convert between: "
                       "it converts at load, by its own factors (unit_source "
                       "'chemduck'), where the plugin's guard asks; CLAUDE.md "
                       "puts conversion in the pipeline, with EQuIS's factors "
                       "(the plan's decision 9, open)",
    "units_by_method_class": "units that differ only between method classes: "
                             "each of ChemDuck's variables is in one unit, the "
                             "plugin's guard pools every method and asks "
                             "(Facility A's Aluminum in SE: 6 results in me/L, "
                             "class UNK, beside mg/kg)",
    "unit_contradicts_matrix": "a unit that contradicts its matrix, a solid's "
                               "on water: ChemDuck refuses it even alone, the "
                               "plugin's guard asks only about mixed units "
                               "and maps it (ChemDuck's shared rules D5)",
}


def close(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= 1e-9 * max(abs(a), abs(b), 1.0)


def day(value):
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value)[:10])


def same(cell, point):
    """The store's cell and one ChemDuck point say the same thing: value,
    detection, day and unit."""
    if not close(cell["value"], point["value"]):
        return False
    if cell["value"] is None:
        return True
    return (cell["detect"] in ("Y", "YES", "TRUE", "1")) == (point["detect"] == "Y") \
        and day(cell["date"]) == day(point["date"]) and cell["unit"] == point["unit"]


def best(points, how):
    """The points the plugin's own rule would put first, ties included (the
    plugin breaks a tie by row order, which ChemDuck's points don't carry)."""
    if how == rd.MAXIMUM:
        key = lambda p: (p["detect"] == "Y", -math.inf if p["value"] is None else p["value"])
    else:
        key = lambda p: (p["date"] is not None, str(p["date"] or ""))
    top = max(key(p) for p in points)
    return [p for p in points if key(p) == top]


def _codes(value):
    """The panel's matrix or fraction choice as a tuple, None for all."""
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    return tuple(value) or None


class Comparison(object):
    """One map's question under one recipe, both sides asked once.

    matrix and fraction are the panel's (a code, a tuple of codes, or None).
    `plugin` is the store's answer when the caller already has it - the
    panel does, it just drew it - so it is not asked twice.
    """

    def __init__(self, store, chem, chemical, matrix, fraction, how, nd, plugin=None):
        self.store, self.chem = store, chem
        self.chemical, self.how, self.nd = chemical, how, nd
        self.matrix, self.fraction = _codes(matrix), _codes(fraction)
        self.kwargs = dict(how=how, non_detect=nd, matrix=self.matrix, fraction=self.fraction)
        self.plugin = plugin if plugin is not None else store.aggregate(chemical, **self.kwargs)
        self.refusal = None
        began = time.perf_counter()
        try:
            self.points = chem.by_location(chemical, **self.kwargs)
        except duckdb_module().Error as exc:
            # ChemDuck's unit guard refuses the whole call. Facility A's
            # Mercury in WS dissolved has 130 results in ug/kg beside 7,818
            # in ug/L (2026-10-29).
            if "unit" not in str(exc):
                raise
            self.points, self.refusal = {}, str(exc).splitlines()[0]
        self.seconds = time.perf_counter() - began
        self._relaxed = {}

    def locations(self):
        return sorted(set(self.plugin) | set(self.points))

    def relaxed(self, name):
        """ChemDuck asked again with one of its defaults taken away."""
        if name not in self._relaxed:
            extra = {"non_reportable": {"reportable_only": False},
                     "qc_samples": {"sample_types": []}}[name]
            self._relaxed[name] = self.chem.by_location(self.chemical, **dict(self.kwargs, **extra))
        return self._relaxed[name]

    def rows(self, location, on_day):
        """The store's rows at a location on one day: (value, detected,
        sample) under this recipe's non-detect column."""
        where, params = self.store._where(chemical=self.chemical, matrix=self.matrix,
                                          fraction=self.fraction, location=location)
        return [r[1:] for r in self.store.con.execute(
            "SELECT CAST(sample_date AS DATE), %s, detected, sample FROM v_results WHERE %s"
            % (ND_COLUMN[self.nd], where), params).fetchall() if r[0] == on_day]

    def explain(self, location):
        """The set of INTENDED causes that account for this location's
        difference (empty when the two agree), or a set holding one
        "unexplained: ..." entry."""
        if self.refusal is not None:
            if self._guarded():
                return {"units_refused"}
            if "contradicts its matrix" in self.refusal:
                return {"unit_contradicts_matrix"}
            return {"unexplained: ChemDuck refused, the plugin's unit guard would not "
                    "(%s)" % self.refusal[:100]}
        if self._guarded():
            # The panel would stop this layer and ask, so its values are
            # not the plugin's answer; ChemDuck answered, so it converted.
            every = [p for found in self.points.values() for p in found]
            if any("chemduck" in str(p.get("unit_source") or "") for p in every):
                return {"units_converted"}
            by_class = collections.defaultdict(set)
            for p in every:
                by_class[p["method_class"]].add(p["unit"])
            if len(by_class) > 1 and all(len(u) == 1 for u in by_class.values()):
                return {"units_by_method_class"}
            return {"unexplained: the plugin's unit guard would ask, and ChemDuck "
                    "answered without converting"}
        cell = self.plugin.get(location)
        points = self.points.get(location, [])
        causes = set()
        if cell is None:
            return {"unexplained: only in ChemDuck"}
        if not points:
            for name in ("non_reportable", "qc_samples"):
                if location in self.relaxed(name):
                    return {name}
            return {"unexplained: only in the plugin"}
        if len(points) > 1:
            if len({p["method_class"] for p in points}) > 1:
                causes.add("method_classes")
            if len({(p["top_depth"], p["bottom_depth"], p["position"]) for p in points}) > 1:
                causes.add("points")
            if not causes:
                return {"unexplained: several points, neither depth nor method class"}
        candidates = best(points, self.how)
        if any(same(cell, p) for p in candidates):
            return causes
        if causes:
            # Equal values at several points: which date the roll-up reports
            # has no rule across points (decision 2). Part of the points'
            # difference, if the plugin really has that value on that date.
            # Facility A: a surface-water station sampled at 21 depths, every
            # Lead result a non-detect at 5 ug/L.
            for p in candidates:
                if same(dict(cell, date=p["date"]), p) and any(
                        close(r[0], cell["value"]) for r in self.rows(location, day(p["date"]))):
                    return causes
        # The same result in another unit: ChemDuck converted it at load and
        # the plugin did not. Without a unit filter the plugin's guard asks
        # first (above); with one (ChemDuck #18) the guard has nothing to ask,
        # so the conversion shows here, one cell at a time.
        if any("chemduck" in str(p.get("unit_source") or "") and p["unit"] != cell["unit"]
               for p in candidates):
            return causes | {"units_converted"}
        point = candidates[0]
        cause = self._why(location, cell, point)
        return causes | {cause}

    def _guarded(self):
        """The panel's unit guard, asked as _confirm_units asks it: more
        than one named unit among the rows that would map."""
        if not hasattr(self, "_mixed"):
            found = self.store.units_for(self.chemical, matrix=self.matrix,
                                         fraction=self.fraction, non_detect=self.nd)
            self._mixed = len([unit for unit, _rows in found if unit]) > 1
        return self._mixed

    def _why(self, location, cell, point):
        factor = ch.DISPLAY_FACTOR[self.nd]
        if cell["value"] is None:
            if point["limit_source"] == "quantitation" and point["detect"] == "N" \
                    and close(point["value"], factor * point["detection_limit"]):
                return "quantitation_limit"
            return "unexplained: no value in the plugin"
        for name in ("non_reportable", "qc_samples"):
            if any(same(cell, p) for p in best(self.relaxed(name).get(location, []) or [point],
                                               self.how)):
                return name
        cell_day, point_day = day(cell["date"]), day(point["date"])
        if close(cell["value"], point["value"]) and cell_day != point_day:
            tied = [r for r in self.rows(location, point_day) if close(r[0], cell["value"])]
            if tied and point_day > cell_day:
                return "tie_later_date"
            return "unexplained: same value, another date"
        if cell_day == point_day:
            on_day = self.rows(location, cell_day)
            if len({r[2] for r in on_day}) > 1:
                return "same_date"
            if len(on_day) > 1 and any(close(r[0], point["value"]) for r in on_day):
                return "reruns"
            if cell["detect"] == "N" and point["detect"] == "N" \
                    and close(point["value"], factor * point["detection_limit"]) \
                    and not close(cell["value"], point["value"]):
                return "nd_limit"
        return "unexplained: value %s vs %s" % (cell["value"], point["value"])


# --------------------------------------------------- the panel's debug shadow

# How long one map's causes may take on the UI thread. Explaining a
# difference can ask the store for a location's rows on a day, and Facility
# A's 12 analytes took 888 s explained cell by cell (2026-10-29). Past the
# budget the rest are COUNTED, not explained, and the line says so: a check
# that never ran is not one that passed.
BUDGET_SECONDS = 2.0


def not_comparable(how, nd, matrix, fraction, method):
    """Why ChemDuck can't be asked the panel's question, or None."""
    if how not in ch.EVENT_AGG:
        return "not an aggregate map; ChemDuck is asked for aggregates only"
    if nd not in ch.DISPLAY_FACTOR:
        return "the non-detect choice has no ChemDuck factor yet (the plan's decision 1)"
    if method:
        return ("an analytic-method filter: ChemDuck has none, it keeps method "
                "classes apart instead")
    if not _codes(matrix):
        return ("no matrix chosen: ChemDuck keeps each matrix's points apart, and how "
                "they pool is phase 2's guard question")
    if len(_codes(fraction) or ()) > 1:
        return "several fractions: a map is one"
    return None


def shadow_map(store, chem, chemical, how, nd, matrix, fraction, method=None,
               plugin=None, budget=BUDGET_SECONDS):
    """One map built, compared: a dict for `describe`.

    Never raises for ChemDuck's sake: a refusal or an error is part of the
    report, because the shadow must not stop the map it is watching.
    """
    report = {"chemical": chemical, "how": how, "nd": nd, "matrix": _codes(matrix),
              "fraction": _codes(fraction)}
    reason = not_comparable(how, nd, matrix, fraction, method)
    if reason:
        report["skipped"] = reason
        return report
    try:
        comparison = Comparison(store, chem, chemical, matrix, fraction, how, nd,
                                plugin=plugin)
    except Exception as exc:                   # noqa: BLE001 - see docstring
        report["error"] = "%s: %s" % (type(exc).__name__, str(exc).splitlines()[0][:200])
        return report
    report["chemduck_seconds"] = comparison.seconds
    report["refusal"] = comparison.refusal
    tally = collections.Counter()
    examples = collections.defaultdict(list)
    locations = comparison.locations()
    began = time.perf_counter()
    for index, location in enumerate(locations):
        if time.perf_counter() - began > budget:
            tally["not explained (time budget)"] += len(locations) - index
            break
        try:
            causes = comparison.explain(location)
        except Exception as exc:               # noqa: BLE001 - see docstring
            causes = {"unexplained: %s while explaining" % type(exc).__name__}
        tally["agree" if not causes else "differ"] += 1
        for cause in causes:
            tally[cause] += 1
            if len(examples[cause]) < 3:
                examples[cause].append(location)
    report["explain_seconds"] = time.perf_counter() - began
    report["locations"] = len(locations)
    report["tally"] = tally
    report["examples"] = dict(examples)
    return report


def describe(report, store_seconds=None):
    """One log line for a shadow_map report."""
    head = "ChemDuck shadow, %s (%s, %s ND, matrix %s, fraction %s)" % (
        report["chemical"], report["how"], report["nd"],
        "+".join(report["matrix"] or ()) or "all", "+".join(report["fraction"] or ()) or "any")
    if "skipped" in report:
        return "%s: not compared, %s." % (head, report["skipped"])
    if "error" in report:
        return "%s: ChemDuck failed, %s." % (head, report["error"])
    tally = report["tally"]
    parts = ["agree %d" % tally.get("agree", 0)]
    for cause, n in sorted(tally.items(), key=lambda item: (-item[1], item[0])):
        if cause in ("agree", "differ"):
            continue
        where = report["examples"].get(cause)
        parts.append("%s %d%s" % (cause, n, " (%s)" % ", ".join(where) if where
                                  and cause.startswith("unexplained") else ""))
    unexplained = sum(n for c, n in tally.items() if c.startswith("unexplained"))
    timing = "ChemDuck %.2f s" % report["chemduck_seconds"]
    if store_seconds is not None:
        timing = "store %.2f s, %s" % (store_seconds, timing)
    return "%s: %d location%s; %s.%s %s, causes %.2f s." % (
        head, report["locations"], "" if report["locations"] == 1 else "s",
        "; ".join(parts),
        " ChemDuck refused: %s." % report["refusal"] if report.get("refusal") else "",
        timing, report["explain_seconds"]) + (
        " %d UNEXPLAINED." % unexplained if unexplained else "")
