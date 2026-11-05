"""In-memory view over one run of an analytical report.

The report is fetched once and held here; the chemical list, the per-location
aggregate that drives the map, and the per-location time series are all served
from that copy so the UI never waits on the API again.

No QGIS imports, so this is testable with a plain interpreter.
"""

import datetime

# Column names as they appear in the EQuIS analytical reports (ARII and
# friends). Each entry lists the acceptable spellings, best first.
COLUMNS = {
    "location": ("SYS_LOC_CODE", "LOCATION_CODE", "LOC_CODE"),
    "chemical": ("CHEMICAL_NAME", "ANALYTE_NAME", "PARAM_NAME"),
    "cas_rn": ("CAS_RN", "CASRN", "CAS"),
    "date": ("SAMPLE_DATE", "SAMPLE_DATE_TIME", "COLLECTION_DATE"),
    "value": ("RESULT_NUMERIC", "REPORT_RESULT_VALUE", "RESULT_VALUE"),
    # Non-detects report no result, only the limit they fell below.
    "limit": (
        "REPORT_METHOD_DETECTION_LIMIT",
        "METHOD_DETECTION_LIMIT",
        "REPORTING_DETECTION_LIMIT",
        "REPORT_REPORTING_LIMIT",
        "REPORT_RESULT_LIMIT",
    ),
    "reported": ("REPORT_RESULT_VALUE",),
    "unit": ("RESULT_UNIT", "REPORT_RESULT_UNIT", "UNIT"),
    "detect": ("DETECT_FLAG", "DETECTED"),
    "x": ("X_COORD", "LONGITUDE", "EASTING"),
    "y": ("Y_COORD", "LATITUDE", "NORTHING"),
    "matrix": ("MATRIX_CODE", "MATRIX"),
    # The sampled interval. A soil boring is sampled every few metres, so
    # without these every result at one borehole looks like the same place.
    "start_depth": ("START_DEPTH", "DT_SAMPLE_START_DEPTH"),
    "end_depth": ("END_DEPTH", "DT_SAMPLE_END_DEPTH"),
    "depth_unit": ("DEPTH_UNIT", "DT_SAMPLE_DEPTH_UNIT"),
    # A per-sample free-text field whose meaning is site-specific.
    "custom_1": ("CUSTOM_FIELD_1", "DT_SAMPLE_CUSTOM_FIELD_1"),
    # Which laboratory method produced the result. One sample is routinely
    # analysed by more than one - at Facility A, 162 of 172 tailings intervals
    # carry exactly two, and they can disagree about whether the analyte was
    # detected at all. Without this the two look like duplicate data.
    "method": ("ANALYTIC_METHOD", "ANALYSIS_METHOD", "METHOD_CODE"),
    # Dissolved vs total. Ignoring this treats results that differ by up to
    # ~29x for the same sample as interchangeable.
    "fraction": ("FRACTION", "FRACTION_CODE"),
    # Normal results vs QC (equipment blanks, field duplicates), which are
    # near-zero by construction and drag any aggregate down.
    "sample_type": ("SAMPLE_TYPE_CODE", "SAMPLE_TYPE"),
    # What a pivot needs and nothing else reads yet (docs/pivot-design.md).
    # The field sample a result came from: a pivot's row. Two samples at one
    # location on one day are depth intervals or sequential grabs, and a
    # pivot keyed on anything coarser puts them in one cell.
    "sample": ("SYS_SAMPLE_CODE",),
    # INITIAL vs REPLICATE: the cell rule prefers the initial analysis.
    "test_type": ("TEST_TYPE",),
    # A pivot's _qualifier reads interpreted qualifiers, falling back to the
    # lab's. Kept as two keys so the fallback is a decision made where the
    # pivot is built, not hidden in an alias order.
    "qual_interpreted": ("INTERPRETED_QUALIFIERS",),
    "qual_lab": ("LAB_QUALIFIERS",),
    # The laboratory, for splitting a variable by lab when asked to.
    "lab": ("LAB_NAME_CODE",),
}

# Formats seen in these exports, tried in order.
# Ordered by how often they actually occur, because parse_date walks the list
# and a miss costs a raised exception. The 12-hour forms are first: EQuIS's
# Analytical Results II emits "4/24/2025 12:37:00 PM" on this tenant, and
# without them every row threw 8 exceptions, fell back to splitting the
# string, then threw 8 more on the date half - 16 per row, which was 28 s of
# a 32 s store ingest on a 222,571-row report.
DATE_FORMATS = (
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d-%b-%y",
    "%d-%b-%Y",
)

# The columns the chemical workflow actually reads. Requesting only these
# shrinks the response when the report genuinely runs; a report answered from
# EQuIS's cache ignores the request and returns everything.
ANALYSIS_COLUMNS = (
    "SYS_LOC_CODE",
    "SAMPLE_DATE",
    "CHEMICAL_NAME",
    "CAS_RN",
    "RESULT_NUMERIC",
    "RESULT_UNIT",
    "DETECT_FLAG",
    "MATRIX_CODE",
    "FRACTION",
    "SAMPLE_TYPE_CODE",
    # Already in the report's default output - keep_columns was the only
    # reason they never reached the plugin.
    "START_DEPTH",
    "END_DEPTH",
    "DEPTH_UNIT",
    # Requested through @extra_fields, which lowercases and table-prefixes
    # what it adds, so both spellings are kept.
    "CUSTOM_FIELD_1",
    "DT_SAMPLE_CUSTOM_FIELD_1",
    # In the report's default output already; without keeping it, two methods
    # on one sample are indistinguishable from duplicated rows.
    "ANALYTIC_METHOD",
    # Non-detects leave RESULT_NUMERIC empty and carry the limit instead, so
    # without these they would vanish from the data entirely.
    "REPORT_RESULT_VALUE",
    "REPORT_METHOD_DETECTION_LIMIT",
    "REPORTING_DETECTION_LIMIT",
    "METHOD_DETECTION_LIMIT",
    # RESULT_UNIT is blank on a non-detect row, so the limit above had no
    # unit in the export. Both of these are filled on every row (verified
    # 2026-09-11 on ARII: REPORT_RESULT_UNIT is the unit of
    # REPORT_RESULT_VALUE, DETECTION_LIMIT_UNIT that of the limit columns).
    "REPORT_RESULT_UNIT",
    "DETECTION_LIMIT_UNIT",
    # Names the sample a result came from, which the export needs to tell
    # two samples at one location on one day apart.
    "SYS_SAMPLE_CODE",
    # The pivot's cell rule and its _qualifier column (docs/pivot-design.md).
    "TEST_TYPE",
    "INTERPRETED_QUALIFIERS",
    "LAB_QUALIFIERS",
    "LAB_NAME_CODE",
    # Tie a result back to its lab delivery and its test. LAB_SDG is in the
    # default output (filled on ~1% of rows at the first facility exported).
    # DT_TEST.IDENTIFIER arrives through @extra_fields as plain IDENTIFIER -
    # NOT table-prefixed, unlike CUSTOM_FIELD_1 (read 2026-10-17 from a run
    # of a slice report asking for it).
    "LAB_SDG",
    "IDENTIFIER",
)

