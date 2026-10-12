"""Bivariate classes for a two-analyte scatter and its map (US-24).

After the R package biscale: each analyte is cut into three classes, and a
sample's class is "x-y" - "1-1" low in both, "3-3" high in both - coloured
from a 3x3 palette. ONE module decides the breaks, the classes and the
colours, and both the page (scatter) and QGIS (map layer) are handed its
answers. Two copies of a palette or a break rule is how a red point on the
chart becomes a blue point on the map.

Decided 2026-10-09:

  * NON-DETECTS ARE GREY. Only a sample detected on BOTH axes gets a
    bivariate colour. A non-detect says "below this limit", and whether that
    is class 1 depends on where the limit sits - on Facility A, 84% of Mn
    non-detects have limits below Mn's first break but only 17% of Pb's
    do, so a plain classification would have called ~51,000 Pb
    non-detects "low" that might not be. The three classes involving a
    non-detect keep their scatter symbols and are drawn grey.
  * JENKS breaks, over the both-detected samples, per axis.
  * ON LOG10 VALUES when every value is positive. Measured on Facility A Mn x Pb
    WS/T: Jenks on raw values gave classes of 1,567 / 4 / 2 (Mn) and
    1,550 / 21 / 2 (Pb) - the extremes take two classes and everything
    else is "low". On log10: 466 / 742 / 365 and 366 / 878 / 329.
    Concentrations are roughly log-normal and the scatter's axes are
    already log, so the classes match what the eye sees.
  * A MAP STATION is coloured by its MOST RECENT sample carrying both
    analytes - one real sample, never the max of one beside the max of the
    other (CLAUDE.md: a multivariate point is one sample).

No QGIS imports. numpy when present (QGIS ships it), pure Python otherwise.
"""

import math

# Joshua Stevens' 3x3 scheme, as biscale ships it under "GrPink": x runs to
# pink, y to cyan, both to dark blue. Keys are "x-y", biscale's convention.
PALETTE = {
    "1-1": "#e8e8e8", "2-1": "#dfb0d6", "3-1": "#be64ac",
    "1-2": "#ace4e4", "2-2": "#a5add3", "3-2": "#8c62aa",
    "1-3": "#5ac8c8", "2-3": "#5698b9", "3-3": "#3b4994",
}
PALETTE_NAME = "GrPink (Joshua Stevens, via biscale)"

# Everything involving a non-detect. One grey, per the decision: the symbol,
# not the colour, says which axis was the non-detect.
GREY = "#9a9a9a"
DETECTION_CLASSES = ("both", "x_only", "y_only", "neither")

CLASSES = 3

# QGIS's own Jenks classifier samples down to 3,000 values for the same
# reason: exact Jenks costs O(k n^2).
MAX_SAMPLE = 3000


def natural_breaks(values, k=CLASSES, max_sample=MAX_SAMPLE):
    """Jenks breaks for `values`: {"breaks": [b1, ..., b(k-1)], ...}.

    On log10 values when all are positive (see the module docstring), with
    the breaks returned in VALUE space so callers never handle logs. Returns
    None when there are too few distinct values to make k classes.
    """
    clean = sorted(float(v) for v in values
                   if v is not None and math.isfinite(float(v)))
    n = len(clean)
    if len(set(clean)) < k:
        return None
    use_log = clean[0] > 0
    space = [math.log10(v) for v in clean] if use_log else clean
    sample = _even_sample(space, max_sample)
    inner = _jenks(sample, k)
    # Each break is a data value, so hand back THAT value, not 10**log of
    # it: the round trip gives 12.000000000000002 for 12, and one landing a
    # hair below would move the sample sitting on the break up a class.
    original = dict(zip(space, clean)) if use_log else None
    breaks = [original[b] for b in inner] if use_log else inner
    return {"breaks": breaks, "scale": "log10" if use_log else "linear",
            "n": n, "used": len(sample)}


def class_of(value, breaks):
    """1..len(breaks)+1 for a value against its axis breaks (b inclusive)."""
    for index, edge in enumerate(breaks):
        if value <= edge:
            return index + 1
    return len(breaks) + 1


def detection_class(x_detected, y_detected):
    if x_detected and y_detected:
        return "both"
    if x_detected:
        return "x_only"
    if y_detected:
        return "y_only"
    return "neither"


def key_for(x_value, y_value, x_detected, y_detected, x_breaks, y_breaks):
    """The legend key: "x-y" for a both-detected sample, else its ND class."""
    detection = detection_class(x_detected, y_detected)
    if detection != "both":
        return detection
    return "%d-%d" % (class_of(x_value, x_breaks), class_of(y_value, y_breaks))


def colour_for(key):
    return PALETTE.get(key, GREY)


