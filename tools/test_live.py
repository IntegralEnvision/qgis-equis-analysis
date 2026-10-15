"""Offline tests for the live-query path: data selection and report lifecycle.

No QGIS and no network: `data_selection.py` and `live_report.py` import
neither, and the EQuIS client is stubbed. Run with a plain interpreter:

    python tools\\test_live.py
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


# live_report.py imports its sibling with a package-relative import, so the
# modules are loaded under the package name rather than bare. Registering the
# package first keeps that import working without QGIS on the path.
_pkg = type(sys)("equis_loader")
_pkg.__path__ = [os.path.join(ROOT, "equis_loader")]
sys.modules.setdefault("equis_loader", _pkg)


def _load(name):
    full = "equis_loader.%s" % name
    spec = importlib.util.spec_from_file_location(
        full, os.path.join(ROOT, "equis_loader", "%s.py" % name)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


ds = _load("data_selection")
rd = _load("report_data")
lr = _load("live_report")


class StubClient(object):
    """An EquisClient that records writes instead of making them."""

    def __init__(self, reports=None, groups=None, members=None,
                 create_fails=False):
        self._reports = list(reports or [])
        self._groups = list(groups or [])
        self._members = dict(members or {})
        self.created = []
        self.saved = []
        self.added = []
        self.create_fails = create_fails
        self.detail_calls = 0

    def list_reports(self, facility_id=None):
        return list(self._reports)

    def report_detail(self, report_id):
        self.detail_calls += 1
        for report in self._reports:
            if abs(report.get("id") or 0) == abs(report_id):
                return dict(report, parameters=list(report.get("parameters") or []))
        raise RuntimeError("no such report %s" % report_id)

    def create_report(self, base_report_id, name, code, facility_id,
                      parameters, remark=""):
        if self.create_fails:
            raise RuntimeError("Not permitted (403).")
        new = {"id": -999, "name": name, "code": code,
               "facilityId": facility_id, "owner": "me",
               "baseReportId": base_report_id,
               "parameters": list(parameters)}
        self._reports.append(new)
        self.created.append(new)
        return new

    def save_report(self, report):
        self.saved.append(report)
        for i, existing in enumerate(self._reports):
            if abs(existing.get("id") or 0) == abs(report.get("id") or 0):
                self._reports[i] = report
        return True

    def list_groups(self):
        return list(self._groups)

    def group_members(self, group_id):
        return self._members.get(group_id)

    def add_group_member(self, group_id, report_id):
        self.added.append((group_id, report_id))
        # A stored None models a group whose members could not be read, which
        # is not the same as an empty one. The add still succeeds server-side;
        # it just cannot be reflected in a listing we could not read.
        current = self._members.get(group_id)
        if isinstance(current, list):
            current.append({"id": -abs(report_id), "type": "user_report_id",
                            "name": "x"})
        return True


def test_encoding():
    """The pipe/comma distinction, which fails silently when wrong."""
    check("encode: values are pipe-separated",
          ds.encode_list(["A", "B", "C"]) == "A|B|C",
          ds.encode_list(["A", "B", "C"]))
    check("encode: a comma never appears as a separator",
          "," not in (ds.encode_list(["7429-90-5", "7440-38-2"]) or ""),
          ds.encode_list(["7429-90-5", "7440-38-2"]))
    check("encode: empty means no filter, not an empty filter",
          ds.encode_list([]) is None and ds.encode_list(None) is None)
    check("encode: blanks are dropped",
          ds.encode_list(["A", "", "  ", "B"]) == "A|B",
          ds.encode_list(["A", "", "  ", "B"]))
    check("encode: duplicates collapse, order kept",
          ds.encode_list(["B", "A", "B"]) == "B|A",
          ds.encode_list(["B", "A", "B"]))
    check("decode: round-trips",
          ds.decode_list(ds.encode_list(["A", "B"])) == ["A", "B"])
    check("decode: None is empty", ds.decode_list(None) == [])

    import datetime
    check("encode: a date becomes the report's format",
          ds.encode_date(datetime.date(2024, 3, 1)) == "2024-03-01T00:00:00",
          ds.encode_date(datetime.date(2024, 3, 1)))
    check("encode: a string passes through",
          ds.encode_date("2024-03-01T00:00:00") == "2024-03-01T00:00:00")
    check("encode: None stays None", ds.encode_date(None) is None)


def test_selection_parameters():
    """A selection maps onto the report parameters EQuIS actually reads."""
    sel = ds.DataSelection(
        facility_id=1000001,
        analytes=["7429-90-5", "7440-38-2"],
        matrices=["WS"],
        fraction=ds.FRACTION_DISSOLVED,
        sample_types=[ds.SAMPLE_TYPE_NORMAL],
    )
    params = sel.to_parameters()
    check("params: analytes are pipe-separated",
          params["@cas_rns"] == "7429-90-5|7440-38-2", params["@cas_rns"])
    check("params: facility is set", params["@facility_id"] == "1000001")
    check("params: fraction is a single code",
          params["@fractions"] == "D", params["@fractions"])
    check("params: sample types default to normal only",
          params["@sample_types"] == "N", params["@sample_types"])
    check("params: unset filters are None, not empty strings",
          params["@sys_loc_codes"] is None and params["@start_date"] is None)
    # Location groups are the top of "Where": verified on the ARII base as
    # @location_groups, a facility-scoped MultiSelect the server expands.
    check("params: location groups unset by default",
          params["@location_groups"] is None)
    grouped = ds.DataSelection(location_groups=["RI_WP", "RI_WS"],
                               locations=["MW-1"]).to_parameters()
    check("params: location groups are pipe-separated",
          grouped["@location_groups"] == "RI_WP|RI_WS",
          grouped["@location_groups"])
    check("params: groups and individual locations travel separately",
          grouped["@sys_loc_codes"] == "MW-1", grouped["@sys_loc_codes"])
    check("describe: names the groups",
          "RI_WP/RI_WS" in ds.DataSelection(
              location_groups=["RI_WP", "RI_WS"]).describe())

    # Unset by default: fraction depends on the matrix, and demanding one a
    # matrix does not use returns zero rows. Measured: sediment carries only N,
    # so a dissolved default silently emptied every sediment map.
    check("default: fraction is unset, not dissolved",
          ds.DataSelection().fraction is None,
          repr(ds.DataSelection().fraction))
    check("default: an unset fraction sets no parameter",
          ds.DataSelection().to_parameters()["@fractions"] is None)
    check("default: sample types are normal only",
          ds.DataSelection().sample_types == ["N"])

    # Methods reach the SERVER. They used to be filtered client-side only, on
    # the belief that the report carried no method parameter - so ticking one
    # narrowed the view while the report still generated every method's rows,
    # which is precisely the work the ~230 s ceiling cannot afford.
    # Verified 2026-09-02 that the ARII base carries '@analytic_methods'.
    methods = ds.DataSelection(methods=["SW6010", "ICM14B"]).to_parameters()
    check("params: methods are sent as @analytic_methods",
          methods["@analytic_methods"] == "SW6010|ICM14B",
          repr(methods.get("@analytic_methods")))
    check("default: no method set means no method filter",
          ds.DataSelection().to_parameters()["@analytic_methods"] is None)

    # Method and fraction are LOCAL filters and must stay out of the cache
    # key, or ticking one would mark the very rows that offered it stale and
    # demand a refetch to look at a subset of data already in hand.
    # dialog.py needs QGIS, so the guard is read from its source text.
    dialog_src = open(os.path.join(ROOT, "equis_loader", "dialog.py"),
                      encoding="utf-8").read()
    check("key: method and fraction are excluded from the fetch key",
          "LOCAL_ONLY_PARAMS = (PARAM_METHODS, PARAM_FRACTIONS)" in dialog_src
          and "name not in self.LOCAL_ONLY_PARAMS" in dialog_src)


def test_apply_to_report():
    """Applying a selection preserves the parameters it does not control."""
    report = {
        "id": -123,
        "name": "r",
        "parameters": [
            {"name": "@facility_id", "value": "1", "displayValue": "1"},
            {"name": "@cas_rns", "value": "old", "displayValue": "old"},
            {"name": "@nd_multiplier", "value": "1", "displayValue": "1"},
            {"name": "@sig_fig_ar", "value": "ignore", "displayValue": "ignore"},
            {"name": "@location_groups", "value": "", "displayValue": ""},
        ],
    }
    sel = ds.DataSelection(facility_id=42, analytes=["50-29-3"],
                           fraction=ds.FRACTION_TOTAL,
                           location_groups=["RI_WP"])
    updated = sel.apply_to(report)
    by_name = {p["name"]: p for p in updated["parameters"]}

    check("apply: the controlled parameter is rewritten",
          by_name["@cas_rns"]["value"] == "50-29-3",
          by_name["@cas_rns"]["value"])
    check("apply: location groups are written",
          by_name["@location_groups"]["value"] == "RI_WP",
          by_name["@location_groups"]["value"])
    check("apply: facility is repointed", by_name["@facility_id"]["value"] == "42")
    check("apply: unrelated parameters survive untouched",
          by_name["@nd_multiplier"]["value"] == "1"
          and by_name["@sig_fig_ar"]["value"] == "ignore")
    check("apply: displayValue tracks value",
          by_name["@cas_rns"]["displayValue"] == "50-29-3")
    check("apply: every parameter is kept",
          len(updated["parameters"]) == 5, str(len(updated["parameters"])))
    check("apply: the original is not mutated",
          report["parameters"][1]["value"] == "old",
          report["parameters"][1]["value"])

    # A filter the selection leaves unset must clear the stored one, or a
    # previous run's filter silently narrows this one.
    cleared = ds.DataSelection(facility_id=42).apply_to(report)
    by_name = {p["name"]: p for p in cleared["parameters"]}
    check("apply: an unset filter clears the stored value",
          by_name["@cas_rns"]["value"] is None,
          repr(by_name["@cas_rns"]["value"]))


def test_warnings():
    """The panel has to say what a selection hides."""
    warned = ds.DataSelection(fraction=ds.FRACTION_DISSOLVED).warnings()
    check("warn: the chosen fraction is stated",
          any("dissolved" in w.lower() for w in warned), str(warned))
    check("warn: an unset fraction claims nothing about fractions",
          not any("fraction" in w.lower()
                  for w in ds.DataSelection(fraction=None).warnings()),
          str(ds.DataSelection(fraction=None).warnings()))
    check("warn: choosing one warns it may return nothing",
          any("return nothing" in w.lower()
              for w in ds.DataSelection(fraction="D").warnings()))
    check("warn: QC exclusion is stated",
          any("quality-control" in w.lower()
              for w in ds.DataSelection().warnings()))
    check("warn: an unfiltered analyte list is called out",
          any("no chemicals" in w.lower()
              for w in ds.DataSelection().warnings()))
    check("warn: selecting analytes drops that warning",
          not any("no chemicals" in w.lower()
                  for w in ds.DataSelection(analytes=["1"]).warnings()))


def test_client_side_matching():
    """The agent path filters rows locally, since stored output is frozen."""
    headers = ["SYS_LOC_CODE", "CHEMICAL_NAME", "RESULT_NUMERIC",
               "MATRIX_CODE", "FRACTION", "SAMPLE_TYPE_CODE"]
    table = rd.ReportTable(headers, [])
    rows = {
        "wanted": ["MW-1", "TCE", "1.0", "WS", "D", "N"],
        "other matrix": ["MW-1", "TCE", "1.0", "SE", "D", "N"],
        "total": ["MW-1", "TCE", "1.0", "WS", "T", "N"],
        "blank": ["MW-1", "TCE", "1.0", "WS", "D", "EB"],
        "other loc": ["MW-9", "TCE", "1.0", "WS", "D", "N"],
    }
    sel = ds.DataSelection(matrices=["WS"], fraction="D",
                           sample_types=["N"], locations=["MW-1"])
    check("match: the wanted row passes", sel.matches(table, rows["wanted"]))
    check("match: another matrix is rejected",
          not sel.matches(table, rows["other matrix"]))
    check("match: the other fraction is rejected",
          not sel.matches(table, rows["total"]))
    check("match: a QC sample is rejected", not sel.matches(table, rows["blank"]))
    check("match: another location is rejected",
          not sel.matches(table, rows["other loc"]))
    check("match: matrix is case-insensitive",
          ds.DataSelection(matrices=["ws"], fraction=None, sample_types=[])
          .matches(table, rows["wanted"]))

    # A report lacking the column cannot express the filter; dropping every
    # row would be worse than ignoring it.
    thin = rd.ReportTable(["SYS_LOC_CODE", "CHEMICAL_NAME"], [])
    check("match: a missing column ignores that filter",
          sel.matches(thin, ["MW-1", "TCE"]))

    # Method filters the agent path too. The live path saves it onto the
    # report, but stored output is frozen and cannot be re-scoped, so the
    # same tick has to be honoured here or it would silently do nothing on
    # one of the two sources.
    with_method = rd.ReportTable(
        ["SYS_LOC_CODE", "CHEMICAL_NAME", "ANALYTIC_METHOD"], [])
    only6010 = ds.DataSelection(methods=["SW6010"], sample_types=[])
    check("match: the chosen method passes",
          only6010.matches(with_method, ["MW-1", "TCE", "SW6010"]))
    check("match: another method is rejected",
          not only6010.matches(with_method, ["MW-1", "TCE", "ICM14B"]))
    check("match: method is case-insensitive",
          ds.DataSelection(methods=["sw6010"], sample_types=[])
          .matches(with_method, ["MW-1", "TCE", "SW6010"]))
    check("match: no method chosen keeps every method",
          ds.DataSelection(sample_types=[])
          .matches(with_method, ["MW-1", "TCE", "ICM14B"]))


def test_mixed_fraction_detection():
    """Mixing is found in the rows, since it cannot be chosen away up front."""
    headers = ["SYS_LOC_CODE", "CHEMICAL_NAME", "RESULT_NUMERIC",
               "MATRIX_CODE", "FRACTION"]
    # Measured shape: surface water carries D and T, sediment only N.
    rows = [
        ["MW-1", "Arsenic", "1.0", "WS", "D"],
        ["MW-1", "Arsenic", "9.0", "WS", "T"],
        ["SD-1", "Arsenic", "5.0", "SE", "N"],
    ]
    table = rd.ReportTable(headers, rows)
    mixed = table.mixed_fractions()
    check("mixed: the water matrix is reported as mixed",
          mixed == {"WS": ["D", "T"]}, str(mixed))
    check("mixed: a single-fraction matrix is not flagged",
          "SE" not in mixed, str(mixed))

    clean = rd.ReportTable(headers, [r for r in rows if r[4] != "T"])
    check("mixed: nothing to report when each matrix has one fraction",
          clean.mixed_fractions() == {}, str(clean.mixed_fractions()))

    none = rd.ReportTable(["SYS_LOC_CODE", "CHEMICAL_NAME"], [["a", "b"]])
    check("mixed: a report with no fraction column reports nothing",
          none.mixed_fractions() == {})


def test_report_naming():
    """Names identify one user and one facility, unambiguously."""
    name = lr.report_name("jo@x.com", "Facility A", 1000001)
    check("name: carries user, facility and id",
          "jo@x.com" in name and "Facility A" in name and "1000001" in name, name)
    check("name: recognised as ours", lr.is_plugin_report(name))
    check("name: someone else's report is not ours",
          not lr.is_plugin_report("Facility_A Analytical Results II"))
    check("name: two facilities give two names",
          lr.report_name("jo", "A", 1) != lr.report_name("jo", "B", 2))
    check("name: two users give two names",
          lr.report_name("a", "F", 1) != lr.report_name("b", "F", 1))
    check("name: a nameless facility still identifies by id",
          "7" in lr.report_name("jo", "", 7))


# The tenant's base report: .system, its own base, named exactly for its
# code. Every real tenant has one; the live path needs it to know which base
# to create on. Numbered unlike 508 so no constant can pass for it.
BASE777 = {"id": 777, "name": "Analytical Results II", "code": "Analytical Results II",
           "owner": ".system", "baseReportId": 777, "facilityId": None,
           "parameters": [{"name": "@facility_id", "value": "",
                           "displayType": "Text", "dbType": 11}]}


def test_lifecycle():
    """Find-or-create, then apply filters without disturbing the rest."""
    mine = lr.report_name("me", "Facility A", 42)
    existing = {"id": -500, "name": mine, "owner": "me", "facilityId": 42,
                "code": lr.BASE_REPORT_CODE,
                "parameters": [
                    {"name": "@facility_id", "value": "42"},
                    {"name": "@cas_rns", "value": "old"},
                    {"name": "@nd_multiplier", "value": "1"},
                ]}
    client = StubClient(reports=[existing])
    live = lr.LiveReport(client, 42, "Facility A", "me")

    check("lifecycle: an existing report is found, not recreated",
          live.ensure() == 500 and not client.created, str(client.created))

    live.apply(ds.DataSelection(facility_id=42, analytes=["50-29-3"]))
    saved = client.saved[-1]
    by_name = {p["name"]: p for p in saved["parameters"]}
    check("lifecycle: filters are saved onto the report",
          by_name["@cas_rns"]["value"] == "50-29-3",
          by_name["@cas_rns"]["value"])
    check("lifecycle: untouched parameters survive the save",
          by_name["@nd_multiplier"]["value"] == "1")

    # A fresh user has nothing to find.
    # Someone else's report on the same base is what a first-time user clones
    # the parameter definitions from - a report cannot be created without them.
    seed = {"id": -700, "name": "Somebody's ARII", "owner": "other",
            "code": lr.BASE_REPORT_CODE, "baseReportId": 777,
            "parameters": [{"name": "@facility_id", "value": "1",
                            "displayType": "Text", "dbType": 11},
                           {"name": "@cas_rns", "value": "x",
                            "displayType": "Text", "dbType": 11}]}
    empty = StubClient(reports=[seed, BASE777])
    fresh = lr.LiveReport(empty, 42, "Facility A", "newbie")
    rid = fresh.ensure()
    check("lifecycle: a missing report is created", fresh.created and rid == 999,
          str(rid))
    check("lifecycle: the created report is named for the user",
          empty.created[0]["name"] == lr.report_name("newbie", "Facility A", 42),
          empty.created[0]["name"])
    check("lifecycle: it is built on Analytical Results II",
          empty.created[0]["code"] == lr.BASE_REPORT_CODE)
    check("lifecycle: on the base its template declares (777), not a constant",
          empty.created[0]["baseReportId"] == 777, str(empty.created[0].get("baseReportId")))

    # A tenant whose only ARII report is the base itself, numbered unlike
    # any other tenant: found by what it is, cloned, and created on it.
    base = {"id": 9001, "name": lr.BASE_REPORT_CODE, "code": lr.BASE_REPORT_CODE,
            "owner": ".system", "baseReportId": 9001,
            "parameters": [{"name": "@facility_id", "value": "",
                            "displayType": "Text", "dbType": 11}]}
    only_base = StubClient(reports=[base])
    lone = lr.LiveReport(only_base, 42, "F", "newbie")
    check("lifecycle: with only the base report visible, a report is still created",
          lone.ensure() == 999 and only_base.created, str(only_base.created))
    check("lifecycle: ...on that tenant's base id (9001)",
          only_base.created and only_base.created[0]["baseReportId"] == 9001,
          str(only_base.created[0].get("baseReportId") if only_base.created else None))
    check("lifecycle: is_base_report needs .system ownership AND id == base",
          lr.is_base_report(base)
          and not lr.is_base_report(dict(base, owner="someone"))
          and not lr.is_base_report(dict(base, baseReportId=508))
          and not lr.is_base_report(seed))

    # A user's ARII report that does not say which base it is on is
    # skipped for the base report, not trusted with a guessed id.
    vague = dict(seed, id=-701, name="No base said")
    vague.pop("baseReportId")
    picky = StubClient(reports=[vague, base])
    lr.LiveReport(picky, 42, "F", "newbie").ensure()
    check("lifecycle: a template with no base id is skipped, not guessed at",
          picky.created and picky.created[0]["baseReportId"] == 9001,
          str(picky.created[0].get("baseReportId") if picky.created else None))

    # The tenant measured 2026-10-12 has three .system bases sharing the
    # code: the base, "...with Sample Selection" (54 parameters) and
    # "...- reportable sample". A variant with a LOWER id, and a user report
    # on it, must not be cloned or created on.
    # Each carries a parameter only that base has, so a clone from the
    # wrong one shows - the created report's BASE id comes from the true
    # base whatever is cloned, so checking the id alone missed it.
    variant = {"id": 100, "name": "Analytical Results II with Sample Selection",
               "code": lr.BASE_REPORT_CODE, "owner": ".system", "baseReportId": 100,
               "parameters": [{"name": "@facility_id", "value": "",
                               "displayType": "Text", "dbType": 11},
                              {"name": "@sample_selection", "value": ""}]}
    on_variant = {"id": -101, "name": "Someone's on the variant", "owner": "other",
                  "code": "", "baseReportId": 100,
                  "parameters": [{"name": "@facility_id", "value": "1"},
                                 {"name": "@sample_selection", "value": ""}]}
    mixed = StubClient(reports=[variant, on_variant, base])
    lr.LiveReport(mixed, 42, "F", "newbie").ensure()
    check("lifecycle: a same-code variant base (lower id) is never used",
          mixed.created and mixed.created[0]["baseReportId"] == 9001,
          str(mixed.created[0].get("baseReportId") if mixed.created else None))
    check("lifecycle: ...nor cloned: no variant-only parameter on the new report",
          mixed.created and not any(p["name"] == "@sample_selection"
                                    for p in mixed.created[0]["parameters"]),
          str([p["name"] for p in mixed.created[0]["parameters"]] if mixed.created else None))
    check("lifecycle: is_base_report is false for the variant",
          not lr.is_base_report(variant))
    ambiguous = lr.LiveReport(StubClient(reports=[base, dict(base, id=9002,
                                                            baseReportId=9002)]),
                              42, "F", "newbie")
    try:
        ambiguous.ensure()
        check("lifecycle: two true bases are refused, not guessed between", False)
    except lr.EquisTemplateError:
        check("lifecycle: two true bases are refused, not guessed between", True)

    # Someone else's identically-named report must not be adopted.
    theirs = {"id": -600, "name": lr.report_name("me", "Facility A", 42),
              "owner": "someone-else", "code": lr.BASE_REPORT_CODE, "facilityId": 42,
              "parameters": [{"name": "@facility_id", "value": "42"}]}
    guarded = lr.LiveReport(StubClient(reports=[theirs]), 42, "Facility A", "me")
    check("lifecycle: another owner's report is not adopted",
          guarded.find() is None)

    # With nothing on the base to clone, creation cannot even be attempted:
    # EQuIS rejects a report whose parameters lack their full descriptors.
    bare = lr.LiveReport(StubClient(reports=[]), 42, "F", "me")
    try:
        bare.ensure()
        check("lifecycle: no template is refused clearly", False, "no error")
    except lr.EquisTemplateError as exc:
        check("lifecycle: no template is refused clearly",
              "parameter definitions" in str(exc), str(exc)[:60])

    # Creating without rights is the common real failure.
    denied = lr.LiveReport(StubClient(reports=[seed, BASE777], create_fails=True),
                           42, "F", "me")
    try:
        denied.ensure()
        check("lifecycle: a refused create surfaces", False, "no error raised")
    except Exception as exc:  # noqa: BLE001
        check("lifecycle: a refused create surfaces", "403" in str(exc), str(exc))


def test_filing():
    """Filing into a facility group is best-effort and never blocks."""
    mine = lr.report_name("me", "Facility A", 42)
    reports = [{"id": -500, "name": mine, "owner": "me", "facilityId": 42,
                "code": lr.BASE_REPORT_CODE, "baseReportId": 777,
                "parameters": [{"name": "@facility_id", "value": "42"}]}]
    groups = [{"id": 7, "code": "003A.Facility A", "name": "Facility A",
               "facility_id": None}]

    client = StubClient(reports=reports, groups=groups, members={7: []})
    live = lr.LiveReport(client, 42, "Facility A", "me")
    live.ensure()
    check("filing: the report is filed under its facility group",
          live.file_under_facility() and client.added == [(7, 500)],
          str(client.added))

    # Already a member: no second add.
    again = StubClient(reports=reports, groups=groups,
                       members={7: [{"id": -500, "type": "user_report_id"}]})
    live2 = lr.LiveReport(again, 42, "Facility A", "me")
    live2.ensure()
    check("filing: an already-filed report is not filed twice",
          live2.file_under_facility() and not again.added, str(again.added))

    # An unreadable group is unknown, not empty. It must still try to add
    # (the add is idempotent server-side) rather than assume either way.
    blind = StubClient(reports=reports, groups=groups, members={7: None})
    live3 = lr.LiveReport(blind, 42, "Facility A", "me")
    live3.ensure()
    check("filing: an unreadable group still attempts the add",
          live3.file_under_facility() and blind.added == [(7, 500)],
          str(blind.added))

    # No matching group is not an error.
    lonely = StubClient(reports=reports + [BASE777], groups=[], members={})
    live4 = lr.LiveReport(lonely, 42, "Nowhere", "me")
    live4.ensure()
    check("filing: no matching group is not an error",
          live4.file_under_facility() is False and not live4.notes,
          str(live4.notes))


def test_prepare_before_fetch():
    """The pickers must be fillable without running the report.

    This is the whole reason the report is created when a facility is picked:
    a facility large enough to need filtering is one whose unfiltered report
    cannot be generated, so "fetch once to learn what to filter by" does not
    exist as an option.
    """
    mine = lr.report_name("me", "Big Site", 9)
    # A real ARII report carries all 54 parameters; apply_to only rewrites
    # ones that already exist, so the template must carry the filters.
    seed = {"id": -700, "name": "Somebody's ARII", "owner": "other",
            "code": lr.BASE_REPORT_CODE, "baseReportId": 777,
            "parameters": [{"name": n, "value": v} for n, v in (
                ("@facility_id", "1"), ("@cas_rns", ""), ("@matrix_codes", ""),
                ("@fractions", ""), ("@sample_types", ""))]}

    class Params(StubClient):
        def __init__(self, **kw):
            StubClient.__init__(self, **kw)
            self.ran = []
            self.asked = []

        def report_parameter_values(self, report_id, name, facility_id=None):
            self.asked.append((report_id, name))
            return {"@cas_rns": [("7440-38-2", "Arsenic")],
                    "@matrix_codes": [("WS", "Surface Water")]}.get(name, [])

        def report_csv(self, *a, **kw):
            self.ran.append(a)
            raise AssertionError("the report must not run to fill the pickers")

    client = Params(reports=[seed, BASE777])
    live = lr.LiveReport(client, 9, "Big Site", "me")
    rid = live.ensure()
    check("prepare: the report exists before any fetch", rid == 999, str(rid))

    values = client.report_parameter_values(rid, "@cas_rns", 9)
    check("prepare: chemicals are known without running anything",
          values == [("7440-38-2", "Arsenic")], str(values))
    check("prepare: no report was run", not client.ran, str(client.ran))
    check("prepare: the lists were read from our own report",
          all(r == rid for r, _ in client.asked), str(client.asked))

    # And the first fetch can therefore already be filtered.
    live.apply(ds.DataSelection(facility_id=9, analytes=["7440-38-2"]))
    saved = {p["name"]: p.get("value") for p in client.saved[-1]["parameters"]}
    check("prepare: the first fetch is already narrowed",
          saved.get("@cas_rns") == "7440-38-2", repr(saved.get("@cas_rns")))

    # Method and fraction are NOT read at prepare time, even though the
    # service would answer. Both are properties of the analysis rather than
    # the facility, so the facility-wide answer (54 methods, 7 fractions) is
    # mostly choices that match nothing in the data about to be fetched.
    # They are offered from the fetched rows instead.
    #
    # Read from the source text rather than imported: tasks.py needs QGIS,
    # and this suite deliberately runs under a plain interpreter.
    source = open(os.path.join(ROOT, "equis_loader", "tasks.py"),
                  encoding="utf-8").read()
    wanted = source.split("wanted = (", 1)[1].split(")", 1)[0]
    check("prepare: analytes and matrices are read up front",
          "@cas_rns" in wanted and "@matrix_codes" in wanted,
          " ".join(wanted.split()))
    check("prepare: method and fraction are NOT read up front",
          "@analytic_methods" not in wanted and "@fractions" not in wanted,
          " ".join(wanted.split()))


def test_truncated_names():
    """EQuIS keeps 64 characters of a name; find() must match what it kept.

    Found 2026-10-12: names were compared whole, so a long one never matched
    and a new report was created on every visit - 10 for one facility.
    """
    me = "someone.long-address@example.com"
    long_name = "Example River Upper Basin Unit"
    full = lr.report_name(me, long_name, 4242)
    stored = full[:lr.NAME_LIMIT]
    check("names: this one really is cut (the id falls off)",
          len(full) > lr.NAME_LIMIT and "4242" not in stored, stored)
    copies = [{"id": -n, "name": stored, "owner": me, "code": "",
               "facilityId": 4242, "baseReportId": 777,
               "parameters": [{"name": "@facility_id", "value": "4242"}]}
              for n in (5001, 5009, 5004)]
    # Same first 64 characters, another facility: must never be adopted.
    other = {"id": -6000, "name": stored, "owner": me, "code": "",
             "facilityId": 999, "baseReportId": 777, "parameters": []}

    class Me(StubClient):
        username = me

    client = Me(reports=copies + [other, BASE777])
    live = lr.LiveReport(client, 4242, long_name, me)
    check("names: the stored (cut) name is found", live.find() is not None,
          str(live.find()))
    check("names: of several copies, the newest", live.find() == 5009, str(live.find()))
    check("names: and it is not recreated", live.ensure() == 5009 and not client.created,
          str(client.created))
    elsewhere = lr.LiveReport(Me(reports=[other, BASE777]), 4242, long_name, me)
    check("names: another facility's report with the same cut name is not adopted",
          elsewhere.find() is None, str(elsewhere.find()))


def test_real_listing():
    """Through the REAL EquisClient.list_reports, not a stub.

    The stub returns raw records, which carry `owner`; the real listing
    dropped it, so find()'s guard against adopting another user's
    same-named report never ran against a tenant - and every test passed,
    because the tests used the stub (found 2026-10-12).
    """
    from equis_loader.equis_api import EquisClient

    mine = lr.report_name("me@x.com", "F", 42)
    raw = [
        {"id": -501, "name": mine, "code": lr.BASE_REPORT_CODE,
         "facilityId": 42, "owner": "someone-else@x.com", "baseReportId": 9001},
        {"id": 9001, "name": lr.BASE_REPORT_CODE, "code": lr.BASE_REPORT_CODE,
         "facilityId": None, "owner": ".system", "baseReportId": 9001},
    ]

    class Canned(EquisClient):
        def _get_json(self, path, params=None, timeout=None):
            assert path == "/api/reports", path
            return [dict(r) for r in raw]

    client = Canned("https://example.invalid", "token")
    listed = {abs(r["id"]): r for r in client.list_reports(42)}
    check("listing: the real list_reports keeps owner and base",
          listed[501]["owner"] == "someone-else@x.com"
          and listed[501]["base_report_id"] == 9001, str(listed.get(501)))
    check("listing: the base report is recognisable from the real listing",
          lr.is_base_report(listed[9001]) and not lr.is_base_report(listed[501]))

    class AsMe(Canned):
        @property
        def username(self):
            return "me@x.com"

    live = lr.LiveReport(AsMe("https://example.invalid", "token"), 42, "F", "me@x.com")
    check("listing: another user's same-named report is not adopted (real listing)",
          live.find() is None, str(live.find()))
    raw[0]["owner"] = "ME@x.com"
    check("listing: our own is found, owner compared case-insensitively",
          live.find() == 501, str(live.find()))


def test_read_only():
    """Read-only refuses every write before it leaves the machine.

    Through the REAL EquisClient with urlopen stubbed, so the gate tested is
    the one in _request that every write passes through, not a stub's
    imitation of it. Reads must still work, or the mode would be useless.
    """
    import urllib.request
    from equis_loader.equis_api import EquisClient, EquisError

    sent = []

    class _Resp(object):
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return b"[]"

    def fake_urlopen(req, **_kw):
        sent.append((req.get_method(), req.full_url))
        return _Resp()

    real = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        client = EquisClient("https://example.invalid", "token",
                             read_only=True)
        writes = [
            ("create_report", lambda: client.create_report(
                1, "n", "c", 42, [], remark="r")),
            ("save_report", lambda: client.save_report({"id": -5})),
            ("delete_report", lambda: client.delete_report(-5)),
        ]
        for label, call in writes:
            try:
                call()
                refused = False
            except EquisError as exc:
                refused = "Read-only" in str(exc)
            check("read-only: %s is refused" % label, refused)
        # These two swallow errors by design (filing is tidiness), so the
        # proof is that nothing was sent and neither claims success.
        check("read-only: filing into a group does not succeed",
              client.add_group_member(7, -5) is False)
        check("read-only: removing a membership does not succeed",
              client.remove_group_member(7, -5) is False)
        check("read-only: no write reached the network", not sent, str(sent))

        client.list_groups()
        check("read-only: reads still go through",
              sent == [("GET", "https://example.invalid/api/groups")],
              str(sent))

        del sent[:]
        client.read_only = False
        client.delete_report(-5)
        check("read-only: released mid-session, writes are sent again",
              [m for m, _ in sent] == ["DELETE"], str(sent))
    finally:
        urllib.request.urlopen = real


def main():
    test_encoding()
    test_selection_parameters()
    test_apply_to_report()
    test_warnings()
    test_client_side_matching()
    test_mixed_fraction_detection()
    test_report_naming()
    test_lifecycle()
    test_filing()
    test_prepare_before_fetch()
    test_truncated_names()
    test_real_listing()
    test_read_only()
    print("\n%s" % ("ALL LIVE TESTS PASSED" if not FAILURES
                    else "FAILURES: %s" % ", ".join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