# The matrix filter's "no filter" state. A report mixes matrices - water,
# soil, air - and aggregating across them compares concentrations that are not
# comparable, so the dialog offers a filter. None means every matrix, which is
# the historical behaviour and stays the default for callers that do not care.
ALL_MATRICES = None

# Sorts undated rows without having to compare None against a date.
_EPOCH = datetime.date(1, 1, 1)

# The fraction filter's "no filter" state, mirroring ALL_MATRICES. Dissolved
# and total results interleave for the same location, chemical and day, and
# can differ by more than an order of magnitude, so aggregating across them
# compares numbers that are not comparable in the same way mixing matrices
# does. None is the historical behaviour and stays the default.
ANY_FRACTION = None

# The method filter's "no filter" state. Unlike matrix and fraction, several
# methods on one sample are not a mistake to be prevented - they are two
# genuine analyses - so the default shows them all and the profile draws a
# line per method rather than collapsing them.
ALL_METHODS = None

# The row scope's "no filter" state: (start, end, sample_types).
#
# Dates and sample types are not like matrix or method. They decide which
# rows EXIST for the session, the way @start_date, @end_date and
# @sample_types do on a live fetch - so they are applied once, to the row set,
# rather than threaded through every question as another parameter. That is
# what lets them work the same whatever the source: a live fetch was already
# narrowed by the server, but a parquet source holds every row the facility
# has, and before 2026-10-03 the date range and the QC checkbox did nothing
# to it at all. A sediment map with Dates ticked for 2015-11-05..2026-01-01
# drew a 2003-01-01 result.
#
# Depth joined it on 2026-10-30, as a fourth part: (top, bottom, unit), or
# None for no depth filter. It never shapes a fetch (selection_panel.py says
# why) - but it is still a statement about which rows exist, and putting it
# anywhere else would make it a filter some question forgets.
#
# Units joined it the same day, as a fifth: the result units kept, as a
# sorted tuple, or () for every unit. A solid's matrix legitimately carries
# a few leachate results in water's units - Facility A's sediment holds
# 3,401 in mg/L beside 635,904 in mg/kg (2026-10-30) - and the unit guard
# then refuses every map of those analytes. Choosing the unit is the way
# out that keeps the guard whole: the rows go, rather than being compared.
# The unit is the one the guard reads (the coalesced result unit), so what
# the filter offers is exactly what the guard complains about.
NO_ROW_SCOPE = (None, None, frozenset(), None, ())

# Metres per unit of depth. A depth filter compares numbers that may be
# written in different units within ONE matrix: Facility A's sediment carries
# 331,986 results in cm beside 1,624 in m (measured 2026-10-30), so a bound
# of "0 to 5" read as plain numbers takes the top 5 cm of one and the top 5 m
# of the other, and the map looks fine.
#
# This is not the presentation-level unit translation CLAUDE.md rules out.
# That rule is about concentrations, whose factors are EQuIS's to own and
# depend on the analyte; a centimetre is a hundredth of a metre by
# definition, and there is nothing for the pipeline to know that this does
# not. A unit not listed here is never guessed at: its rows compare only
# against a filter written in that same unit (see depth_factor).
DEPTH_UNIT_METRES = {
    "m": 1.0,
    "cm": 0.01,
    "mm": 0.001,
    "ft": 0.3048,
    "in": 0.0254,
}
# The spellings EQuIS's RT_UNIT and hand-made sources actually use for the
# same units. Compared lower-cased, so "FT" and "Ft" need no entry.
DEPTH_UNIT_ALIASES = {
    "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "centimeter": "cm", "centimeters": "cm", "centimetre": "cm",
    "centimetres": "cm",
    "millimeter": "mm", "millimeters": "mm", "millimetre": "mm",
    "millimetres": "mm",
    "feet": "ft", "foot": "ft",
    "inch": "in", "inches": "in",
}
# Depths are compared after converting and rounding to this many places, in
# Python and in SQL alike, so 30 cm is 0.3 m on both sides of a bound rather
# than 0.30000000000000004 on one of them.
DEPTH_PLACES = 9


def depth_unit(text):
    """A depth unit as compared: trimmed, lower-cased, aliases resolved.

    A blank unit stays "", which is a unit of its own: a source whose depths
    carry no unit can still be filtered, in those numbers, but never
    converted to or from anything.
    """
    text = str(text or "").strip().lower()
    return DEPTH_UNIT_ALIASES.get(text, text)


def depth_factor(unit, target):
    """What a depth in `unit` is multiplied by to read in `target`, or None.

    None means the two cannot be compared: one of them is a unit this module
    does not know, and its rows are left out of the filter rather than
    compared as though the numbers meant the same thing.
    """
    unit, target = depth_unit(unit), depth_unit(target)
    if unit == target:
        return 1.0
    if unit in DEPTH_UNIT_METRES and target in DEPTH_UNIT_METRES:
        return DEPTH_UNIT_METRES[unit] / DEPTH_UNIT_METRES[target]
    return None


def depth_interval(start, end):
    """(top, bottom) of a sample's interval, or None when it has no depth.

    A point sample (no end depth, or end equal to start) is an interval of
    no thickness. A row with only an end depth is placed there too, and an
    interval written bottom-first is turned round; Facility A has neither
    (2026-10-30), so these are guards, not a measured case.
    """
    if start is None and end is None:
        return None
    top = start if start is not None else end
    bottom = end if end is not None else start
    return (top, bottom) if top <= bottom else (bottom, top)


def in_depth(scope_depth, start, end, unit):
    """Whether a row's depth interval lies wholly inside a depth filter.

    WHOLLY inside, not merely overlapping: a 0-10 ft composite under a
    "0 to 2 ft" filter is mostly material from below the range, and the
    number it reports is not a surface number. A row with no depth is
    outside any depth filter, as an undated row is outside any date range.
    """
    interval = depth_interval(start, end)
    if interval is None:
        return False
    top, bottom, target = scope_depth
    factor = depth_factor(unit, target)
    if factor is None:
        return False
    if top is not None and round(interval[0] * factor, DEPTH_PLACES) < top:
        return False
    if bottom is not None and round(interval[1] * factor, DEPTH_PLACES) > bottom:
        return False
    return True


