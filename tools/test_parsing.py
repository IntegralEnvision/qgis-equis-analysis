"""Offline tests for report parsing and coordinate detection.

Runs on a plain interpreter (no QGIS, no network):

  python tools\\test_parsing.py
"""

import importlib.util
import io
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# equis_api deliberately has no QGIS imports, so load it directly.
_spec = importlib.util.spec_from_file_location(
    "equis_api", os.path.join(ROOT, "equis_loader", "equis_api.py")
)
equis_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(equis_api)

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


class StubClient(equis_api.EquisClient):
    """An EquisClient whose HTTP layer returns a canned payload."""

    def __init__(self, payload, content_type="text/csv"):
        self._payload = payload
        self._content_type = content_type

    def _request(self, path, params=None, timeout=None, accept=None, retries=None):
        self.last_accept = accept
        return self._payload, self._content_type


class CapturingStub(StubClient):
    """A stub that also records the query parameters it was called with."""

    def _request(self, path, params=None, timeout=None, accept=None, retries=None):
        self.last_params = params
        return super()._request(path, params, timeout, accept, retries)


def parse(payload, content_type="text/csv", **kwargs):
    return StubClient(payload, content_type).report_csv(1, 1, **kwargs)


def test_report_parsing():
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as archive:
        archive.writestr("report.csv", "A,B\n7,8\n")

    cases = [
        ("plain csv", (b"A,B,C\n1,2,3\n4,5,6\n", "text/csv"), (3, 2)),
        ("utf-8 bom", ("﻿A,B,C\n1,2,3\n".encode(), "text/csv"), (3, 1)),
        ("trailing comma in header", (b"A,B,C,\n1,2,3,\n", "text/csv"), (3, 1)),
        ("leading blank lines", (b"\n\nA,B,C\n1,2,3\n", "text/csv"), (3, 1)),
        ("title line before header", (b"Title\n\nA,B,C\n1,2,3\n", "text/csv"), (3, 1)),
        ("crlf line endings", (b"A,B,C\r\n1,2,3\r\n", "text/csv"), (3, 1)),
        ("quoted embedded commas", (b'"A","B"\n"x,y","2"\n', "text/csv"), (2, 1)),
        ("blank rows skipped", (b"A,B\n1,2\n\n,\n3,4\n", "text/csv"), (2, 2)),
        ("json array", (b'[{"A":1,"B":"x"},{"A":2,"B":null}]', "application/json"), (2, 2)),
        ("json wrapped in rows", (b'{"rows":[{"A":1,"B":2}]}', "application/json"), (2, 1)),
        # The service also returns an envelope whose row key is not one of the
        # usual names, alongside schema/stats metadata.
        ("json envelope, odd key",
         (b'{"stats":null,"schema":{"title":"t","properties":{"A":{}}},'
          b'"EX - Report":[{"A":1,"B":2},{"A":3,"B":4}]}', "application/json"),
         (2, 2)),
        ("zipped csv", (zbuf.getvalue(), "application/zip"), (2, 1)),
        ("single column report", (b"ONLY\n1\n2\n", "text/csv"), (1, 2)),
        ("empty body", (b"", "text/csv"), (0, 0)),
        ("whitespace only", (b"\n\n\n", "text/csv"), (0, 0)),
    ]
    for label, (payload, ctype), (want_cols, want_rows) in cases:
        headers, rows, _ = parse(payload, ctype)
        check("parse: " + label,
              len(headers) == want_cols and len(rows) == want_rows,
              "%d cols x %d rows (wanted %d x %d)" % (
                  len(headers), len(rows), want_cols, want_rows))

    headers, rows, truncated = parse(b"A,B\n1,2\n3,4\n5,6\n", max_rows=2)
    check("parse: max_rows truncates", len(rows) == 2 and truncated,
          "%d rows, truncated=%s" % (len(rows), truncated))

    # Regression: asking for JSON in the Accept header makes the service
    # answer report requests with a JSON envelope even when oType=csv, so the
    # report call must request CSV explicitly.
    stub = StubClient(b"A,B\n1,2\n")
    stub.report_csv(1, 1)
    check("report request does not ask for json",
          stub.last_accept is not None and "json" not in stub.last_accept.lower(),
          repr(stub.last_accept))

    # The server-side row cap is what keeps these responses small.
    cap = CapturingStub(b"A,B\n1,2\n")
    cap.report_csv(1, 1, max_rows=500)
    check("max_rows is passed to the server",
          cap.last_params.get("resultRecordCount") == 500,
          repr(cap.last_params))

    # Location filtering: the report endpoint honours `where`, so the plugin
    # builds the clause from a user-entered code list.
    cases = [
        ("a,b", ["a", "b"]),
        (" a , b ,, c ", ["a", "b", "c"]),
        ("'MW-1', \"MW-2\"", ["MW-1", "MW-2"]),
        ("", []),
        (None, []),
    ]
    ok = all(equis_api.parse_code_list(text) == want for text, want in cases)
    check("parse_code_list handles messy input", ok)

    check("location_where builds an IN clause",
          equis_api.EquisClient.location_where(["MW-1", "MW-2"])
          == "SYS_LOC_CODE IN ('MW-1','MW-2')",
          equis_api.EquisClient.location_where(["MW-1", "MW-2"]))
    check("location_where escapes quotes",
          equis_api.EquisClient.location_where(["O'Brien"])
          == "SYS_LOC_CODE IN ('O''Brien')",
          equis_api.EquisClient.location_where(["O'Brien"]))
    check("location_where is None when empty",
          equis_api.EquisClient.location_where([]) is None
          and equis_api.EquisClient.location_where("") is None)

    where_stub = CapturingStub(b"A,B\n1,2\n")
    where_stub.report_csv(1, 1, where="SYS_LOC_CODE IN ('MW-1')")
    check("where is sent to the report endpoint",
          where_stub.last_params.get("where") == "SYS_LOC_CODE IN ('MW-1')",
          repr(where_stub.last_params.get("where")))

    # Transient 5xx should be retried; 4xx should fail immediately. This drives
    # the real _request loop with urlopen and sleep stubbed out.
    import urllib.error
    import urllib.request

    calls = {"n": 0}

    def make_urlopen(codes):
        def fake_urlopen(req, **kwargs):
            calls["n"] += 1
            code = codes.pop(0)
            if code:
                raise urllib.error.HTTPError(req.full_url, code, "err", None, None)

            class Resp:
                headers = {"Content-Type": "text/csv"}

                def read(self):
                    return b"ok"

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            return Resp()
        return fake_urlopen

    real_urlopen, real_sleep = urllib.request.urlopen, equis_api.time.sleep
    urllib.request.urlopen = make_urlopen([500, 500, 0])
    equis_api.time.sleep = lambda _s: None
    try:
        client = equis_api.EquisClient("https://example.invalid", "key")
        body, _ = client._request("/x")
        check("retries transient 500s", body == b"ok" and calls["n"] == 3,
              "%d attempts" % calls["n"])

        calls["n"] = 0
        urllib.request.urlopen = make_urlopen([400, 0])
        try:
            client._request("/x")
            check("does not retry 4xx", False, "no error raised")
        except equis_api.EquisError:
            check("does not retry 4xx", calls["n"] == 1, "%d attempts" % calls["n"])
    finally:
        urllib.request.urlopen, equis_api.time.sleep = real_urlopen, real_sleep

    # Cached reports ignore outFields and return every column, so unwanted
    # columns are dropped while parsing instead.
    wide = (b"SYS_LOC_CODE,JUNK1,CHEMICAL_NAME,JUNK2,RESULT_NUMERIC,SAMPLE_DATE\n"
            b"MW-1,x,TCE,y,5.0,01/01/2020\n"
            b"MW-2,x,TCE,y,6.0,01/01/2021\n")
    headers, rows, _ = parse(
        wide, keep_columns=("SYS_LOC_CODE", "CHEMICAL_NAME", "RESULT_NUMERIC",
                            "SAMPLE_DATE"))
    check("keep_columns drops unwanted columns",
          headers == ["SYS_LOC_CODE", "CHEMICAL_NAME", "RESULT_NUMERIC",
                      "SAMPLE_DATE"],
          str(headers))
    check("keep_columns keeps the values aligned",
          rows == [["MW-1", "TCE", "5.0", "01/01/2020"],
                   ["MW-2", "TCE", "6.0", "01/01/2021"]],
          str(rows[0]))

    # If none of the wanted columns exist, keep everything rather than
    # returning an empty table.
    headers, rows, _ = parse(b"A,B\n1,2\n", keep_columns=("NOPE", "ALSO_NOPE"))
    check("keep_columns falls back when nothing matches",
          headers == ["A", "B"] and rows == [["1", "2"]], str(headers))

    # Output that is not row-delimited must be rejected, not mis-parsed.
    wide = b",".join(b"col%d" % i for i in range(equis_api.MAX_REPORT_COLUMNS + 5))
    try:
        parse(wide, "text/plain")
        check("parse: rejects non-delimited output", False, "no error raised")
    except equis_api.EquisError:
        check("parse: rejects non-delimited output", True)


def test_coordinate_detection():
    # Imported lazily: layer_builder needs QGIS, so only run when available.
    try:
        sys.path.insert(0, ROOT)
        from equis_loader.layer_builder import find_coordinate_columns
    except ImportError:
        print("[SKIP] coordinate detection (QGIS not importable)")
        return

    cases = [
        (["SYS_LOC_CODE", "X_COORD", "Y_COORD", "RESULT"], (1, 2)),
        (["LOC", "LONGITUDE", "LATITUDE"], (1, 2)),
        (["a", "easting", "northing"], (1, 2)),
        (["A", "B", "C"], (-1, -1)),
    ]
    for headers, expected in cases:
        got = find_coordinate_columns(headers)
        check("coords: %s" % ",".join(headers[:3]), got == expected,
              "got %s wanted %s" % (got, expected))


