"""What subset of a facility's chemistry to fetch, and how to ask for it.

EQuIS only applies a report filter that is SAVED on the report: passing one as
a query parameter is silently ignored and the report runs unfiltered, which on
a large facility means a 230 s timeout rather than a wrong answer. So the live
path writes these values onto a report before running it, and this module is
the translation between what the user picked and what EQuIS stores.

No QGIS imports, so this is testable with a plain interpreter.
"""

# Report parameters this maps onto, on Analytical Results II (its base report,
# found by name - ids are per tenant, never assumed).
# Names must match exactly - '@cas_rn' and '@analytes' are both 400s.
PARAM_FACILITY = "@facility_id"
PARAM_ANALYTES = "@cas_rns"
PARAM_LOCATIONS = "@sys_loc_codes"
# Location groups (RT_GROUP with a location member type). Expanded to their
# member locations by the server, so the plugin never needs the membership.
# Verified 2026-09-05 on the ARII base: MultiSelect, caption "Location\Group(s):",
# depends on @facility_id like every other list here. Most facilities define
# none, so the picker is shown only when the parameter service returns some.
PARAM_LOCATION_GROUPS = "@location_groups"
PARAM_MATRICES = "@matrix_codes"
PARAM_FRACTIONS = "@fractions"
# Verified 2026-09-02 on the ARII base report: it DOES carry a MultiSelect
# analytic-method parameter, alongside '@prep_methods'. The plugin used to
# filter methods client-side only, on the belief that no such parameter
# existed - so ticking a method narrowed the view but not the query, and did
# nothing for the ~230 s ceiling it looks like it should help with.
PARAM_METHODS = "@analytic_methods"
PARAM_EXTRA_FIELDS = "@extra_fields"
PARAM_SAMPLE_TYPES = "@sample_types"
PARAM_START = "@start_date"
PARAM_END = "@end_date"

# Columns the plugin wants that the report does not emit by default.
# START_DEPTH/END_DEPTH/DEPTH_UNIT are deliberately NOT here: verified
# 2026-09-01 that Analytical Results II already returns them, and asking
# again through @extra_fields only appends a lowercased, table-prefixed
# duplicate (dt_sample_start_depth) alongside the real column.
# DT_TEST.IDENTIFIER is not in the default output; it is among the 956
# fields the ARII base offers here (read 2026-10-17). DT_TEST.LAB_SDG is
# deliberately NOT asked for: the default output already carries LAB_SDG,
# and asking again would only append a duplicate, as depth did.
EXTRA_FIELDS = ("DT_SAMPLE.CUSTOM_FIELD_1", "DT_TEST.IDENTIFIER")

# Multi-value parameters are PIPE-separated. A comma-separated list is
# accepted with HTTP 200 and returns ZERO ROWS, which is the dangerous
# failure: it looks like it worked.
LIST_SEPARATOR = "|"

# Dissolved and total results interleave for the same location, chemical and
# date, and differ by up to ~29x, so aggregating across them is meaningless -
# but only where both exist. Fraction is a property of the analysis, not of
# the facility: measured on one site, surface water carries D and T while
# sediment carries only N, so defaulting to dissolved returns ZERO ROWS for
# sediment.
#
# The parameter service cannot help here: @fractions answers with the same
# seven codes whatever matrix is saved on the report, so the list cannot be
# narrowed to what a matrix actually holds without fetching data first.
# Verified 2026-09-02 that this is general, not a quirk of @fractions: every
# filter parameter declares dependencies:['@facility_id'] and nothing else,
# and a report with an analyte AND matrix saved returns byte-identical lists
# to an unfiltered one on the same facility. So no picker can ever be
# narrowed by another's answer - only annotated once rows are in hand.
#
# So the default is unfiltered, and the mixing is dealt with where the
# evidence is - once rows are in hand, report_data can see whether a chemical
# really carries two fractions and say so. An empty map with no explanation is
# a worse failure than a mixed one that announces itself.
FRACTION_DISSOLVED = "D"
FRACTION_TOTAL = "T"
FRACTION_NONE = "N"
FRACTIONS = (
    (FRACTION_DISSOLVED, "Dissolved"),
    (FRACTION_TOTAL, "Total"),
    (FRACTION_NONE, "Not applicable (N)"),
)

# Equipment blanks and field duplicates are QC, not results to map: they were
# 8,300 of 15,858 rows in one report, and blanks are near-zero by
# construction, so including them drags any aggregate down.
SAMPLE_TYPE_NORMAL = "N"

# Sample types that are QC rather than results. Shown so the user can see what
# is being excluded rather than discovering it as a row-count discrepancy.
QC_SAMPLE_TYPES = {
    "EB": "Equipment Blank",
    "FD": "Field Duplicate",
    "TB": "Trip Blank",
    "FB": "Field Blank",
}


