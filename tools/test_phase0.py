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
    exec(open(r"C:\\path\\to\\equis_qgis\\tools\\test_phase0.py").read())

Build the bundle first with tools/vendor_deps.py.
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


def main():
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
    record("bundle present for this interpreter", state["bundle_present"],
           state["bundle_path"])
    if not state["bundle_present"]:
        print("")
        print("  Build it:  python tools/vendor_deps.py --target local")
        print("  For a different QGIS:  --target win|mac|linux --python 312")
        return 1

    for name, info in state["modules"].items():
        if not info["ok"]:
            record("import %s" % name, False, info["error"])
            continue
        # A system-wide copy answering instead of ours would make the bundle
        # look like it worked when it had done nothing.
        record("import %s" % name, True,
               "%s%s" % (info["version"],
                         "" if info["from_bundle"] else "  (NOT from bundle!)"))
        if not info["from_bundle"]:
            record("%s came from the bundle" % name, False, info["path"])

    if not state["ok"]:
        return 1

    # Snapshot before any of our own Arrow work. The claim being protected is
    # "OUR path does not need pyarrow", not "pyarrow is absent from this
    # process" - QGIS ships and imports it for its own reasons, and that costs
    # the bundle nothing. Asserting the global condition reported a healthy
    # Windows install as BLOCKED.
    pyarrow_before = "pyarrow" in sys.modules

    import duckdb
    from arro3.core import Table
    from arro3.io import write_ipc

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
    # NOT DuckDBPyRelation.arrow(), which imports pyarrow - roughly 40 MB plus
    # numpy, and a second Arrow runtime beside the one arro3 already carries.
    # DuckDB exposes __arrow_c_stream__, so the Arrow PyCapsule interface
    # hands the data to arro3 with no pyarrow anywhere. This is the reason
    # boro depends on arro3-io rather than pyarrow, and it is worth a test of
    # its own because .arrow() is the obvious call and quietly costs the win.
    relation = con.sql("""SELECT location, plot_value FROM v_results
                          WHERE chemical = 'Nickel' LIMIT 20000""")
    record("duckdb exposes __arrow_c_stream__",
           hasattr(relation, "__arrow_c_stream__"))
    table = Table.from_arrow(relation)
    start = time.time()
    buffer = io.BytesIO()
    write_ipc(table, buffer)
    payload = buffer.getvalue()
    TIMINGS["ipc"] = time.time() - start
    record("Arrow IPC serialize 20,000 rows", len(payload) > 0,
           "%s bytes in %.1f ms" % (format(len(payload), ",d"),
                                    TIMINGS["ipc"] * 1000))

    import base64
    encoded = base64.b64encode(payload).decode()
    record("base64 for the QWebChannel hop", len(encoded) > 0,
           "%s chars (+%.0f%%)"
           % (format(len(encoded), ",d"),
              100.0 * (len(encoded) - len(payload)) / max(len(payload), 1)))

    # --- what it costs on disk -------------------------------------------
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

    # And the behavioural half: we did not drag it in ourselves.
    pyarrow_after = "pyarrow" in sys.modules
    if pyarrow_before:
        record("our Arrow path did not import pyarrow", True,
               "pyarrow was already loaded by the host (QGIS ships it); "
               "not ours, and not in the bundle")
    else:
        record("our Arrow path did not import pyarrow", not pyarrow_after,
               "imported during serialization" if pyarrow_after
               else "PyCapsule route used throughout")

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
    else:
        print("  CLEAR. The substrate imports from the plugin's own bundle,")
        print("  computes, serializes to Arrow IPC, and answers every")
        print("  post-fetch operation well inside 100 ms at report scale.")
    print("")
    print("  Paste this line back:")
    print("    %s | Python %s | %s | duckdb %s | bundle %.0f MB | "
          "slowest %.1f ms | ipc %.1f ms"
          % (platform.system(), env["python"], env["bundle"],
             state["modules"]["duckdb"].get("version", "?"),
             total / 1048576.0,
             1000 * max([v for k, v in TIMINGS.items()
                         if k not in ("build", "ipc")] or [0]),
             1000 * TIMINGS.get("ipc", 0)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
else:
    main()