def axis_breaks(con, table, x, y, detect_true):
    """Jenks breaks per axis over the samples detected on BOTH axes.

    Only those samples are coloured, so only they define the classes. Each
    axis gets its own breaks; the result says which scale they were found
    on, for the legend.
    """
    truth = "(%s)" % ", ".join("'%s'" % v for v in detect_true)
    rows = con.execute(
        "SELECT {x}_value, {y}_value FROM {t} "
        "WHERE upper({x}_detect) IN {truth} AND upper({y}_detect) IN {truth} "
        "AND {x}_value IS NOT NULL AND {y}_value IS NOT NULL".format(
            x=x, y=y, t=table, truth=truth)).fetchall()
    return {"x": natural_breaks([r[0] for r in rows]),
            "y": natural_breaks([r[1] for r in rows]),
            "samples": len(rows)}


# WHICH SAMPLE STANDS FOR A LOCATION (decided 2026-10-12). Every mode
# picks ONE REAL SAMPLE per location - never X's maximum beside Y's from
# another sample, which is a point no sample had (CLAUDE.md; 174 of 216
# Facility A WS/T stations for Mn x Pb). "all" draws every sample; on the
# scatter map it means the most recent, as before.
MODES = ("all", "recent", "max_x", "max_y", "max_z")
MODE_LABELS = {
    "all": "every sample",
    "recent": "each location's most recent sample",
    "max_x": "each location's sample with the highest {x}",
    "max_y": "each location's sample with the highest {y}",
    "max_z": "each location's sample with the highest {z}",
}
# Which of a chart's analytes a "highest" mode ranks by: X, Y, or a
# ternary's third (Z).
_AXIS = {"max_x": 0, "max_y": 1, "max_z": 2}


def _order(mode, prefixes, truth):
    """The ranking within a location, best first.

    "Highest" ranks DETECTED first, then by value: a non-detect carries
    only the limit it fell below and can never be a location's maximum
    while a detection exists there (CLAUDE.md) - the store's own maximum
    ordering. Then the latest date, then the sample code, so a pick is
    stable.
    """
    recent = "sample_date DESC NULLS LAST, sample"
    if mode in _AXIS:
        axis = prefixes[_AXIS[mode]]
        return ("(upper({a}_detect) IN {t}) DESC, {a}_value DESC, {r}"
                .format(a=axis, t=truth, r=recent))
    return recent


def _tie_key(mode, prefixes, truth):
    """What counts as a tie for `tied`: the same rank before the code."""
    if mode in _AXIS:
        axis = prefixes[_AXIS[mode]]
        return "(upper({a}_detect) IN {t}), {a}_value".format(a=axis, t=truth)
    return "sample_date"


def window_clause(window, column="sample_date"):
    """SQL for a (start, end) window of dates, or "TRUE" for none.

    Whole days, inclusive at both ends, and an undated sample is OUTSIDE
    any window - the panel's own date rule (CLAUDE.md, "Every filter in the
    panel applies whatever the source"). Either end may be None.
    """
    if not window:
        return "TRUE"
    import datetime
    start, end = window
    parts = []
    if start is not None:
        parts.append("%s >= DATE '%s'" % (column, _day(start).isoformat()))
    if end is not None:
        parts.append("%s < DATE '%s'" % (
            column, (_day(end) + datetime.timedelta(days=1)).isoformat()))
    if not parts:
        return "TRUE"
    return "(%s IS NOT NULL AND %s)" % (column, " AND ".join(parts))


def _day(value):
    import datetime
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value)[:10])


def picks_sql(table, prefixes, detect_true, mode="recent", window=None):
    """SQL for the samples a mode draws solid: one per location, or all.

    `prefixes` are the chart's analytes: two for a scatter, three for a
    ternary. Only samples the chart can place are candidates - every value
    and every detect flag known (a blank flag is unknown, not a
    non-detect) - and only those inside `window`. Columns: every column of `table`, plus `n`
    (candidates at the location) and `tied` (candidates sharing the pick's
    rank, so an arbitrary pick is said, never hidden).
    """
    if mode not in MODES or (mode in _AXIS and _AXIS[mode] >= len(prefixes)):
        raise ValueError("Unknown points mode %r for %d analytes."
                         % (mode, len(prefixes)))
    truth = "(%s)" % ", ".join("'%s'" % v for v in detect_true)
    known = " AND ".join("{p}_value IS NOT NULL AND {p}_detect IS NOT NULL"
                         .format(p=p) for p in prefixes)
    candidates = """
        SELECT * FROM {t}
        WHERE {known}
          AND {w}""".format(t=table, known=known, w=window_clause(window))
    if mode == "all":
        return ("SELECT *, 1 AS n, 1 AS tied FROM (%s)" % candidates)
    return """
        SELECT * EXCLUDE (pick) FROM (
          SELECT *,
                 row_number() OVER (PARTITION BY upper(location)
                                    ORDER BY {order}) AS pick,
                 count(*) OVER (PARTITION BY upper(location)) AS n,
                 count(*) OVER (PARTITION BY upper(location), {tie}) AS tied
          FROM ({candidates})
          WHERE coalesce(location, '') <> '')
        WHERE pick = 1""".format(order=_order(mode, prefixes, truth),
                                 tie=_tie_key(mode, prefixes, truth),
                                 candidates=candidates)


