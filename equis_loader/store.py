"""The report, in DuckDB, with report_data's rules as SQL.

Phase 1 of docs/vision-linked-views.md, recommendation R2. The point is NOT
that a list of rows becomes a table - that part is trivial. Most of
report_data.py is domain judgement:

  * a non-detect leaves RESULT_NUMERIC empty and carries only the limit it
    fell below, so the value falls back to REPORT_RESULT_VALUE and then the
    detection-limit columns;
  * DETECT_FLAG and "the number shown is a limit" are different facts;
  * a chemical is identified by CAS RN when the report has one and by name
    otherwise;
  * a filter on a column the report does not have matches everything, because
    refusing every row is a worse failure than ignoring a filter the data
    cannot express;
  * ties are broken by the order rows arrived in.

Losing any of that would be a regression no amount of query speed pays for,
so every rule above is reproduced here and tools/test_store_parity.py holds
the two implementations to identical answers.

No QGIS imports, and the database path comes from the caller, exactly as
cache.py does - so this is testable with a plain interpreter.

WHAT IS PARSED IN PYTHON AND WHY. Dates and numbers are parsed at ingest
using report_data's own parse_date and parse_number rather than by SQL casts.
The report carries eight date formats and numbers written "<0.5", and
re-expressing those in SQL would be a second implementation of a rule that
already exists - the one kind of divergence a parity test cannot catch,
because it would be comparing two things that were never the same. The query
semantics move to SQL; the parsing stays where it was proven.
"""

import csv
import json
import os
import tempfile
import time as _time
import urllib.parse
import urllib.request

from . import report_data as rd

# Columns the store holds, and where each comes from in the source report.
# Order matters: it is the INSERT order.
# The column read_parquet adds for each row's source file. Named so that it
# cannot collide with anything a report carries.
FILE_COLUMN = "__equis_file"

FIELDS = (
    ("row_id", "INTEGER"),
    ("location", "VARCHAR"),
    ("chemical", "VARCHAR"),
    ("cas_rn", "VARCHAR"),
    ("sample_date", "TIMESTAMP"),
    ("value", "DOUBLE"),
    ("reported", "DOUBLE"),
    ("detect_limit", "DOUBLE"),
    ("unit", "VARCHAR"),
    ("detect", "VARCHAR"),
    ("matrix", "VARCHAR"),
    ("fraction", "VARCHAR"),
    ("method", "VARCHAR"),
    ("sample_type", "VARCHAR"),
    ("start_depth", "DOUBLE"),
    ("end_depth", "DOUBLE"),
    ("depth_unit", "VARCHAR"),
    ("custom_1", "VARCHAR"),
    # The date cell as it arrived. filtered_rows sorts an
    # unparseable date as its own text rather than dropping the
    # row from an export, so the original has to survive.
    ("date_text", "VARCHAR"),
    # For the pivot (pivot.py). Appended, not interleaved: _csv_row is
    # positional and must stay in step with this list.
    ("sample", "VARCHAR"),
    ("test_type", "VARCHAR"),
    ("qual_interpreted", "VARCHAR"),
    ("qual_lab", "VARCHAR"),
    ("lab", "VARCHAR"),
)

DETECT_TRUE = ("Y", "YES", "TRUE", "1")

# Which non-detect convention reads which column of the view.
ND_COLUMN = {
    rd.ND_HALF: "value_half",
    rd.ND_FULL: "value_full",
    rd.ND_EXCLUDE: "value_exclude",
}


def duckdb_module():
    """Import duckdb, activating the plugin's bundle first if there is one."""
    try:
        from . import vendor
        vendor.activate()
    except Exception:
        pass
    import duckdb
    return duckdb


class StoreError(RuntimeError):
    pass


def build_store_file(table, path, with_raw=False):
    """Build a store into `path` and close it. Returns `path`.

    Deliberately a plain function with no QGIS in sight: this is what a
    background task calls, and keeping it here means the part that does the
    work can be tested without a task, a thread or a GUI. The task is a
    wrapper around this and nothing else.
    """
    if os.path.exists(path):
        # DuckDB will not open a file written by a different version, and a
        # half-built one from a cancelled run is worse than none.
        os.unlink(path)
    store = ReportStore.from_table(table, path=path, with_raw=with_raw)
    try:
        store.con.execute("CHECKPOINT")
    except Exception:
        # Not fatal: the data is committed, this only flushes the WAL early.
        pass
    store.close()
    return path


