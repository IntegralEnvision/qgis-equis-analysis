"""What crosses the wire to the web view.

No QGIS, no Qt: bridge_payload.py is deliberately the half of the bridge that
decides serialization, so it can be checked under a plain interpreter.

    python tools/test_bridge_payload.py
"""
import base64
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from equis_loader import bridge_payload as bp  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


try:
    import duckdb
except ImportError as exc:
    print("duckdb is not importable: %s" % exc)
    print("SKIPPED")
    raise SystemExit(0)


def fixture():
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE results AS "
        "SELECT i AS row_id, 'MW-' || (i % 5) AS location, "
        "       CASE WHEN i % 3 = 0 THEN 'Arsenic' ELSE 'Lead' END AS chemical, "
        "       i * 1.5 AS value, "
        "       CAST('2020-01-01' AS TIMESTAMP) + INTERVAL (i) DAY AS sample_date, "
        "       CASE WHEN i % 4 = 0 THEN NULL ELSE 'Y' END AS detect "
        "FROM range(200) tbl(i)")
    return con


con = fixture()

# --- exec -------------------------------------------------------------------
out = bp.answer(con, "CREATE TEMP TABLE t AS SELECT 1", kind=bp.KIND_EXEC)
check("exec returns an empty string", out == "", repr(out))

# --- json -------------------------------------------------------------------
raw = bp.answer(con, "SELECT * FROM results ORDER BY row_id LIMIT 3",
                kind=bp.KIND_JSON)
rows = json.loads(raw)
check("json returns a list of objects",
      isinstance(rows, list) and len(rows) == 3 and isinstance(rows[0], dict))
check("json keeps the column names",
      set(rows[0]) == {"row_id", "location", "chemical", "value",
                       "sample_date", "detect"},
      str(sorted(rows[0])))
check("json renders a timestamp rather than raising",
      isinstance(rows[0]["sample_date"], str)
      and rows[0]["sample_date"].startswith("2020-01-01"),
      str(rows[0]["sample_date"]))
check("json preserves NULL as None", rows[0]["detect"] is None,
      repr(rows[0]["detect"]))

# --- arrow ------------------------------------------------------------------
payload = bp.answer(con, "SELECT * FROM results ORDER BY row_id")
check("arrow returns base64 text",
      isinstance(payload, str) and payload == payload.strip())
blob = base64.b64decode(payload)
check("arrow payload is an IPC stream", blob[:4] != b"", "%d bytes" % len(blob))

import arro3.core  # noqa: E402
import arro3.io  # noqa: E402

table = arro3.core.Table.from_arrow(
    arro3.io.read_ipc_stream(io.BytesIO(blob)))
check("arrow round-trips every row", table.num_rows == 200,
      str(table.num_rows))
check("arrow round-trips every column",
      [f.name for f in table.schema] == ["row_id", "location", "chemical",
                                         "value", "sample_date", "detect"],
      str([f.name for f in table.schema]))

# Types are the reason Arrow is worth a decode step: JSON would have turned
# the timestamp into text and the value into a double that looks the same but
# is not typed.
types = {f.name: str(f.type) for f in table.schema}
check("arrow keeps the timestamp typed",
      "time" in types["sample_date"].lower(), types["sample_date"])
# Numeric, but not necessarily a float: DuckDB types `i * 1.5` as
# Decimal128. That is the right answer - a decimal is exact where a double
# is not - and the point of Arrow here is that the distinction survives the
# trip at all, which JSON would have flattened to a bare number.
numeric = ("float", "double", "decimal", "int")
check("arrow keeps the value numeric",
      any(word in types["value"].lower() for word in numeric),
      types["value"])

# --- BOTH Arrow runtimes, forced ------------------------------------------
# The writer list is ordered: pyarrow first because QGIS already ships it,
# arro3 second for a build that has duckdb without it. Testing only whichever
# this machine happens to have would leave the other path unexercised until
# it failed on someone else's install.
def ipc_via(writer_name):
    writer = getattr(bp, writer_name)
    original = bp.IPC_WRITERS
    bp.IPC_WRITERS = (writer,)
    try:
        return bp.answer(con, "SELECT * FROM results ORDER BY row_id")
    finally:
        bp.IPC_WRITERS = original


