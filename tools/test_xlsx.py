"""Reading .xlsx report output: cell types, sparse rows, and column alignment.

Pure Python - no QGIS and no network:

  python tools\\test_xlsx.py

EIA output arrives as a workbook rather than CSV, and workbooks differ from
CSV in two ways that quietly corrupt data if mishandled. Empty cells are
omitted from the XML entirely, so a row after a blank cell shifts a column
left unless the gaps are filled from each cell reference; and values arrive
typed, so a location code of 1200 becomes "1200.0" and a sample date becomes
a five-digit serial unless they are rendered back deliberately.
"""

import datetime
import io
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from equis_loader.equis_api import (  # noqa: E402
    EquisClient,
    EquisError,
    _cell_text,
    _xlsx_column,
    _xlsx_serial_to_text,
)

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def build_xlsx(rows, date_columns=(), shared=None):
    """A minimal workbook. `rows` is a list of lists of (ref, type, value)."""
    shared = shared or []
    parts = ['<?xml version="1.0"?><worksheet xmlns="%s"><sheetData>' % NS]
    for index, cells in enumerate(rows, start=1):
        parts.append('<row r="%d">' % index)
        for ref, kind, value in cells:
            style = ' s="1"' if ref[0] in date_columns and index > 1 else ""
            attr = ' t="%s"' % kind if kind else ""
            parts.append('<c r="%s%d"%s%s><v>%s</v></c>'
                         % (ref, index, style, attr, value))
        parts.append("</row>")
    parts.append("</sheetData></worksheet>")

    strings = "".join("<si><t>%s</t></si>" % s for s in shared)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("xl/worksheets/sheet1.xml", "".join(parts))
        archive.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0"?><sst xmlns="%s">%s</sst>' % (NS, strings),
        )
        # Style 1 carries a date format (numFmtId 14); style 0 does not.
        archive.writestr(
            "xl/styles.xml",
            '<?xml version="1.0"?><styleSheet xmlns="%s"><cellXfs count="2">'
            '<xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>' % NS,
        )
    return buffer.getvalue()


