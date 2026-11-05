r"""Put ChemDuck through a plugin session, and say where each second went.

A harness for whoever is making ChemDuck fast enough for this plugin. It
opens ChemDuck the way the plugin does and asks it what a user's clicks ask,
in the order they ask it. The output is meant to be taken apart, not just
read: every install statement of the open is timed, and with --explain every
call leaves a DuckDB profile.

    python tools/profile_chemduck.py --manifest <facility>/manifest.json --out <dir>
    "C:\Program Files\QGIS 4.2.3\bin\python-qgis.bat" tools\profile_chemduck.py ^
        --manifest <facility>\manifest.json --out <dir>

Run it under BOTH interpreters and compare. That is the open question:
- **The open.** On 2026-11-05 (docs/chemduck-adoption-plan.md, open item 2)
  it took 1.7 s in plain Python 3.14.6 and 18.6 s in QGIS 4.2.3's Python
  3.12.15, both on DuckDB 1.5.5. It was 40.5 s in QGIS on 2026-10-29.
- **The lead, found by this script's first runs (2026-11-05).** Nearly all
  of the open is ONE statement: the first `INSERT OR IGNORE INTO
  chemduck_objects VALUES (?, ?)`, statement 5, right after the first
  sidecar CTAS. It took 17.9 of 18.9 s in QGIS and 1.09 of 2.08 s in
  plain Python. Every other registry insert took under 4 ms. A second
  open in the same process took 0.9 s, and the whole open used only
  4.5 s of CPU in QGIS, so it is a one-time wait, not work. It is the
  open's first statement with bound parameters. DuckDB's Python client
  imports numpy, pandas and pyarrow on its first parameter binding:
  `SELECT ?` alone took 0.84 s in plain Python and 1.93 s in QGIS, and
  imported all three. ChemDuck's own measurement put the rest of the
  17.9 s on a cold read of those packages' files, once per boot.
- **ChemDuck 2986b3f (branch open-no-bind, 2026-11-05)**: the open binds
  no parameter and writes its registry in two statements, 207 statements
  become 115. Through this script in QGIS: open 0.84-0.96 s, a second
  open 0.84-0.91 s. The import is NOT gone: the session's first bound
  parameter pays it, and the plugin binds parameters on every map
  (store.py's aggregate, chem.py's points). This script now names the step
  that paid it ("first bound parameter: paid by ..."); with warm file
  caches it was 2.17 s on the first click. Before 2026-11-05 the script
  itself bound parameters between the open and the session, which hid it.
  - Untested: whether real QGIS, with its usual plugins, has imported the
    three before this plugin's first click.
  - Not tried: the plugin importing them early, off the main thread.
- **The calls.** They take the same time under both interpreters. At 4
  threads they are within budget, except one location's series at
  0.21-0.22 s against 0.2. The plugin's own store answers each in
  0.02-0.07 s.

WHAT "HOW THE PLUGIN USES IT" MEANS HERE
- **One connection.** ChemDuck is opened into schema `chemduck` of the
  plugin's own ReportStore connection, through the adapter
  (`equis_loader/chem.py`, `Chem.open_published`), over the facility's
  partitions and the sidecars ChemDuck published. The plugin's views sit
  beside it in `main`. A ChemDuck opened alone in a fresh connection is
  not the plugin's case.
- **Threads.** `threads` is the whole database's setting. Decision 8 of
  the plan proposes 4, set once where the store opens. --threads 0 leaves
  DuckDB's default.
- **Map calls** go through `Chem.points`, the adapter's one question today.
  The others - point_counts (footer), point_series (one location; every
  location, for sparklines), km_quartiles (box plot), rollup_points,
  sample_cell_maps (scatter) - are the macros phases 2-4 of the plan put
  on a click. Here they are raw SQL on the adapter's cursor.
- **A session.** Every step is cold the first time, because a user picks a
  new analyte and the plugin asks it once. --repeat then runs each step
  again for warm medians. The first pass is reported separately, never
  averaged into the warm figures.
- **A reference beside each call.** The plugin's own store answers the
  same question, so a ratio is always at hand.

WHAT IT RECORDS (into --out)
- `summary.json`: the environment, every step's wall and CPU seconds (the
  process's, all threads), cold and warm, its budget, and the plugin's
  time for the same question.
- `open_statements.csv`: every statement the open ran, in order, with wall
  and CPU seconds, its source file and whether it failed. A failure is a
  statement deferred for a missing object and retried; run_install does
  that. The Python time spent outside statements is reported too.
- `environment` in summary.json: the interpreter, duckdb's version and
  file, the DuckDB settings an open could depend on (threads, memory,
  extension autoload and autoinstall, the extension directory), and which
  extensions were loaded before and after the open.
- `--cprofile`: `open.pstats`, Python's profile of the open.
- `--explain`: `profiles/<step>.json`, DuckDB's detailed profile of each
  ChemDuck step, from a separate pass after the timing so profiling never
  costs the timed runs. Planner and optimizer time is in it; on
  2026-10-29 that was about 0.2-0.3 s of CPU per call before a row was
  read.
- `--open-repeat N`: N more opens, each into a fresh store, for a
  distribution of open times rather than one.

ITERATING ON CHEMDUCK
--chemduck-src <checkout> imports ChemDuck from that checkout's `chemduck/`
package instead of the copy vendored at equis_loader/chemduck, so a change
there is measured here without re-vendoring. The checkout's publish is used
too: give it its own --pub, because sidecars written by one version may not
open in another.

The machine this was written on runs other jobs at 0-75% CPU, often within
a single run. Compare CPU seconds when wall times disagree, alternate
before/after builds rather than running one after the other, and record
the load (`--load` samples it, Windows only).

Imports no QGIS: it runs under any Python with duckdb and pyyaml.
"""
import argparse
import contextlib
import cProfile
import csv
import importlib.util
import io
import json
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

