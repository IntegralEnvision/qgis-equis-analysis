"""What each ChemDuck call the plugin would make costs, beside today's answer.

Phase 1 of docs/chemduck-adoption-plan.md: before the panel's questions move
to ChemDuck's macros, time each one on a real facility against the plugin's
own answer to the same question, in the Python the plugin runs in. ChemDuck's
own numbers were seconds where the plugin answers a click in tens of
milliseconds; this says, call by call, whether that holds here.

    "C:\\Program Files\\QGIS 4.2.3\\bin\\python-qgis.bat" tools\\measure_chemduck_calls.py
        --manifest <facility>/manifest.json --pub <dir> [--out <dir>/calls.json]
        [--threads N] [--skip-load]

`--pub` is where ChemDuck's sidecars are published (built there if absent,
and that time reported). Each call is run once cold, then three times; the
median of the three is reported beside the cold time, never averaged with it.
`--threads` sets DuckDB's `threads` after ChemDuck is open. It is the whole
database's setting, not a connection's (ChemDuck's docs/duckdb-thread-cap.md),
so it governs the plugin's calls and ChemDuck's alike: run once with it and
once without to see both sides move. `--skip-load` leaves out the two
report-loading calls, which take minutes and run in the background anyway.
The row counts say each pair answered the same KIND of question; whether the
answers agree is the parity test's business, not this one's.
"""
import argparse
import contextlib
import datetime
import io
import json
import os
import platform
import statistics
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from equis_loader import chemduck as cd                # noqa: E402
from equis_loader import report_data as rd             # noqa: E402
from equis_loader.pivot import Variable, build_pivot   # noqa: E402
from equis_loader.store import ReportStore             # noqa: E402

# ChemDuck names fractions; the plugin codes them.
FRACTION = {"T": "Total", "D": "Dissolved", "N": "N"}


def timed(fn, repeat=3):
    """(cold seconds, median of `repeat` warm seconds, rows of the answer)."""
    began = time.perf_counter()
    answer = fn()
    cold = time.perf_counter() - began
    warm = []
    for _ in range(repeat):
        began = time.perf_counter()
        fn()
        warm.append(time.perf_counter() - began)
    rows = len(answer) if hasattr(answer, "__len__") else answer
    return round(cold, 3), round(statistics.median(warm), 3), rows


