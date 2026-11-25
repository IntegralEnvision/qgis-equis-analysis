"""Publish ChemDuck's sidecars beside a facility's manifest.

    python -m parquet_source.chemduck_sidecars --root data/<facility>

Phase 6 of the plugin's ChemDuck adoption plan (docs/chemduck-adoption-
plan.md in the plugin's repository). ChemDuck answers from a facility's own
parquet partitions plus small tables derived from them - locations,
samples, catalogues, its settings and load report - and the SQL that makes
`results` a view over the partitions. Deriving those means reading every
result once (57.7 s on the first facility, 3.45 million rows, 2026-10-29),
which is a night's work and not a reader's. This writes them, by
ChemDuck's own `publish`, into <root>/chemduck/, and run_nightly.py uploads
that folder before manifest.json.

WHY CHEMDUCK WRITES THEM, NOT THIS PACKAGE. The samples and locations are
ChemDuck's loader rules run once (its sample rules, id conflict checks,
method classes, unit plan), and results_view.sql is its loader's SELECT. A
copy written here would be a second set of those rules to keep in step.

WHICH CHEMDUCK. The package's own vendored copy (parquet_source/chemduck),
the same commit as the plugin's, written by the plugin repository's
tools/vendor_chemduck.py: a facility published tonight and the plugin
reading it run the same rules. It needs duckdb and pyyaml:
`pip install -e "pipeline[chemduck]"`.

THE FOLDER IS NOT IN manifest.json. sidecars.json records the SHA-256 of
the manifest it was built from (source_manifest.sha256). Listing the
folder in the manifest would change the manifest after that hash was
taken, so a reader could never tell current sidecars from stale ones. At a
fixed path, unlisted, a reader compares the hash with the manifest it
fetched and knows. That matters because a failed night leaves the last
upload's sidecars in place.

CONFIGURED in pipeline.json, off unless present:

    "chemduck": {"x": "LONGITUDE", "y": "LATITUDE"}

`x` and `y` name the coordinate columns, which must be WGS84 longitude and
latitude. They are a setting, never inferred: X_COORD is longitude at one
facility and a projected easting at the next (ChemDuck refuses anything
else). `memory_limit` is DuckDB's for the publish, default "8GB".

WHAT IT REFUSES, exit 2, writing nothing: no config, no published
manifest, a DuckDB ChemDuck wasn't tested with, and anything ChemDuck's
publish refuses (a row count that disagrees with the manifest, no
DETECT_FLAG, an id with conflicting values, coordinates that aren't
WGS84). The previous <root>/chemduck/ is left as it was: the folder is
built beside it and swapped in only whole.

WHAT IT REPORTS, exit 0. ChemDuck's unit plan, from its load_report. The
collect converts units with EQuIS's own factors (unit_convert.py) and
leaves a pair with no EQuIS factor alone. ChemDuck converts what is left by
ITS factors, and nothing in its mapping turns that off. Conversion belongs
here, with EQuIS's factors (the plugin's CLAUDE.md), so each ChemDuck
conversion is printed as an ATTENTION line, which run_nightly.py turns into
a note and an exit of 1: published, and needing a look. The adoption plan's
decision 9 is open on it.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys

FOLDER = "chemduck"
PUBLISHED_MANIFEST = "manifest.json"
CONFIG_NAME = "pipeline.json"
# A line run_nightly.py reads as needing a look (its ATTENTION markers).
ATTENTION = "ATTENTION:"


class Refused(Exception):
    """Nothing was written; the message says why."""


def sha256(path):
    """In ChemDuck's form, "sha256:<hex>" of the file's bytes, as its
    sidecars.json records a manifest (arii_hybrid._sha256)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def settings(root):
    path = os.path.join(root, CONFIG_NAME)
    try:
        with open(path, encoding="utf-8") as handle:
            config = json.load(handle).get("chemduck")
    except (OSError, ValueError) as exc:
        raise Refused("cannot read %s: %s" % (path, exc))
    if not isinstance(config, dict):
        raise Refused("%s has no \"chemduck\" section; see this module's docstring."
                      % CONFIG_NAME)
    if not config.get("x") or not config.get("y"):
        raise Refused("\"chemduck\" in %s needs \"x\" and \"y\", the WGS84 longitude "
                      "and latitude columns. They are never guessed." % CONFIG_NAME)
    return config


