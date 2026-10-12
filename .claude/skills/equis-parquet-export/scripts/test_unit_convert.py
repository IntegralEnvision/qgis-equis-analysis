r"""Unit conversion: the factors are EQuIS's, the arithmetic is ours.

    python scripts/test_unit_convert.py

No network and no GUI: the factor table is passed in, which is the whole
point - this file must not know any chemistry of its own.
"""
import os
import sys

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
except ImportError:
    print("needs pyarrow")
    print("SKIPPED")
    raise SystemExit(0)

from unit_convert import (convert_table, describe, propose_target,  # noqa: E402
                          target_for, unit_counts)

# Exactly the rows EQuIS actually has, and deliberately NO percent row -
# that gap is why this stopgap exists.
FACTORS = {("mg/L", "ug/L"): 1000.0, ("ng/L", "ug/L"): 0.001,
           ("ug/g", "mg/kg"): 1.0, ("ug/kg", "mg/kg"): 0.001}


def table(units, values):
    return pa.table({"RESULT_UNIT": pa.array(units, pa.string()),
                     "RESULT_NUMERIC": pa.array(values, pa.float64()),
                     "SYS_LOC_CODE": pa.array(["MW-1"] * len(units))})


# --- the arithmetic ----------------------------------------------------------
out, counts = convert_table(table(["mg/L", "ug/L", "ng/L"],
                                  [0.19, 5.0, 2000.0]), "ug/L", FACTORS)
values = out.column("RESULT_NUMERIC").to_pylist()
check("mg/L is multiplied by 1000", values[0] == 190.0, str(values[0]))
check("a row already in the target is untouched", values[1] == 5.0,
      str(values[1]))
check("ng/L is divided by 1000", values[2] == 2.0, str(values[2]))
check("every row now reads as the target",
      set(out.column("RESULT_UNIT").to_pylist()) == {"ug/L"},
      str(set(out.column("RESULT_UNIT").to_pylist())))
check("only the rows that MOVED are counted",
      counts == {("mg/L", "ug/L"): 1, ("ng/L", "ug/L"): 1}, str(counts))

# --- the non-detect ladder ---------------------------------------------------
# A non-detect reports no result and no RESULT_UNIT, only the limit and ITS
# unit in REPORT_RESULT_UNIT. Reading only the first column left 630,021 WS
# rows looking unitless, so they were skipped and the facility came out MORE
# mixed after converting than before - 237 pairs became 292.
nd = pa.table({
    "RESULT_UNIT": pa.array([None, "mg/L"], pa.string()),
    "REPORT_RESULT_UNIT": pa.array(["mg/L", "mg/L"], pa.string()),
    "RESULT_NUMERIC": pa.array([0.5, 0.19], pa.float64()),
})
out, counts = convert_table(nd, "ug/L", FACTORS)
check("a unit found only in REPORT_RESULT_UNIT still converts",
      out.column("RESULT_NUMERIC").to_pylist() == [500.0, 190.0],
      str(out.column("RESULT_NUMERIC").to_pylist()))
check("and both rows are counted", counts == {("mg/L", "ug/L"): 2},
      str(counts))
check("the fallback column is rewritten too",
      out.column("REPORT_RESULT_UNIT").to_pylist() == ["ug/L", "ug/L"],
      str(out.column("REPORT_RESULT_UNIT").to_pylist()))

# A factor of exactly 1.0 is still a conversion: ug/g and mg/kg are the same
# number with different spelling. Inferring "moved" from the multiplier left
# 1,588 Pristane rows labelled ug/g beside mg/kg ones - a mixed layer over
# nothing but a name.
same, counts = convert_table(table(["ug/g", "mg/kg"], [4.0, 9.0]),
                             "mg/kg", FACTORS)
check("a factor of 1.0 still relabels the unit",
      same.column("RESULT_UNIT").to_pylist() == ["mg/kg", "mg/kg"],
      str(same.column("RESULT_UNIT").to_pylist()))
check("the value is unchanged by a factor of 1.0",
      same.column("RESULT_NUMERIC").to_pylist() == [4.0, 9.0])
check("and it is reported as a conversion",
      counts == {("ug/g", "mg/kg"): 1}, str(counts))

# --- what it refuses ---------------------------------------------------------
# percent has no factor in this tenant. Guessing one here would be this
# repository disagreeing with the database, which is the whole failure.
out, counts = convert_table(table(["percent", "mg/kg"], [5.0, 400.0]),
                            "mg/kg", FACTORS)
