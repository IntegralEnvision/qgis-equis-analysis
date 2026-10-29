"""The adapter's map beside the store's: every difference listed, or a failure.

Phase 1 of docs/chemduck-adoption-plan.md. The panel maps ReportStore's
answer; equis_loader/chem.py asks ChemDuck the same question in the shadow.
They are meant to differ - ChemDuck keeps depths and method classes apart,
ranks the samples of one date, chooses among a sample's re-runs, reaches the
quantitation limit, leaves out what EQuIS marks not reportable and QC
samples - so this does not demand that they agree. It demands that EVERY
difference is one of INTENDED below, and it checks each claimed cause
against the rows, so a broken rule cannot hide behind a listed category: a
non-detect drawn at the wrong factor is not "a different limit", because
the limit is checked.

It replaces tools/measure_chemduck_parity.py, which compared against
ChemDuck 0.1's aggregate_results through its repository's tools/ path and
reported causes without failing on any.

    python tools/test_chemduck_parity.py
        the fixture facility (tools/chem_fixture.py): every location's
        differences must be exactly the ones it declares, both ways of
        opening ChemDuck must give the same points, and every INTENDED
        cause must be exercised
    python tools/test_chemduck_parity.py --manifest <facility>/manifest.json
        --pub <sidecar dir> [--analytes 12] [--threads 4] [--out <dir>]
        a real facility, the pre-release smoke test: a tally by cause, and a
        failure for any difference no listed cause explains

Needs duckdb and pyyaml, as ChemDuck does; no pyarrow (the fixture is written
by DuckDB, and ChemDuck reads it by path).
"""
import argparse
import collections
import datetime
import json
import math
import os
import shutil
import sys
import tempfile
import time

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from equis_loader import chem as ch                 # noqa: E402
from equis_loader import chemduck as cd             # noqa: E402
from equis_loader import report_data as rd          # noqa: E402
from equis_loader.store import ND_COLUMN, ReportStore   # noqa: E402

# Every way the two maps may differ, and why it is meant. A difference that
# is none of these fails. Each is checked against the rows in explain(),
# never assumed from the shape of the answer alone.
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

# (aggregation, non-detect choice): the panel's choices ChemDuck can answer.
# "Excluded" waits on decision 1.
RECIPES = [(rd.MOST_RECENT, rd.ND_HALF), (rd.MOST_RECENT, rd.ND_FULL),
           (rd.MAXIMUM, rd.ND_HALF), (rd.MAXIMUM, rd.ND_FULL)]

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + str(detail)) if detail != "" else ""))
    if not condition:
        FAILURES.append(label)


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


class Comparison(object):
    """One (analyte, matrix, fraction) under one recipe, both sides asked once."""

    def __init__(self, store, chem, chemical, matrix, fraction, how, nd):
        self.store, self.chem = store, chem
        self.chemical, self.matrix, self.fraction = chemical, matrix, fraction
        self.how, self.nd = how, nd
        self.kwargs = dict(how=how, non_detect=nd, matrix=(matrix,), fraction=(fraction,))
        self.plugin = store.aggregate(chemical, **self.kwargs)
        self.refusal = None
        try:
            self.points = chem.by_location(chemical, **self.kwargs)
        except duckdb.Error as exc:
            # ChemDuck's unit guard refuses the whole call. Facility A's
            # Mercury in WS dissolved has 130 results in ug/kg beside 7,818
            # in ug/L (2026-10-29).
            if "unit" not in str(exc):
                raise
            self.points, self.refusal = {}, str(exc).splitlines()[0]
        self._relaxed = {}

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
        where, params = self.store._where(chemical=self.chemical, matrix=(self.matrix,),
                                          fraction=(self.fraction,), location=location)
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
        point = candidates[0]
        cause = self._why(location, cell, point)
        return causes | {cause}

    def _guarded(self):
        """The panel's unit guard, asked as _confirm_units asks it: more
        than one named unit among the rows that would map."""
        if not hasattr(self, "_mixed"):
            found = self.store.units_for(self.chemical, matrix=(self.matrix,),
                                         fraction=(self.fraction,), non_detect=self.nd)
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