def chemduck_module():
    """The vendored ChemDuck, or Refused naming what to install."""
    try:
        from . import chemduck
    except ImportError as exc:
        raise Refused("ChemDuck needs duckdb and pyyaml (%s); "
                      "pip install -e \"pipeline[chemduck]\"" % exc)
    import duckdb
    if duckdb.__version__ not in chemduck.SUPPORTED_DUCKDB:
        raise Refused("DuckDB %s; the vendored ChemDuck %s was tested with %s only"
                      % (duckdb.__version__, chemduck.__version__,
                         ", ".join(chemduck.SUPPORTED_DUCKDB)))
    return chemduck


def unit_notes(folder):
    """ChemDuck's own conversions, from the load_report it published."""
    import duckdb
    path = os.path.join(folder, "load_report.parquet").replace("\\", "/")
    con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT item, n FROM read_parquet(?) WHERE category = 'units' ORDER BY item",
            [path]).fetchall()
    finally:
        con.close()
    totals = {item[len("rows "):]: n for item, n in rows if item.startswith("rows ")}
    converted = [(item, n) for item, n in rows
                 if item.startswith("converted:") and item.endswith("(chemduck)")]
    return totals, converted


def publish(root, config, chemduck):
    """Build the sidecars beside <root>/chemduck and swap them in whole.
    Returns the parsed sidecars.json."""
    manifest = os.path.join(root, PUBLISHED_MANIFEST)
    if not os.path.isfile(manifest):
        raise Refused("no %s in %s: publish_manifest.py runs first"
                      % (PUBLISHED_MANIFEST, root))
    final = os.path.join(root, FOLDER)
    building = final + ".building"
    if os.path.isdir(building):
        shutil.rmtree(building)          # a crashed night's leftovers
    try:
        chemduck.publish(manifest, building, x_col=config["x"], y_col=config["y"],
                         memory_limit=config.get("memory_limit", "8GB"), quiet=True)
        with open(os.path.join(building, "sidecars.json"), encoding="utf-8") as handle:
            sidecars = json.load(handle)
        recorded = (sidecars.get("source_manifest") or {}).get("sha256")
        if recorded != sha256(manifest):
            # The manifest changed under the publish. Uploading these would
            # pair them with a manifest they don't describe.
            raise Refused("%s changed while the sidecars were built" % PUBLISHED_MANIFEST)
    except Exception:
        shutil.rmtree(building, ignore_errors=True)
        raise
    if os.path.isdir(final):
        shutil.rmtree(final)
    os.rename(building, final)
    return sidecars


def files(root):
    """The folder's files, relative to root with forward slashes, for the
    upload; sidecars.json last, so a reader that finds it finds the rest."""
    folder = os.path.join(root, FOLDER)
    found = []
    for directory, _dirs, names in os.walk(folder):
        for name in sorted(names):
            found.append(os.path.relpath(os.path.join(directory, name), root)
                         .replace(os.sep, "/"))
    index = FOLDER + "/sidecars.json"
    return sorted(f for f in found if f != index) + ([index] if index in found else [])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, help="data/<facility>")
    args = parser.parse_args(argv)
    try:
        config = settings(args.root)
        chemduck = chemduck_module()
        sidecars = publish(args.root, config, chemduck)
    except Exception as exc:                   # noqa: BLE001 - a step reports
        print("chemduck_sidecars: %s: %s" % (type(exc).__name__, exc))
        return 2
    folder = os.path.join(args.root, FOLDER)
    tables = sidecars.get("tables") or {}
    print("ChemDuck %s sidecars for %s rows: %s" % (
        sidecars.get("chemduck", {}).get("version", "?"),
        format((sidecars.get("source_manifest") or {}).get("rows") or 0, ","),
        ", ".join("%s %s" % (name, format(info.get("rows") or 0, ","))
                  for name, info in sorted(tables.items()))))
    totals, converted = unit_notes(folder)
    print("units: %s" % ", ".join("%s %s" % (k, format(v, ","))
                                  for k, v in sorted(totals.items())))
    for item, n in converted:
        print("%s ChemDuck converted %s row(s) by its own factor, not EQuIS's: %s"
              % (ATTENTION, format(n, ","), item[len("converted: "):]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
