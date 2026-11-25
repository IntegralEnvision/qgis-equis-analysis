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
from equis_loader.chem_parity import INTENDED, Comparison   # noqa: E402
from equis_loader.store import ReportStore             # noqa: E402

# The causes, and the comparison that names them, are equis_loader/
# chem_parity.py's: the panel's debug shadow logs the same causes for
# each map built, so the rules live in one place.

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
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def scope_checks(store, chem):
    """The panel's row scope, set on both sides as _apply_row_scope does,
    reaches ChemDuck's call: a date window changes the answer on both, and
    a sample-type scope that takes in field duplicates removes the one
    difference they made."""
    def scoped(start=None, end=None, types=None, depth=None, units=None):
        store.set_row_scope(start, end, types, depth, units)
        chem.set_row_scope(start, end, types, depth, units)
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

        # The store's depth and unit filters (2026-10-30) reach ChemDuck
        # as its row scope (#18). Until then the adapter refused them by
        # name, rather than answer unfiltered beside a filtered store.
        found, cells = scoped(depth=(0, 0.5, "m"))
        loose = sorted(c for c in found if c not in INTENDED)
        check("under a depth filter, every difference is still a listed one", not loose,
              ", ".join(loose))
        mapped = {key[3] for key in cells}
        left = {cause for causes in cells.values() for cause in causes}
        check("0-0.5 m leaves PROFILE's two 0.5 m samples and nothing undepthed, on both "
              "maps: one point, so no 'points' difference; only its equal non-detects' "
              "tie under maximum is left",
              mapped == {"PROFILE"} and left == {"tie_later_date"},
              "%s; %s" % (sorted(mapped), sorted(left)))

        found, cells = scoped(units=["ug/L"])
        loose = sorted(c for c in found if c not in INTENDED)
        check("under a unit filter, every difference is still a listed one", not loose,
              ", ".join(loose))
        solid = {k: v for k, v in cells.items() if k[3] == "MIXED-SOLID-UNITS"}
        check("a ug/L filter drops MIXED-SOLID-UNITS' ug/kg result on both sides, so the "
              "map both refused agrees", solid and not any(solid.values()),
              str(solid) or "not compared")
        iron = {cause for k, v in cells.items() if k[3] == "MIXED-UNITS" for cause in v}
        check("... and keeps MIXED-UNITS' ug/L result on both: the plugin maps it as "
              "reported, ChemDuck in the unit it converted to at load", iron == {
                  "units_converted"}, sorted(iron))
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
