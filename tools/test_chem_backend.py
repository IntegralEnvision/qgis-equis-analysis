"""The chemistry switch's backend: ChemDuck answering the hot path.

equis_loader/chem_backend.py, on the fixture facility (tools/chem_fixture.py):

- a map is ChemDuck's, one point per location by the plugin's rule, in the
  shape the layer builder reads, and where the fixture says ChemDuck
  differs (a QC sample, a non-reportable result) it is ChemDuck's answer;
- what ChemDuck can't be asked - excluded non-detects, a method filter, a
  refusal - is the store's answer, with the reason in `notes`;
- the footer count is point_counts', the series point_series', in the
  store's shapes;
- everything else (pickers, thread_view) is the store's;
- the row scope set on the shadow reaches every answer.

    python tools/test_chem_backend.py

Needs duckdb and pyyaml, as ChemDuck does.
"""
import datetime
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import chem_fixture as fx                            # noqa: E402

from equis_loader import chem as ch                  # noqa: E402
from equis_loader import report_data as rd           # noqa: E402
from equis_loader.chem_backend import CHEMDUCK, STORE, ChemBackend   # noqa: E402
from equis_loader.chem_parity import best            # noqa: E402
from equis_loader.store import ReportStore           # noqa: E402

FAILURES = []
KEYS = {"location", "value", "unit", "date", "detect", "at_limit", "count", "detect_count"}


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + str(detail)) if detail != "" else ""))
    if not condition:
        FAILURES.append(label)


