"""Can your EQuIS account change a report's filters?

WHAT THIS IS FOR
----------------
We are deciding how the EQuIS QGIS plugin should fetch data. A full report for
a large facility takes longer to generate than EQuIS allows for one web
request, so it fails with an HTTP 500. The plugin has to narrow the report -
to one chemical, say - before running it.

EQuIS only applies a filter when it is SAVED on the report. Passing it in the
URL does nothing. So the plugin must write to a report before each fetch.

Report ownership cannot be transferred (tested: the API accepts the change and
ignores it), so we cannot give each person their own scratch report. That
leaves one question, and it is the only thing this script tests:

    Can you SAVE a filter onto a report that someone else owns?

If yes, the plugin can share one scratch report and everyone can use it.
If no, the dynamic filtering feature cannot work for you and we will fall back
to a fixed set of pre-built reports.

A "no" is a useful result. Please send the output either way.

WHAT IT DOES TO YOUR EQuIS SITE
-------------------------------
It writes to ONE report, created for this purpose and named:

    QGIS plugin PUT test (safe to modify)

That report exists only to be scribbled on. The script changes its chemical
filter, checks whether the change stuck, and puts the original value back. It
never runs the report, so it costs no server time. Nothing else is touched -
no other report, no agent, no dashboard. It creates and deletes nothing.

If it cannot find that report, it stops and tells you, rather than picking a
different one to write to.

HOW TO RUN IT
-------------
1. Put this file in a folder together with a file named `.env` containing:

       equis_site: https://yoursite.equisonline.com
       api_key: <paste your EQuIS API key here>

   Generate an API key in EQuIS Enterprise under your user profile. It must be
   YOUR key - the point is to test your permissions, not someone else's. The
   key is read from disk and sent only to your own EQuIS site.

2. Run it with any Python 3.8+:

       python check_report_permissions.py

   No packages to install - standard library only.

3. Send back everything it prints.

Options:
    --report ID     test a different report id (default: the one named above)
    --insecure      skip TLS verification, only if your site uses an internal
                    certificate and the script tells you to
"""

import argparse
import base64
import datetime
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")

# The scratch report this test writes to. It exists to be modified.
TARGET_NAME = "QGIS plugin PUT test (safe to modify)"
TARGET_ID = 3833630
FACILITY_ID = 1000001          # Facility_A

# The parameter we flip, and the two values we flip between. Both are real
# analytes in this report, so neither leaves it in a broken state.
PARAM = "@cas_rns"
PROBE_VALUE = "7440-38-2"      # Arsenic
PROBE_LABEL = "Arsenic"


def fail(message):
    print("\nSTOPPED: %s" % message)
    sys.exit(2)