def encode_list(values):
    """Encode a multi-value parameter. Empty means "no filter", not "none"."""
    items = [str(v).strip() for v in (values or []) if str(v or "").strip()]
    if not items:
        return None
    # De-duplicate while keeping the caller's order, so a value picked twice
    # in the UI does not become a doubled filter.
    seen = set()
    unique = []
    for item in items:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return LIST_SEPARATOR.join(unique)


def merge_extra_fields(current, wanted=EXTRA_FIELDS):
    """Add the columns the plugin needs to whatever the report already asks for.

    @extra_fields is a pipe-separated MultiSelect of TABLE.COLUMN names. It is
    the one parameter the plugin ADDS to rather than replaces: the report
    carries its own choices and a user may have added more, and clobbering
    them would silently change what everyone else gets out of the report.
    """
    have = decode_list(current)
    seen = {item.upper() for item in have}
    for column in wanted:
        if column.upper() not in seen:
            have.append(column)
            seen.add(column.upper())
    return LIST_SEPARATOR.join(have) if have else None


def decode_list(text):
    """Split a stored parameter value back into a list."""
    if text is None:
        return []
    return [part.strip() for part in str(text).split(LIST_SEPARATOR)
            if part.strip()]


def encode_date(value):
    """Encode a date for a report parameter, or None.

    Accepts anything with .strftime (date/datetime) or a string already in
    the report's format, which is what a round-trip through a saved report
    hands back.
    """
    if value is None or value == "":
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%dT00:00:00")
    return str(value)


