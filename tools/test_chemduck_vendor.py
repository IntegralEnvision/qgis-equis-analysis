"""The vendored ChemDuck: the pinned commit, unedited, usable here, shipped.

ChemDuck (docs/chemduck-adoption-plan.md) is copied into the plugin as the
subpackage `equis_loader.chemduck` by tools/vendor_chemduck.py, from one
commit of its repository, with each file's hash in VENDORED.json. A
subpackage rather than a top-level `chemduck` on sys.path, so a different
ChemDuck a user has installed can never answer for this one. Checks:

  * every file is the one recorded, and none was added - a hand edit to a
    vendored rule would otherwise be a fourth copy of the chemistry rules
    hiding inside the "one authority";
  * the plugin imports its own copy, at the recorded version, and that copy
    imports no QGIS (CLAUDE.md's structural rule);
  * the DuckDB running this is one ChemDuck has tested (SUPPORTED_DUCKDB).
    Run it under each QGIS the plugin supports: that is the check that the
    QGIS's own DuckDB, or the plugin's bundle, will do;
  * it installs into a schema of its own, with extensions neither loaded
    nor installed (the plugin ships no `spatial`), leaves `main` empty, and
    answers a macro;
  * it reaches the plugin zip;
  * where the ChemDuck checkout is present, the files are that commit's.

    python tools/test_chemduck_vendor.py [--source ../chemduck]
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

VENDORED = os.path.join(ROOT, "equis_loader", "chemduck")
RECORD = os.path.join(VENDORED, "VENDORED.json")
FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + str(detail)) if detail != "" else ""))
    if not condition:
        FAILURES.append(label)


def on_disk():
    found = set()
    for base, dirs, files in os.walk(VENDORED):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if name == "VENDORED.json" or name.endswith((".pyc", ".pyo")):
                continue
            found.add(os.path.relpath(os.path.join(base, name), VENDORED)
                      .replace("\\", "/"))
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=os.path.join(ROOT, "..", "..", "chemduck"),
                        help="a ChemDuck checkout, to compare against the pinned "
                             "commit; skipped when absent")
    args = parser.parse_args()

    check("the vendored ChemDuck is present", os.path.isfile(RECORD), RECORD)
    if not os.path.isfile(RECORD):
        print("\nRun: python tools/vendor_chemduck.py --commit <ChemDuck commit>")
        return 1
    with open(RECORD, encoding="utf-8") as handle:
        record = json.load(handle)
    files = record.get("files", {})
    commit = record.get("commit", "")
    check("VENDORED.json names a full commit",
          len(commit) == 40 and all(c in "0123456789abcdef" for c in commit), commit)
    check("VENDORED.json names the version", bool(record.get("version")))

    # --- every file the one recorded, and nothing added --------------------
    bad = []
    for name, entry in sorted(files.items()):
        path = os.path.join(VENDORED, name)
        if not os.path.isfile(path):
            bad.append("%s missing" % name)
            continue
        with open(path, "rb") as handle:
            blob = handle.read()
        if (hashlib.sha256(blob).hexdigest() != entry.get("sha256")
                or len(blob) != entry.get("bytes")):
            bad.append("%s differs" % name)
    check("every vendored file matches its recorded hash (%d files)" % len(files),
          not bad, "; ".join(bad[:5]))
    extra = sorted(on_disk() - set(files))
    check("no file was added to the vendored copy", not extra, ", ".join(extra[:5]))
    for name in ("__init__.py", "load_data.py", "arii_hybrid.py",
                 "equis_arii_parquet_mapping.yaml", "sql/08_aggregate_points.sql",
                 "sql/10_sample_results.sql"):
        check("%s is vendored" % name, name in files)

    # --- this plugin's copy, imports no QGIS -------------------------------
    # Imports, not mentions: ChemDuck's comments name "the QGIS plugin" it
    # serves.
    import re
    importing = re.compile(rb"^\s*(?:import|from)\s+(?:qgis|PyQt\d?)\b", re.M)
    qgis = [name for name in files if name.endswith(".py")
            and importing.search(open(os.path.join(VENDORED, name), "rb").read())]
    check("the vendored ChemDuck imports no QGIS or Qt", not qgis, ", ".join(qgis))
    try:
        from equis_loader import chemduck
        imported = ""
    except Exception as exc:                  # noqa: BLE001 - that is the failure
        chemduck, imported = None, "%s: %s" % (type(exc).__name__, exc)
    check("it imports as equis_loader.chemduck", chemduck is not None, imported)
    if chemduck is None:
        return finish()
    check("the copy imported is the vendored one",
          os.path.dirname(os.path.abspath(chemduck.__file__)) == os.path.abspath(VENDORED),
          chemduck.__file__)
    check("its version is the recorded one", chemduck.__version__ == record.get("version"),
          "%s vs %s" % (chemduck.__version__, record.get("version")))
    check("nothing in sys.modules is a top-level chemduck",
          "chemduck" not in sys.modules,
          "a top-level copy would mean another ChemDuck could answer")

    # --- the DuckDB here is one ChemDuck has tested ------------------------
    import duckdb
    check("DuckDB %s is in ChemDuck's SUPPORTED_DUCKDB %s"
          % (duckdb.__version__, ", ".join(chemduck.SUPPORTED_DUCKDB)),
          duckdb.__version__ in chemduck.SUPPORTED_DUCKDB)

    # --- in its own schema, no extensions, and it answers -------------------
    con = duckdb.connect(config={"autoload_known_extensions": False,
                                 "autoinstall_known_extensions": False})
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            chemduck.load_schema(con, schema="chemduck")
        answered = ""
    except Exception as exc:                  # noqa: BLE001
        answered = "%s: %s" % (type(exc).__name__, exc)
    check("it installs into schema chemduck with extensions off", not answered,
          answered[:200])
    if not answered:
        in_main = con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'").fetchall()
        check("it creates nothing in main", not in_main, str(in_main[:5]))
        loaded = con.execute(
            "SELECT extension_name FROM duckdb_extensions() WHERE loaded "
            "AND extension_name = 'spatial'").fetchall()
        check("spatial is not loaded", not loaded)
        # The hand case from ChemDuck's test_km_quartiles, through the
        # qualified name from main, as the plugin's page will call it.
        row = con.execute("""
            WITH km_rows AS (SELECT 'a' AS grp, v AS value, TRUE AS detected
                             FROM (VALUES (1.0), (2.0), (3.0), (4.0)) t(v))
            SELECT q1, med, q3 FROM chemduck.km_quartiles_of('km_rows')""").fetchone()
        check("a macro answers: 1, 2, 3, 4 gives 1.5 / 2.5 / 3.5",
              row == (1.5, 2.5, 3.5), row)
    con.close()

    # --- it ships -----------------------------------------------------------
    import package as packager
    shipped = {relative.replace("\\", "/") for _, relative in
               packager.collect_files(os.path.join(ROOT, "equis_loader"))}
    missing = sorted("chemduck/" + name for name in list(files) + ["VENDORED.json"]
                     if "chemduck/" + name not in shipped)
    check("every vendored file is packaged", not missing, ", ".join(missing[:5]))

    # --- the pinned commit's files ------------------------------------------
    source = os.path.abspath(args.source)
    if not os.path.isdir(os.path.join(source, ".git")):
        print("[SKIP] no ChemDuck checkout at %s: provenance not compared" % source)
        return finish()
    import shutil
    if shutil.which("git") is None:
        # python-qgis.bat rebuilds PATH from OSGeo4W's, as it does to node
        # (tools/node_path.py), so under QGIS git is usually absent. The
        # hashes above still hold; this compares them to the commit.
        print("[SKIP] git is not on PATH: provenance not compared")
        return finish()
    differ = []
    for name, entry in sorted(files.items()):
        blob = subprocess.run(["git", "-C", source, "cat-file", "blob",
                               "%s:chemduck/%s" % (commit, name)],
                              capture_output=True).stdout
        if hashlib.sha256(blob).hexdigest() != entry["sha256"]:
            differ.append(name)
    check("each file is the pinned commit's (%s)" % commit[:7], not differ,
          ", ".join(differ[:5]))
    return finish()


def finish():
    print()
    if FAILURES:
        print("FAILED: %s" % ", ".join(FAILURES))
        return 1
    print("The vendored ChemDuck is the pinned commit, usable here, and shipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