def test_paging():
    """Draining a layer must survive the service's tail behaviour.

    Observed live: near the end of a layer, large pages are rejected with a
    500 while small ones succeed at the same offset (offset 10000 + size 600
    failed; size 100 returned rows). The reader has to step the page size
    down rather than lose the tail.
    """
    class PagingStub(equis_api.EquisClient):
        def __init__(self, total, max_tail_page=100):
            self.base = "https://example.invalid"
            self.api_key = "k"
            self._ssl_ctx = None
            self.total = total
            self.max_tail_page = max_tail_page
            self.requests = []

        def layer_count(self, facility_id, layer_id, where="1=1"):
            return self.total

        def query_layer_page(self, facility_id, layer_id, offset, where="1=1",
                             page_size=equis_api.PAGE_SIZE, out_fields="*",
                             return_geometry=True):
            self.requests.append((offset, page_size))
            # Mimic the real service: past a round-thousand tail boundary only
            # small pages are honoured; anything larger errors.
            tail = self.total - (self.total % 1000)
            if offset >= tail and page_size > self.max_tail_page:
                raise equis_api.EquisError("EQuIS returned HTTP 500")
            count = min(page_size, self.total - offset)
            if count <= 0:
                return {"fields": [], "features": []}
            return {
                "fields": [{"name": "code", "type": "esriFieldTypeString"}],
                "features": [{"attributes": {"code": "c%d" % (offset + i)}}
                             for i in range(count)],
            }

    real_sleep = equis_api.time.sleep
    equis_api.time.sleep = lambda _s: None
    try:
        # Preferred path: one request for the whole layer. maxRecordCount is
        # advertised as 1000 but not enforced, and single reads avoid the
        # service's unreliable behaviour on the final pages.
        stub = PagingStub(10636)
        pages = list(stub.iter_layer_features(1, 1))
        fetched = sum(len(f) for _, f in pages)
        check("drains the whole layer in one request",
              fetched == 10636 and len(stub.requests) == 1,
              "%d of 10636 over %d request(s)" % (fetched, len(stub.requests)))

        # Fallback: when the single read is refused, page and step the size
        # down rather than losing the tail.
        class NoSingleReadStub(PagingStub):
            def query_layer_page(self, facility_id, layer_id, offset,
                                 where="1=1", page_size=equis_api.PAGE_SIZE,
                                 out_fields="*", return_geometry=True):
                if page_size > equis_api.PAGE_SIZE:
                    self.requests.append((offset, page_size))
                    raise equis_api.EquisError("EQuIS returned HTTP 500")
                return super().query_layer_page(facility_id, layer_id, offset,
                                                where, page_size, out_fields,
                                                return_geometry)

        paged = NoSingleReadStub(10636)
        pages = list(paged.iter_layer_features(1, 1))
        fetched = sum(len(f) for _, f in pages)
        check("falls back to paging and reaches the tail",
              fetched == 10636,
              "%d of 10636 over %d requests" % (fetched, len(paged.requests)))
        check("steps the page size down on rejection",
              any(size < equis_api.PAGE_SIZE for _, size in paged.requests),
              "sizes used: %s" % sorted({s for _, s in paged.requests}))

        # A facility whose locations have no coordinates: the service returns
        # nothing for returnGeometry=true, so the reader must retry without it
        # rather than reporting an empty layer (seen live at Facility B).
        class NoCoordinatesStub(PagingStub):
            def query_layer_page(self, facility_id, layer_id, offset,
                                 where="1=1", page_size=equis_api.PAGE_SIZE,
                                 out_fields="*", return_geometry=True):
                self.requests.append((offset, page_size, return_geometry))
                if return_geometry:
                    return {"fields": [], "features": []}
                count = min(page_size, self.total - offset)
                return {
                    "fields": [{"name": "code", "type": "esriFieldTypeString"}],
                    "features": [{"attributes": {"code": "L%d" % (offset + i)}}
                                 for i in range(max(0, count))],
                }

        nogeom = NoCoordinatesStub(28)
        pages = list(nogeom.iter_layer_features(1, 1))
        fetched = sum(len(f) for _, f in pages)
        check("retries without geometry when a layer returns nothing",
              fetched == 28, "%d of 28 features" % fetched)
        check("flags that geometry was unavailable",
              bool(pages and pages[0][0].get("equisGeometryUnavailable")),
              str(pages[0][0].get("equisGeometryUnavailable") if pages else None))

        # Location filtering happens client-side; the limit counts matches.
        stub2 = PagingStub(2500)
        pages = list(stub2.iter_layer_features(1, 1, location_codes=["c7", "c1500"]))
        got = sorted(f["attributes"]["code"] for _, fl in pages for f in fl)
        check("client-side location filter", got == ["c1500", "c7"], str(got))
    finally:
        equis_api.time.sleep = real_sleep


