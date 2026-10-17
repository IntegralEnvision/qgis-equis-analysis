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
