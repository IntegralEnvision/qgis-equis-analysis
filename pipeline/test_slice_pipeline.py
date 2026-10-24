"""The parquet pipeline's bookkeeping, without EQuIS or a network.

    python pipeline/test_slice_pipeline.py

Each check here is a way the pipeline can lose data while reporting
success. The two that were real bugs until 2026-10-05:

  - repack rebuilt a cell's partition from whichever slices happened to
    produce output, so one slice timing out dropped its locations from the
    published data with no error;
  - location weights were one facility-wide dict summed across cells, so
    every cell was binned on the wrong numbers.

Needs pyarrow.
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    print("pyarrow is not importable; skipping.")
    raise SystemExit(0)

import nightly_slices  # noqa: E402
import slice_store as ss  # noqa: E402


def write_slice(staging, matrix, fraction, name, locations):
    directory = ss.partition_path(staging, matrix, fraction)
    os.makedirs(directory, exist_ok=True)
    table = pa.table({"SYS_LOC_CODE": locations,
                      "CHEMICAL_NAME": ["Arsenic"] * len(locations),
                      "SAMPLE_DATE": ["2020-01-01"] * len(locations)})
    pq.write_table(table, os.path.join(directory, name + ".parquet"))


def partition_rows(root, matrix, fraction):
    path = os.path.join(ss.partition_path(root, matrix, fraction),
                        "part-0000.parquet")
    return (pq.read_table(path).column("SYS_LOC_CODE").to_pylist()
            if os.path.isfile(path) else None)


root = tempfile.mkdtemp(prefix="slice_test_")
try:
    # ---------------------------------------------------------- names
    check("a legacy manifest keeps the names already in the tenant",
          ss.Manifest(os.path.join(root, "none.json")).prefix
          == ss.LEGACY_PREFIX)
    check("a new facility's names carry its id",
          ss.slice_name("WS", "D", 3, ss.default_prefix(1234567))
          == "parquet_1234567_WS_D_03")
    fresh = ss.Manifest(os.path.join(root, "fresh.json"))
    fresh.set_prefix("parquet_7")
    fresh.set_prefix("parquet_8")
    check("a prefix, once chosen, is not replaced", fresh.prefix == "parquet_7")

    # ------------------------------------------------------- weights
    manifest = ss.Manifest(os.path.join(root, ss.MANIFEST_NAME))
    manifest.data["location_rows"] = {"MW-1": 999}   # the old, summed dict
    check("the old facility-wide weights are not read for any cell",
          manifest.location_weights("WS", "D") == {})
    manifest.update_location_weights("WS", "D", {"MW-1": 10, "MW-2": 4})
    manifest.update_location_weights("WS", "N", {"MW-1": 500})
    check("a location's weight in one cell ignores its rows in another",
          manifest.location_weights("WS", "D") == {"MW-1": 10, "MW-2": 4},
          str(manifest.location_weights("WS", "D")))
    manifest.update_location_weights("WS", "D", {"MW-1": 12})
    check("a night's measurement merges, keeping locations it did not see",
          manifest.location_weights("ws", "d") == {"MW-1": 12, "MW-2": 4})

    # --------------------------------------------------------- units
    check("no units.json means no cell converts", ss.load_units(root) == {})
    check("a proposal is written where none exists",
          ss.propose_unit(root, "WS", "D", "ug/L", "test"))
    with open(os.path.join(root, ss.UNITS_NAME), "r+",
              encoding="utf-8") as handle:
        data = json.load(handle)
        data["cells"]["WS/D"] = "mg/L"          # a person's edit, bare form
        handle.seek(0)
        handle.truncate()
        json.dump(data, handle)
    check("a person's choice is never overwritten by a later proposal",
          not ss.propose_unit(root, "ws", "d", "ug/L", "test")
          and ss.load_units(root) == {"WS/D": "mg/L"},
          str(ss.load_units(root)))

    # ------------------------------------------------- repack holds
    # Cell WS/D: two slices, both staged -> rebuilt.
    # Cell WS/T: two slices, ONE staged, and yesterday's partition on disk
    # -> must be KEPT as it was, not rebuilt from half the locations.
    for name, matrix, fraction, locs in (
            ("p_WS_D_00", "WS", "D", ["A"]), ("p_WS_D_01", "WS", "D", ["B"]),
            ("p_WS_T_00", "WS", "T", ["C"]), ("p_WS_T_01", "WS", "T", ["D"])):
        manifest.put_slice(name, matrix=matrix, fraction=fraction,
                           locations=locs)
    yesterday = ss.partition_path(root, "WS", "T")
    os.makedirs(yesterday, exist_ok=True)
    pq.write_table(pa.table({"SYS_LOC_CODE": ["C", "D"],
                             "CHEMICAL_NAME": ["Arsenic"] * 2,
                             "SAMPLE_DATE": ["2019-01-01"] * 2}),
                   os.path.join(yesterday, "part-0000.parquet"))
    staging = os.path.join(root, "_staging")
    write_slice(staging, "WS", "D", "p_WS_D_00", ["A", "A"])
    write_slice(staging, "WS", "D", "p_WS_D_01", ["B"])
    write_slice(staging, "WS", "T", "p_WS_T_00", ["C"])   # _01 stored nothing

    held = nightly_slices.incomplete_cells(manifest, staging)
    check("the incomplete cell is identified, with its missing slice",
          held == {("WS", "T"): ["p_WS_T_01"]}, str(held))
    nightly_slices.repack(root, manifest)
    check("a complete cell is rebuilt from every slice",
          sorted(partition_rows(root, "WS", "D")) == ["A", "A", "B"])
    check("an incomplete cell keeps yesterday's partition, whole",
          sorted(partition_rows(root, "WS", "T")) == ["C", "D"],
          str(partition_rows(root, "WS", "T")))
    check("only the rebuilt partition is dated",
          manifest.partition_as_of("WS", "D") is not None
          and manifest.partition_as_of("WS", "T") is None)
    check("staging is removed, so a glob cannot count rows twice",
          not os.path.exists(staging))
    leftovers = [f for d, _, files in os.walk(root) for f in files
                 if f.endswith(".tmp")]
    check("no half-written partition is left behind", not leftovers,
          str(leftovers))

    # ------------------------------------------- rewrite needs weights
    class NoApi(object):
        def json(self, *args, **kwargs):
            raise AssertionError("a dry run must not call EQuIS")

    unmeasured = ss.Manifest(os.path.join(root, "unmeasured.json"))
    for index in range(4):
        unmeasured.put_slice("x_WS_N_%02d" % index, matrix="WS",
                             fraction="N", index=index,
                             locations=["L%d" % index])
    changes = nightly_slices.rewrite(NoApi(), unmeasured, 92000, dry_run=True)
    check("a cell nothing has measured is not re-binned onto zero weights",
          changes == [], str(changes))

    # ------------------------------------- the walk attributes by cell
    class FakeOData(object):
        PAGES = {
            "DT_SAMPLE": [{"SAMPLE_ID": 1, "SYS_LOC_CODE": "MW-1",
                           "MATRIX_CODE": "WS"},
                          {"SAMPLE_ID": 2, "SYS_LOC_CODE": "SB-1",
                           "MATRIX_CODE": "SO"},
                          {"SAMPLE_ID": 3, "SYS_LOC_CODE": "MW-9",
                           "MATRIX_CODE": "WS"}],
            "DT_TEST": [{"TEST_ID": 10, "SAMPLE_ID": 1, "FRACTION": "D"},
                        {"TEST_ID": 11, "SAMPLE_ID": 1, "FRACTION": "T"},
                        {"TEST_ID": 12, "SAMPLE_ID": 2, "FRACTION": "N"}],
            "DT_RESULT": [{"TEST_ID": 10}, {"TEST_ID": 10}, {"TEST_ID": 11},
                          {"TEST_ID": 12}],
        }

        def json(self, path, quote=True):
            entity = path.split("/api/odata/", 1)[1].split("?", 1)[0]
            return 200, {"value": self.PAGES[entity]}

    walked = ss.count_results_by_location(FakeOData(), 1)
    check("the exact walk counts each cell separately",
          walked.get("WS/D", {}).get("MW-1") == 2
          and walked.get("WS/T", {}).get("MW-1") == 1
          and walked.get("SO/N", {}).get("SB-1") == 1, str(walked))
    check("a location with samples but no results still gets a bin",
          walked.get("WS/D", {}).get("MW-9") == 0, str(walked))

    # ---------------------------------------- extra columns on every slice
    merged = ss.merge_extra_fields("RT_MATRIX.MATRIX_DESC")
    check("extra fields: the template's own survive, ours are appended",
          merged == "RT_MATRIX.MATRIX_DESC|DT_TEST.IDENTIFIER", merged)
    check("extra fields: merging twice changes nothing",
          ss.merge_extra_fields(merged) == merged)
    check("extra fields: EQuIS's literal 'None' is an empty parameter",
          ss.merge_extra_fields("None") == "DT_TEST.IDENTIFIER",
          str(ss.merge_extra_fields("None")))
    check("extra fields: LAB_SDG is not asked for twice",
          "LAB_SDG" not in merged.upper(), merged)
    import create_slice_reports
    cloned = create_slice_reports.clone_parameters(
        {"parameters": [{"name": "@extra_fields", "value": "None"},
                        {"name": "@sys_loc_codes", "value": "X"}]},
        7, "WS", "D", ["MW-1", "MW-2"])
    by_name = {p["name"]: p for p in cloned}
    check("extra fields: a new slice asks for the identifier",
          by_name["@extra_fields"]["value"] == "DT_TEST.IDENTIFIER"
          and by_name["@extra_fields"]["displayValue"] == "DT_TEST.IDENTIFIER",
          str(by_name["@extra_fields"]))
    check("extra fields: the slice's own filters are still applied",
          by_name["@sys_loc_codes"]["value"] == "MW-1|MW-2")

    # ------------------------------------------- pipeline.json at setup
    import create_slice_reports as csr
    sroot = os.path.join(root, "setup")
    csr.record_config(sroot, 7, "N", None, write=False)
    check("setup: a dry run writes no pipeline.json",
          not os.path.exists(os.path.join(sroot, ss.CONFIG_NAME)))
    path = csr.record_config(sroot, 7, "N", None)
    with open(path) as handle:
        written = json.load(handle)
    check("setup: the first cell writes what its slices were made with",
          written == {"facility": 7, "sample_types": "N", "template": None},
          str(written))
    written["upload"] = {"base": "https://a.blob.core.windows.net/c/x",
                         "sas_env": "X"}
    with open(path, "w") as handle:
        json.dump(written, handle)
    csr.record_config(sroot, 7, "N", None)
    with open(path) as handle:
        check("setup: a later cell keeps what a person added",
              "upload" in json.load(handle))
    try:
        csr.record_config(sroot, 7, None, None)
        refused = False
    except SystemExit:
        refused = True
    check("setup: a cell made with other sample types is refused", refused)
    import run_nightly
    check("setup: what it writes is what the check accepts",
          run_nightly.load_config(sroot)["sample_types"] == "N")

    # ---------------------------------- a unit on the wrong basis is dropped
    droot = os.path.join(root, "dropping")
    os.makedirs(droot)
    raw = os.path.join(droot, "slice.csv")
    with open(raw, "w") as handle:
        handle.write("SYS_LOC_CODE,CHEMICAL_NAME,RESULT_UNIT,REPORT_RESULT_UNIT,"
                     "RESULT_NUMERIC\n"
                     "S1,Arsenic,ug/L,ug/L,1.5\n"
                     "S1,Mercury,,ug/kg,\n"
                     "S2,Lead,mg/L,mg/L,0.002\n")
    out = os.path.join(droot, "staged.parquet")
    audit = os.path.join(droot, "_dropped", "slice.csv")
    rows, counts, moved, dropped = nightly_slices.csv_to_parquet(
        raw, out, target="ug/L", factors={("mg/L", "ug/L"): 1000.0},
        dropped_path=audit)
    staged = pq.read_table(out)
    check("drop: the mg/kg-basis row is not in the parquet",
          staged.column("CHEMICAL_NAME").to_pylist() == ["Arsenic", "Lead"]
          and rows == 2)
    check("drop: and is written, as stored, for whoever fixes the source",
          os.path.isfile(audit) and "ug/kg" in open(audit).read())
    check("drop: counted for the warning", dropped == {"Mercury ug/kg": 1},
          str(dropped))
    check("drop: the rest still converts",
          staged.column("RESULT_NUMERIC").to_pylist()[1] == 2.0)
    rows, _c, _m, dropped = nightly_slices.csv_to_parquet(raw, out)
    check("drop: a cell with no target unit drops nothing",
          rows == 3 and dropped == {})

    marked = ss.Manifest(os.path.join(droot, ss.MANIFEST_NAME))
    marked.put_slice("m_WS_D_00", matrix="WS", fraction="D",
                     dropped={"Mercury ug/kg": 100})
    marked.put_slice("m_WS_D_01", matrix="WS", fraction="D",
                     dropped={"Mercury ug/kg": 30})
    marked.put_slice("m_WS_T_00", matrix="WS", fraction="T", dropped={})
    marked.mark_partition("WS", "D", 10)
    marked.mark_partition("WS", "T", 10)
    check("drop: a rebuilt partition carries what its slices dropped",
          marked.partition_dropped("WS", "D") == {"Mercury ug/kg": 130}
          and marked.partition_dropped("WS", "T") == {},
          str(marked.partition_dropped("WS", "D")))

    # ------------------------------------- what EQuIS stores on failure
    stub = os.path.join(root, "stub.csv")
    with open(stub, "wb") as handle:
        handle.write(b'"Errors"\r\n"Report did not generate output.  '
                     b'Please try again."\r\n')
    check("stored error text is not report output",
          ss.output_problem(stub) is not None, str(ss.output_problem(stub)))
    with open(stub, "wb") as handle:
        handle.write(b"\xef\xbb\xbfFACILITY_CODE,SYS_LOC_CODE,CHEMICAL_NAME\r\n"
                     b"F,MW-1,Arsenic\r\n")
    check("a report's own CSV is", ss.output_problem(stub) is None)

    # ----------------------------------------------- the two clocks
    check("EQuIS's last-run text and the manifest's compare as times",
          ss.parse_utc("2026-10-19T20:35:19.82")
          > ss.parse_utc("2026-10-19T20:35:00Z")
          and ss.parse_utc("2026-10-19T20:35:19.82").tzinfo is not None)
    check("an empty time is None, not the epoch", ss.parse_utc(None) is None)

    # ------------------------------------------------ cell readiness
    ready = ss.Manifest(os.path.join(root, "ready.json"))
    for name, cell, event in (("r_WS_D_00", "D", 1), ("r_WS_D_01", "D", 2),
                              ("r_WS_T_00", "T", 3)):
        ready.put_slice(name, matrix="WS", fraction=cell, report_id=event,
                        event_id=event, armed_at="2026-10-23T10:00:00Z")
    runs = {1: "2026-10-23T10:40:00.5", 2: "2026-10-23T09:00:00.1",
            3: "2026-10-23T10:50:00.0"}
    states = ready.cell_states(runs)
    check("readiness: a cell with a slice that has not run since it was "
          "armed waits, and names it",
          states[("WS", "D")] == ("waiting", ["r_WS_D_01"]), str(states))
    check("readiness: a cell whose slices have all run is ready",
          states[("WS", "T")] == ("ready", ["r_WS_T_00"]), str(states))
    # A re-bin arms only the slices whose lists changed. One has run on its
    # new list, one has not: building now would mix old lists with new.
    runs[2] = "2026-10-23T10:45:00.0"
    ready.put_slice("r_WS_D_01", armed_at="2026-10-23T11:00:00Z")
    check("readiness: after a re-bin, a cell half run on its new lists waits",
          ready.cell_states(runs)[("WS", "D")][0] == "waiting")
    runs[2] = "2026-10-23T11:30:00.0"
    check("readiness: and is ready once every armed slice has run",
          ready.cell_states(runs)[("WS", "D")][0] == "ready")
    for name, event in (("r_WS_D_00", 1), ("r_WS_D_01", 2)):
        ready.put_slice(name, collected_run=runs[event], last_status="ok")
    check("readiness: output already collected is unchanged, not rebuilt",
          ready.cell_states(runs)[("WS", "D")] == ("unchanged", []))
    ready.put_slice("r_WS_D_01", last_status="error output")
    check("readiness: a collected error is reported as failed, not waited on",
          ready.cell_states(runs)[("WS", "D")] == ("failed", ["r_WS_D_01"]))
    ready.put_slice("r_WS_X_00", matrix="WS", fraction="X", needs_report=True)
    check("readiness: a slice without a report holds its cell",
          ready.cell_states(runs)[("WS", "X")][0] == "waiting")

    # ------------------------------------------------ the check itself
    class Tenant(object):
        """EQuIS as the check sees it: signal, agents, stored output."""
        base, api_key = "http://127.0.0.1:9", "test"

        def __init__(self):
            self.ebatch = 100
            self.agents = {}
            self.puts = []
            self.stored = {}
            self.unreachable = set()

        def json(self, path, method="GET", body=None, **_kw):
            if path.startswith("/api/odata/"):
                return 200, {"value": [{"EBATCH": self.ebatch}]}
            if path == "/api/eia":
                return 200, [{"ReportEvent": {
                    "id": i, "lastExecutionDateUTC_ReadOnly": a["ran"]}}
                    for i, a in self.agents.items()]
            event_id = int(path.rsplit("/", 1)[1])
            if method == "PUT":
                self.puts.append(event_id)
                self.agents[event_id]["schedule"] = body["Schedule"]
                return 200, None
            return 200, {"ReportEvent": {"id": event_id},
                         "Schedule": self.agents[event_id]["schedule"]}

        def eia_output(self, report_id, event_id, out_path, timeout=0):
            if event_id in self.unreachable:
                return None, 0.1, "URLError: getaddrinfo failed"
            with open(out_path, "wb") as handle:
                handle.write(self.stored[event_id])
            return len(self.stored[event_id]), 0.1, None

        def run_all(self, when):
            for agent in self.agents.values():
                agent["ran"] = when

    tenant = Tenant()
    croot = os.path.join(root, "checked")
    os.makedirs(croot)
    checked = ss.Manifest(os.path.join(croot, ss.MANIFEST_NAME))
    for name, event, loc in (("c_WS_D_00", 21, "MW-1"),
                             ("c_WS_D_01", 22, "MW-2")):
        checked.put_slice(name, matrix="WS", fraction="D", report_id=event,
                          event_id=event, locations=[loc])
        tenant.agents[event] = {"ran": None,
                                "schedule": {"id": event, "recurrenceType": 2,
                                             "effective": "2026-01-01T00:00:00Z"}}
        tenant.stored[event] = (
            "SYS_LOC_CODE,CHEMICAL_NAME,SAMPLE_DATE\n%s,Arsenic,2020-01-01\n"
            % loc).encode()

    nightly_slices.check(tenant, checked, croot, 7)
    check("check: the first only records the signal, and arms nothing",
          checked.data.get("load_signal") and not tenant.puts,
          str(tenant.puts))

    nightly_slices.check(tenant, checked, croot, 7)
    check("check: no new data arms nothing", not tenant.puts)

    tenant.ebatch = 101
    nightly_slices.check(tenant, checked, croot, 7)
    check("check: new data arms every slice",
          sorted(tenant.puts) == [21, 22], str(tenant.puts))
    check("check: arming makes a daily agent one-time, due today",
          all(a["schedule"]["recurrenceType"] == 1
              for a in tenant.agents.values()))
    check("check: and nothing is built before the slices have run",
          partition_rows(croot, "WS", "D") is None)

    del tenant.puts[:]
    tenant.run_all("2099-01-01T00:00:00.0")
    # A network fault on one download says nothing about the run.
    tenant.unreachable.add(22)
    nightly_slices.check(tenant, checked, croot, 7)
    check("check: a failed download holds the cell",
          partition_rows(croot, "WS", "D") is None)
    check("check: and is not taken as the run's outcome - the cell is "
          "still ready, to be retried",
          checked.cell_states({21: "2099-01-01T00:00:00.0",
                               22: "2099-01-01T00:00:00.0"})[("WS", "D")][0]
          == "ready" and checked.slice("c_WS_D_01").get("collected_run")
          is None)
    tenant.unreachable.clear()
    nightly_slices.check(tenant, checked, croot, 7)
    check("check: once all have run, the cell is collected and built",
          sorted(partition_rows(croot, "WS", "D") or []) == ["MW-1", "MW-2"],
          str(partition_rows(croot, "WS", "D")))
    check("check: collecting arms nothing", not tenant.puts)

    built = checked.partition_as_of("WS", "D")
    tenant.stored[22] = b"SHOULD NOT BE READ"
    nightly_slices.check(tenant, checked, croot, 7)
    check("check: output already collected is not downloaded again",
          checked.partition_as_of("WS", "D") == built
          and checked.slice("c_WS_D_01")["last_status"] == "ok")

    # A run that stored EQuIS's error text: the cell keeps what it had.
    tenant.stored[22] = (b'"Errors"\r\n"Report did not generate output.  '
                         b'Please try again."\r\n')
    tenant.run_all("2099-01-02T00:00:00.0")
    nightly_slices.check(tenant, checked, croot, 7)
    check("check: a slice that stored an error holds its cell's partition",
          sorted(partition_rows(croot, "WS", "D") or []) == ["MW-1", "MW-2"]
          and checked.slice("c_WS_D_01")["last_status"] == "error output",
          str(checked.slice("c_WS_D_01").get("last_status")))
    check("check: and the cell then reads as failed, not waiting",
          checked.cell_states({21: "2099-01-02T00:00:00.0",
                               22: "2099-01-02T00:00:00.0"})[("WS", "D")][0]
          == "failed")

    nightly_slices.check(tenant, checked, croot, 7, force=True)
    check("check: --trigger arms every slice with no new data",
          sorted(tenant.puts) == [21, 22], str(tenant.puts))

    # ---------------------------------------- the published manifest
    try:
        import publish_manifest
        manifest.data["facility_id"] = 4242
        manifest.save()
        published = publish_manifest.build(root, "test")
        parts = {(p["matrix"], p["fraction"]): p
                 for p in published["partitions"]}
        check("published partitions say how old they are",
              "as_of" in parts[("WS", "D")]
              and "as_of" not in parts[("WS", "T")])
        check("published partitions carry their unit, from units.json",
              parts[("WS", "D")].get("unit") == "mg/L")
        check("the published manifest names its facility",
              published.get("facility_id") == 4242)
        text = json.dumps(published)
        check("no report or agent id leaks into what is published",
              "report_id" not in text and "event_id" not in text
              and "locations" not in text)
    except ImportError as exc:
        print("[SKIP] published manifest: %s" % exc)
finally:
    shutil.rmtree(root, ignore_errors=True)

print()
if FAILURES:
    print("%d failure(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
    raise SystemExit(1)
print("The pipeline's bookkeeping holds.")