class ReportStore(object):
    """A ReportTable's rows in DuckDB, answering the same questions."""

    def __init__(self, connection, present, headers=None, has_raw=False):
        self.con = connection
        # Which source columns existed. A filter on a column the report does
        # not have must match everything, and in the store that column is all
        # NULL - so the predicate has to be skipped rather than evaluated.
        self.present = set(present or ())
        self.headers = list(headers or [])
        # Whether the report's own columns were kept. Only filtered_rows
        # needs them, and a caller that never exports can skip the cost.
        self.has_raw = has_raw
        # See set_row_scope. A reopened store's view was written unscoped.
        self._scope = rd.NO_ROW_SCOPE

    @property
    def missing_for_analysis(self):
        """ReportTable.missing_for_analysis, from the columns this store has."""
        return rd.missing_required(lambda key: key in self.present)

    # ------------------------------------------------------------- loading

    @classmethod
    def from_table(cls, table, path=":memory:", with_raw=False):
        """Build from a ReportTable. `path` is the caller's, like cache.py.

        `with_raw` keeps the report's own columns for export, and is OFF by
        default. Re-measured 2026-09-23 on a real 222,571-row x 22-column
        report, after the parse_date fix that both paths were paying for:

            with_raw=False    ingest  8.2 s     4.3 MB on disk
            with_raw=True     ingest 10.2 s    10.0 MB on disk

        So the premium is ~1.2x, not the 2x an earlier synthetic benchmark
        showed. It stays off anyway, for a better reason than cost: the panel
        keeps its ReportTable resident whenever a store exists, and
        filtered_rows() reads the table directly. Nothing needs the raw
        columns here. Settled as 1b.5 in docs/implementation-plan.md.

        Kept rather than deleted because it is the only way a store can
        answer filtered_rows() at all, which matters if ReportTable is ever
        dropped from memory after ingest.
        """
        duckdb = duckdb_module()
        con = duckdb.connect(path)
        present = {key for key in rd.COLUMNS if table.column(key) >= 0}

        columns = ", ".join("%s %s" % (name, kind) for name, kind in FIELDS)
        con.execute("DROP TABLE IF EXISTS results")
        con.execute("CREATE TABLE results (%s)" % columns)

        # Written to a CSV and COPYed in rather than inserted row by row.
        # executemany binds one row at a time through the Python API: 42 s for
        # 50,000 rows, which is 6.5 minutes at report scale against the 2.9 s
        # index this replaces. COPY does the same work in ~1.1 s. The
        # intermediate is OUR format, not the report's - ISO timestamps and
        # repr'd floats - so nothing ambiguous is re-parsed; the report's
        # eight date formats and "<0.5" numbers were already resolved above.
        handle, csv_path = tempfile.mkstemp(suffix=".csv", prefix="equis_")
        os.close(handle)
        written = 0
        try:
            with open(csv_path, "w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                for position, row in enumerate(table.rows):
                    writer.writerow(cls._csv_row(table, position, row))
                    written += 1
            if written:
                # NULLSTR '': an empty cell becomes NULL. Safe because every
                # predicate in the views coalesces text columns to '', so NULL
                # and empty are the same thing to every query - and the parity
                # test exercises reports full of empty cells.
                con.execute(
                    "COPY results FROM ? (FORMAT CSV, HEADER FALSE, "
                    "NULLSTR '', DATEFORMAT '%Y-%m-%d', "
                    "TIMESTAMPFORMAT '%Y-%m-%dT%H:%M:%S')", [csv_path])
        finally:
            try:
                os.unlink(csv_path)
            except OSError:
                pass

        if with_raw:
            cls._load_raw(con, table)

        store = cls(con, present, table.headers, has_raw=with_raw)
        store._create_views()
        store._write_meta()
        return store

    def _write_meta(self):
        """Make the file self-describing.

        `present` and `headers` are not recoverable from the data - a report
        with no matrix column and one where every matrix cell is blank look
        identical once loaded, and they must behave differently, because a
        filter on a missing column matches everything. So they are written
        down, and open() reads them back.
        """
        self.con.execute("DROP TABLE IF EXISTS meta")
        self.con.execute("CREATE TABLE meta (key VARCHAR, value VARCHAR)")
        rows = [("header", h) for h in self.headers]
        rows += [("present", key) for key in sorted(self.present)]
        rows += [("has_raw", "1" if self.has_raw else "0")]
        if rows:
            self.con.executemany("INSERT INTO meta VALUES (?, ?)", rows)

    MANIFEST_NAME = "manifest.json"

    @classmethod
    def from_manifest(cls, url, path=":memory:", timeout=30):
        """A store over parquet published at `url`, read through its manifest.

        `url` is the DIRECTORY the partitions live under; the manifest is
        fetched from `<url>/manifest.json`. It names the files, so nothing
        here globs.

        WHY A MANIFEST RATHER THAN A GLOB. DuckDB rejects globs over generic
        HTTP outright - "Globs (`*`) for generic HTTP file is are not
        supported" - so a glob would force `az://` and credentials on what is
        otherwise a plain signed URL. Enumerating the files also removes the
        LIST round trip, and lets the caller check it got what the publisher
        said was there, which a glob cannot.

        RANGE REQUESTS DO THE REAL WORK. Measured 2026-09-23 against the real
        41.1 MiB WS/N partition over plain HTTP: count(*) fetched 0.25 MiB in
        5 requests (0.6% of the file) and a single-analyte filter fetched
        under 0.01 MiB in 9. That is Parquet's footer plus row-group
        statistics, and it happens over https:// exactly as over az:// - the
        scheme buys globbing and auth, not speed.

        The manifest is data from elsewhere, so it is validated rather than
        trusted: a path that escapes the source directory is refused.
        """
        # A `file:` URL becomes a plain path HERE, once, before anything
        # joins onto it - so the manifest fetch and every partition path
        # built below are local paths, which _fetch and DuckDB both already
        # handle. Doing it later would leave the partition list as URLs.
        url = cls._local_path(url) or url
        text = cls._fetch(cls._join(url, cls.MANIFEST_NAME), timeout)
        try:
            manifest = json.loads(text)
        except ValueError as exc:
            raise StoreError("%s is not valid JSON: %s"
                             % (cls._join(url, cls.MANIFEST_NAME), exc))
        parts = manifest.get("partitions")
        if not parts:
            raise StoreError("manifest at %r lists no partitions" % url)

        files = []
        for entry in parts:
            relative = (entry or {}).get("path")
            if not relative:
                raise StoreError("a manifest partition has no `path`")
            # A published manifest is fetched from the network. A path with
            # `..` in it would make the store read something the publisher
            # did not offer, so it is refused rather than normalised.
            normalised = relative.replace("\\", "/")
            # Absolute paths are refused as well as `..`: a manifest is
            # fetched from the network, and "C:/..." or "/etc/..." would make
            # the store read something the publisher never offered. A
            # published path is always relative to the manifest's own
            # directory. (Windows drive letters do not start with "/", so
            # they need naming explicitly.)
            absolute = (normalised.startswith("/")
                        or (len(normalised) > 1 and normalised[1] == ":")
                        or "://" in normalised)
            if absolute or ".." in normalised.split("/"):
                raise StoreError(
                    "manifest path %r is not relative to the source; a "
                    "published manifest names paths under its own directory."
                    % relative)
            files.append(cls._join(url, relative))

        store = cls.from_parquet(files, path=path)
        store.manifest = manifest
        store.source_url = url
        # The publisher's own count, checked rather than displayed. A
        # truncated upload or a half-written republish reads as a smaller
        # facility, which is the silent-wrong-number failure this codebase
        # keeps being bitten by - see _refuse_duplicated_rows.
        declared = manifest.get("rows")
        if isinstance(declared, int):
            actual = store.count()
            if actual != declared:
                store.close()
                raise StoreError(
                    "the manifest says %s rows but the parquet holds %s; the "
                    "publish may be incomplete."
                    % (format(declared, ",d"), format(actual, ",d")))
        return store

    def warm(self, note=None):
        """Read the columns the panel filters on, once, deliberately.

        `note` is called with a short line per step, so a caller running
        this off the UI thread can say what it is doing. It is the slow
        part of opening - 2.3 s of the 3.1 s locally - so a progress
        message that names it is the difference between "working" and
        "stuck".

        FIRST TOUCH IS THE WHOLE COST. Measured 2026-09-23 on the real
        3,452,146-row facility: the first query that reads a column takes
        ~1.2 s while every later one takes ~0.08 s, because 110 MB of zstd
        parquet has to be read and decompressed once. It is not the filter -
        name and CAS cost the same cold (1.18 s vs 1.20 s) and the same warm.

        Left lazy, that cost lands on the first analyte click, several
        queries over - which is what made the panel appear to hang for ten
        seconds on a selection that should be instant. Paid here it lands
        during the open, which already shows progress and is already the
        moment the user expects to wait.

        Returns the seconds it took, so the caller can log what it bought.
        """
        started = _time.time()
        try:
            # The panel's OWN queries, run once against a chemical that
            # exists. Anything less specific leaves the first real call
            # paying ~2 s: measured that the cost follows whichever
            # chemical is asked FIRST, not which one - Lead took 2.07 s
            # when asked first and 0.14 s when asked second, and Arsenic
            # exactly the reverse. So the way to pay it here is to make
            # the same calls, not to approximate them.
            def say(message):
                if note is not None:
                    try:
                        note(message)
                    except Exception:           # noqa: BLE001
                        pass

            first = self.con.execute(
                "SELECT chemical FROM v_results "
                "WHERE coalesce(chemical, '') <> '' LIMIT 1").fetchone()
            if first:
                say("Reading analytic methods…")
                self.methods(first[0])
                say("Reading fractions…")
                self.fractions_for(first[0])
                say("Warming DuckDB…")
                self.aggregate(first[0], how=rd.MAXIMUM)
            # A GROUP BY with a WHERE, not a bare count: the panel's queries
            # all have that shape, and a plain count(column) leaves them
            # still paying ~2 s on the first real one. Grouping every column
            # the pickers offer forces the view's own work - the non-detect
            # ladders, the detected flag - to be materialised once too.
            self.con.execute("""
                SELECT count(*) FROM (
                    SELECT chemical, cas_rn, method, matrix, fraction,
                           count(*) AS n, max(value_half) AS v,
                           max(sample_date) AS d, max(location) AS l,
                           max(unit) AS u, max(detected) AS t
                    FROM v_results
                    WHERE coalesce(chemical, '') <> ''
                    GROUP BY 1, 2, 3, 4, 5
                )
            """).fetchone()
        except Exception:                       # noqa: BLE001
            # A warm-up is an optimisation. Failing it must never cost the
            # caller the store it just opened.
            pass
        return _time.time() - started

    def location_features(self, wkid=4326):
        """Location points as Esri FeatureSet pages, from the parquet itself.

        THE PARALLEL TO THE FEATURE SERVER. On the live path the panel loads
        a facility's location points from EQuIS's feature service and keeps
        them as `[(feature_set, features)]`, which build_feature_layer,
        build_joined_layer and build_result_layer all consume. A published
        parquet carries LATITUDE and LONGITUDE on every row, so the same
        pages can be produced here and every one of those builders works
        unchanged - which is the point. A second layer-building path written
        for parquet would be the divergence that no parity test can catch.

        One row per distinct location, not per result: 3.45 million rows
        collapse to 906 locations in 0.29 s. A location whose coordinates are
        missing is still returned, with no geometry, exactly as the feature
        service returns unplaced locations - the panel already counts and
        reports those, and dropping them here would make them invisible.
        """
        code = self._raw_alias("SYS_LOC_CODE")
        if code is None:
            return []
        latitude = self._raw_alias("LATITUDE")
        longitude = self._raw_alias("LONGITUDE")
        if latitude is None or longitude is None:
            return []
        name = self._raw_alias("LOC_NAME")

        # A COUNT PER MATRIX, not just a total. 272 of this facility's 906
        # locations carry more than one matrix, and a single `results`
        # number hides that completely - a location with 13 tailings
        # results and one with 13 groundwater results look identical. With
        # a column each they can be styled, filtered and labelled apart
        # before any chemical is chosen. Measured at 0.31 s for the whole
        # pivot over 3.45 million rows, so it is not worth making optional.
        #
        # The matrices come from the DATA, never a hardcoded list: another
        # facility has different ones, and inventing columns for matrices
        # that are not there would offer filters that match nothing.
        matrix = self._raw_alias("MATRIX_CODE")
        matrices = []
        if matrix is not None:
            matrices = [row[0] for row in self.con.execute(
                "SELECT DISTINCT upper(CAST(%s AS VARCHAR)) FROM raw "
                "WHERE coalesce(CAST(%s AS VARCHAR), '') <> '' ORDER BY 1"
                % (matrix, matrix)).fetchall() if row[0]]

        counts = "".join(
            ", sum(CASE WHEN upper(CAST(%s AS VARCHAR)) = '%s' THEN 1 ELSE 0 END)"
            % (matrix, code.replace("'", "''")) for code in matrices)
        rows = self.con.execute("""
            SELECT %(code)s AS code,
                   any_value(%(name)s) AS name,
                   any_value(%(lat)s) AS latitude,
                   any_value(%(lon)s) AS longitude,
                   count(*) AS results%(counts)s
            FROM raw
            WHERE coalesce(CAST(%(code)s AS VARCHAR), '') <> ''
            GROUP BY 1
            ORDER BY 1
        """ % {"code": code, "name": name or "NULL", "lat": latitude,
               "lon": longitude, "counts": counts}).fetchall()
        if not rows:
            return []

        features = []
        for row in rows:
            location, label, latitude_value, longitude_value, results = row[:5]
            geometry = None
            if latitude_value is not None and longitude_value is not None:
                geometry = {"x": float(longitude_value),
                            "y": float(latitude_value)}
            attributes = {"code": location,
                          "name": label if label is not None else location,
                          "results": int(results)}
            for index, code in enumerate(matrices):
                attributes["count_%s" % code] = int(row[5 + index] or 0)
            features.append({"attributes": attributes, "geometry": geometry})
        feature_set = {
            "geometryType": "esriGeometryPoint",
            "spatialReference": {"wkid": wkid},
            # esriFieldTypeInteger for the count so it sorts and styles as a
            # number rather than as text.
            "fields": [{"name": "code", "type": "esriFieldTypeString"},
                        {"name": "name", "type": "esriFieldTypeString"},
                        {"name": "results", "type": "esriFieldTypeInteger"}]
                       + [{"name": "count_%s" % code,
                           "type": "esriFieldTypeInteger"}
                          for code in matrices],
        }
        return [(feature_set, features)]

    def _raw_alias(self, name):
        """`raw`'s positional alias for a report column, or None.

        `raw` is aliased c0, c1, ... because filtered_rows selects it that
        way, so the report's own names are not bindable there. `headers`
        holds the same order, which makes this a lookup rather than a guess.
        """
        headers = [header.upper() for header in (self.headers or [])]
        try:
            return "c%d" % headers.index(name.upper())
        except ValueError:
            return None

    @staticmethod
    def _local_path(source):
        r"""A `file:` URL as a plain local path, or None if it is not one.

        WHY THIS EXISTS. Only `file:///C:/dir` is strictly correct on
        Windows, but `file://C:\dir` is what people type - pasting a
        Windows path after `file://` is the obvious thing to do, and the
        panel's URL box invites it. urlparse then reads `C:` (or the WHOLE
        backslashed path) as a HOSTNAME and leaves `path` empty, so the
        manifest fetch resolved to `\manifest.json` and failed with
        "The system cannot find the file specified". Reported 2026-09-28.

        Rather than teach _join and _fetch about that, a file URL is turned
        into a path once, here, and everything downstream sees the local
        source it already handles.
        """
        import urllib.parse
        text = str(source)
        if not text.lower().startswith("file:"):
            return None
        parsed = urllib.parse.urlparse(text)
        # netloc holds `C:` for the two-slash form, and the entire
        # backslashed path when no forward slash follows `file://`.
        path = urllib.parse.unquote(parsed.netloc + parsed.path)
        path = path.replace("/", os.sep).replace("\\", os.sep)
        # `/C:/dir` from the correct three-slash form.
        if os.sep == "\\" and len(path) > 2 and path[0] == os.sep and path[2] == ":":
            path = path[1:]
        return path or None

    @staticmethod
    def _join(base, relative):
        """Join a source directory and a manifest-relative path.

        urljoin is wrong here: it treats a base without a trailing slash as a
        FILE and drops its last segment, so `.../facility_a` + `matrix=WS/...`
        would silently become `.../matrix=WS/...` and 404.

        THE QUERY STRING IS THE CREDENTIAL. An Azure SAS URL carries its
        token as `?sp=r&...&sig=...`, so appending a path to the END puts it
        after the signature:

            ...container?sp=r&sig=abc%3D/manifest.json

        Azure then reads a different resource path than the one that was
        signed and answers 403 AuthenticationFailed - which reads exactly
        like a bad token, and cost an afternoon of suspecting one. The path
        goes BEFORE the query, and the query is carried across unchanged.
        """
        relative = str(relative).replace("\\", "/").lstrip("/")
        base = str(base)
        if "://" not in base:
            # A local directory, which is what the tests and an offline copy
            # use. os.path.join keeps this usable without a server.
            return os.path.join(base, *relative.split("/"))
        head, separator, query = base.partition("?")
        joined = head.rstrip("/") + "/" + relative
        return joined + separator + query

    @staticmethod
    def _fetch(url, timeout=30):
        if "://" not in url:
            try:
                with open(url, "r", encoding="utf-8") as handle:
                    return handle.read()
            except OSError as exc:
                raise StoreError("no manifest at %r: %s" % (url, exc))
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            raise StoreError("could not fetch %r: %s" % (url, exc))

    @classmethod
    def from_parquet(cls, source, path=":memory:", hive=True):
        """A store over parquet, with NO INGEST at all.

        `source` is what DuckDB's read_parquet takes: one file, or a glob
        like `data/facility_a/matrix=*/fraction=*/*.parquet` for a whole facility.
        With Hive partitioning on, `matrix=WS/fraction=D/...` in the path
        becomes columns DuckDB can prune whole files on, so the panel's matrix
        and fraction pickers cost nothing to apply.

        Name the partition levels, as above, rather than sweeping with `**`.
        A `**` also reaches any leftover intermediate and counts its rows a
        second time - see `_refuse_duplicated_rows`, which refuses that rather
        than answering it.

        WHY A VIEW RATHER THAN A LOAD. DuckDB reads parquet natively:
        355,440 rows counted in 6 ms and grouped in 19 ms, against 6.7 s to
        ingest the same rows from CSV. Loading them into a table would spend
        that time to arrive at what the file already is.

        THE RULES ARE NOT REWRITTEN. The canonical column names are mapped
        here, and then `_create_views` builds the SAME view it builds over an
        ingested table - the non-detect ladders, the detected flag and the
        three value columns are defined once, in one place. A second copy
        written for parquet is precisely the divergence a parity test cannot
        catch.

        WHAT IS TYPED AND WHAT IS NOT. A slicing-pipeline parquet carries
        its NUMBERS typed - RESULT_NUMERIC and the limits are DOUBLE - so
        those casts are honest. Its DATE is not: EQuIS writes
        `4/24/2025 12:37:00 PM` and the extract keeps the string, so a plain
        CAST raises "invalid timestamp field format". `_parsed_date` handles
        that with the same format list, in the same order, that parse_date
        uses - see there for why one list rather than two.
        """
        duckdb = duckdb_module()
        con = duckdb.connect(path)
        # DuckDB reads a `file:///C:/dir` URL but not the two-slash form
        # people actually type; normalising here means both work, and a
        # plain path is untouched.
        if not isinstance(source, (list, tuple)):
            source = cls._local_path(source) or source
        # Inlined rather than bound: DuckDB refuses a prepared parameter in
        # CREATE VIEW ("this type of statement can't be prepared"). The path
        # is therefore quoted here, doubling any single quote, which is the
        # only escaping a SQL string literal needs.
        if isinstance(source, (list, tuple)):
            # An explicit file list, which is what from_manifest builds. Hive
            # columns still come off the paths - verified: 10 matrix/fraction
            # combos recovered from a list with no glob and no directory
            # listing anywhere.
            literal = "[%s]" % ", ".join(
                "'%s'" % str(one).replace("'", "''") for one in source)
            globbed = False
        else:
            literal = "'%s'" % str(source).replace("'", "''")
            globbed = True
        reader = "read_parquet(%s, hive_partitioning=%d, union_by_name=true)" % (
            literal, 1 if hive else 0)
        if globbed:
            # Only a glob can widen to reach an intermediate; a named list
            # says exactly what it wants, and glob() would not work over
            # plain HTTP anyway.
            cls._refuse_duplicated_rows(con, literal, source)
        # The reader the VIEWS use, which also says where each row came from.
        # `reader` stays as it is for DESCRIBE, so these two columns never
        # reach `headers` and the export keeps the report's own columns.
        scanned = ("read_parquet(%s, hive_partitioning=%d, union_by_name=true, "
                   "filename='%s', file_row_number=true)"
                   % (literal, 1 if hive else 0, FILE_COLUMN))
        row_id, files = cls._row_id_expression(con, source, literal, globbed)
        # What the file actually carries, so `present` is honest: a filter on
        # a column the data lacks must match everything, exactly as it does
        # for a report without that column.
        try:
            described = con.execute(
                "DESCRIBE SELECT * FROM %s" % reader).fetchall()
        except Exception as exc:  # noqa: BLE001
            raise StoreError("could not read parquet at %r: %s" % (source, exc))
        available = {row[0].upper() for row in described}

        selects = []
        present = set()
        for key, kind in FIELDS:
            if key == "row_id":
                # Parquet has no arrival order to preserve, so one is made.
                # Ties in aggregate() break on it, and a stable value matters
                # more than which row wins. See _row_id_expression for why
                # it is NOT row_number() OVER ().
                selects.append("%s AS row_id" % row_id)
                continue
            if key == "date_text":
                column = next((c for c in rd.COLUMNS["date"]
                               if c.upper() in available), None)
                selects.append("CAST(%s AS VARCHAR) AS date_text" % column
                               if column else "NULL AS date_text")
                continue
            source_key = {"sample_date": "date",
                          "detect_limit": "limit"}.get(key, key)
            candidates = rd.COLUMNS.get(source_key, ())
            found = [c for c in candidates if c.upper() in available]
            column = found[0] if found else None
            if column is None:
                selects.append("CAST(NULL AS %s) AS %s" % (kind, key))
                continue
            present.add(source_key)
            if key == "unit" and len(found) > 1:
                # COALESCED ACROSS THE CANDIDATES, not just the first one
                # present. rd.COLUMNS lists RESULT_UNIT then
                # REPORT_RESULT_UNIT for the same reason `value` lists
                # RESULT_NUMERIC then REPORT_RESULT_VALUE: a non-detect
                # reports no result and no result unit, only the limit and
                # ITS unit. Taking the first column that EXISTS rather than
                # the first that has a value left 57,868 Arsenic rows
                # reading as unitless while carrying ug/L in
                # REPORT_RESULT_UNIT - and a unitless row cannot be compared
                # to anything.
                #
                # Only `unit` is changed here. `value` has its own ladder in
                # the view below, where the non-detect conventions live, and
                # the other columns are single-source in practice.
                parts = ", ".join("nullif(trim(CAST(%s AS VARCHAR)), '')" % c
                                  for c in found)
                selects.append("coalesce(%s) AS unit" % parts)
                continue
            if key == "sample_date":
                selects.append("%s AS sample_date" % cls._parsed_date(column))
                continue
            selects.append("CAST(%s AS %s) AS %s" % (column, kind, key))

        # The refusal a report gets in the panel, applied here because a
        # parquet source never passes through a ReportTable. Same list, same
        # words: rd.REQUIRED_FOR_ANALYSIS is the one authority. A file
        # without DETECT_FLAG would otherwise open fine and halve every
        # measured value it holds.
        missing = rd.missing_required(lambda key: key in present)
        if missing:
            con.close()
            raise StoreError(rd.missing_message(
                missing, subject="This parquet source"))

        con.execute("DROP VIEW IF EXISTS results")
        con.execute("CREATE VIEW results AS SELECT %s FROM %s"
                    % (", ".join(selects), scanned))
        # The report's own columns, for export. On the ingest path these are
        # a second TABLE copied out of the ReportTable; here the parquet IS
        # the report's columns, so a view over the same files costs nothing
        # and needs no copy. `c0, c1, ...` positionally, because that is what
        # filtered_rows selects and `headers` is the matching order.
        headers = [row[0] for row in described]
        aliased = ", ".join('"%s" AS c%d' % (name.replace('"', '""'), index)
                            for index, name in enumerate(headers))
        con.execute("DROP VIEW IF EXISTS raw")
        con.execute("CREATE VIEW raw AS SELECT %s AS row_id, "
                    "%s FROM %s" % (row_id, aliased, scanned))

        store = cls(con, present, headers, has_raw=True)
        store._create_views()
        store.source = source
        store.files = files
        store._opened_stats = cls._stat_files(files)
        return store

    @classmethod
    def _row_id_expression(cls, con, source, literal, globbed):
        """SQL giving each parquet row a stable id, computed in the scan.

        IT USED TO BE `row_number() OVER ()`, AND THAT DOWNLOADED THE
        FACILITY. A window over the whole scan has to see every row before
        any filter applies, so no partition or row group could be skipped:
        the first query on a store opened from Azure read all ~110 MB, 31-60
        s, whatever it asked. Measured 2026-09-30: every Arsenic row in WS/T
        straight from Azure is 1.6 s without the window. Later queries hid
        it behind DuckDB's file cache, so it looked like a slow open.

        The scan already knows where each row is: which file, and which row
        in it. (file's place in the list) * 2^40 + (row in file) orders
        EXACTLY as row_number() did - list order, then file order - which is
        what keeps aggregate()'s tie-breaks and the export's row order
        unchanged. 2^40 rows per file is far beyond any partition.

        Paths are compared with forward slashes: glob() answers with the
        platform's separator while the scan reports the one it was given.
        """
        if globbed:
            files = [row[0] for row in con.execute(
                "SELECT file FROM glob(%s)" % literal).fetchall()]
        else:
            files = [str(one) for one in source]
        normalised = "[%s]" % ", ".join(
            "'%s'" % f.replace("\\", "/").replace("'", "''") for f in files)
        return ("(CAST(list_position(%s, replace(%s, '\\', '/')) AS BIGINT)"
                " * 1099511627776 + file_row_number)"
                % (normalised, FILE_COLUMN)), files

    @staticmethod
    def _stat_files(files):
        """{path: (size, mtime_ns)} for every file that is on local disk.

        Remote files are left out: a URL has no cheap stat, and a published
        set is replaced by a new manifest rather than rewritten in place.
        """
        found = {}
        for path in files:
            try:
                info = os.stat(path)
            except (OSError, ValueError):
                continue
            found[path] = (info.st_size, info.st_mtime_ns)
        return found

    def changed_files(self):
        """Local files that changed or vanished since this store opened.

        THE STORE IS A VIEW, NOT A COPY: every question re-reads the files.
        So a file rewritten under an open session is read in its NEW state
        by every later query, while the panel's chemical list, counts and
        the layers already drawn describe the old one - nothing errors, and
        the numbers quietly stop agreeing. While the rewrite is in progress
        it fails outright instead ("No magic bytes found at end of file"),
        which is how this was found, 2026-10-01: the synthetic generator
        rewrote arii.parquet under an open QGIS session.

        Size plus modification time: cheap enough to ask before every draw.
        """
        opened = getattr(self, "_opened_stats", None) or {}
        now = self._stat_files(list(opened))
        return [path for path, stat in opened.items() if now.get(path) != stat]

    # Directory names that hold an INTERMEDIATE copy of rows that also live
    # in the finished partitions. `**/*.parquet` matches both.
    INTERMEDIATE_DIRS = ("_staging",)

    @classmethod
    def _refuse_duplicated_rows(cls, con, literal, source):
        """Raise if the glob reaches an intermediate as well as the real files.

        This is not fussiness about tidy directories. `data/facility_a/**/*.parquet`
        once matched 46 files - 10 repacked partitions plus the 36 per-slice
        files they were built from - and the store answered 6,904,292 rows for
        a facility holding 3,452,146. Nothing raised. Every count, every
        aggregate and every histogram was simply twice what it should be, and
        the only reason it was caught is that the number happened to be a
        suspiciously round double.

        A wrong answer that looks like an answer is the worst failure this
        store can have, so an ambiguous glob is refused rather than served.
        `repack` now removes its staging, so reaching this means a run died
        between writing the partitions and clearing them.
        """
        try:
            files = [row[0] for row in con.execute(
                "SELECT file FROM glob(%s)" % literal).fetchall()]
        except Exception:  # noqa: BLE001
            # glob() is a convenience here; if it is unavailable the read
            # below will report any real problem with the path itself.
            return
        marked = [f for f in files
                  if any(("/%s/" % d) in f.replace("\\", "/")
                         for d in cls.INTERMEDIATE_DIRS)]
        if marked and len(marked) != len(files):
            raise StoreError(
                "%r matches %d intermediate file(s) under %s as well as %d "
                "finished one(s); every row in them would be counted twice. "
                "Read the partitions directly (matrix=*/fraction=*/*.parquet) "
                "or delete the leftover staging."
                % (source, len(marked), "/".join(cls.INTERMEDIATE_DIRS),
                   len(files) - len(marked)))

    @staticmethod
    def _parsed_date(column):
        """SQL that parses a report date column, in report_data's own order.

        The numbers in a slicing-pipeline parquet are typed, but the DATE is
        not: EQuIS writes `4/24/2025 12:37:00 PM` and the extract keeps it as
        a string, so a plain CAST raises "invalid timestamp field format".

        strptime with a LIST tries each format in turn and returns NULL for
        a value none of them match, which is what parse_date does in Python -
        an unparseable date becomes None rather than an error. The formats
        come from rd.DATE_FORMATS so the two orders cannot drift: that list
        is ordered by how often each occurs, and the 12-hour form is first
        because it is what Analytical Results II actually emits.
        """
        formats = ", ".join(
            "'%s'" % fmt.replace("'", "''") for fmt in rd.DATE_FORMATS)
        return ("try_cast(try_strptime(CAST(%s AS VARCHAR), [%s]) AS TIMESTAMP)"
                % (column, formats))

    @classmethod
    def open(cls, path):
        """Reopen a store written earlier, without re-ingesting anything.

        This is the worker-thread handoff. A DuckDB connection should not be
        shared between threads, so a background build writes a file, closes
        it, and the main thread opens it again here - which is unambiguously
        safe, and gives the session persistence of US-13 almost for free.
        """
        duckdb = duckdb_module()
        con = duckdb.connect(path)
        try:
            rows = con.execute("SELECT key, value FROM meta").fetchall()
        except Exception as exc:
            con.close()
            raise StoreError("%s is not a report store: %s" % (path, exc))
        headers = [v for k, v in rows if k == "header"]
        present = {v for k, v in rows if k == "present"}
        has_raw = any(k == "has_raw" and v == "1" for k, v in rows)
        return cls(con, present, headers, has_raw=has_raw)

    @staticmethod
    def _load_raw(con, table):
        """The report's own columns, verbatim, for export.

        Separate from `results` because the two serve different masters: the
        rules need typed, aliased columns and an export needs the original
        text under the original headers, in the original order. Columns are
        named positionally - report headers are arbitrary text and quoting
        them into SQL is a needless hazard.
        """
        width = len(table.headers)
        columns = ", ".join(["row_id INTEGER"]
                            + ["c%d VARCHAR" % i for i in range(width)])
        con.execute("DROP TABLE IF EXISTS raw")
        con.execute("CREATE TABLE raw (%s)" % columns)
        handle, path = tempfile.mkstemp(suffix=".csv", prefix="equis_raw_")
        os.close(handle)
        written = 0
        try:
            with open(path, "w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                for position, row in enumerate(table.rows):
                    cells = list(row[:width])
                    cells += [""] * (width - len(cells))
                    writer.writerow([position] + [
                        "" if cell is None else cell for cell in cells])
                    written += 1
            if written:
                con.execute("COPY raw FROM ? (FORMAT CSV, HEADER FALSE)",
                            [path])
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    @staticmethod
    def _csv_row(table, position, row):
        """One row, in the column order FIELDS declares."""
        def number(key):
            parsed = rd.parse_number(table.cell(row, key))
            return "" if parsed is None else repr(parsed)

        when = rd.parse_date(table.cell(row, "date"))
        return (
            position,
            table.cell(row, "location"),
            table.cell(row, "chemical"),
            table.cell(row, "cas_rn"),
            "" if when is None else when.isoformat(),
            number("value"),
            number("reported"),
            number("limit"),
            table.cell(row, "unit"),
            table.cell(row, "detect"),
            table.cell(row, "matrix"),
            table.cell(row, "fraction"),
            table.cell(row, "method"),
            table.cell(row, "sample_type"),
            number("start_depth"),
            number("end_depth"),
            table.cell(row, "depth_unit"),
            table.cell(row, "custom_1"),
            table.cell(row, "date"),
            table.cell(row, "sample"),
            table.cell(row, "test_type"),
            table.cell(row, "qual_interpreted"),
            table.cell(row, "qual_lab"),
            table.cell(row, "lab"),
        )

    def _create_views(self, source="results"):
        """The rules, as one view every query reads.

        row_value() in three columns rather than a parameter, so the view can
        be static: the caller picks the column its non-detect convention
        names. The CASE ladders below are that function transcribed - read
        them side by side.

        `source` is what the view selects FROM. It exists so a parquet-backed
        store can present the same view over different bytes: the rules are
        defined once, here, and a second copy written for parquet would be
        exactly the divergence that a parity test cannot catch, because it
        would be comparing two things that were never the same.
        """
        detected = "upper(coalesce(detect, '')) IN %s" % str(DETECT_TRUE)
        # The fallback chain: RESULT_NUMERIC, then REPORT_RESULT_VALUE, then
        # the detection-limit columns. Note `value` may BE `reported` when the
        # report carries only the latter - ReportTable resolves aliases the
        # same way, so both see the same number.
        base = "coalesce(value, reported, detect_limit)"
        self.con.execute("DROP VIEW IF EXISTS v_results")
        self.con.execute("""
            CREATE VIEW v_results AS
            SELECT *,
                {detected} AS detected,
                {base} AS nd_base,
                -- at_limit: true only when the number shown is a limit
                -- standing in for a measurement that was never made.
                CASE WHEN value IS NOT NULL AND {detected} THEN FALSE
                     WHEN {base} IS NULL THEN FALSE
                     WHEN NOT {detected} THEN TRUE
                     ELSE FALSE END AS at_limit,
                CASE WHEN value IS NOT NULL AND {detected} THEN value
                     WHEN {base} IS NULL THEN NULL
                     WHEN NOT {detected} THEN {base} / 2.0
                     ELSE {base} END AS value_half,
                CASE WHEN value IS NOT NULL AND {detected} THEN value
                     WHEN {base} IS NULL THEN NULL
                     ELSE {base} END AS value_full,
                CASE WHEN value IS NOT NULL AND {detected} THEN value
                     WHEN {base} IS NULL THEN NULL
                     WHEN NOT {detected} THEN NULL
                     ELSE {base} END AS value_exclude
            FROM {source}
            WHERE {scope}
        """.format(detected=detected, base=base, source=source,
                   scope=self._scope_clause()))

    # ----------------------------------------------------------- row scope

    @property
    def scope(self):
        return self._scope

    def set_row_scope(self, start=None, end=None, sample_types=None):
        """ReportTable.set_row_scope, as the WHERE of the one view.

        In the view rather than in _where() because every question reads
        v_results - aggregate, series, rows_for, the pickers' counts, the
        export - and a scope added per query is a scope some query forgets.
        This is the same reason the non-detect rules live there.

        Redefining a view is a catalogue change, not a scan: it costs
        nothing until the next query, which pays only the extra predicate.
        A thread_view() shares the catalogue, so it follows too.
        """
        scope = rd.row_scope(start, end, sample_types)
        if scope == self._scope:
            return False
        self._scope = scope
        self._create_views()
        return True

    def _scope_clause(self):
        """The row scope as SQL, inlined because DuckDB refuses a prepared
        parameter in CREATE VIEW. Dates are date objects and the codes are
        quoted with the only escaping a SQL string literal needs."""
        start, end, codes = self._scope
        clauses = []
        if (start is not None or end is not None) and "date" in self.present:
            # Undated rows are out whenever a bound is set: a NULL compared
            # to a date is NULL, which WHERE drops, but say it outright.
            clauses.append("sample_date IS NOT NULL")
            if start is not None:
                clauses.append("CAST(sample_date AS DATE) >= DATE '%s'"
                               % start.isoformat())
            if end is not None:
                clauses.append("CAST(sample_date AS DATE) <= DATE '%s'"
                               % end.isoformat())
        if codes and "sample_type" in self.present:
            clauses.append("upper(coalesce(trim(sample_type), '')) IN (%s)"
                           % ", ".join("'%s'" % c.replace("'", "''")
                                       for c in sorted(codes)))
        return " AND ".join(clauses) if clauses else "TRUE"

    # ---------------------------------------------------------- predicates

    def _code_filter(self, column, wanted, key):
        """One of the matrix/fraction/method filters, as SQL plus parameters.

        Reproduces _in_matrix exactly: None means no filter, an empty
        collection means no filter, a column the report lacks means no filter,
        and comparison is case-insensitive.
        """
        codes = self.filter_codes(key, wanted)
        if codes is None:
            return "", []
        if len(codes) > 1:
            marks = ", ".join("?" * len(codes))
            return ("upper(coalesce(%s, '')) IN (%s)" % (column, marks)), codes
        return ("upper(coalesce(%s, '')) = ?" % column), codes

    def filter_codes(self, key, wanted):
        """The codes a matrix/fraction/method filter compares, or None.

        None is "no filter": nothing wanted, an empty collection, or a column
        this source lacks. Otherwise the codes upper-cased, to compare with
        upper(coalesce(column, '')). Public because the histogram's SQL is
        written by the page (web/src/histogram.js): the host sends it this,
        so the page filters by the store's answer instead of its own. It
        once compared codes as typed, binned only a layer's first matrix,
        and dropped every row on a column the source lacks (found
        2026-10-28; tools/test_histogram_sql.py).
        """
        if wanted is None or key not in self.present:
            return None
        if isinstance(wanted, (list, tuple, set, frozenset)):
            codes = [str(w).upper() for w in wanted if str(w or "").strip()]
            return codes or None
        return [str(wanted or "").upper()]

    def _analyte_filter(self, chemical, cas_rn):
        """_matches(), as SQL.

        CAS RN wins when the report has one: a lookup by CAS then never falls
        back to the name, which is what makes it authoritative rather than
        merely preferred.
        """
        if cas_rn and "cas_rn" in self.present:
            return "coalesce(cas_rn, '') = ?", [cas_rn]
        if cas_rn:
            # No CAS column, so the name is all there is - the Python falls
            # through to the same place.
            return "coalesce(chemical, '') = ?", [chemical or ""]
        return "coalesce(chemical, '') = ?", [chemical or ""]

    def _where(self, chemical=None, cas_rn=None, matrix=rd.ALL_MATRICES,
               fraction=rd.ANY_FRACTION, method=rd.ALL_METHODS,
               location=None, detected_only=False):
        clauses, params = [], []
        if chemical is not None or cas_rn:
            clause, values = self._analyte_filter(chemical, cas_rn)
            clauses.append(clause)
            params.extend(values)
        for column, wanted, key in (("matrix", matrix, "matrix"),
                                    ("fraction", fraction, "fraction"),
                                    ("method", method, "method")):
            clause, values = self._code_filter(column, wanted, key)
            if clause:
                clauses.append(clause)
                params.extend(values)
        if location is not None:
            clauses.append("coalesce(location, '') = ?")
            params.append(location)
        if detected_only:
            clauses.append("detected")
        return (" AND ".join(clauses) if clauses else "TRUE"), params

    # ------------------------------------------------------------ questions

    def analytes(self, matrix=rd.ALL_MATRICES):
        where, params = self._where(matrix=matrix)
        rows = self.con.execute("""
            SELECT chemical, coalesce(cas_rn, '') AS cas_rn, count(*) AS n,
                   min(row_id) AS first_seen
            FROM v_results
            WHERE (%s) AND coalesce(chemical, '') <> ''
            GROUP BY 1, 2
            -- lower() then first-seen: Python sorts case-insensitively with a
            -- stable sort, so entries that compare equal keep the order they
            -- were first encountered in.
            ORDER BY lower(chemical), first_seen
        """ % where, params).fetchall()
        return [{"chemical": r[0], "cas_rn": r[1], "count": r[2]}
                for r in rows]

    def matrices(self):
        if "matrix" not in self.present:
            return []
        rows = self.con.execute("""
            SELECT upper(coalesce(matrix, '')) AS code, count(*) AS n
            FROM v_results GROUP BY 1 ORDER BY n DESC, code
        """).fetchall()
        return [{"matrix": r[0], "count": r[1]} for r in rows]

    def mixed_fractions(self):
        if "fraction" not in self.present:
            return {}
        rows = self.con.execute("""
            SELECT upper(coalesce(matrix, '')) AS m,
                   upper(coalesce(fraction, '')) AS f
            FROM v_results
            WHERE coalesce(trim(fraction), '') <> ''
            GROUP BY 1, 2 ORDER BY 1, 2
        """).fetchall()
        seen = {}
        for matrix, fraction in rows:
            seen.setdefault(matrix, []).append(fraction)
        return {m: f for m, f in seen.items() if len(f) > 1}

    def methods(self, chemical=None, cas_rn=None, matrix=rd.ALL_MATRICES):
        if "method" not in self.present:
            return []
        where, params = self._where(chemical=chemical, cas_rn=cas_rn,
                                    matrix=matrix)
        rows = self.con.execute("""
            SELECT upper(coalesce(method, '')) AS code, count(*) AS n
            FROM v_results WHERE (%s) AND coalesce(trim(method), '') <> ''
            GROUP BY 1 ORDER BY n DESC, code
        """ % where, params).fetchall()
        return [{"method": r[0], "count": r[1]} for r in rows]

    def fractions_for(self, chemical, cas_rn=None, matrix=rd.ALL_MATRICES):
        if "fraction" not in self.present:
            return []
        where, params = self._where(chemical=chemical, cas_rn=cas_rn,
                                    matrix=matrix)
        rows = self.con.execute("""
            SELECT DISTINCT upper(coalesce(fraction, '')) AS code
            FROM v_results WHERE (%s) AND coalesce(trim(fraction), '') <> ''
            ORDER BY 1
        """ % where, params).fetchall()
        return [r[0] for r in rows]

    def locations(self, matrix=rd.ALL_MATRICES):
        where, params = self._where(matrix=matrix)
        rows = self.con.execute("""
            SELECT DISTINCT location FROM v_results
            WHERE (%s) AND coalesce(location, '') <> '' ORDER BY location
        """ % where, params).fetchall()
        return [r[0] for r in rows]

    def aggregate(self, chemical, cas_rn=None, how=rd.MOST_RECENT,
                  detected_only=False, non_detect=rd.ND_HALF,
                  matrix=rd.ALL_MATRICES, fraction=rd.ANY_FRACTION,
                  method=rd.ALL_METHODS):
        """Per-location summary, matching ReportTable.aggregate().

        Two subtleties carried over from the loop it replaces. Rows with no
        usable number are COUNTED but cannot win, so the counts come from a
        separate aggregation over every matching row. And ties keep the row
        that arrived first, because the Python compares with a strict > - so
        every ordering here breaks ties on row_id.
        """
        column = ND_COLUMN.get(non_detect, "value_half")
        where, params = self._where(chemical=chemical, cas_rn=cas_rn,
                                    matrix=matrix, fraction=fraction,
                                    method=method, detected_only=detected_only)
        if how == rd.MAXIMUM:
            # A NON-DETECT MUST NEVER BE THE MAXIMUM while a real detection
            # exists at that location. A non-detect carries only the limit
            # it fell BELOW, so `<50` substituted at half its limit is 25 -
            # a number about laboratory sensitivity, not about the site -
            # and it was beating genuine detections. Measured on Facility A
            # 2026-09-28: 759 (chemical, matrix, location) cells across 47
            # analytes, the worst overstating a location's real maximum by
            # 1,250x (nitrogen at 5.0 mg/kg where the true maximum detected
            # was 0.004).
            #
            # `detected DESC` first, so every detection outranks every
            # non-detect, and only THEN by value. A location with nothing
            # but non-detects still reports one - 9,660 cells here have no
            # detection at all, and "we looked and found nothing" is a
            # result worth mapping, marked at_limit as it already was.
            order = "detected DESC, %s DESC, row_id ASC" % column
        else:
            # Undated rows only ever fill an empty slot, which is what NULLS
            # LAST says: a dated row always precedes an undated one.
            order = "sample_date DESC NULLS LAST, row_id ASC"

        sql = """
            WITH matched AS (
                SELECT * FROM v_results
                WHERE (%(where)s) AND coalesce(location, '') <> ''
            ),
            counts AS (
                SELECT location, count(*) AS n,
                       sum(CASE WHEN detected THEN 1 ELSE 0 END) AS detects
                FROM matched GROUP BY location
            ),
            ranked AS (
                SELECT location, %(col)s AS val, unit, sample_date, detect,
                       at_limit,
                       row_number() OVER (PARTITION BY location
                                          ORDER BY %(order)s) AS rn
                FROM matched WHERE %(col)s IS NOT NULL
            )
            SELECT c.location, c.n, c.detects,
                   r.val, r.unit, r.sample_date, r.detect, r.at_limit
            FROM counts c
            LEFT JOIN ranked r ON r.location = c.location AND r.rn = 1
        """ % {"where": where, "col": column, "order": order}

        summary = {}
        for row in self.con.execute(sql, params).fetchall():
            location, count, detects, value, unit, when, detect, at_limit = row
            summary[location] = {
                "location": location,
                "value": value,
                "unit": unit or "",
                "date": when,
                "detect": (detect or "").upper() if value is not None else "",
                "at_limit": bool(at_limit) if value is not None else False,
                "count": int(count),
                "detect_count": int(detects or 0),
            }
        return summary

    def series(self, location, chemical, cas_rn=None,
               non_detect=rd.ND_HALF, matrix=rd.ALL_MATRICES,
               fraction=rd.ANY_FRACTION, method=rd.ALL_METHODS):
        """One location's history, oldest first.

        Rows without both a date and a value are dropped, as they cannot be
        plotted. Equal dates keep row order, because the Python sorts with a
        stable sort.
        """
        column = ND_COLUMN.get(non_detect, "value_half")
        where, params = self._where(chemical=chemical, cas_rn=cas_rn,
                                    matrix=matrix, fraction=fraction,
                                    method=method, location=location)
        rows = self.con.execute("""
            SELECT sample_date, %(col)s AS val, unit, detect, at_limit
            FROM v_results
            WHERE (%(where)s) AND sample_date IS NOT NULL
              AND %(col)s IS NOT NULL
            ORDER BY sample_date, row_id
        """ % {"col": column, "where": where}, params).fetchall()
        return [{"date": r[0], "value": r[1], "unit": r[2] or "",
                 "detect": (r[3] or "").upper(), "at_limit": bool(r[4])}
                for r in rows]

    def series_many(self, locations, chemical, cas_rn=None,
                    non_detect=rd.ND_HALF, matrix=rd.ALL_MATRICES,
                    fraction=rd.ANY_FRACTION, method=rd.ALL_METHODS):
        """Every requested location's series, in ONE query.

        This exists because of a measurement. A database pays a fixed cost per
        query - parse, plan, execute, fetch - of a few milliseconds, which is
        nothing beside the work it saves on a large scan and everything when
        the query is tiny. Looping series() over 300 locations, which is what
        drawing sparklines does, cost 1.44 s against 27 ms for the Python row
        index: a 54x REGRESSION on the one case that index was built for.

        The same work as a single grouped scan is ~30 ms. So the rule the
        store imposes on its callers is: ask once for many, never many times
        for one. Keep series() for the click-a-point case, where a human just
        clicked and 5 ms is invisible.

        `locations` may be None, meaning every location that has results.
        """
        column = ND_COLUMN.get(non_detect, "value_half")
        where, params = self._where(chemical=chemical, cas_rn=cas_rn,
                                    matrix=matrix, fraction=fraction,
                                    method=method)
        clauses = [where, "sample_date IS NOT NULL",
                   "%s IS NOT NULL" % column,
                   "coalesce(location, '') <> ''"]
        wanted = None
        if locations is not None:
            wanted = [str(code) for code in locations]
            if not wanted:
                return {}
            clauses.append("location IN (%s)" % ", ".join("?" * len(wanted)))
            params = list(params) + wanted

        rows = self.con.execute("""
            SELECT location, sample_date, %(col)s AS val, unit, detect,
                   at_limit
            FROM v_results
            WHERE %(where)s
            ORDER BY location, sample_date, row_id
        """ % {"col": column, "where": " AND ".join("(%s)" % c
                                                    for c in clauses)},
            params).fetchall()

        out = {}
        for location, when, value, unit, detect, at_limit in rows:
            out.setdefault(location, []).append({
                "date": when, "value": value, "unit": unit or "",
                "detect": (detect or "").upper(), "at_limit": bool(at_limit)})
        # Locations asked for but holding nothing still answer, with an empty
        # list, so a caller does not have to distinguish "no data" from
        # "forgot to ask".
        if wanted is not None:
            for code in wanted:
                out.setdefault(code, [])
        return out

    def thread_view(self):
        """A view on the same data, safe to query from another thread.

        DuckDB's own answer to concurrency: cursor() returns a connection
        sharing this one's database and catalogue, with its own execution
        state. Verified on the real facility - a worker thread counted
        115,017 Arsenic rows through one while the main connection kept
        answering.

        Returned as a ReportStore rather than a bare connection so the
        caller gets the same methods with the same rules, instead of a
        second place where SQL is written by hand.

        The returned store must NOT be closed: closing a cursor's parent is
        what closes the database, and this shares it.
        """
        clone = type(self)(self.con.cursor(), self.present, self.headers,
                           has_raw=self.has_raw)
        clone.source = getattr(self, "source", None)
        # The view is shared, so the clone is already scoped; it must know,
        # or a set_row_scope on it would think nothing changed.
        clone._scope = self._scope
        return clone

    def units_for(self, chemical, cas_rn=None, detected_only=False,
                  non_detect=rd.ND_HALF, matrix=rd.ALL_MATRICES,
                  fraction=rd.ANY_FRACTION, method=rd.ALL_METHODS):
        """The units this selection reports in, commonest first.

        MIXED UNITS ARE A WRONG ANSWER, NOT A PRESENTATION DETAIL. Measured
        on the Facility A 2026-09-24: Arsenic in WS is 10,503 rows in
        ug/L and 6,591 in mg/L - a factor of 1000 - and 118 (chemical,
        matrix) pairs mix units the same way. A maximum taken across them
        ranks by UNIT rather than by concentration: 0.57 mg/L is 570 ug/L
        and loses to 168 ug/L. The map draws both on one colour ramp.

        This belongs beside "never aggregate across fractions" and "never
        aggregate across matrices" in CLAUDE.md - the same failure, which
        is a plausible number rather than an error.

        Returns [(unit, rows), ...]. An empty unit is reported as '' rather
        than dropped: rows with no unit at all cannot be compared either,
        and on this facility there are locations where every row is
        unitless.
        """
        if "unit" not in self.present:
            return []
        where, params = self._where(chemical=chemical, cas_rn=cas_rn,
                                    matrix=matrix, fraction=fraction,
                                    method=method, detected_only=detected_only)
        column = ND_COLUMN.get(non_detect, "value_half")
        rows = self.con.execute("""
            SELECT coalesce(trim(unit), '') AS u, count(*) AS n
            FROM v_results
            WHERE (%s) AND %s IS NOT NULL
            GROUP BY 1 ORDER BY n DESC, u
        """ % (where, column), params).fetchall()
        return [(row[0], row[1]) for row in rows]

    def count_for(self, chemical, cas_rn=None, detected_only=False,
                  non_detect=rd.ND_HALF, matrix=rd.ALL_MATRICES,
                  fraction=rd.ANY_FRACTION, method=rd.ALL_METHODS):
        """(locations, results) for one analyte, WITHOUT building the rows.

        The panel's footer wants two numbers and nothing else. It used to
        get them by calling rows_for and measuring the list, which
        materialises one Python dict per matching row - 115,017 of them for
        Arsenic on the Facility A - purely to count them. Measured at
        1.53 s on the UI thread, on every analyte click.

        The same filters, counted in SQL: 0.02 s. This is CLAUDE.md's "ask
        once for many" applied to a question that never needed the rows at
        all.

        The predicate is deliberately IDENTICAL to rows_for's - same
        `_where`, same non-detect column, same "has a usable number and a
        location" rule - so the footer cannot promise a count the build then
        fails to draw. tools/test_store_parity.py holds them together.
        """
        column = ND_COLUMN.get(non_detect, "value_half")
        where, params = self._where(chemical=chemical, cas_rn=cas_rn,
                                    matrix=matrix, fraction=fraction,
                                    method=method, detected_only=detected_only)
        row = self.con.execute("""
            SELECT count(DISTINCT location), count(*)
            FROM v_results
            WHERE (%s) AND coalesce(location, '') <> '' AND %s IS NOT NULL
        """ % (where, column), params).fetchone()
        return (row[0] or 0, row[1] or 0) if row else (0, 0)

    def rows_for(self, chemical, cas_rn=None, detected_only=False,
                 non_detect=rd.ND_HALF, matrix=rd.ALL_MATRICES,
                 fraction=rd.ANY_FRACTION, method=rd.ALL_METHODS, limit=0):
        """Every result for one analyte, one dict per row. Returns (rows, dropped).

        The unaggregated counterpart to aggregate(), and what "every result"
        mode maps. Rows with no usable number are dropped, not carried as
        blanks: a feature with no value cannot be styled and would sit on top
        of the ones that have one.

        THE CAP IS APPLIED IN PYTHON, DELIBERATELY. `limit` drops whole
        locations, never part of a location's history, and the rule is a
        greedy first fit in descending-history order: a location that would
        overflow the cap is SKIPPED and the scan continues, so a smaller one
        later can still fit. That is sequential, and expressing it as a
        window function would either change the answer or become unreadable.
        It runs over one row per location - thousands at most - after a query
        that costs milliseconds, so the loop is free.
        """
        column = ND_COLUMN.get(non_detect, "value_half")
        where, params = self._where(chemical=chemical, cas_rn=cas_rn,
                                    matrix=matrix, fraction=fraction,
                                    method=method, detected_only=detected_only)
        base = "(%s) AND coalesce(location, '') <> '' AND %s IS NOT NULL" % (
            where, column)

        keep = None
        dropped = 0
        if limit:
            counts = self.con.execute("""
                SELECT location, count(*) AS n FROM v_results
                WHERE %s GROUP BY 1
            """ % base, params).fetchall()
            if sum(n for _code, n in counts) > limit:
                # Most history first, ties by code - the same key the Python
                # sorts on, so the two keep the same locations.
                ordered = sorted(counts, key=lambda kv: (-kv[1], kv[0]))
                keep, total = [], 0
                for code, n in ordered:
                    if total + n > limit:
                        dropped += 1
                        continue
                    keep.append(code)
                    total += n
                if not keep:
                    return [], dropped

        clauses, values = [base], list(params)
        if keep is not None:
            clauses.append("location IN (%s)" % ", ".join("?" * len(keep)))
            values.extend(keep)

        rows = self.con.execute("""
            SELECT location, %(col)s AS val, unit, sample_date, detect,
                   at_limit, matrix, fraction, sample_type, start_depth,
                   end_depth, depth_unit, custom_1, method,
                   row_number() OVER (
                       PARTITION BY location
                       -- Undated rows sort last and keep their arrival order
                       -- among themselves, as the Python's stable sort does.
                       ORDER BY sample_date ASC NULLS LAST, row_id ASC
                   ) - 1 AS seq
            FROM v_results
            WHERE %(where)s
            ORDER BY location, sample_date ASC NULLS LAST, row_id ASC
        """ % {"col": column, "where": " AND ".join(clauses)},
            values).fetchall()

        out = []
        for r in rows:
            out.append({
                "location": r[0], "value": r[1], "unit": r[2] or "",
                "date": r[3], "detect": (r[4] or "").upper(),
                "at_limit": bool(r[5]),
                "matrix": (r[6] or "").upper(),
                "fraction": (r[7] or "").upper(),
                "sample_type": (r[8] or "").upper(),
                "start_depth": r[9], "end_depth": r[10],
                "depth_unit": r[11] or "", "custom_1": r[12] or "",
                "method": (r[13] or "").upper(),
                "seq": int(r[14]),
            })
        return out, dropped

    def filtered_rows(self, analytes=None, matrix=rd.ALL_MATRICES,
                      fraction=rd.ANY_FRACTION, method=rd.ALL_METHODS,
                      locations=None):
        """The report rows behind a mapped layer, for export. (headers, rows).

        Not the canonical columns this store reasons over - the REPORT'S own
        columns, in the report's own order, so the export is recognisably the
        report it came from. That is why `raw` exists as a second table: the
        rules need typed, aliased columns and the export needs the original
        text, and neither substitutes for the other.
        """
        clauses, params = [], []
        wanted = list(analytes or [])
        if wanted:
            parts = []
            for analyte in wanted:
                clause, values = self._analyte_filter(
                    (analyte or {}).get("chemical"), (analyte or {}).get("cas_rn"))
                parts.append(clause)
                params.extend(values)
            clauses.append("(%s)" % " OR ".join(parts))
        for column, value, key in (("matrix", matrix, "matrix"),
                                   ("fraction", fraction, "fraction"),
                                   ("method", method, "method")):
            clause, values = self._code_filter(column, value, key)
            if clause:
                clauses.append(clause)
                params.extend(values)
        keep = [str(code) for code in (locations or [])]
        if keep:
            clauses.append("coalesce(location, '') IN (%s)"
                           % ", ".join("?" * len(keep)))
            params.extend(keep)

        if not self.has_raw:
            raise StoreError(
                "This store was built with with_raw=False, so the report's "
                "own columns are not available to export.")

        where = " AND ".join("(%s)" % c for c in clauses) if clauses else "TRUE"
        picked = ", ".join("r.c%d" % i for i in range(len(self.headers)))
        rows = self.con.execute("""
            SELECT %(cols)s
            FROM v_results v JOIN raw r USING (row_id)
            WHERE %(where)s
            -- An unparseable date sorts as its own text, which is stable if
            -- not chronological: better than dropping a row from an export
            -- for carrying a date the parser does not recognise.
            ORDER BY upper(coalesce(v.chemical, '')),
                     upper(coalesce(v.location, '')),
                     coalesce(strftime(v.sample_date,
                                       '%%Y-%%m-%%dT%%H:%%M:%%S'),
                              v.date_text, ''),
                     -- ARRIVAL ORDER BREAKS THE TIE. Python's sort is
                     -- STABLE, so rows equal on the three keys above keep
                     -- the order they were read in; SQL guarantees nothing,
                     -- and the two exports then differ by which of two
                     -- same-day rows comes first. Latent until a fixture
                     -- row created such a tie, which is the whole reason
                     -- the parity gate exists.
                     v.row_id
        """ % {"cols": picked, "where": where}, params).fetchall()
        # The export must look like the report, so empty cells come back as
        # the empty strings they were, not as the NULLs the CSV load made.
        return list(self.headers), [
            ["" if cell is None else cell for cell in row] for row in rows]

    # --------------------------------------------------------------- admin

    def count(self):
        return self.con.execute("SELECT count(*) FROM results").fetchone()[0]

    def close(self):
        try:
            self.con.close()
        except Exception:
            pass
