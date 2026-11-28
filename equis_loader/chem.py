"""The plugin's one door to ChemDuck (docs/chemduck-adoption-plan.md).

Every chemistry question that moves to ChemDuck goes through here, and
nothing else in the plugin writes ChemDuck SQL. Phase 1 is a shadow: the
adapter opens ChemDuck beside the plugin's own store and answers the map's
question, and only the parity test (tools/test_chemduck_parity.py) and a
debug log (chem_parity.shadow_map, when the setting `chemduck_shadow` is
on) read the answer. The panel still maps ReportStore's.

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
import csv
import os
import tempfile

from . import chemduck
from . import report_data as rd

SCHEMA = "chemduck"
# DuckDB threads for the shadow's own database (Chem.shadow).
SHADOW_THREADS = 4
# Where a publish puts ChemDuck's sidecars, beside manifest.json: the
# pipeline's chemduck_sidecars.FOLDER, and integrator's.
SIDECARS = "chemduck"
# The sidecar holding point_counts' rows counted once for every analyte
# (ChemDuck's point_catalog_rows): what the footer and the pickers read when
# the panel's scope is one it keeps. A publish older than it has none, and
# every count is then the live point_counts, as before.
CATALOG = "point_catalog"


class SidecarsStale(Exception):
    """A source's sidecars describe another manifest than the one it has."""


class _Absent(Exception):
    """A file the source does not have (no sidecars published there)."""


