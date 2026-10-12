"""Exercise the cache decision logic in tasks.py without QGIS.

Runs on a plain interpreter (no QGIS, no network):

  python tools\\test_tasks.py

tasks.py imports qgis.core and qgis.PyQt, so this installs stub modules first,
then loads tasks.py normally. Only the cache branching is under test; the
QgsTask machinery is not.
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


# ---------------------------------------------------------------- qgis stubs

class _StubTask:
    class Flag:
        CanCancel = 1

    def __init__(self, description="", flags=None):
        self._description = description
        self._canceled = False

    def setProgress(self, value): pass
    def setDescription(self, text): self._description = text
    def isCanceled(self): return self._canceled


class _StubSignal:
    def __init__(self, *a, **k): pass
    def __get__(self, obj, objtype=None): return self
    def connect(self, *a, **k): pass
    def emit(self, *a, **k): pass


qgis = types.ModuleType("qgis")
qgis_core = types.ModuleType("qgis.core")
qgis_core.QgsTask = _StubTask
qgis_pyqt = types.ModuleType("qgis.PyQt")
qgis_qtcore = types.ModuleType("qgis.PyQt.QtCore")
qgis_qtcore.pyqtSignal = lambda *a, **k: _StubSignal()
sys.modules.update({
    "qgis": qgis, "qgis.core": qgis_core,
    "qgis.PyQt": qgis_pyqt, "qgis.PyQt.QtCore": qgis_qtcore,
})

# layer_builder pulls in the real QGIS API; tasks.py only needs the two names.
layer_builder = types.ModuleType("layer_builder")
layer_builder.build_feature_layer = lambda *a, **k: (None, 0)
layer_builder.build_report_layer = lambda *a, **k: (None, 0, 0)

# Load the package modules by hand so the relative imports resolve.
pkg = types.ModuleType("equis_loader")
pkg.__path__ = [os.path.join(ROOT, "equis_loader")]
sys.modules["equis_loader"] = pkg
sys.modules["equis_loader.layer_builder"] = layer_builder


def _load(name):
    spec = importlib.util.spec_from_file_location(
        "equis_loader." + name, os.path.join(ROOT, "equis_loader", name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["equis_loader." + name] = module
    spec.loader.exec_module(module)
    return module


ec = _load("cache")
equis_api = _load("equis_api")
tasks = _load("tasks")


# --------------------------------------------------------------- fake client

class FakeClient:
    """Stands in for EquisClient; can be told to fail like the real one does."""

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def _maybe_fail(self, what):
        self.calls.append(what)
        if self.fail:
            raise equis_api.EquisError("Could not reach host (simulated outage)")

    def list_facilities(self):
        self._maybe_fail("list_facilities")
        return [{"id": 1, "code": "A", "name": "Alpha", "wkid": 26917}]

    def facility_layers(self, fid):
        self._maybe_fail("facility_layers")
        return [{"id": 1, "name": "VW_LOCATION"}], 26917

    def layer_count(self, fid, lid, where="1=1"):
        return 2

    def list_reports(self, fid=None):
        return [{"id": 77, "name": "ARII", "code": "ARII", "type": "",
                 "facility_id": fid, "spatial": False}]

    def iter_layer_features(self, fid, lid, **kwargs):
        self._maybe_fail("iter_layer_features")
        yield (
            {"fields": [{"name": "code", "type": "esriFieldTypeString"}],
             "spatialReference": {"wkid": 0, "latestWkid": 26917},
             "geometryType": "esriGeometryPoint"},
            [{"attributes": {"code": "MW-1"}, "geometry": {"x": 1.0, "y": 2.0}}],
        )

    def report_csv(self, report_id, facility_id, **kwargs):
        self._maybe_fail("report_csv")
        headers = ["SYS_LOC_CODE", "SAMPLE_DATE", "CHEMICAL_NAME",
                   "RESULT_NUMERIC", "DETECT_FLAG"]
        rows = [["MW-1", "01/01/2020", "Benzene", "5.0", "Y"]]
        max_rows = kwargs.get("max_rows")
        if max_rows is not None:
            return headers, rows[:max_rows], True
        return headers, rows, False


SITE = "https://test.equisonline.com"
REPORT = {"id": 77, "name": "ARII"}
KEEP = ["SYS_LOC_CODE", "SAMPLE_DATE", "CHEMICAL_NAME", "RESULT_NUMERIC",
        "DETECT_FLAG"]


def main():
    root = tempfile.mkdtemp(prefix="equis_tasks_")
    try:
        cache = ec.EquisCache(root)

        # --- live run writes through -----------------------------------
        client = FakeClient()
        t = tasks.RunReportTask(client, REPORT, 1, "Alpha", 26917,
                                keep_columns=KEEP, cache=cache, site=SITE,
                                prefer_cache=True)
        ok = t.run()
        check("task: first run goes live", ok and t.source == tasks.LIVE,
              t.source)
        check("task: live run populated the cache",
              cache.report_entry(SITE, 1, 77, t.cache_params) is not None)

        # --- second run is served from cache, no client call ------------
        client2 = FakeClient()
        t2 = tasks.RunReportTask(client2, REPORT, 1, "Alpha", 26917,
                                 keep_columns=KEEP, cache=cache, site=SITE,
                                 prefer_cache=True)
        ok = t2.run()
        check("task: second run is served from cache",
              ok and t2.source == tasks.CACHE, t2.source)
        check("task: a cached run never touches the network",
              client2.calls == [], repr(client2.calls))
        check("task: cached rows match the live ones",
              t2._result == t._result)

        # --- refresh forces a live run ----------------------------------
        client3 = FakeClient()
        t3 = tasks.RunReportTask(client3, REPORT, 1, "Alpha", 26917,
                                 keep_columns=KEEP, cache=cache, site=SITE,
                                 prefer_cache=False)
        ok = t3.run()
        check("task: refresh bypasses the cache",
              ok and t3.source == tasks.LIVE and "report_csv" in client3.calls,
              t3.source)

        # --- outage falls back to cache ---------------------------------
        down = FakeClient(fail=True)
        t4 = tasks.RunReportTask(down, REPORT, 1, "Alpha", 26917,
                                 keep_columns=KEEP, cache=cache, site=SITE,
                                 prefer_cache=False)
        ok = t4.run()
        check("task: an outage falls back to the cached copy",
              ok and t4.source == tasks.CACHE_FALLBACK, t4.source)
        check("task: the fallback keeps the error for the UI to quote",
              "simulated outage" in (t4.error or ""), t4.error)
        check("task: from_cache is true for both cache sources",
              t2.from_cache and t4.from_cache and not t.from_cache)

        # --- offline with no client at all -------------------------------
        t5 = tasks.RunReportTask(None, REPORT, 1, "Alpha", 26917,
                                 keep_columns=KEEP, cache=cache, site=SITE,
                                 prefer_cache=True)
        ok = t5.run()
        check("task: offline serves the cache with no client",
              ok and t5.source == tasks.CACHE, t5.source)

        t6 = tasks.RunReportTask(None, {"id": 999, "name": "Never run"}, 1,
                                 "Alpha", 26917, keep_columns=KEEP,
                                 cache=cache, site=SITE, prefer_cache=True)
        ok = t6.run()
        check("task: offline fails clearly for an uncached report",
              not ok and "not available offline" in t6.error, t6.error)

        # --- truncation must not satisfy an uncapped request -------------
        capped = tasks.RunReportTask(FakeClient(), REPORT, 2, "Beta", 26917,
                                     max_rows=1, keep_columns=KEEP,
                                     cache=cache, site=SITE, prefer_cache=True)
        capped.run()
        check("task: a capped run records truncation", capped.truncated is True)
        full = tasks.RunReportTask(None, REPORT, 2, "Beta", 26917,
                                   keep_columns=KEEP, cache=cache, site=SITE,
                                   prefer_cache=True)
        ok = full.run()
        check("task: a truncated cache entry cannot satisfy an uncapped run",
              not ok, "source=%s truncated=%s" % (full.source, full.truncated))
        recap = tasks.RunReportTask(None, REPORT, 2, "Beta", 26917, max_rows=1,
                                    keep_columns=KEEP, cache=cache, site=SITE,
                                    prefer_cache=True)
        ok = recap.run()
        check("task: the same capped request is still served from cache",
              ok and recap.source == tasks.CACHE and recap.truncated is True)

        # --- facilities --------------------------------------------------
        ft = tasks.FetchFacilitiesTask(FakeClient(), cache=cache, site=SITE)
        ft.run()
        check("task: facilities cache on a live fetch",
              ft.source == tasks.LIVE
              and cache.get_facilities(SITE)[0] is not None)
        ft2 = tasks.FetchFacilitiesTask(FakeClient(fail=True), cache=cache,
                                        site=SITE)
        ok = ft2.run()
        check("task: facilities fall back when the site is unreachable",
              ok and ft2.source == tasks.CACHE_FALLBACK, ft2.source)
        ft3 = tasks.FetchFacilitiesTask(FakeClient(fail=True), cache=cache,
                                        site="https://never.seen.example")
        ok = ft3.run()
        check("task: an unreachable, never-cached site still fails",
              not ok and "simulated outage" in ft3.error, ft3.error)

        # --- locations ---------------------------------------------------
        lt = tasks.LoadLayersTask(FakeClient(), 1, "Alpha",
                                  [{"id": 1, "name": "VW_LOCATION", "count": 2}],
                                  26917, cache=cache, site=SITE)
        lt.run()
        check("task: locations cache on a live load",
              lt.source == tasks.LIVE
              and cache.get_locations(SITE, 1)[0] is not None)
        lt2 = tasks.LoadLayersTask(None, 1, "Alpha",
                                   [{"id": 1, "name": "VW_LOCATION"}], 26917,
                                   cache=cache, site=SITE, prefer_cache=True)
        ok = lt2.run()
        pages = lt2._result[0]["pages"] if ok else []
        check("task: cached locations restore in the builder's page shape",
              ok and isinstance(pages[0], tuple)
              and pages[0][1][0]["geometry"]["x"] == 1.0)

        # A filtered read is not the whole layer and must not be cached as it.
        filtered = tasks.LoadLayersTask(FakeClient(), 5, "Gamma",
                                        [{"id": 1, "name": "VW_LOCATION"}],
                                        26917, location_codes=["MW-1"],
                                        cache=cache, site=SITE)
        filtered.run()
        check("task: a filtered location read is not cached as the full layer",
              cache.get_locations(SITE, 5)[0] is None)

        # --- layer naming -------------------------------------------------
        check("task: cached layers are marked in their name",
              tasks.cached_layer_name("Alpha - Benzene", True)
              == "Alpha - Benzene [cached]")
        check("task: live layers are left alone",
              tasks.cached_layer_name("Alpha - Benzene", False)
              == "Alpha - Benzene")
        check("task: the marker never stacks on a rebuild",
              tasks.cached_layer_name(
                  tasks.cached_layer_name("A", True), True) == "A [cached]")

        # --- a broken cache must not break a live run ---------------------
        class Exploding:
            max_bytes = 0
            def get_report(self, *a, **k): return None, None
            def report_entry(self, *a, **k): return None
            def put_report(self, *a, **k): raise OSError("disk full")

        survivor = tasks.RunReportTask(FakeClient(), REPORT, 9, "Delta", 26917,
                                       keep_columns=KEEP, cache=Exploding(),
                                       site=SITE, prefer_cache=True)
        ok = survivor.run()
        check("task: a failed cache write never costs the live result",
              ok and survivor.source == tasks.LIVE and survivor._result)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n%s" % ("ALL TASK TESTS PASSED" if not FAILURES
                    else "FAILURES: %s" % ", ".join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
