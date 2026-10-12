"""QgsTask wrappers so network work never blocks the QGIS UI thread.

Each task does only network/parse work in run(); layers are constructed in
finished(), which QGIS guarantees runs on the main thread.

Tasks are also where the on-disk cache is written and read. That seam is here
rather than in EquisClient because the client is a transport layer with no
notion of facilities or sessions, and it is shared with tools/selftest.py;
these tasks already bound one logical operation and already run off the UI
thread, which is where the gzip and disk I/O belong.

Every result carries a `source`: LIVE, CACHE (we chose the cache) or
CACHE_FALLBACK (the network failed and the cache rescued the operation). The
distinction drives what the UI says, so it must not be collapsed into a
boolean.
"""

from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from .cache import CacheError
from .equis_api import EquisError
from .layer_builder import build_feature_layer, build_report_layer

LIVE = "live"
CACHE = "cache"
CACHE_FALLBACK = "cache-fallback"
# Read from output an information agent stored earlier, rather than by running
# the report. Named separately from LIVE because the data is as old as the
# agent's last run, which the panel has to be able to say out loud.
EIA = "eia"

# Marks layers built from cached data. This is the only signal that outlives
# the session: it shows in the layers panel, in print layouts, and in a project
# reopened months later, long after any status message has gone.
CACHED_SUFFIX = " [cached]"


def cached_layer_name(name, from_cache):
    """Append the cached marker once, without stacking it on a rebuild."""
    if not from_cache or name.endswith(CACHED_SUFFIX):
        return name
    return name + CACHED_SUFFIX


class _BaseTask(QgsTask):
    """Shared error handling, caching and completion signalling."""

    finishedOk = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, description, cache=None, site="", prefer_cache=False):
        super().__init__(description, QgsTask.Flag.CanCancel)
        self.error = ""
        self._result = None
        self.cache = cache
        self.site = site
        self.prefer_cache = prefer_cache
        # Set by whichever path produced _result.
        self.source = LIVE
        # The CacheEntry behind a cached result, so the UI can say how old it
        # is without going back to the manifest.
        self.entry = None

    @property
    def from_cache(self):
        return self.source in (CACHE, CACHE_FALLBACK)

    def _store(self, write):
        """Run a cache write, downgrading any failure to a no-op.

        A full disk must never cost the user the report they just waited
        minutes for, so this swallows everything the write can raise.
        """
        if self.cache is None:
            return
        try:
            write()
        except (CacheError, OSError, TypeError, ValueError):
            pass

    def _fail(self, message):
        self.error = message
        return False

    def finished(self, ok):
        if self.isCanceled():
            return
        if ok:
            self.finishedOk.emit(self._result)
        else:
            self.failed.emit(self.error or "The operation failed.")


class FetchFacilitiesTask(_BaseTask):
    """Load the facility list the user has access to."""

    def __init__(self, client, cache=None, site="", prefer_cache=False):
        super().__init__("EQuIS: loading facilities", cache, site, prefer_cache)
        self.client = client

    def _cached(self):
        if self.cache is None:
            return None, None
        return self.cache.get_facilities(self.site)

    def run(self):
        if self.prefer_cache or self.client is None:
            facilities, entry = self._cached()
            if facilities:
                self._result = facilities
                self.source = CACHE
                self.entry = entry
                return True
            if self.client is None:
                return self._fail(
                    "No facilities are cached for this site, so there is "
                    "nothing to show offline."
                )
        try:
            facilities = self.client.list_facilities()
            self._store(lambda: self.cache.put_facilities(self.site, facilities))
            self._result = facilities
            self.source = LIVE
            return True
        except EquisError as exc:
            # The connection is the thing that failed; if this site was used
            # before, its facility list is still on disk and is a far better
            # outcome than a dead end.
            facilities, entry = self._cached()
            if facilities:
                self._result = facilities
                self.source = CACHE_FALLBACK
                self.entry = entry
                self.error = str(exc)
                return True
            return self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            return self._fail("Unexpected error loading facilities: %s" % exc)