def combos(store, analytes=None):
    """Every (analyte, matrix, fraction) with a location, most results first.
    A blank matrix or fraction is no combination: ChemDuck can't be asked for
    the blank one alone."""
    rows = store.con.execute("""
        SELECT chemical, upper(matrix), upper(fraction), count(*) AS n FROM v_results
        WHERE coalesce(location, '') <> '' AND coalesce(trim(matrix), '') <> ''
          AND coalesce(trim(fraction), '') <> ''
        GROUP BY ALL ORDER BY n DESC, 1, 2, 3""").fetchall()
    if analytes:
        totals = collections.Counter()
        for chemical, _m, _f, n in rows:
            totals[chemical] += n
        keep = {c for c, _ in totals.most_common(analytes)}
        rows = [r for r in rows if r[0] in keep]
    return [r[:3] for r in rows]


def compare(store, chem, analytes=None, on_cell=None):
    """{(event, nd): Counter of cause -> cells} over every combination."""
    tallies = {}
    for how, nd in RECIPES:
        tally = collections.Counter()
        for chemical, matrix, fraction in combos(store, analytes):
            comparison = Comparison(store, chem, chemical, matrix, fraction, how, nd)
            for location in sorted(set(comparison.plugin) | set(comparison.points)):
                causes = comparison.explain(location)
                tally["cells"] += 1
                tally["agree" if not causes else "differ"] += 1
                for cause in causes:
                    tally[cause] += 1
                if on_cell:
                    on_cell(how, nd, (chemical, matrix, fraction), location, causes)
        tallies[(how, nd)] = tally
    return tallies


# ------------------------------------------------------------- the fixture