def test_report_analysis():
    """Chemical list, aggregation and time series over a report's rows."""
    spec = importlib.util.spec_from_file_location(
        "report_data", os.path.join(ROOT, "equis_loader", "report_data.py")
    )
    rd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rd)

    headers = ["SYS_LOC_CODE", "SAMPLE_DATE", "CAS_RN", "CHEMICAL_NAME",
               "RESULT_NUMERIC", "RESULT_UNIT", "DETECT_FLAG"]
    rows = [
        ["MW-1", "01/01/2020", "79-01-6", "Trichloroethylene", "5.0", "ug/L", "Y"],
        ["MW-1", "06/01/2021", "79-01-6", "Trichloroethylene", "9.0", "ug/L", "Y"],
        ["MW-1", "03/01/2022", "79-01-6", "Trichloroethylene", "2.0", "ug/L", "Y"],
        ["MW-2", "02/01/2020", "79-01-6", "Trichloroethylene", "<0.5", "ug/L", "N"],
        ["MW-1", "01/01/2020", "71-43-2", "Benzene", "1.0", "ug/L", "Y"],
    ]
    table = rd.ReportTable(headers, rows)

    check("analysis: report has the needed columns",
          table.missing_for_analysis == [], str(table.missing_for_analysis))

    analytes = table.analytes()
    check("analysis: unique analytes, sorted",
          [a["chemical"] for a in analytes] == ["Benzene", "Trichloroethylene"],
          str([a["chemical"] for a in analytes]))
    check("analysis: analyte carries CAS and count",
          analytes[1]["cas_rn"] == "79-01-6" and analytes[1]["count"] == 4,
          str(analytes[1]))

    recent = table.aggregate("Trichloroethylene", "79-01-6", rd.MOST_RECENT)
    check("analysis: most recent picks the latest date",
          recent["MW-1"]["value"] == 2.0
          and recent["MW-1"]["date"].year == 2022,
          str(recent["MW-1"]["value"]))

    largest = table.aggregate("Trichloroethylene", "79-01-6", rd.MAXIMUM)
    check("analysis: maximum picks the biggest value",
          largest["MW-1"]["value"] == 9.0, str(largest["MW-1"]["value"]))

    # Non-detects default to half the limit; the full limit is selectable.
    check("analysis: qualified non-detect halves its limit by default",
          recent["MW-2"]["value"] == 0.25
          and recent["MW-2"]["detect_count"] == 0,
          str(recent["MW-2"]))
    full = table.aggregate("Trichloroethylene", "79-01-6", rd.MOST_RECENT,
                           non_detect=rd.ND_FULL)
    check("analysis: full-DL option keeps the whole limit",
          full["MW-2"]["value"] == 0.5, str(full["MW-2"]["value"]))
    dropped = table.aggregate("Trichloroethylene", "79-01-6", rd.MOST_RECENT,
                              non_detect=rd.ND_EXCLUDE)
    check("analysis: exclude option drops non-detect-only locations",
          dropped.get("MW-2", {}).get("value") is None,
          str(dropped.get("MW-2")))

    detected = table.aggregate("Trichloroethylene", "79-01-6", rd.MOST_RECENT,
                               detected_only=True)
    check("analysis: detected-only drops non-detect locations",
          "MW-2" not in detected, str(sorted(detected)))

    # An analyte selected by CAS must not pick up another chemical's rows.
    benzene = table.aggregate("Benzene", "71-43-2", rd.MAXIMUM)
    check("analysis: analytes do not bleed into each other",
          benzene["MW-1"]["value"] == 1.0 and len(benzene) == 1,
          str(benzene))

    series = table.series("MW-1", "Trichloroethylene", "79-01-6")
    check("analysis: series is chronological",
          [p["value"] for p in series] == [5.0, 9.0, 2.0],
          str([p["value"] for p in series]))
    check("analysis: series excludes other locations",
          table.series("MW-2", "Trichloroethylene", "79-01-6",
                       non_detect=rd.ND_FULL)[0]["value"] == 0.5)
    check("analysis: unknown location yields no series",
          table.series("MW-99", "Trichloroethylene", "79-01-6") == [])

    # Dates: several formats appear across these exports.
    for text, year in (("01/02/2020", 2020), ("2020-01-02", 2020),
                       ("02-Jan-20", 2020), ("2020-01-02T10:30:00", 2020)):
        parsed = rd.parse_date(text)
        check("analysis: parses date %r" % text,
              parsed is not None and parsed.year == year, str(parsed))
    check("analysis: rejects an unparseable date",
          rd.parse_date("not a date") is None)

    # The 12-hour form Analytical Results II actually emits. It was missing
    # from DATE_FORMATS, so every row fell through all eight formats, split
    # the string, and fell through them again - 16 raised exceptions per row,
    # which was 28 s of a 32 s store ingest. Worse than slow: the time of day
    # was silently dropped, collapsing samples 12 hours apart onto one
    # instant.
    noon = rd.parse_date("4/24/2025 12:37:00 PM")
    check("analysis: parses the 12-hour AM/PM form",
          noon is not None and (noon.hour, noon.minute) == (12, 37), str(noon))
    midnight = rd.parse_date("4/24/2025 12:37:00 AM")
    check("analysis: AM and PM are distinguished",
          midnight is not None and midnight.hour == 0, str(midnight))
    check("analysis: 24-hour form still parses after the AM/PM formats",
          (rd.parse_date("4/24/2025 13:05:09") or "").hour == 13)
    # parse_date remembers the last format that worked, so a report whose
    # dates change shape mid-file must not get stuck on the remembered one.
    mixed = [rd.parse_date(text) for text in
             ("4/24/2025 12:37:00 PM", "2020-01-02", "4/24/2025 12:37:00 PM")]
    check("analysis: the format cache does not stick",
          all(value is not None for value in mixed)
          and mixed[1].year == 2020 and mixed[2].hour == 12)

    # Non-detects in these reports leave RESULT_NUMERIC empty and carry only
    # the detection limit. Reading RESULT_NUMERIC alone dropped them from the
    # data entirely, so they never reached the plot (seen live at Facility B).
    nd_headers = ["SYS_LOC_CODE", "SAMPLE_DATE", "CAS_RN", "CHEMICAL_NAME",
                  "RESULT_NUMERIC", "REPORT_RESULT_VALUE",
                  "REPORT_METHOD_DETECTION_LIMIT", "RESULT_UNIT", "DETECT_FLAG"]
    nd_rows = [
        ["MW-1", "01/01/2020", "7", "Nitrate", "5.0", "5.0", "", "mg/L", "Y"],
        ["MW-1", "01/01/2021", "7", "Nitrate", "", "0.01", "0.010", "mg/L", "N"],
        ["MW-1", "01/01/2022", "7", "Nitrate", "", "", "0.010", "mg/L", "N"],
    ]
    nd = rd.ReportTable(nd_headers, nd_rows)
    series = nd.series("MW-1", "Nitrate", "7", non_detect=rd.ND_FULL)
    check("non-detects survive into the series",
          len(series) == 3, "%d of 3 points" % len(series))
    check("non-detects plot at their limit",
          [p["value"] for p in series] == [5.0, 0.01, 0.01],
          str([p["value"] for p in series]))
    half = nd.series("MW-1", "Nitrate", "7", non_detect=rd.ND_HALF)
    check("non-detects can plot at half the limit",
          [p["value"] for p in half] == [5.0, 0.005, 0.005],
          str([p["value"] for p in half]))
    kept = nd.series("MW-1", "Nitrate", "7", non_detect=rd.ND_EXCLUDE)
    check("non-detects can be excluded entirely",
          [p["value"] for p in kept] == [5.0], str([p["value"] for p in kept]))
    check("non-detects are flagged as limit values",
          [p["at_limit"] for p in series] == [False, True, True],
          str([p["at_limit"] for p in series]))

    nd_max = nd.aggregate("Nitrate", "7", rd.MAXIMUM)
    check("aggregation counts non-detects too",
          nd_max["MW-1"]["count"] == 3 and nd_max["MW-1"]["detect_count"] == 1,
          str(nd_max["MW-1"]))

    # A report lacking the analytical columns must be reported, not crash.
    thin = rd.ReportTable(["SYS_LOC_CODE", "X_COORD"], [["MW-1", "1"]])
    check("analysis: missing columns are named",
          sorted(thin.missing_for_analysis) == ["CHEMICAL_NAME", "DETECT_FLAG",
                                                "RESULT_NUMERIC", "SAMPLE_DATE"],
          str(sorted(thin.missing_for_analysis)))

    # Everything but DETECT_FLAG: refused, not valued. Without the column
    # every result reads as a non-detect, and under 1/2 DL a measured 10 is
    # mapped as 5 - which is what this used to do, silently.
    no_flag = rd.ReportTable(
        ["SYS_LOC_CODE", "CHEMICAL_NAME", "CAS_RN", "SAMPLE_DATE",
         "RESULT_NUMERIC", "RESULT_UNIT"],
        [["MW-1", "Arsenic", "7440-38-2", "01/02/2020", "10.0", "ug/L"]])
    check("analysis: a report without DETECT_FLAG is refused, naming it",
          no_flag.missing_for_analysis == ["DETECT_FLAG"],
          str(no_flag.missing_for_analysis))
    check("analysis: the refusal says why detection cannot be guessed",
          "never guessed" in rd.missing_message(no_flag.missing_for_analysis))
    # DETECTED is an accepted alias, so a source using it is not refused.
    aliased = rd.ReportTable(
        ["SYS_LOC_CODE", "CHEMICAL_NAME", "SAMPLE_DATE", "RESULT_NUMERIC",
         "DETECTED"], [["MW-1", "Arsenic", "01/02/2020", "10.0", "Y"]])
    check("analysis: the DETECTED alias satisfies the requirement",
          aliased.missing_for_analysis == [], str(aliased.missing_for_analysis))


def test_sparklines():
    """SVG markers carry the location code and survive awkward input."""
    import datetime

    spec = importlib.util.spec_from_file_location(
        "sparkline", os.path.join(ROOT, "equis_loader", "sparkline.py")
    )
    sp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sp)

    def points(values):
        return [{"date": datetime.datetime(2018 + i, 1, 1), "value": v,
                 "unit": "ug/L", "detect": "Y", "at_limit": False}
                for i, v in enumerate(values)]

    svg = sp.render_svg(points([1, 5, 3]), label="MW-MR26")
    check("sparkline: draws the location code",
          "<text" in svg and "MW-MR26" in svg)
    # No location name means no location text - but the scale caption is
    # text too, so the old "no <text> at all" test would now pass for the
    # wrong reason. Ask the question that was actually meant.
    unlabelled = sp.render_svg(points([1, 2])) or ""
    check("sparkline: omits the label when none is given",
          "MW-MR26" not in unlabelled and "<text" in unlabelled)
    check("sparkline: and drops all text when the scale is off too",
          "<text" not in (sp.render_svg(points([1, 2]), show_scale=False) or ""))

    # The peak is named, so two markers of the same shape but wildly
    # different magnitude can be told apart on the map.
    scaled = sp.render_svg(points([1, 5, 3]), label="MW-MR26")
    check("sparkline: the peak value is captioned",
          ">5 ug/L<" in scaled, scaled[scaled.find("ug/L") - 24:
                                       scaled.find("ug/L") + 5])
    check("sparkline: the unit comes from the points themselves",
          "ug/L" in scaled)

    # Location codes are user data and can contain XML metacharacters.
    escaped = sp.render_svg(points([1, 2]), label='A&B<C>"D"')
    check("sparkline: escapes XML metacharacters in the code",
          "&amp;" in escaped and "&lt;" in escaped and "&quot;" in escaped
          and "<C>" not in escaped.split("</text>")[0].split(">")[-1],
          escaped[escaped.find("<text"):escaped.find("</text>")][-40:])

    # Long codes keep the tail, which is what distinguishes them.
    long_svg = sp.render_svg(points([1, 2]), label="VERY-LONG-CODE-0001")
    label = long_svg[long_svg.find("<text"):long_svg.find("</text>")]
    check("sparkline: long codes keep their distinguishing tail",
          "0001" in label and label.count("…") == 1, label[-40:])

    # The trend must stay inside the plot band, below the label.
    coords = sp._scale(points([0, 10]))
    check("sparkline: the line never intrudes on the label band",
          all(y >= sp.LABEL_HEIGHT for _, y, _ in coords),
          "min y %.1f vs label band %g" % (
              min(y for _, y, _ in coords), sp.LABEL_HEIGHT))

    check("sparkline: nothing to draw yields no file",
          sp.render_svg([]) is None)


