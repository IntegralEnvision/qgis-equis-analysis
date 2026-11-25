"""The panel's ChemDuck shadow, without QGIS: built, compared, logged.

Phase 1 of docs/chemduck-adoption-plan.md: with the setting
`equis_loader/chemduck_shadow` on, the panel loads ChemDuck in the
background (chem.Chem.shadow) and logs, for each map built, its answer
beside the store's (chem_parity.shadow_map, describe). This holds the parts
the panel calls to the fixture facility:

- a live report's shadow (ChemDuck from the ReportTable's CSV) answers as
  ChemDuck from the published parquet does;
- on the live path - the store built from the table, as BuildStoreTask
  builds it - every fixture location differs exactly as it declares, so
  the causes the log names are the parity test's;
- what can't be compared says why, the time budget is said, and the line
  names an unexplained difference;
- the shadow's file goes when it closes.

    python tools/test_chem_shadow.py

Needs duckdb and pyyaml, as ChemDuck does.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import chem_fixture as fx                            # noqa: E402
from test_chemduck_parity import RECIPES, combos     # noqa: E402

from equis_loader import chem as ch                  # noqa: E402
from equis_loader import chem_parity as cp           # noqa: E402
from equis_loader import report_data as rd           # noqa: E402
from equis_loader.store import ReportStore           # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + str(detail)) if detail != "" else ""))
    if not condition:
        FAILURES.append(label)


def report_table():
    """The fixture as a report reaches the panel: every cell text, a blank
    for none."""
    def text(value):
        if value is None:
            return ""
        return repr(value) if isinstance(value, float) else str(value)
    return rd.ReportTable(fx.COLUMNS, [[text(row.get(c)) for c in fx.COLUMNS]
                                       for row in fx.rows()])


def points(chem, chemical, kwargs):
    try:
        return [{k: v for k, v in p.items() if k not in ("result_id", "x", "y")}
                for p in chem.points(chemical, **kwargs)]
    except Exception as exc:                   # noqa: BLE001 - compared as text
        return "refused: " + str(exc).splitlines()[0]


def main():
    folder = tempfile.mkdtemp(prefix="chem_shadow_")
    try:
        table = report_table()
        manifest = fx.write(os.path.join(folder, "facility"))

        # --- the live report's shadow, against ChemDuck from the parquet -----
        path = os.path.join(folder, "shadow.duckdb")
        shadow = ch.Chem.shadow(table, path, threads=1)
        check("the shadow is a file of its own", os.path.exists(path))
        published = ch.Chem.shadow(manifest, os.path.join(folder, "from_parquet.duckdb"),
                                   threads=1)
        parquet_store = ReportStore.from_manifest(os.path.dirname(manifest))
        differ = []
        for how, nd in RECIPES:
            for chemical, matrix, fraction in combos(parquet_store):
                if "_" in (matrix, fraction):
                    # The fixture's partition for a blank code is named '_',
                    # and read from the parquet the hive column stands in
                    # for the rows' blank (chem_fixture.write). The CSV has
                    # the blank itself, so '_' is no fraction of its.
                    continue
                kwargs = dict(how=how, non_detect=nd, matrix=(matrix,), fraction=(fraction,))
                a, b = points(shadow, chemical, kwargs), points(published, chemical, kwargs)
                if a != b:
                    differ.append("%s %s/%s %s %s" % (chemical, matrix, fraction, how, nd))
        check("ChemDuck from the report's CSV answers as ChemDuck from the parquet, "
              "every recipe", not differ, "; ".join(differ[:3]))
        published.close()
        parquet_store.close()

        # --- the live path's causes are the parity test's --------------------
        store = ReportStore.from_table(table)
        store.con.execute("SET threads = 1")
        expected = {loc: expect for loc, _what, _rows, expect in fx.CASES}
        wrong = []
        unexplained = []
        lines = []
        for how, nd in RECIPES:
            for chemical, matrix, fraction in combos(store):
                summary = store.aggregate(chemical, how=how, non_detect=nd,
                                          matrix=(matrix,), fraction=(fraction,))
                report = cp.shadow_map(store, shadow, chemical, how, nd, (matrix,),
                                       (fraction,), plugin=summary, budget=60)
                lines.append(cp.describe(report, store_seconds=0.01))
                comparison = cp.Comparison(store, shadow, chemical, matrix, fraction, how, nd,
                                           plugin=summary)
                for location in comparison.locations():
                    causes = comparison.explain(location)
                    if set(causes) != set(expected[location][how]):
                        wrong.append("%s %s %s: %s, declared %s" % (
                            location, how, nd, sorted(causes), sorted(expected[location][how])))
                tally = report["tally"]
                unexplained += [c for c in tally if c.startswith("unexplained")]
        check("on the live path every fixture location differs exactly as it declares",
              not wrong, "; ".join(wrong[:3]))
        check("... and no map's line counts an unexplained difference", not unexplained,
              unexplained[:3])
        sample = [line for line in lines if "Arsenic" in line and "WG" in line][:1]
        print("    " + (sample[0] if sample else lines[0]))
        refused = [line for line in lines if "ChemDuck refused" in line]
        check("a map ChemDuck refuses says so, and why the plugin differs",
              refused and all("units_refused" in line or "unit_contradicts_matrix" in line
                              for line in refused), refused[:1])

        # --- what can't be compared says why --------------------------------
        cases = [
            ("unaggregated", dict(how=rd.UNAGGREGATED, nd=rd.ND_HALF, matrix=("WG",))),
            ("excluded non-detects", dict(how=rd.MAXIMUM, nd=rd.ND_EXCLUDE, matrix=("WG",))),
            ("a method filter", dict(how=rd.MAXIMUM, nd=rd.ND_HALF, matrix=("WG",),
                                     method=("6010B",))),
            ("no matrix", dict(how=rd.MAXIMUM, nd=rd.ND_HALF, matrix=None)),
            ("two fractions", dict(how=rd.MAXIMUM, nd=rd.ND_HALF, matrix=("WG",),
                                   fraction=("T", "D"))),
        ]
        for label, kw in cases:
            report = cp.shadow_map(store, shadow, "Arsenic", kw["how"], kw["nd"],
                                   kw.get("matrix"), kw.get("fraction"), kw.get("method"))
            line = cp.describe(report)
            check("%s is not compared, and the line says so" % label,
                  "skipped" in report and "not compared" in line, line)

        # --- the budget, and an unexplained difference, are said -------------
        report = cp.shadow_map(store, shadow, "Arsenic", rd.MAXIMUM, rd.ND_HALF, ("WG",),
                               ("T",), budget=-1)
        counted = report["tally"].get("not explained (time budget)")
        check("past the budget the rest are counted as not explained, not as agreeing",
              counted == report["locations"] and not report["tally"].get("agree"),
              cp.describe(report))
        report["tally"]["unexplained: value 1 vs 2"] = 1
        report["examples"]["unexplained: value 1 vs 2"] = ["LOC-9"]
        line = cp.describe(report)
        check("an unexplained difference is named, with where, and shouted",
              "LOC-9" in line and "1 UNEXPLAINED" in line, line)

        # --- the row scope reaches the shadow --------------------------------
        shadow.set_row_scope(units=["mg/kg"])
        store.set_row_scope(units=["mg/kg"])
        report = cp.shadow_map(store, shadow, "Arsenic", rd.MAXIMUM, rd.ND_HALF, ("WG",), ("T",))
        check("a unit scope no Arsenic in WG is in leaves both sides empty",
              report["locations"] == 0, cp.describe(report))
        store.close()

        shadow.close()
        check("closing the shadow removes its file",
              not os.path.exists(path) and not os.path.exists(path + ".wal"))

        # --- a load on a worker swallows no one else's output ----------------
        # The panel loads the shadow on a worker thread. Silencing ChemDuck
        # by redirect_stdout swapped sys.stdout for the whole process, so
        # what the main thread printed meanwhile - QGIS's Python console -
        # vanished (2026-11-25).
        import io
        import threading
        captured = io.StringIO()
        real = sys.stdout
        sys.stdout = captured
        try:
            worker = threading.Thread(target=lambda: ch.Chem.shadow(
                table, os.path.join(folder, "threaded.duckdb"), threads=1).close())
            worker.start()
            sent = 0
            while worker.is_alive():
                print("main thread line %d" % sent)
                sent += 1
                worker.join(0.005)
        finally:
            sys.stdout = real
        text = captured.getvalue()
        got = text.count("main thread line")
        check("every line the main thread printed during a load arrives (%d)" % sent,
              sent > 0 and got == sent, "%d of %d" % (got, sent))
        check("... and none of ChemDuck's progress lines", "Loading" not in text
              and "---" not in text, text[:120])
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print()
    if FAILURES:
        print("FAILED: %d" % len(FAILURES))
        return 1
    print("The shadow is built, compared and logged as the parity test compares.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