def fixture_checks():
    import chem_fixture as fx
    folder = tempfile.mkdtemp(prefix="chem_fixture_")
    try:
        manifest = fx.write(os.path.join(folder, "facility"))
        store = ReportStore.from_manifest(os.path.dirname(manifest))
        # One thread: on 36 rows a ChemDuck call took 0.11 s on one and
        # 0.41-0.85 s on fourteen (2026-10-29, this laptop) - per-thread
        # setup at every pipeline break, with nothing to share out.
        store.con.execute("SET threads = 1")
        chem = ch.Chem.load(store.con, manifest)

        # --- both ways of opening a facility give the same points -------------
        publish = os.path.join(folder, "published")
        ch._quiet(cd.publish, manifest, publish, x_col="LONGITUDE", y_col="LATITUDE",
                  quiet=True)
        with open(manifest, encoding="utf-8") as handle:
            parts = [os.path.join(os.path.dirname(manifest), p["path"])
                     for p in json.load(handle)["partitions"]]
        consumer = ReportStore.from_manifest(os.path.dirname(manifest))
        consumer.con.execute("SET threads = 1")
        published = ch.Chem.open_published(consumer.con, publish, parts)
        drop = ("x", "y")   # publish placed them from LONGITUDE/LATITUDE
        differ = []
        for how, nd in RECIPES:
            for chemical, matrix, fraction in combos(store):
                kwargs = dict(how=how, non_detect=nd, matrix=(matrix,), fraction=(fraction,))
                loaded, opened = (_answer(side, chemical, kwargs, drop)
                                  for side in (chem, published))
                if loaded != opened:
                    differ.append("%s %s/%s %s %s: %s" % (chemical, matrix, fraction, how, nd,
                                                          _first_difference(loaded, opened)))
        check("load_source and open_consumer give the same points, every recipe",
              not differ, "; ".join(differ[:3]))
        consumer.close()

        # --- every location's differences are the ones it declares -----------
        expected = {loc: expect for loc, _what, _rows, expect in fx.CASES}
        what = {loc: text for loc, text, _rows, _expect in fx.CASES}
        seen = collections.defaultdict(set)
        wrong = []

        def on_cell(how, nd, combo, location, causes):
            seen[location].add(how)
            want = expected.get(location)
            if want is None:
                wrong.append("%s: not a fixture location" % location)
                return
            if set(causes) != set(want[how]):
                wrong.append("%s (%s) %s/%s %s, %s: got %s, declared %s"
                             % (location, what[location], combo[1], combo[2], how, nd,
                                sorted(causes) or "agree", sorted(want[how]) or "agree"))

        tallies = compare(store, chem, on_cell=on_cell)
        for line in wrong:
            print("    " + line)
        check("every fixture location differs exactly as it declares (%d cells)"
              % sum(t["cells"] for t in tallies.values()), not wrong,
              "%d wrong" % len(wrong))
        missing = sorted(loc for loc in expected if seen[loc] != {rd.MOST_RECENT, rd.MAXIMUM})
        check("every fixture location was compared under both aggregations", not missing,
              ", ".join(missing))
        unknown = sorted({c for loc, _w, _r, e in fx.CASES for s in e.values() for c in s}
                         - set(INTENDED))
        check("every declared difference is an INTENDED one", not unknown, ", ".join(unknown))
        exercised = {c for t in tallies.values() for c in t if c in INTENDED}
        check("every INTENDED difference is exercised by the fixture",
              exercised == set(INTENDED), ", ".join(sorted(set(INTENDED) - exercised)))
        for (how, nd), tally in tallies.items():
            print("    %-12s %-5s %s" % (how, nd, dict(sorted(tally.items()))))
        scope_checks(store, chem)
        store.close()

        # --- what ChemDuck refuses to load today -------------------------------
        for location, text, case_rows in fx.REFUSED:
            sub = fx.write(os.path.join(folder, location), case_rows)
            probe = ReportStore.from_manifest(os.path.dirname(sub))
            plugin = probe.aggregate("Arsenic", matrix=("WG",), fraction=("T",))
            try:
                ch.Chem.load(probe.con, sub)
                refused = ""
            except Exception as exc:          # noqa: BLE001 - that is the finding
                refused = "%s: %s" % (type(exc).__name__, str(exc).splitlines()[0][:120])
            check("known: ChemDuck refuses a load with %s (raised; the plugin maps it, "
                  "value %s)" % (text, plugin.get(location, {}).get("value")),
                  bool(refused) and location in plugin,
                  refused or "it loads now: move %s into CASES" % location)
            probe.close()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def scope_checks(store, chem):
    """The panel's row scope, set on both sides as _apply_row_scope does,
    reaches ChemDuck's call: a date window changes the answer on both, and
    a sample-type scope that takes in field duplicates removes the one
    difference they made."""
    def scoped(start=None, end=None, types=None):
        store.set_row_scope(start, end, types)
        chem.set_row_scope(start, end, types)
        found = collections.Counter()
        cells = {}

        def on_cell(how, nd, combo, location, causes):
            found.update(causes)
            cells[(how, nd, combo, location)] = causes
        compare(store, chem, on_cell=on_cell)
        return found, cells

    try:
        found, _ = scoped(datetime.date(2020, 6, 1), datetime.date(2021, 12, 31))
        loose = sorted(c for c in found if c not in INTENDED)
        check("under a date window, every difference is still a listed one", not loose,
              ", ".join(loose))
        kwargs = dict(how=rd.MAXIMUM, matrix=("WG",), fraction=("T",))
        plugin = store.aggregate("Arsenic", **kwargs).get("AGREE", {}).get("value")
        adapter = [p["value"] for p in chem.points("Arsenic", **kwargs)
                   if p["location"] == "AGREE"]
        check("the date window moves both maxima at AGREE from 3 (2020-01-01) to 2",
              plugin == 2.0 and adapter == [2.0], "plugin %s, adapter %s" % (plugin, adapter))

        _, cells = scoped(types=["N", "FD"])
        field_dup = {cause for key, causes in cells.items() if key[3] == "FIELD-DUP"
                     for cause in causes}
        check("a sample-type scope taking in FD takes it in on both sides (FIELD-DUP agrees)",
              not field_dup, sorted(field_dup))
        check("... and leaves the equipment blank out of both (ONLY-BLANK on neither map)",
              not any(key[3] == "ONLY-BLANK" for key in cells))
    finally:
        store.set_row_scope()
        chem.set_row_scope()