def depth_extent_from(groups, boundaries=()):
    """The range a depth filter can offer, from per-unit summaries.

    `groups` is [(unit, rows, shallowest, deepest)], one per depth unit
    already normalised by depth_unit(). `boundaries` is every (unit, depth)
    at which some interval starts or ends, in the same units. Shared by
    ReportTable and ReportStore, which each produce both their own way, so
    the choice of unit and the conversions are made in one place.

    The unit offered is the one most rows are in (ties by name), and every
    other unit is converted into it. Rows in a unit that cannot be converted
    are counted under `unconverted` and left out of the range, because a
    filter on them would compare numbers that mean different things.
    Returns None when no row has a depth.

    `stops` are the boundaries in that unit, sorted and distinct: the only
    depths at which moving a bound changes which rows are kept. A row is in
    when its top is at or below the filter's top and its bottom at or above
    the filter's bottom (in_depth), so between two neighbouring boundaries
    nothing can change. A slider evenly spaced over 0-200 ft instead moved a
    foot at a time and could not pick out a 0-0.33 ft soil interval (seen
    2026-10-30); stops crowd where the samples do, which is the surface.
    """
    groups = [g for g in groups if g[1]]
    if not groups:
        return None
    unit = sorted(groups, key=lambda g: (-g[1], g[0]))[0][0]
    shallowest = deepest = None
    rows = 0
    unconverted = {}
    for name, count, top, bottom in groups:
        factor = depth_factor(name, unit)
        if factor is None:
            unconverted[name] = unconverted.get(name, 0) + count
            continue
        rows += count
        top = round(top * factor, DEPTH_PLACES)
        bottom = round(bottom * factor, DEPTH_PLACES)
        shallowest = top if shallowest is None else min(shallowest, top)
        deepest = bottom if deepest is None else max(deepest, bottom)
    stops = set()
    for name, depth in boundaries:
        factor = depth_factor(name, unit)
        if factor is not None and depth is not None:
            stops.add(round(depth * factor, DEPTH_PLACES))
    return {"unit": unit, "top": shallowest, "bottom": deepest,
            "rows": rows, "unconverted": unconverted,
            "stops": tuple(sorted(stops))}


def matrix_key(matrix):
    """A matrix filter as something hashable that compares as the filter does.

    ALL_MATRICES and an empty collection are both "no filter", and codes
    compare case-insensitively, so all of those meet at one key.
    """
    if matrix is ALL_MATRICES:
        return None
    if isinstance(matrix, (list, tuple, set, frozenset)):
        codes = frozenset(str(m).upper() for m in matrix if str(m or "").strip())
        return codes or None
    return frozenset([str(matrix or "").upper()])


def row_scope(start=None, end=None, sample_types=None, depth=None,
              units=None):
    """Normalise a row scope: dates as date, sample types upper-cased.

    A datetime is reduced to its date: the bounds are whole days, inclusive
    at both ends, so a sample taken at 3 PM on the end date is inside.

    `depth` is (top, bottom, unit) with either bound None for an open end,
    or None for no depth filter. Bounds are inclusive and rounded as depths
    are (DEPTH_PLACES), so a bound typed as 0.3 meets a depth stored as
    30 cm.

    `units` are result units, compared trimmed and case-SENSITIVELY, as the
    unit guard counts them: "mg/L" and "MG/L" are two entries there, so they
    are two here. "" is the unit of a row with none. A sorted tuple rather
    than a set, because analysis.py and pivot.py key caches on str(scope).
    """
    def day(value):
        if isinstance(value, datetime.datetime):
            return value.date()
        return value
    codes = frozenset(str(code).strip().upper() for code in sample_types or ()
                      if str(code or "").strip())
    if depth is not None:
        top, bottom, unit = depth
        top = None if top is None else round(float(top), DEPTH_PLACES)
        bottom = None if bottom is None else round(float(bottom), DEPTH_PLACES)
        depth = (top, bottom, depth_unit(unit))
    units = tuple(sorted({str(u if u is not None else "").strip()
                          for u in units or ()}))
    return (day(start), day(end), codes, depth, units)


MOST_RECENT = "most_recent"
MAXIMUM = "maximum"
# Not an aggregation at all, which is the point: it is the answer "do not
# collapse them" to the same question the other two answer, so it belongs in
# the same control rather than in a checkbox beside it. Keeping it here also
# means every place that already carries the aggregation - the view identity,
# the layer name, the GeoPackage table name - distinguishes an unaggregated
# layer from an aggregated one without being taught how.
UNAGGREGATED = "unaggregated"
AGGREGATIONS = (
    (MOST_RECENT, "Most recent"),
    (MAXIMUM, "Maximum"),
    (UNAGGREGATED, "Every result (one point per sample)"),
)
# Short forms for layer and table names, where the combo's explanatory label
# would be unwieldy.
AGGREGATION_SHORT = {
    MOST_RECENT: "most recent",
    MAXIMUM: "maximum",
    UNAGGREGATED: "every result",
}

# How to treat a non-detect, which is reported only as the limit it fell
# below. Full DL is conservative for screening; DL/2 is the usual convention
# for summary statistics and trend work.
ND_HALF = "half"
ND_FULL = "full"
ND_EXCLUDE = "exclude"
ND_HANDLING = (
    (ND_HALF, "1/2 detection limit"),
    (ND_FULL, "Full detection limit"),
    (ND_EXCLUDE, "Exclude non-detects"),
)


_DATE_FORMAT_CACHE = {}


def parse_date(text):
    """Parse a report date cell, or None if it is empty/unrecognised."""
    text = (text or "").strip()
    if not text:
        return None
    # A report's dates are all written the same way, so the format that
    # worked last time almost always works again. Trying it first turns the
    # common case into one strptime instead of a walk down the list throwing
    # an exception per miss - which dominated store ingest before this.
    remembered = _DATE_FORMAT_CACHE.get("last")
    if remembered is not None:
        try:
            return datetime.datetime.strptime(text, remembered)
        except ValueError:
            pass
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        _DATE_FORMAT_CACHE["last"] = fmt
        return parsed
    # Fall back to the date half of an unrecognised timestamp.
    head = text.split("T")[0].split(" ")[0]
    if head != text:
        for fmt in DATE_FORMATS:
            try:
                return datetime.datetime.strptime(head, fmt)
            except ValueError:
                continue
    return None