# Click budgets, raised with ChemDuck on 2026-10-29 (adoption plan, open
# item 2). None: no budget was set for that step.
BUDGET = {"open": 2.0, "catalogue": None, "map": 0.5, "rollup": 0.5,
          "footer": 0.2, "series_one": 0.2, "sparklines": 1.0,
          "box_plot": None, "scatter_pivot": None}
FRACTION_NAME = {"T": "Total", "D": "Dissolved"}
DEFAULT_CASES = "Arsenic:WS:T,Iron:WS:D,Arsenic:SE:N"
SCATTER_PAIR = "Manganese:Lead:WS:D"


def use_chemduck_checkout(src):
    """Make `equis_loader.chemduck` the checkout's package, before chem.py
    imports it. Relative imports inside the package then resolve there."""
    pkg = Path(src).resolve() / "chemduck"
    if not (pkg / "__init__.py").exists():
        raise SystemExit("No chemduck/ package in %s" % src)
    import equis_loader  # noqa: F401 - the parent, which imports no QGIS
    spec = importlib.util.spec_from_file_location(
        "equis_loader.chemduck", pkg / "__init__.py",
        submodule_search_locations=[str(pkg)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["equis_loader.chemduck"] = module
    spec.loader.exec_module(module)
    return module


def git_head(path):
    try:
        out = subprocess.run(["git", "-C", str(path), "log", "-1", "--format=%h %s"],
                             capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "-C", str(path), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return out.stdout.strip() + (" (+ uncommitted changes)" if dirty else "")
    except Exception:            # noqa: BLE001 - a missing git is not a failure here
        return None


class LoadSampler(object):
    """CPU load every few seconds while a run lasts (Windows, via CIM)."""

    def __init__(self, every=3.0):
        self.every, self.samples, self._stop = every, [], threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            try:
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "(Get-CimInstance Win32_Processor).LoadPercentage"],
                    capture_output=True, text=True, timeout=20).stdout.split()
                self.samples.append(int(out[0]))
            except Exception:    # noqa: BLE001
                pass
            self._stop.wait(self.every)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()


def clock(fn):
    """(wall s, cpu s, answer) of one call."""
    wall, cpu = time.perf_counter(), time.process_time()
    answer = fn()
    return time.perf_counter() - wall, time.process_time() - cpu, answer


class TimedCursor(object):
    """A DuckDB cursor that times every execute and keeps going.

    open_consumer installs through one cursor (load_data.install_target), so
    wrapping that cursor times each statement of the open, failed attempts
    included."""

    def __init__(self, real, log, source=None):
        self._real, self._log, self.source = real, log, source

    def execute(self, sql, *params):
        wall, cpu = time.perf_counter(), time.process_time()
        error = None
        try:
            return self._real.execute(sql, *params)
        except Exception as exc:
            error = "%s: %s" % (type(exc).__name__, str(exc).splitlines()[0][:160])
            raise
        finally:
            text = " ".join(str(sql).split())
            self._log.append({"n": len(self._log) + 1,
                              "wall_s": time.perf_counter() - wall,
                              "cpu_s": time.process_time() - cpu,
                              "failed": error is not None, "error": error or "",
                              "creates": _creates(text), "sql": text[:240]})

    def __getattr__(self, name):
        return getattr(self._real, name)


def _creates(text):
    words = text.split()
    upper = [w.upper() for w in words[:8]]
    if not upper:
        return ""
    if upper[0] != "CREATE":
        # "insert into chemduck_objects", "set global ...": the verb and
        # what it acts on, so a slow non-CREATE names itself.
        return " ".join(w.lower() for w in words[:3])
    for i, w in enumerate(upper):
        if w in ("TABLE", "VIEW", "MACRO", "FUNCTION", "SCHEMA") and i + 1 < len(words):
            rest = [x for x in words[i + 1:i + 4] if x.upper() not in ("IF", "NOT", "EXISTS")]
            return "%s %s" % (w.lower(), rest[0].split("(")[0] if rest else "")
    return "create"


def environment(con, chemduck):
    import duckdb
    names = ("threads", "memory_limit", "autoload_known_extensions",
             "autoinstall_known_extensions", "extension_directory",
             "parquet_metadata_cache", "temp_directory", "enable_external_access",
             "allow_unsigned_extensions", "home_directory")
    # Literals, not bound parameters: the first parameter bound on a
    # connection imports numpy, pandas and pyarrow (1-3 s, more cold), and
    # this ran between the open and the session, so it paid that for the
    # session and hid it from every step (2026-11-05).
    settings = dict(con.execute(
        "SELECT name, value FROM duckdb_settings() WHERE name IN (%s)"
        % ", ".join("'%s'" % n for n in names)).fetchall())
    src = Path(chemduck.__file__).resolve().parent
    return {
        "python": sys.version.split()[0], "executable": sys.executable,
        "platform": platform.platform(), "processor": platform.processor(),
        "cpus": os.cpu_count(),
        "duckdb": duckdb.__version__, "duckdb_file": getattr(duckdb, "__file__", None),
        "chemduck_version": chemduck.__version__, "chemduck_from": str(src),
        "chemduck_git": git_head(src),
        "vendored_commit": _vendored_commit(src),
        "plugin_git": git_head(ROOT),
        "settings": settings,
        "env": {k: v for k, v in os.environ.items()
                if k.upper().startswith(("DUCKDB", "QGIS", "PYTHON", "GDAL"))},
    }


def _vendored_commit(src):
    record = src / "VENDORED.json"
    if record.exists():
        return json.loads(record.read_text(encoding="utf-8")).get("commit")
    return None


def extensions(con):
    return [r[0] for r in con.execute(
        "SELECT extension_name FROM duckdb_extensions() WHERE loaded ORDER BY 1").fetchall()]


def open_like_the_plugin(facility, parts, pub, threads, statements=None, profile_to=None):
    """ReportStore over the facility, then ChemDuck in its schema through the
    adapter. Returns (store, chem, timings). `statements`, a list, collects
    each install statement if given."""
    from equis_loader import chem as chem_mod
    from equis_loader.chemduck import arii_hybrid, load_data
    from equis_loader.store import ReportStore

    timings = {}
    wall, cpu, store = clock(lambda: ReportStore.from_parquet(
        os.path.join(facility, "matrix=*", "fraction=*", "*.parquet")))
    timings["store_open"] = {"wall_s": wall, "cpu_s": cpu}
    if threads:
        # Before ChemDuck opens, as decision 8 has the store set it.
        store.con.execute("SET threads = %d" % threads)
    before = extensions(store.con)

    original = load_data.install_target
    if statements is not None:
        def timed_target(conn, schema):
            return TimedCursor(original(conn, schema), statements)
        load_data.install_target = arii_hybrid.install_target = timed_target
    profiler = cProfile.Profile() if profile_to else None
    try:
        if profiler:
            profiler.enable()
        wall, cpu, chem = clock(lambda: chem_mod.Chem.open_published(
            store.con, pub, parts))
    finally:
        if profiler:
            profiler.disable()
            profiler.dump_stats(str(profile_to))
        load_data.install_target = arii_hybrid.install_target = original
    timings["chemduck_open"] = {"wall_s": wall, "cpu_s": cpu}
    if statements is not None:
        inside = sum(s["wall_s"] for s in statements)
        timings["chemduck_open"]["in_statements_s"] = inside
        timings["chemduck_open"]["outside_statements_s"] = wall - inside
        timings["chemduck_open"]["statements"] = len(statements)
        timings["chemduck_open"]["failed_attempts"] = sum(s["failed"] for s in statements)
    timings["extensions_loaded_before"] = before
    timings["extensions_loaded_after"] = extensions(store.con)
    timings["threads"] = store.con.execute("SELECT current_setting('threads')").fetchone()[0]
    return store, chem, timings


def session_steps(store, chem, cases, scatter):
    """The clicks of one session, as (step name, kind, plugin fn, chemduck fn).

    `kind` keys BUDGET. Each case is one analyte in one matrix and fraction,
    as the panel asks for them."""
    from equis_loader import report_data as rd
    from equis_loader.pivot import Variable, build_pivot
    cur = chem.cur

    def sql(text, params):
        return lambda: cur.execute(text, params).fetchall()

    steps = []
    matrices = sorted({m for _, m, _ in cases})
    for matrix in matrices:
        steps.append(("catalogue %s" % matrix, "catalogue",
                      lambda m=matrix: store.analytes(matrix=(m,)),
                      sql("FROM analyte_summary WHERE matrix = ?", [matrix])))
    for chemical, matrix, fraction in cases:
        label = "%s %s/%s" % (chemical, matrix, fraction)
        fname = FRACTION_NAME.get(fraction, fraction)
        kw = dict(matrix=(matrix,), fraction=(fraction,))
        for how in (rd.MOST_RECENT, rd.MAXIMUM):
            steps.append(("map %s, %s" % (how, label), "map",
                          lambda c=chemical, h=how, k=kw: store.aggregate(c, how=h, **k),
                          lambda c=chemical, h=how, k=kw: chem.points(c, how=h, **k)))
        steps.append(("rollup max_over_points, %s" % label, "rollup", None,
                      sql("FROM rollup_points(?, 'max_over_points', p_matrix := ?, "
                          "p_fraction := ?)", [chemical, matrix, fname])))
        steps.append(("footer point_counts, %s" % label, "footer",
                      lambda c=chemical, k=kw: store.count_for(c, **k),
                      sql("FROM point_counts(?, p_matrix := ?, p_fraction := ?)",
                          [chemical, matrix, fname])))
        # Literals: a bound parameter here would pay the first bind's import
        # before the session, out of every step's sight (see run_side).
        row = store.con.execute(
            "SELECT location FROM v_results WHERE chemical = %s AND upper(matrix) = %s "
            "AND upper(fraction) = %s GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
            % tuple("'%s'" % str(v).replace("'", "''")
                    for v in (chemical, matrix, fraction))).fetchone()
        if row:
            where = row[0]
            steps.append(("series at %s (most rows), %s" % (where, label), "series_one",
                          lambda c=chemical, w=where, k=kw: store.series(w, c, **k),
                          sql("FROM point_series(?, p_locations := [?], p_matrix := ?, "
                              "p_fraction := ?)", [chemical, where, matrix, fname])))
        steps.append(("sparklines, every location, %s" % label, "sparklines",
                      lambda c=chemical, k=kw: store.series_many(None, c, **k),
                      sql("FROM point_series(?, p_matrix := ?, p_fraction := ?)",
                          [chemical, matrix, fname])))
        steps.append(("box plot by location, %s" % label, "box_plot", None,
                      sql("FROM km_quartiles([?], p_group := 'location', p_matrix := ?, "
                          "p_fraction := ?)", [chemical, matrix, fname])))
    if scatter:
        first, second, matrix, fraction = scatter
        steps.append(("scatter pivot %s x %s, %s/%s" % (first, second, matrix, fraction),
                      "scatter_pivot",
                      lambda: build_pivot(store, [Variable(first, fraction=fraction),
                                                  Variable(second, fraction=fraction)],
                                          matrix=(matrix,)).rows,
                      sql("FROM sample_cell_maps([?, ?], p_matrix := ?, p_fraction := ?)",
                          [first, second, matrix, FRACTION_NAME.get(fraction, fraction)])))
    return steps


BIND_IMPORTS = ("numpy", "pandas", "pyarrow")


def bind_imports_loaded():
    """Which of the packages DuckDB's Python client imports on the first
    bound parameter this process has loaded so far."""
    return [m for m in BIND_IMPORTS if m in sys.modules]


def run_side(fn):
    if fn is None:
        return None
    before = set(bind_imports_loaded())
    try:
        wall, cpu, answer = clock(fn)
        rows = len(answer) if hasattr(answer, "__len__") else None
        out = {"wall_s": wall, "cpu_s": cpu, "rows": rows}
    except Exception as exc:     # noqa: BLE001 - recorded beside the others
        out = {"error": "%s: %s" % (type(exc).__name__, str(exc).splitlines()[0][:200])}
    # The step that paid the first bind's import, named rather than left
    # inside its cold time: since ChemDuck 2986b3f the open binds nothing,
    # so a session's first click pays it.
    imported = [m for m in bind_imports_loaded() if m not in before]
    if imported:
        out["imported"] = imported
    return out


def profile_step(chem, name, fn, folder):
    """One more run of a ChemDuck step with DuckDB's detailed profiler on.

    Profiling is a client setting, so it is set on the adapter's cursor,
    which is the connection every ChemDuck call goes through."""
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() else "_" for ch in name)[:90]
    target = folder / ("%s.json" % safe)
    cur = chem.cur
    # In this order: DuckDB checks the output file's extension against the
    # profiling type already set.
    cur.execute("SET enable_profiling = 'json'")
    cur.execute("SET profiling_mode = 'detailed'")
    cur.execute("SET profiling_output = '%s'" % str(target).replace("\\", "/").replace("'", "''"))
    try:
        fn()
    finally:
        cur.execute("SET enable_profiling = 'no_output'")
        cur.execute("RESET profiling_output")
    return target.name


