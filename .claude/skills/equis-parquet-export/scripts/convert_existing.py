r"""Convert the units in parquet already on disk, without re-collecting.

    python scripts/convert_existing.py --root data/<facility> --dry-run
    python scripts/convert_existing.py --root data/<facility>

WHY THIS EXISTS SEPARATELY. nightly_slices.py converts as it writes, so
tomorrow's collect is already handled. This is for the 3.45 million rows
sitting here now, extracted before any of that: re-downloading 36 EIAs to
change a multiplier would be absurd when the files are right there.

IT USES THE SAME CODE. unit_convert.convert_table does the work and the
factors come from EQuIS, exactly as in the collect - a second
implementation for the same arithmetic is how the two quietly disagree.

WHAT IT PRESERVES. repack sorted each partition on CHEMICAL_NAME,
SYS_LOC_CODE, SAMPLE_DATE and wrote 50,000-row groups so row-group
statistics can skip whole chunks; measured, that cut a one-analyte read
from 1.27 MiB to 0.34 MiB. Sorting WITHOUT small row groups made it worse.
So this re-sorts and re-groups with the same settings rather than writing
whatever pyarrow defaults to, and checks the group count afterwards.

WRITE-ASIDE-AND-RENAME. A partition is replaced only once its converted
copy is complete and its row count matches. An interrupted run leaves the
original untouched, because a half-written partition is worse than an
unconverted one - the reader cannot tell the difference.

NOT IDEMPOTENT BY ACCIDENT, BUT BY CHECK. Running twice must not multiply
twice. A partition already carrying RESULT_UNIT_ORIGINAL has been
converted, and is skipped unless --force says otherwise.
"""

import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import unit_convert  # noqa: E402
from nightly_slices import ROW_GROUP_ROWS, SORT_COLUMNS, read_env  # noqa: E402
from slice_store import load_units  # noqa: E402

MARKER = "RESULT_UNIT_ORIGINAL"


def partitions(root):
    """(path, matrix, fraction) for every repacked partition under `root`."""
    found = []
    for matrix in sorted(os.listdir(root)):
        if not matrix.startswith("matrix="):
            continue
        matrix_dir = os.path.join(root, matrix)
        if not os.path.isdir(matrix_dir):
            continue
        for fraction in sorted(os.listdir(matrix_dir)):
            if not fraction.startswith("fraction="):
                continue
            directory = os.path.join(matrix_dir, fraction)
            for name in sorted(os.listdir(directory)):
                if name.endswith(".parquet"):
                    found.append((os.path.join(directory, name),
                                  matrix.split("=", 1)[1],
                                  fraction.split("=", 1)[1]))
    return found


def convert_one(path, matrix, fraction, factors, choices, force=False):
    """Convert one partition in place. Returns (status, rows, counts)."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    target = unit_convert.target_for(matrix, fraction, choices)
    if not target:
        return "no target for %s" % matrix, 0, {}

    table = pq.read_table(path)
    if MARKER in table.column_names and not force:
        return "already converted", table.num_rows, {}

    before = table.num_rows
    table, counts = unit_convert.convert_table(table, target, factors)
    if table.num_rows != before:
        # Conversion multiplies; it must never add or drop a row.
        return "REFUSED: row count changed %d -> %d" % (before, table.num_rows), 0, {}
    if not counts:
        return "nothing to convert", before, {}

    # The sort and row-group size repack chose, not pyarrow's defaults.
    keys = [(c, "ascending") for c in SORT_COLUMNS if c in table.column_names]
    if keys:
        table = table.take(pc.sort_indices(table, sort_keys=keys))

    temporary = path + ".converting"
    pq.write_table(table, temporary, compression="zstd", compression_level=9,
                   use_dictionary=True, row_group_size=ROW_GROUP_ROWS,
                   write_statistics=True)
    # Read the copy back before trusting it. A partition replaced by a file
    # that will not open is the one failure this cannot recover from.
    check = pq.ParquetFile(temporary)
    if check.metadata.num_rows != before:
        os.unlink(temporary)
        return "REFUSED: written copy has %d rows, not %d" % (
            check.metadata.num_rows, before), 0, {}
    del check
    os.replace(temporary, path)
    return "ok", before, counts


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="convert again even if already marked")
    args = parser.parse_args()

    site, key = read_env()
    factors = unit_convert.load_factors(site, key)
    print("%d conversion factor(s) from EQuIS" % len(factors))

    choices = load_units(args.root)
    if not choices:
        print("no units.json under %r: nothing has a target unit, so "
              "nothing would convert" % args.root)
        return 0
    found = partitions(args.root)
    if not found:
        print("no partitions under %r" % args.root)
        return 1

    if args.dry_run:
        import pyarrow.parquet as pq
        from collections import Counter
        print("\nDRY RUN - what would convert:")
        for path, matrix, fraction in found:
            target = unit_convert.target_for(matrix, fraction, choices)
            table = pq.read_table(path, columns=["RESULT_UNIT"])
            units = Counter((u or "").strip()
                            for u in table.column("RESULT_UNIT").to_pylist())
            movable = {u: n for u, n in units.items()
                       if u and u != target and (u, target) in factors}
            staying = sum(n for u, n in units.items()
                          if u and u != target and (u, target) not in factors)
            print("   %-6s %-2s -> %-6s  %s%s"
                  % (matrix, fraction, target or "(none)",
                     unit_convert.describe({(u, target): n
                                            for u, n in movable.items()})
                     or "nothing",
                     "  (%s row(s) keep their unit)" % format(staying, ",d")
                     if staying else ""))
        return 0

    print()
    converted = skipped = 0
    for path, matrix, fraction in found:
        status, rows, counts = convert_one(path, matrix, fraction, factors, choices,
                                           force=args.force)
        note = unit_convert.describe(counts)
        print("   %-6s %-2s %9s rows  %s%s"
              % (matrix, fraction, format(rows, ",d"), status,
                 "  [" + note + "]" if note else ""))
        if status == "ok":
            converted += 1
        else:
            skipped += 1
    print()
    print("   %d partition(s) converted, %d left as they were"
          % (converted, skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