def quiet(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--pub", required=True, help="ChemDuck's sidecar folder")
    parser.add_argument("--out")
    parser.add_argument("--threads", type=int,
                        help="SET threads for the whole database (default: DuckDB's)")
    parser.add_argument("--skip-load", action="store_true",
                        help="leave out loading a report's rows")
    args = parser.parse_args()

    facility = os.path.dirname(os.path.abspath(args.manifest))
    with open(args.manifest, encoding="utf-8") as handle:
        manifest = json.load(handle)
    parts = [os.path.join(facility, p["path"]) for p in manifest["partitions"]]
    import duckdb
    report = {"date": datetime.date.today().isoformat(),
              "machine": "%s, %s, Python %s, DuckDB %s"
                         % (platform.platform(), platform.processor() or platform.machine(),
                            platform.python_version(), duckdb.__version__),
              "chemduck": "%s (vendored)" % cd.__version__,
              "rows": manifest.get("rows"), "setup": {}, "calls": []}

    if not os.path.exists(os.path.join(args.pub, "sidecars.json")):
        began = time.perf_counter()
        quiet(cd.publish, args.manifest, args.pub, x_col="LONGITUDE", y_col="LATITUDE",
              quiet=True)
        report["setup"]["publish_s"] = round(time.perf_counter() - began, 1)

    began = time.perf_counter()
    store = ReportStore.from_parquet(os.path.join(facility, "matrix=*", "fraction=*",
                                                  "*.parquet"))
    report["setup"]["plugin_store_open_s"] = round(time.perf_counter() - began, 2)
    began = time.perf_counter()
    quiet(cd.open_consumer, store.con, args.pub, parts, schema="chemduck")
    report["setup"]["chemduck_open_s"] = round(time.perf_counter() - began, 2)
    if args.threads:
        store.con.execute("SET threads = %d" % args.threads)
    report["setup"]["threads"] = store.con.execute(
        "SELECT current_setting('threads')").fetchone()[0]
    cur = cd.schema_cursor(store.con, "chemduck")

    def sql(text, params=None):
        return lambda: cur.execute(text, params or []).fetchall()

    def record(question, plugin, chemduck, note=""):
        entry = {"question": question, "note": note}
        for side, fn in (("plugin", plugin), ("chemduck", chemduck)):
            if fn is None:
                entry[side] = None
                continue
            try:
                cold, warm, rows = timed(fn)
                entry[side] = {"cold_s": cold, "warm_s": warm, "rows": rows}
            except Exception as exc:          # noqa: BLE001 - recorded, not hidden
                entry[side] = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
        report["calls"].append(entry)
        show = lambda e: ("-" if e is None else e.get("error") or
                          "cold %.3f  warm %.3f  rows %s" % (e["cold_s"], e["warm_s"], e["rows"]))
        print("%s\n    plugin   %s\n    chemduck %s" % (question, show(entry["plugin"]),
                                                       show(entry["chemduck"])))

    cases = [("Arsenic", "WS", "T"), ("Iron", "WS", "D"), ("Arsenic", "SE", "N")]
    for chemical, matrix, fraction in cases:
        label = "%s %s/%s" % (chemical, matrix, fraction)
        for how, agg in ((rd.MOST_RECENT, "most_recent"), (rd.MAXIMUM, "maximum")):
            record("map: one value per location, %s, %s" % (agg, label),
                   lambda: store.aggregate(chemical, how=how, matrix=(matrix,),
                                           fraction=(fraction,)),
                   sql("FROM aggregate_points(?, p_event_agg := ?, p_matrix := ?, "
                       "p_fraction := ?)", [chemical, agg, matrix, FRACTION[fraction]]))
        record("map, rolled up to one per location (max_over_points), %s" % label, None,
               sql("FROM rollup_points(?, 'max_over_points', p_matrix := ?, "
                   "p_fraction := ?)", [chemical, matrix, FRACTION[fraction]]))
        record("footer: locations and results that would map, %s" % label,
               lambda: [store.count_for(chemical, matrix=(matrix,), fraction=(fraction,))],
               sql("SELECT count(DISTINCT location_id), sum(n_events) FROM "
                   "aggregate_points(?, p_matrix := ?, p_fraction := ?)",
                   [chemical, matrix, FRACTION[fraction]]),
               "ChemDuck has no count; this is a map call, counted")

        busiest = store.con.execute(
            "SELECT location FROM v_results WHERE chemical = ? AND upper(matrix) = ? "
            "AND upper(fraction) = ? GROUP BY 1 ORDER BY count(*) DESC LIMIT 1",
            [chemical, matrix, fraction]).fetchone()[0]
        record("time series: one location (%s rows there), %s" % ("the most", label),
               lambda: store.series(busiest, chemical, matrix=(matrix,), fraction=(fraction,)),
               sql("FROM point_series(?, p_locations := [?], p_matrix := ?, p_fraction := ?)",
                   [chemical, busiest, matrix, FRACTION[fraction]]))
        record("sparklines: every location's series, %s" % label,
               lambda: store.series_many(None, chemical, matrix=(matrix,),
                                         fraction=(fraction,)),
               sql("FROM point_series(?, p_matrix := ?, p_fraction := ?)",
                   [chemical, matrix, FRACTION[fraction]]))
        record("box plot by location: Kaplan-Meier quartiles, %s" % label, None,
               sql("FROM km_quartiles([?], p_group := 'location', p_matrix := ?, "
                   "p_fraction := ?)", [chemical, matrix, FRACTION[fraction]]),
               "the plugin computes these on the page, over the pivot")

    # The published sidecar, not the view: v_analyte_summary is the live
    # view over every result (5.4 s for WS on Facility A, 2026-10-29) and the
    # sidecar `analyte_summary` is that view written out at publish time.
    for matrix in ("WS", "SE"):
        record("catalogue: analytes in %s" % matrix,
               lambda: store.analytes(matrix=(matrix,)),
               sql("FROM analyte_summary WHERE matrix = ?", [matrix]))

    pair = [("Manganese", "D"), ("Lead", "D")]
    record("pivot: two analytes per sample (a scatter), WS dissolved",
           lambda: build_pivot(store, [Variable(c, fraction=f) for c, f in pair],
                               matrix=("WS",)).rows,
           sql("SELECT count(*) FROM sample_cell_maps([?, ?], p_matrix := 'WS', "
               "p_fraction := 'Dissolved')", [c for c, _ in pair]))
    # Every (analyte, fraction) in tailings: one variable each on both sides,
    # since ChemDuck keeps fractions apart and the plugin refuses to mix them.
    pairs = store.con.execute(
        "SELECT DISTINCT chemical, upper(fraction) FROM v_results "
        "WHERE upper(matrix) = 'TAILS' AND coalesce(chemical, '') <> ''").fetchall()
    every = sorted({c for c, _ in pairs})
    record("pivot: every analyte per sample, TAILS (%d analyte-fractions)" % len(pairs),
           lambda: build_pivot(store, [Variable(c, fraction=f) for c, f in pairs],
                               matrix=("TAILS",), on_mixed="skip").rows,
           sql("SELECT count(*) FROM sample_cell_maps(?, p_matrix := 'TAILS')", [every]))

    # Loading report rows: the live path ingests a fetched report into a
    # store; ChemDuck's load_source would load the same rows into its schema.
    for part in () if args.skip_load else ("matrix=TAILS/fraction=T", "matrix=WS/fraction=D"):
        path = os.path.join(facility, *part.split("/"), "part-0000.parquet").replace("\\", "/")
        reader = duckdb.connect()
        headers = [d[0] for d in reader.sql("FROM read_parquet('%s') LIMIT 0" % path).description]
        rows = reader.sql("SELECT %s FROM read_parquet('%s')" % (
            ", ".join('CAST("%s" AS VARCHAR)' % h for h in headers), path)).fetchall()
        table = rd.ReportTable(headers, [["" if v is None else v for v in r] for r in rows])
        folder = tempfile.mkdtemp(prefix="calls_")

        def plugin_load():
            target = os.path.join(folder, "s%d.duckdb" % time.perf_counter_ns())
            ReportStore.from_table(table, path=target).close()
            return [len(rows)]

        def chemduck_load():
            con = duckdb.connect()
            relation = con.sql("FROM read_parquet('%s')" % path)
            done = quiet(cd.load_source, con, relation,
                         cd.load_mapping(cd.DEFAULT_MAPPING), schema="chemduck")
            con.close()
            return [done["total_rows"]]

        record("load a report's rows (%s, %s rows)" % (part, format(len(rows), ",")),
               plugin_load, chemduck_load,
               "plugin: ReportStore.from_table to a file, as the live path does; "
               "ChemDuck: load_source from a relation, into a fresh connection")
        reader.close()

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=1)
        print("wrote", args.out)
    print(json.dumps(report["setup"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
