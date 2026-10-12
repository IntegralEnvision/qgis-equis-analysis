"""Does a store survive being built on one thread and used on another?

The panel cannot build a store on the UI thread - 7.35 s at report scale -
so a background task builds it. A DuckDB connection should not be shared
between threads, so the task writes a FILE, closes it, and the main thread
opens it again. That handoff is the part of the wiring that can actually go
wrong, and it is testable without QGIS, a task or a GUI: `build_store_file`
and `ReportStore.open` are plain functions.

What is checked:

  * a store built into a file, closed, and reopened answers every question
    identically to the ReportTable it came from;
  * `present` and `headers` survive, which they cannot be recovered without -
    a report with no matrix column and one where every matrix cell is blank
    look identical once loaded, and must behave differently, because a
    filter on a missing column matches everything;
  * the build really does run somewhere else: it is driven from a worker
    thread here, as the real one will be.

    python tools\\test_store_handoff.py
"""

import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from equis_loader import report_data as rd          # noqa: E402
from equis_loader import store as st                # noqa: E402

import importlib.util                               # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


def _parity_fixtures():
    """Reuse the parity suite's fixtures rather than inventing weaker ones."""
    spec = importlib.util.spec_from_file_location(
        "parity", os.path.join(HERE, "test_store_parity.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ask_everything(source, analytes, locations):
    """Every question, as one comparable structure."""
    out = {
        "analytes": source.analytes(),
        "matrices": source.matrices(),
        "mixed_fractions": source.mixed_fractions(),
        "locations": source.locations(),
        "analytes_wg": source.analytes(matrix="WG"),
        "methods": source.methods(),
    }
    for chemical, cas in analytes:
        out["methods:%s" % chemical] = source.methods(chemical, cas)
        out["fractions:%s" % chemical] = source.fractions_for(chemical, cas)
        for how in (rd.MOST_RECENT, rd.MAXIMUM):
            for nd in (rd.ND_HALF, rd.ND_FULL, rd.ND_EXCLUDE):
                out["agg:%s:%s:%s" % (chemical, how, nd)] = source.aggregate(
                    chemical, cas, how=how, non_detect=nd)
        for cap in (0, 3, 10):
            out["rows:%s:%d" % (chemical, cap)] = source.rows_for(
                chemical, cas, limit=cap)
        for location in locations:
            out["series:%s:%s" % (chemical, location)] = source.series(
                location, chemical, cas)
    return out


def build_on_a_thread(table, path, with_raw):
    """Run the build somewhere other than here, as the real task will."""
    result = {}

    def work():
        try:
            result["path"] = st.build_store_file(table, path,
                                                 with_raw=with_raw)
        except Exception as exc:                    # noqa: BLE001
            result["error"] = "%s: %s" % (type(exc).__name__, exc)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(120)
    check("build finished on the worker thread", not thread.is_alive())
    check("build raised nothing", "error" not in result,
          result.get("error", ""))
    return result.get("path")


def main():
    try:
        st.duckdb_module()
    except ImportError as exc:
        print("duckdb is not importable: %s" % exc)
        return 1

    parity = _parity_fixtures()
    rows = parity.awkward_rows()
    scratch = tempfile.mkdtemp(prefix="equis_handoff_")
    print("")
    print("Worker-thread build, main-thread open")
    print("")

    try:
        for label, headers, data in (
                ("full", parity.FULL_HEADERS, rows),
                ("lean", ["SYS_LOC_CODE", "CHEMICAL_NAME", "SAMPLE_DATE",
                          "RESULT_NUMERIC", "DETECT_FLAG"],
                 [[r[0], r[1], r[3], r[4], r[8]] for r in rows])):
            table = rd.ReportTable(headers, data)
            analytes = [(a["chemical"], a["cas_rn"])
                        for a in table.analytes()][:3]
            locations = table.locations()[:4]

            path = os.path.join(scratch, "%s.duckdb" % label)
            built = build_on_a_thread(table, path, with_raw=True)
            check("%s: file written" % label,
                  bool(built) and os.path.exists(path),
                  "%.1f KB" % (os.path.getsize(path) / 1024.0)
                  if built and os.path.exists(path) else "missing")
            if not built:
                continue

            store = st.ReportStore.open(path)
            check("%s: headers survived" % label,
                  store.headers == list(headers),
                  "%r" % (store.headers,))
            expected_present = {k for k in rd.COLUMNS if table.column(k) >= 0}
            check("%s: present-columns survived" % label,
                  store.present == expected_present,
                  "missing %r" % (expected_present - store.present,))
            check("%s: raw columns available" % label, store.has_raw)

            want = ask_everything(table, analytes, locations)
            got = ask_everything(store, analytes, locations)
            differing = [k for k in want if want[k] != got[k]]
            check("%s: reopened store answers %d questions identically"
                  % (label, len(want)), not differing,
                  "" if not differing else
                  "\n      differs: %s\n      python: %r\n      sql   : %r"
                  % (differing[0], want[differing[0]], got[differing[0]]))

            check("%s: export matches after reopen" % label,
                  table.filtered_rows() == store.filtered_rows())
            store.close()

            # Reopening twice must work: the panel may rebuild a layer long
            # after the build, and nothing should depend on being first.
            again = st.ReportStore.open(path)
            check("%s: opens a second time" % label,
                  again.analytes() == want["analytes"])
            again.close()

        # A store built WITHOUT raw must say so rather than exporting wrong
        # columns - the default, since raw doubles ingest.
        table = rd.ReportTable(parity.FULL_HEADERS, rows)
        lean_path = os.path.join(scratch, "noraw.duckdb")
        st.build_store_file(table, lean_path, with_raw=False)
        noraw = st.ReportStore.open(lean_path)
        check("without raw: has_raw is false", not noraw.has_raw)
        try:
            noraw.filtered_rows()
            check("without raw: export refuses", False, "it returned rows")
        except st.StoreError:
            check("without raw: export refuses with a clear error", True)
        noraw.close()

        # A file that is not a store must be refused, not half-read.
        junk = os.path.join(scratch, "junk.duckdb")
        with open(junk, "wb") as handle:
            handle.write(b"not a database")
        try:
            st.ReportStore.open(junk)
            check("a non-store file is refused", False, "it opened")
        except Exception:
            check("a non-store file is refused", True)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    print("")
    if FAILURES:
        print("%d failure(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("A store survives the worker-thread handoff intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