def _answer(chem, chemical, kwargs, drop):
    """A call's points less `drop`, or its refusal's first line: two ways of
    opening ChemDuck must refuse alike as well as answer alike."""
    try:
        return [{k: v for k, v in p.items() if k not in drop}
                for p in chem.points(chemical, **kwargs)]
    except duckdb.Error as exc:
        return "refused: " + str(exc).splitlines()[0]


def _first_difference(left, right):
    if isinstance(left, str) or isinstance(right, str):
        return "%s vs %s" % (str(left)[:80], str(right)[:80])
    if len(left) != len(right):
        return "%d points vs %d" % (len(left), len(right))
    for a, b in zip(left, right):
        if a != b:
            keys = [k for k in a if a.get(k) != b.get(k)]
            return "%s: %s" % (a.get("location_id"),
                               ", ".join("%s %r vs %r" % (k, a[k], b.get(k)) for k in keys[:4]))
    return ""


# ---------------------------------------------------------- a real facility

def facility_checks(args):
    facility = os.path.dirname(os.path.abspath(args.manifest))
    with open(args.manifest, encoding="utf-8") as handle:
        parts = [os.path.join(facility, p["path"]) for p in json.load(handle)["partitions"]]
    if not os.path.exists(os.path.join(args.pub, "sidecars.json")):
        began = time.perf_counter()
        ch._quiet(cd.publish, args.manifest, args.pub, x_col="LONGITUDE", y_col="LATITUDE",
                  quiet=True)
        print("published the sidecars in %.1f s" % (time.perf_counter() - began))
    store = ReportStore.from_manifest(facility)
    chem = ch.Chem.open_published(store.con, args.pub, parts)
    if args.threads:
        store.con.execute("SET threads = %d" % args.threads)
    examples = collections.defaultdict(list)

    def on_cell(how, nd, combo, location, causes):
        for cause in causes:
            if len(examples[cause]) < 5:
                examples[cause].append({"recipe": "%s/%s" % (how, nd), "combo": combo,
                                        "location": location})

    began = time.perf_counter()
    tallies = compare(store, chem, analytes=args.analytes, on_cell=on_cell)
    seconds = round(time.perf_counter() - began, 1)
    unexplained = collections.Counter()
    for (how, nd), tally in tallies.items():
        print("%-12s %-5s %s" % (how, nd, dict(sorted(tally.items()))))
        for cause, n in tally.items():
            if cause.startswith("unexplained"):
                unexplained[cause] += n
    for cause in sorted(c for c in examples if c.startswith("unexplained")):
        print("    %s: %s" % (cause, examples[cause]))
    check("every difference on %s falls in a listed cause (%d analytes, %.0f s)"
          % (os.path.basename(facility), args.analytes, seconds),
          not unexplained, dict(unexplained))
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        path = os.path.join(args.out, "chemduck_parity.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"date": datetime.date.today().isoformat(), "manifest": args.manifest,
                       "analytes": args.analytes, "seconds": seconds,
                       "chemduck": cd.__version__,
                       "tallies": {"%s/%s" % k: dict(v) for k, v in tallies.items()},
                       "examples": examples}, handle, indent=1, default=str)
        print("wrote", path)
    store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--manifest", help="a real facility; omit for the fixture")
    parser.add_argument("--pub", help="that facility's ChemDuck sidecars (built if absent)")
    parser.add_argument("--analytes", type=int, default=12,
                        help="the most-measured analytes to compare")
    parser.add_argument("--threads", type=int, help="SET threads for the whole database")
    parser.add_argument("--out")
    args = parser.parse_args()
    if args.manifest:
        if not args.pub:
            parser.error("--manifest needs --pub")
        facility_checks(args)
    else:
        fixture_checks()
    print()
    if FAILURES:
        print("FAILED: %d" % len(FAILURES))
        return 1
    print("Every difference between the adapter's map and the store's is a listed one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
