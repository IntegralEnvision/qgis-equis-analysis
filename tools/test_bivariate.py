"""Bivariate classes: Jenks breaks, classes, keys and colours (US-24 map).

    python tools/test_bivariate.py

Plain interpreter. The last section runs on the real Facility A parquet when it
is present and checks the breaks measured on 2026-10-09.
"""
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from equis_loader import bivariate as bv  # noqa: E402

FAILED = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - %s" % (detail,)) if detail != "" else ""))
    if not condition:
        FAILED.append(label)


def main():
    print("-- Jenks")
    clusters = [1, 2, 3, 10, 11, 12, 100, 101, 102]
    got = bv._jenks(sorted(clusters), 3)
    check("three clear clusters split between them", got == [3, 12], got)
    check("numpy and pure Python agree",
          bv._jenks_pure(sorted(clusters), 3) == got, bv._jenks_pure(sorted(clusters), 3))
    rng = random.Random(7)
    noisy = sorted(rng.lognormvariate(2, 1.5) for _ in range(400))
    check("numpy and pure Python agree on noisy data",
          [round(b, 9) for b in bv._jenks(noisy, 3)]
          == [round(b, 9) for b in bv._jenks_pure(noisy, 3)])

    print("\n-- the scale the breaks are found on")
    out = bv.natural_breaks([1, 2, 3, 10, 11, 12, 100, 101, 102])
    check("all positive: log10, breaks are the DATA values themselves",
          out["scale"] == "log10" and out["breaks"] == [3, 12], out)
    out = bv.natural_breaks([0, 1, 2, 10, 11, 12, 100, 101, 102])
    check("a zero forces linear (log cannot place it)", out["scale"] == "linear", out)
    check("too few distinct values: no breaks, not invented ones",
          bv.natural_breaks([5, 5, 7]) is None)
    big = [float(i) for i in range(1, 20001)]
    out = bv.natural_breaks(big, max_sample=3000)
    check("large inputs are sampled down, deterministically",
          out["used"] == 3000 and out["n"] == 20000
          and out == bv.natural_breaks(big, max_sample=3000), (out["used"], out["n"]))
    check("the sample keeps both ends",
          bv._even_sample(big, 3000)[0] == 1.0 and bv._even_sample(big, 3000)[-1] == 20000.0)

    print("\n-- classes and keys")
    b = [3.0, 12.0]
    check("a break is inside its class (<=)",
          (bv.class_of(3.0, b), bv.class_of(3.0001, b), bv.class_of(12, b),
           bv.class_of(13, b)) == (1, 2, 2, 3))
    check("both detected: an x-y key", bv.key_for(2, 50, True, True, b, b) == "1-3")
    check("any non-detect: its detection class, not a colour class",
          [bv.key_for(2, 2, xd, yd, b, b) for xd, yd in
           ((True, False), (False, True), (False, False))]
          == ["x_only", "y_only", "neither"])
    check("non-detect classes are grey", all(bv.colour_for(k) == bv.GREY for k in
                                             ("x_only", "y_only", "neither")))
    check("nine keys, nine distinct colours",
          sorted(bv.PALETTE) == sorted("%d-%d" % (x, y) for x in (1, 2, 3)
                                       for y in (1, 2, 3))
          and len(set(bv.PALETTE.values())) == 9)
    check("grey is not one of the nine", bv.GREY not in bv.PALETTE.values())

    stations_fixture()
    modes_fixture()
    facility_a()
    print("\n%s" % ("ALL BIVARIATE TESTS PASSED" if not FAILED
                    else "%d FAILED" % len(FAILED)))
    return 1 if FAILED else 0


