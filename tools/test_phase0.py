"""Phase 0: does the bundled substrate actually work here?

docs/vision-linked-views.md sequences this first, before anything is built on
it, because it is the least interesting part and the most able to stop
everything. Three questions:

  1. Do duckdb and arro3 import from the plugin's own bundle - not from some
     other copy that happens to be installed?
  2. Does the Arrow IPC round-trip work? That is the wire format between
     Python and the web view, so a substrate that cannot serialize is no use.
  3. Is it fast enough to make US-11's promise ("every post-fetch interaction
     under 100 ms on a 460,000-row report") true rather than hopeful?

Runs anywhere - a plain interpreter or the QGIS Python console:

    python tools/test_phase0.py
    python tools/test_phase0.py --require-bundle
    exec(open(r"C:\\path\\to\\equis_qgis\\tools\\test_phase0.py").read())

TWO SUBSTRATES, as the plugin has. With a bundle for this interpreter
(tools/vendor_deps.py - the Windows release zip), question 1 is asked as
written: duckdb and arro3 must import from it. Without one, the plugin does
not fail: store.duckdb_module() imports the host's duckdb, and
bridge_payload.IPC_WRITERS serializes with whichever Arrow runtime is there.
A conda-forge/pixi QGIS on Linux supplies its own duckdb (2.0.0 on the pixi
QGIS 4.2.3 build, Python 3.13, 2026-11-21), so a cp313 bundle is neither built
nor wanted, and this test used to report that working install as BLOCKED.
It now checks the host path instead, and says the bundle checks did not run
rather than counting them as passed. --require-bundle restores the old rule,
for checking a bundled zip.
"""

import os
import platform
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in dir() \
    else os.getcwd()
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

RESULTS = []
TIMINGS = {}


def record(label, ok, detail=""):
    RESULTS.append((label, ok, detail))
    mark = "PASS" if ok else ("INFO" if ok is None else "FAIL")
    print("[%s] %s%s" % (mark, label, (" - " + detail) if detail else ""))


def _build_hint():
    print("")
    print("  Build it:  python tools/vendor_deps.py --target local")
    print("  For a different QGIS:  --target win|mac|linux --python 312")