class DataSelection(object):
    """The subset of a facility's chemistry to work with.

    The same selection drives both data sources, but it means different things
    in each, and that difference is not cosmetic:

      live - the values are SAVED onto a report and shape what EQuIS
             generates, which is what keeps a large facility under the
             230 s ceiling.
      agent - an information agent's stored output is frozen from when it ran
             and cannot be re-scoped, so the same values filter the rows
             client-side after they arrive.
    """

    def __init__(self, facility_id=None, analytes=None, locations=None,
                 matrices=None, fraction=None, fractions=None, methods=None,
                 sample_types=(SAMPLE_TYPE_NORMAL,), start=None, end=None,
                 location_groups=None):
        self.facility_id = facility_id
        self.analytes = list(analytes or [])
        self.locations = list(locations or [])
        # Group codes, resolved to locations by the server. Not applied in
        # matches(): a stored agent output carries location codes, not the
        # groups they belong to, so there is nothing to test a row against.
        self.location_groups = list(location_groups or [])
        self.matrices = list(matrices or [])
        # A list now: dissolved and total are two genuine analyses and
        # comparing them is legitimate. What must not happen is aggregating
        # ACROSS them, which the build gate refuses - so this carries the
        # selection and the gate decides whether it is safe to aggregate.
        # `fraction` (singular) is still accepted so existing callers and
        # saved selections keep working.
        self.fractions = list(fractions if fractions is not None
                              else ([fraction] if fraction else []))
        # Methods ARE a list: two analyses of one sample are both real, and
        # comparing them is the point rather than a mistake to prevent.
        self.methods = list(methods or [])
        self.sample_types = list(sample_types or [])
        self.start = start
        self.end = end
        # Filters the last apply_to() could not set, because the report has no
        # such parameter. Empty unless a report is missing one.
        self.unsupported = []

    @property
    def fraction(self):
        """The single chosen fraction, or None when zero or several are.

        Kept so callers that can only express one fraction - the report
        parameter, and anything aggregating - keep reading a scalar. A
        multi-fraction selection reads as None here, which is correct: there
        is no single fraction to name, and the caller must handle the list.
        """
        return self.fractions[0] if len(self.fractions) == 1 else None

    # ------------------------------------------------------------ encoding

    def to_parameters(self):
        """The report parameters this selection sets, as {name: value}.

        A None value means "leave unfiltered". Only the parameters this
        selection actually controls appear, so applying it to a report never
        disturbs the other 46 it does not understand.
        """
        params = {
            PARAM_ANALYTES: encode_list(self.analytes),
            PARAM_LOCATIONS: encode_list(self.locations),
            PARAM_LOCATION_GROUPS: encode_list(self.location_groups),
            PARAM_MATRICES: encode_list(self.matrices),
            PARAM_FRACTIONS: encode_list(self.fractions),
            PARAM_METHODS: encode_list(self.methods),
            PARAM_SAMPLE_TYPES: encode_list(self.sample_types),
            PARAM_START: encode_date(self.start),
            PARAM_END: encode_date(self.end),
        }
        if self.facility_id is not None:
            params[PARAM_FACILITY] = str(self.facility_id)
        return params

    def apply_to(self, report):
        """Return a copy of `report` carrying this selection's filters.

        The report is copied rather than mutated so a failed save cannot leave
        the caller holding a half-modified report that no longer describes
        what is on the server.
        """
        updated = dict(report or {})
        wanted = self.to_parameters()
        # A filter can only be applied to a parameter the report already
        # carries, so one that is missing would be dropped in silence - the
        # report would run wider than asked and look like it worked. All three
        # ARII variants on this tenant carry every one, so this is a guard
        # against a report shape we have not seen rather than a known case.
        present = {p.get("name") for p in (report or {}).get("parameters") or []}
        self.unsupported = sorted(
            name for name, value in wanted.items()
            if value is not None and name not in present
        )
        parameters = []
        for entry in (report or {}).get("parameters") or []:
            item = dict(entry)
            name = item.get("name")
            if name == PARAM_EXTRA_FIELDS:
                # Additive, unlike every other parameter here: the report
                # already asks for columns of its own (ANALYTE_TYPE,
                # MATRIX_DESC on this tenant) and overwriting them would
                # quietly drop output someone else depends on.
                item["value"] = merge_extra_fields(item.get("value"))
                item["displayValue"] = item["value"] or ""
                parameters.append(item)
                continue
            if name in wanted:
                value = wanted[name]
                item["value"] = value
                # displayValue is what the Enterprise UI shows; leaving a
                # stale one makes a correctly-filtered report look wrong to
                # anyone who opens it there.
                item["displayValue"] = value or ""
            parameters.append(item)
        updated["parameters"] = parameters
        return updated

    # ------------------------------------------------------------ warnings

    def warnings(self):
        """Things the user should know about what this selection hides.

        Returned rather than logged: these belong in the panel, next to the
        controls that caused them, not in a message log nobody reads.
        """
        notes = []
        if self.fraction:
            label = dict(FRACTIONS).get(self.fraction, self.fraction)
            notes.append(
                "Showing %s results only. A matrix that does not use this "
                "fraction will return nothing." % label.lower()
            )
        excluded = sorted(QC_SAMPLE_TYPES[t] for t in QC_SAMPLE_TYPES
                          if t not in self.sample_types)
        if self.sample_types and excluded:
            notes.append(
                "Quality-control samples are excluded (%s)."
                % ", ".join(excluded).lower()
            )
        if not self.analytes:
            notes.append(
                "No chemicals selected, so the whole report is requested. "
                "Filtering by chemical is the strongest way to keep a large "
                "facility inside the time limit."
            )
        return notes

    def describe(self):
        """One line naming what is selected, for the panel's status text."""
        bits = []
        bits.append("%d chemical(s)" % len(self.analytes) if self.analytes
                    else "all chemicals")
        if self.matrices:
            bits.append("/".join(self.matrices))
        if self.fraction:
            bits.append(dict(FRACTIONS).get(self.fraction, self.fraction).lower())
        if self.methods:
            bits.append("/".join(self.methods) if len(self.methods) <= 2
                        else "%d methods" % len(self.methods))
        if self.location_groups:
            bits.append("/".join(self.location_groups)
                        if len(self.location_groups) <= 2
                        else "%d location groups" % len(self.location_groups))
        if self.locations:
            bits.append("%d location(s)" % len(self.locations))
        if self.start or self.end:
            bits.append("%s to %s" % (encode_date(self.start) or "start",
                                      encode_date(self.end) or "now"))
        return ", ".join(bits)

    # -------------------------------------------------------- client-side

    def matches(self, table, row):
        """Whether one report row passes this selection.

        Used for the agent path, whose stored output is frozen and cannot be
        re-scoped server-side. Only the filters that map onto columns the
        report actually carries are applied; one it cannot express is ignored
        rather than dropping every row.
        """
        if self.matrices and table.column("matrix") >= 0:
            if table.cell(row, "matrix").upper() not in {
                    m.upper() for m in self.matrices}:
                return False
        if self.fractions and table.column("fraction") >= 0:
            wanted = {f.upper() for f in self.fractions}
            if table.cell(row, "fraction").upper() not in wanted:
                return False
        if self.methods and table.column("method") >= 0:
            wanted = {m.upper() for m in self.methods}
            if table.cell(row, "method").upper() not in wanted:
                return False
        if self.sample_types and table.column("sample_type") >= 0:
            if table.cell(row, "sample_type").upper() not in {
                    s.upper() for s in self.sample_types}:
                return False
        if self.locations and table.column("location") >= 0:
            if table.cell(row, "location") not in set(self.locations):
                return False
        return True