def stations(con, table, x, y, detect_true, mode="recent", window=None):
    """One row per location, for the scatter map: the sample `mode` picks.

    "recent" (and "all", which cannot put every sample on one station)
    is the MOST RECENT sample carrying both analytes; "max_x"/"max_y" the
    one with the highest X or Y, detected first. Ties fall to the latest
    date then the sample code, so the choice is stable - and `tied` counts
    them, because a tie is common and the pick is then arbitrary: at
    Facility A every SO station's latest date holds 6-9 field replicates
    (ST7_20250121_SO_PT, _R2 ... _R9, all typed N), so the map shows one of
    nine. Said, never hidden. Returns dicts keyed for the layer.
    """
    truth = "(%s)" % ", ".join("'%s'" % v for v in detect_true)
    rows = con.execute("""
        SELECT location, sample, sample_date, {x}_value, {y}_value,
               upper({x}_detect) IN {truth}, upper({y}_detect) IN {truth},
               {x}_qualifier, {y}_qualifier, n, tied
        FROM ({picks})
        ORDER BY location
    """.format(x=x, y=y, truth=truth, picks=picks_sql(
        table, [x, y], detect_true, "recent" if mode == "all" else mode,
        window))).fetchall()
    return [{"location": r[0], "sample": r[1], "date": r[2],
             "x": r[3], "y": r[4], "x_detected": bool(r[5]),
             "y_detected": bool(r[6]), "x_qualifier": r[7],
             "y_qualifier": r[8], "samples_here": r[9],
             "tied": r[10]} for r in rows]


# --------------------------------------------------------------- internals

def _even_sample(sorted_values, limit):
    """At most `limit` values, evenly spaced over the SORTED input.

    Deterministic, and keeps the distribution's shape including both ends,
    which a random sample would not guarantee.
    """
    n = len(sorted_values)
    if n <= limit:
        return list(sorted_values)
    step = (n - 1) / float(limit - 1)
    return [sorted_values[int(round(i * step))] for i in range(limit)]


def _jenks(values, k):
    """Fisher's exact optimal k-class split of sorted `values` (inner breaks).

    Minimises the total within-class sum of squared deviations. Each break
    is the LAST value of a class, so class_of's `<=` puts it inside.
    """
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - QGIS ships numpy
        return _jenks_pure(values, k)
    v = np.asarray(values, dtype=float)
    n = len(v)
    s1 = np.concatenate(([0.0], np.cumsum(v)))
    s2 = np.concatenate(([0.0], np.cumsum(v * v)))

    def ssd(i, j):  # rows i..j inclusive, vectorised over i
        count = j - i + 1
        total = s1[j + 1] - s1[i]
        return (s2[j + 1] - s2[i]) - total * total / count

    cost = np.full((k, n), np.inf)
    arg = np.zeros((k, n), dtype=int)
    cost[0] = ssd(np.zeros(n, dtype=int), np.arange(n))
    for c in range(1, k):
        for j in range(c, n):
            starts = np.arange(c, j + 1)
            candidates = cost[c - 1][starts - 1] + ssd(starts, j)
            best = int(np.argmin(candidates))
            cost[c][j] = candidates[best]
            arg[c][j] = starts[best]
    cuts, j = [], n - 1
    for c in range(k - 1, 0, -1):
        i = arg[c][j]
        cuts.append(float(v[i - 1]))
        j = i - 1
    return sorted(cuts)


def _jenks_pure(values, k):
    n = len(values)
    s1, s2 = [0.0], [0.0]
    for x in values:
        s1.append(s1[-1] + x)
        s2.append(s2[-1] + x * x)

    def ssd(i, j):
        count = j - i + 1
        total = s1[j + 1] - s1[i]
        return (s2[j + 1] - s2[i]) - total * total / count

    inf = float("inf")
    cost = [[inf] * n for _ in range(k)]
    arg = [[0] * n for _ in range(k)]
    for j in range(n):
        cost[0][j] = ssd(0, j)
    for c in range(1, k):
        for j in range(c, n):
            for i in range(c, j + 1):
                val = cost[c - 1][i - 1] + ssd(i, j)
                if val < cost[c][j]:
                    cost[c][j], arg[c][j] = val, i
    cuts, j = [], n - 1
    for c in range(k - 1, 0, -1):
        i = arg[c][j]
        cuts.append(values[i - 1])
        j = i - 1
    return sorted(cuts)