def main():
    folder = tempfile.mkdtemp(prefix="chem_backend_")
    chem = store = None
    try:
        manifest = fx.write(os.path.join(folder, "facility"))
        store = ReportStore.from_manifest(os.path.dirname(manifest))
        store.con.execute("SET threads = 1")
        chem = ch.Chem.shadow(manifest, os.path.join(folder, "chem.duckdb"), threads=1)
        backend = ChemBackend(store, chem)
        kwargs = dict(matrix=("WG",), fraction=("T",))

        # --- a map --------------------------------------------------------------
        for how in (rd.MAXIMUM, rd.MOST_RECENT):
            summary = backend.aggregate("Arsenic", how=how, fields=True, **kwargs)
            note = backend.take_notes()
            grouped = chem.by_location("Arsenic", how=how, **kwargs)
            chosen = {loc: best(points, how)[0]["value"] for loc, points in grouped.items()}
            check("%s: a map is ChemDuck's, one point per location by the plugin's rule" % how,
                  note == [("map of Arsenic", CHEMDUCK, "")]
                  and {loc: e["value"] for loc, e in summary.items()} == chosen, note)
            check("%s: in the shape the layer builder reads, report columns empty" % how,
                  all(KEYS <= set(e) and e["fields"] is None for e in summary.values())
                  and all(isinstance(e["date"], (datetime.date, type(None)))
                          for e in summary.values()))
        maximum = backend.aggregate("Arsenic", how=rd.MAXIMUM, **kwargs)
        plugin = store.aggregate("Arsenic", how=rd.MAXIMUM, **kwargs)
        check("where the fixture agrees, so do the two (AGREE)",
              maximum["AGREE"]["value"] == plugin["AGREE"]["value"],
              (maximum["AGREE"]["value"], plugin["AGREE"]["value"]))
        differs = [loc for loc, _what, _rows, expect in fx.CASES
                   if {"qc_samples", "non_reportable"} & set(expect[rd.MAXIMUM])
                   and loc in maximum and loc in plugin]
        check("where ChemDuck's rules differ (QC samples, non-reportable), it is "
              "ChemDuck's answer, not the store's",
              differs and all(maximum[loc]["value"] != plugin[loc]["value"]
                              for loc in differs), differs)
        profile = maximum.get("PROFILE", {})
        check("a location with several points says how many, and takes the highest "
              "detected one", profile.get("n_points", 0) > 1
              and profile["value"] == max(p["value"] for p in grouped["PROFILE"]
                                          if p["detect"] == "Y" or not any(
                                              q["detect"] == "Y" for q in grouped["PROFILE"])),
              profile)

        # --- what ChemDuck can't be asked -------------------------------------
        cases = [
            ("excluded non-detects", dict(non_detect=rd.ND_EXCLUDE), "decision 1"),
            ("a method filter", dict(method=("6010B",)), "method"),
        ]
        for label, extra, reason in cases:
            answer = backend.aggregate("Arsenic", how=rd.MAXIMUM, **dict(kwargs, **extra))
            note = backend.take_notes()
            check("%s: the store's answer, and the note says why" % label,
                  answer == store.aggregate("Arsenic", how=rd.MAXIMUM, **dict(kwargs, **extra))
                  and note and note[-1][1] == STORE and reason in note[-1][2], note)
        refused = backend.aggregate("Mercury", how=rd.MAXIMUM, **kwargs)
        note = backend.take_notes()
        check("a map ChemDuck refuses is the store's, and the note quotes the refusal",
              refused == store.aggregate("Mercury", how=rd.MAXIMUM, **kwargs)
              and note and note[-1][1] == STORE and "ChemDuck refused" in note[-1][2], note)

        # --- the footer ---------------------------------------------------------
        count = backend.count_for("Arsenic", **kwargs)
        note = backend.take_notes()
        check("the footer count is point_counts'",
              count == chem.counts("Arsenic", **kwargs) and note[-1][1] == CHEMDUCK,
              (count, store.count_for("Arsenic", **kwargs)))
        check("... fewer results than the store's, QC and non-reportable left out",
              count[1] < store.count_for("Arsenic", **kwargs)[1])

        # --- series -----------------------------------------------------------------
        many = backend.series_many(["AGREE", "PROFILE", "NOWHERE"], "Arsenic", **kwargs)
        check("series_many answers every location asked, oldest first, in the store's keys",
              set(many) == {"AGREE", "PROFILE", "NOWHERE"} and many["NOWHERE"] == []
              and all({"date", "value", "unit", "detect", "at_limit"} <= set(r)
                      for rows in many.values() for r in rows)
              and all([str(r["date"]) for r in rows] == sorted(str(r["date"]) for r in rows)
                      for rows in many.values()))
        check("series is series_many for one location",
              backend.series("AGREE", "Arsenic", **kwargs) == many["AGREE"])
        check("AGREE's series has the store's dates and values",
              [(str(r["date"])[:10], r["value"]) for r in many["AGREE"]]
              == [(str(r["date"])[:10], r["value"])
                  for r in store.series("AGREE", "Arsenic", **kwargs)])
        check("every location: series_many(None) answers all ChemDuck has",
              set(backend.series_many(None, "Arsenic", **kwargs)) >= {"AGREE", "PROFILE"})

        # --- the store's, unchanged ------------------------------------------------
        check("pickers and thread_view are the store's",
              backend.analytes() == store.analytes()
              and backend.matrices() == store.matrices()
              and backend.thread_view().count() == store.count())

        # --- the row scope -----------------------------------------------------------
        chem.set_row_scope(units=["mg/kg"])
        store.set_row_scope(units=["mg/kg"])
        empty = backend.aggregate("Arsenic", how=rd.MAXIMUM, **kwargs)
        check("the row scope set on the shadow reaches the map (no Arsenic in mg/kg)",
              empty == {} and backend.count_for("Arsenic", **kwargs) == (0, 0), empty)
        chem.set_row_scope()
        store.set_row_scope()
    finally:
        if chem is not None:
            chem.close()
        if store is not None:
            store.close()
        shutil.rmtree(folder, ignore_errors=True)
    print()
    if FAILURES:
        print("FAILED: %d" % len(FAILURES))
        return 1
    print("ChemDuck answers the hot path where it can be asked, and says where not.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
