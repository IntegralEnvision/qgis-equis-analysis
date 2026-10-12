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
# base report's parameter list.
BASE_REPORT_ID = 508
BASE_REPORT_CODE = "Analytical Results II"

# What the plugin's own reports are called. The prefix is how they are
# recognised on a later run, so it must not change casually: a new prefix
# orphans every report the previous version made.
NAME_PREFIX = "QGIS plugin"

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
        wanted = self.name.lower()
        for report in self.client.list_reports(self.facility_id):
            if (report.get("name") or "").strip().lower() != wanted:
                continue
            owner = (report.get("owner") or "").strip().lower()
            if owner and self.username and owner != self.username.lower():
                # Same name, someone else's report. Leave it alone.
                continue
            return abs(report.get("id") or 0) or None
        return None

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

        template = self._template()
        created = self.client.create_report(
            base_report_id=BASE_REPORT_ID,
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
        """The parameter array a new report starts from.

        Cloned wholesale from an existing report on the same base. This is not
        an optimisation: a parameter is a full descriptor - displayType,
        dbType, caption, visibility, isRequired - and a report POSTed with
        bare {name, value} parameters is rejected with HTTP 500. There is no
        way to synthesise a valid array, so a template must be found.

        Any report on base 508 will do; a previous plugin report is preferred
        only because its filters are already the shape we overwrite.
        """
        candidates = []
        for report in self.client.list_reports(self.facility_id):
            if (report.get("code") or "") != BASE_REPORT_CODE:
                continue
            # Our own reports first, then any other report on this base.
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
            if params:
                return self._with_facility(params)

        raise EquisTemplateError(
            "Could not find an existing '%s' report to copy the parameter "
            "definitions from. EQuIS rejects a report created without them, "
            "so the live query path needs at least one such report to be "
            "visible to this account." % BASE_REPORT_CODE
        )

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
