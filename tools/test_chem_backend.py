"""The chemistry switch's backend: ChemDuck answering the hot path.

equis_loader/chem_backend.py, on the fixture facility (tools/chem_fixture.py):

- a map is ChemDuck's, one point per location by the plugin's rule, in the
  shape the layer builder reads, and where the fixture says ChemDuck
  differs (a QC sample, a non-reportable result) it is ChemDuck's answer;
- what ChemDuck can't be asked - excluded non-detects, a method filter, a
  refusal - is the store's answer, with the reason in `notes`;
- the footer count is point_counts', the series point_series', in the
  store's shapes;
- the footer and the pickers read the published counts (point_catalog)
  where the scope allows, equal to the live count, and live otherwise;
- everything else (the method list, thread_view) is the store's;
- the row scope set on the shadow reaches every answer.

    python tools/test_chem_backend.py

Needs duckdb and pyyaml, as ChemDuck does.
"""
import datetime
import json
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
        # The pickers are the published counts' (below); the rest is the store's.
        check("thread_view and the method list are the store's",
              backend.thread_view().count() == store.count()
              and backend.methods("Arsenic") == store.methods("Arsenic"))

        # --- the row scope -----------------------------------------------------------
        chem.set_row_scope(units=["mg/kg"])
        store.set_row_scope(units=["mg/kg"])
        empty = backend.aggregate("Arsenic", how=rd.MAXIMUM, **kwargs)
        check("the row scope set on the shadow reaches the map (no Arsenic in mg/kg)",
              empty == {} and backend.count_for("Arsenic", **kwargs) == (0, 0), empty)
        chem.set_row_scope()
        store.set_row_scope()

        # --- the published counts (point_catalog) -------------------------------------
        check("a load builds point_catalog, as a publish writes it", chem.catalog)
        variables = [(a, (m,), (f,)) for a in ("Arsenic", "Lead", "Iron", "Aluminum", "Mercury",
                                                 "Nothing")
                     for m in ("WG", "SO") for f in ("T", "D", "N")]
        scopes = [dict(), dict(sample_types=["N"]), dict(sample_types=["N", "FD"])]
        same, published, refused_alike = [], 0, 0
        for scope in scopes:
            chem.set_row_scope(**scope)
            for analyte, m, f in variables:
                try:
                    live = chem.counts(analyte, matrix=m, fraction=f)
                except Exception as exc:       # noqa: BLE001 - compared below
                    live = "refused: " + str(exc).splitlines()[0]
                try:
                    read = chem.catalog_counts(analyte, matrix=m, fraction=f)
                except Exception as exc:       # noqa: BLE001
                    read = "refused: " + str(exc).splitlines()[0]
                same.append(live == read or (analyte, m, f, scope, live, read))
                refused_alike += isinstance(live, str) and live == read
                if backend.count_for(analyte, matrix=m, fraction=f) is not None:
                    note = backend.take_notes()[-1]
                    published += note[2] == "from the published counts (point_catalog)"
        chem.set_row_scope()
        check("the published count equals the live one under every scope the panel sets "
              "(%d calls, %d refused alike)" % (len(same), refused_alike),
              all(s is True for s in same) and refused_alike > 0,
              [s for s in same if s is not True][:3])
        check("... and the footer reads it, except where ChemDuck refuses (asked live, "
              "then the store)", published == len(same) - refused_alike,
              (published, len(same), refused_alike))
        live_cases = [("a date range", dict(start=datetime.date(2020, 1, 1))),
                      ("a unit filter", dict(units=["ug/L"])),
                      ("a depth filter", dict(depth=(0, 1, "m")))]
        for reason, scope in live_cases:
            chem.set_row_scope(**scope)
            store.set_row_scope(**scope)
            count = backend.count_for("Arsenic", **kwargs)
            note = backend.take_notes()[-1]
            check("%s: the footer is the live count, with every result" % reason,
                  chem.catalog_reason() == reason and note == ("count of Arsenic", CHEMDUCK, "")
                  and count == chem.counts("Arsenic", **kwargs), (chem.catalog_reason(), note))
            check("%s: ... and the pickers are the store's" % reason,
                  backend.analytes() == store.analytes())
        chem.set_row_scope()
        store.set_row_scope()
        check("several matrices: the live count (combining them is ChemDuck's rule)",
              chem.catalog_reason(("WG", "SO")) == "several matrices")

        offer = backend.analytes()
        summary = chem.catalog_summary()
        check("the analyte offer is the published one: ChemDuck's results per analyte, in "
              "the store's shape and order",
              [r["chemical"] for r in offer]
              == sorted({r["analyte"] for r in summary}, key=str.lower)
              and all(set(r) == {"chemical", "cas_rn", "count"} for r in offer)
              and sum(r["count"] for r in offer) == sum(r["n_results"] for r in summary),
              offer[:3])
        arsenic = next(r for r in offer if r["chemical"] == "Arsenic")
        check("... keyed by the store's CAS, counted as ChemDuck counts (fewer than the "
              "store's: QC samples and non-reportable results out)",
              arsenic["cas_rn"] == "7440-38-2"
              and arsenic["count"] < next(r["count"] for r in store.analytes()
                                          if r["chemical"] == "Arsenic"), arsenic)
        check("the matrix offer is the published one, most results first",
              {r["matrix"] for r in backend.matrices()}
              == {str(r["matrix"] or "").upper() for r in summary}
              and [r["count"] for r in backend.matrices()]
              == sorted((r["count"] for r in backend.matrices()), reverse=True))
        fractions = backend.fractions_for("Arsenic", matrix=("WG",))
        check("fractions are the panel's codes (ChemDuck's 'Total' is 'T'), by CAS too",
              fractions and set(fractions) <= set(store.fractions_for("Arsenic", matrix=("WG",)))
              and backend.fractions_for("ignored", cas_rn="7440-38-2", matrix=("WG",))
              == fractions, fractions)
        check("methods stay the store's (ChemDuck keeps method classes, not methods)",
              backend.methods("Arsenic") == store.methods("Arsenic"))

        # A publish older than the catalog: no point_catalog sidecar.
        old = os.path.join(folder, "old_publish")
        ch._quiet(ch.chemduck.publish, manifest, old, x_col="LONGITUDE", y_col="LATITUDE",
                  quiet=True)
        meta_path = os.path.join(old, "sidecars.json")
        with open(meta_path, encoding="utf-8") as handle:
            meta = json.load(handle)
        published_too = ch.CATALOG in meta["tables"]
        del meta["tables"][ch.CATALOG]
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump(meta, handle)
        parts = [os.path.join(os.path.dirname(manifest), p["path"])
                 for p in meta["source_manifest"]["partitions"]]
        from equis_loader.store import duckdb_module
        con = duckdb_module().connect()
        try:
            old_chem = ch.Chem.open_published(con, old, parts)
            old_backend = ChemBackend(store, old_chem)
            count = old_backend.count_for("Arsenic", **kwargs)
            note = old_backend.take_notes()[-1]
            check("a publish writes point_catalog; one without it (an older publish) counts "
                  "live and offers the store's lists",
                  published_too and not old_chem.catalog
                  and old_chem.catalog_reason().startswith("no published counts")
                  and note == ("count of Arsenic", CHEMDUCK, "")
                  and count == chem.counts("Arsenic", **kwargs)
                  and old_backend.analytes() == store.analytes(), (old_chem.catalog, note))
        finally:
            con.close()
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