def main(require_bundle=False):
    from equis_loader import vendor

    env = vendor.describe_environment()
    print("")
    print("Phase 0: bundled substrate")
    print("  Python %s (%s) on %s %s"
          % (env["python"], env["python_tag"], env["platform"], env["machine"]))
    print("  wants bundle: %s" % env["bundle"])
    print("  bundles present: %s"
          % (", ".join(vendor.available_bundles()) or "none"))
    print("")

    state = vendor.check()
    bundled = state["bundle_present"]
    if bundled:
        record("bundle present for this interpreter", True,
               state["bundle_path"])
        for name, info in state["modules"].items():
            if not info["ok"]:
                record("import %s" % name, False, info["error"])
                continue
            # A system-wide copy answering instead of ours would make the
            # bundle look like it worked when it had done nothing.
            record("import %s" % name, True,
                   "%s%s" % (info["version"], "" if info["from_bundle"]
                             else "  (NOT from bundle!)"))
            if not info["from_bundle"]:
                record("%s came from the bundle" % name, False, info["path"])
        if not state["ok"]:
            return 1
    elif require_bundle:
        record("bundle present for this interpreter", False,
               state["bundle_path"])
        _build_hint()
        return 1
    else:
        # INFO, not PASS: nothing about a bundle was verified.
        record("bundle present for this interpreter", None,
               "none - checking the host's own duckdb, as the plugin does")
        duck = state["modules"]["duckdb"]
        if not duck["ok"]:
            record("import duckdb from the host", False, duck["error"])
            _build_hint()
            return 1
        record("import duckdb from the host", True,
               "%s  %s" % (duck["version"], duck["path"]))

    # Snapshot before any of our own Arrow work. The claim being protected is
    # "OUR path does not need pyarrow", not "pyarrow is absent from this
    # process" - QGIS ships and imports it for its own reasons, and that costs
    # the bundle nothing. Asserting the global condition reported a healthy
    # Windows install as BLOCKED.
    pyarrow_before = "pyarrow" in sys.modules

    import duckdb

    # --- 2. does it compute, and does it serialize ----------------------
    con = duckdb.connect()
    record("duckdb connect + query", True,
           str(con.execute("select 42").fetchone()[0]))

    # --- 3. a report-shaped table, at the size the README quotes ---------
    rows = 460000
    print("")
    print("  Building a %s-row report-shaped table..." % format(rows, ",d"))
    start = time.time()
    # No %-formatting on this string: it is full of SQL modulo operators,
    # and mixing the two turns a valid query into a format error.
    con.execute("""
        CREATE TABLE results AS
        SELECT
            'LOC-' || lpad(((i * 7919) % 5300)::VARCHAR, 5, '0') AS location,
            ['Benzene','Toluene','Nickel','Arsenic','PFOA','Lead'][
                (i % 6) + 1] AS chemical,
            DATE '2007-01-01' + (((i * 13) % 6800)::INTEGER) AS sample_date,
            CASE WHEN (i % 7) = 0 THEN NULL
                 ELSE round((random() * 100)::DECIMAL(10,3), 3) END AS value,
            CASE WHEN (i % 7) = 0 THEN 'N' ELSE 'Y' END AS detect_flag,
            round((random() * 2)::DECIMAL(10,3), 3) AS detect_limit,
            ['WG','SO','SE'][(i % 3) + 1] AS matrix
        FROM range(""" + str(rows) + """) t(i)
    """)
    TIMINGS["build"] = time.time() - start
    count = con.execute("select count(*) from results").fetchone()[0]
    record("build %s rows" % format(rows, ",d"), count == rows,
           "%.2f s" % TIMINGS["build"])

    # The non-detect rule the plugin lives by, as a view rather than a loop.
    # This is R2 in miniature: report_data's judgement becomes SQL.
    con.execute("""
        CREATE VIEW v_results AS
        SELECT *,
            COALESCE(value, detect_limit / 2) AS plot_value,
            (value IS NULL) AS at_limit
        FROM results
    """)

    def timed(label, sql, expect=None, budget=0.1):
        con.execute(sql).fetchall()                   # warm
        start = time.time()
        out = con.execute(sql).fetchall()
        elapsed = time.time() - start
        TIMINGS[label] = elapsed
        ok = elapsed < budget if expect is None else (out == expect)
        record("%s (<%.0f ms)" % (label, budget * 1000), ok,
               "%.1f ms" % (elapsed * 1000))
        return out

    print("")
    print("  Post-fetch operations, warm, against %s rows:"
          % format(rows, ",d"))
    timed("distinct chemicals",
          "SELECT chemical, count(*) FROM v_results GROUP BY 1")
    # Arithmetic binning rather than width_bucket, which is not in every
    # DuckDB build - and which is what a Mosaic bin mark generates anyway.
    timed("histogram, 40 bins",
          """SELECT floor(plot_value / 2.5)::INTEGER AS bin, count(*)
             FROM v_results WHERE chemical = 'Nickel' GROUP BY 1 ORDER BY 1""")
    timed("brush: filter a value range",
          """SELECT count(DISTINCT location) FROM v_results
             WHERE chemical = 'Nickel' AND plot_value BETWEEN 20 AND 60""")
    timed("aggregate: max per location",
          """SELECT location, max(plot_value) FROM v_results
             WHERE chemical = 'Nickel' GROUP BY 1""")
    timed("aggregate: most recent per location",
          """SELECT DISTINCT ON (location) location, plot_value
             FROM v_results WHERE chemical = 'Nickel'
             ORDER BY location, sample_date DESC""")
    timed("one location's time series",
          """SELECT sample_date, plot_value, at_limit FROM v_results
             WHERE chemical = 'Nickel' AND location = 'LOC-00042'
             ORDER BY sample_date""")

    # --- Arrow IPC, the wire format the bridge needs ---------------------
    print("")
    import io

    def nickel():
        return con.sql("""SELECT location, plot_value FROM v_results
                          WHERE chemical = 'Nickel' LIMIT 20000""")

    if bundled:
        # The bundle's own route. NOT DuckDBPyRelation.arrow(), which imports
        # pyarrow - roughly 40 MB plus numpy, and a second Arrow runtime
        # beside the one arro3 already carries. DuckDB exposes
        # __arrow_c_stream__, so the Arrow PyCapsule interface hands the data
        # to arro3 with no pyarrow anywhere. This is the reason boro depends
        # on arro3-io rather than pyarrow, and it is worth a test of its own
        # because .arrow() is the obvious call and quietly costs the win.
        from arro3.core import Table
        from arro3.io import write_ipc
        relation = nickel()
        record("duckdb exposes __arrow_c_stream__",
               hasattr(relation, "__arrow_c_stream__"))
        table = Table.from_arrow(relation)
        start = time.time()
        buffer = io.BytesIO()
        write_ipc(table, buffer)
        TIMINGS["ipc"] = time.time() - start
        record("Arrow IPC via the bundle's arro3, 20,000 rows",
               len(buffer.getvalue()) > 0,
               "%s bytes in %.1f ms" % (format(len(buffer.getvalue()), ",d"),
                                        TIMINGS["ipc"] * 1000))
        # Snapshot NOW, before the plugin's writers below: they prefer
        # pyarrow when the host has it, by design, and judging the arro3
        # route after them failed a clean bundle (2026-11-21).
        pyarrow_after_arro3 = "pyarrow" in sys.modules

    # The route the plugin actually takes, whatever the substrate: the first
    # of bridge_payload's writers whose runtime is present. Walked here
    # rather than through to_arrow_ipc() so the verdict can name which one
    # answered; IPC_WRITERS is exposed for exactly that.
    from equis_loader import bridge_payload
    payload, used = None, None
    for writer in bridge_payload.IPC_WRITERS:
        payload = writer(nickel())
        if payload is not None:
            used = writer.__name__.replace("_ipc_with_", "")
            break
    if payload:
        # Timed warm, on a second call: the first paid for importing the
        # runtime (pyarrow's first import was most of 79 ms, pixi QGIS
        # 4.2.3, 2026-11-21), which is a once-per-session cost, not a
        # per-payload one.
        start = time.time()
        payload = writer(nickel())
        elapsed = time.time() - start
        TIMINGS.setdefault("ipc", elapsed)
        record("Arrow IPC via the plugin's writers, 20,000 rows", True,
               "%s: %s bytes in %.1f ms" % (used, format(len(payload), ",d"),
                                            elapsed * 1000))
        import base64
        encoded = base64.b64encode(payload).decode()
        record("base64 for the QWebChannel hop", len(encoded) > 0,
               "%s chars (+%.0f%%)"
               % (format(len(encoded), ",d"),
                  100.0 * (len(encoded) - len(payload)) / len(payload)))
    else:
        record("Arrow IPC via the plugin's writers, 20,000 rows", False,
               "no Arrow runtime: install pyarrow, or arro3-core and "
               "arro3-io")
        # One cause, one failure: with no payload there is nothing to encode.
        print("[NOT RUN] base64 for the QWebChannel hop - no payload")

    total = None
    if bundled:
        # --- what it costs on disk ---------------------------------------
        total = 0
        for base, _dirs, files in os.walk(state["bundle_path"]):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(base, name))
                except OSError:
                    pass
        record("bundle size on disk", None, "%.1f MB" % (total / 1048576.0))

        # The real size claim: pyarrow is not something we have to ship.
        shipped = set(os.listdir(state["bundle_path"]))
        unwanted = sorted(n for n in shipped
                          if n.split("-")[0].lower() in ("pyarrow", "numpy"))
        record("bundle ships neither pyarrow nor numpy", not unwanted,
               ", ".join(unwanted) if unwanted
               else "~40 MB and a second Arrow runtime avoided")

        # And the behavioural half: the arro3 route did not drag it in.
        if pyarrow_before:
            record("the arro3 route did not import pyarrow", True,
                   "pyarrow was already loaded by the host (QGIS ships it); "
                   "not ours, and not in the bundle")
        else:
            record("the arro3 route did not import pyarrow",
                   not pyarrow_after_arro3,
                   "imported during serialization" if pyarrow_after_arro3
                   else "PyCapsule route used throughout")
    else:
        print("[NOT RUN] bundle size, bundle ships no pyarrow, arro3 route "
              "imports no pyarrow - there is no bundle to check")

    # --- verdict ----------------------------------------------------------
    fails = [r for r in RESULTS if r[1] is False]
    slow = [k for k, v in TIMINGS.items()
            if k not in ("build", "ipc") and v >= 0.1]
    print("")
    print("=" * 64)
    print("PHASE 0 VERDICT")
    print("=" * 64)
    if fails:
        print("  BLOCKED. %d check(s) failed:" % len(fails))
        for label, _ok, detail in fails:
            print("    %s - %s" % (label, detail))
    elif slow:
        print("  WORKS, BUT SLOW. These missed the 100 ms budget: %s"
              % ", ".join(slow))
        print("  US-11's promise would need qualifying.")
    elif bundled:
        print("  CLEAR. The substrate imports from the plugin's own bundle,")
        print("  computes, serializes to Arrow IPC, and answers every")
        print("  post-fetch operation well inside 100 ms at report scale.")
    else:
        print("  CLEAR ON THE HOST'S DUCKDB. No bundle here; the host's")
        print("  duckdb computes, the plugin's writers serialize to Arrow IPC")
        print("  (%s), and every post-fetch operation is inside 100 ms." % used)
        print("  The bundle checks did not run - not a pass for a bundle.")
    print("")
    print("  Paste this line back:")
    print("    %s | Python %s | %s | duckdb %s | %s | arrow %s | "
          "slowest %.1f ms | ipc %.1f ms"
          % (platform.system(), env["python"],
             env["bundle"] if bundled else "host (no bundle)",
             state["modules"]["duckdb"].get("version", "?"),
             "bundle %.0f MB" % (total / 1048576.0) if bundled
             else "bundle n/a", used,
             1000 * max([v for k, v in TIMINGS.items()
                         if k not in ("build", "ipc")] or [0]),
             1000 * TIMINGS.get("ipc", 0)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(require_bundle="--require-bundle" in sys.argv[1:]))
else:
    main()
