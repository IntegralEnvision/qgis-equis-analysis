"""Reusable probes for the EQuIS API.

The most valuable habit when authoring against this API is to SURVEY BEFORE
INVENTING: before assuming a structure is valid, count how many existing
objects use it. A shape used by 924 objects is safe; a shape used by none is a
guess, and this API accepts guesses silently.

Copy what you need. Nothing here writes.
"""

import base64
import collections
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request


# ------------------------------------------------------------------ client

def load_env(path=".env"):
    """Read a `.env` of `key: value` (or `key=value`) lines: equis_site, api_key."""
    values = {}
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for sep in (":", "="):
                if sep in line:
                    k, v = line.split(sep, 1)
                    values[k.strip().lower()] = v.strip().strip("'\"")
                    break
    return values.get("equis_site", "").rstrip("/"), values.get("api_key", "")


class Equis(object):
    def __init__(self, base, key, insecure=True):
        self.base, self.key = base, key
        self.ctx = None
        if insecure:                      # many tenants use internal certs
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def call(self, path, method="GET", body=None, params=None,
             timeout=600, tries=4, accept="application/json"):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        last = (None, b"")
        for attempt in range(tries):
            req = urllib.request.Request(
                url, data=data, method=method,
                headers={"Authorization": "Bearer " + self.key,
                         "Accept": accept,
                         "Content-Type": "application/json"})
            try:
                kw = {"timeout": timeout}
                if self.ctx is not None:
                    kw["context"] = self.ctx
                with urllib.request.urlopen(req, **kw) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                # The status IS the answer here - never retry it.
                return exc.code, exc.read()
            except Exception as exc:      # noqa: BLE001 - DNS/connection blips
                last = (None, repr(exc)[:120].encode())
                if attempt < tries - 1:
                    time.sleep(4)
        return last

    def json(self, path, **kw):
        status, body = self.call(path, **kw)
        if status != 200 or not body.strip():
            return None
        try:
            return json.loads(body)
        except ValueError:
            return None


# ------------------------------------------------------------- identity

def whoami(key):
    """The caller's username, from the JWT. NOT /api/users[0]."""
    parts = key.split(".")
    if len(parts) < 2:
        return None
    pad = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(pad)).get("jti")
    except Exception:                     # noqa: BLE001
        return None


def fetch_spec(api):
    """The live Swagger JSON, found via the UI page.

    The path embeds the build version (`/swagger/docs/7_25_1_25129`), so it
    cannot be guessed - the usual Swashbuckle locations all 404. Grepping this
    is much faster than clicking through the UI, but remember the spec
    describes what EXISTS, not how it behaves.
    """
    status, body = api.call("/swagger", accept="text/html")
    if status != 200:
        return None
    match = re.search(r"'(swagger/docs/[^']+)'", body.decode("utf-8", "replace"))
    if not match:
        return None
    path = "/" + match.group(1).split("|")[0]
    status, body = api.call(path)
    if status != 200:
        return None
    spec = json.loads(body)
    print("spec %s: %d paths, %d definitions"
          % (spec.get("info", {}).get("version"),
             len(spec.get("paths", {})), len(spec.get("definitions", {}))))
    return spec


# --------------------------------------------------------------- surveys

def survey_timeseries_widgets(api):
    """Every TimeSeries widget on the tenant, with the shapes it uses.

    Use this before inventing a series/axis structure: if no existing widget
    does what you are about to do, expect it not to render.
    """
    shapes = collections.Counter()
    axis_types = collections.Counter()
    formats = collections.Counter()
    reports_per_widget = collections.Counter()
    out = []
    for dash in api.json("/api/dashboards") or []:
        widgets = api.json("/api/dashboards/%d/widgets" % dash["id"]) or []
        for w in widgets:
            if w.get("widgetTypeId") != 383:
                continue
            try:
                cp = json.loads(w["userSettings"])["chartParams"]
            except Exception:             # noqa: BLE001
                continue
            series = cp.get("series") or []
            axes = cp.get("yAxis") or []
            reports_per_widget[len({s.get("report") for s in series})] += 1
            for s in series:
                shapes[sum(1 for p in s.get("id", "").split("::") if "==" in p)] += 1
            for a in axes:
                axis_types[a.get("type")] += 1
                formats[(a.get("labels") or {}).get("format")] += 1
            out.append({"dashboard": dash["id"], "widget": w["id"],
                        "title": w.get("title"),
                        "series": len(series), "axes": len(axes)})
    print("filters per series id:", dict(shapes))
    print("axis types           :", dict(axis_types))
    print("label formats        :", dict(formats))
    print("distinct reports/widget:", dict(reports_per_widget))
    for row in out:
        flag = "" if row["series"] == row["axes"] else "   <-- axes != series"
        print("  dash %-9s widget %-9s %-34s %4d series %4d axes%s" % (
            row["dashboard"], row["widget"], str(row["title"])[:34],
            row["series"], row["axes"], flag))
    return out