for writer_name, package in (("_ipc_with_pyarrow", "pyarrow"),
                             ("_ipc_with_arro3", "arro3.core")):
    try:
        __import__(package)
    except ImportError:
        print("[SKIP] %s is not installed here" % package)
        continue
    forced = ipc_via(writer_name)
    decoded = arro3.core.Table.from_arrow(
        arro3.io.read_ipc_stream(io.BytesIO(base64.b64decode(forced))))
    check("%s writes a readable IPC stream" % package,
          decoded.num_rows == 200 and len(decoded.schema) == 6,
          "%d rows, %d cols" % (decoded.num_rows, len(decoded.schema)))
    names = [f.name for f in decoded.schema]
    check("%s preserves the column order" % package,
          names == ["row_id", "location", "chemical", "value",
                    "sample_date", "detect"], str(names))

# With no runtime at all the failure must be explicit, not a silent empty
# chart - this is the one case a user on an unusual build would hit.
original = bp.IPC_WRITERS
bp.IPC_WRITERS = (lambda relation: None,)
try:
    bp.answer(con, "SELECT 1")
    raised = False
except bp.PayloadError as exc:
    raised = "arrow runtime" in str(exc).lower()
finally:
    bp.IPC_WRITERS = original
check("no Arrow runtime raises a message naming the fix", raised)

# --- failures are raised, never returned as emptiness ------------------------
try:
    bp.answer(con, "SELECT * FROM no_such_table")
    raised = False
except bp.PayloadError as exc:
    raised = "no_such_table" in str(exc) or "not" in str(exc).lower()
check("a bad query raises with a usable message", raised)

try:
    bp.answer(con, "SELECT 1", kind="nonsense")
    raised = False
except bp.PayloadError:
    raised = True
check("an unknown result kind raises", raised)

# An empty result is NOT an error, and must be distinguishable from one.
empty = bp.answer(con, "SELECT * FROM results WHERE 1 = 0",
                  kind=bp.KIND_JSON)
check("an empty result is an empty list, not an error", empty == "[]", empty)
empty_arrow = bp.answer(con, "SELECT * FROM results WHERE 1 = 0")
table = arro3.core.Table.from_arrow(
    arro3.io.read_ipc_stream(io.BytesIO(base64.b64decode(empty_arrow))))
check("an empty arrow result keeps its schema",
      table.num_rows == 0 and len(table.schema) == 6,
      "%d rows, %d cols" % (table.num_rows, len(table.schema)))

# --- a table made on ANOTHER connection is seen ----------------------------
# The Analysis worker makes its tables on its own cursor and the page reads
# them through this connection. A result left pending here (a fetchone() on
# many rows) holds an old snapshot in which they do not exist; found
# 2026-10-11 as "Table with name pp_3 does not exist!".
con.execute("SELECT * FROM results").fetchone()
con.cursor().execute("CREATE TABLE made_elsewhere AS SELECT 42 AS answer")
try:
    seen = bp.answer(con, "SELECT answer FROM made_elsewhere", kind=bp.KIND_JSON)
except bp.PayloadError as exc:
    seen = "raised: %s" % str(exc).splitlines()[0]
check("a table another connection made is seen despite a pending result",
      seen == '[{"answer": 42}]', seen)

# --- size, which is the argument for Arrow at all ---------------------------
as_json = bp.answer(con, "SELECT * FROM results", kind=bp.KIND_JSON)
as_arrow = bp.answer(con, "SELECT * FROM results")
check("arrow is smaller than json at 200 rows",
      len(as_arrow) < len(as_json),
      "arrow %d chars vs json %d" % (len(as_arrow), len(as_json)))

print()
if FAILURES:
    print("FAILED: %s" % ", ".join(FAILURES))
    raise SystemExit(1)
print("Bridge payloads serialize as the page expects.")