def test_row_index():
    """The row index must answer exactly as a full scan would.

    aggregate() and series() narrow their candidate rows through an index; if
    it ever disagrees with a linear scan the map silently shows the wrong
    chemistry, so this compares the two directly.
    """
    spec = importlib.util.spec_from_file_location(
        "report_data", os.path.join(ROOT, "equis_loader", "report_data.py")
    )
    rd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rd)

    headers = ["SYS_LOC_CODE", "SAMPLE_DATE", "CHEMICAL_NAME", "CAS_RN",
               "RESULT_NUMERIC", "RESULT_UNIT", "DETECT_FLAG",
               "REPORT_RESULT_VALUE"]
    rows = []
    chems = [("Benzene", "71-43-2"), ("Toluene", "108-88-3"),
             ("Trichloroethylene", "79-01-6")]
    for i in range(600):
        chem, cas = chems[i % 3]
        nd = (i % 5 == 0)
        rows.append([
            "MW-%02d" % (i % 20), "%02d/01/%d" % (i % 12 + 1, 2010 + i % 12),
            chem, cas, "" if nd else "%.2f" % (1.0 + i % 30), "ug/L",
            "N" if nd else "Y", "0.010" if nd else "",
        ])
    table = rd.ReportTable(headers, rows)
    # The same data with indexing disabled, as the reference.
    plain = rd.ReportTable(headers, rows)
    plain._candidate_rows = lambda c, cas=None, location=None: plain.rows

    mismatches = []
    for chem, cas in chems:
        for how in (rd.MOST_RECENT, rd.MAXIMUM):
            for mode in (rd.ND_HALF, rd.ND_FULL, rd.ND_EXCLUDE):
                if (table.aggregate(chem, cas, how=how, non_detect=mode)
                        != plain.aggregate(chem, cas, how=how, non_detect=mode)):
                    mismatches.append("aggregate %s %s %s" % (chem, how, mode))
        for code in ("MW-00", "MW-07", "MW-19"):
            if table.series(code, chem, cas) != plain.series(code, chem, cas):
                mismatches.append("series %s %s" % (chem, code))
    check("index: aggregate and series match a full scan",
          not mismatches, "; ".join(mismatches[:3]))

    # Looking a chemical up by name alone must reach the same rows as by CAS.
    check("index: a name-only lookup matches the scan",
          table.aggregate("Benzene") == plain.aggregate("Benzene")
          and table.series("MW-00", "Benzene") == plain.series("MW-00", "Benzene"))

    # Narrowing must never invent matches.
    check("index: an unknown chemical aggregates to nothing",
          table.aggregate("Nonexistent", "0-0-0") == {})
    check("index: an unknown location has no series",
          table.series("NO-SUCH", "Benzene", "71-43-2") == [])
    check("index: every chemical is still discoverable",
          len(table.analytes()) == 3, str(len(table.analytes())))
    check("index: every location is still discoverable",
          len(table.locations()) == 20, str(len(table.locations())))

    # A report with no chemical column cannot be indexed; it must degrade to a
    # scan rather than returning nothing.
    thin = rd.ReportTable(["SYS_LOC_CODE", "SAMPLE_DATE"],
                          [["MW-1", "01/01/2020"]])
    check("index: an unindexable report falls back to scanning",
          thin._candidate_rows("Benzene") == thin.rows)

    # Built once, on demand, not at construction.
    fresh = rd.ReportTable(headers, rows)
    check("index: nothing is built until it is needed",
          fresh._by_chemical is None)
    fresh.aggregate("Benzene", "71-43-2")
    check("index: the first lookup builds it",
          fresh._by_chemical is not None)
    built = fresh._by_chemical
    fresh.series("MW-01", "Toluene", "108-88-3")
    check("index: later lookups reuse it", fresh._by_chemical is built)

    # Duplicate rows must all be kept: a location sampled twice on the same
    # day for the same chemical has two results, not one.
    dupes = rd.ReportTable(headers, [rows[0], rows[0], rows[0]])
    check("index: repeated rows are all indexed",
          len(dupes._candidate_rows(rows[0][2], rows[0][3])) == 3,
          str(len(dupes._candidate_rows(rows[0][2], rows[0][3]))))