check("a unit with no factor is left alone, not guessed at",
      out.column("RESULT_NUMERIC").to_pylist() == [5.0, 400.0],
      str(out.column("RESULT_NUMERIC").to_pylist()))
check("and keeps its own unit",
      out.column("RESULT_UNIT").to_pylist() == ["percent", "mg/kg"],
      str(out.column("RESULT_UNIT").to_pylist()))
check("and is not reported as converted", counts == {}, str(counts))

# mass-per-weight to mass-per-volume needs a density; the table has no such
# row in either direction, so it must not happen by accident.
out, counts = convert_table(table(["mg/kg"], [400.0]), "mg/L", FACTORS)
check("mg/kg is never silently turned into mg/L",
      out.column("RESULT_NUMERIC").to_pylist() == [400.0] and not counts)

# --- the audit trail ---------------------------------------------------------
out, _ = convert_table(table(["mg/L"], [0.19]), "ug/L", FACTORS)
check("the lab's own unit is kept",
      out.column("RESULT_UNIT_ORIGINAL").to_pylist() == ["mg/L"])
check("the lab's own value is kept",
      out.column("RESULT_VALUE_ORIGINAL").to_pylist() == [0.19])

# A batch where NOTHING converts must still carry the audit columns, or its
# schema differs from one that did and the parquet writer refuses the file.
# Found by collecting a real 47 MB slice.
quiet, counts = convert_table(table(["ug/L"], [5.0]), "ug/L", FACTORS)
check("a batch that converts nothing has the same schema as one that does",
      "RESULT_UNIT_ORIGINAL" in quiet.column_names
      and "RESULT_VALUE_ORIGINAL" in quiet.column_names,
      str(quiet.column_names))
check("and reports no conversions", counts == {}, str(counts))

# --- no target means no change -----------------------------------------------
same, counts = convert_table(table(["mg/L"], [0.19]), None, FACTORS)
check("without a target nothing is touched",
      same.column("RESULT_NUMERIC").to_pylist() == [0.19] and not counts)
check("and no audit columns are added",
      "RESULT_UNIT_ORIGINAL" not in same.column_names)

# --- the slice targets -------------------------------------------------------
# Targets come from a facility's units.json, keyed by cell. Nothing about any
# matrix code is known to the code itself.
UNITS = {"WS/N": "ug/L", "SE/N": "mg/kg", "SE/D": "mg/L"}
check("a cell's target is read from the facility's choices",
      target_for("ws", "n", UNITS) == "ug/L")
# A DISSOLVED fraction on a solid is a leachate - liquid chemistry on
# a solid - and is mg/L. Setting SE to mg/kg wholesale killed that
# slice outright, which is why the key is the cell and not the matrix.
check("a leachate keeps its own target", target_for("SE", "D", UNITS)
      == "mg/L", str(target_for("SE", "D", UNITS)))
check("a cell with no entry is left alone",
      target_for("SE", "T", UNITS) is None)
check("no units file means nothing converts",
      target_for("WS", "N", {}) is None)

# --- proposing a target from a probe ------------------------------------------
# A non-detect has no RESULT_UNIT; its unit is one column over. Counting
# RESULT_UNIT alone would call every non-detect unitless.
probe = [{"RESULT_UNIT": "mg/kg"}, {"RESULT_UNIT": "",
                                    "REPORT_RESULT_UNIT": "mg/kg"},
         {"RESULT_UNIT": "percent"}, {"RESULT_UNIT": "percent"},
         {"RESULT_UNIT": "percent"}, {"RESULT_UNIT": "SU"}]
seen = unit_counts(probe)
check("unit_counts follows the unit-column ladder",
      seen == {"mg/kg": 2, "percent": 3, "SU": 1}, str(seen))
unit, share = propose_target(seen)
# percent is the plurality here, as it can be for bulk solids, but it is
# not a concentration to convert TOWARD - the stopgap exists because
# percent -> mg/kg is the missing factor.
check("a dimensionless plurality is not proposed",
      unit == "mg/kg" and abs(share - 2 / 6.0) < 1e-9, "%r %r" % (unit, share))
check("a cell with only pH proposes nothing",
      propose_target({"SU": 9}) == (None, 0.0))

check("describe names what moved",
      describe({("mg/L", "ug/L"): 18397}) == "mg/L->ug/L x18,397",
      describe({("mg/L", "ug/L"): 18397}))

print()
if FAILURES:
    print("FAILED: %s" % ", ".join(FAILURES))
    raise SystemExit(1)
print("Units convert by EQuIS's factors, and nothing else moves.")
