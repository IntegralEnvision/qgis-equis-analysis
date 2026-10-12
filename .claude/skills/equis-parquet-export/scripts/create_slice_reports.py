"""Create the slice reports and their nightly agents, and seed the manifest.

    # see the plan without writing anything
    python scripts/create_slice_reports.py --facility <id> --root data/<name> \
        --template <report id> --cell WS/D --dry-run

    # create it
    python scripts/create_slice_reports.py --facility <id> --root data/<name> \
        --template <report id> --cell WS/D

    # give reports to the slices nightly_slices.py --rewrite added
    python scripts/create_slice_reports.py --facility <id> --root data/<name> \
        --template <report id> --pending

This is the step `nightly_slices.py` assumes has already run. It divides one
(matrix, fraction) cell's locations into bins, creates one report per bin with
those locations saved to @sys_loc_codes, attaches a daily EIA to each, and
records the lot in the manifest.

WHAT IT WRITES TO THE SHARED TENANT
-----------------------------------
One report and one agent per slice, named <prefix>_<matrix>_<fraction>_NN.
The prefix is fixed in the manifest the first time a facility is sliced
(default `parquet_<facility id>`), because the names then live in the tenant.
Nothing else is touched: it never edits or deletes an object it did not create,
and it refuses to overwrite a name that already exists unless --replace is
given. --dry-run prints the plan and writes nothing.

Start with ONE cell (--cell WS/D). Twelve cells is 40-odd permanent objects on
a tenant other people use, and until a slice has actually produced output the
whole approach is unproven.

WHY A TEMPLATE REPORT IS REQUIRED
---------------------------------
A report parameter is a full descriptor - displayType, dbType, caption,
visibility, isRequired, dependencies - and EQuIS rejects a POST whose
parameters are bare {name, value} pairs with an opaque HTTP 500 that looks
exactly like a permissions failure. There is no way to synthesise the array, so
it must be cloned from an existing report on the same base. --template names
that report; its saved filters (analytes, sample types, matrix, fraction) are
inherited by every slice, which is what makes the slices comparable.

WHY 03:00
---------
The agents run daily at 03:00 UTC by default (--at). EQuIS schedules are in
UTC: the tenant's existing agents carry timeStart 420 and were observed firing
at 07:00 UTC. Times are minutes from midnight, and a daily schedule sets
timeStart == timeEnd.
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from slice_store import (  # noqa: E402
    read_env,
    DEFAULT_TARGET_ROWS, MANIFEST_NAME, UNITS_NAME, Manifest, bin_count,
    cell_key, default_prefix, pack_bins, propose_unit, slice_name, utc_now,
)
import unit_convert  # noqa: E402


# Result rows per sample, used to convert sample-count weights into the row
# units DEFAULT_TARGET_ROWS is expressed in. Measured at 5.36 on the one
# extract available; it is refined per cell from the manifest once any slice
# has produced output, and this default only shapes the very first plan.
ROWS_PER_SAMPLE = 5.36

# EQuIS schedule times are minutes from midnight, UTC.
DEFAULT_RUN_AT = 3 * 60


def fail(message):
    sys.stderr.write("\n%s\n\n" % message)
    raise SystemExit(2)



def token_identity(api_key):
    """(username, user_id_or_None) from the JWT, no request needed."""
    import base64
    parts = (api_key or "").split(".")
    if len(parts) < 2:
        return None, None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001
        return None, None
    return claims.get("jti"), None


class Api(object):
    def __init__(self, base, api_key):
        self.base, self.api_key = base, api_key

    def json(self, path, method="GET", body=None, timeout=180, tries=3,
             quote=True):
        url = self.base + (urllib.parse.quote(path, safe="/?$=&'()*,")
                           if quote else path)
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(tries):
            request = urllib.request.Request(
                url, data=data, method=method,
                headers={"Authorization": "Bearer " + self.api_key,
                         "Accept": "application/json",
                         "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=timeout) as resp:
                    raw = resp.read()
                    return resp.status, (json.loads(raw) if raw else None)
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                try:
                    return exc.code, json.loads(raw)
                except ValueError:
                    return exc.code, raw[:300]
            except Exception as exc:  # noqa: BLE001
                if attempt == tries - 1:
                    return None, repr(exc)[:200]
                time.sleep(2 * (attempt + 1))
        return None, "unreachable"


def caller(api, api_key):
    """The caller's user record. `jti` is the username; match it.

    GET /api/users returns ONE record without admin rights and the whole
    directory with them, so [0] is the `administrator` account rather than the
    caller on an admin token. Matching on the JWT's username is the only form
    that is right either way.
    """
    username, _ = token_identity(api_key)
    status, body = api.json("/api/users")
    records = body if isinstance(body, list) else (body or {}).get("value") or []
    for record in records:
        name = (record.get("userName") or record.get("username")
                or record.get("name") or "")
        if username and name.strip().lower() == username.strip().lower():
            return record, username
    if len(records) == 1:
        return records[0], username
    return None, username


def location_weights(api, facility_id, matrix):
    """Samples per location for one matrix, which seeds the first plan.

    Counted on DT_SAMPLE because it is cheap (117 pages for this facility) and
    because per-location RESULT counts are not directly readable. The bins this
    produces are approximate; the nightly job replaces them with measured row
    counts after the first successful run.

    It OVERSTATES, which is the safe direction. Only the matrix is filtered
    here - the fraction lives on DT_TEST and the analyte list on DT_RESULT, so
    a location with WS samples is counted even if it has no D-fraction result
    for any of the report's analytes. Measured on the first facility: this
    returns 512 locations and 84,734 samples for WS, while the report's actual
    WS/D output covers 250 locations and 355,440 rows. The plan therefore sizes
    slices for ~28% more rows than arrive, so the first night comes in under
    target rather than over.

    Filtering properly would mean the DT_SAMPLE -> DT_TEST -> DT_RESULT walk
    (slice_store.count_results_by_location), which is exact but took 224.7
    minutes on this facility. Overstating by a quarter for one night is the
    better trade.
    """
    weights = {}
    where = "FACILITY_ID eq %d" % facility_id
    if matrix:
        where += " and MATRIX_CODE eq '%s'" % matrix
    path = ("/api/odata/DT_SAMPLE?$filter=%s&$orderby=SAMPLE_ID" % where)
    raw = True
    pages = 0
    while path:
        status, body = api.json(path, quote=raw)
        raw = False
        if status != 200 or not isinstance(body, dict):
            fail("Could not read DT_SAMPLE (page %d): %s" % (pages, body))
        for row in body.get("value") or []:
            code = (row.get("SYS_LOC_CODE") or "").strip()
            if code:
                weights[code] = weights.get(code, 0) + 1
        pages += 1
        if pages % 25 == 0:
            print("   ...%d pages, %d locations" % (pages, len(weights)))
            sys.stdout.flush()
        nxt = body.get("@odata.nextLink")
        path = ("/api/odata/" + nxt.split("/api/odata/", 1)[1]
                if nxt and "/api/odata/" in nxt else None)
    return weights


def clone_parameters(template, facility_id, matrix, fraction, locations):
    """The template's parameter array with this slice's filters applied.

    Cloned wholesale, not synthesised: see the module docstring. Only the
    values change.
    """
    parameters = json.loads(json.dumps(template.get("parameters") or []))
    wanted = {"@facility_id": facility_id,
              "@matrix_codes": matrix,
              "@fractions": fraction,
              # Cleared deliberately. A template report usually carries a
              # narrow @cas_rns - the one this work started from saved 18
              # metals, which are 45% of the facility's results - and
              # inheriting it would silently scope the whole extraction to
              # that list. Cells differ in which analytes they carry: one solid
              # cell had 2,664 rows across all analytes and none at all under
              # those 18, so it would have looked like an empty cell.
              "@cas_rns": None,
              # MultiSelect values are PIPE-separated. A comma returns 200 and
              # zero rows, which is a silent wrong answer.
              "@sys_loc_codes": "|".join(locations)}
    for param in parameters:
        name = param.get("name")
        # EQuIS stores an empty parameter as the literal string "None".
        # Carried forward unscrubbed that becomes a filter for a chemical
        # actually named "None".
        value = param.get("value")
        if isinstance(value, str) and value.strip() == "None":
            param["value"], param["displayValue"] = None, ""
        if name in wanted:
            if wanted[name] is None:
                param["value"], param["displayValue"] = None, ""
            else:
                param["value"] = wanted[name]
                param["displayValue"] = str(wanted[name])
    return parameters


def probe_cell(api, template, facility_id, matrix, fraction, locations):
    """Run one throwaway slice of this cell.

    Returns (rows, seconds, status, unit_counts). `unit_counts` is
    {unit: rows} over what came back, which proposes the cell's entry in
    units.json - the unit the data is actually reported in, not a guess
    from the matrix code.

    The report is created, run and deleted here. It measures the cell's real
    rows-per-location, which the per-matrix sample estimate cannot see: a
    D-sized bin returned 18,214 rows in WS/N and 164,463 in SE/N on this
    facility, because the fraction's share of a matrix varies enormously.

    Deleted in a finally block, and only if its name still starts with ZZ_, so
    an unexpected id cannot destroy a real report.
    """
    name = "ZZ_cellprobe_%s_%s_%d" % (matrix, fraction, int(time.time()))
    body = {"baseReportId": template["baseReportId"],
            "name": name,
            "code": template.get("code") or "Analytical Results II",
            "type": "class", "facilityId": facility_id, "statusFlag": "A",
            "remark": "Throwaway sizing probe. Safe to delete.",
            "parameters": clone_parameters(template, facility_id, matrix,
                                           fraction, locations)}
    status, created = api.json("/api/reports", "POST", body)
    if status not in (200, 201) or not isinstance(created, dict):
        return None, 0.0, "create failed: %s" % str(created)[:80]
    report_id = abs(int(created.get("id") or 0))
    try:
        params = {"facilityId": facility_id, "oType": "csv",
                  "where": "SAMPLE_DATE >= '1900-01-01'"}
        url = "%s/api/reports/%d/data?%s" % (api.base, report_id,
                                             urllib.parse.urlencode(params))
        request = urllib.request.Request(url)
        request.add_header("Authorization", "Bearer " + api.api_key)
        request.add_header("Accept", "text/csv, application/zip, */*")
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=280) as response:
                raw = response.read()
                seconds, status = time.time() - started, response.status
        except urllib.error.HTTPError as exc:
            return None, time.time() - started, exc.code, {}
        except Exception as exc:  # noqa: BLE001
            return None, time.time() - started, type(exc).__name__, {}
        rows, units = read_probe(raw)
        return rows, seconds, status, units
    finally:
        status, check = api.json("/api/reports/%d" % report_id)
        if (check or {}).get("name", "").startswith("ZZ_"):
            api.json("/api/reports/%d" % report_id, "DELETE")


def read_probe(raw):
    """(rows, unit_counts) from a report run's body: CSV, or CSV in a zip.

    The output format is not stable - some runs return a zip holding one
    CSV, others plain text - so both are handled.
    """
    import csv
    import io
    import zipfile
    if raw[:2] == b"PK":
        archive = zipfile.ZipFile(io.BytesIO(raw))
        raw = archive.read(archive.namelist()[0])
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig",
                                                      "replace"))))
    return len(rows), unit_convert.unit_counts(rows)


def find_report(api, facility_id, name):
    status, body = api.json("/api/reports?facilityId=%d" % facility_id)
    records = body if isinstance(body, list) else (body or {}).get("value") or []
    for record in records:
        if (record.get("name") or "").strip() == name:
            return abs(int(record.get("id") or 0)) or None
    return None


def create_report(api, template, facility_id, name, matrix, fraction,
                  locations):
    body = {
        # The TEMPLATE's base, never a remembered id: a base report's id
        # differs between tenants, and main() refuses a template without one.
        "baseReportId": template["baseReportId"],
        "name": name,
        "code": template.get("code") or "Analytical Results II",
        "type": "class",
        "facilityId": facility_id,
        "statusFlag": "A",
        "remark": "Parquet slice: %s/%s, %d locations. Managed by "
                  "scripts/nightly_slices.py." % (matrix, fraction,
                                                len(locations)),
        "parameters": clone_parameters(template, facility_id, matrix,
                                       fraction, locations),
    }
    status, created = api.json("/api/reports", "POST", body)
    if status not in (200, 201) or not isinstance(created, dict):
        return None, "HTTP %s %s" % (status, str(created)[:200])
    report_id = abs(int(created.get("id") or 0))
    if not report_id:
        return None, "no id in response"

    # A 201 does not mean the parameters applied. Read back and assert the one
    # that matters - a slice whose locations did not save would fetch the whole
    # facility and time out.
    status, check = api.json("/api/reports/%d" % report_id)
    saved = None
    for param in (check or {}).get("parameters") or []:
        if param.get("name") == "@sys_loc_codes":
            saved = param.get("value")
    if saved != "|".join(locations):
        return report_id, ("created, but @sys_loc_codes did not stick "
                           "(got %r)" % (str(saved)[:60]))
    return report_id, None


def create_agent(api, report_id, name, user_id, email, run_at):
    """Attach a daily agent. Returns (event_id, error).

    Every sentinel id here matters: omitting them makes the server drop the
    Schedule block silently, returning 201 with scheduleId null - an agent
    that never runs. `effective` is midnight and Z-suffixed for the same
    reason; a runOnce agent effective at the creation moment is never
    evaluated, and daily schedules inherit the same evaluator.
    """
    today = datetime.date.today().isoformat()
    event = {"id": -1,
             "isDisabled": False,
             "userReportId": -report_id,          # NEGATIVE
             "name": name,
             "description": None,
             "remark": name,                      # becomes the attachment name
             "outputFormatString_ReadOnly": "csv",
             "outputFormat": 0,
             "isForAnyFacility": True,
             "isForOnlyNewData": False,
             "agentType": 0,
             "agentTypeString_ReadOnly": "schedule",
             "formatName": None,
             "supressNoDataNotice": False,
             # Store only. The output is still written and still served by the
             # API; this just stops a large attachment being emailed nightly.
             "supressAllNotices": True,
             "ownerUserId": user_id,
             "scheduleId": 0,
             "noticeTemplateId": 0}
    schedule = {"id": 0,
                "statusFlag": "A",
                "createdBy": user_id,
                # Minutes from midnight UTC; a daily schedule sets both ends
                # to the same minute, as every working agent here does.
                "timeStart": run_at, "timeEnd": run_at, "timeInterval": 0,
                "effective": today + "T00:00:00.000Z",
                "expiration": None, "lastEvaluated": None,
                "recurrenceType": 2,               # daily
                "recurrenceDay": 0, "recurrenceInterval": 1,
                "recurrenceOrdinal": 0,
                "title": "daily:"}
    template = {"id": 0,
                "templateCode": "eia-%s-%d-%d" % (user_id, report_id,
                                                  int(time.time() * 1000)),
                "templateName": "report.success",
                "subject": name, "body": name,
                "ietfLang": "en",
                "contentType": "text/plain; charset=utf-8"}
    status, created = api.json(
        "/api/eia", "POST", {"ReportEvent": event, "Schedule": schedule,
                             "NoticeTemplate": template,
                             "Recipients": [{"id": user_id,
                                             "emailAddress": email}],
                             "HookInfo": None})
    if status not in (200, 201):
        return None, "HTTP %s %s" % (status, str(created)[:200])

    # Find it and assert the schedule attached. This is the single most common
    # way to end up with an agent that never fires.
    status, body = api.json("/api/eia")
    records = body if isinstance(body, list) else (body or {}).get("value") or []
    for record in records:
        report_event = record.get("ReportEvent") or {}
        if (report_event.get("name") or "") != name:
            continue
        event_id = report_event.get("id")
        if not report_event.get("scheduleId"):
            return event_id, "created, but no schedule attached"
        return event_id, None
    return None, "created but not found on read-back"


def set_up_manifest(manifest, facility_id, prefix):
    """Bind the manifest to one facility and fix its slice-name prefix.

    The facility is checked because a root reused for a second facility
    would mix two facilities' slices in one manifest, and every later step
    trusts the manifest completely.

    An EXISTING manifest keeps the prefix its slices already carry - the
    names are on reports and agents in the tenant - including a hand-made
    one with no prefix, which keeps the fallback.
    """
    recorded = manifest.data.get("facility_id")
    if recorded is not None and int(recorded) != int(facility_id):
        fail("%s belongs to facility %s, not %s. Use a separate --root per "
             "facility." % (manifest.path, recorded, facility_id))
    manifest.data["facility_id"] = int(facility_id)
    if not manifest.data["slices"]:
        manifest.set_prefix(prefix or default_prefix(facility_id))
    elif prefix and prefix != manifest.prefix:
        fail("This facility's slices are already named %s_*; --prefix %s "
             "would orphan them." % (manifest.prefix, prefix))


def provision(api, template, facility_id, manifest, name, matrix, fraction,
              index, codes, user_id, email, run_at, replace):
    """One slice: its report (created, or reused with THESE locations saved)
    and its agent, recorded in the manifest. Returns True on success."""
    from nightly_slices import save_locations
    existing = find_report(api, facility_id, name)
    if existing and not replace:
        print("   %-28s a report of this name exists (%d); use --replace"
              % (name, existing))
        return False
    if existing:
        # REUSED REPORTS GET THIS SLICE'S LOCATIONS. Reusing one as it stood
        # left the manifest recording one location list while the report
        # extracted another, and whatever moved between them fell out of
        # every slice.
        report_id = existing
        if not save_locations(api, report_id, codes):
            print("   %-28s reusing report %d, but its locations did NOT "
                  "save - skipped" % (name, report_id))
            return False
        print("   %-28s reusing report %d, locations saved"
              % (name, report_id))
    else:
        report_id, problem = create_report(api, template, facility_id,
                                           name, matrix, fraction, codes)
        if report_id is None:
            print("   %-28s REPORT FAILED: %s" % (name, problem))
            return False
        print("   %-28s report %d%s"
              % (name, report_id, "  (%s)" % problem if problem else ""))
    event_id, agent_problem = create_agent(api, report_id, name, user_id,
                                           email, run_at)
    if event_id is None:
        print("   %-28s AGENT FAILED: %s" % ("", agent_problem))
    elif agent_problem:
        print("   %-28s agent %s  (%s)" % ("", event_id, agent_problem))
    else:
        print("   %-28s agent %s" % ("", event_id))
    entry = manifest.put_slice(name, matrix=matrix, fraction=fraction,
                               index=index, locations=codes,
                               report_id=report_id, event_id=event_id,
                               created=utc_now())
    entry.pop("needs_report", None)
    manifest.save()
    return event_id is not None and not agent_problem


def provision_pending(api, template, args, manifest, user_id, email):
    """--pending: reports and agents for slices --rewrite added."""
    waiting = sorted(name for name, entry in manifest.data["slices"].items()
                     if entry.get("needs_report") or not entry.get("report_id"))
    if not waiting:
        print("\nno slice is waiting for a report.\n")
        return 0
    print("\n%d slice(s) waiting for a report:" % len(waiting))
    for name in waiting:
        entry = manifest.slice(name)
        print("   %-28s %s  %d locations"
              % (name, cell_key(entry.get("matrix"), entry.get("fraction")),
                 len(entry.get("locations") or [])))
    if args.dry_run:
        print("\n--dry-run: nothing created.\n")
        return 0
    print()
    ok = 0
    for name in waiting:
        entry = manifest.slice(name)
        ok += provision(api, template, args.facility, manifest, name,
                        entry.get("matrix"), entry.get("fraction"),
                        entry.get("index"), entry.get("locations") or [],
                        user_id, email, args.at, args.replace)
    print("\n%d of %d provisioned; manifest: %s"
          % (ok, len(waiting), manifest.path))
    return 0 if ok == len(waiting) else 1


def report_units(args, matrix, fraction, probe_rows, probe_units):
    """Print the probe's units, and propose the cell's target from them."""
    unit, share = unit_convert.propose_target(probe_units)
    spread = ", ".join("%s %s" % (u or "(none)", "{:,}".format(n))
                       for u, n in sorted(probe_units.items(),
                                          key=lambda kv: -kv[1]))
    print("   units: %s" % spread)
    if not unit:
        print("   no concentration unit to propose; this cell will not be "
              "converted unless %s names one\n" % UNITS_NAME)
        return
    if args.dry_run:
        print("   would propose %s -> %s (%.0f%% of rows)\n"
              % (cell_key(matrix, fraction), unit, 100 * share))
        return
    written = propose_unit(
        args.root, matrix, fraction, unit,
        "proposed %s from a %s-row probe: %.0f%% of rows; edit to change"
        % (utc_now()[:10], "{:,}".format(probe_rows), 100 * share))
    if written:
        print("   proposed %s -> %s in %s\n"
              % (cell_key(matrix, fraction), unit, UNITS_NAME))
    else:
        print("   %s already has a unit in %s; left as it is\n"
              % (cell_key(matrix, fraction), UNITS_NAME))


def main():
    parser = argparse.ArgumentParser(
        description="Create slice reports and nightly agents for one cell.")
    parser.add_argument("--facility", type=int, required=True)
    parser.add_argument("--root", required=True,
                        help="directory holding the parquet and manifest; "
                             "one per facility")
    parser.add_argument("--template", type=int, required=True,
                        help="an existing report on the same base whose "
                             "parameters and filters the slices inherit")
    parser.add_argument("--cell", default=None,
                        help="which (matrix, fraction) cell, e.g. WS/D")
    parser.add_argument("--pending", action="store_true",
                        help="instead of planning a cell, give a report and "
                             "agent to every slice nightly_slices.py "
                             "--rewrite added")
    parser.add_argument("--prefix", default=None,
                        help="slice-name prefix for a NEW facility (default "
                             "parquet_<facility id>); fixed once chosen")
    parser.add_argument("--target-rows", type=int, default=DEFAULT_TARGET_ROWS)
    parser.add_argument("--at", type=int, default=DEFAULT_RUN_AT,
                        help="minutes from midnight UTC (default %d = 03:00)"
                             % DEFAULT_RUN_AT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-probe", action="store_true",
                        help="skip the calibration run (faster, but the slice "
                             "count is then only as good as the sample-count "
                             "estimate, and no unit is proposed)")
    parser.add_argument("--allow-empty", action="store_true",
                        help="create reports for a cell the probe finds has "
                             "no data at all")
    parser.add_argument("--replace", action="store_true",
                        help="reuse a slice report that already exists, "
                             "saving this plan's locations onto it")
    args = parser.parse_args()

    if bool(args.cell) == bool(args.pending):
        fail("Give exactly one of --cell WS/D or --pending.")
    matrix = fraction = None
    if args.cell:
        if "/" not in args.cell:
            fail("--cell looks like WS/D (matrix/fraction).")
        matrix, fraction = [part.strip() or None
                            for part in args.cell.split("/", 1)]

    site, key = read_env()
    api = Api(site, key)
    user, username = caller(api, key)
    if not user and not args.dry_run:
        fail("Could not identify the calling user, which the agent payload "
             "needs (ownerUserId).")
    user_id = (user or {}).get("id")
    email = (user or {}).get("emailAddress") or (user or {}).get("email") or ""

    status, template = api.json("/api/reports/%d" % args.template)
    if status != 200 or not isinstance(template, dict):
        fail("Could not read the template report %d: %s"
             % (args.template, template))
    if not template.get("baseReportId"):
        fail("Template %d names no baseReportId. Slices are created on the "
             "template's base, and a guessed id differs between tenants."
             % args.template)
    saved = {p.get("name"): p.get("value")
             for p in template.get("parameters") or []
             if p.get("value") not in (None, "", [])}

    manifest = Manifest(os.path.join(args.root, MANIFEST_NAME))
    set_up_manifest(manifest, args.facility, args.prefix)

    print("site     : %s" % site)
    print("user     : %s (id %s)" % (username, user_id))
    print("template : %d (%s), base %s"
          % (args.template, template.get("name"), template.get("baseReportId")))
    # Only what the slices really inherit: clone_parameters overrides these,
    # and listing the template's @cas_rns here read as if every slice would
    # be scoped to it - the opposite of what happens.
    overridden = ("@facility_id", "@sys_loc_codes", "@matrix_codes",
                  "@fractions", "@cas_rns")
    print("inherited: %s"
          % ", ".join("%s=%s" % (k, str(v)[:28]) for k, v in sorted(saved.items())
                      if k not in overridden))
    print("set per slice: @matrix_codes, @fractions, @sys_loc_codes; "
          "@cas_rns cleared (every analyte)")
    print("names    : %s_*" % manifest.prefix)

    if args.pending:
        return provision_pending(api, template, args, manifest, user_id, email)

    print("cell     : %s   run daily at %02d:%02d UTC\n"
          % (cell_key(matrix, fraction), args.at // 60, args.at % 60))

    print("counting samples per location ...")
    weights = location_weights(api, args.facility, matrix)
    if not weights:
        fail("No locations with samples in matrix %r - nothing to slice."
             % matrix)

    # Prefer what a previous run measured FOR THIS CELL; those are real rows,
    # in the units the target is expressed in.
    measured = manifest.location_weights(matrix, fraction)
    rows_per_unit = ROWS_PER_SAMPLE
    if measured:
        known = {code: measured[code] for code in weights if code in measured}
        if len(known) >= len(weights) // 2:
            weights = {code: measured.get(code, 0) for code in weights}
            rows_per_unit = 1.0
            print("   using measured row counts from a previous run")
    print("   %d locations, %s %s\n"
          % (len(weights), "{:,}".format(int(sum(weights.values()))),
             "rows" if rows_per_unit == 1.0 else "samples"))

    parts = bin_count(weights, args.target_rows, rows_per_unit=rows_per_unit)
    bins = pack_bins(weights, parts)

    # A probe of one slice is worth more than the estimate it checks: sample
    # counts are per-MATRIX, so they cannot see that a cell's fraction carries
    # far more or far less data than the matrix average. Measured on the
    # first facility, a D-sized bin returned 18,214 rows in WS/N but 164,463
    # in SE/N - a 9x spread the estimate is blind to. One report run costs
    # seconds and replaces the guess with the cell's real rows-per-location.
    if not args.no_probe:
        print("probing one slice to calibrate this cell ...")
        probe_rows, probe_seconds, status, probe_units = probe_cell(
            api, template, args.facility, matrix, fraction, bins[0])
        if probe_rows is None:
            print("   probe did not generate (%s after %.0fs). Slices may be "
                  "too large; continuing with the estimate.\n"
                  % (status, probe_seconds))
        elif probe_rows == 0:
            print("   this cell has NO DATA (0 rows in %.1fs).\n"
                  % probe_seconds)
            if not args.allow_empty:
                fail("Refusing to create reports and agents for a cell with "
                     "no data - they would store nothing every night, which "
                     "in the manifest is indistinguishable from failing. Use "
                     "--allow-empty to override.")
        else:
            per_location = probe_rows / float(len(bins[0]))
            measured = {code: per_location for code in weights}
            new_parts = bin_count(measured, args.target_rows,
                                  rows_per_unit=1.0)
            print("   %s rows from %d locations in %.1fs -> %.0f rows/location"
                  % ("{:,}".format(probe_rows), len(bins[0]), probe_seconds,
                     per_location))
            if new_parts != parts:
                print("   re-sizing %d -> %d slices on the measured rate"
                      % (parts, new_parts))
                parts = new_parts
                bins = pack_bins(weights, parts)
            else:
                print("   estimate holds at %d slices" % parts)
            report_units(args, matrix, fraction, probe_rows, probe_units)
    print("%d slice(s) at ~%s rows each:" % (len(bins),
                                             "{:,}".format(args.target_rows)))
    for index, codes in enumerate(bins):
        weight = sum(weights.get(code, 0) for code in codes)
        print("   %-28s %3d locations  ~%s rows"
              % (slice_name(matrix, fraction, index, manifest.prefix),
                 len(codes), "{:,}".format(int(weight * rows_per_unit))))
    print()

    if args.dry_run:
        print("--dry-run: nothing created.\n")
        return 0

    manifest.save()
    created = 0
    for index, codes in enumerate(bins):
        name = slice_name(matrix, fraction, index, manifest.prefix)
        created += provision(api, template, args.facility, manifest, name,
                             matrix, fraction, index, codes, user_id, email,
                             args.at, args.replace)

    print("\ncreated %d slice(s); manifest: %s" % (created, manifest.path))
    print("They run daily at %02d:%02d UTC. Tomorrow:\n"
          "   python scripts/nightly_slices.py --facility %d --root %s"
          % (args.at // 60, args.at % 60, args.facility, args.root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