def test_cache():
    """Round-tripping, parameter keying, staleness and eviction."""
    import shutil
    import tempfile

    spec = importlib.util.spec_from_file_location(
        "cache", os.path.join(ROOT, "equis_loader", "cache.py")
    )
    ec = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ec)

    root = tempfile.mkdtemp(prefix="equis_cache_test_")
    SITE = "https://yoursite.equisonline.com"
    try:
        cache = ec.EquisCache(root)

        # --- facilities -------------------------------------------------
        facilities = [
            {"id": 1000002, "code": "EX", "name": "Example Facility", "wkid": 26917},
            {"id": 1000003, "code": "FB", "name": "Facility B", "wkid": 0},
        ]
        cache.put_facilities(SITE, facilities)
        got, entry = cache.get_facilities(SITE)
        check("cache: facilities round-trip unchanged", got == facilities)
        check("cache: entry records how many were stored",
              entry is not None and entry.row_count == 2)

        # A site the user never visited must be a clean miss, not an error.
        miss, _ = cache.get_facilities("https://other.equisonline.com")
        check("cache: unknown site misses cleanly", miss is None)

        # Trailing slashes and casing must not split one site's cache in two.
        alias, _ = cache.get_facilities("HTTPS://YourSite.EquisOnline.com/")
        check("cache: site URLs normalise to one key", alias == facilities)

        # --- locations, in the shape layer_builder consumes --------------
        pages = [
            (
                {
                    "fields": [{"name": "code", "type": "esriFieldTypeString"}],
                    "spatialReference": {"wkid": 0, "latestWkid": 26917},
                    "geometryType": "esriGeometryPoint",
                },
                [
                    {"attributes": {"code": "MW-1"},
                     "geometry": {"x": 500000.0, "y": 4900000.0}},
                    {"attributes": {"code": "MW-2"},
                     "geometry": {"x": 500100.0, "y": 4900100.0}},
                ],
            )
        ]
        cache.put_locations(SITE, 1000002, pages)
        loc, entry = cache.get_locations(SITE, 1000002)
        check("cache: locations restore as (feature_set, features) tuples",
              loc is not None and len(loc["pages"]) == 1
              and isinstance(loc["pages"][0], tuple)
              and len(loc["pages"][0]) == 2)
        restored_set, restored_features = loc["pages"][0]
        check("cache: spatialReference survives the round-trip",
              restored_set.get("spatialReference", {}).get("latestWkid") == 26917)
        check("cache: geometry survives the round-trip",
              restored_features[0]["geometry"]["x"] == 500000.0)
        check("cache: geometry presence is recorded", loc["has_geometry"] is True)

        # The attribute-only case: a facility whose locations have no
        # coordinates must come back flagged, not silently look mappable.
        flat_pages = [
            ({"fields": [], "equisGeometryUnavailable": True},
             [{"attributes": {"code": "X-1"}}]),
        ]
        cache.put_locations(SITE, 1000003, flat_pages)
        flat, _ = cache.get_locations(SITE, 1000003)
        check("cache: coordinate-less locations round-trip as unmappable",
              flat["has_geometry"] is False
              and flat["pages"][0][0].get("equisGeometryUnavailable") is True)

        # --- reports ----------------------------------------------------
        headers = ["SYS_LOC_CODE", "SAMPLE_DATE", "CHEMICAL_NAME",
                   "RESULT_NUMERIC", "DETECT_FLAG"]
        rows = [
            ["MW-1", "01/01/2020", "Trichloroethylene", "5.0", "Y"],
            ["MW-2", "02/01/2020", "Benzene", "", "N"],
            # Commas and quotes are ordinary in chemical names.
            ["MW-3", "03/01/2020", 'Xylenes, m,p- "total"', "1.5", "Y"],
        ]
        params = {"max_rows": None, "where": None,
                  "keep_columns": list(headers)}
        cache.put_report(SITE, 1000002, 2000001, headers, rows,
                         truncated=False, params=params, label="ARII")
        report, entry = cache.get_report(SITE, 1000002, 2000001, params)
        check("cache: report headers round-trip", report["headers"] == headers)
        check("cache: report rows round-trip through CSV quoting",
              report["rows"] == rows,
              repr(report["rows"][2]) if report else "")
        check("cache: report entry carries its row count",
              entry is not None and entry.row_count == 3)

        # Parameters must key the file: a 100-row run is not the same data.
        capped = dict(params, max_rows=100)
        miss, _ = cache.get_report(SITE, 1000002, 2000001, capped)
        check("cache: different row limits are different cache entries",
              miss is None)

        # ...and a where clause changes which rows exist at all.
        filtered = dict(params, where="SYS_LOC_CODE IN ('MW-1')")
        miss, _ = cache.get_report(SITE, 1000002, 2000001, filtered)
        check("cache: a where clause is part of the cache key", miss is None)

        # outFields is ignored by the service on cached runs, so it must not
        # split the cache into two copies of identical output.
        same = dict(params, out_fields="SYS_LOC_CODE,RESULT_NUMERIC")
        hit, _ = cache.get_report(SITE, 1000002, 2000001, same)
        check("cache: out_fields does not affect the cache key",
              hit is not None and hit["rows"] == rows)

        # --- truncation must stay sticky --------------------------------
        cache.put_report(SITE, 1000002, 2000002, headers, rows[:2],
                         truncated=True, params=capped, label="Capped")
        part, entry = cache.get_report(SITE, 1000002, 2000002, capped)
        check("cache: a truncated report is flagged on read",
              part is not None and part["truncated"] is True
              and entry.truncated is True)
        refused, _ = cache.get_report(SITE, 1000002, 2000002, capped,
                                      allow_truncated=False)
        check("cache: a truncated report is refused when completeness is required",
              refused is None)

        # The guard has to hold when the key is identical, which is the case
        # that actually bites: a run capped at 500,000 rows against a report
        # with fewer stores a complete entry under the same key a truncated
        # one would use, so `truncated` is the only thing separating them.
        same_key = {"max_rows": 500000, "where": None,
                    "keep_columns": list(headers)}
        cache.put_report(SITE, 1000002, 2000999, headers, rows,
                         truncated=True, params=same_key)
        hit, _ = cache.get_report(SITE, 1000002, 2000999, same_key,
                                  allow_truncated=True)
        blocked, _ = cache.get_report(SITE, 1000002, 2000999, same_key,
                                      allow_truncated=False)
        check("cache: truncation alone decides the hit when keys are identical",
              hit is not None and blocked is None)

        # --- freshness metadata -----------------------------------------
        peek = cache.report_entry(SITE, 1000002, 2000001, params)
        check("cache: report_entry reports without loading rows",
              peek is not None and peek.row_count == 3)
        check("cache: a fresh entry reads as recent",
              peek.age_seconds < 60 and peek.age_text == "just now",
              peek.age_text)
        check("cache: age phrasing scales to days",
              ec.describe_age(2 * 86400) == "2 days ago"
              and ec.describe_age(3600) == "1 hour ago",
              ec.describe_age(2 * 86400))

        # --- offline discovery ------------------------------------------
        cache.put_facility_meta(
            SITE, 1000002,
            layers=[{"id": 1, "name": "VW_LOCATION", "count": 10636}],
            reports=[{"id": 2000001, "name": "ARII", "code": "ARII"}],
            service_wkid=26917,
            facility=facilities[0],
        )
        meta, _ = cache.get_facility_meta(SITE, 1000002)
        check("cache: facility metadata round-trips",
              meta["service_wkid"] == 26917 and len(meta["layers"]) == 1
              and meta["facility"]["name"] == "Example Facility")
        check("cache: cached facilities are discoverable for offline mode",
              cache.cached_facility_ids(SITE) == [1000002])

        # --- corruption degrades to a miss, not a crash -----------------
        with open(peek.path, "wb") as handle:
            handle.write(b"this is not gzip")
        broken, _ = cache.get_report(SITE, 1000002, 2000001, params)
        check("cache: a corrupt file reads as a miss", broken is None)
        check("cache: a corrupt entry is dropped from the manifest",
              cache.report_entry(SITE, 1000002, 2000001, params) is None)

        # A file deleted behind the cache's back is likewise just a miss.
        os.remove(cache.get_locations(SITE, 1000002)[1].path)
        gone, _ = cache.get_locations(SITE, 1000002)
        check("cache: a deleted file reads as a miss", gone is None)

        # --- eviction ---------------------------------------------------
        small = ec.EquisCache(root, max_bytes=1)
        before = len(small.list_entries(kind=ec.REPORT))
        small.evict_to_fit()
        after = small.list_entries(kind=ec.REPORT)
        check("cache: eviction drops reports when over the cap",
              before > 0 and len(after) == 0)
        check("cache: eviction keeps metadata that reports depend on",
              small.get_facility_meta(SITE, 1000002)[0] is not None
              and small.get_facilities(SITE)[0] is not None)

        # --- a bad cap must not wipe the cache ---------------------------
        # QSettings coerces through a 32-bit int on Windows, so the 2 GB
        # default reads back as -2147483648. Left unguarded that makes every
        # write evict the entire cache.
        for bad in (-2147483648, 0, None):
            guarded = ec.EquisCache(root, max_bytes=bad)
            check("cache: a %s cap falls back to the default" % bad,
                  guarded.max_bytes == ec.DEFAULT_MAX_BYTES,
                  str(guarded.max_bytes))
        overflowed = ec.EquisCache(root, max_bytes=-2147483648)
        before_evict = len(overflowed.list_entries())
        overflowed.evict_to_fit()
        check("cache: an overflowed cap does not evict everything",
              len(overflowed.list_entries()) == before_evict,
              "%d -> %d" % (before_evict, len(overflowed.list_entries())))

        # --- clearing ---------------------------------------------------
        cache2 = ec.EquisCache(root)
        cache2.put_report(SITE, 1000002, 999, headers, rows, params=params)
        cache2.clear(site=SITE)
        check("cache: clearing a site empties it",
              cache2.list_entries(site=SITE) == []
              and cache2.get_facilities(SITE)[0] is None)

        # --- a second instance sees what the first wrote ----------------
        fresh = ec.EquisCache(root)
        fresh.put_facilities(SITE, facilities)
        reopened = ec.EquisCache(root)
        check("cache: the manifest persists across instances",
              reopened.get_facilities(SITE)[0] == facilities)
        check("cache: size accounting is non-zero",
              reopened.total_bytes(site=SITE) > 0,
              ec.describe_size(reopened.total_bytes(site=SITE)))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_fraction_filter():
    """One fraction through the aggregate, and rows_for keeping them apart."""
    spec = importlib.util.spec_from_file_location(
        "report_data", os.path.join(ROOT, "equis_loader", "report_data.py")
    )
    rd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rd)

    headers = ["SYS_LOC_CODE", "SAMPLE_DATE", "CAS_RN", "CHEMICAL_NAME",
               "RESULT_NUMERIC", "RESULT_UNIT", "DETECT_FLAG", "MATRIX_CODE",
               "FRACTION", "SAMPLE_TYPE_CODE", "REPORT_RESULT_VALUE"]
    rows = [
        # The same sample, same day, reported both ways. Dissolved is what a
        # groundwater assessment wants; total is nearly thirty times larger
        # because it includes what is stuck to the sediment in the sample.
        ["MW-1", "01/01/2022", "7440-38-2", "Arsenic", "1.0", "ug/L", "Y",
         "WG", "D", "N", ""],
        ["MW-1", "01/01/2022", "7440-38-2", "Arsenic", "29.0", "ug/L", "Y",
         "WG", "T", "N", ""],
        ["MW-1", "01/06/2022", "7440-38-2", "Arsenic", "2.0", "ug/L", "Y",
         "WG", "D", "N", ""],
        # A non-detect, carried as its limit.
        ["MW-2", "01/01/2022", "7440-38-2", "Arsenic", "", "ug/L", "N",
         "WG", "D", "N", "0.5"],
        # A second analyte that is NOT mixed, in the same report.
        ["MW-2", "01/01/2022", "71-43-2", "Benzene", "5.0", "ug/L", "Y",
         "WG", "N", "N", ""],
    ]
    table = rd.ReportTable(headers, rows)

    # The bug this closes: a maximum crossing dissolved into total reports
    # 29.0 for a well whose dissolved arsenic never exceeded 2.
    crossed = table.aggregate("Arsenic", "7440-38-2", rd.MAXIMUM, matrix="WG")
    check("fraction: unfiltered, the maximum still crosses fractions",
          crossed["MW-1"]["value"] == 29.0, str(crossed["MW-1"]["value"]))
    dissolved = table.aggregate("Arsenic", "7440-38-2", rd.MAXIMUM,
                                matrix="WG", fraction="D")
    check("fraction: asking for dissolved gets the dissolved maximum",
          dissolved["MW-1"]["value"] == 2.0, str(dissolved["MW-1"]["value"]))
    total = table.aggregate("Arsenic", "7440-38-2", rd.MAXIMUM,
                            matrix="WG", fraction="T")
    check("fraction: asking for total gets the total",
          total["MW-1"]["value"] == 29.0, str(total["MW-1"]["value"]))

    # Per analyte, per matrix - not the whole report, or a clean chemical
    # would be blocked because a different one is mixed.
    check("fraction: the mixed analyte reports both",
          table.fractions_for("Arsenic", "7440-38-2", "WG") == ["D", "T"],
          str(table.fractions_for("Arsenic", "7440-38-2", "WG")))
    check("fraction: a clean analyte in the same report reports one",
          table.fractions_for("Benzene", "71-43-2", "WG") == ["N"],
          str(table.fractions_for("Benzene", "71-43-2", "WG")))

    check("fraction: series honours it too",
          len(table.series("MW-1", "Arsenic", "7440-38-2", matrix="WG",
                           fraction="D")) == 2,
          str(len(table.series("MW-1", "Arsenic", "7440-38-2", matrix="WG",
                               fraction="D"))))

    # --- rows_for: the unaggregated view -------------------------------
    every, dropped = table.rows_for("Arsenic", "7440-38-2", matrix="WG")
    check("rows_for: one entry per result, not per location",
          len(every) == 4, str(len(every)))
    check("rows_for: carries the matrix the aggregate throws away",
          all(e["matrix"] == "WG" for e in every))
    check("rows_for: carries the fraction",
          sorted({e["fraction"] for e in every}) == ["D", "T"],
          str(sorted({e["fraction"] for e in every})))
    mw1 = [e for e in every if e["location"] == "MW-1"]
    check("rows_for: seq is 0-based within a location",
          [e["seq"] for e in mw1] == [0, 1, 2],
          str([e["seq"] for e in mw1]))
    check("rows_for: a location's rows are oldest first",
          [e["date"] for e in mw1] == sorted(e["date"] for e in mw1))
    check("rows_for: the non-detect is kept, at half its limit",
          any(e["at_limit"] and e["value"] == 0.25 for e in every),
          str([(e["location"], e["value"]) for e in every]))
    excluded, _ = table.rows_for("Arsenic", "7440-38-2", matrix="WG",
                                 non_detect=rd.ND_EXCLUDE)
    check("rows_for: ND_EXCLUDE drops it", len(excluded) == 3,
          str(len(excluded)))
    only_d, _ = table.rows_for("Arsenic", "7440-38-2", matrix="WG",
                               fraction="D")
    check("rows_for: the fraction filter applies", len(only_d) == 3,
          str(len(only_d)))

    # A cap drops whole locations: half a location's history would misstate
    # how many results it has.
    capped, lost = table.rows_for("Arsenic", "7440-38-2", matrix="WG", limit=3)
    check("rows_for: a cap is respected", len(capped) <= 3, str(len(capped)))
    check("rows_for: no location is left half-present",
          all(len([e for e in capped if e["location"] == code]) ==
              len([e for e in every if e["location"] == code])
              for code in {e["location"] for e in capped}))
    check("rows_for: and it says what it dropped", lost >= 1, str(lost))

    check("fraction: unaggregated is offered as an aggregation",
          rd.UNAGGREGATED in dict(rd.AGGREGATIONS))


