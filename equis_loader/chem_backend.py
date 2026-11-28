"""ChemDuck answering the panel's hot path: an experimental backend.

With the switch on (settings.load_chemduck_mode() == "backend": the panel's
Chemistry box, or EQUIS_CHEMDUCK=backend in QGIS's environment) the panel's
`_analysis` is a ChemBackend over the store and the ChemDuck shadow
(chem.Chem). The questions a click asks of the chemistry are ChemDuck's:

    aggregate      a map: aggregate_points, one point per location by the
                   plugin's own rule across a location's points (below)
    count_for      the footer: the published point_catalog, else point_counts
    series         a location's time series: point_series
    series_many    sparklines and the roll: point_series, one call for all
    analytes, matrices, fractions_for
                   the pickers: the published point_catalog, else the store

Everything else is the store's, unchanged: the analytic-method list
(ChemDuck keeps method classes, not methods), locations and their geometry,
report columns, export, the Analysis tab's pivots (`thread_view`), the unit
and fraction guards. Those are lists and plumbing, or the plan's later
phases (4: the Analysis tab), not the click-time chemistry rules.

THE PUBLISHED COUNTS. A source published with ChemDuck's point_catalog
sidecar (point_counts' rows, counted once by location for every analyte)
answers the footer and the pickers from that table: 0.04 s a count against
0.4-0.5 s live at Facility A (2026-11-27), and the same numbers, which
ChemDuck's test_point_catalog holds. It keeps sample types and the
reportable flag; a date range, a depth or unit filter, or several matrices
need every result, and those counts are live (Chem.catalog_reason). A
publish older than the catalog has none, and everything is live as before.

WHERE CHEMDUCK CAN'T BE ASKED, THE STORE ANSWERS, AND SAYS SO. Excluded
non-detects (the plan's decision 1), an analytic-method filter (ChemDuck has
none; it keeps method classes apart), detected-only, and anything ChemDuck
refuses (a variable in two units, a unit contradicting its matrix) go to the
store, and `notes` records the question and why. The panel reads `notes`
after each build, so a layer is never ChemDuck's by assumption.

A LOCATION WITH SEVERAL POINTS. ChemDuck answers per measurement point -
depths, positions and method classes apart (its rules D1, D3) - and how a
map shows a location with several is the plan's decision 2, not taken.
Until it is, the backend takes the point the plugin's own rule would:
the highest with detections first under maximum, the most recent date
under most recent (chem_parity.best, the rule the parity test reads the
plugin by). The layer's count is the samples behind every point.

WHAT DIFFERS FROM THE STORE, BY DESIGN: everything tools/test_chemduck_
parity.py lists as intended - QC samples and non-reportable results left
out, a sample's re-runs and one day's samples ranked, the limit chain,
units converted at load. That is the point of the switch: these are
ChemDuck's answers.

A ChemDuck layer carries no report columns yet: a ChemDuck point is not one
of the store's rows, so `fields` is empty, and the layer's report columns
are blank. Phase 2 of the plan.

Imports no QGIS, like store.py and chem.py.
"""
from . import report_data as rd
from .chem import FRACTION_NAME
from .chem_parity import best

CHEMDUCK = "chemduck"
STORE = "store"
# A note's why when ChemDuck answered from the published point_catalog.
PUBLISHED = "from the published counts (point_catalog)"
# The panel's fraction code for ChemDuck's name of it ('Dissolved' -> 'D').
FRACTION_CODE = {name: code for code, name in FRACTION_NAME.items()}


def _unsupported(non_detect, method=None, detected_only=False, fraction=None):
    """Why ChemDuck can't be asked this, or None."""
    if non_detect not in ("half", "full"):
        return "excluded non-detects have no ChemDuck form yet (the plan's decision 1)"
    if method:
        return ("an analytic-method filter: ChemDuck has none, it keeps method "
                "classes apart")
    if detected_only:
        return "detected-only has no ChemDuck form"
    if len([f for f in (fraction or ()) if str(f or "").strip()]) > 1:
        return "several fractions: ChemDuck is asked for one at a time"
    return None


