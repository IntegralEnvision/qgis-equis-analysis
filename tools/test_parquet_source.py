"""A parquet source must never answer with rows counted twice.

The bug this exists for: `data/facility_a/**/*.parquet` matched the 10 repacked
partitions AND the 36 per-slice files they were built from, and the store
answered 6,904,292 rows for a facility holding 3,452,146. Nothing raised.
Counts, aggregates and histograms were all exactly twice what they should
have been, which is the failure mode a test has to catch, because a person
never will - a plausible number looks like an answer.

    python tools/test_parquet_source.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import equis_loader.report_data as rd  # noqa: E402
from equis_loader.store import ReportStore, StoreError  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


def write_rows(path, rows):
    """A minimal results parquet at `path`, via DuckDB so pyarrow is optional."""
    from equis_loader.store import duckdb_module
    con = duckdb_module().connect(":memory:")
    values = ", ".join(
        "('%s', '%s', %s, '%s', 'Y', 'mg/L')" % r for r in rows)
    con.execute(
        "COPY (SELECT * FROM (VALUES %s) AS t(SYS_LOC_CODE, CHEMICAL_NAME, "
        "RESULT_NUMERIC, SAMPLE_DATE, DETECT_FLAG, RESULT_UNIT)) TO '%s' "
        "(FORMAT PARQUET)" % (values, path.replace("\\", "/")))
    con.close()


root = tempfile.mkdtemp(prefix="equis_parquet_")
try:
    part = os.path.join(root, "matrix=WS", "fraction=N")
    os.makedirs(part)
    rows = [("MW-1", "Arsenic", 1.5, "4/24/2025 12:37:00 PM"),
            ("MW-2", "Arsenic", 2.5, "4/24/2025 12:38:00 PM"),
            ("MW-3", "Lead", 3.5, "4/25/2025 9:00:00 AM")]
    write_rows(os.path.join(part, "data.parquet"), rows)

    named = os.path.join(root, "matrix=*", "fraction=*", "*.parquet")
    sweep = os.path.join(root, "**", "*.parquet")

    # --- the partitions alone, with no intermediate anywhere -----------------
    store = ReportStore.from_parquet(named)
    total = store.con.execute("SELECT count(*) FROM results").fetchone()[0]
    check("a clean partition set counts each row once", total == 3, str(total))
    hive = store.con.execute(
        "SELECT DISTINCT matrix, fraction FROM read_parquet('%s', "
        "hive_partitioning=1, union_by_name=true)"
        % named.replace("\\", "/")).fetchall()
    check("the path's matrix/fraction become columns",
          hive == [("WS", "N")], str(hive))
    dates = store.con.execute(
        "SELECT count(sample_date) FROM results").fetchone()[0]
    check("EQuIS's own date format parses rather than raising",
          dates == 3, str(dates))
    store.close()

    # A sweeping glob is fine while there IS no intermediate to reach.
    store = ReportStore.from_parquet(sweep)
    total = store.con.execute("SELECT count(*) FROM results").fetchone()[0]
    check("a sweep with nothing to double is still served", total == 3,
          str(total))
    store.close()

    # --- now leave staging behind, as a died-mid-repack run would ------------
    # Staging carries its OWN matrix=/fraction= subdirectories, because
    # collect() writes to partition_path(staging, matrix, fraction). This
    # detail is the whole reason the guard has to exist: with staging laid
    # out FLAT, DuckDB raises "Hive partition mismatch" by itself and the bug
    # is impossible. Laid out as collect() actually writes it, the layouts
    # agree, DuckDB objects to nothing, and the rows are silently doubled -
    # which is what happened to the real facility.
    staging = os.path.join(root, "_staging", "matrix=WS", "fraction=N")
    os.makedirs(staging)
    shutil.copyfile(os.path.join(part, "data.parquet"),
                    os.path.join(staging, "slice_00.parquet"))

    # Asserted on the ROW COUNT, not merely on "something raised": a store
    # that failed to read for an unrelated reason would satisfy a bare
    # `except StoreError` while leaving the real bug in place. Disarming the
    # guard must make THIS check fail, and it does.
    try:
        doomed = ReportStore.from_parquet(sweep)
        got = doomed.con.execute("SELECT count(*) FROM results").fetchone()[0]
        doomed.close()
        check("a sweep that would double rows is refused", False,
              "served %d rows (%s) instead of raising"
              % (got, "DOUBLED" if got == 6 else "unexpected count"))
    except StoreError as exc:
        check("a sweep that would double rows is refused", True)
        check("and the refusal names the double-counting",
              "twice" in str(exc), str(exc)[:90])
        check("and the refusal says what to do instead",
              "matrix=*" in str(exc), str(exc)[:90])

    # The named glob cannot reach staging, so it keeps working throughout.
    store = ReportStore.from_parquet(named)
    total = store.con.execute("SELECT count(*) FROM results").fetchone()[0]
    check("the named glob is unaffected by leftover staging", total == 3,
          str(total))
    store.close()

    # --- a filter on a column the parquet lacks matches everything -----------
    # The domain rule in CLAUDE.md: dropping every row is a worse failure than
    # ignoring a filter the data cannot express. The parity gate proves this
    # for a store built from a ReportTable, but from_parquet computes `present`
    # from the FILE's columns by a different route, so the rule needs its own
    # case here. A slice parquet legitimately lacks columns - the bare extract
    # below has no matrix, fraction or method at all.
    # Outside `root`: a differing schema inside the swept tree would break the
    # sweep's own read and mask what the doubling checks above are testing.
    bare_dir = tempfile.mkdtemp(prefix="equis_bare_")
    bare = os.path.join(bare_dir, "bare.parquet")
    write_rows(bare, [("MW-1", "Arsenic", 1.5, "4/24/2025 12:37:00 PM"),
                      ("MW-2", "Arsenic", 2.5, "4/24/2025 12:38:00 PM")])
    store = ReportStore.from_parquet(bare, hive=False)
    check("a column the file lacks is absent from `present`",
          "fraction" not in store.present and "matrix" not in store.present,
          str(sorted(store.present)))
    baseline = len(store.aggregate("Arsenic"))
    for label, kw in (("fraction", {"fraction": "D"}),
                      ("matrix", {"matrix": "WS"}),
                      ("method", {"method": "SW6020"}),
                      ("all three", {"fraction": "D", "matrix": "WS",
                                     "method": "SW6020"})):
        got = len(store.aggregate("Arsenic", **kw))
        check("filtering on a missing %s keeps every row" % label,
              got == baseline == 2, "%d of %d" % (got, baseline))
    store.close()

    # --- a parquet without DETECT_FLAG is refused, not valued ----------------
    # Missing OPTIONAL columns are ignored, as above. A missing DETECT_FLAG is
    # not optional: without it every result reads as a non-detect and is
    # halved under 1/2 DL. A parquet source never passes through a
    # ReportTable, so from_parquet must refuse it itself.
    from equis_loader.store import duckdb_module
    no_flag = os.path.join(bare_dir, "no_flag.parquet")
    con = duckdb_module().connect(":memory:")
    con.execute("COPY (SELECT * FROM (VALUES ('MW-1', 'Arsenic', 10.0, "
                "'4/24/2025 12:37:00 PM', 'ug/L')) AS t(SYS_LOC_CODE, "
                "CHEMICAL_NAME, RESULT_NUMERIC, SAMPLE_DATE, RESULT_UNIT)) "
                "TO '%s' (FORMAT PARQUET)" % no_flag.replace("\\", "/"))
    con.close()
    try:
        ReportStore.from_parquet(no_flag, hive=False)
        refused = None
    except StoreError as exc:
        refused = str(exc)
    check("a parquet without DETECT_FLAG is refused, naming it",
          refused is not None and "DETECT_FLAG" in refused, str(refused))
    shutil.rmtree(bare_dir, ignore_errors=True)

    # --- a single FLAT parquet, with no hive layout at all -------------------
    # The pipeline produces matrix=/fraction= partitions, but nothing stops
    # someone pointing this at one file a colleague sent them. Asked
    # 2026-09-28; it works, and these pin the reasons.
    flat_dir = tempfile.mkdtemp(prefix="equis_flat_")
    try:
        flat = os.path.join(flat_dir, "everything.parquet")
        write_rows(flat, [("MW-1", "Arsenic", 1.5, "4/24/2025 12:37:00 PM"),
                          ("MW-2", "Arsenic", 2.5, "4/24/2025 12:38:00 PM"),
                          ("MW-3", "Lead", 3.5, "4/25/2025 9:00:00 AM")])

        store = ReportStore.from_parquet(flat)
        check("one flat file opens", store.count() == 3, str(store.count()))
        store.close()

        # hive_partitioning defaults to ON. A path with no `key=value` level
        # must not make that an error - DuckDB simply finds no partition
        # columns, and the caller should not have to know to turn it off.
        store = ReportStore.from_parquet(flat, hive=True)
        check("hive=True on a non-hive path is harmless",
              store.count() == 3, str(store.count()))
        store.close()

        # A glob matching flat files in one directory, with no levels below.
        write_rows(os.path.join(flat_dir, "more.parquet"),
                   [("MW-4", "Arsenic", 4.5, "4/26/2025 9:00:00 AM")])
        store = ReportStore.from_parquet(
            os.path.join(flat_dir, "*.parquet"))
        check("a glob over flat files in one directory works",
              store.count() == 4, str(store.count()))

        # THE RULE THAT MATTERS. A flat extract may carry no MATRIX_CODE or
        # FRACTION at all - on a partitioned set those can live only in the
        # directory name. Filtering on a column the data lacks must match
        # EVERYTHING, never drop every row (CLAUDE.md).
        check("matrix is absent from this fixture",
              "matrix" not in store.present, str(sorted(store.present)))
        everything = len(store.aggregate("Arsenic", how=rd.MAXIMUM))
        filtered = len(store.aggregate("Arsenic", how=rd.MAXIMUM,
                                       matrix=["WS"], fraction=["D"]))
        check("filtering on absent matrix/fraction keeps every row",
              filtered == everything and everything > 0,
              "%d of %d" % (filtered, everything))
        store.close()
    finally:
        shutil.rmtree(flat_dir, ignore_errors=True)

    # --- a file: URL, in every shape someone might type ----------------------
    # The panel's URL box invites a pasted Windows path after `file://`.
    # Only `file:///C:/dir` is strictly correct; with two slashes urlparse
    # reads `C:` - or the WHOLE backslashed path - as a HOSTNAME and leaves
    # `path` empty, so the manifest resolved to `\manifest.json` and
    # failed with "The system cannot find the file specified". Reported
    # from the panel 2026-09-28.
    url_dir = tempfile.mkdtemp(prefix="equis_url_")
    try:
        write_rows(os.path.join(url_dir, "part.parquet"),
                   [("MW-1", "Arsenic", 1.5, "4/24/2025 12:37:00 PM")])
        with open(os.path.join(url_dir, "manifest.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"version": 1, "facility": "t", "rows": 1,
                       "partitions": [{"path": "part.parquet", "rows": 1}]},
                      handle)

        forms = [
            ("two slashes, backslashes", "file://" + url_dir),
            ("three slashes, backslashes", "file:///" + url_dir),
            ("two slashes, forward", "file://" + url_dir.replace("\\", "/")),
            ("three slashes, forward", "file:///" + url_dir.replace("\\", "/")),
            ("a plain path", url_dir),
        ]
        for label, source in forms:
            try:
                store = ReportStore.from_manifest(source)
                ok = store.count() == 1
                store.close()
            except Exception as exc:            # noqa: BLE001
                ok = False
                label = "%s (%s)" % (label, type(exc).__name__)
            check("from_manifest accepts %s" % label, ok)

        # _local_path is what makes that work; a non-file source must pass
        # through untouched or an https:// manifest would break.
        check("an https URL is not treated as a path",
              ReportStore._local_path("https://host/dir") is None)
        check("a plain path is not treated as a URL",
              ReportStore._local_path(url_dir) is None)
        check("a percent-escape in a file URL is decoded",
              ReportStore._local_path("file:///C:/tmp/a%20b").endswith("a b"),
              ReportStore._local_path("file:///C:/tmp/a%20b"))
    finally:
        shutil.rmtree(url_dir, ignore_errors=True)

    # --- a signed URL survives being joined ----------------------------------
    # An Azure SAS URL carries its credential in the QUERY STRING. Appending
    # a path to the end puts it after the signature, Azure reads a different
    # resource path than the one that was signed, and answers 403
    # AuthenticationFailed - indistinguishable from a bad token, and it cost
    # an afternoon of suspecting a perfectly good one.
    sas = ("https://acct.blob.core.windows.net/container"
           "?sp=r&se=2027-05-14T04:48:05Z&sig=kuJW%3D")
    joined = ReportStore._join(sas, "matrix=WS/fraction=N/part-0000.parquet")
    check("a path joins BEFORE the query string",
          joined.split("?")[0].endswith(
              "/container/matrix=WS/fraction=N/part-0000.parquet"),
          joined.split("?")[0][-60:])
    check("the signature crosses over untouched",
          joined.split("?", 1)[1] == sas.split("?", 1)[1],
          joined.split("?", 1)[1])
    check("a plain URL still joins normally",
          ReportStore._join("https://h/d", "manifest.json")
          == "https://h/d/manifest.json")
    check("a trailing slash does not double",
          ReportStore._join("https://h/d/", "manifest.json")
          == "https://h/d/manifest.json")

    # --- row_id: arrival order, computed without a window --------------------
    # row_id breaks aggregate()'s ties and orders the export, so it must
    # order rows exactly as row_number() OVER () did - file-list order, then
    # row order within a file. It must ALSO not be a window: one over the
    # whole scan blocks every filter pushdown, which made the first query on
    # a store opened from Azure download the whole facility (31-60 s,
    # measured 2026-09-30, against 1.6 s for the same read without it).
    order_dir = tempfile.mkdtemp(prefix="equis_order_")
    try:
        for matrix, chems in (("SE", ("Zinc", "Arsenic")),
                              ("WS", ("Lead", "Copper", "Arsenic"))):
            where = os.path.join(order_dir, "matrix=%s" % matrix, "fraction=T")
            os.makedirs(where)
            write_rows(os.path.join(where, "data.parquet"),
                       [("L%d" % i, c, float(i), "4/24/2025 12:00:00 PM")
                        for i, c in enumerate(chems)])
        se = os.path.join(order_dir, "matrix=SE", "fraction=T", "data.parquet")
        ws = os.path.join(order_dir, "matrix=WS", "fraction=T", "data.parquet")
        # The list deliberately NOT in alphabetical order: list order is the
        # arrival order, and sorting it would be a different answer.
        for label, source in (
                ("a glob", os.path.join(order_dir, "matrix=*", "fraction=*",
                                        "*.parquet")),
                ("an explicit list", [ws, se])):
            store = ReportStore.from_parquet(source)
            got = [r[0] for r in store.con.execute(
                "SELECT chemical FROM results ORDER BY row_id").fetchall()]
            literal = ("'%s'" % source.replace("\\", "/")
                       if isinstance(source, str) else
                       "[%s]" % ", ".join("'%s'" % s.replace("\\", "/")
                                          for s in source))
            expected = [r[0] for r in store.con.execute(
                "SELECT CHEMICAL_NAME FROM (SELECT row_number() OVER () AS n, "
                "CHEMICAL_NAME FROM read_parquet(%s, hive_partitioning=1, "
                "union_by_name=true)) ORDER BY n" % literal).fetchall()]
            check("row_id keeps arrival order for %s" % label,
                  got == expected, "%s vs %s" % (got, expected))
            unique = store.con.execute(
                "SELECT count(DISTINCT row_id) = count(*) FROM results"
            ).fetchone()[0]
            check("row_id is unique across files for %s" % label, unique)
            joined = store.con.execute(
                "SELECT count(*) FROM v_results JOIN raw USING (row_id)"
            ).fetchone()[0]
            check("the export's join still pairs every row for %s" % label,
                  joined == 5, str(joined))
            plan = " ".join(str(r) for r in store.con.execute(
                "EXPLAIN SELECT count(*) FROM v_results WHERE chemical = "
                "'Arsenic'").fetchall()).upper()
            check("no window sits between a filter and the scan (%s)" % label,
                  "WINDOW" not in plan,
                  "WINDOW in plan" if "WINDOW" in plan else "")
            store.close()
    finally:
        shutil.rmtree(order_dir, ignore_errors=True)

    # --- a file rewritten under an open store is noticed ---------------------
    # The store re-reads its files on every query, so a rewrite changes the
    # answers under a panel whose lists describe the old file. Found
    # 2026-10-01 when a generator rewrote arii.parquet under an open session.
    moving_dir = tempfile.mkdtemp(prefix="equis_moving_")
    try:
        moving = os.path.join(moving_dir, "arii.parquet")
        write_rows(moving, [("MW-1", "Arsenic", 1.0, "4/24/2025 12:00:00 PM")])
        store = ReportStore.from_parquet(moving, hive=False)
        check("a freshly opened store reports nothing changed",
              store.changed_files() == [], str(store.changed_files()))
        write_rows(moving, [("MW-1", "Arsenic", 1.0, "4/24/2025 12:00:00 PM"),
                            ("MW-2", "Lead", 2.0, "4/24/2025 12:00:00 PM")])
        changed = store.changed_files()
        check("a rewritten file is reported by name",
              len(changed) == 1 and changed[0].endswith("arii.parquet"),
              str(changed))
        store.close()
    finally:
        shutil.rmtree(moving_dir, ignore_errors=True)

    # --- reading staging ON PURPOSE is not an error --------------------------
    # Every matched file is an intermediate, so nothing is double-counted.
    store = ReportStore.from_parquet(
        os.path.join(root, "_staging", "matrix=*", "fraction=*", "*.parquet"))
    total = store.con.execute("SELECT count(*) FROM results").fetchone()[0]
    check("reading only staging is allowed", total == 3, str(total))
    store.close()
finally:
    shutil.rmtree(root, ignore_errors=True)

print()
if FAILURES:
    print("FAILED: %s" % ", ".join(FAILURES))
    raise SystemExit(1)
print("A parquet source answers with every row counted exactly once.")
