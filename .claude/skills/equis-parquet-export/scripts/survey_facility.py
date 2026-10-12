"""Survey a facility before slicing it: which (matrix, fraction) cells exist.

    python scripts/survey_facility.py --facility <id>
    python scripts/survey_facility.py --facility <id> --json survey.json

The first step of extracting a facility to parquet. It writes nothing to
EQuIS: it reads DT_SAMPLE and counts DT_TEST over OData, then prints each
matrix's sample and location counts, the facility's fractions, and the
create_slice_reports.py command for every candidate cell.

WHY CELLS ARE (matrix, fraction)
--------------------------------
They are not discovered so much as chosen. Both are saved report
parameters (@matrix_codes, @fractions), both become the Hive partition keys
(matrix=WS/fraction=D) that a reader prunes on, and a reader should never
aggregate across either. Location is what then divides a cell into slices,
because it is the one axis that subdivides almost without limit and is still
a saved parameter (@sys_loc_codes).

WHY THE CANDIDATES ARE A CROSS PRODUCT
--------------------------------------
Matrix lives on DT_SAMPLE and fraction on DT_TEST, and OData cannot join, so
which fractions each matrix carries is not cheaply knowable here. Every
(matrix, fraction) pair is listed; create_slice_reports.py's calibration
probe then finds the empty ones and refuses to build them, at the cost of
one throwaway report run each.

SUPERSEDES plan_report_slices.py, which cut slices by date window through
the report's `where=` query parameter. Report runs ignore `where=` (the
report runs unfiltered and 500s at ~230 s), so those plans never described
what a slice would fetch.
"""

import argparse
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from nightly_slices import Api, read_env  # noqa: E402
from slice_store import cell_key  # noqa: E402

# Fraction codes counted on DT_TEST. N is "not applicable" (most solids),
# T total and D dissolved. A code outside these is reported by --all-fractions.
FRACTIONS = ("N", "T", "D")


def fail(message):
    sys.stderr.write("\n%s\n\n" % message)
    raise SystemExit(2)


def odata_count(api, entity, where):
    """COUNT via $count=true and nothing else.

    `$apply=aggregate(...)` is SILENTLY IGNORED by this endpoint - it returns
    1000 ordinary rows with HTTP 200, which reads as an aggregate if you are
    not looking. `$count=true` with `$top` is a 400.
    """
    status, body = api.json("/api/odata/%s?$count=true&$filter=%s"
                            % (entity, where))
    if status == 200 and isinstance(body, int):
        return body
    return None


def samples(api, facility_id):
    """Page DT_SAMPLE once: {matrix: {"samples", "locations", "types", dates}}."""
    out = collections.defaultdict(lambda: {"samples": 0, "locations": set(),
                                           "types": collections.Counter(),
                                           "first": None, "last": None})
    path = ("/api/odata/DT_SAMPLE?$filter=FACILITY_ID eq %d&$orderby=SAMPLE_ID"
            % facility_id)
    raw, pages = True, 0
    while path:
        status, body = api.json(path, quote=raw)
        raw = False
        if status != 200 or not isinstance(body, dict):
            fail("DT_SAMPLE page %d failed: %s" % (pages, str(body)[:200]))
        for row in body.get("value") or []:
            entry = out[(row.get("MATRIX_CODE") or "").strip().upper()]
            entry["samples"] += 1
            code = (row.get("SYS_LOC_CODE") or "").strip()
            if code:
                entry["locations"].add(code)
            entry["types"][(row.get("SAMPLE_TYPE_CODE") or "").strip()] += 1
            when = (row.get("SAMPLE_DATE") or "")[:10]
            if when:
                entry["first"] = min(entry["first"] or when, when)
                entry["last"] = max(entry["last"] or when, when)
        pages += 1
        if pages % 25 == 0:
            print("   ...%d pages" % pages)
            sys.stdout.flush()
        nxt = body.get("@odata.nextLink")
        path = ("/api/odata/" + nxt.split("/api/odata/", 1)[1]
                if nxt and "/api/odata/" in nxt else None)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--facility", type=int, required=True)
    parser.add_argument("--json", default=None, help="also write the survey")
    args = parser.parse_args()

    site, key = read_env()
    api = Api(site, key)
    print("site    : %s\nfacility: %d\n" % (site, args.facility))

    results = odata_count(api, "DT_RESULT", "FACILITY_ID eq %d" % args.facility)
    if results is None:
        fail("Could not count results for facility %d. Check the id and that "
             "the key can read it." % args.facility)
    print("results : %s" % "{:,}".format(results))

    print("reading samples ...")
    by_matrix = samples(api, args.facility)
    fractions = {}
    uncounted = []
    for code in FRACTIONS:
        count = odata_count(api, "DT_TEST", "FACILITY_ID eq %d and FRACTION eq "
                            "'%s'" % (args.facility, code))
        if count is None:
            # A failed count is NOT zero: treating it as absent would drop a
            # fraction from every plan without a word.
            uncounted.append(code)
        elif count:
            fractions[code] = count

    print("\n%-8s %9s %9s  %-23s %s" % ("MATRIX", "SAMPLES", "LOCATIONS",
                                       "SAMPLED", "SAMPLE TYPES"))
    for matrix, entry in sorted(by_matrix.items(),
                                key=lambda kv: -kv[1]["samples"]):
        types = ", ".join("%s %d" % (t or "-", n)
                          for t, n in entry["types"].most_common(4))
        print("%-8s %9s %9d  %s..%s  %s"
              % (matrix or "(none)", "{:,}".format(entry["samples"]),
                 len(entry["locations"]), entry["first"], entry["last"],
                 types))
    print("\nfractions (tests): %s"
          % ", ".join("%s %s" % (c, "{:,}".format(n))
                      for c, n in sorted(fractions.items(),
                                         key=lambda kv: -kv[1])))
    if uncounted:
        print("WARNING: could not count fraction(s) %s - re-run before "
              "trusting the cell list." % ", ".join(uncounted))
    if "" in by_matrix:
        print("WARNING: %d sample(s) have no matrix code. No cell can reach "
              "them; they will be in no slice." % by_matrix[""]["samples"])

    cells = [(m, f) for m in sorted(by_matrix) if m for f in sorted(fractions)]
    print("\n%d candidate cell(s). Dry-run each; the probe refuses empty ones:"
          % len(cells))
    for matrix, fraction in cells:
        print("   python scripts/create_slice_reports.py --facility %d "
              "--root data/<name> --template <report id> --cell %s --dry-run"
              % (args.facility, cell_key(matrix, fraction)))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"facility_id": args.facility, "results": results,
                       "fractions": fractions, "uncounted": uncounted,
                       "matrices": {m: {"samples": e["samples"],
                                        "locations": len(e["locations"]),
                                        "first": e["first"], "last": e["last"],
                                        "sample_types": dict(e["types"])}
                                    for m, e in by_matrix.items()},
                       "cells": [cell_key(m, f) for m, f in cells]},
                      handle, indent=1, sort_keys=True)
        print("\nwrote %s" % args.json)
    return 1 if uncounted else 0


if __name__ == "__main__":
    raise SystemExit(main())