def stations_fixture():
    print("\n-- a station is coloured by ONE real sample: its most recent")
    from equis_loader import report_data as rd
    from equis_loader.store import DETECT_TRUE, ReportStore
    from equis_loader.pivot import Variable, build_pivot
    sys.path.insert(0, HERE)
    import test_pivot as fx
    store = ReportStore.from_table(rd.ReportTable(fx.HEADERS, fx.ROWS))
    p = build_pivot(store, [Variable(*fx.AS), Variable(*fx.PB)])
    got = {s["location"]: s for s in bv.stations(
        store.con, p.table, "arsenic", "lead", list(DETECT_TRUE))}
    # MW-1 holds S1 (2020-01-01, both detected) and S2 (2020-02-01, lead
    # not detected): S2 is the most recent, so MW-1 is NOT "both".
    check("MW-1 takes its most recent sample (S2)",
          got["MW-1"]["sample"] == "S2" and got["MW-1"]["samples_here"] == 2,
          got.get("MW-1"))
    check("...and so is drawn as a non-detect class, not a colour",
          bv.key_for(got["MW-1"]["x"], got["MW-1"]["y"],
                     got["MW-1"]["x_detected"], got["MW-1"]["y_detected"],
                     [5], [5]) == "x_only")
    check("a station alone on its latest date is not tied",
          got["MW-1"]["tied"] == 1, got["MW-1"]["tied"])
    check("MW-2 has no lead at all: not a station on this map",
          "MW-2" not in got, sorted(got))
    check("MW-3's only sample has a blank flag: not placed",
          "MW-3" not in got, sorted(got))


def modes_fixture():
    print("\n-- points modes: one real sample per location, by a stated rank")
    import datetime
    import duckdb
    con = duckdb.connect()
    con.execute("""CREATE TABLE t (location VARCHAR, sample VARCHAR,
        sample_date TIMESTAMP, a_value DOUBLE, a_detect VARCHAR,
        a_qualifier VARCHAR, b_value DOUBLE, b_detect VARCHAR,
        b_qualifier VARCHAR)""")
    rows = [
        # L1: S1 old with the highest A as a NON-DETECT limit (50); S2 a
        # detected A of 9 with the highest B; S3 most recent.
        ("L1", "S1", "2020-01-01", 50, "N", None, 1, "Y", None),
        ("L1", "S2", "2021-06-01", 9, "Y", None, 30, "Y", None),
        ("L1", "S3", "2023-03-01", 4, "Y", None, 2, "Y", None),
        # L2: only non-detects of A: the highest limit is still the pick
        # ("we looked and found nothing" is a result). Two share it: a tie.
        ("L2", "T1", "2020-05-01", 7, "N", None, 3, "Y", None),
        ("L2", "T2", "2022-05-01", 7, "N", None, 5, "Y", None),
        # L3: undated - outside any window, still a candidate without one.
        ("L3", "U1", None, 3, "Y", None, 3, "Y", None),
    ]
    con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    truth = ["Y"]

    def picks(mode, window=None):
        return {s["location"]: s for s in bv.stations(con, "t", "a", "b", truth,
                                                      mode=mode, window=window)}

    def solid(mode, window=None):
        return sorted(r[0] for r in con.execute(
            "SELECT sample FROM (%s)" % bv.picks_sql("t", ["a", "b"], truth, mode,
                                                       window)).fetchall())

    got = picks("recent")
    check("recent: each location's latest sample (S3, T2; undated U1)",
          (got["L1"]["sample"], got["L2"]["sample"], got["L3"]["sample"])
          == ("S3", "T2", "U1"), {k: v["sample"] for k, v in got.items()})
    got = picks("max_x")
    check("max A: a DETECTED 9 outranks a non-detect limit of 50 (S2, not S1)",
          got["L1"]["sample"] == "S2", got["L1"]["sample"])
    check("max A where A is never detected: the highest limit still stands for it",
          got["L2"]["sample"] in ("T1", "T2") and not got["L2"]["x_detected"],
          got["L2"])
    check("...two share it: the later one is picked, and the tie is counted",
          got["L2"]["sample"] == "T2" and got["L2"]["tied"] == 2, got["L2"])
    got = picks("max_y")
    check("max B: L1's sample with the highest B (S2)", got["L1"]["sample"] == "S2",
          got["L1"]["sample"])
    check("one point per location, every mode but 'all'",
          all(len(solid(m)) == 3 for m in ("recent", "max_x", "max_y"))
          and len(solid("all")) == 6, [len(solid(m)) for m in ("all", "recent", "max_x", "max_y")])
    w = (datetime.date(2020, 1, 1), datetime.date(2021, 12, 31))
    check("a window: most recent WITHIN it (S2, T1), and undated U1 is out",
          sorted(picks("recent", w)) == ["L1", "L2"]
          and (picks("recent", w)["L1"]["sample"], picks("recent", w)["L2"]["sample"])
          == ("S2", "T1"), {k: v["sample"] for k, v in picks("recent", w).items()})
    check("a window's end day is inclusive (2021-06-01 to 2021-06-01 keeps S2)",
          solid("all", (datetime.date(2021, 6, 1), datetime.date(2021, 6, 1))) == ["S2"],
          solid("all", (datetime.date(2021, 6, 1), datetime.date(2021, 6, 1))))
    check("an open-ended window works from either end",
          solid("all", (datetime.date(2022, 1, 1), None)) == ["S3", "T2"]
          and solid("all", (None, datetime.date(2020, 1, 1))) == ["S1"],
          (solid("all", (datetime.date(2022, 1, 1), None)),
           solid("all", (None, datetime.date(2020, 1, 1)))))
    try:
        bv.picks_sql("t", ["a", "b"], truth, "average")
        refused = False
    except ValueError:
        refused = True
    check("an unknown mode is refused, never guessed", refused)
    try:
        bv.picks_sql("t", ["a", "b"], truth, "max_z")
        refused = False
    except ValueError:
        refused = True
    check("'highest Z' is refused for a two-analyte chart", refused)