def test_depth_columns():
    """Sample depth and the custom field reach rows_for."""
    spec = importlib.util.spec_from_file_location(
        "report_data", os.path.join(ROOT, "equis_loader", "report_data.py")
    )
    rd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rd)

    # The spellings Analytical Results II actually uses: the plain names are
    # in the default output, the dt_sample_* ones are what @extra_fields adds.
    headers = ["SYS_LOC_CODE", "SAMPLE_DATE", "CAS_RN", "CHEMICAL_NAME",
               "RESULT_NUMERIC", "RESULT_UNIT", "DETECT_FLAG", "MATRIX_CODE",
               "START_DEPTH", "END_DEPTH", "DEPTH_UNIT", "CUSTOM_FIELD_1"]
    rows = [
        ["F38_00", "01/01/2022", "7440-43-9", "Cadmium", "0.02", "mg/kg", "Y",
         "TAILS", "10", "15", "m", "upper tailings"],
        ["F38_00", "01/01/2022", "7440-43-9", "Cadmium", "0.05", "mg/kg", "Y",
         "TAILS", "15", "20", "m", "lower tailings"],
    ]
    table = rd.ReportTable(headers, rows)
    for key in ("start_depth", "end_depth", "depth_unit", "custom_1"):
        check("depth: the %s column is recognised" % key,
              table.column(key) >= 0, str(table.column(key)))

    got, _ = table.rows_for("Cadmium", "7440-43-9")
    check("depth: one result per sampled interval", len(got) == 2,
          str(len(got)))
    check("depth: start/end come through as numbers",
          [(g["start_depth"], g["end_depth"]) for g in got] ==
          [(10.0, 15.0), (15.0, 20.0)],
          str([(g["start_depth"], g["end_depth"]) for g in got]))
    check("depth: the unit comes through",
          all(g["depth_unit"] == "m" for g in got))
    check("depth: the custom field comes through",
          [g["custom_1"] for g in got] == ["upper tailings",
                                           "lower tailings"],
          str([g["custom_1"] for g in got]))
    check("depth: the intervals distinguish results at one borehole",
          len({(g["start_depth"], g["end_depth"]) for g in got}) == 2)

    # The table-prefixed spelling @extra_fields produces must work too.
    alt = ["SYS_LOC_CODE", "SAMPLE_DATE", "CAS_RN", "CHEMICAL_NAME",
           "RESULT_NUMERIC", "RESULT_UNIT", "DETECT_FLAG", "MATRIX_CODE",
           "dt_sample_start_depth", "dt_sample_end_depth",
           "dt_sample_depth_unit", "dt_sample_custom_field_1"]
    table2 = rd.ReportTable(alt, rows)
    check("depth: the dt_sample_* spelling is recognised too",
          table2.column("start_depth") >= 0
          and table2.column("custom_1") >= 0,
          "%s / %s" % (table2.column("start_depth"), table2.column("custom_1")))

    # A report without depth must still work.
    plain = ["SYS_LOC_CODE", "SAMPLE_DATE", "CAS_RN", "CHEMICAL_NAME",
             "RESULT_NUMERIC", "RESULT_UNIT", "DETECT_FLAG", "MATRIX_CODE"]
    table3 = rd.ReportTable(plain, [r[:8] for r in rows])
    got3, _ = table3.rows_for("Cadmium", "7440-43-9")
    check("depth: a report without depth still yields results",
          len(got3) == 2, str(len(got3)))
    check("depth: and reports None rather than failing",
          all(g["start_depth"] is None for g in got3))


def test_multi_matrix():
    """A matrix filter can name several matrices at once."""
    spec = importlib.util.spec_from_file_location(
        "report_data", os.path.join(ROOT, "equis_loader", "report_data.py")
    )
    rd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rd)

    headers = ["SYS_LOC_CODE", "SAMPLE_DATE", "CAS_RN", "CHEMICAL_NAME",
               "RESULT_NUMERIC", "RESULT_UNIT", "DETECT_FLAG", "MATRIX_CODE"]
    rows = [
        ["L1", "01/01/2022", "7440-43-9", "Cadmium", "10", "mg/kg", "Y", "SE"],
        ["L2", "01/01/2022", "7440-43-9", "Cadmium", "20", "mg/kg", "Y", "TAILS"],
        ["L3", "01/01/2022", "7440-43-9", "Cadmium", "30", "ug/L", "Y", "WS"],
    ]
    table = rd.ReportTable(headers, rows)

    # Sediment and tailings are both solids and belong on one map; water
    # does not.
    both = table.aggregate("Cadmium", "7440-43-9", rd.MAXIMUM,
                           matrix=("SE", "TAILS"))
    check("multi-matrix: both wanted matrices are kept",
          sorted(both) == ["L1", "L2"], str(sorted(both)))
    check("multi-matrix: the third is excluded", "L3" not in both)

    one = table.aggregate("Cadmium", "7440-43-9", rd.MAXIMUM, matrix=("SE",))
    check("multi-matrix: a one-element tuple still filters",
          sorted(one) == ["L1"], str(sorted(one)))

    every = table.aggregate("Cadmium", "7440-43-9", rd.MAXIMUM, matrix=())
    check("multi-matrix: an empty tuple means no filter, not no rows",
          sorted(every) == ["L1", "L2", "L3"], str(sorted(every)))

    got, _ = table.rows_for("Cadmium", "7440-43-9", matrix=("SE", "TAILS"))
    check("multi-matrix: rows_for honours it too",
          sorted(r["location"] for r in got) == ["L1", "L2"],
          str(sorted(r["location"] for r in got)))
    check("multi-matrix: series honours it too",
          len(table.series("L1", "Cadmium", "7440-43-9",
                           matrix=("SE", "TAILS"))) == 1)
    check("multi-matrix: analytes() scopes to the set",
          len(table.analytes(("SE", "TAILS"))) == 1,
          str(len(table.analytes(("SE", "TAILS")))))
    # Case is not consistent between reports.
    check("multi-matrix: matched case-insensitively",
          sorted(table.aggregate("Cadmium", "7440-43-9", rd.MAXIMUM,
                                 matrix=("se", "tails"))) == ["L1", "L2"])


def test_analytic_method():
    """Two methods on one sample are two results, not a duplicate."""
    spec = importlib.util.spec_from_file_location(
        "report_data", os.path.join(ROOT, "equis_loader", "report_data.py")
    )
    rd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rd)

    # The real Facility A shape: every depth analysed by both methods, one
    # reporting a non-detect at its limit and the other a detection.
    headers = ["SYS_LOC_CODE", "SAMPLE_DATE", "CAS_RN", "CHEMICAL_NAME",
               "RESULT_NUMERIC", "RESULT_UNIT", "DETECT_FLAG", "MATRIX_CODE",
               "START_DEPTH", "END_DEPTH", "DEPTH_UNIT", "ANALYTIC_METHOD",
               "REPORT_RESULT_VALUE"]
    rows = []
    for index, (top, base) in enumerate([(10, 15), (15, 20), (20, 25)]):
        rows.append(["F38_00", "01/01/2014", "7440-43-9", "Cadmium", "",
                     "mg/kg", "N", "TAILS", str(top), str(base), "m",
                     "ICM14B", "0.01"])
        rows.append(["F38_00", "01/01/2014", "7440-43-9", "Cadmium",
                     str(0.02 - 0.003 * index), "mg/kg", "Y", "TAILS",
                     str(top), str(base), "m", "ICM40B", ""])
    table = rd.ReportTable(headers, rows)

    check("method: the column is recognised", table.column("method") >= 0,
          str(table.column("method")))
    found = [m["method"] for m in table.methods("Cadmium", "7440-43-9")]
    check("method: methods() lists both", found == ["ICM14B", "ICM40B"],
          str(found))

    def per_interval(entries):
        seen = {}
        for entry in entries:
            key = (entry["location"], entry["start_depth"])
            seen.setdefault(key, []).append(entry)
        return max(len(v) for v in seen.values()) if seen else 0

    every, _ = table.rows_for("Cadmium", "7440-43-9")
    check("method: unfiltered keeps both analyses of each depth",
          per_interval(every) == 2, str(per_interval(every)))
    for code in ("ICM14B", "ICM40B"):
        only, _ = table.rows_for("Cadmium", "7440-43-9", method=(code,))
        check("method: %s alone gives one result per depth" % code,
              per_interval(only) == 1 and len(only) == 3,
              "%d per depth, %d results" % (per_interval(only), len(only)))

    check("method: an empty tuple means no filter",
          len(table.rows_for("Cadmium", "7440-43-9", method=())[0]) == 6)
    check("method: matched case-insensitively",
          len(table.rows_for("Cadmium", "7440-43-9",
                             method=("icm14b",))[0]) == 3)
    check("method: aggregate honours it too",
          table.aggregate("Cadmium", "7440-43-9", rd.MAXIMUM,
                          method=("ICM40B",))["F38_00"]["value"] == 0.02,
          str(table.aggregate("Cadmium", "7440-43-9", rd.MAXIMUM,
                              method=("ICM40B",))["F38_00"]["value"]))

    # A report with no method column must behave exactly as before.
    plain = rd.ReportTable([h for h in headers if h != "ANALYTIC_METHOD"],
                           [[c for i, c in enumerate(r)
                             if headers[i] != "ANALYTIC_METHOD"] for r in rows])
    check("method: a report without the column still works",
          len(plain.rows_for("Cadmium", "7440-43-9")[0]) == 6)
    check("method: and offers no methods to pick",
          plain.methods("Cadmium", "7440-43-9") == [])


def test_multi_fraction():
    """Dissolved and total can be compared, just never aggregated together."""
    spec = importlib.util.spec_from_file_location(
        "report_data", os.path.join(ROOT, "equis_loader", "report_data.py")
    )
    rd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rd)

    headers = ["SYS_LOC_CODE", "SAMPLE_DATE", "CAS_RN", "CHEMICAL_NAME",
               "RESULT_NUMERIC", "RESULT_UNIT", "DETECT_FLAG", "MATRIX_CODE",
               "FRACTION"]
    rows = [
        ["W-1", "01/01/2022", "7440-38-2", "Arsenic", "1.0", "ug/L", "Y",
         "WG", "D"],
        ["W-1", "01/01/2022", "7440-38-2", "Arsenic", "29.0", "ug/L", "Y",
         "WG", "T"],
        ["W-1", "01/01/2022", "7440-38-2", "Arsenic", "5.0", "ug/L", "Y",
         "WG", "N"],
    ]
    table = rd.ReportTable(headers, rows)

    check("multi-fraction: a tuple keeps every listed fraction",
          len(table.rows_for("Arsenic", "7440-38-2",
                             fraction=("D", "T"))[0]) == 2,
          str(len(table.rows_for("Arsenic", "7440-38-2",
                                 fraction=("D", "T"))[0])))
    check("multi-fraction: one code still filters to one",
          len(table.rows_for("Arsenic", "7440-38-2", fraction=("D",))[0]) == 1)
    check("multi-fraction: an empty tuple is no filter, not no rows",
          len(table.rows_for("Arsenic", "7440-38-2", fraction=())[0]) == 3)
    check("multi-fraction: a bare string still works",
          len(table.rows_for("Arsenic", "7440-38-2", fraction="T")[0]) == 1)
    check("multi-fraction: matched case-insensitively",
          len(table.rows_for("Arsenic", "7440-38-2",
                             fraction=("d", "t"))[0]) == 2)
    # The aggregate honours it, and crossing D and T is still the danger.
    crossed = table.aggregate("Arsenic", "7440-38-2", rd.MAXIMUM,
                              fraction=("D", "T"))
    check("multi-fraction: aggregating across both takes the higher (why it "
          "is gated)", crossed["W-1"]["value"] == 29.0,
          str(crossed["W-1"]["value"]))
    single = table.aggregate("Arsenic", "7440-38-2", rd.MAXIMUM,
                             fraction=("D",))
    check("multi-fraction: one fraction aggregates safely",
          single["W-1"]["value"] == 1.0, str(single["W-1"]["value"]))