class FetchFacilityDetailTask(_BaseTask):
    """Load a facility's layers and its reports in one background pass."""

    def __init__(self, client, facility_id, cache=None, site="",
                 prefer_cache=False, facility=None):
        super().__init__("EQuIS: loading facility contents", cache, site,
                         prefer_cache)
        self.client = client
        self.facility_id = facility_id
        self.facility = facility or {}

    def _cached(self):
        if self.cache is None:
            return None, None
        meta, entry = self.cache.get_facility_meta(self.site, self.facility_id)
        if not meta:
            return None, None
        return {
            "layers": meta.get("layers") or [],
            "reports": meta.get("reports") or [],
            "service_wkid": meta.get("service_wkid") or 0,
        }, entry

    def run(self):
        if self.prefer_cache or self.client is None:
            detail, entry = self._cached()
            if detail:
                self._result = detail
                self.source = CACHE
                self.entry = entry
                return True
            if self.client is None:
                return self._fail(
                    "This facility has not been loaded before, so it is not "
                    "available offline."
                )
        try:
            layers, service_wkid = self.client.facility_layers(self.facility_id)
            self.setProgress(40)
            if self.isCanceled():
                return False

            # Count features per layer so the UI can show sizes and hide empty
            # layers. A failure here must not sink the whole dialog.
            for layer in layers:
                if self.isCanceled():
                    return False
                try:
                    layer["count"] = self.client.layer_count(self.facility_id, layer["id"])
                except EquisError:
                    layer["count"] = None
            self.setProgress(75)

            try:
                reports = self.client.list_reports(self.facility_id)
            except EquisError:
                reports = []

            self._store(lambda: self.cache.put_facility_meta(
                self.site, self.facility_id, layers, reports, service_wkid,
                facility=self.facility,
            ))
            self._result = {
                "layers": layers,
                "reports": reports,
                "service_wkid": service_wkid,
            }
            self.source = LIVE
            return True
        except EquisError as exc:
            detail, entry = self._cached()
            if detail:
                self._result = detail
                self.source = CACHE_FALLBACK
                self.entry = entry
                self.error = str(exc)
                return True
            return self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._fail("Unexpected error loading facility: %s" % exc)


class LoadLayersTask(_BaseTask):
    """Page through the chosen FeatureServer layers and collect their features."""

    def __init__(self, client, facility_id, facility_name, layers, fallback_wkid,
                 where="1=1", max_features=None, out_fields="*",
                 location_codes=None, cache=None, site="", prefer_cache=False):
        super().__init__("EQuIS: loading features", cache, site, prefer_cache)
        self.client = client
        self.facility_id = facility_id
        self.facility_name = facility_name
        self.layers = layers
        self.fallback_wkid = fallback_wkid
        self.where = where or "1=1"
        self.max_features = max_features
        self.out_fields = out_fields or "*"
        self.location_codes = location_codes or None

    @property
    def _cacheable(self):
        """Only an unfiltered, uncapped read represents the whole layer.

        Caching a filtered or truncated read under the same key would later
        serve a partial layer as if it were the facility's full location set.
        """
        return (
            len(self.layers) == 1
            and not self.location_codes
            and self.max_features is None
            and self.where == "1=1"
        )

    def _cached(self):
        if self.cache is None or len(self.layers) != 1:
            return None, None
        payload, entry = self.cache.get_locations(self.site, self.facility_id)
        if not payload or not payload.get("pages"):
            return None, None
        return [{"layer": self.layers[0], "pages": payload["pages"]}], entry

    def run(self):
        if self.prefer_cache or self.client is None:
            collected, entry = self._cached()
            if collected:
                self._result = collected
                self.source = CACHE
                self.entry = entry
                return True
            if self.client is None:
                return self._fail(
                    "This facility's locations are not cached, so they cannot "
                    "be shown offline."
                )
        collected = []
        try:
            total = len(self.layers) or 1
            for index, layer in enumerate(self.layers):
                if self.isCanceled():
                    return False
                pages = []
                loaded = 0
                for feature_set, features in self.client.iter_layer_features(
                    self.facility_id,
                    layer["id"],
                    where=self.where,
                    limit=self.max_features,
                    is_canceled=self.isCanceled,
                    out_fields=self.out_fields,
                    location_codes=self.location_codes,
                ):
                    pages.append((feature_set, features))
                    loaded += len(features)
                    expected = layer.get("count") or 0
                    # With a location filter the layer count is no longer the
                    # target, so progress within the layer is indeterminate.
                    if expected and not self.location_codes:
                        fraction = min(1.0, loaded / float(expected))
                    else:
                        fraction = 0.5
                    self.setProgress((index + fraction) / total * 100.0)
                    self.setDescription(
                        "EQuIS: %s - %d features" % (layer["name"], loaded)
                    )
                if self.isCanceled():
                    return False
                collected.append({"layer": layer, "pages": pages})
            if self._cacheable and collected:
                pages = collected[0]["pages"]
                self._store(lambda: self.cache.put_locations(
                    self.site, self.facility_id, pages
                ))
            self._result = collected
            self.source = LIVE
            return True
        except EquisError as exc:
            collected, entry = self._cached()
            if collected:
                self._result = collected
                self.source = CACHE_FALLBACK
                self.entry = entry
                self.error = str(exc)
                return True
            return self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._fail("Unexpected error loading features: %s" % exc)

    def build_layers(self):
        """Create the QGIS layers. Call from the main thread after success."""
        built = []
        for entry in self._result or []:
            info = entry["layer"]
            name = "%s - %s" % (self.facility_name, info["name"])
            # The service already prefixes layer names with the facility name.
            if info["name"].startswith(self.facility_name):
                name = info["name"]
            name = cached_layer_name(name, self.from_cache)
            layer, skipped = build_feature_layer(
                name, entry["pages"], fallback_wkid=self.fallback_wkid
            )
            if layer is not None:
                built.append({"layer": layer, "skipped": skipped})
        return built


