r"""Convert result units locally, until EQuIS does it at the source.

A STOPGAP, AND IT SAYS SO. @reporting_unit is the right answer and does
this in the database, where the conversion table lives. It cannot be used
here yet: RT_UNIT_CONVERSION_FACTOR has no row for `percent` at all, so a
report asked for mg/kg dies on the first percent-unit result with
"MISSING UNIT CONVERSION FACTOR - cannot convert 'percent' to 'mg/kg'" and
returns 78 bytes instead of its rows. Measured 2026-09-24 on
a dissolved slice on a solid matrix. Once the tenant's factors are complete, set
@reporting_unit per slice and delete this.

WHY IT IS STILL WORTH DOING. Mixed units are a wrong answer, not a
presentation detail: Arsenic in WS is 68,371 rows of ug/L and 22,121 of
mg/L, and a maximum across them ranks by UNIT - 0.57 mg/L is 570 ug/L and
loses to 168 ug/L. A reader that refuses to aggregate across units cannot
answer for those analytes at all. Converting at collect time makes them
answerable.

THE FACTORS COME FROM EQuIS, NOT FROM HERE. RT_UNIT_CONVERSION_FACTOR is
read over OData and used as given. This file knows how to APPLY a factor
and nothing about chemistry: inventing a number here would be this
code quietly disagreeing with the database, which is the failure the
whole unit exercise is about. A pair with no factor is LEFT ALONE, never
guessed at.

WHAT IT REFUSES. Anything needing more than a multiplication. mg/kg to
mg/L needs a density and mass-per-weight to mass-per-volume is absent from
the table in both directions, so those rows keep their unit and a
reader's unit guard keeps protecting them.

WHAT IT RECORDS. Every conversion is counted per (from, to) and returned,
so the collect can say what it changed rather than changing it silently.
The ORIGINAL unit and value are kept in RESULT_UNIT_ORIGINAL and
RESULT_VALUE_ORIGINAL: a converted number that cannot be traced back to
what the lab reported is not an audit trail, and someone will ask.
"""

import json
import urllib.request

# Which unit each cell is converted TO is not decided here. It is a
# per-facility choice kept in <root>/units.json (slice_store.load_units),
# proposed from the data and editable by a person. This module used to hold
# a table of one facility's matrix codes, and at any other facility it
# converted nothing without saying so.
#
# Two lessons that table encoded, which the proposals must respect:
#  - Fraction matters as much as matrix. A DISSOLVED fraction on a solid is
#    a LEACHATE, liquid chemistry run on soil or sediment, measured in
#    mg/L. Setting a whole solid matrix to mg/kg killed its dissolved slice.
#  - Water reads better in ug/L: trace metals in mg/L are a row of zeros at
#    map precision. The dominant unit is only a proposal; change it in
#    units.json when the map needs a different one.

# Where the converted value comes from and where the original is kept.
VALUE_COLUMNS = ("RESULT_NUMERIC", "REPORT_RESULT_VALUE",
                 "REPORT_RESULT_LIMIT", "REPORT_METHOD_DETECTION_LIMIT",
                 "REPORT_REPORTING_LIMIT", "QUANTITATION_LIMIT",
                 "METHOD_DETECTION_LIMIT", "REPORTING_DETECTION_LIMIT",
                 "REPORT_QUANTITATION_LIMIT")
UNIT_COLUMNS = ("RESULT_UNIT", "REPORT_RESULT_UNIT", "DETECTION_LIMIT_UNIT")


def target_for(matrix, fraction, units):
    """The unit a slice should end up in, or None to leave it alone.

    `units` is slice_store.load_units(root). A cell it does not name is not
    converted.
    """
    key = "%s/%s" % ((matrix or "ALL").upper(), (fraction or "ALL").upper())
    return (units or {}).get(key)


def unit_counts(rows):
    """{unit: rows} over CSV dict rows, using the same ladder as the convert.

    For proposing a cell's target from a probe: the first non-empty of the
    unit columns, because a non-detect carries no RESULT_UNIT and only the
    limit's unit one column over. Reading RESULT_UNIT alone would count every
    non-detect as unitless.
    """
    counts = {}
    for row in rows:
        unit = ""
        for column in UNIT_COLUMNS:
            unit = (row.get(column) or "").strip()
            if unit:
                break
        counts[unit] = counts.get(unit, 0) + 1
    return counts


def propose_target(counts):
    """(unit, share) for the dominant real unit in `counts`, or (None, 0).

    Unitless rows and dimensionless units (pH SU, percent, ratios) are not
    candidates: a cell whose plurality is pH is still a concentration cell
    for everything else, and converting toward `SU` converts nothing.
    """
    skip = {"", "SU", "PH", "NONE", "%", "PERCENT", "PPTH", "MV", "UNITS"}
    candidates = {u: n for u, n in counts.items() if u.upper() not in skip}
    if not candidates:
        return None, 0.0
    unit = max(sorted(candidates), key=lambda u: candidates[u])
    total = float(sum(counts.values())) or 1.0
    return unit, candidates[unit] / total