def survey_eias(api):
    """Every agent, and whether it actually produced output."""
    rows = []
    for e in api.json("/api/eia") or []:
        ev = e.get("ReportEvent") or {}
        sc = e.get("Schedule") or {}
        rows.append({
            "event": ev.get("id"),
            "name": ev.get("name"),
            "type": ev.get("agentTypeString_ReadOnly"),
            "scheduleId": ev.get("scheduleId"),
            "effective": sc.get("effective"),
            "lastEvaluated": sc.get("lastEvaluated"),
            "lastExecution": ev.get("lastExecutionDateUTC_ReadOnly"),
            "attachment": ev.get("attachment_id_ReadOnly"),
            "suppressed": ev.get("supressAllNotices"),
        })
    for r in sorted(rows, key=lambda x: str(x["lastExecution"])):
        verdict = ("OUTPUT" if r["attachment"] else
                   "ran, nothing stored" if r["lastExecution"] else
                   "never ran")
        print("  %-9s %-8s sched=%-9s %-22s %s" % (
            r["event"], r["type"], r["scheduleId"],
            str(r["lastExecution"])[:19], verdict))
    return rows


def report_parameter_values(api, report_id, facility_id, name):
    """Valid values for a report parameter, WITHOUT running the report.

    Rows 0 and 1 are a header and a blank; real values start at index 2.
    """
    data = api.json("/api/reports/%d/parameters/%s"
                    % (report_id, urllib.parse.quote(name)),
                    params={"facilityId": facility_id})
    return [r for r in (data or [])[2:] if r and r[0]]


def time_report(api, report_id, facility_id, cap=50):
    """Generation time with a tiny row cap - measures generation, not transfer."""
    start = time.time()
    status, body = api.call(
        "/api/reports/%d/data" % report_id,
        params={"facilityId": facility_id, "oType": "csv",
                "resultRecordCount": cap},
        accept="text/csv, application/zip, */*")
    elapsed = time.time() - start
    print("report %s -> HTTP %s in %.1fs" % (report_id, status, elapsed))
    if status == 500 and b"timed out" in body.lower():
        print("   the ~230s request limit - narrow by analyte first")
    return status, elapsed


def report_shape(api, report_id, facility_id):
    """Rows, series and per-tab density - what decides whether a chart renders."""
    o = api.json("/api/reports/%d/data" % report_id,
                 params={"facilityId": facility_id})
    if not o:
        return None
    cols = list(o["schema"]["properties"].keys())
    ix = {c: i for i, c in enumerate(cols)}
    rows = o["data"]
    pairs = collections.Counter(
        (r[ix["SYS_LOC_CODE"]], r[ix["CHEMICAL_NAME"]]) for r in rows)
    tabs = collections.Counter(r[ix["SYS_LOC_CODE"]] for r in rows)
    print("rows=%d series=%d  points/series %d..%d  points/tab %d..%d"
          % (len(rows), len(pairs), min(pairs.values()), max(pairs.values()),
             min(tabs.values()), max(tabs.values())))
    if len(rows) > 4000:
        print("   over ~4000 rows - charts have failed above this")
    return {"rows": len(rows), "pairs": pairs, "tabs": tabs, "columns": cols}


def varying_columns(api, facility_id, layer=1):
    """Which location attributes actually vary - i.e. are usable as filters.

    Frequently the answer is 'none' - metadata is often unpopulated.
    """
    o = api.json("/api/facilities/%d/FeatureServer/%d/query" % (facility_id, layer),
                 params={"f": "json", "where": "1=1", "outFields": "*",
                         "returnGeometry": "false", "resultRecordCount": 2000})
    feats = [f["attributes"] for f in (o or {}).get("features", [])]
    if not feats:
        return {}
    useful = {}
    for col in sorted(feats[0]):
        vals = collections.Counter(f.get(col) for f in feats)
        populated = sum(v for k, v in vals.items() if k not in (None, ""))
        if populated and 1 < len(vals) < len(feats):
            useful[col] = dict(vals.most_common(8))
    print("%d locations; columns that vary: %s"
          % (len(feats), sorted(useful) or "NONE - filter by code prefix only"))
    return useful


def cleanup(api, facility_id, prefix="ZZ_"):
    """Delete throwaway reports. Run at the end of any session that made them."""
    reports = api.json("/api/reports", params={"facilityId": facility_id}) or []
    for r in [x for x in reports if str(x.get("name", "")).startswith(prefix)]:
        for rid in (abs(r["id"]), r["id"]):
            status, _ = api.call("/api/reports/%d" % rid, method="DELETE")
            if status in (200, 204):
                print("deleted %s" % r["name"])
                break


if __name__ == "__main__":
    site, key = load_env()
    api = Equis(site, key)
    print("caller:", whoami(key))
    survey_timeseries_widgets(api)