def test_selection_fractions():
    """DataSelection carries a list, and still answers .fraction."""
    spec = importlib.util.spec_from_file_location(
        "data_selection", os.path.join(ROOT, "equis_loader",
                                       "data_selection.py")
    )
    ds = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ds)

    one = ds.DataSelection(fractions=["D"])
    check("selection: one ticked reads as a scalar", one.fraction == "D",
          str(one.fraction))
    check("selection: and as a list", one.fractions == ["D"])
    two = ds.DataSelection(fractions=["D", "T"])
    check("selection: two ticked have no single fraction",
          two.fraction is None, str(two.fraction))
    check("selection: but both are carried", two.fractions == ["D", "T"])
    check("selection: both reach the report parameter",
          two.to_parameters()[ds.PARAM_FRACTIONS] == "D|T",
          str(two.to_parameters()[ds.PARAM_FRACTIONS]))
    # The old keyword still works, so saved selections keep loading.
    old = ds.DataSelection(fraction="T")
    check("selection: the singular keyword still works",
          old.fractions == ["T"] and old.fraction == "T")
    check("selection: none ticked sends no fraction filter",
          ds.DataSelection().to_parameters()[ds.PARAM_FRACTIONS] is None)


def test_profile_series():
    """A profile draws one line per method when several are present."""
    spec = importlib.util.spec_from_file_location(
        "sparkline", os.path.join(ROOT, "equis_loader", "sparkline.py")
    )
    sp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sp)

    entries = []
    for index, (top, base) in enumerate([(0, 5), (5, 10), (10, 15)]):
        entries.append({"start_depth": top, "end_depth": base, "value": 0.01,
                        "detect": "N", "at_limit": True, "method": "ICM14B"})
        entries.append({"start_depth": top, "end_depth": base,
                        "value": 0.02 + index, "detect": "Y",
                        "at_limit": False, "method": "ICM40B"})

    series = sp.profile_series(entries)
    check("series: one per method", len(series) == 2,
          str([(m, len(g)) for m, g in series]))
    check("series: each carries its own depths",
          all(len(g) == 3 for _, g in series))
    check("series: the busiest method is drawn first",
          series[0][0] in ("ICM14B", "ICM40B"))

    svg = sp.render_profile_svg(entries, label="F38_00", analyte="Cadmium",
                                unit="mg/kg")
    check("series: both methods are named on the marker",
          "ICM14B" in svg and "ICM40B" in svg)

    # One method, or none named: no legend, and the original single colour.
    single = [e for e in entries if e["method"] == "ICM40B"]
    solo = sp.render_profile_svg(single, label="F38_00")
    check("series: a single-method profile carries no legend",
          solo is not None and "ICM40B" not in solo)
    unlabelled = [dict(e, method=None) for e in entries[::2]]
    check("series: results with no method still draw",
          sp.render_profile_svg(unlabelled) is not None)

    # --- one line per SAMPLING EVENT ----------------------------------
    # A hole re-sampled a year later is a second profile of that hole, not
    # more points on the first: concentrations move between events, so
    # joining them would draw a line through readings that were never in
    # the ground together.
    dated = []
    for when, scale in (("2020-03-11", 1.0), ("2021-06-02", 2.0)):
        for top, base in ((0, 5), (5, 10), (10, 15)):
            dated.append({"start_depth": top, "end_depth": base,
                          "value": 0.01 * scale * (top + 1), "detect": "Y",
                          "at_limit": False, "method": "ICM14B",
                          "date": when})
    by_date = sp.profile_series(dated)
    check("series: one per sampling date", len(by_date) == 2,
          str([(m, len(g)) for m, g in by_date]))
    check("series: dates are ordered oldest first",
          by_date[0][0].startswith("2020"), str(by_date[0][0]))

    # The points are JOINED down the hole. Each used to carry a horizontal
    # stem back to the zero axis, which drew a sideways bar chart and never
    # showed the shape of the profile.
    drawn = sp.render_profile_svg(dated, label="F38_00", analyte="Cadmium",
                                  unit="mg/kg")
    check("series: readings are joined down the hole",
          drawn.count("<polyline") == 2, str(drawn.count("<polyline")))
    # A stem started at the axis x and ended at the point, at the same y.
    # Counting lines whose two y values match catches them whatever their
    # colour or opacity; the interval bars are vertical, so they do not.
    horizontal = re.findall(
        r'<line x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)"',
        drawn)
    # The hairline under the location label is horizontal too and is not a
    # stem, so it is excluded by its opacity - stems were fully opaque.
    stems = [m for m in re.findall(
        r'<line x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)"'
        r'[^/]*?opacity="1.00"', drawn)
        if m[1] == m[3] and m[0] != m[2]]
    check("series: no stems back to the axis", not stems, str(stems[:2]))

    # The legend keeps only what tells the lines apart: both share ICM14B,
    # so spending its ten characters on the method would leave nothing.
    trimmed = sp._distinguish_labels(["2020-03-11 ICM14B", "2021-06-02 ICM14B"])
    check("series: the legend drops what every line shares",
          all("ICM14B" not in t for t in trimmed), str(trimmed))
    same_day = sp._distinguish_labels(["2020-03-11 SW6010", "2020-03-11 ICM14B"])
    check("series: and keeps the method when that is what differs",
          set(same_day) == {"SW6010", "ICM14B"}, str(same_day))
    check("series: identical labels are never trimmed to nothing",
          all(sp._distinguish_labels(["A B", "A B"])), str(
              sp._distinguish_labels(["A B", "A B"])))

    # One event only: a plain profile, no legend, no date in it.
    one_event = [e for e in dated if e["date"] == "2020-03-11"]
    solo_date = sp.render_profile_svg(one_event, label="F38_00")
    check("series: a single event carries no legend",
          solo_date is not None and "2020" not in solo_date)


def test_depth_profile():
    """The depth-profile marker draws only when it has something to say."""
    spec = importlib.util.spec_from_file_location(
        "sparkline", os.path.join(ROOT, "equis_loader", "sparkline.py")
    )
    sp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sp)

    entries = [
        {"start_depth": 0.0, "end_depth": 5.0, "value": 322.0,
         "detect": "Y", "at_limit": False},
        {"start_depth": 5.0, "end_depth": 10.0, "value": 74.0,
         "detect": "Y", "at_limit": False},
        {"start_depth": 10.0, "end_depth": 15.0, "value": 0.005,
         "detect": "N", "at_limit": True},
    ]
    svg = sp.render_profile_svg(entries, label="F38_00", analyte="Cadmium",
                                unit="mg/kg")
    check("profile: renders for a multi-depth boring", bool(svg))
    check("profile: is well-formed SVG",
          svg.startswith("<svg") and svg.endswith("</svg>"))
    check("profile: captions the location", "F38_00" in svg)
    check("profile: captions the analyte", "Cadmium" in svg)
    check("profile: names the value axis", "mg/kg" in svg)
    check("profile: shows the base of the hole", ">15<" in svg, svg[-260:])
    # Non-detects use the map's hollow-square convention, detections a disc.
    # The non-detect is a hollow square, matching the map's own convention;
    # a filled disc would read as a measurement.
    check("profile: draws the non-detect as a hollow square",
          'fill="none"' in svg and "<rect" in svg)
    check("profile: draws detections filled", "<circle" in svg)

    # A profile needs more than one depth to say anything.
    check("profile: one depth is not a profile",
          sp.render_profile_svg(entries[:1]) is None)
    same = [dict(entries[0]), dict(entries[0])]
    check("profile: two results at one depth is not a profile",
          sp.render_profile_svg(same) is None)
    check("profile: no depth at all is not a profile",
          sp.render_profile_svg(
              [{"value": 1.0}, {"value": 2.0}]) is None)

    # Midpoints place a result inside its interval.
    points = sp.profile_points(entries)
    check("profile: results are ordered shallowest first",
          [d for d, _ in points] == [2.5, 7.5, 12.5],
          str([d for d, _ in points]))

    embedded = sp.embed_depth_profiles({"F38_00": entries, "SHALLOW": entries[:1]},
                                       analyte="Cadmium")
    check("profile: only locations worth drawing get a marker",
          list(embedded) == ["F38_00"], str(list(embedded)))
    check("profile: embedded as a data URI",
          embedded["F38_00"].startswith("base64:"))