class ChemBackend(object):
    """The store, with its hot-path chemistry answered by ChemDuck."""

    def __init__(self, store, chem):
        self.store = store
        self.chem = chem
        # (question, who answered, why) for each hot-path call, newest
        # last; the panel reads and clears it.
        self.notes = []

    def __getattr__(self, name):
        # Only for what this class doesn't define: the pickers, locations,
        # report fields, export, thread_view, set_row_scope, ...
        return getattr(self.store, name)

    def _note(self, question, who, why=""):
        # Bounded: the footer asks on every click, and only a build reads.
        self.notes = self.notes[-49:] + [(question, who, why)]

    def _refused(self, exc):
        # DuckDB's error() raises an InvalidInputException; a macro's own
        # refusal says why in its first line.
        return "ChemDuck refused: %s" % str(exc).splitlines()[0][:200]

    # ---------------------------------------------------------------- map

    def aggregate(self, chemical, cas_rn=None, how=rd.MOST_RECENT, detected_only=False,
                  non_detect=rd.ND_HALF, matrix=rd.ALL_MATRICES, fraction=rd.ANY_FRACTION,
                  method=rd.ALL_METHODS, fields=False):
        question = "map of %s" % chemical
        why = _unsupported(non_detect, method, detected_only, fraction)
        if how not in (rd.MOST_RECENT, rd.MAXIMUM):
            why = why or "not an aggregate"
        if why:
            self._note(question, STORE, why)
            return self.store.aggregate(chemical, cas_rn, how, detected_only, non_detect,
                                        matrix, fraction, method, fields)
        try:
            grouped = self.chem.by_location(chemical, how=how, non_detect=non_detect,
                                            matrix=matrix, fraction=fraction)
        except Exception as exc:               # noqa: BLE001 - said, then the store
            self._note(question, STORE, self._refused(exc))
            return self.store.aggregate(chemical, cas_rn, how, detected_only, non_detect,
                                        matrix, fraction, method, fields)
        summary = {}
        for location, points in grouped.items():
            if not location:
                continue
            chosen = best(points, how)[0]
            summary[location] = {
                "location": location,
                "value": chosen["value"],
                "unit": chosen["unit"],
                "date": chosen["date"],
                "detect": chosen["detect"],
                "at_limit": chosen["at_limit"],
                # The samples behind the location's points, and their
                # detections: ChemDuck's n_events counts each sample once.
                "count": sum(int(p.get("n_events") or 0) for p in points),
                "detect_count": sum(int(p.get("n_detects") or 0) for p in points),
                # Provenance, for a reader of the summary; the layer builder
                # reads only the keys above.
                "sample_id": chosen.get("sample_id"),
                "method_code": chosen.get("method_code"),
                "method_class": chosen.get("method_class"),
                "selected_by": chosen.get("selected_by"),
                "n_points": len(points),
            }
            if fields:
                summary[location]["fields"] = None
        self._note(question, CHEMDUCK)
        return summary

    # ------------------------------------------------------------- footer

    def count_for(self, chemical, cas_rn=None, detected_only=False, non_detect=rd.ND_HALF,
                  matrix=rd.ALL_MATRICES, fraction=rd.ANY_FRACTION, method=rd.ALL_METHODS):
        question = "count of %s" % chemical
        why = _unsupported(non_detect, method, detected_only, fraction)
        if why:
            self._note(question, STORE, why)
            return self.store.count_for(chemical, cas_rn, detected_only, non_detect,
                                        matrix, fraction, method)
        if self.chem.catalog_reason(matrix) is None:
            try:
                answer = self.chem.catalog_counts(chemical, matrix=matrix, fraction=fraction)
            except Exception:                  # noqa: BLE001 - asked live below
                # A refusal (point_counts' own, in the same words) or a
                # catalog that won't read: the live call says which, and
                # its refusal sends the count to the store as before.
                pass
            else:
                self._note(question, CHEMDUCK, PUBLISHED)
                return answer
        try:
            answer = self.chem.counts(chemical, matrix=matrix, fraction=fraction)
        except Exception as exc:               # noqa: BLE001 - said, then the store
            self._note(question, STORE, self._refused(exc))
            return self.store.count_for(chemical, cas_rn, detected_only, non_detect,
                                        matrix, fraction, method)
        self._note(question, CHEMDUCK)
        return answer

    # ------------------------------------------------------------ pickers

    def _summary(self):
        """The published summary under the scope set now, or None when the
        catalog can't answer it (and the store's lists stand)."""
        if self.chem.catalog_reason() is not None:
            return None
        try:
            return self.chem.catalog_summary()
        except Exception:                      # noqa: BLE001 - the store's lists
            return None

    @staticmethod
    def _codes(wanted):
        """A matrix choice as the store compares it: upper-cased codes, or
        None for no filter (None, or an empty collection)."""
        codes = [str(c or "").strip().upper() for c in (wanted or ())]
        return set(codes) if codes else None

    def analytes(self, matrix=rd.ALL_MATRICES):
        """The analyte offer, in the store's shape ({chemical, cas_rn,
        count}, case-insensitively by name): ChemDuck's results per analyte
        and CAS under the scope set now."""
        summary = self._summary()
        if summary is None:
            return self.store.analytes(matrix)
        codes = self._codes(matrix)
        counts = {}
        for row in summary:
            if codes is not None and str(row["matrix"] or "").upper() not in codes:
                continue
            key = (row["analyte"] or "", row["cas_number"] or "")
            if key[0]:
                counts[key] = counts.get(key, 0) + int(row["n_results"] or 0)
        ordered = sorted(counts.items(), key=lambda kv: (kv[0][0].lower(), kv[0]))
        return [{"chemical": name, "cas_rn": cas, "count": n} for (name, cas), n in ordered]

    def matrices(self):
        """The matrix offer, in the store's shape ({matrix, count}, most
        results first): ChemDuck's results per matrix."""
        summary = self._summary()
        if summary is None:
            return self.store.matrices()
        counts = {}
        for row in summary:
            code = str(row["matrix"] or "").upper()
            counts[code] = counts.get(code, 0) + int(row["n_results"] or 0)
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return [{"matrix": code, "count": n} for code, n in ordered]

    def fractions_for(self, chemical, cas_rn=None, matrix=rd.ALL_MATRICES):
        """The fraction codes an analyte has in the matrices chosen, as the
        store gives them (the panel's codes: ChemDuck's 'Dissolved' is
        'D'), matched by CAS when one is given, as the store matches."""
        summary = self._summary()
        if summary is None:
            return self.store.fractions_for(chemical, cas_rn, matrix)
        codes = self._codes(matrix)
        found = set()
        for row in summary:
            if cas_rn:
                if (row["cas_number"] or "") != cas_rn:
                    continue
            elif (row["analyte"] or "") != (chemical or ""):
                continue
            if codes is not None and str(row["matrix"] or "").upper() not in codes:
                continue
            name = str(row["fraction"] or "").strip()
            if name:
                found.add(FRACTION_CODE.get(name, name).upper())
        return sorted(found)

    # -------------------------------------------------------------- series

    def series(self, location, chemical, cas_rn=None, non_detect=rd.ND_HALF,
               matrix=rd.ALL_MATRICES, fraction=rd.ANY_FRACTION, method=rd.ALL_METHODS):
        return self.series_many([location], chemical, cas_rn, non_detect, matrix,
                                fraction, method).get(str(location), [])

    def series_many(self, locations, chemical, cas_rn=None, non_detect=rd.ND_HALF,
                    matrix=rd.ALL_MATRICES, fraction=rd.ANY_FRACTION, method=rd.ALL_METHODS):
        question = "series of %s" % chemical
        why = _unsupported(non_detect, method, fraction=fraction)
        if why:
            self._note(question, STORE, why)
            return self.store.series_many(locations, chemical, cas_rn, non_detect, matrix,
                                          fraction, method)
        if locations is not None and not list(locations):
            return {}
        try:
            answer = self.chem.series(None if locations is None else list(locations),
                                      chemical, non_detect=non_detect, matrix=matrix,
                                      fraction=fraction)
        except Exception as exc:               # noqa: BLE001 - said, then the store
            self._note(question, STORE, self._refused(exc))
            return self.store.series_many(locations, chemical, cas_rn, non_detect, matrix,
                                          fraction, method)
        self._note(question, CHEMDUCK)
        return answer

    # ---------------------------------------------------------- reporting

    def take_notes(self):
        """The notes since the last call, and clear them."""
        notes, self.notes = self.notes, []
        return notes