def parse_cases(text):
    cases = []
    for item in filter(None, (t.strip() for t in text.split(","))):
        chemical, matrix, fraction = item.split(":")
        cases.append((chemical, matrix.upper(), fraction.upper()))
    return cases


def top_cases(store, n):
    """The n most-measured (analyte, matrix, fraction) cells of the facility."""
    return [(c, m.upper(), f.upper()) for c, m, f, _ in store.con.execute(
        "SELECT chemical, matrix, fraction, count(*) FROM v_results "
        "WHERE coalesce(chemical, '') <> '' GROUP BY ALL ORDER BY 4 DESC LIMIT ?",
        [n]).fetchall()]


def fmt(side):
    if side is None:
        return "-"
    if "error" in side:
        return "ERROR"
    return "%.3f/%.3f" % (side["wall_s"], side["cpu_s"])


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--manifest", required=True, help="the facility's manifest.json")
    parser.add_argument("--out", required=True, help="folder for what this run records")
    parser.add_argument("--pub", help="ChemDuck's published sidecars (default <out>/pub; "
                                      "published there if absent, timed apart from the open)")
    parser.add_argument("--chemduck-src", help="a ChemDuck checkout to import instead of "
                                               "the vendored copy")
    parser.add_argument("--threads", type=int, default=4,
                        help="SET threads before ChemDuck opens (decision 8: 4); "
                             "0 leaves DuckDB's default")
    parser.add_argument("--cases", default=DEFAULT_CASES,
                        help="chemical:matrix:fraction,... (default %(default)s)")
    parser.add_argument("--top", type=int, default=0,
                        help="also the N most-measured analyte/matrix/fraction cells")
    parser.add_argument("--scatter", default=SCATTER_PAIR,
                        help="first:second:matrix:fraction, or '' for none")
    parser.add_argument("--repeat", type=int, default=3,
                        help="warm runs of each step after the cold pass")
    parser.add_argument("--open-repeat", type=int, default=0,
                        help="further opens, each into a fresh store")
    parser.add_argument("--open-only", action="store_true", help="time the open; skip the session")
    parser.add_argument("--explain", action="store_true",
                        help="a DuckDB profile of each ChemDuck step, after the timing")
    parser.add_argument("--cprofile", action="store_true", help="Python's profile of the open")
    parser.add_argument("--load", action="store_true", help="sample CPU load (Windows)")
    args = parser.parse_args()

    chemduck = (use_chemduck_checkout(args.chemduck_src) if args.chemduck_src
                else importlib.import_module("equis_loader.chemduck"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pub = Path(args.pub) if args.pub else out / "pub"
    facility = os.path.dirname(os.path.abspath(args.manifest))
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    parts = [os.path.join(facility, p["path"]) for p in manifest["partitions"]]

    report = {"date": time.strftime("%Y-%m-%d %H:%M"), "manifest": os.path.abspath(args.manifest),
              "rows": manifest.get("rows"), "args": vars(args), "budgets_s": BUDGET}
    sampler = LoadSampler() if args.load else contextlib.nullcontext()
    with sampler:
        if not (pub / "sidecars.json").exists():
            print("publishing ChemDuck's sidecars to %s ..." % pub)
            def publish():
                with contextlib.redirect_stdout(io.StringIO()):
                    return chemduck.publish(args.manifest, str(pub), x_col="LONGITUDE",
                                            y_col="LATITUDE", quiet=True)
            wall, cpu, _ = clock(publish)
            report["publish"] = {"wall_s": wall, "cpu_s": cpu, "to": str(pub)}

        statements = []
        store, chem, opened = open_like_the_plugin(
            facility, parts, str(pub), args.threads, statements,
            out / "open.pstats" if args.cprofile else None)
        opened["bind_imports_loaded_after_open"] = bind_imports_loaded()
        report["environment"] = environment(store.con, chemduck)
        report["open"] = opened
        with open(out / "open_statements.csv", "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(statements[0]) if statements else ["n"])
            writer.writeheader()
            writer.writerows(statements)
        slowest = sorted(statements, key=lambda s: -s["wall_s"])[:15]
        report["open_slowest_statements"] = slowest

        reopen = []
        for _ in range(args.open_repeat):
            again, again_chem, t = open_like_the_plugin(facility, parts, str(pub), args.threads)
            reopen.append(t["chemduck_open"]["wall_s"])
            again.close()
        if reopen:
            report["open_repeat_wall_s"] = reopen

        print("\n%s, Python %s, DuckDB %s, ChemDuck %s (%s), threads %s"
              % (platform.platform(), report["environment"]["python"],
                 report["environment"]["duckdb"], chemduck.__version__,
                 report["environment"]["chemduck_git"] or report["environment"]["vendored_commit"],
                 opened["threads"]))
        o = opened["chemduck_open"]
        print("open: store %.2f s; ChemDuck %.2f s wall, %.2f s CPU: %.2f s in %d statements "
              "(%d failed attempts), %.2f s outside them%s"
              % (opened["store_open"]["wall_s"], o["wall_s"], o["cpu_s"], o["in_statements_s"],
                 o["statements"], o["failed_attempts"], o["outside_statements_s"],
                 ("; again: %s" % ", ".join("%.2f" % w for w in reopen)) if reopen else ""))
        for s in slowest[:8]:
            print("    %7.3f s  %s%s" % (s["wall_s"], s["creates"] or s["sql"][:60],
                                       "  [failed, retried]" if s["failed"] else ""))
        print("extensions loaded: before %s, after %s"
              % (opened["extensions_loaded_before"], opened["extensions_loaded_after"]))
        print("loaded by the open, of %s: %s"
              % ("/".join(BIND_IMPORTS), opened["bind_imports_loaded_after_open"] or "none"))

        if not args.open_only:
            cases = parse_cases(args.cases) + (top_cases(store, args.top) if args.top else [])
            scatter = tuple(args.scatter.split(":")) if args.scatter else None
            steps = session_steps(store, chem, cases, scatter)
            results = []
            print("\n%-62s %-7s %-13s %-13s %-13s %s" % (
                "step (wall/cpu s)", "budget", "cold", "warm", "plugin warm", "rows"))
            # Cold: the session once, in click order.
            for name, kind, plugin_fn, chem_fn in steps:
                results.append({"step": name, "kind": kind, "budget_s": BUDGET.get(kind),
                                "chemduck_cold": run_side(chem_fn), "plugin_cold": run_side(plugin_fn),
                                "chemduck_warm": [], "plugin_warm": []})
            # Warm: the whole session again, --repeat times, so each step's
            # repeats are spread over the run rather than back to back.
            for _ in range(args.repeat):
                for entry, (_, _, plugin_fn, chem_fn) in zip(results, steps):
                    entry["chemduck_warm"].append(run_side(chem_fn))
                    entry["plugin_warm"].append(run_side(plugin_fn))
            for entry in results:
                for side in ("chemduck", "plugin"):
                    runs = [r for r in entry.pop(side + "_warm") if r and "error" not in r]
                    entry[side + "_warm"] = ({
                        "wall_s": statistics.median(r["wall_s"] for r in runs),
                        "cpu_s": statistics.median(r["cpu_s"] for r in runs),
                        "runs": len(runs), "rows": runs[0]["rows"]} if runs else None)
                warm, budget = entry["chemduck_warm"], entry["budget_s"]
                entry["within_budget"] = (None if not warm or budget is None
                                          else warm["wall_s"] <= budget)
                cold = entry["chemduck_cold"] or {}
                print("%-62s %-7s %-13s %-13s %-13s %s%s" % (
                    entry["step"][:62], "-" if budget is None else "%.1f" % budget,
                    fmt(entry["chemduck_cold"]), fmt(warm), fmt(entry["plugin_warm"]),
                    (warm or {}).get("rows", "-"),
                    ("  " + cold["error"]) if "error" in cold else
                    ("  OVER" if entry["within_budget"] is False else "")))
            report["steps"] = results
            for entry in results:
                for side in ("chemduck", "plugin"):
                    paid = (entry[side + "_cold"] or {}).get("imported")
                    if paid:
                        print("first bound parameter: paid by the %s side of %r, cold %s "
                              "(imported %s)" % (side, entry["step"],
                                                fmt(entry[side + "_cold"]), ", ".join(paid)))

            if args.explain:
                folder = out / "profiles"
                for entry, (name, _, _, chem_fn) in zip(results, steps):
                    if chem_fn is not None and "error" not in (entry["chemduck_cold"] or {}):
                        entry["profile"] = profile_step(chem, name, chem_fn, folder)
                print("\nDuckDB profiles in %s" % folder)
        store.close()
    if args.load:
        report["cpu_load_percent"] = sampler.samples
        print("CPU load during the run: %s" % sampler.samples)
    with open(out / "summary.json", "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1, default=str)
    print("\nwrote %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