def read_env():
    if not os.path.isfile(ENV_PATH):
        fail("No .env file next to this script.\n"
             "Create %s containing:\n\n"
             "    equis_site: https://yoursite.equisonline.com\n"
             "    api_key: <your key>\n" % ENV_PATH)
    values = {}
    with open(ENV_PATH, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for sep in (":", "="):
                if sep in line:
                    name, value = line.split(sep, 1)
                    values[name.strip().lower()] = value.strip().strip("'\"")
                    break
    site = values.get("equis_site") or values.get("site")
    api_key = values.get("api_key") or values.get("key")
    if not site or not api_key:
        fail("The .env must set both `equis_site` and `api_key`.")
    return site.rstrip("/"), api_key


def describe_token(api_key):
    """Username and expiry, read from the key itself - no request needed."""
    parts = (api_key or "").split(".")
    if len(parts) < 2:
        return None, None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001 - an opaque key is not an error
        return None, None
    expiry = None
    if claims.get("exp"):
        try:
            expiry = datetime.datetime.fromtimestamp(
                float(claims["exp"]), datetime.timezone.utc).replace(tzinfo=None)
        except Exception:  # noqa: BLE001
            expiry = None
    return claims.get("jti"), expiry


class Api(object):
    def __init__(self, base, api_key, insecure=False):
        self.base, self.api_key = base, api_key
        self.ctx = None
        if insecure:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def call(self, path, method="GET", body=None, params=None, timeout=180,
             tries=3):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        last = (None, b"")
        for attempt in range(tries):
            request = urllib.request.Request(
                url, data=data, method=method,
                headers={"Authorization": "Bearer " + self.api_key,
                         "Accept": "application/json",
                         "Content-Type": "application/json"})
            try:
                kwargs = {"timeout": timeout}
                if self.ctx is not None:
                    kwargs["context"] = self.ctx
                with urllib.request.urlopen(request, **kwargs) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as exc:
                # The status is the answer we want - never retry it.
                return exc.code, exc.read()
            except Exception as exc:  # noqa: BLE001 - DNS/connection blips
                last = (None, repr(exc)[:120].encode("utf-8"))
                if attempt < tries - 1:
                    time.sleep(3)
        return last


def parameter_value(report, name):
    for item in report.get("parameters") or []:
        if item.get("name") == name:
            return item.get("value")
    return None


def set_parameter(report, name, value, label):
    """Return a copy of the report with one parameter changed."""
    updated = json.loads(json.dumps(report))
    params = []
    for item in updated.get("parameters") or []:
        entry = dict(item)
        if entry.get("name") == name:
            entry["value"] = value
            entry["displayValue"] = label
        params.append(entry)
    updated["parameters"] = params
    return updated


def main():
    parser = argparse.ArgumentParser(
        description="Test whether this account can save a filter onto a report.")
    parser.add_argument("--report", type=int, default=TARGET_ID,
                        help="report id to test (default %d)" % TARGET_ID)
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS certificate verification")
    args = parser.parse_args()

    site, api_key = read_env()
    api = Api(site, api_key, insecure=args.insecure)

    print("EQuIS report-filter permission check")
    print("=" * 62)
    print("site : %s" % site)
    who, expiry = describe_token(api_key)
    if who:
        print("you  : %s" % who)
    if expiry is not None:
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        if expiry <= now:
            fail("This API key expired on %s UTC. Generate a new one."
                 % expiry.strftime("%Y-%m-%d %H:%M"))
        print("       key valid until %s UTC" % expiry.strftime("%Y-%m-%d %H:%M"))
    print()

    # 1. Can we read the target at all? -------------------------------
    print("1. Reading the test report")
    status, body = api.call("/api/reports/%d" % args.report)
    if status in (401, 403):
        fail("The server rejected this key for reading (HTTP %s). If it was "
             "pasted correctly, the account may not have access to this "
             "facility." % status)
    if status != 200 or not body.strip():
        fail("Could not read report %d (HTTP %s). It may have been deleted - "
             "ask for a new test report id." % (args.report, status))
    report = json.loads(body)
    print("   id %s: %r" % (args.report, report.get("name")))
    # Refuse to scribble on anything that is not the designated scratch
    # report, whatever id was passed. `--report` is for when a NEW scratch
    # report has been made, not for pointing this at real work.
    if report.get("name") != TARGET_NAME:
        fail("Report %d is named %r, not %r. This test rewrites a "
             "report's filters, so it only writes to the designated "
             "scratch report. Ask for the current test report id."
             % (args.report, report.get("name"), TARGET_NAME))

    original = parameter_value(report, PARAM)
    print("   its %s filter is currently: %r" % (PARAM, original))

    # Who owns it? The interesting case is that it is NOT you.
    status, body = api.call("/api/reports", params={"facilityId": FACILITY_ID})
    owner = None
    if status == 200 and body.strip():
        for item in json.loads(body):
            if abs(item.get("id") or 0) == args.report:
                owner = item.get("owner")
                break
    print("   owned by: %s" % (owner or "unknown"))
    if who and owner and str(owner).lower() == str(who).lower():
        print("   NOTE: you own this report, so a success here does not prove")
        print("         you could edit someone else's.")

    # 2. Can we save a filter onto it? --------------------------------
    print("\n2. Saving a filter (%s = %s)" % (PARAM, PROBE_LABEL))
    probe = set_parameter(report, PARAM, PROBE_VALUE, PROBE_LABEL)
    status, body = api.call("/api/reports/%d" % args.report, method="PUT",
                            body=probe)
    print("   PUT returned HTTP %s" % status)
    if status in (401, 403):
        print("\n   -> NO. The server refused the write.")
        print("      Dynamic filtering will not work for this account.")
        return 0
    if status is None:
        fail("Could not reach the server (%s). Try again."
             % body.decode("utf-8", "replace")[:80])

    # A 200 here means nothing on this API - it accepts writes and silently
    # discards them. The read-back is the actual test.
    time.sleep(2)
    status, body = api.call("/api/reports/%d" % args.report)
    if status != 200 or not body.strip():
        fail("Could not read the report back (HTTP %s)." % status)
    now_value = parameter_value(json.loads(body), PARAM)
    print("   reading it back: %s is now %r" % (PARAM, now_value))

    worked = str(now_value) == PROBE_VALUE

    # 3. Put it back --------------------------------------------------
    if worked:
        print("\n3. Restoring the original filter")
        restored = set_parameter(json.loads(body), PARAM, original, "restored")
        status, _ = api.call("/api/reports/%d" % args.report, method="PUT",
                             body=restored)
        time.sleep(2)
        status2, body2 = api.call("/api/reports/%d" % args.report)
        back = parameter_value(json.loads(body2), PARAM) if status2 == 200 else None
        print("   %s is %r again" % (PARAM, back))
        if str(back) != str(original):
            print("   (could not restore it exactly - harmless, this report is")
            print("    scratch, but mention it when you send the output)")
    else:
        print("\n3. Nothing to restore - the change did not take effect.")

    # Summary ---------------------------------------------------------
    print("\n" + "=" * 62)
    print("RESULT")
    if worked:
        print("  YES - you can save filters onto this report.")
        print("  The plugin's dynamic filtering can work for your account.")
    else:
        print("  NO - the write was accepted (HTTP %s) but the value did not"
              % status)
        print("  change. On this API that means the edit was silently ignored,")
        print("  which is what a permissions failure usually looks like.")
        print("  Dynamic filtering will not work for your account.")
    print("\nPlease send this whole output back. Thank you.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
