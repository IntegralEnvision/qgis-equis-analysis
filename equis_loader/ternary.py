"""Three analytes as shares of their sum: a ternary plot and its map.

Decided 2026-10-12 with the user:

  * ONE POINT IS ONE SAMPLE, as for the scatter (CLAUDE.md). The Points
    modes apply unchanged: every sample, or one real sample per location
    by a stated rank (bivariate.picks_sql).
  * ONE UNIT. The three must report in the same unit or the ternary is
    refused: a share of ug/L beside mg/L is a share of nothing. Units are
    not translated here (CLAUDE.md: conversion belongs to the pipeline).
  * NON-DETECTS ARE PLOTTED AT THEIR LIMIT, GREY AND HOLLOW, with a switch
    to hide them. A component below its limit has only an upper bound, so
    the sample's true place is a region, not the point drawn; grey says
    so, as the scatter's grey classes do. At Facility A, As + Pb + Zn in water
    is 62,288 samples of which 624 are detected on all three - the choice
    decides that plot, so it is visible rather than silent.
  * NOT a Piper diagram: no milliequivalents, no ion balance. Shares of
    the reported values, in their own unit.

ONE MODULE decides shares, positions and colours; the page's SQL
(web/src/ternary.js) is checked against it by tools/test_ternary.py, as
the scatter's is against bivariate.py.

No QGIS imports.
"""

import math

from .bivariate import GREY, picks_sql

# Corner hues: ColorBrewer Dark2's first three (orange, teal, purple), told
# apart in the common colour-vision deficiencies better than RGB primaries.
# A point's colour is its shares' mix of these, so the corners are pure and
# the middle is their average.
CORNERS = ("#d95f02", "#1b9e77", "#7570b3")
HEIGHT = math.sqrt(3) / 2


def _rgb(hex_colour):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def shares(a, b, c):
    """(sa, sb, sc) summing to 1, or None when the sample cannot be placed.

    A missing value, a negative one, or a zero sum has no share to draw.
    """
    if a is None or b is None or c is None:
        return None
    if a < 0 or b < 0 or c < 0:
        return None
    total = a + b + c
    if total <= 0:
        return None
    return (a / total, b / total, c / total)


def position(sa, sb, sc):
    """Plot coordinates: A at (0, 0), B at (1, 0), C at the top (1/2, h)."""
    return (sb + sc / 2.0, sc * HEIGHT)


def colour_for(a, b, c, all_detected, corners=CORNERS):
    """The point's colour: its shares' mix of the corners, or grey.

    Grey whenever any of the three is a non-detect: its position rests on
    a limit, and colour would claim a composition the data does not give.
    """
    s = shares(a, b, c)
    if s is None or not all_detected:
        return GREY
    mixed = [0.0, 0.0, 0.0]
    for share, corner in zip(s, corners):
        for i, channel in enumerate(_rgb(corner)):
            mixed[i] += share * channel
    # floor(v + 0.5), not round(): Python rounds halves to even and SQL
    # away from zero, and the page computes this same colour in SQL.
    return "#%02x%02x%02x" % tuple(int(math.floor(v + 0.5)) for v in mixed)


def stations(con, table, prefixes, detect_true, mode="recent", window=None):
    """One row per location for the ternary's map: the sample `mode` picks.

    As bivariate.stations, with three analytes: "all" maps the most recent
    sample, and "highest" ranks detected first. Each row carries its
    colour, decided here and nowhere else.
    """
    a, b, c = prefixes
    truth = "(%s)" % ", ".join("'%s'" % v for v in detect_true)
    rows = con.execute("""
        SELECT location, sample, sample_date,
               {a}_value, {b}_value, {c}_value,
               upper({a}_detect) IN {t}, upper({b}_detect) IN {t},
               upper({c}_detect) IN {t}, n, tied
        FROM ({picks})
        ORDER BY location
    """.format(a=a, b=b, c=c, t=truth, picks=picks_sql(
        table, list(prefixes), detect_true,
        "recent" if mode == "all" else mode, window))).fetchall()
    out = []
    for r in rows:
        values = (r[3], r[4], r[5])
        detected = (bool(r[6]), bool(r[7]), bool(r[8]))
        s = shares(*values)
        if s is None:
            continue                        # cannot be placed: not mapped
        out.append({"location": r[0], "sample": r[1], "date": r[2],
                    "values": values, "detected": detected, "shares": s,
                    "colour": colour_for(*values, all_detected=all(detected)),
                    "samples_here": r[9], "tied": r[10]})
    return out


def unit_refusal(columns):
    """None when the three share one unit; otherwise the sentence why not."""
    units = [(c.get("unit") or "").strip() for c in columns]
    if all(units) and len(set(units)) == 1:
        return None
    named = ", ".join("%s %s" % (c["chemical"], u or "(unit not reported)")
                      for c, u in zip(columns, units))
    return ("A ternary needs one unit for all three, so their shares compare "
            "like with like: %s. Choose analytes reported in the same unit; "
            "conversion belongs to the export pipeline." % named)