def facility_a():
    print("\n-- Facility A (real parquet)")
    if not os.path.isdir(os.path.join(ROOT, "data", "facility_a")):
        print("[SKIP] no data/facility_a - the real-data checks did not run")
        return
    from equis_loader.store import ReportStore
    from equis_loader.pivot import Variable, build_pivot
    store = ReportStore.from_manifest(os.path.join(ROOT, "data", "facility_a"))
    p = build_pivot(store, [Variable("Manganese", fraction="T"),
                            Variable("Lead", fraction="T")], matrix=["WS"])
    rows = store.con.execute(
        "SELECT manganese_value, lead_value FROM %s WHERE manganese_detect = 'Y' "
        "AND lead_detect = 'Y'" % p.table).fetchall()
    mn = bv.natural_breaks([r[0] for r in rows])
    pb = bv.natural_breaks([r[1] for r in rows])
    # Measured 2026-10-09: log10 Jenks over the 1,573 both-detected samples.
    check("Mn breaks on log10: ~76.15 / ~810",
          mn["scale"] == "log10" and abs(mn["breaks"][0] - 76.15) < 0.1
          and abs(mn["breaks"][1] - 810) < 1, mn)
    check("Pb breaks on log10: ~1.70 / ~16.78",
          abs(pb["breaks"][0] - 1.70) < 0.01 and abs(pb["breaks"][1] - 16.78) < 0.05, pb)
    counts = [sum(1 for r in rows if bv.class_of(r[0], mn["breaks"]) == c)
              for c in (1, 2, 3)]
    check("Mn classes are balanced, not 1,567 / 4 / 2", counts == [466, 742, 365],
          counts)

    # The test pair since 2026-10-09. SO stations are replicate-heavy: on
    # every station's latest date sit 6-9 field replicates (_R2 ... _R9).
    so = build_pivot(store, [Variable("Arsenic"), Variable("Chromium")],
                     matrix=["SO"])
    from equis_loader.store import DETECT_TRUE
    b = bv.axis_breaks(store.con, so.table, "arsenic", "chromium",
                       list(DETECT_TRUE))
    # Over the 109 both-detected PRIMARIES; replicates are not plotted.
    check("As x Cr SO: log10 Jenks breaks ~2.69/9.85 and ~2.64/6.34",
          b["x"]["scale"] == "log10"
          and [round(v, 2) for v in b["x"]["breaks"]] == [2.69, 9.85]
          and [round(v, 2) for v in b["y"]["breaks"]] == [2.64, 6.34], b)
    st = bv.stations(store.con, so.table, "arsenic", "chromium",
                     list(DETECT_TRUE))
    # With the 6-9 replicates on each latest date gone, every station's
    # most recent sample is unique: the pick is no longer arbitrary.
    check("As x Cr SO: 10 stations, none tied once replicates are out",
          len(st) == 10 and all(s["tied"] == 1 for s in st),
          sorted((s["location"], s["tied"]) for s in st))
    store.close()


if __name__ == "__main__":
    sys.exit(main())
