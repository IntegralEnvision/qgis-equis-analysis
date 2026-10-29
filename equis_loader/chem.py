"""The plugin's one door to ChemDuck (docs/chemduck-adoption-plan.md).

Every chemistry question that moves to ChemDuck goes through here, and
nothing else in the plugin writes ChemDuck SQL. Phase 1 is a shadow: the
adapter opens ChemDuck beside the plugin's own store and answers the map's
question, and only the parity test (tools/test_chemduck_parity.py) and a
debug log read the answer. The panel still maps ReportStore's.

Like store.py this imports no QGIS: it takes a DuckDB connection and paths
from its caller, so it is tested under a plain interpreter.

ChemDuck lives in its own schema of the caller's connection (the plugin's
tables stay in `main`), and its macros are called through a cursor whose
current schema is that one, because a macro binds tables in the caller's
schema. ChemDuck's answer is per MEASUREMENT POINT - a location's depths,
positions and method classes are kept apart (its shared rules D1 and D3) -
and the adapter does not roll them up: how a map shows a location with
several points is decision 2 of the plan, not this module's to take.
"""
import contextlib
import io

from . import chemduck
from . import report_data as rd

SCHEMA = "chemduck"

# The plugin codes fractions as EQuIS does; ChemDuck's ARII mapping names
# them ('T' -> 'Total', 'D' -> 'Dissolved') and passes other codes through,
# as Facility A's 'N' (not applicable, every solid).
FRACTION_NAME = {"T": "Total", "D": "Dissolved"}
EVENT_AGG = {rd.MOST_RECENT: "most_recent", rd.MAXIMUM: "maximum"}
# The panel's non-detect choice is a display factor on the selected
# non-detect's limit: it changes how a non-detect is drawn, never which
# result is selected. "Excluded" has no factor; what it becomes is the
# plan's decision 1, so it is refused here rather than guessed.
DISPLAY_FACTOR = {rd.ND_HALF: 0.5, rd.ND_FULL: 1.0}


def _quiet(fn, *args, **kwargs):
    # ChemDuck prints a line per SQL file it loads; in QGIS that lands in
    # the Python console. Until it logs through `logging`, swallow it here.
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


class Chem(object):
    """ChemDuck opened in one schema of a caller's DuckDB connection."""

    def __init__(self, con, schema=SCHEMA):
        self.con = con
        self.schema = schema
        self.cur = chemduck.schema_cursor(con, schema)
        self._scope = rd.row_scope(None, None, None)

    @classmethod
    def load(cls, con, source, mapping=None, schema=SCHEMA):
        """ChemDuck over rows the plugin holds: a path (CSV, parquet, a
        manifest.json) or a DuckDB relation. A relation is read through its
        Arrow stream, which needs pyarrow (QGIS 4.2.3's Python has it; the
        plugin's own DuckDB bundle does not)."""
        _quiet(chemduck.load_source, con, source,
               chemduck.load_mapping(mapping or chemduck.DEFAULT_MAPPING), schema=schema)
        return cls(con, schema)

    @classmethod
    def open_published(cls, con, folder, partition_files, schema=SCHEMA):
        """ChemDuck over a parquet facility through the sidecars its
        publish wrote, the way Chemrooms opens it in the browser."""
        _quiet(chemduck.open_consumer, con, folder, partition_files, schema=schema)
        return cls(con, schema)

    def set_row_scope(self, start=None, end=None, sample_types=None):
        """The panel's row scope, the same tuple ReportStore keeps.

        A sample-type scope the panel has not set is ChemDuck's default:
        normal samples only (typed N or untyped). The plugin's default is
        every type, so a QC sample reaches the store's map and not this
        one: the plan's decision 5, an intended difference."""
        scope = rd.row_scope(start, end, sample_types)
        changed = scope != self._scope
        self._scope = scope
        return changed

    def points(self, chemical, how=rd.MOST_RECENT, non_detect=rd.ND_HALF,
               matrix=rd.ALL_MATRICES, fraction=rd.ANY_FRACTION, method_class=None,
               locations=None, reportable_only=None, sample_types=None):
        """aggregate_points: one row per measurement point and variable.

        matrix and fraction take the panel's codes (a tuple, or None for
        all). One matrix is p_matrix; several are p_matrices, which ChemDuck
        combines only when its matrix classes say they may be (and refuses
        otherwise). reportable_only and sample_types override the dataset's
        default and the row scope for one call: the parity test uses them
        to take a difference apart. Each row is ChemDuck's, plus the keys
        ReportStore.aggregate returns (value, detect, date, unit,
        at_limit), so a caller can set the two side by side.
        """
        if non_detect not in DISPLAY_FACTOR:
            raise ValueError("non-detect choice %r has no ChemDuck display factor "
                             "(docs/chemduck-adoption-plan.md, decision 1)" % (non_detect,))
        start, end, codes = self._scope
        params = {"p_event_agg": EVENT_AGG[how],
                  "p_display_factor": DISPLAY_FACTOR[non_detect],
                  "p_start_date": start, "p_end_date": end,
                  "p_method_class": method_class,
                  "p_locations": list(locations) if locations is not None else None,
                  "p_reportable_only": reportable_only}
        matrices = [str(m) for m in (matrix or ()) if str(m or "").strip()]
        if len(matrices) == 1:
            params["p_matrix"] = matrices[0]
        elif matrices:
            params["p_matrices"] = matrices
        fractions = [str(f).upper() for f in (fraction or ()) if str(f or "").strip()]
        if len(fractions) > 1:
            raise ValueError("a map is one fraction; ChemDuck is asked for one at a time")
        if fractions:
            params["p_fraction"] = FRACTION_NAME.get(fractions[0], fractions[0])
        types = sample_types if sample_types is not None else (
            sorted(codes) if codes else None)
        if types is not None:
            params["p_sample_types"] = list(types)
        given = {k: v for k, v in params.items() if v is not None}
        # The whole point key, so the order is the data's and not the
        # threads': two points at one depth (a field and a lab method class)
        # otherwise came back in either order from one run to the next.
        sql = ("FROM aggregate_points(?%s) ORDER BY location_id, matrix, top_depth, "
               "bottom_depth, position, fraction, meas_basis, method_class"
               % "".join(", %s := ?" % k for k in given))
        cursor = self.cur.execute(sql, [chemical] + list(given.values()))
        names = [d[0] for d in cursor.description]
        out = []
        for row in cursor.fetchall():
            point = dict(zip(names, row))
            point.update(_plugin_keys(point))
            out.append(point)
        return out

    def by_location(self, *args, **kwargs):
        """points(), grouped: {location: [points]}, in ChemDuck's order."""
        grouped = {}
        for point in self.points(*args, **kwargs):
            grouped.setdefault(point["location_id"], []).append(point)
        return grouped


def _plugin_keys(point):
    detected = point.get("detected")
    value = point.get("result")
    return {
        "location": point.get("location_id"),
        "value": value,
        "detect": "" if value is None or detected is None else ("Y" if detected else "N"),
        # A selected non-detect is drawn at its limit, so it is at_limit,
        # as the store marks it: the flag and the limit say the same thing.
        "at_limit": bool(value is not None and detected is False),
        "date": point.get("rep_sample_datetime") or point.get("rep_sample_date"),
        "unit": point.get("units") or "",
    }