def load_factors(site, api_key, timeout=240):
    """{(reported, target): factor} straight from EQuIS.

    Read rather than hardcoded so this file stays correct when the tenant's
    table is completed - the missing `percent` rows are the whole reason
    this stopgap exists, and it should start converting them the day they
    appear without anyone editing Python.
    """
    request = urllib.request.Request(
        site.rstrip("/") + "/api/odata/RT_UNIT_CONVERSION_FACTOR")
    request.add_header("Authorization", "Bearer " + api_key)
    request.add_header("Accept", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8-sig"))
    factors = {}
    for row in body.get("value") or []:
        reported = (row.get("REPORTED_UNIT") or "").strip()
        target = (row.get("TARGET_UNIT") or "").strip()
        factor = row.get("CONVERSION_FACTOR")
        # A FORMULA means the conversion is not a multiplication - a delta
        # or a polynomial. Applying the factor alone would be wrong in a way
        # nothing downstream could detect, so those are skipped.
        if not reported or not target or factor is None:
            continue
        if row.get("FORMULA"):
            continue
        if (row.get("STATUS_FLAG") or "A").upper() != "A":
            continue
        try:
            factors[(reported, target)] = float(factor)
        except (TypeError, ValueError):
            continue
    return factors


def convert_table(table, target, factors, keep_original=True):
    """Convert `table` to `target` in place. Returns (table, counts).

    `counts` is {(from_unit, to_unit): rows}, so the caller can report what
    changed. A row whose unit already IS the target is not counted: nothing
    happened to it.
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    names = set(table.column_names)
    present = [c for c in UNIT_COLUMNS if c in names]
    if not present or not target:
        return table, {}

    # COALESCED ACROSS THE UNIT COLUMNS, first non-empty wins. A non-detect
    # reports no result and no RESULT_UNIT, only the limit and ITS unit in
    # REPORT_RESULT_UNIT - the same ladder rd.COLUMNS describes and the
    # store applies. Reading only the first column left 630,021 WS rows
    # looking unitless while carrying mg/L one column over, so they were
    # skipped and the facility came out MORE mixed than before: 237 mixed
    # (chemical, matrix) pairs became 292. Caught by checking the result
    # rather than trusting the "ok" the converter printed.
    columns = [table.column(c).to_pylist() for c in present]
    units = []
    for index in range(table.num_rows):
        value = ""
        for column in columns:
            candidate = (column[index] or "").strip()
            if candidate:
                value = candidate
                break
        units.append(value)
    # Which rows move, and by how much. Built once as a plain list because
    # the factor depends on the row's own unit; pyarrow has no elementwise
    # dictionary lookup, and the alternative is one mask per unit, which is
    # slower at the handful of units a slice actually holds.
    multipliers = []
    changed = []
    counts = {}
    for unit in units:
        clean = (unit or "").strip()
        if not clean or clean == target:
            multipliers.append(1.0)
            changed.append(False)
            continue
        factor = factors.get((clean, target))
        if factor is None:
            multipliers.append(1.0)
            changed.append(False)
            continue
        multipliers.append(factor)
        # TRACKED SEPARATELY, not inferred from the multiplier. ug/g to
        # mg/kg is a factor of exactly 1.0 - the same number, renamed - so a
        # `scale != 1.0` mask reads it as "did not convert" and leaves the
        # label saying ug/g. 1,588 Pristane rows stayed mixed that way,
        # which is a mixed layer over a difference that is only spelling.
        changed.append(True)
        key = (clean, target)
        counts[key] = counts.get(key, 0) + 1

    # NOTE: no early return when `counts` is empty. A batch where nothing
    # converted must still gain the audit columns, or its schema differs
    # from the batch that did and the parquet writer refuses the file
    # ("Table schema does not match schema used to create file"). Found by
    # collecting a real 47 MB slice, where the first batch had mg/L rows
    # and a later one did not.
    scale = pa.array(multipliers, type=pa.float64())
    moved = pa.array(changed, type=pa.bool_())

    if keep_original:
        # The lab's own numbers, kept beside the converted ones. A value
        # that cannot be traced back to what was reported is not an audit
        # trail, and on environmental data someone always asks.
        if "RESULT_UNIT_ORIGINAL" not in names:
            table = table.append_column(
                "RESULT_UNIT_ORIGINAL", pa.array(units, type=pa.string()))
        first_value = next((c for c in VALUE_COLUMNS if c in names), None)
        if first_value and "RESULT_VALUE_ORIGINAL" not in names:
            table = table.append_column(
                "RESULT_VALUE_ORIGINAL",
                pc.cast(table.column(first_value), pa.float64()))

    for column in VALUE_COLUMNS:
        if column not in table.column_names:
            continue
        values = pc.cast(table.column(column), pa.float64())
        table = table.set_column(
            table.column_names.index(column), column,
            pc.multiply(values, scale))

    # Every unit column that was the source unit now reads as the target.
    for column in UNIT_COLUMNS:
        if column not in table.column_names:
            continue
        current = table.column(column)
        table = table.set_column(
            table.column_names.index(column), column,
            pc.if_else(moved, pa.scalar(target), current))

    return table, counts


def describe(counts):
    """One line naming what was converted, for the collect's output."""
    if not counts:
        return ""
    parts = ["%s->%s x%s" % (a, b, format(n, ",d"))
             for (a, b), n in sorted(counts.items(), key=lambda kv: -kv[1])]
    return ", ".join(parts)