def _read_bytes(url, timeout=60):
    """A source file's bytes: a local path, or an http URL. Raises _Absent
    for one that isn't there, and anything else as it comes (a refused
    token, a network down) - those are errors, not 'no sidecars'."""
    if "://" not in url:
        try:
            with open(url, "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            raise _Absent(url)
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise _Absent(url)
        raise

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


def _no_print(*_args, **_kwargs):
    pass


# ChemDuck prints a line per SQL file it loads and per load step; in QGIS
# that lands in the Python console. Until it logs through `logging` (the
# plan's open item 4), its modules get a print of their own that says
# nothing: a module's global `print` is found before the builtin, so this
# silences ChemDuck and nothing else, on whatever thread it runs. Not
# contextlib.redirect_stdout, as this did until 2026-11-25: that swaps
# sys.stdout for the WHOLE PROCESS, and the shadow loads on a worker, so
# whatever the main thread printed meanwhile - the Python console's own
# output - vanished with ChemDuck's. tools/test_chem_shadow_ui.py found it
# when two of its own checks never appeared. (ChemDuck's publish still
# redirects stdout itself; the plugin doesn't run publish.)
chemduck.load_data.print = _no_print
chemduck.arii_hybrid.print = _no_print


def _quiet(fn, *args, **kwargs):
    """fn, with ChemDuck silenced (above). Kept as the one door callers use,
    so a later ChemDuck that logs needs a change here only."""
    return fn(*args, **kwargs)


class Chem(object):
    """ChemDuck opened in one schema of a caller's DuckDB connection."""

    def __init__(self, con, schema=SCHEMA):
        self.con = con
        self.schema = schema
        self.cur = chemduck.schema_cursor(con, schema)
        self._scope = rd.row_scope(None, None, None)
        # Whether point_catalog is here to read (a publish that wrote it, or
        # a load that built it), and its per-variable summary by sample-type
        # scope: the catalog never changes under an open Chem, so neither
        # does a summary of it.
        self.catalog = False
        self._summaries = {}

    @classmethod
    def load(cls, con, source, mapping=None, schema=SCHEMA):
        """ChemDuck over rows the plugin holds: a path (CSV, parquet, a
        manifest.json) or a DuckDB relation. A relation is read through its
        Arrow stream, which needs pyarrow (QGIS 4.2.3's Python has it; the
        plugin's own DuckDB bundle does not)."""
        _quiet(chemduck.load_source, con, source,
               chemduck.load_mapping(mapping or chemduck.DEFAULT_MAPPING), schema=schema)
        chem = cls(con, schema)
        chem.build_catalog()
        return chem

    @classmethod
    def open_published(cls, con, folder, partition_files, schema=SCHEMA):
        """ChemDuck over a parquet facility through the sidecars its
        publish wrote, the way Chemrooms opens it in the browser."""
        sidecars = _quiet(chemduck.open_consumer, con, folder, partition_files, schema=schema)
        chem = cls(con, schema)
        chem.catalog = CATALOG in ((sidecars or {}).get("tables") or {})
        return chem

    def build_catalog(self):
        """Make point_catalog from the rows loaded, as a publish writes it:
        one pass over every result, so the counts after it read a table.

        For a load (Chem.load), which has no sidecars to bring one. A
        ChemDuck without point_catalog_rows (older than the catalog) leaves
        `catalog` False and every count live."""
        try:
            self.cur.execute("CREATE OR REPLACE TABLE %s AS FROM point_catalog_rows()" % CATALOG)
        except Exception:                      # noqa: BLE001 - no catalog: counts stay live
            self.catalog = False
            return False
        self.catalog = True
        self._summaries = {}
        return True

    @classmethod
    def shadow(cls, source, path, threads=SHADOW_THREADS):
        """ChemDuck over the panel's rows in a DuckDB file of its own: the
        phase 1 shadow, built off the UI thread when the debug setting is on.

        source: a ReportTable (a live report, a saved one, an agent's
        output), written to a CSV as the report gave it, so ChemDuck maps
        the report's own columns and not the store's parsed ones; or a
        LOCAL manifest.json, read as the pipeline published it.

        WHY ITS OWN DATABASE, not schema `chemduck` of the store's
        connection as the plan's design has it. The shadow watches; it must
        not be able to slow or break what it watches. In the store's
        database ChemDuck's load would take the store's connection for the
        length of a load (89 s for 557,691 rows, 2026-10-29), its threads
        setting would be the store's (decision 8, not taken), and a pending
        result on either side hides the other's tables (CLAUDE.md). Here a
        failure closes a file nobody else reads. The design's one database
        is phase 2's, once ChemDuck answers for the map.

        threads: this database's alone, so it can be ChemDuck's measured
        best (4, docs/duckdb-thread-cap.md in ChemDuck) without taking
        decision 8 for the store.
        """
        from .store import duckdb_module
        duckdb = duckdb_module()
        if os.path.exists(path):
            # A file from a previous session, or a cancelled build: never
            # merged into (ChemDuck refuses a second load into a schema).
            os.unlink(path)
        con = duckdb.connect(path)
        try:
            if threads:
                con.execute("SET threads = %d" % int(threads))
            if isinstance(source, (str, os.PathLike)):
                chem = cls.load(con, str(source))
            else:
                chem = cls._load_table(con, source)
        except BaseException:
            con.close()
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        chem.path = path
        return chem

    @classmethod
    def shadow_published(cls, source, manifest, path, folder, threads=SHADOW_THREADS,
                         timeout=60):
        """ChemDuck over a published parquet source through its sidecars, or
        None when the source has none. The pipeline's `chemduck_sidecars`
        step and integrator's `release --arii` publish them in `chemduck/`
        beside manifest.json (the plan's phase 6).

        source: the address the store opened (a local folder, or an http URL
        whose query is the SAS token); manifest: the manifest the store
        parsed. The sidecars - a few tables derived from the partitions, and
        the SQL that made them - are copied into `folder`; the results stay
        where they are, read from the partitions by `results` as a view. On
        the first facility that is about a second against minutes for
        load_source's copy of every row (adoption plan, open item 2).

        Raises SidecarsStale, opening nothing, when the sidecars were built
        from another manifest than this one: sidecars.json records its
        SHA-256, and a new publish of the data with the sidecar step failed
        leaves the old ones in place (pipeline/README.md). Over the old
        sidecars the new partitions' results would join samples that are
        not there, and answer with no error.

        The partition URLs, token and all, become part of the `results`
        view in the shadow's database file: a local file, removed on close,
        holding the token this person's settings already hold.
        """
        import hashlib
        import json
        import shutil
        from .store import ReportStore, duckdb_module
        duckdb = duckdb_module()
        base = ReportStore._local_path(source) or source

        def where(relative):
            return ReportStore._join(base, relative)

        try:
            sidecars = json.loads(_read_bytes(where(SIDECARS + "/sidecars.json"), timeout))
        except _Absent:
            return None
        raw = _read_bytes(where(ReportStore.MANIFEST_NAME), timeout)
        if json.loads(raw) != manifest:
            raise SidecarsStale("the manifest changed since the source opened; reopen it")
        recorded = (sidecars.get("source_manifest") or {}).get("sha256")
        if recorded != "sha256:" + hashlib.sha256(raw).hexdigest():
            raise SidecarsStale(
                "the sidecars were built from another manifest (%s), not the one this "
                "source publishes now: its sidecar step failed or has not run since"
                % ("release %s" % sidecars["release"] if sidecars.get("release")
                   else "published %s" % (sidecars.get("generated") or "at an unknown time")))

        if os.path.isdir(folder):
            shutil.rmtree(folder)
        os.makedirs(os.path.join(folder, "sql"))
        try:
            source_sql = json.loads(_read_bytes(where(SIDECARS + "/sql/SOURCE.json"), timeout))
            wanted = ([info["file"] for info in sidecars["tables"].values()]
                      + ["aggregation_rules.parquet", sidecars["results_view"]["file"],
                         "sql/SOURCE.json"]
                      + ["sql/" + name for name in source_sql["files"]])
            for relative in wanted:
                data = _read_bytes(where(SIDECARS + "/" + relative), timeout)
                with open(os.path.join(folder, *relative.split("/")), "wb") as handle:
                    handle.write(data)
            with open(os.path.join(folder, "sidecars.json"), "w", encoding="utf-8") as handle:
                json.dump(sidecars, handle)
            # In the order the results view was generated for, which is the
            # manifest's; the sidecars name it, so take it from them.
            partitions = [where(part["path"])
                          for part in sidecars["source_manifest"]["partitions"]]
            if os.path.exists(path):
                os.unlink(path)
            con = duckdb.connect(path)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        try:
            if threads:
                con.execute("SET threads = %d" % int(threads))
            if "://" in base:
                # Every call reads the partitions' footers over http; with
                # the HEAD answers cached a warm call sends no request
                # (ReportStore.from_manifest). Safe for the same reason it is
                # there: a new publish replaces the shadow (dialog's reopen).
                con.execute("SET enable_http_metadata_cache = true")
            chem = cls.open_published(con, folder, partitions)
        except BaseException:
            con.close()
            for name in (path, path + ".wal"):
                try:
                    os.unlink(name)
                except OSError:
                    pass
            shutil.rmtree(folder, ignore_errors=True)
            raise
        chem.path = path
        chem.folder = folder
        chem.sidecars = sidecars
        return chem

    @classmethod
    def _load_table(cls, con, table):
        # Through a CSV and not a relation: a relation is read through its
        # Arrow stream, which needs pyarrow, and the plugin's own DuckDB
        # bundle has none. ChemDuck reads a CSV as text (all_varchar), so
        # the report's cells reach its mapping as the report wrote them.
        handle, csv_path = tempfile.mkstemp(suffix=".csv", prefix="equis_chem_")
        os.close(handle)
        try:
            with open(csv_path, "w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(table.headers)
                writer.writerows(table.rows)
            return cls.load(con, csv_path)
        finally:
            try:
                os.unlink(csv_path)
            except OSError:
                pass

    def close(self):
        """Close the connection, and remove the shadow's file if it has one."""
        try:
            self.cur.close()
        except Exception:                      # noqa: BLE001 - closing anyway
            pass
        self.con.close()
        path = getattr(self, "path", None)
        if path:
            for name in (path, path + ".wal"):
                try:
                    os.unlink(name)
                except OSError:
                    pass
        folder = getattr(self, "folder", None)
        if folder:
            import shutil
            shutil.rmtree(folder, ignore_errors=True)

    def set_row_scope(self, start=None, end=None, sample_types=None,
                      depth=None, units=None):
        """The panel's row scope, the same tuple ReportStore keeps.

        A sample-type scope the panel has not set is ChemDuck's default:
        normal samples only (typed N or untyped). The plugin's default is
        every type, so a QC sample reaches the store's map and not this
        one: the plan's decision 5, an intended difference.

        Depth and units go to ChemDuck as its row scope (p_depth_min,
        p_depth_max, p_depth_units, p_reported_units; ChemDuck #18), applied
        before anything is selected, as the store applies them. Filtering
        its points afterwards would not be the same filter: a point's
        winner is chosen across every unit, and a whole-interval bound
        needs the depths converted first. Until #18 they were refused here
        (the plan's open item 6).

        ChemDuck compares a result's unit as its source reported it
        (REPORT_RESULT_UNIT in its ARII mapping); the store coalesces
        RESULT_UNIT first. On a row where the two differ, the same tick can
        keep different rows; the parity test says where."""
        scope = rd.row_scope(start, end, sample_types, depth, units)
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
        params = self._params(matrix, fraction, method_class, locations, reportable_only,
                              sample_types)
        params.update(p_event_agg=EVENT_AGG[how],
                      p_display_factor=self._factor(non_detect))
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

    @staticmethod
    def _factor(non_detect):
        if non_detect not in DISPLAY_FACTOR:
            raise ValueError("non-detect choice %r has no ChemDuck display factor "
                             "(docs/chemduck-adoption-plan.md, decision 1)" % (non_detect,))
        return DISPLAY_FACTOR[non_detect]

    def _params(self, matrix=rd.ALL_MATRICES, fraction=rd.ANY_FRACTION, method_class=None,
                locations=None, reportable_only=None, sample_types=None):
        """The macro parameters every engine call shares: the row scope, and
        the panel's matrix, fraction and location choices in ChemDuck's
        terms. One place, so a map, its footer count and its series are
        asked the same question."""
        start, end, codes, depth, units = self._scope
        params = {"p_start_date": start, "p_end_date": end,
                  "p_method_class": method_class,
                  "p_locations": list(locations) if locations is not None else None,
                  "p_reportable_only": reportable_only}
        if depth is not None:
            # (top, bottom, unit), either bound None for an open end; the
            # unit turns the filter on, so it goes even with both open.
            params["p_depth_min"], params["p_depth_max"], params["p_depth_units"] = depth
        if units:
            params["p_reported_units"] = list(units)
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
        return params

    def _call(self, macro, chemical, params, order=""):
        given = {k: v for k, v in params.items() if v is not None}
        sql = "FROM %s(?%s)%s" % (macro, "".join(", %s := ?" % k for k in given), order)
        cursor = self.cur.execute(sql, [chemical] + list(given.values()))
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def counts(self, chemical, matrix=rd.ALL_MATRICES, fraction=rd.ANY_FRACTION):
        """point_counts: (locations, results) for the panel's footer, by the
        same scope a map would select from (the store's count_for)."""
        rows = self._call("point_counts", chemical, self._params(matrix, fraction))
        row = rows[0] if rows else {}
        return int(row.get("n_locations") or 0), int(row.get("n_results") or 0)

    # ------------------------------------------------- the published counts

    def catalog_reason(self, matrix=rd.ALL_MATRICES):
        """Why point_catalog can't answer under the row scope set now (and
        this matrix choice), or None when it can.

        The catalog keeps sample types and the reportable flag, and counts
        by location; it keeps no dates, depths or units, and combining
        matrices is ChemDuck's matrix-class rule, which only a live call
        applies. Any of those is a live call."""
        if not self.catalog:
            return "no published counts (point_catalog) for this source"
        start, end, _codes, depth, units = self._scope
        if start is not None or end is not None:
            return "a date range"
        if depth is not None:
            return "a depth filter"
        if units:
            return "a unit filter"
        if len([m for m in (matrix or ()) if str(m or "").strip()]) > 1:
            return "several matrices"
        return None

    def _catalog_params(self, matrix=rd.ALL_MATRICES, fraction=rd.ANY_FRACTION):
        """_params, kept to what the catalog's readers take. A parameter left
        over is a filter they cannot apply: refused here, never dropped
        (ChemDuck would refuse it too, as an unknown parameter)."""
        params = self._params(matrix, fraction)
        kept = {k: params.get(k) for k in ("p_matrix", "p_fraction", "p_sample_types")}
        left = sorted(k for k, v in params.items() if v is not None and k not in kept)
        if left:
            raise ValueError("the published counts cannot apply %s" % ", ".join(left))
        return kept

    def catalog_counts(self, chemical, matrix=rd.ALL_MATRICES, fraction=rd.ANY_FRACTION):
        """counts(), read off point_catalog: the same (locations, results)
        for the scopes it keeps (ChemDuck's test_point_catalog holds the two
        equal), refusing what point_counts refuses. Ask catalog_reason
        first."""
        rows = self._call("point_catalog_counts", chemical,
                          self._catalog_params(matrix, fraction))
        row = rows[0] if rows else {}
        return int(row.get("n_locations") or 0), int(row.get("n_results") or 0)

    def catalog_summary(self):
        """point_catalog_summary under the sample types set now: a row per
        (analyte, CAS, matrix, fraction) with its results and locations,
        for the pickers. Cached by scope, since the catalog never changes
        under an open Chem."""
        params = {k: v for k, v in self._catalog_params().items()
                  if k == "p_sample_types" and v is not None}
        key = tuple(params.get("p_sample_types") or ()) if params else None
        if key not in self._summaries:
            sql = "FROM point_catalog_summary(%s)" % ", ".join("%s := ?" % k for k in params)
            cursor = self.cur.execute(sql, list(params.values()))
            names = [d[0] for d in cursor.description]
            self._summaries[key] = [dict(zip(names, row)) for row in cursor.fetchall()]
        return self._summaries[key]

    def series(self, locations, chemical, non_detect=rd.ND_HALF, matrix=rd.ALL_MATRICES,
               fraction=rd.ANY_FRACTION):
        """point_series for many locations at once: {location: [rows]},
        each row the store's series keys (date, value, unit, detect,
        at_limit) plus ChemDuck's own, oldest first; an undated sample is
        left out, as the store leaves it out (it can't be plotted).

        A location sampled at several points (depths, method classes) has
        each point's samples, merged by date: the store's series mixes them
        the same way, and plotting them apart is the plan's phase 3.
        point_series takes one matrix, so several are asked one at a time.
        """
        matrices = [str(m) for m in (matrix or ()) if str(m or "").strip()] or [None]
        out = {}
        for one in matrices:
            params = self._params((one,) if one else None, fraction, locations=locations)
            params["p_display_factor"] = self._factor(non_detect)
            for row in self._call("point_series", chemical, params):
                if row.get("sample_date") is None or row.get("result") is None:
                    continue
                detected = row.get("detected")
                row.update({
                    "date": row.get("sample_datetime") or row.get("sample_date"),
                    "value": row.get("result"),
                    "unit": row.get("units") or "",
                    "detect": "" if detected is None else ("Y" if detected else "N"),
                    "at_limit": detected is False,
                })
                out.setdefault(row["location_id"], []).append(row)
        for rows in out.values():
            rows.sort(key=lambda r: (str(r["sample_date"]), str(r.get("top_depth")),
                                     str(r.get("method_class"))))
        if locations is not None:
            for code in locations:
                out.setdefault(str(code), [])
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
