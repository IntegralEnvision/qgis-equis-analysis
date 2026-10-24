"""Write the reader-facing manifest that a client points at.

    python pipeline/publish_manifest.py --root data/<name>

TWO MANIFESTS, ON PURPOSE. The nightly pipeline's manifest.json holds
report ids, agent event ids, per-slice location lists and run history -
everything needed to re-run and re-bin tomorrow, and nothing a reader wants.
This writes a second, small one for publication:

  * it names the partition FILES, so a reader never globs (DuckDB rejects
    globs over generic HTTP, which would otherwise force az:// and an
    account key onto what can be a plain signed URL);
  * it carries row counts, so a truncated upload is caught rather than read
    as a smaller facility;
  * it carries no report ids and no location lists, because the published
    copy is readable by anyone holding the URL.

Upload this beside the matrix=*/fraction=* tree; do NOT upload the pipeline
manifest.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from slice_store import (  # noqa: E402
    MANIFEST_NAME, Manifest, cell_key, load_units, utc_now,
)

PUBLISHED_NAME = "manifest.json"


def build(root, facility, partition_file="part-0000.parquet"):
    """Describe what is actually on disk, not what the pipeline intended.

    Read from the FILES rather than from the pipeline manifest: a slice that
    failed still has an entry there, and publishing a partition that was
    never written would send readers at a 404.
    """
    partitions = []
    for matrix in sorted(os.listdir(root)):
        if not matrix.startswith("matrix="):
            continue
        matrix_dir = os.path.join(root, matrix)
        if not os.path.isdir(matrix_dir):
            continue
        for fraction in sorted(os.listdir(matrix_dir)):
            if not fraction.startswith("fraction="):
                continue
            path = os.path.join(matrix_dir, fraction, partition_file)
            if not os.path.isfile(path):
                continue
            partitions.append({
                "matrix": matrix.split("=", 1)[1],
                "fraction": fraction.split("=", 1)[1],
                # Forward slashes: this becomes a URL fragment, and
                # os.path.join would write backslashes on Windows.
                "path": "%s/%s/%s" % (matrix, fraction, partition_file),
                "bytes": os.path.getsize(path),
            })
    if not partitions:
        raise SystemExit("no matrix=*/fraction=*/%s under %r"
                         % (partition_file, root))

    # Row counts from the parquet footers - metadata, not a scan. Read with
    # pyarrow, which the pipeline already needs, rather than DuckDB: this
    # keeps the pipeline's dependencies to the stdlib and pyarrow.
    import pyarrow.parquet as pq
    total = 0
    for entry in partitions:
        full = os.path.join(root, *entry["path"].split("/"))
        entry["rows"] = pq.ParquetFile(full).metadata.num_rows
        total += entry["rows"]

    # AS OF, per partition. nightly_slices.py keeps a cell's previous
    # partition when any of its slices stored nothing, rather than publishing
    # a partial one - so partitions can differ in age, and a reader must be
    # able to see that instead of assuming the whole set is from one night.
    # The target unit is published too: a reader comparing two partitions
    # needs to know whether their numbers are in the same unit.
    local = Manifest(os.path.join(root, MANIFEST_NAME))
    units = load_units(root)
    for entry in partitions:
        when = local.partition_as_of(entry["matrix"], entry["fraction"])
        if when:
            entry["as_of"] = when
        unit = units.get(cell_key(entry["matrix"], entry["fraction"]))
        if unit:
            entry["unit"] = unit
        # Rows the pipeline left out for a unit on the other basis from
        # `unit` (mg/kg in a ug/L cell), by "<chemical> <unit>". Published
        # so a reader can see the partition is short, and of what.
        dropped = local.partition_dropped(entry["matrix"], entry["fraction"])
        if dropped:
            entry["dropped"] = dropped
            entry["dropped_rows"] = sum(dropped.values())
    published = {"version": 1, "facility": facility, "rows": total,
                 "generated": utc_now(), "partitions": partitions}
    if local.data.get("facility_id") is not None:
        published["facility_id"] = local.data["facility_id"]
    return published


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True)
    parser.add_argument("--facility", default=None,
                        help="name for the published manifest; defaults to "
                             "the root directory's name")
    parser.add_argument("--out", default=None,
                        help="where to write it; defaults to <root>/%s"
                             % PUBLISHED_NAME)
    parser.add_argument("--print-upload", action="store_true",
                        help="say what to upload and where")
    args = parser.parse_args()

    facility = args.facility or os.path.basename(os.path.abspath(args.root))
    published = build(args.root, facility)
    out = args.out or os.path.join(args.root, PUBLISHED_NAME)

    # The two manifests have different names precisely so this cannot happen
    # by default, but --out is free-form and the pipeline manifest holds the
    # only copy of the slice-to-report mapping, which cannot be rebuilt from
    # the parquet.
    if os.path.abspath(out) == os.path.abspath(
            os.path.join(args.root, MANIFEST_NAME)):
        raise SystemExit(
            "that would overwrite the pipeline manifest (%s), which holds the "
            "report and agent ids." % MANIFEST_NAME)

    with open(out, "w", encoding="utf-8") as handle:
        json.dump(published, handle, indent=1, sort_keys=True)

    print("%s" % out)
    print("  UPLOAD THIS to the container root as '%s'." % PUBLISHED_NAME)
    print("  %d partitions, %s rows, %.1f KB"
          % (len(published["partitions"]), format(published["rows"], ",d"),
             os.path.getsize(out) / 1024.0))
    for entry in published["partitions"]:
        print("   %-6s %-2s %12s rows  %6.1f MiB  %-8s as of %s"
              % (entry["matrix"], entry["fraction"],
                 format(entry["rows"], ",d"),
                 entry["bytes"] / 1048576.0, entry.get("unit") or "-",
                 entry.get("as_of") or "unknown"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
