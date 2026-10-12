"""Turning a DuckDB result into what the web view expects.

Split out of the Qt bridge on purpose: this is the half that decides what
crosses the wire, and keeping it free of QGIS and Qt means the serialization
rules can be tested under a plain interpreter, the way store.py and cache.py
are. The bridge is then a thin QObject that calls in here.

WHICH ARROW RUNTIME. DuckDB has no Arrow library of its own - `rel.arrow()`
imports pyarrow, verified 2026-09-23 by watching `sys.modules` flip. So the
question is only which implementation does the encoding, and the answer is
"whichever is already here":

  * QGIS 4.2.1 ships **pyarrow 25.0.0, numpy and duckdb** in its own
    site-packages. On a stock install nothing needs vendoring at all.
  * `arro3` (37 MB) is the fallback for a build that has duckdb but no
    pyarrow. It reads DuckDB's C Data Interface through
    `arro3.core.Table.from_arrow(relation)` and never imports pyarrow.

phase0's finding 1 said to avoid `.arrow()` because it drags in ~40 MB plus
numpy. That reasoning assumed pyarrow was absent; on the QGIS builds measured
so far it is already loaded, and vendoring arro3 would add a SECOND Arrow
runtime beside it. The preference is therefore inverted here, deliberately,
and the finding still holds anywhere pyarrow is genuinely missing.

Both paths are exercised by tools/test_bridge_payload.py, which forces each
in turn rather than testing whichever the machine happens to have.

WHY BASE64. QWebChannel carries JSON-compatible values, not binary. Arrow
IPC is bytes, so it is base64'd for the trip and decoded in the page. That
costs 33% on the wire and is still far cheaper than JSON for anything
numeric - 1,000 rows x 4 columns is 21 KB of IPC against 28 KB of base64,
where the same rows as JSON objects run several times that and lose their
types on the way.
"""

import base64
import io
import json

# Keep the JSON path for small results and for anything a human will read in
# a debugger. Arrow is worth its decode step at scale, not at ten rows.
KIND_ARROW = "arrow"
KIND_JSON = "json"
KIND_EXEC = "exec"


class PayloadError(RuntimeError):
    """A query failed, or its result could not be serialized."""


def _ipc_with_pyarrow(relation):
    """Arrow IPC via pyarrow, which QGIS already ships. None if unavailable."""
    try:
        import pyarrow as pa
    except ImportError:
        return None
    # to_arrow_table(), not arrow(): the latter is deprecated in duckdb 1.5
    # and returns a RecordBatchReader on some builds, which new_stream will
    # not take.
    try:
        table = relation.to_arrow_table()
    except AttributeError:          # older duckdb
        table = relation.arrow()
        if not isinstance(table, pa.Table):
            table = pa.Table.from_batches(list(table), table.schema)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _ipc_with_arro3(relation):
    """Arrow IPC via arro3, for a build with duckdb but no pyarrow."""
    try:
        import arro3.core
        import arro3.io
    except ImportError:
        return None
    table = arro3.core.Table.from_arrow(relation)
    sink = io.BytesIO()
    arro3.io.write_ipc_stream(table, sink)
    return sink.getvalue()


# Order matters: prefer the runtime that is already in the process. Exposed
# as a module attribute so the test suite can force each path rather than
# only exercising whichever this machine happens to have.
IPC_WRITERS = (_ipc_with_pyarrow, _ipc_with_arro3)


def to_arrow_ipc(relation):
    """Arrow IPC stream bytes for a DuckDB relation."""
    for writer in IPC_WRITERS:
        payload = writer(relation)
        if payload is not None:
            return payload
    raise PayloadError(
        "No Arrow runtime available: install pyarrow (QGIS 4.2 ships it) or "
        "arro3-core and arro3-io.")


def to_json_rows(relation):
    """A relation as a list of dicts, with values coerced to JSON types.

    `default=str` on anything json cannot express: timestamps and decimals
    become their text form rather than raising. A chart reading the JSON path
    is reading it for convenience, and a readable date beats an exception.
    """
    columns = [description[0] for description in relation.description]
    rows = relation.fetchall()
    return [dict(zip(columns, row)) for row in rows]


def _fresh_snapshot(connection):
    """End any result left pending on `connection`, so it sees new tables.

    A result left unfetched or partly fetched (`execute(...).fetchone()`
    on many rows) holds the connection's transaction open, and with it a
    snapshot of the catalogue from before. Tables made since by ANOTHER
    connection are then invisible: "Table with name pp_3 does not exist!
    Did you mean memory.pp_3?" - the Analysis worker's pair table, made on
    its own cursor, refused to the page (found 2026-10-11). `.sql()` does
    not end a pending result; `execute()` does, and `commit()` does not
    help. Costs 0.2-0.5 ms a question.
    """
    connection.execute("SELECT 1").fetchall()


def answer(connection, sql, kind=KIND_ARROW):
    """Run `sql` and return what the page asked for.

    Returns a str in every case, because that is what a QWebChannel slot can
    carry:

      exec   ""                      the statement ran, nothing to send back
      json   a JSON array of objects
      arrow  base64 of an Arrow IPC stream

    Errors are RAISED, not returned as an empty result. A page that cannot
    tell "no rows" from "your SQL was wrong" will draw an empty chart and
    call it data.
    """
    if kind == KIND_EXEC:
        connection.execute(sql)
        return ""
    _fresh_snapshot(connection)
    try:
        relation = connection.sql(sql)
    except Exception as exc:  # noqa: BLE001 - the page needs the message
        raise PayloadError(str(exc))
    if relation is None:
        # A statement with no result set reached the query path.
        return ""
    if kind == KIND_JSON:
        return json.dumps(to_json_rows(relation), default=str)
    if kind != KIND_ARROW:
        raise PayloadError("unknown result kind %r" % (kind,))
    return base64.b64encode(to_arrow_ipc(relation)).decode("ascii")