def test_extra_fields_merge():
    """@extra_fields is added to, never overwritten."""
    spec = importlib.util.spec_from_file_location(
        "data_selection", os.path.join(ROOT, "equis_loader",
                                       "data_selection.py")
    )
    ds = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ds)

    existing = "RT_ANALYTE.ANALYTE_TYPE|RT_MATRIX.MATRIX_DESC"
    merged = ds.merge_extra_fields(existing)
    check("extra: the report's own columns survive",
          merged.startswith(existing), merged)
    check("extra: ours is appended",
          "DT_SAMPLE.CUSTOM_FIELD_1" in merged, merged)
    check("extra: merging twice changes nothing",
          ds.merge_extra_fields(merged) == merged)
    check("extra: an empty parameter still gets ours",
          ds.merge_extra_fields(None) == "DT_SAMPLE.CUSTOM_FIELD_1",
          str(ds.merge_extra_fields(None)))
    check("extra: it is pipe-separated, not comma",
          "," not in merged, merged)

    # apply_to must not clobber it.
    report = {"parameters": [
        {"name": "@extra_fields", "value": existing, "displayValue": existing},
        {"name": "@cas_rns", "value": None, "displayValue": ""},
    ]}
    out = ds.DataSelection(analytes=["7440-43-9"]).apply_to(report)
    value = {p["name"]: p.get("value") for p in out["parameters"]}
    check("extra: apply_to merges rather than replacing",
          "RT_ANALYTE.ANALYTE_TYPE" in (value.get("@extra_fields") or "")
          and "DT_SAMPLE.CUSTOM_FIELD_1" in (value.get("@extra_fields") or ""),
          value.get("@extra_fields"))


def test_matrix_filter():
    """The matrix filter scopes analytes, aggregates and series."""
    spec = importlib.util.spec_from_file_location(
        "report_data", os.path.join(ROOT, "equis_loader", "report_data.py")
    )
    rd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rd)

    headers = ["SYS_LOC_CODE", "SAMPLE_DATE", "CAS_RN", "CHEMICAL_NAME",
               "RESULT_NUMERIC", "RESULT_UNIT", "DETECT_FLAG", "MATRIX_CODE"]
    rows = [
        # One well sampled for both water and soil, water rising over time.
        ["MW-1", "01/01/2020", "79-01-6", "TCE", "5.0", "ug/L", "Y", "WG"],
        ["MW-1", "01/01/2021", "79-01-6", "TCE", "9.0", "ug/L", "Y", "WG"],
        # A soil result an order of magnitude larger, in different units.
        ["MW-1", "01/01/2021", "79-01-6", "TCE", "900.0", "mg/kg", "Y", "SO"],
        # Lower-case spelling of the same matrix, which reports do mix.
        ["MW-2", "01/01/2021", "79-01-6", "TCE", "3.0", "ug/L", "Y", "wg"],
        # A chemical that exists only in soil.
        ["MW-1", "01/01/2021", "7440-38-2", "Arsenic", "12.0", "mg/kg", "Y", "SO"],
    ]
    table = rd.ReportTable(headers, rows)

    matrices = table.matrices()
    check("matrix: codes listed most common first",
          [m["matrix"] for m in matrices] == ["WG", "SO"],
          str([(m["matrix"], m["count"]) for m in matrices]))
    check("matrix: case variants counted as one code",
          matrices[0]["count"] == 3, str(matrices[0]))

    check("matrix: analytes scoped to the matrix",
          [a["chemical"] for a in table.analytes("SO")] == ["Arsenic", "TCE"],
          str([a["chemical"] for a in table.analytes("SO")]))
    check("matrix: unfiltered analytes unchanged",
          len(table.analytes()) == 2, str(len(table.analytes())))

    # The point of the whole feature: a maximum must not cross matrices.
    water = table.aggregate("TCE", "79-01-6", rd.MAXIMUM, matrix="WG")
    check("matrix: aggregate ignores the other matrix",
          water["MW-1"]["value"] == 9.0, str(water["MW-1"]["value"]))
    check("matrix: lower-case row still matched",
          water["MW-2"]["value"] == 3.0, str(water.get("MW-2")))
    mixed = table.aggregate("TCE", "79-01-6", rd.MAXIMUM)
    check("matrix: unfiltered aggregate still crosses matrices",
          mixed["MW-1"]["value"] == 900.0, str(mixed["MW-1"]["value"]))

    soil = table.aggregate("TCE", "79-01-6", rd.MAXIMUM, matrix="SO")
    check("matrix: soil aggregate sees only soil",
          soil["MW-1"]["value"] == 900.0 and "MW-2" not in soil, str(soil))

    series = table.series("MW-1", "TCE", "79-01-6", matrix="WG")
    check("matrix: series stays within the matrix",
          [p["value"] for p in series] == [5.0, 9.0],
          str([p["value"] for p in series]))
    check("matrix: unfiltered series still mixes",
          len(table.series("MW-1", "TCE", "79-01-6")) == 3,
          str(len(table.series("MW-1", "TCE", "79-01-6"))))

    check("matrix: locations scoped to the matrix",
          table.locations("SO") == ["MW-1"], str(table.locations("SO")))

    # A report with no matrix column must keep working, filter or not: the
    # dialog hides the control, but nothing should break if one is passed.
    plain = rd.ReportTable(headers[:-1], [r[:-1] for r in rows])
    check("matrix: no matrix column lists nothing",
          plain.matrices() == [], str(plain.matrices()))
    check("matrix: filter ignored when unavailable",
          plain.aggregate("TCE", "79-01-6", rd.MAXIMUM, matrix="WG")
          == plain.aggregate("TCE", "79-01-6", rd.MAXIMUM))

    # A code the report has never heard of yields nothing, rather than
    # silently falling back to everything.
    check("matrix: unknown code matches no rows",
          table.aggregate("TCE", "79-01-6", rd.MAXIMUM, matrix="AIR") == {})



def test_filtered_rows():
    """The rows behind a mapped layer, which is what the CSV export writes."""
    spec = importlib.util.spec_from_file_location(
        "report_data_fr", os.path.join(ROOT, "equis_loader", "report_data.py")
    )
    rd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rd)

    headers = ["SYS_LOC_CODE", "SAMPLE_DATE", "CHEMICAL_NAME", "CAS_RN",
               "RESULT_NUMERIC", "MATRIX_CODE", "ANALYTIC_METHOD", "FRACTION"]
    rows = []
    for loc in ("MW-2", "MW-1"):
        for chem, cas in (("Arsenic", "7440-38-2"), ("Cobalt", "7440-48-4")):
            for i, (mx, meth, fr) in enumerate((("WS", "SW6010", "D"),
                                                ("SE", "ICM14B", "N"),
                                                ("TAILS", "SW6010", "N"))):
                rows.append([loc, "0%d/01/2020" % (i + 1), chem, cas,
                             "%.2f" % (i + 1), mx, meth, fr])
    table = rd.ReportTable(headers, rows)
    arsenic = {"chemical": "Arsenic", "cas_rn": "7440-38-2"}
    cobalt = {"chemical": "Cobalt", "cas_rn": "7440-48-4"}

    head, got = table.filtered_rows(analytes=[arsenic])
    check("export: the report's own columns are kept", head == headers,
          str(head[:3]))
    check("export: one analyte returns only its rows",
          len(got) == 6 and {r[2] for r in got} == {"Arsenic"},
          str(len(got)))

    # Every filter the layer applied has to apply here too, or the CSV
    # describes different data from the map it came from.
    _, tails = table.filtered_rows(analytes=[arsenic], matrix=("TAILS",))
    check("export: the matrix filter applies",
          {r[5] for r in tails} == {"TAILS"} and len(tails) == 2,
          str(len(tails)))
    _, by_method = table.filtered_rows(analytes=[arsenic, cobalt],
                                       method=("SW6010",))
    check("export: the method filter applies",
          {r[6] for r in by_method} == {"SW6010"}, str(len(by_method)))
    _, by_fraction = table.filtered_rows(analytes=[arsenic, cobalt],
                                         fraction=("N",))
    check("export: the fraction filter applies",
          {r[7] for r in by_fraction} == {"N"}, str(len(by_fraction)))
    _, one_loc = table.filtered_rows(analytes=[arsenic], locations=["MW-1"])
    check("export: the location filter applies",
          {r[0] for r in one_loc} == {"MW-1"}, str(len(one_loc)))

    # Several chemicals land in ONE table, which is the point of a combined
    # export - a per-chemical file would have to be reassembled to pivot.
    _, both = table.filtered_rows(analytes=[arsenic, cobalt], matrix=("TAILS",))
    check("export: several analytes combine into one table",
          {r[2] for r in both} == {"Arsenic", "Cobalt"} and len(both) == 4,
          str(len(both)))
    check("export: no rows are duplicated by combining",
          len(both) == len({tuple(r) for r in both}))

    # Ordered for a reader, not by the order layers happened to be built.
    _, ordered = table.filtered_rows(analytes=[arsenic, cobalt])
    keys = [(r[2], r[0]) for r in ordered]
    check("export: sorted by chemical then location", keys == sorted(keys),
          str(keys[:3]))

    _, everything = table.filtered_rows()
    check("export: no analytes means the whole table",
          len(everything) == len(rows), str(len(everything)))
    _, none = table.filtered_rows(analytes=[arsenic], matrix=("NOPE",))
    check("export: a filter matching nothing yields nothing", none == [],
          str(none))


def main():
    test_report_parsing()
    test_paging()
    test_report_analysis()
    test_matrix_filter()
    test_fraction_filter()
    test_depth_columns()
    test_multi_matrix()
    test_analytic_method()
    test_multi_fraction()
    test_selection_fractions()
    test_filtered_rows()
    test_profile_series()
    test_depth_profile()
    test_extra_fields_merge()
    test_sparklines()
    test_coordinate_detection()
    test_row_index()
    test_cache()
    print("\n%s" % ("ALL PARSING TESTS PASSED" if not FAILURES
                    else "FAILURES: %s" % ", ".join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