def parse_number(text):
    """Parse a numeric result cell, or None."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        # Some exports prefix non-detects with a qualifier, e.g. "<0.5".
        stripped = text.lstrip("<>~=").strip()
        try:
            return float(stripped)
        except ValueError:
            return None


# The columns a result cannot be valued without, as (key, the name reported
# when it is missing). ONE list, read by ReportTable and ReportStore alike, so
# the two cannot disagree about what "cannot drive the map" means.
#
# DETECT_FLAG is on it because detection is never inferred. Without the column
# is_detected() and the store's view both read every result as a non-detect,
# and under the 1/2 DL default every MEASURED value is halved and drawn as a
# limit - Arsenic 10 and 40 ug/L mapped as 5 and 20, no error anywhere
# (reproduced 2026-10-08 on both implementations; the parity gate could not
# see it because they agreed). Qualifier text is no substitute: it is a lab's
# vocabulary, and at Facility A 35,662 non-detects carry no qualifier at all. The
# plugin is scoped to EQuIS and EQuIS-derived extractions, which carry the
# flag; a source that lacks it is fixed upstream, not guessed at here.
REQUIRED_FOR_ANALYSIS = (
    ("location", "SYS_LOC_CODE"),
    ("chemical", "CHEMICAL_NAME"),
    ("value", "RESULT_NUMERIC"),
    ("date", "SAMPLE_DATE"),
    ("detect", "DETECT_FLAG"),
)


def missing_required(has):
    """Names of the required columns for which `has(key)` is false."""
    return [label for key, label in REQUIRED_FOR_ANALYSIS if not has(key)]


def missing_message(missing, subject="This report"):
    """The refusal, worded once for every source that can be refused."""
    text = ("%s cannot drive the map: it has no %s column(s)."
            % (subject, ", ".join(missing)))
    if "DETECT_FLAG" in missing:
        text += (" Detection is read from DETECT_FLAG and never guessed, "
                 "because without it every measured value would be treated "
                 "as a detection limit. Add it to the extraction.")
    return text


# The report columns that describe a SAMPLE, not a result: EQuIS's facility,
# location and sample records as Analytical Results II names them. They are
# what a pivot's point - one sample, many results - may carry (CLAUDE.md, "A
# multivariate point is one sample"). Declared, not inferred per map: over
# Facility A's 106,408 multi-row samples (2026-10-30) exactly these held one
# value in every sample, while REPORTABLE_RESULT, APPROVAL_CODE and
# VALIDATOR_QUALIFIERS did too only by coincidence - they are result facts
# that happen to agree there - and inferring from a map's own samples let
# qualifiers and limits in wherever its few samples agreed. Checking every
# sample instead took 98 s. sample_fields() still drops one of these that
# disagrees within a mapped sample, so nothing is ever shown as a sample's
# that one of its rows contradicts.
SAMPLE_COLUMNS = (
    "FACILITY_ID", "FACILITY_CODE", "SYS_LOC_CODE", "LOC_NAME", "SAMPLE_ID",
    "SYS_SAMPLE_CODE", "SAMPLE_NAME", "SAMPLE_DATE", "SAMPLEDATE",
    "SAMPLETIME", "SAMPLE_TYPE_CODE", "START_DEPTH", "END_DEPTH",
    "DEPTH_UNIT", "MATRIX_CODE", "TASK_CODE", "TASK_CODE_2",
    "PARENT_SAMPLE_CODE", "FIELD_SDG", "COMPOSITE_YN", "X_COORD", "Y_COORD",
    "Z_COORD_AVG", "ZFROM", "ZTO", "LONGITUDE", "LATITUDE",
    "CUSTOM_FIELD_1",
)
# What @extra_fields adds from the sample and location tables arrives
# table-prefixed (DT_SAMPLE_CUSTOM_FIELD_1), so the prefix says where it is from.
SAMPLE_PREFIXES = ("DT_SAMPLE_", "DT_LOCATION_", "DT_FACILITY_")


def is_sample_column(header):
    """Whether a report column describes the sample rather than a result."""
    name = str(header or "").strip().upper()
    return name in SAMPLE_COLUMNS or name.startswith(SAMPLE_PREFIXES)


class ReportTable:
    """Rows of one report run, indexed for the chemical workflow."""

    def __init__(self, headers, rows):
        self.headers = list(headers or [])
        self.rows = list(rows or [])
        self._index = {}
        upper = [h.strip().upper() for h in self.headers]
        for key, candidates in COLUMNS.items():
            self._index[key] = -1
            for want in candidates:
                if want in upper:
                    self._index[key] = upper.index(want)
                    break
        # Every unit column present, in COLUMNS order, for cell() to coalesce
        # across. A non-detect reports no result and no RESULT_UNIT, only the
        # limit and ITS unit in REPORT_RESULT_UNIT. Taking the first column
        # that existed read every live non-detect as unitless where the same
        # row from parquet read ug/L (ReportStore.from_parquet coalesces),
        # and hid a 1,000x unit mix from the guard: Lead at 3 ug/L beside a
        # non-detect at 0.002 mg/L read as ug/L and blank (found 2026-10-29,
        # tools/test_store_parity.py's unit checks).
        self._unit_at = [upper.index(want) for want in COLUMNS["unit"]
                         if want in upper]
        # Row lookup by chemical, and by (chemical, location). Built on first
        # use rather than here: loading a report should not pay for an index
        # nobody asks for, and reports that cannot drive the map are discarded
        # immediately after construction.
        self._by_chemical = None
        self._by_chemical_location = None
        # The rows every question is asked of: `rows` narrowed by the row
        # scope (see set_row_scope). `rows` itself stays whole, because the
        # store build and the cache write read it and must see everything.
        self._scope = NO_ROW_SCOPE
        self._view = self.rows
        # Parsed sample dates, aligned with `rows`. Filled on the first dated
        # scope: parse_date is ~5 us a row, which is seconds on a real report
        # and should be paid once, not on every change of the date range.
        self._dates = None
        # depth_extent() per matrix. It reads `rows`, which never change, and
        # the panel asks again on every change of chemical. units_in() the
        # same.
        self._depth_extents = {}
        self._unit_offers = {}

    # ----------------------------------------------------------- row scope

    @property
    def scope(self):
        return self._scope

    def set_row_scope(self, start=None, end=None, sample_types=None,
                      depth=None, units=None):
        """Narrow every later question to rows in a date range and of a type.

        `start` and `end` are dates, either may be None for an open end, and
        both are inclusive. `sample_types` is a collection of codes ("N"), or
        empty for every type. `depth` is (top, bottom, unit), or None.
        `units` are the result units kept, or empty for every unit.
        Returns True when the scope actually changed, so a caller knows
        whether anything it showed is now out of date.

        The rules, which ReportStore.set_row_scope must match exactly and
        tools/test_store_parity.py holds it to:

        - A date bound excludes UNDATED rows. A row with no date cannot be
          placed inside a range, and EQuIS's own @start_date drops them too.
        - A depth filter excludes rows with no depth, and keeps a row only
          when its whole interval lies inside the bounds, converted into the
          filter's unit (in_depth).
        - A filter on a column the report lacks matches everything, as for
          matrix and method: dropping every row is the worse failure.
        - Sample types compare case-insensitively; a blank type is not "N".
        - Units compare exactly, trimmed, as cell("unit") coalesces them.
        """
        scope = row_scope(start, end, sample_types, depth, units)
        if scope == self._scope:
            return False
        self._scope = scope
        start, end, codes, depth, units = scope
        dated = (start is not None or end is not None) and self.column("date") >= 0
        typed = bool(codes) and self.column("sample_type") >= 0
        deep = depth is not None and self._has_depth()
        united = bool(units) and bool(self._unit_at)
        if not dated and not typed and not deep and not united:
            self._view = self.rows
        else:
            if dated and self._dates is None:
                self._dates = []
                for row in self.rows:
                    when = parse_date(self.cell(row, "date"))
                    self._dates.append(when.date() if when else None)
            view = []
            for position, row in enumerate(self.rows):
                if dated:
                    when = self._dates[position]
                    if when is None:
                        continue
                    if start is not None and when < start:
                        continue
                    if end is not None and when > end:
                        continue
                if typed and self.cell(row, "sample_type").upper() not in codes:
                    continue
                if deep and not in_depth(
                        depth, parse_number(self.cell(row, "start_depth")),
                        parse_number(self.cell(row, "end_depth")),
                        self.cell(row, "depth_unit")):
                    continue
                if united and self.cell(row, "unit") not in units:
                    continue
                view.append(row)
            self._view = view
        # The index holds positions into the view, so it goes with it.
        self._by_chemical = None
        self._by_chemical_location = None
        return True

    def _has_depth(self):
        return self.column("start_depth") >= 0 or self.column("end_depth") >= 0

    def depth_extent(self, matrix=ALL_MATRICES):
        """The depths a depth filter can offer: see depth_extent_from.

        Over every row, NOT the scoped view: the range the slider spans must
        not shrink as the slider narrows it, or the filter could only ever
        be tightened. Narrowed by matrix, since sediment in cm and a well in
        m want different ranges and different units.
        """
        if not self._has_depth():
            return None
        key = matrix_key(matrix)
        if key not in self._depth_extents:
            self._depth_extents[key] = self._depth_extent(matrix)
        return self._depth_extents[key]

    def _depth_extent(self, matrix):
        groups = {}
        boundaries = set()
        for row in self.rows:
            if not self._in_matrix(row, matrix):
                continue
            interval = depth_interval(
                parse_number(self.cell(row, "start_depth")),
                parse_number(self.cell(row, "end_depth")))
            if interval is None:
                continue
            unit = depth_unit(self.cell(row, "depth_unit"))
            count, top, bottom = groups.get(unit, (0, interval[0], interval[1]))
            groups[unit] = (count + 1, min(top, interval[0]),
                            max(bottom, interval[1]))
            boundaries.add((unit, interval[0]))
            boundaries.add((unit, interval[1]))
        return depth_extent_from(
            [(unit, n, top, bottom) for unit, (n, top, bottom) in groups.items()],
            boundaries)

    def units_in(self, matrix=ALL_MATRICES):
        """[(unit, rows)] for a unit filter to offer, commonest first.

        Over every row in the matrix, not the scoped view, for depth_extent's
        reason: ticking a unit must not take the others off the list. Every
        row counts, value or not - a row the filter would drop is a row it
        should name. "" is the unit of rows with none.
        """
        if not self._unit_at:
            return []
        key = matrix_key(matrix)
        if key not in self._unit_offers:
            counts = {}
            for row in self.rows:
                if self._in_matrix(row, matrix):
                    unit = self.cell(row, "unit")
                    counts[unit] = counts.get(unit, 0) + 1
            self._unit_offers[key] = sorted(
                counts.items(), key=lambda item: (-item[1], item[0]))
        return self._unit_offers[key]

    # --------------------------------------------------------------- lookup

    def _build_row_index(self):
        """Group row positions by chemical and by chemical+location.

        Without this every aggregate and series call scans the whole report.
        That is affordable once but not per location: drawing sparklines for
        300 locations means 300 scans of ~460,000 rows, which takes the best
        part of a minute. Indexing costs a few seconds and about 20 MB once,
        then answers each lookup in microseconds.
        """
        if self._by_chemical is not None:
            return
        chemical_at = self._index.get("chemical", -1)
        location_at = self._index.get("location", -1)
        by_chemical = {}
        by_pair = {}
        if chemical_at < 0:
            # Nothing to group by; every lookup falls back to a full scan.
            self._by_chemical, self._by_chemical_location = {}, {}
            return
        cas_at = self._index.get("cas_rn", -1)
        for position, row in enumerate(self._view):
            if chemical_at >= len(row):
                continue
            name = (row[chemical_at] or "").strip()
            if not name:
                continue
            cas = ((row[cas_at] or "").strip()
                   if 0 <= cas_at < len(row) else "")
            # Keyed on both spellings the matcher accepts, so a lookup by CAS
            # and a lookup by name reach the same rows.
            by_chemical.setdefault(name, []).append(position)
            if cas:
                by_chemical.setdefault(("cas", cas), []).append(position)
            if location_at >= 0 and location_at < len(row):
                code = (row[location_at] or "").strip()
                if code:
                    by_pair.setdefault((name, code), []).append(position)
                    if cas:
                        by_pair.setdefault((("cas", cas), code), []).append(position)
        self._by_chemical = by_chemical
        self._by_chemical_location = by_pair

    def _candidate_rows(self, chemical, cas_rn=None, location=None):
        """Rows that could match, cheaply narrowed before the exact test.

        Falls back to every row when the report has no chemical column, so the
        callers keep working on reports this cannot index.
        """
        self._build_row_index()
        if not self._by_chemical:
            return self._view
        # Prefer CAS when the report carries one: it is the more reliable
        # identifier and _matches() treats it as authoritative.
        keys = []
        if cas_rn and self.column("cas_rn") >= 0:
            keys.append(("cas", cas_rn))
        if chemical:
            keys.append(chemical)
        for key in keys:
            if location is not None:
                positions = self._by_chemical_location.get((key, location))
            else:
                positions = self._by_chemical.get(key)
            if positions:
                return [self._view[p] for p in positions]
        return []

    # ------------------------------------------------------------- columns

    def column(self, key):
        return self._index.get(key, -1)

    def has(self, *keys):
        return all(self._index.get(k, -1) >= 0 for k in keys)

    @property
    def missing_for_analysis(self):
        """Columns needed for the chemical workflow that this report lacks."""
        return missing_required(lambda key: self._index.get(key, -1) >= 0)

    def report_fields(self):
        """[(header, type)] of the report's own columns, in its own order.

        What a map layer carries beside the canonical result fields, so a
        feature holds everything the report said about its row - sample code,
        qualifiers, limits, lab, SDG - not just the dozen columns the rules
        read. Every column is text here: a report arrives as text, and typing
        it would be guessing at what "<0.5" or "ND" in a numeric column
        meant. ReportStore.report_fields() types them when the source does.
        """
        return [(header, "VARCHAR") for header in self.headers]

    def report_cells(self, row):
        """One row's report columns, aligned with report_fields().

        An empty cell is None, because "the report said nothing" is NULL in
        a layer's attribute table, and ReportStore's raw columns read it the
        same way. Nothing else is touched: no stripping, no parsing, since
        these are the report's own words.
        """
        width = len(self.headers)
        cells = [None if cell in (None, "") else cell for cell in row[:width]]
        return cells + [None] * (width - len(cells))

    def sample_fields(self, samples, matrix=ALL_MATRICES):
        """The sample's own report columns, for a pivot's points.

        A scatter or ternary point is one sample, not one row: it stands for
        every result that sample has, so only SAMPLE_COLUMNS belong on its
        layer - never a result's value, method or qualifier, which would put
        one analyte's beside another's.

        A sample column that disagrees within any of `samples` - across its
        in-scope rows, an empty cell counting as a value - is left off the
        layer rather than shown with one row's value. EQuIS gives a sample
        one value, so this is a guard, not an expectation.

        Returns (report_fields() of the kept columns, {sample: cells}).
        """
        wanted = {str(code).strip() for code in samples or () if code}
        listed = [index for index, header in enumerate(self.headers)
                  if is_sample_column(header)]
        if self.column("sample") < 0 or not wanted or not listed:
            return [], {}
        seen = {}
        for row in self._view:
            code = self.cell(row, "sample")
            if code in wanted and self._in_matrix(row, matrix):
                cells = self.report_cells(row)
                seen.setdefault(code, []).append([cells[i] for i in listed])
        if not seen:
            return [], {}
        kept = [n for n in range(len(listed))
                if all(len({cells[n] for cells in rows}) <= 1
                       for rows in seen.values())]
        fields = self.report_fields()
        return ([fields[listed[n]] for n in kept],
                {code: [rows[0][n] for n in kept]
                 for code, rows in seen.items()})

    def cell(self, row, key):
        if key == "unit":
            for index in self._unit_at:
                if index < len(row):
                    unit = (row[index] or "").strip()
                    if unit:
                        return unit
            return ""
        index = self._index.get(key, -1)
        if index < 0 or index >= len(row):
            return ""
        return (row[index] or "").strip()

    # ------------------------------------------------------------ analytes

    def analytes(self, matrix=ALL_MATRICES):
        """Unique (chemical_name, cas_rn) pairs with their result counts.

        Sorted by name. CAS RN is carried through because the same chemical can
        appear under more than one name, and vice versa. `matrix` restricts the
        list to one matrix, so the counts shown match what a map of that matrix
        would actually draw.
        """
        counts = {}
        for row in self._view:
            if not self._in_matrix(row, matrix):
                continue
            name = self.cell(row, "chemical")
            if not name:
                continue
            cas = self.cell(row, "cas_rn")
            key = (name, cas)
            counts[key] = counts.get(key, 0) + 1
        out = [
            {"chemical": name, "cas_rn": cas, "count": count}
            for (name, cas), count in counts.items()
        ]
        out.sort(key=lambda a: a["chemical"].lower())
        return out

    def matrices(self):
        """Unique matrix codes with their row counts, most common first.

        Rows with no matrix code are grouped under "" so the dialog can say
        how many results it cannot place in a matrix, rather than hiding them.
        """
        if self.column("matrix") < 0:
            return []
        counts = {}
        for row in self._view:
            code = self.cell(row, "matrix").upper()
            counts[code] = counts.get(code, 0) + 1
        out = [{"matrix": code, "count": count}
               for code, count in counts.items()]
        # Most common first: the matrix a report is mostly about should be the
        # easiest to reach, and a stray handful of rows should not head the list.
        out.sort(key=lambda m: (-m["count"], m["matrix"]))
        return out

    def mixed_fractions(self):
        """Fractions present per matrix, where more than one exists.

        Returns {matrix_code: [fraction, ...]} for the matrices that carry
        several. Fraction cannot be chosen safely up front - the parameter
        service reports the same codes whatever matrix is asked for, and a
        matrix that uses only one (sediment is N) returns nothing at all if
        another is demanded. So the mixing is detected here, once the rows are
        in hand and the answer is observable rather than guessed.
        """
        matrix_at = self.column("matrix")
        fraction_at = self.column("fraction")
        if fraction_at < 0:
            return {}
        seen = {}
        for row in self._view:
            if fraction_at >= len(row):
                continue
            fraction = (row[fraction_at] or "").strip().upper()
            if not fraction:
                continue
            key = ""
            if 0 <= matrix_at < len(row):
                key = (row[matrix_at] or "").strip().upper()
            seen.setdefault(key, set()).add(fraction)
        return {matrix: sorted(fractions)
                for matrix, fractions in seen.items() if len(fractions) > 1}

    def _in_matrix(self, row, matrix):
        """Whether a row belongs to the requested matrix or matrices.

        `matrix` is a single code or any collection of them - sediment and
        tailings are both solids and a user may reasonably want them on one
        map, while water never belongs beside either.

        Compared case-insensitively because the same code is spelled both ways
        across reports. A report with no matrix column matches everything: the
        filter cannot be honoured, and dropping every row would be worse than
        ignoring a filter the data cannot express.
        """
        if matrix is ALL_MATRICES:
            return True
        if self.column("matrix") < 0:
            return True
        found = self.cell(row, "matrix").upper()
        if isinstance(matrix, (list, tuple, set, frozenset)):
            wanted = {str(m).upper() for m in matrix if str(m or "").strip()}
            # An empty collection is "no filter", matching ALL_MATRICES rather
            # than excluding every row.
            return not wanted or found in wanted
        return found == (matrix or "").upper()

    def _in_method(self, row, method):
        """Whether a row was produced by one of the requested methods.

        Accepts a single code or a collection, like _in_matrix. A report with
        no method column matches everything: the filter cannot be honoured,
        and dropping every row would be worse than ignoring it.
        """
        if method is ALL_METHODS:
            return True
        if self.column("method") < 0:
            return True
        found = self.cell(row, "method").upper()
        if isinstance(method, (list, tuple, set, frozenset)):
            wanted = {str(m).upper() for m in method if str(m or "").strip()}
            return not wanted or found in wanted
        return found == (method or "").upper()

    def methods(self, chemical=None, cas_rn=None, matrix=ALL_MATRICES):
        """Distinct analytic methods, with how many results each produced.

        Scoped to one analyte when given, because which methods are in play
        is analyte-specific: a metals suite and an organics suite share a
        report but never a result. Most-used first, so the picker leads with
        the method the data is mostly about.
        """
        if self.column("method") < 0:
            return []
        counts = {}
        rows = (self._candidate_rows(chemical, cas_rn) if chemical
                else self._view)
        for row in rows:
            if chemical and not self._matches(row, chemical, cas_rn):
                continue
            if not self._in_matrix(row, matrix):
                continue
            code = self.cell(row, "method").upper()
            if code:
                counts[code] = counts.get(code, 0) + 1
        out = [{"method": code, "count": count}
               for code, count in counts.items()]
        out.sort(key=lambda m: (-m["count"], m["method"]))
        return out

    def _in_fraction(self, row, fraction):
        """Whether a row belongs to the requested fraction.

        Mirrors _in_matrix, including the "no such column matches everything"
        rule: refusing every row because the report cannot express the filter
        would be a worse failure than ignoring the filter. Accepts a single
        code or a collection - dissolved and total can legitimately be
        compared side by side, they just must not be aggregated together.
        """
        if fraction is ANY_FRACTION:
            return True
        if self.column("fraction") < 0:
            return True
        found = self.cell(row, "fraction").upper()
        if isinstance(fraction, (list, tuple, set, frozenset)):
            wanted = {str(f).upper() for f in fraction if str(f or "").strip()}
            return not wanted or found in wanted
        return found == (fraction or "").upper()

    def fractions_for(self, chemical, cas_rn=None, matrix=ALL_MATRICES):
        """The distinct fraction codes one analyte carries in one matrix.

        Narrower than mixed_fractions(), which answers for the whole report.
        A report can mix fractions overall while the chemical being mapped
        carries only one, and refusing to map that chemical would be a false
        alarm - so the question has to be asked per analyte, per matrix.

        Returns a sorted list of codes; [] when the report has no fraction
        column or no rows match.
        """
        if self.column("fraction") < 0:
            return []
        found = set()
        for row in self._candidate_rows(chemical, cas_rn):
            if not self._matches(row, chemical, cas_rn):
                continue
            if not self._in_matrix(row, matrix):
                continue
            code = self.cell(row, "fraction").upper()
            if code:
                found.add(code)
        return sorted(found)

    def is_detected(self, row):
        return self.cell(row, "detect").upper() in ("Y", "YES", "TRUE", "1")

    def row_value(self, row, non_detect=ND_HALF):
        """The plottable value for a row, and whether it is a detection limit.

        Non-detects leave RESULT_NUMERIC empty and report only the limit they
        fell below, so reading RESULT_NUMERIC alone drops them silently.
        `non_detect` picks the convention: ND_HALF plots them at half the
        limit, ND_FULL at the limit, ND_EXCLUDE drops them.
        """
        detected = self.is_detected(row)
        value = parse_number(self.cell(row, "value"))
        if value is not None and detected:
            return value, False

        # A non-detect: take whatever limit the report carries.
        limit = value
        if limit is None:
            for key in ("reported", "limit"):
                limit = parse_number(self.cell(row, key))
                if limit is not None:
                    break
        if limit is None:
            return None, False
        if not detected:
            if non_detect == ND_EXCLUDE:
                return None, True
            if non_detect == ND_HALF:
                return limit / 2.0, True
            return limit, True
        return limit, False

    def _matches(self, row, chemical, cas_rn):
        if cas_rn:
            # CAS RN is the more reliable identifier when the report has one.
            if self.cell(row, "cas_rn") == cas_rn:
                return True
            if self.column("cas_rn") >= 0:
                return False
        return self.cell(row, "chemical") == chemical

    # ---------------------------------------------------------- aggregates

    def aggregate(self, chemical, cas_rn=None, how=MOST_RECENT,
                  detected_only=False, non_detect=ND_HALF,
                  matrix=ALL_MATRICES, fraction=ANY_FRACTION,
                  method=ALL_METHODS, fields=False):
        """Per-location summary of one analyte.

        Returns {location_code: {value, unit, date, detect, count, ...}}.
        With `fields`, each entry also carries "fields": report_cells() of
        the row that WON - the one sample the mapped number came from, so
        its sample code, qualifiers and lab are that number's, never a mix
        from several rows - or None where no row had a usable number.
        Rows without a usable number are counted but cannot win the aggregate.
        `matrix` restricts the rows considered: without it a maximum can be
        drawn from a soil result and plotted next to groundwater criteria.
        `fraction` does the same for dissolved vs total, which interleave for
        the same location, chemical and day: a maximum taken across both
        reports whichever fraction happened to be higher, which for one real
        sample can be an order of magnitude apart.
        """
        summary = {}
        for row in self._candidate_rows(chemical, cas_rn):
            if not self._matches(row, chemical, cas_rn):
                continue
            if not self._in_matrix(row, matrix):
                continue
            if not self._in_fraction(row, fraction):
                continue
            if not self._in_method(row, method):
                continue
            location = self.cell(row, "location")
            if not location:
                continue
            detect = self.cell(row, "detect").upper()
            if detected_only and detect not in ("Y", "YES", "TRUE", "1"):
                continue

            entry = summary.setdefault(
                location,
                {
                    "location": location,
                    "value": None,
                    "unit": "",
                    "date": None,
                    "detect": "",
                    "at_limit": False,
                    "count": 0,
                    "detect_count": 0,
                },
            )
            if fields and "fields" not in entry:
                entry["fields"] = None
            entry["count"] += 1
            if detect in ("Y", "YES", "TRUE", "1"):
                entry["detect_count"] += 1

            value, at_limit = self.row_value(row, non_detect=non_detect)
            if value is None:
                continue
            when = parse_date(self.cell(row, "date"))

            if how == MAXIMUM:
                # A NON-DETECT MUST NEVER BE THE MAXIMUM while a real
                # detection exists at that location. A non-detect carries
                # only the limit it fell BELOW, so `<50` substituted at
                # half its limit is 25 - a number about laboratory
                # sensitivity, not about the site. Measured on Facility A
                # 2026-09-28: 759 cells were reporting one, the worst
                # overstating a location's real maximum by 1,250x.
                #
                # A detection always beats a non-detect; among equals the
                # larger value wins. A location with only non-detects still
                # reports one, marked at_limit - "we looked and found
                # nothing" is a result worth mapping.
                # DETECT_FLAG, the same fact the store's `detected`
                # column carries - NOT `at_limit`, which is narrower: a row
                # can be undetected and still have no limit to stand in for
                # it, and the two implementations would then rank it
                # differently. CLAUDE.md is explicit that these are
                # different facts.
                is_detect = detect in ("Y", "YES", "TRUE", "1")
                was_detect = entry.get("detect", "") in ("Y", "YES", "TRUE", "1")
                if entry["value"] is None:
                    better = True
                elif is_detect != was_detect:
                    better = is_detect
                else:
                    better = value > entry["value"]
            else:
                # Most recent: undated rows only fill an empty slot, so a dated
                # result always wins over one we cannot place in time.
                if entry["value"] is None:
                    better = True
                elif when is None:
                    better = False
                elif entry["date"] is None:
                    better = True
                else:
                    better = when > entry["date"]

            if better:
                entry["value"] = value
                entry["unit"] = self.cell(row, "unit")
                entry["date"] = when
                entry["detect"] = detect
                entry["at_limit"] = at_limit
                if fields:
                    entry["fields"] = self.report_cells(row)
        return summary

    def units_for(self, chemical, cas_rn=None, detected_only=False,
                  non_detect=ND_HALF, matrix=ALL_MATRICES,
                  fraction=ANY_FRACTION, method=ALL_METHODS):
        """The units this selection reports in, commonest first.

        ReportStore.units_for's twin, for the mixed-unit guard. Without it
        the guard was skipped on a live or cached report until its store
        landed in the background, so a map built in those first seconds was
        unchecked (found 2026-10-28). Same predicate as the store: the
        filters, a usable value under `non_detect`, no location required.
        Returns [(unit, rows), ...], '' for a row with no unit at all.
        """
        if self.column("unit") < 0:
            return []
        counts = {}
        for row in self._candidate_rows(chemical, cas_rn):
            if not self._matches(row, chemical, cas_rn):
                continue
            if not (self._in_matrix(row, matrix)
                    and self._in_fraction(row, fraction)
                    and self._in_method(row, method)):
                continue
            if detected_only and not self.is_detected(row):
                continue
            if self.row_value(row, non_detect=non_detect)[0] is None:
                continue
            unit = self.cell(row, "unit")
            counts[unit] = counts.get(unit, 0) + 1
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    def rows_for(self, chemical, cas_rn=None, detected_only=False,
                 non_detect=ND_HALF, matrix=ALL_MATRICES,
                 fraction=ANY_FRACTION, method=ALL_METHODS, limit=0,
                 fields=False):
        """Every result for one analyte, one dict per row.

        The unaggregated counterpart to aggregate(): where that collapses a
        location's history to a single winning row - discarding the sample
        dates, the matrix and the fraction along with it - this keeps them
        all, so one map feature can be one result.

        Returns a list of {location, value, unit, date, detect, at_limit,
        matrix, fraction, sample_type, seq}, sorted by location then date,
        and with `fields`, "fields": the row's report_cells().
        `seq` is the row's 0-based ordinal within its own location, which is
        what lets a caller tell the members of one stack apart.

        Rows with no usable number are dropped rather than carried as blanks:
        a feature with no value cannot be styled and would sit on top of the
        rows that do have one.

        `limit` caps the result count (0 means no cap). Whole locations are
        dropped to stay under it, never part of a location's history - a
        truncated stack would misstate how many results a location has, in
        the same way a truncated report makes a chemical's history appear to
        end early. Returns (rows, dropped_locations).
        """
        collected = {}
        for row in self._candidate_rows(chemical, cas_rn):
            if not self._matches(row, chemical, cas_rn):
                continue
            if not self._in_matrix(row, matrix):
                continue
            if not self._in_fraction(row, fraction):
                continue
            if not self._in_method(row, method):
                continue
            location = self.cell(row, "location")
            if not location:
                continue
            detect = self.cell(row, "detect").upper()
            if detected_only and detect not in ("Y", "YES", "TRUE", "1"):
                continue
            value, at_limit = self.row_value(row, non_detect=non_detect)
            if value is None:
                continue
            entry = {
                "location": location,
                "value": value,
                "unit": self.cell(row, "unit"),
                "date": parse_date(self.cell(row, "date")),
                "detect": detect,
                "at_limit": at_limit,
                "matrix": self.cell(row, "matrix").upper(),
                "fraction": self.cell(row, "fraction").upper(),
                "sample_type": self.cell(row, "sample_type").upper(),
                "start_depth": parse_number(self.cell(row, "start_depth")),
                "end_depth": parse_number(self.cell(row, "end_depth")),
                "depth_unit": self.cell(row, "depth_unit"),
                "custom_1": self.cell(row, "custom_1"),
                "method": self.cell(row, "method").upper(),
            }
            if fields:
                entry["fields"] = self.report_cells(row)
            collected.setdefault(location, []).append(entry)

        dropped = 0
        if limit and sum(len(v) for v in collected.values()) > limit:
            # Keep the locations with the most history: they are the ones a
            # per-result view is actually for, and dropping the sparse ones
            # loses the least.
            ordered = sorted(collected.items(),
                             key=lambda kv: (-len(kv[1]), kv[0]))
            kept, total = {}, 0
            for code, rows in ordered:
                if total + len(rows) > limit:
                    dropped += 1
                    continue
                kept[code] = rows
                total += len(rows)
            collected = kept

        out = []
        for code in sorted(collected):
            rows = collected[code]
            # Undated rows sort last rather than crashing the comparison, and
            # keep a stable order among themselves.
            rows.sort(key=lambda r: (r["date"] is None, r["date"] or _EPOCH))
            for seq, entry in enumerate(rows):
                entry["seq"] = seq
                out.append(entry)
        return out, dropped

    def filtered_rows(self, analytes=None, matrix=ALL_MATRICES,
                      fraction=ANY_FRACTION, method=ALL_METHODS,
                      locations=None):
        """The report rows behind a mapped layer, with every filter applied.

        This is the data the map was built FROM, not the map's own features:
        every column the report returned, one row per result, after the same
        matrix / fraction / method / location filtering the layer used. An
        aggregated layer collapses these to one value per location, so the
        rows here are what that value was computed over - which is exactly
        what someone checking a number on the map needs to see.

        `analytes` is a list of {chemical, cas_rn} dicts, or None for every
        analyte in the table. Rows keep the report's own column order, so the
        export is recognisably the report it came from.

        Returns (headers, rows). Rows are ordered by analyte, then location,
        then date, so a combined export reads as a table rather than as the
        order the layers happened to be built in.
        """
        wanted = list(analytes or [])
        if wanted:
            candidates = []
            seen = set()
            for analyte in wanted:
                chemical = (analyte or {}).get("chemical")
                cas = (analyte or {}).get("cas_rn")
                for row in self._candidate_rows(chemical, cas):
                    if not self._matches(row, chemical, cas):
                        continue
                    # One row can match two analyte entries only if they name
                    # the same chemical, but identity by position keeps the
                    # export honest either way.
                    marker = id(row)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    candidates.append(row)
        else:
            candidates = list(self._view)

        keep = set(locations or [])
        out = []
        for row in candidates:
            if not self._in_matrix(row, matrix):
                continue
            if not self._in_fraction(row, fraction):
                continue
            if not self._in_method(row, method):
                continue
            if keep and self.cell(row, "location") not in keep:
                continue
            out.append(row)

        def order(row):
            # Dates sort as text when unparseable, which is stable if not
            # chronological - better than dropping a row from the export for
            # having a date the parser does not recognise.
            when = parse_date(self.cell(row, "date"))
            return (self.cell(row, "chemical").upper(),
                    self.cell(row, "location").upper(),
                    when.isoformat() if when else self.cell(row, "date"))

        out.sort(key=order)
        return list(self.headers), out

    def series(self, location, chemical, cas_rn=None, non_detect=ND_HALF,
               matrix=ALL_MATRICES, fraction=ANY_FRACTION,
               method=ALL_METHODS):
        """Time series for one location and analyte, oldest first.

        Returns a list of {date, value, unit, detect}; rows without both a date
        and a value are dropped since they cannot be plotted. `matrix` keeps
        the trend within one matrix, so a well sampled for both water and soil
        does not plot as one implausibly jagged line. `fraction` does the same
        for dissolved vs total, which are sampled on the same days and would
        otherwise saw up and down against each other.
        """
        points = []
        for row in self._candidate_rows(chemical, cas_rn, location=location):
            if self.cell(row, "location") != location:
                continue
            if not self._matches(row, chemical, cas_rn):
                continue
            if not self._in_matrix(row, matrix):
                continue
            if not self._in_fraction(row, fraction):
                continue
            if not self._in_method(row, method):
                continue
            when = parse_date(self.cell(row, "date"))
            value, at_limit = self.row_value(row, non_detect=non_detect)
            if when is None or value is None:
                continue
            points.append(
                {
                    "date": when,
                    "value": value,
                    "unit": self.cell(row, "unit"),
                    "detect": self.cell(row, "detect").upper(),
                    # True when the value shown is a detection limit rather
                    # than a measured concentration.
                    "at_limit": at_limit,
                }
            )
        points.sort(key=lambda p: p["date"])
        return points

    def series_many(self, locations, chemical, cas_rn=None,
                    non_detect=ND_HALF, matrix=ALL_MATRICES,
                    fraction=ANY_FRACTION, method=ALL_METHODS):
        """Several locations' series at once: {location: [points, ...]}.

        A plain loop here, because a dict lookup per location is already what
        this class is fast at. It exists so that CALLERS can ask one question
        instead of many - which costs nothing here and is the difference
        between 52 ms and 1.44 s on the DuckDB store, where every query pays a
        fixed parse-plan-execute cost. ReportStore.series_many answers it as a
        single grouped scan.

        Keeping both classes to one interface is what lets the panel hold a
        single `_analysis` reference and never ask which it has.

        `locations` may be None, meaning every location with results.
        """
        codes = (list(locations) if locations is not None
                 else self.locations(matrix=matrix))
        out = {}
        for code in codes:
            out[str(code)] = self.series(
                str(code), chemical, cas_rn, non_detect=non_detect,
                matrix=matrix, fraction=fraction, method=method)
        return out

    def locations(self, matrix=ALL_MATRICES):
        """Every location code mentioned by the report."""
        seen = set()
        for row in self._view:
            if not self._in_matrix(row, matrix):
                continue
            code = self.cell(row, "location")
            if code:
                seen.add(code)
        return sorted(seen)