def main():
    # --- the pieces, before the whole ---------------------------------
    check("column: A is 1", _xlsx_column("A1") == 1)
    check("column: Z/AA cross the 26 boundary",
          (_xlsx_column("Z9"), _xlsx_column("AA9")) == (26, 27),
          "%s/%s" % (_xlsx_column("Z9"), _xlsx_column("AA9")))
    check("column: BC is 55", _xlsx_column("BC42") == 55)
    check("column: an empty ref is not a column", _xlsx_column("") == 0)

    check("cell: a whole float loses the .0 that would corrupt a code",
          _cell_text(1200.0) == "1200", _cell_text(1200.0))
    check("cell: a real decimal is kept", _cell_text(0.01) == "0.01")
    check("cell: None is empty, not the text None", _cell_text(None) == "")
    check("cell: booleans read as the Y/N the reports use",
          (_cell_text(True), _cell_text(False)) == ("Y", "N"))
    check("cell: dates are ISO",
          _cell_text(datetime.datetime(2019, 5, 4)) == "2019-05-04T00:00:00")
    check("serial: 38481 is the date CSV would have shown",
          _xlsx_serial_to_text("38481").startswith("2005-05-09"),
          _xlsx_serial_to_text("38481"))

    # --- a sheet with a hole in it, which is the real hazard ----------
    # B is missing from the data row: without gap filling, C slides into B
    # and every later column follows it.
    raw = build_xlsx(
        [
            [("A", "s", 0), ("B", "s", 1), ("C", "s", 2), ("D", "s", 3)],
            [("A", "s", 4), ("C", "s", 5), ("D", None, "0.5")],
        ],
        shared=["SYS_LOC_CODE", "LOC_NAME", "CHEMICAL_NAME",
                "REPORT_RESULT_VALUE", "MW-1", "Benzene"],
    )
    headers, rows, truncated = EquisClient._rows_from_xlsx(raw)
    check("sparse: the header is read whole",
          headers == ["SYS_LOC_CODE", "LOC_NAME", "CHEMICAL_NAME",
                      "REPORT_RESULT_VALUE"], str(headers))
    check("sparse: a missing cell leaves a gap, it does not shift the row",
          rows == [["MW-1", "", "Benzene", "0.5"]], str(rows))
    check("sparse: rows keep the header width",
          all(len(r) == len(headers) for r in rows))

    # --- typed values through the whole reader ------------------------
    raw = build_xlsx(
        [
            [("A", "s", 0), ("B", "s", 1), ("C", "s", 2)],
            [("A", None, "1200"), ("B", None, "38481"), ("C", "b", "1")],
        ],
        date_columns=("B",),
        shared=["SYS_LOC_CODE", "SAMPLE_DATE", "DETECT_FLAG"],
    )
    headers, rows, _ = EquisClient._rows_from_xlsx(raw)
    check("typed: a numeric location code does not gain a decimal point",
          rows[0][0] == "1200", rows[0][0])
    check("typed: a date-styled number is rendered as a date",
          rows[0][1].startswith("2005-05-09"), rows[0][1])
    check("typed: a boolean cell reads as Y", rows[0][2] == "Y", rows[0][2])

    # --- keep_columns, which is what keeps big reports affordable -----
    headers, rows, _ = EquisClient._rows_from_xlsx(
        raw, keep_columns=["SYS_LOC_CODE", "DETECT_FLAG"])
    check("keep: only the wanted columns survive",
          headers == ["SYS_LOC_CODE", "DETECT_FLAG"], str(headers))
    check("keep: and the values follow their headers",
          rows == [["1200", "Y"]], str(rows))
    headers, rows, _ = EquisClient._rows_from_xlsx(
        raw, keep_columns=["NOT_A_COLUMN"])
    check("keep: an unmatched list keeps everything rather than nothing",
          len(headers) == 3, str(headers))

    # --- caps and refusals --------------------------------------------
    many = [[("A", "s", 0), ("B", "s", 1)]] + [
        [("A", None, str(i)), ("B", None, "1.5")] for i in range(50)
    ]
    raw_many = build_xlsx(many, shared=["SYS_LOC_CODE", "REPORT_RESULT_VALUE"])
    headers, rows, truncated = EquisClient._rows_from_xlsx(raw_many, max_rows=10)
    check("max_rows: stops at the cap and says so",
          len(rows) == 10 and truncated, "%d rows" % len(rows))

    _, cancelled, _ = EquisClient._rows_from_xlsx(
        raw_many, is_canceled=lambda: True)
    check("cancel: a cancelled read stops early rather than finishing",
          len(cancelled) < 50, "%d rows" % len(cancelled))

    try:
        EquisClient._rows_from_xlsx(b"not a zip at all")
        check("garbage: a non-workbook is refused clearly", False)
    except EquisError as exc:
        check("garbage: a non-workbook is refused clearly",
              "Excel" in str(exc), str(exc))

    # --- a title line above the header, as some reports carry ---------
    raw_title = build_xlsx(
        [
            [("A", "s", 0)],
            [("A", "s", 1), ("B", "s", 2)],
            [("A", "s", 3), ("B", None, "2.5")],
        ],
        shared=["Analytical Results II", "SYS_LOC_CODE",
                "REPORT_RESULT_VALUE", "MW-7"],
    )
    headers, rows, _ = EquisClient._rows_from_xlsx(raw_title)
    check("title: a one-cell title line is skipped, not read as the header",
          headers == ["SYS_LOC_CODE", "REPORT_RESULT_VALUE"], str(headers))
    check("title: and its data row survives", rows == [["MW-7", "2.5"]], str(rows))

    print("\n%s" % ("ALL XLSX TESTS PASSED" if not FAILURES
                    else "FAILURES: %s" % ", ".join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