class PrepareLiveReportTask(_BaseTask):
    """Create this user's report for a facility and read its filter lists.

    Done when a facility is picked rather than at the first fetch, because the
    parameter service only enumerates values within a report's own facility:
    the unscoped base reports answer with nothing, so before this report
    exists there is no way to offer the user a chemical to filter by. Without
    it the first fetch of a large facility would have to run unfiltered, which
    is exactly the request that times out.

    Costs one creation (about 6 s, once per user per facility) plus about 3 s
    of lookups, and runs no report.
    """

    def __init__(self, live_report, facility_id):
        super().__init__("EQuIS: preparing your report")
        self.live_report = live_report
        self.facility_id = facility_id
        # {parameter name: [(value, label), ...]}
        self.choices = {}

    def run(self):
        try:
            self.setProgress(10)
            self.live_report.ensure()
            self.setProgress(40)
            # Tidiness, and never worth failing the step for.
            try:
                self.live_report.file_under_facility()
            except EquisError:
                pass
            self.setProgress(55)

            client = self.live_report.client
            report_id = self.live_report.report_id
            # Every one of these is scoped to the FACILITY only - each
            # declares dependencies:['@facility_id'] and nothing else - so
            # asking after a narrower filter is saved returns a
            # byte-identical list. There is no staged read that would narrow
            # one picker using another's answer.
            #
            # '@analytic_methods' and '@fractions' are deliberately NOT read
            # here even though the service offers them (54 and 7 values).
            # Which methods and fractions actually ran is a property of the
            # ANALYSIS, not the facility, so a facility-wide list would be
            # mostly choices that match nothing in the data about to be
            # fetched. Both are offered from the fetched rows instead, and
            # both filter locally, so nothing is lost by waiting.
            # '@location_groups' is facility-scoped like the rest and answers
            # in ~0.3 s; most facilities return nothing, and the panel hides
            # the picker when they do.
            wanted = ("@cas_rns", "@matrix_codes",
                      "@sample_types", "@sys_loc_codes", "@location_groups")
            for index, name in enumerate(wanted):
                if self.isCanceled():
                    return False
                self.choices[name] = client.report_parameter_values(
                    report_id, name, self.facility_id
                )
                self.setProgress(55 + 45.0 * (index + 1) / len(wanted))
            self._result = self.choices
            return True
        except EquisError as exc:
            return self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - template/permission errors
            return self._fail(str(exc))


