r"""Make each slice report return ONE unit, by setting @reporting_unit.

    python pipeline/set_reporting_units.py --root data/<facility> --dry-run
    python pipeline/set_reporting_units.py --root data/<facility>

WHY. A result's unit is whatever the lab reported, so one analyte in one
matrix arrives in several. Measured on the first facility 2026-09-24: Arsenic in WS is
68,371 rows in ug/L and 22,121 in mg/L - a factor of 1000 - and 237
(chemical, matrix) pairs mix units the same way. A maximum taken across
them ranks by UNIT rather than by concentration: 0.57 mg/L is 570 ug/L and
loses to 168 ug/L. Every number on the resulting map looks plausible.

A careful reader refuses to aggregate across units, which makes the problem
visible. This removes it, at the source, where EQuIS owns the conversion
table and this code does not.

ONE UNIT PER CELL, NOT ONE OVERALL. Arsenic is mg/kg and ug/g in solids
and mg/L and ug/L in waters; no single reporting unit spans them. The
slices are already partitioned by (matrix, fraction), so each gets the
unit that cell is measured in, from <root>/units.json. That is the whole
reason this is per-slice rather than one edit to the base report.

WHAT IT TOUCHES. Only @reporting_unit, and only on reports named in the
manifest - never a report it did not create, never another parameter. It
re-reads after writing, because a 200 from the PUT does not mean the value
applied; save_locations in nightly_slices.py learned that the hard way.

@skip_missing_unit_conversion IS LEFT ALONE, deliberately. It defaults to
False, meaning a result EQuIS cannot convert is NOT skipped - and whether
that drops the row or passes it through unconverted decides between fewer
rows and silently wrong ones. That wants measuring against real output
before anyone changes it, and this tool does not guess.
"""

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from nightly_slices import Api, read_env  # noqa: E402
from slice_store import (  # noqa: E402
    MANIFEST_NAME, UNITS_NAME, Manifest, cell_key, load_units,
)

PARAMETER = "@reporting_unit"

# Which unit each cell is set to is the facility's choice, kept in
# <root>/units.json and shared with unit_convert.py - one authority, where
# there used to be two copies of one facility's table. See
# slice_store.load_units for why it is keyed (matrix, fraction): a dissolved
# fraction on a solid is a LEACHATE in mg/L, and setting a solid matrix to
# mg/kg wholesale made EQuIS answer
#
#     "MISSING UNIT CONVERSION FACTOR - cannot convert 'mg/L' to 'mg/kg'"
#
# for the WHOLE report, 78 bytes instead of 1,331 rows.


def fail(message):
    sys.stderr.write("\n%s\n\n" % message)
    raise SystemExit(2)


def allowed_units(api, report_id):
    """What the report will accept, asked rather than assumed.

    Asked of one of the slices' OWN reports, not a hard-coded base report:
    a base report's id differs between tenants.
    """
    status, body = api.json("/api/reports/%d/parameters/%s"
                            % (report_id, PARAMETER))
    if status != 200:
        return None
    options = body if isinstance(body, list) else (
        body.get("values") or body.get("options") or [])
    return {o[0] if isinstance(o, (list, tuple)) else o for o in options}


def set_unit(api, report_id, unit):
    """Write @reporting_unit and verify it stuck. Returns (ok, before)."""
    status, report = api.json("/api/reports/%d" % report_id)
    if status != 200 or not isinstance(report, dict):
        return False, None
    before = None
    found = False
    for param in report.get("parameters") or []:
        if param.get("name") == PARAMETER:
            before = param.get("value")
            param["value"] = unit
            param["displayValue"] = unit
            found = True
    if not found:
        return False, None
    api.json("/api/reports/%d" % report_id, "PUT", report)
    # The same settle the location writer needs: a 200 from the PUT is not
    # a promise that a re-read will show the value.
    time.sleep(1)
    status, check = api.json("/api/reports/%d" % report_id)
    if status != 200 or not isinstance(check, dict):
        return False, before
    for param in check.get("parameters") or []:
        if param.get("name") == PARAMETER:
            return param.get("value") == unit, before
    return False, before


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

    choices = load_units(args.root)
    if not choices:
        fail("No %s under %s - no cell has a unit chosen, so there is "
             "nothing to set." % (UNITS_NAME, args.root))
    any_report = next((entry.get("report_id") for entry in slices.values()
                       if entry.get("report_id")), None)
    permitted = allowed_units(api, int(any_report)) if any_report else None
    if permitted:
        wanted_units = set(choices.values())
        unknown = {u for u in wanted_units if u not in permitted}
        if unknown:
            fail("The report does not offer %s. It offers: %s"
                 % (", ".join(sorted(unknown)),
                    ", ".join(sorted(permitted))))
        print("the report accepts %d units; the ones wanted are among them"
              % len(permitted))

    print("%s%s" % ("DRY RUN: " if args.dry_run else "",
                    "setting %s per cell" % PARAMETER))
    changed = skipped = failed = 0
    for name in sorted(slices):
        if args.only and name != args.only:
            continue
        entry = slices[name]
        matrix = (entry.get("matrix") or "").upper()
        report_id = entry.get("report_id")
        unit = choices.get(cell_key(matrix, entry.get("fraction")))
        if not report_id:
            print("   %-32s no report recorded - skipped" % name)
            skipped += 1
            continue
        if not unit:
            # A cell nobody decided a unit for. Refusing is right: a
            # guess here is a silent unit conversion on real data.
            print("   %-32s %s has no unit in %s - skipped"
                  % (name, cell_key(matrix, entry.get("fraction")),
                     UNITS_NAME))
            skipped += 1
            continue
        if args.dry_run:
            print("   %-32s %-6s -> %s" % (name, matrix, unit))
            changed += 1
            continue
        ok, before = set_unit(api, report_id, unit)
        if ok:
            was = "unset" if not before else before
            print("   %-32s %-6s %s -> %s" % (name, matrix, was, unit))
            manifest.put_slice(name, reporting_unit=unit)
            manifest.save()
            changed += 1
        else:
            print("   %-32s FAILED to set %s" % (name, unit))
            failed += 1

    print()
    print("   %d set, %d skipped, %d failed" % (changed, skipped, failed))
    if changed and not args.dry_run:
        print()
        print("   The reports now convert on generation. The parquet on disk")
        print("   is unchanged - it was extracted under the old settings, so")
        print("   the mixed units stay there until the next collect.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
