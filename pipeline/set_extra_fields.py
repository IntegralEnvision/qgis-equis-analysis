r"""Add slice_store.EXTRA_FIELDS to every slice report's @extra_fields.

    python pipeline/set_extra_fields.py --root data/<facility> --dry-run
    python pipeline/set_extra_fields.py --root data/<facility> --only <slice>
    python pipeline/set_extra_fields.py --root data/<facility>

WHY. create_slice_reports.py asks for the extra columns when it makes a
slice, but slices made before a column was added keep the @extra_fields
they were saved with. This brings them level, so every slice in a cell
returns the same columns. Until it has run, a repacked cell holds the new
column for some locations and nulls for the rest - repack's permissive
concat allows that, and a reader cannot tell "not asked" from "empty".

WHAT IT TOUCHES. Only @extra_fields, and only on reports named in the
manifest - never a report it did not create, never another parameter. It
appends; a column the report already asks for is left as it is. It re-reads
after writing, because a 200 from the PUT does not mean the value applied
(save_locations in nightly_slices.py, set_reporting_units.py).

The parquet on disk is unchanged: the column arrives with each slice's
next stored output, and the nightly collect brings it in.
"""

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from nightly_slices import Api, read_env  # noqa: E402
from slice_store import (  # noqa: E402
    EXTRA_FIELDS, MANIFEST_NAME, Manifest, merge_extra_fields,
)

PARAMETER = "@extra_fields"


def fail(message):
    sys.stderr.write("\n%s\n\n" % message)
    raise SystemExit(2)


def offered(api, report_id):
    """The TABLE.COLUMN names the report offers, upper-cased, or None.

    Asked of one of the slices' own reports, so a field the tenant does not
    have is refused here rather than saved onto every slice.
    """
    status, body = api.json("/api/reports/%d/parameters/%s"
                            % (report_id, PARAMETER))
    if status != 200 or not isinstance(body, list):
        return None
    # The service leads with a header row and a blank row.
    return {str(row[0] if isinstance(row, (list, tuple)) and row else row)
            .strip().upper() for row in body[2:]}


def current_value(report):
    for param in report.get("parameters") or []:
        if param.get("name") == PARAMETER:
            return True, param.get("value")
    return False, None


def set_fields(api, report_id, wanted):
    """Write the merged @extra_fields and verify it stuck. Returns ok."""
    status, report = api.json("/api/reports/%d" % report_id)
    if status != 200 or not isinstance(report, dict):
        return False
    for param in report.get("parameters") or []:
        if param.get("name") == PARAMETER:
            param["value"] = wanted
            param["displayValue"] = wanted
    api.json("/api/reports/%d" % report_id, "PUT", report)
    time.sleep(1)
    status, check = api.json("/api/reports/%d" % report_id)
    if status != 200 or not isinstance(check, dict):
        return False
    return current_value(check)[1] == wanted


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would change, write nothing")
    parser.add_argument("--only", help="one slice name, for a careful first try")
    args = parser.parse_args()

    site, key = read_env()
    api = Api(site, key)
    manifest = Manifest(os.path.join(args.root, MANIFEST_NAME))
    slices = manifest.data.get("slices") or {}
    if not slices:
        fail("No slices in %s - nothing to set."
             % os.path.join(args.root, MANIFEST_NAME))

    any_report = next((entry.get("report_id") for entry in slices.values()
                       if entry.get("report_id")), None)
    available = offered(api, int(any_report)) if any_report else None
    if available:
        missing = [f for f in EXTRA_FIELDS if f.upper() not in available]
        if missing:
            fail("The report does not offer %s, so it cannot be asked for."
                 % ", ".join(missing))
        print("the report offers %d fields; %s among them"
              % (len(available), ", ".join(EXTRA_FIELDS)))
    else:
        print("could not read what %s offers; going on, and the re-read "
              "after each write is the check" % PARAMETER)

    print("%sadding %s to %s"
          % ("DRY RUN: " if args.dry_run else "", ", ".join(EXTRA_FIELDS),
             PARAMETER))
    changed = already = skipped = failed = 0
    for name in sorted(slices):
        if args.only and name != args.only:
            continue
        report_id = slices[name].get("report_id")
        if not report_id:
            print("   %-32s no report recorded - skipped" % name)
            skipped += 1
            continue
        status, report = api.json("/api/reports/%d" % int(report_id))
        if status != 200 or not isinstance(report, dict):
            print("   %-32s could not read report %s" % (name, report_id))
            failed += 1
            continue
        found, before = current_value(report)
        if not found:
            # The descriptor cannot be synthesised (create_slice_reports.py
            # explains), so a report without the parameter is left alone.
            print("   %-32s report has no %s - skipped" % (name, PARAMETER))
            skipped += 1
            continue
        wanted = merge_extra_fields(before)
        if wanted == (before or None):
            print("   %-32s already asks for them" % name)
            already += 1
            continue
        if args.dry_run:
            print("   %-32s %s -> %s" % (name, before or "unset", wanted))
            changed += 1
            continue
        if set_fields(api, int(report_id), wanted):
            print("   %-32s %s -> %s" % (name, before or "unset", wanted))
            changed += 1
        else:
            print("   %-32s FAILED to set %s" % (name, wanted))
            failed += 1

    print()
    print("   %d %s, %d already set, %d skipped, %d failed"
          % (changed, "would change" if args.dry_run else "set", already,
             skipped, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