class RunReportTask(_BaseTask):
    """Execute a report and hold onto its rows for layer construction."""

    def __init__(self, client, report, facility_id, facility_name, wkid,
                 max_rows=None, out_fields=None, where=None, keep_columns=None,
                 cache=None, site="", prefer_cache=True, use_eia=True,
                 live_report=None, selection=None):
        # Reports default to preferring the cache: a re-run costs minutes and
        # the stored output is the same report run.
        super().__init__("EQuIS: running report", cache, site, prefer_cache)
        self.client = client
        self.report = report
        self.facility_id = facility_id
        self.facility_name = facility_name
        self.wkid = wkid
        self.max_rows = max_rows
        self.out_fields = out_fields
        self.where = where
        self.keep_columns = keep_columns
        self.truncated = False
        # Reports too large to generate inside the service's time limit can
        # only be read from an agent's stored output, so that is tried first
        # when one exists rather than kept as a fallback after a slow failure.
        self.use_eia = use_eia
        self.eia = None
        # The live path saves `selection` onto `live_report` before running
        # it, because EQuIS applies a filter only when it is stored on the
        # report. Both None means the report runs exactly as it is saved.
        self.live_report = live_report
        self.selection = selection
        self.created_report = False
        # What this fetch answers, echoed back so the panel can tell whether
        # a later selection still matches the rows in hand.
        self.selection_key = (
            tuple(sorted((n, v) for n, v in selection.to_parameters().items()
                         if v is not None))
            if selection is not None else None
        )

    @property
    def cache_params(self):
        """The parameters that decide which stored output matches this run."""
        return {
            "max_rows": self.max_rows,
            "where": self.where,
            "keep_columns": list(self.keep_columns or []),
            # An agent's output is a different dataset from a live run - it is
            # whatever the agent was scheduled to produce, frozen at its last
            # execution - so the two must not satisfy each other's cache reads.
            "eia": self.eia["event_id"] if self.eia else None,
            # The saved filters are part of what the report returns, so two
            # selections of one report are two datasets. Without this a
            # dissolved run would be served from a total run's cache entry.
            "selection": (self.selection.to_parameters()
                          if self.selection is not None else None),
        }

    def _cached(self):
        if self.cache is None:
            return None, None
        payload, entry = self.cache.get_report(
            self.site, self.facility_id, self.report["id"], self.cache_params,
            # An uncapped request must not be satisfied by a truncated run:
            # the missing rows are the most recent sample dates, so the time
            # series would silently end early.
            allow_truncated=self.max_rows is not None,
        )
        if not payload:
            return None, None
        return payload, entry

    def _on_rows(self, count):
        self.setDescription("EQuIS: reading report - %s rows" % format(count, ",d"))

    def _find_eia(self):
        """The newest stored agent output for this report, if any.

        Resolved before the cache is consulted because it is part of the cache
        key: output stored by an agent and a live run of the same report are
        different datasets and must not be confused for one another.
        """
        if not self.use_eia or self.client is None or not self.report.get("has_eia"):
            return None
        try:
            agents = self.client.list_report_eias(self.report["id"])
        except EquisError:
            # Discovery failing is not worth losing the run over; the direct
            # path below still works for reports that can be generated.
            return None
        return agents[0] if agents else None

    def run(self):
        if self.client is not None and self.live_report is None:
            # An agent's stored output is frozen and cannot be re-scoped, so
            # it can only answer a request that carries no saved filters.
            self.eia = self._find_eia()

        if self.prefer_cache or self.client is None:
            payload, entry = self._cached()
            if payload:
                self._result = (payload["headers"], payload["rows"])
                self.truncated = payload["truncated"]
                self.source = CACHE
                self.entry = entry
                return True
            if self.client is None:
                return self._fail(
                    "This report has not been run before, so it is not "
                    "available offline."
                )

        if self.eia is not None:
            try:
                self.setProgress(10)
                self.setDescription("EQuIS: reading stored report output")
                headers, rows, truncated = self.client.eia_output(
                    self.eia["report_id"] or self.report["id"],
                    self.eia["event_id"],
                    max_rows=self.max_rows,
                    progress=self._on_rows,
                    is_canceled=self.isCanceled,
                    keep_columns=self.keep_columns,
                )
                if self.isCanceled():
                    return False
                if headers and rows:
                    self.truncated = truncated
                    self._store(lambda: self.cache.put_report(
                        self.site, self.facility_id, self.report["id"],
                        headers, rows, truncated=truncated,
                        params=self.cache_params,
                        label=self.report.get("name", ""),
                    ))
                    self._result = (headers, rows)
                    self.source = EIA
                    return True
                # Stored output that reads as empty is not a reason to give
                # up: fall through and run the report itself.
                self.eia = None
            except EquisError:
                self.eia = None
            except MemoryError:
                return self._fail(
                    "The stored report output was too large to load. Set a "
                    "row limit and try again."
                )

        # The live path writes its filters onto the report first: EQuIS
        # applies a filter only when it is saved, so this is what makes the
        # request narrow enough to generate at all.
        report_id = self.report["id"]
        if self.live_report is not None and self.selection is not None:
            try:
                self.setProgress(5)
                self.setDescription("EQuIS: preparing your report")
                report_id = self.live_report.ensure()
                self.created_report = self.live_report.created
                self.live_report.apply(self.selection)
                # Tidiness only, and it must never cost the user their data.
                try:
                    self.live_report.file_under_facility()
                except EquisError:
                    pass
            except EquisError as exc:
                return self._fail(str(exc))
            except Exception as exc:  # noqa: BLE001 - template/permission
                return self._fail(str(exc))

        try:
            self.setProgress(10)
            headers, rows, truncated = self.client.report_csv(
                report_id,
                self.facility_id,
                max_rows=self.max_rows,
                progress=self._on_rows,
                is_canceled=self.isCanceled,
                out_fields=self.out_fields,
                where=self.where,
                keep_columns=self.keep_columns,
            )
            if self.isCanceled():
                return False
            if not headers:
                return self._fail("The report returned no columns.")
            if not rows:
                return self._fail("The report ran but returned no rows.")
            self.truncated = truncated
            self._store(lambda: self.cache.put_report(
                self.site, self.facility_id, self.report["id"], headers, rows,
                truncated=truncated, params=self.cache_params,
                label=self.report.get("name", ""),
            ))
            self._result = (headers, rows)
            self.source = LIVE
            return True
        except EquisError as exc:
            # A report that failed to run is exactly when a previous run of it
            # is worth having; only offer it when it is not a truncated stand-in
            # for a full request.
            payload, entry = self._cached()
            if payload:
                self._result = (payload["headers"], payload["rows"])
                self.truncated = payload["truncated"]
                self.source = CACHE_FALLBACK
                self.entry = entry
                self.error = str(exc)
                return True
            return self._fail(str(exc))
        except MemoryError:
            return self._fail(
                "The report output was too large to load. Set a row limit and "
                "try again."
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail("Unexpected error running report: %s" % exc)

    def build_layer(self):
        """Create the QGIS layer. Call from the main thread after success."""
        if not self._result:
            return None, 0, 0
        headers, rows = self._result
        name = cached_layer_name(
            "%s - %s" % (self.facility_name, self.report["name"]),
            self.from_cache,
        )
        return build_report_layer(name, headers, rows, self.wkid)


class FetchManifestTask(_BaseTask):
    """Fetch a published manifest and open the store, off the UI thread.

    WHY THE WHOLE OPEN AND NOT JUST THE FETCH. The manifest itself is a
    1.5 KB GET - 0.00 s locally, well under a second over Azure. The time
    is everywhere else, and it was all happening on the UI thread under a
    message that said "Reading the parquet manifest", which was both a long
    wait and a lie about what was happening. Measured 2026-09-23 on the
    real 3,452,146-row facility:

        fetch manifest           0.00 s      (0.7 s of footers over Azure)
        open + row-count check   0.41 s
        location points          0.19 s
        chemical list            0.21 s
        warming the columns      2.28 s   <- three quarters of the wait

    So every phase reports what it is doing through `note`, and the panel
    shows it. The DuckDB connection is created HERE and used on the main
    thread afterwards - see the note in _on_manifest_ready about why that
    is safe for this task and not for BuildStoreTask.
    """

    note = pyqtSignal(str)

    def __init__(self, url, timeout=30):
        super().__init__("Opening the published parquet")
        self.url = url
        self.timeout = timeout
        self.manifest = None
        self.store = None

    def _say(self, message, progress=None):
        # Progress BEFORE the note: the two are read together, and setting
        # it afterwards leaves every message paired with the previous
        # step's percentage.
        if progress is not None:
            self.setProgress(progress)
        self.note.emit(message)

    def run(self):
        try:
            from .store import ReportStore
        except ImportError as exc:
            return self._fail("duckdb is not available: %s" % exc)

        self._say("Reading manifest.json…", 5)
        try:
            text = ReportStore._fetch(
                ReportStore._join(self.url, ReportStore.MANIFEST_NAME),
                self.timeout)
        except Exception as exc:               # noqa: BLE001
            return self._fail(str(exc))
        if self.isCanceled():
            return False

        import json
        try:
            manifest = json.loads(text)
        except ValueError as exc:
            return self._fail("manifest.json is not valid JSON: %s" % exc)
        self.manifest = manifest
        partitions = manifest.get("partitions") or []
        declared = manifest.get("rows")
        self._say("Found %d parquet file(s)%s."
                  % (len(partitions),
                     ", %s rows declared" % format(declared, ",d")
                     if isinstance(declared, int) else ""), 15)
        if self.isCanceled():
            return False

        self._say("Reading %d parquet header(s)…" % len(partitions), 25)
        try:
            store = ReportStore.from_manifest(self.url, timeout=self.timeout)
        except Exception as exc:               # noqa: BLE001
            return self._fail(str(exc))
        if self.isCanceled():
            store.close()
            return False
        self._say("%s rows confirmed." % format(store.count(), ",d"), 40)

        # The slow part, and the one that used to land on the first analyte
        # click instead. Named step by step so the wait is legible.
        # Warming is ~70% of the wait, so it gets most of the bar rather
        # than leaving it parked at 40 for the longest phase.
        steps = {"Reading analytic methods…": 50,
                 "Reading fractions…": 60,
                 "Warming DuckDB…": 70}
        try:
            store.warm(note=lambda m: self._say(m, steps.get(m)))
        except Exception:                      # noqa: BLE001
            pass                               # an optimisation, never fatal
        if self.isCanceled():
            store.close()
            return False

        self.store = store
        self._result = text
        self._say("Ready.", 100)
        return True


class BuildStoreTask(_BaseTask):
    """Load a fetched report into a DuckDB file, off the UI thread.

    Ingest is ~7.4 s on a 460,000-row report - fine in the background, an
    unacceptable freeze on the main thread. The task writes a FILE and closes
    it; the panel opens it again afterwards. That is deliberate: a DuckDB
    connection should not be handed between threads, and a file handoff is
    unambiguously safe where passing a live connection is merely usually
    fine. tools/test_store_handoff.py exercises exactly this.

    Failure is survivable by design. The store is an accelerator, not the
    source of truth: if duckdb is missing, the disk is full, or the build
    raises anything at all, the panel keeps using ReportTable and the user
    loses some speed rather than their afternoon.
    """

    def __init__(self, table, path, with_raw=False):
        super().__init__("Indexing report data")
        self.table = table
        self.path = path
        self.with_raw = with_raw

    def run(self):
        try:
            from .store import build_store_file
        except ImportError as exc:
            return self._fail("duckdb is not available: %s" % exc)
        try:
            self._result = build_store_file(self.table, self.path,
                                            with_raw=self.with_raw)
        except Exception as exc:               # noqa: BLE001 - see docstring
            return self._fail("%s: %s" % (type(exc).__name__, exc))
        if self.isCanceled():
            # A cancelled build leaves a half-written file that must not be
            # opened later as though it were complete.
            try:
                import os
                os.unlink(self.path)
            except OSError:
                pass
            return False
        return True


class AnalysisTask(_BaseTask):
    """Answer one Analysis-chooser choice off the UI thread.

    On the UI thread this froze all of QGIS: water's every-analyte pivot is
    3-4 s, a pair's table, breaks and stations up to a second more. Here
    it runs on the Session's own DuckDB cursor (analysis.Session.db),
    which releases the GIL while it works: measured 2026-10-11, the main
    thread ticked with a worst gap of 88 ms through a 4.1 s water build,
    querying the store all the while.

    What Session.respond says and sends is relayed by `message`, a signal:
    emitted here, delivered on the main thread, which owns the web view.
    The result - a Scatter or None - comes back through finishedOk; the
    map layer is built there, in finished(), where QGIS allows it.
    """

    message = pyqtSignal(str, object)
    cancelledByUser = pyqtSignal()

    def __init__(self, session, choice, locations=None, window=False):
        super().__init__("EQuIS: preparing the analysis view")
        self.session = session
        self.choice = choice
        self.locations = locations
        # Not False: re-pick the drawn scatter's points for this time window
        # (Session.repoint) instead of answering a choice. None is "no
        # window". The map's Temporal Controller drives it.
        self.window = window

    def run(self):
        def say(text, where):
            self.message.emit("working", {"message": text, "where": where})

        try:
            if self.window is not False:
                self._result = self.session.repoint(window=self.window)
            else:
                self._result = self.session.respond(
                    self.choice, say, self.message.emit,
                    locations=self.locations)
        except Exception as exc:               # noqa: BLE001 - the page asked
            # and must be told; analysis.py's own refusals read as sentences.
            return self._fail(str(exc) if isinstance(exc, ValueError)
                              else "%s: %s" % (type(exc).__name__, exc))
        return True

    def cancel(self):
        # A query in flight does not notice a cancelled task; interrupt it.
        try:
            self.session.db.con.interrupt()
        except Exception:                      # noqa: BLE001 - best effort
            pass
        super().cancel()

    def finished(self, ok):
        if self.isCanceled():
            # _BaseTask says nothing on cancel; the controller must still
            # learn the task is over, or it would wait for it forever.
            self.cancelledByUser.emit()
            return
        super().finished(ok)
