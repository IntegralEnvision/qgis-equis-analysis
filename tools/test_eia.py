"""Reading a report from an information agent's stored output.

Needs QGIS (the task layer is a QgsTask) but no network:

  "C:\\Program Files\\QGIS 4.0.2\\bin\\python-qgis.bat" tools\\test_eia.py

Reports whose generation exceeds the service's time limit answer with an HTTP
500 and cannot be read any other way, so the agent path is not an optimisation
for those - it is the only route. These cases cover the decision to use it,
what happens when it fails, and the thing most likely to go quietly wrong:
agent output and a live run are different datasets and must not satisfy each
other's cache reads.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from qgis.core import QgsApplication  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", ""), True)
app = QgsApplication([], False)
app.initQgis()

from equis_loader.cache import report_params_key  # noqa: E402
from equis_loader.equis_api import EquisError  # noqa: E402
from equis_loader.tasks import CACHE, EIA, LIVE, RunReportTask  # noqa: E402

HEADERS = ["SYS_LOC_CODE", "SAMPLE_DATE", "CHEMICAL_NAME",
           "REPORT_RESULT_VALUE", "REPORT_RESULT_UNIT", "DETECT_FLAG"]
AGENT_ROWS = [["MW-1", "2/11/2009 12:00:00 AM", "Benzene", "0.5", "ug/L", "Y"]]
LIVE_ROWS = [["MW-9", "2020-01-02T00:00:00", "Toluene", "1.25", "ug/L", "Y"]]

AGENT = {
    "event_id": 4242,
    "name": "Nightly ARII",
    "report_id": 993787,
    "report_name": "ARII All Data",
    "format": "csv",
    "attachment": "arii.csv",
    "executed": "2026-04-01T16:40:43",
}


class FakeClient(object):
    """Stands in for EquisClient, recording which path was taken."""

    def __init__(self, agents=(), eia_error=None, report_error=None):
        self.agents = list(agents)
        self.eia_error = eia_error
        self.report_error = report_error
        self.calls = []

    def list_report_eias(self, report_id):
        self.calls.append("list")
        return list(self.agents)

    def eia_output(self, report_id, event_id, **kwargs):
        self.calls.append("eia")
        if self.eia_error:
            raise self.eia_error
        rows = AGENT_ROWS
        if kwargs.get("max_rows") == 0:
            rows = []
        return list(HEADERS), [list(r) for r in rows], False

    def report_csv(self, report_id, facility_id, **kwargs):
        self.calls.append("report")
        if self.report_error:
            raise self.report_error
        return list(HEADERS), [list(r) for r in LIVE_ROWS], False


def make_task(client, report, cache=None, prefer_cache=False, use_eia=True):
    return RunReportTask(
        client, report, 1000001, "Facility_A", 4326,
        cache=cache, site="https://example.invalid",
        prefer_cache=prefer_cache, use_eia=use_eia,
    )


WITH_EIA = {"id": 4321, "name": "Analytical Results II", "has_eia": True}
NO_EIA = {"id": 4321, "name": "Analytical Results II", "has_eia": False}


class FakeCache(object):
    """Just enough cache to prove the key separates the two sources.

    Keys through the real report_params_key so this exercises the shipped
    hashing rather than a stand-in that might separate entries the real one
    would collide.
    """

    def __init__(self):
        self.stored = {}

    @staticmethod
    def _key(facility_id, report_id, params):
        return (facility_id, report_id, report_params_key(**(params or {})))

    def get_report(self, site, facility_id, report_id, params,
                   allow_truncated=False):
        found = self.stored.get(self._key(facility_id, report_id, params))
        return (found, None) if found else (None, None)

    def put_report(self, site, facility_id, report_id, headers, rows,
                   truncated=False, params=None, label=""):
        self.stored[self._key(facility_id, report_id, params)] = {
            "headers": headers, "rows": rows, "truncated": truncated,
        }
        return None


def main():
    # --- the agent path is preferred when one exists -------------------
    client = FakeClient(agents=[AGENT])
    task = make_task(client, WITH_EIA)
    ok = task.run()
    check("agent: a report with stored output reads it instead of running",
          ok and task.source == EIA, "source=%s calls=%s" % (task.source, client.calls))
    check("agent: and the report itself is never run",
          "report" not in client.calls, str(client.calls))
    check("agent: the rows are the stored ones",
          task._result and task._result[1] == AGENT_ROWS, str(task._result))
    check("agent: which agent was read is recorded for the panel to say",
          task.eia is not None and task.eia["event_id"] == 4242)

    # --- a report with no agent behaves exactly as before ---------------
    client = FakeClient(agents=[])
    task = make_task(client, NO_EIA)
    ok = task.run()
    check("no agent: the report runs directly",
          ok and task.source == LIVE, "source=%s" % task.source)
    check("no agent: discovery is not even attempted when the flag is unset",
          client.calls == ["report"], str(client.calls))

    # --- the flag says yes but no agent has produced output ------------
    client = FakeClient(agents=[])
    task = make_task(client, WITH_EIA)
    ok = task.run()
    check("flagged but empty: falls through to running the report",
          ok and task.source == LIVE, "source=%s calls=%s" % (task.source, client.calls))

    # --- reading stored output fails ------------------------------------
    client = FakeClient(agents=[AGENT], eia_error=EquisError("gone"))
    task = make_task(client, WITH_EIA)
    ok = task.run()
    check("agent fails: the report is run rather than the load being lost",
          ok and task.source == LIVE, "source=%s calls=%s" % (task.source, client.calls))
    check("agent fails: and the agent is not claimed as the source",
          task.eia is None)

    # --- both fail -------------------------------------------------------
    client = FakeClient(agents=[AGENT], eia_error=EquisError("gone"),
                        report_error=EquisError("timed out"))
    task = make_task(client, WITH_EIA)
    ok = task.run()
    check("both fail: the task fails with the report's own error",
          not ok and "timed out" in (task.error or ""), str(task.error))

    # --- use_eia=False is honoured ---------------------------------------
    client = FakeClient(agents=[AGENT])
    task = make_task(client, WITH_EIA, use_eia=False)
    task.run()
    check("opt out: use_eia=False runs the report and skips discovery",
          task.source == LIVE and client.calls == ["report"], str(client.calls))

    # --- the cache must not confuse the two sources ----------------------
    cache = FakeCache()
    agent_task = make_task(FakeClient(agents=[AGENT]), WITH_EIA, cache=cache)
    agent_task.run()
    live_task = make_task(FakeClient(agents=[]), NO_EIA, cache=cache)
    live_task.run()
    check("cache: agent output and a live run are stored separately",
          len(cache.stored) == 2, "%d entries" % len(cache.stored))

    # A second agent run reads its own cached copy, not the live one.
    again = make_task(FakeClient(agents=[AGENT]), WITH_EIA, cache=cache,
                      prefer_cache=True)
    again.run()
    check("cache: a cached agent read returns the agent's rows",
          again.source == CACHE and again._result[1] == AGENT_ROWS,
          "source=%s rows=%s" % (again.source, again._result[1]))

    # And a live run prefers its own cached copy rather than the agent's.
    again_live = make_task(FakeClient(agents=[]), NO_EIA, cache=cache,
                           prefer_cache=True)
    again_live.run()
    check("cache: a cached live run returns the live rows",
          again_live.source == CACHE and again_live._result[1] == LIVE_ROWS,
          "source=%s rows=%s" % (again_live.source, again_live._result[1]))

    print("\n%s" % ("ALL EIA TESTS PASSED" if not FAILURES
                    else "FAILURES: %s" % ", ".join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    code = main()
    app.exitQgis()
    sys.exit(code)
