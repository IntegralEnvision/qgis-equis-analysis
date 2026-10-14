"""The per-user report the live path writes its filters onto.

EQuIS applies a filter only when it is SAVED on a report, and report ownership
cannot be transferred, so the plugin cannot hand every user a copy of one
shared report. Instead each user gets their own saved report per facility,
created from Analytical Results II on first use and reused from then on.

Per user AND per facility: `@facility_id` is itself a saved parameter, so one
report can only point at one facility. A QGIS session holds one facility at a
time, but the tenant is shared - two people working different sites would
otherwise own identically named reports, and the group filing could not tell
them apart.

No QGIS imports, so this is testable with a plain interpreter.
"""

from .data_selection import PARAM_FACILITY


class EquisTemplateError(Exception):
    """No report was available to copy parameter definitions from."""


# Analytical Results II. Every filter the data selection sets lives on this
# base report's parameter list. Found by NAME, never by id: base report ids
# are allocated per tenant (508 on the one this was built against, which is
# exactly why no code may assume it).
BASE_REPORT_CODE = "Analytical Results II"
SYSTEM_OWNER = ".system"


def _base_of(report):
    """A listing or detail record's base id, whichever spelling it uses."""
    value = report.get("base_report_id", report.get("baseReportId"))
    try:
        return abs(int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def is_base_report(report, code=BASE_REPORT_CODE):
    """Whether `report` IS the tenant's base report for `code`.

    A base report is owned by .system and is its own base (id equals
    baseReportId). Identified by those, not by an id number.
    """
    return ((report.get("name") or "").strip() == code
            and (report.get("code") or "").strip() == code
            and (report.get("owner") or "") == SYSTEM_OWNER
            and _base_of(report) == abs(int(report.get("id") or 0)))

# What the plugin's own reports are called. The prefix is how they are
# recognised on a later run, so it must not change casually: a new prefix
# orphans every report the previous version made.
NAME_PREFIX = "QGIS plugin"

# EQuIS stores at most this many characters of a report name and silently
# drops the rest. Measured 2026-10-12: no name of the tenant's 964 reports is
# longer, and 31 stop at exactly 64, mid-word.
NAME_LIMIT = 64

REMARK = (
    "Created by the EQuIS Chemical Explorer QGIS plugin. Its filters are "
    "rewritten on each fetch, so edits here do not persist. Safe to delete "
    "when no longer needed - the plugin recreates it."
)


def report_name(username, facility_name, facility_id):
    """The name identifying one user's report for one facility.

    The facility id is included because names are not unique - two facilities
    can share a display name, and picking the wrong report would silently
    fetch another site's data.
    """
    who = (username or "unknown").strip()
    where = (facility_name or "").strip() or str(facility_id)
    return "%s - %s - %s (%s)" % (NAME_PREFIX, who, where, facility_id)


def is_plugin_report(name):
    """Whether a report name looks like one of ours."""
    return (name or "").strip().startswith(NAME_PREFIX + " - ")


def facility_group_name(facility_name):
    """The group these reports are filed under, matched case-insensitively."""
    return (facility_name or "").strip()


class LiveReport(object):
    """Finds, creates and updates the report the live path runs.

    Deliberately does no fetching: it owns the report, not the data. That
    keeps the "which report" question - the one with all the ownership and
    permission edge cases - separate from reading rows.
    """

    def __init__(self, client, facility_id, facility_name="", username=""):
        self.client = client
        self.facility_id = facility_id
        self.facility_name = facility_name
        self.username = username
        self.report_id = None
        self.created = False
        self.filed = False
        self.notes = []

    @property
    def name(self):
        return report_name(self.username, self.facility_name, self.facility_id)

    # ------------------------------------------------------------- finding

    def find(self):
        """The id of this user's report for this facility, or None.

        Matched on name, which encodes both the user and the facility. Owner
        is checked too where the listing reports one: a name collision with
        another user's report would otherwise have us rewriting their filters.
        """
        # AS STORED: EQuIS cuts report names to NAME_LIMIT characters, so a
        # full name never matched and a NEW report was created on every
        # visit - 10 copies for one facility by 2026-08-31, found 2026-10-12.
        # The cut can take the facility id off the end, so the report's own
        # facility must match too: two facilities whose names share their
        # first 64 characters must never trade reports.
        wanted = self.name[:NAME_LIMIT].strip().lower()
        found = []
        for report in self.client.list_reports(self.facility_id):
            if (report.get("name") or "").strip().lower() != wanted:
                continue
            if report.get("facility_id", report.get("facilityId")) != self.facility_id:
                continue
            owner = (report.get("owner") or "").strip().lower()
            if owner and self.username and owner != self.username.lower():
                # Same name, someone else's report. Leave it alone.
                continue
            found.append(abs(report.get("id") or 0))
        # Earlier copies may exist (see above); the newest, deterministically.
        return max(found) if found and max(found) else None

    def ensure(self):
        """Find this user's report or create it. Returns its id.

        Creating is the exceptional path - it happens once per user per
        facility - so a failure here is worth an explicit message rather than
        a generic one: it almost always means the account lacks Creator on
        the Reports object type.
        """
        found = self.find()
        if found:
            self.report_id = found
            return found

        template, base_report_id = self._template()
        created = self.client.create_report(
            base_report_id=base_report_id,
            name=self.name,
            code=BASE_REPORT_CODE,
            facility_id=self.facility_id,
            parameters=template,
            remark=REMARK,
        )
        self.report_id = abs(created.get("id") or 0)
        self.created = True
        return self.report_id

    def _template(self):
        """(parameters, base report id) a new report starts from.

        The parameters are cloned wholesale from an existing report on the
        base. This is not an optimisation: a parameter is a full descriptor -
        displayType, dbType, caption, visibility, isRequired - and a report
        POSTed with bare {name, value} parameters is rejected with HTTP 500.
        There is no way to synthesise a valid array, so one is copied.

        THE BASE FIRST, BY NAME, AND ONLY REPORTS ON IT. Measured on the
        tenant 2026-10-12: three .system base reports share the code
        "Analytical Results II" - the base itself (508 there), "...with
        Sample Selection" (54 parameters to its 55) and "...- reportable
        sample" - so the code does not identify a base. User reports are
        not even listed with that code. The old search matched on code and
        created on a hard-coded 508; it worked only because 508 is the
        smallest of the three ids. Now: the base is the .system report
        NAMED "Analytical Results II" that is its own base; the clone comes
        from our own reports on it, then anyone's, then the base itself
        (always there, so a tenant with no reports yet still works); and the
        new report is created on that base, whatever its id.
        """
        listing = self.client.list_reports(self.facility_id)
        bases = [r for r in listing if is_base_report(r)]
        if len(bases) != 1:
            raise EquisTemplateError(
                "Expected one '%s' base report owned by %s, found %d. EQuIS "
                "rejects a report created without a base's parameter "
                "definitions, so the live query path cannot create one."
                % (BASE_REPORT_CODE, SYSTEM_OWNER, len(bases)))
        base_id = abs(int(bases[0].get("id") or 0))

        candidates = [(2, base_id)]
        for report in listing:
            if _base_of(report) != base_id or is_base_report(report):
                continue
            rank = 0 if is_plugin_report(report.get("name")) else 1
            candidates.append((rank, abs(report.get("id") or 0)))
        candidates.sort()

        for _, report_id in candidates:
            if not report_id:
                continue
            try:
                detail = self.client.report_detail(report_id)
            except Exception:  # noqa: BLE001 - try the next candidate
                continue
            params = detail.get("parameters") or []
            # The listing said this report is on the base; its detail must
            # agree, or its descriptors may be another base's.
            if params and _base_of(detail) == base_id:
                return self._with_facility(params), base_id

        raise EquisTemplateError(
            "Could not read the parameter definitions of the '%s' base report "
            "or any report on it. EQuIS rejects a report created without them."
            % BASE_REPORT_CODE)

    def _with_facility(self, parameters):
        """A parameter copy pointed at this facility."""
        out = []
        for entry in parameters:
            item = dict(entry)
            if item.get("name") == PARAM_FACILITY:
                item["value"] = str(self.facility_id)
                item["displayValue"] = str(self.facility_id)
            out.append(item)
        return out

    # ------------------------------------------------------------ updating

    def apply(self, selection):
        """Save `selection`'s filters onto the report. Returns its id.

        Read-modify-write rather than a blind PUT: the report carries 54
        parameters and the selection controls 8 of them, so the rest have to
        survive the round trip.
        """
        report_id = self.report_id or self.ensure()
        detail = self.client.report_detail(report_id)
        updated = selection.apply_to(detail)
        if selection.unsupported:
            # Saying so beats running wider than asked and looking correct.
            self.notes.append(
                "This report has no %s parameter, so that filter was not "
                "applied." % ", ".join(selection.unsupported)
            )
        self.client.save_report(updated)
        return report_id

    # -------------------------------------------------------------- filing

    def file_under_facility(self):
        """File the report into the facility's group, if there is one.

        Tidiness, never a precondition: a tenant with no matching group, or a
        blip reading one, must not stop the user fetching data. Failures are
        recorded in `notes` and otherwise ignored.
        """
        if not self.report_id:
            return False
        wanted = facility_group_name(self.facility_name).lower()
        if not wanted:
            return False
        try:
            groups = self.client.list_groups()
        except Exception as exc:  # noqa: BLE001
            self.notes.append("Could not list groups (%s)." % exc)
            return False
        group = None
        for candidate in groups:
            if (candidate.get("name") or "").strip().lower() == wanted:
                group = candidate
                break
        if group is None:
            return False

        # An unreadable group is unknown, NOT empty: reading a connection blip
        # as "no members" would file a duplicate every run.
        members = self.client.group_members(group["id"])
        if members is not None:
            for member in members:
                if abs(member.get("id") or 0) == self.report_id:
                    self.filed = True
                    return True
        self.filed = bool(self.client.add_group_member(group["id"],
                                                       self.report_id))
        if not self.filed:
            self.notes.append(
                "Could not file the report under '%s'; it still works."
                % group.get("name")
            )
        return self.filed
