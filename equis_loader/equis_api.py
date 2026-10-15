"""Thin client for the EQuIS REST API.

Only the pieces the plugin needs: facility discovery, Esri FeatureServer
access, and report execution. Networking goes through urllib rather than
QgsNetworkAccessManager so that this module can be exercised outside of a
running QGIS (see tools/selftest.py).
"""

import base64
import binascii
import csv
import datetime
import io
import itertools
import json
import re
import time
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from xml.etree import ElementTree

# Reports are generated on demand and can take a couple of minutes on a cold
# cache; feature queries are comparatively quick.
FEATURE_TIMEOUT = 120
REPORT_TIMEOUT = 600

# The service caps a single query at this many rows regardless of what we ask
# for, so paged reads step by this amount.
PAGE_SIZE = 1000

# Crosstab reports legitimately produce wide output (one column per analyte),
# so this only guards against output that is not row-delimited at all.
MAX_REPORT_COLUMNS = 20000

# This host intermittently answers with 500s and transient DNS failures that
# succeed when tried again a moment later.
RETRIES = 4
RETRY_BACKOFF = 4.0

# Draining a layer issues a page request every couple of seconds; a short pause
# between them keeps the service from failing under a rapid burst.
PAGE_PAUSE = 0.4

# Smallest page worth retrying with when the service rejects a larger one.
MIN_PAGE_SIZE = 50

# `maxRecordCount` is advertised as 1000 but not enforced, so layers up to this
# size are fetched in a single request. That sidesteps the service's unreliable
# behaviour on the final pages of a paged read.
SINGLE_REQUEST_LIMIT = 50000


class EquisError(Exception):
    """Any failure talking to EQuIS, with a message fit to show a user."""


def parse_code_list(text):
    """Split a user-entered list of location codes on commas/whitespace."""
    if not text:
        return []
    cleaned = []
    for chunk in str(text).replace("\n", ",").replace("\t", ",").split(","):
        code = chunk.strip().strip("'\"")
        if code:
            cleaned.append(code)
    return cleaned


# The spreadsheet XML namespace, and the handful of tags worth naming.
_X_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_X_ROW = _X_NS + "row"
_X_C = _X_NS + "c"
_X_V = _X_NS + "v"
_X_T = _X_NS + "t"
_X_SI = _X_NS + "si"

# Excel's built-in numeric formats below 50 that denote a date or a time. Any
# custom format is detected by looking for date field characters instead.
_X_BUILTIN_DATE_FORMATS = frozenset(list(range(14, 23)) + list(range(45, 48)))

# Serial 0 in the 1900 date system. Excel treats 1900 as a leap year, which it
# was not, so serials at or below 60 are ambiguous; only 59 and below need the
# extra day removed, which this epoch already accounts for.
_X_EPOCH = datetime.datetime(1899, 12, 30)


def _xlsx_date_styles(archive, names):
    """Style indices whose number format makes the cell a date or time.

    A date in a workbook is just a number; only its format says otherwise, so
    reading values without this turns every sample date into a five-digit
    serial.
    """
    if "xl/styles.xml" not in names:
        return frozenset()
    try:
        with archive.open("xl/styles.xml") as handle:
            root = ElementTree.parse(handle).getroot()
    except (ElementTree.ParseError, KeyError):
        return frozenset()

    custom = {}
    for node in root.iter(_X_NS + "numFmt"):
        try:
            fmt_id = int(node.get("numFmtId", "-1"))
        except ValueError:
            continue
        code = (node.get("formatCode") or "").lower()
        # Strip quoted literals and colour/condition blocks so a stray "d" in
        # something like "0.00 \"kg/d\"" does not read as a day field.
        stripped = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code)
        custom[fmt_id] = any(ch in stripped for ch in "ymdhs")

    styles = set()
    fmts = root.find(_X_NS + "cellXfs")
    if fmts is None:
        return frozenset()
    for position, node in enumerate(fmts.findall(_X_NS + "xf")):
        try:
            fmt_id = int(node.get("numFmtId", "0"))
        except ValueError:
            continue
        if fmt_id in _X_BUILTIN_DATE_FORMATS or custom.get(fmt_id):
            styles.add(position)
    return frozenset(styles)


def _xlsx_serial_to_text(number):
    """Render an Excel date serial as ISO text."""
    try:
        stamp = _X_EPOCH + datetime.timedelta(days=float(number))
    except (OverflowError, ValueError):
        return str(number)
    # A serial below 1 is a clock time with no date part.
    if abs(float(number)) < 1:
        return stamp.time().isoformat()
    return stamp.isoformat()


def _xlsx_row_values(row, shared, date_styles):
    """Decode one <row> into a list of cell strings, gaps padded.

    Empty cells are omitted from the XML entirely, so each cell's column is
    read from its reference and the gaps filled - otherwise every row after a
    blank cell would shift a column to the left.
    """
    values = []
    for cell in row:
        if cell.tag != _X_C:
            continue
        column = _xlsx_column(cell.get("r"))
        if column:
            index = column - 1
            if index < len(values):
                # Out-of-order or repeated reference; keep the first.
                continue
            values.extend([""] * (index - len(values)))

        kind = cell.get("t") or "n"
        if kind == "inlineStr":
            text = "".join(node.text or "" for node in cell.iter(_X_T)).strip()
            values.append(text)
            continue
        node = None
        for child in cell:
            if child.tag == _X_V:
                node = child
                break
        raw = node.text if node is not None else None
        if raw is None:
            values.append("")
            continue
        if kind == "s":
            try:
                values.append(shared[int(raw)].strip())
            except (ValueError, IndexError):
                values.append("")
        elif kind == "b":
            # Report flags are Y/N text; Excel stores them as 1/0.
            values.append("Y" if raw.strip() == "1" else "N")
        elif kind in ("str", "e"):
            values.append((raw or "").strip())
        else:
            try:
                style = int(cell.get("s", "-1"))
            except ValueError:
                style = -1
            if style in date_styles:
                values.append(_xlsx_serial_to_text(raw))
            else:
                values.append(_cell_text(float(raw))
                              if _looks_numeric(raw) else raw.strip())
    return values


# Keyed on the letters alone, not the whole reference: "A1" and "A2" are the
# same column, and caching per reference would store one useless entry per
# cell in the sheet - millions of them, never hit twice.
_X_COLUMN_CACHE = {}


def _xlsx_column(ref):
    """The 1-based column number in a cell reference like "BC42"."""
    if not ref:
        return 0
    letters = ref[:3].rstrip("0123456789")
    if not letters:
        # A reference longer than the three leading characters sampled above
        # is not a column reference we can read.
        return 0
    cached = _X_COLUMN_CACHE.get(letters)
    if cached is not None:
        return cached
    column = 0
    for char in letters:
        if not char.isalpha():
            return 0
        column = column * 26 + (ord(char.upper()) - 64)
    _X_COLUMN_CACHE[letters] = column
    return column


def _looks_numeric(text):
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


def _cell_text(value):
    """Render one spreadsheet cell as the text the CSV path would have held.

    Workbook cells arrive typed, so a result of 0.01 comes back as a float and
    a sample date as a datetime. The parsers downstream were written against
    CSV text, so each type is rendered to the string form they already handle:
    dates as ISO, whole floats without the trailing ".0" that would otherwise
    turn a location code like 1200 into "1200.0".
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        # Excel stores these as TRUE/FALSE; report flags are Y/N text.
        return "Y" if value else "N"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return ""
        # Excel stores every number as a float, so an integral value here is
        # an integer that would otherwise gain a misleading ".0".
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    return str(value).strip()


def utc_now():
    """The current UTC time, without a tzinfo.

    `datetime.utcnow()` is deprecated from Python 3.12 and slated for
    removal, but the timezone-aware replacement compares unequal to the naive
    timestamps EQuIS returns. Producing a naive UTC value here keeps one
    comparable representation across the plugin and both Python versions.
    """
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _utc_from_epoch(seconds):
    """Naive UTC datetime from an epoch timestamp."""
    return datetime.datetime.fromtimestamp(
        seconds, datetime.timezone.utc).replace(tzinfo=None)


def token_claims(api_key):
    """The claims inside an EQuIS JWT, or {} if it cannot be read.

    The key is a JSON Web Token whose middle segment is base64url-encoded
    JSON. Reading it is local and free - no request to the service - so the
    plugin can say "expired 14 hours ago" instead of "it may have expired",
    which is the difference between a user renewing the key and a user
    filing a bug.

    Nothing here trusts the token: the signature is not checked, so this is
    only ever used to explain a failure the service has already reported, or
    to name the caller in something the caller owns.
    """
    parts = (api_key or "").split(".")
    if len(parts) < 2:
        return {}  # Not a JWT; some deployments use opaque keys.
    payload = parts[1]
    # base64url without padding, which b64decode insists on.
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError, binascii.Error):
        return {}
    return claims if isinstance(claims, dict) else {}


def token_username(api_key):
    """Who the key belongs to, from its `jti` claim, or "".

    Read from the token rather than /api/users, which returns only the
    caller's own record without admin rights and the whole directory with
    them - so taking [0] from it yields the `administrator` account rather
    than the caller.
    """
    who = token_claims(api_key).get("jti")
    return str(who).strip() if who else ""


def token_expiry(api_key):
    """When an EQuIS JWT expires, or None if it cannot be read."""
    try:
        return _utc_from_epoch(float(token_claims(api_key)["exp"]))
    except (ValueError, KeyError, TypeError):
        return None


def describe_token_state(api_key, now=None):
    """A clause describing the key's expiry, for an auth error message.

    Returns "" when the token carries no readable expiry, so the caller can
    concatenate it unconditionally.
    """
    expiry = token_expiry(api_key)
    if expiry is None:
        return ""
    now = now or utc_now()
    delta = expiry - now
    hours = delta.total_seconds() / 3600.0
    if hours < 0:
        ago = -hours
        if ago < 48:
            return " The key expired %s ago (%s UTC)." % (
                _describe_hours(ago), expiry.strftime("%Y-%m-%d %H:%M"))
        return " The key expired on %s UTC." % expiry.strftime("%Y-%m-%d %H:%M")
    # Still valid: say so, because it means the cause is something else.
    return " The key is valid until %s UTC, so this is not an expiry." % (
        expiry.strftime("%Y-%m-%d %H:%M"))


def _describe_hours(hours):
    if hours < 1:
        return "%d minutes" % max(1, int(hours * 60))
    if hours < 24:
        return "%d hours" % int(hours)
    return "%d days" % int(hours / 24)


def _is_timeout_body(error):
    """Whether an HTTP error is the service's request-timeout page.

    The status is a plain 500 and the explanation is only in the body, which
    is read once here - it is a short HTML page - and swallowed on failure,
    since a body that cannot be read tells us nothing either way.
    """
    try:
        body = error.read(2048)
    except Exception:  # noqa: BLE001
        return False
    text = body.decode("utf-8", "replace").lower() if body else ""
    return "request timed out" in text or "failed to respond within" in text


def _describe_size(count):
    """Human-readable byte/character count for error messages."""
    for unit in ("bytes", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return "%.0f %s" % (count, unit) if unit == "bytes" else "%.1f %s" % (count, unit)
        count /= 1024.0
    return "%d bytes" % count


def _clean_base_url(site):
    site = (site or "").strip().rstrip("/")
    if not site:
        raise EquisError("No EQuIS site URL configured.")
    if not site.startswith(("http://", "https://")):
        site = "https://" + site
    return site


class EquisClient:
    def __init__(self, site, api_key, verify_ssl=True, read_only=False):
        self.base = _clean_base_url(site)
        self.api_key = (api_key or "").strip()
        if not self.api_key:
            raise EquisError("No EQuIS API key configured.")
        self._ssl_ctx = None if verify_ssl else ssl._create_unverified_context()
        # Refuse every write, here rather than in the panel: _request is the
        # one door all five writes (create/save/delete a report, add/remove a
        # group member) go through, so a caller added later cannot miss it.
        # For read-only keys and administrators who want nothing created on
        # their tenant; the panel reads this same flag to stay off the
        # own-report path instead of keeping an opinion of its own.
        self.read_only = bool(read_only)

    # ------------------------------------------------------------------ http

    def _request(self, path, params=None, timeout=FEATURE_TIMEOUT,
                 accept="application/json", retries=RETRIES, method=None,
                 body=None):
        """One HTTP call, retried on the failures this host actually shows.

        `body` is JSON-encoded when given, which makes this usable for the
        writes the live-query path needs (creating a report, saving its
        filters) as well as the reads everything else does.
        """
        url = self.base + path
        if params:
            # Drop unset values so we never send "param=None".
            query = {k: v for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        headers = {"Authorization": "Bearer " + self.api_key}
        if accept:
            headers["Accept"] = accept
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=payload, headers=headers,
                                     method=method)

        # Draining a large layer means hundreds of requests; this host emits
        # occasional 500s and DNS blips that succeed on a retry, and losing a
        # long load to one of them is worse than waiting a few seconds.
        # Writes are not retried: a POST that succeeded but whose response was
        # lost would be replayed into a second report, which is worse than
        # surfacing the error.
        writes = payload is not None or (method and method not in ("GET", "HEAD"))
        if writes and self.read_only:
            raise EquisError(
                "Read-only mode is on, so nothing is written to EQuIS "
                "(refused %s %s)." % (method or "POST", path))
        if writes:
            retries = 1
        last = None
        for attempt in range(max(1, retries)):
            if attempt:
                time.sleep(RETRY_BACKOFF * attempt)
            try:
                kwargs = {"timeout": timeout}
                if self._ssl_ctx is not None:
                    kwargs["context"] = self._ssl_ctx
                with urllib.request.urlopen(req, **kwargs) as resp:
                    return resp.read(), resp.headers.get("Content-Type", "")
            except urllib.error.HTTPError as exc:
                # Client errors are the caller's fault and will not improve.
                if exc.code == 401:
                    # The key carries its own expiry, so say whether that is
                    # the cause rather than leaving the user to guess between
                    # a stale key, a revoked one and a typo.
                    raise EquisError(
                        "EQuIS rejected the API key (401).%s Paste a current "
                        "key into the settings above."
                        % describe_token_state(self.api_key)
                    )
                if exc.code == 403:
                    # A live key that is refused is a permissions problem, and
                    # naming that distinction saves the user renewing a key
                    # that was never the issue.
                    raise EquisError(
                        "Not permitted (403). The key is recognised but this "
                        "account may not have access to this item.%s"
                        % describe_token_state(self.api_key)
                    )
                if exc.code == 404:
                    raise EquisError("Not found on the server (404).")
                # A report that takes too long to generate comes back as a
                # bare 500 whose body is the only thing that says why. Without
                # this the failure reads as a server fault and the fix - read
                # the stored output instead, or narrow the report - is not
                # discoverable from the message.
                if exc.code >= 500 and _is_timeout_body(exc):
                    raise EquisError(
                        "EQuIS timed out generating this report (it allows "
                        "about 230 seconds). Reports this large have to be "
                        "read from an information agent's stored output, or "
                        "narrowed with a row limit or a location filter."
                    )
                last = EquisError("EQuIS returned HTTP %s for %s" % (exc.code, path))
                if exc.code < 500:
                    raise last
            except urllib.error.URLError as exc:
                last = EquisError("Could not reach %s (%s)" % (self.base, exc.reason))
            except TimeoutError:
                last = EquisError(
                    "Timed out after %ss waiting for %s" % (timeout, path)
                )
        raise last

    def _get_json(self, path, params=None, timeout=FEATURE_TIMEOUT):
        body, _ = self._request(path, params, timeout)
        try:
            return json.loads(body)
        except ValueError:
            raise EquisError("Expected JSON from %s but got something else." % path)

    # ------------------------------------------------------------- facilities

    def test_connection(self):
        """Fetch facilities as a cheap credential check. Returns the count."""
        return len(self.list_facilities())

    def list_facilities(self):
        """Facilities the user can see, sorted by display name."""
        data = self._get_json("/api/facilities", {"f": "json"})
        if not isinstance(data, list):
            raise EquisError("Unexpected response listing facilities.")
        facilities = []
        for item in data:
            fid = item.get("id")
            if fid is None:
                continue
            facilities.append(
                {
                    "id": fid,
                    "code": item.get("code") or "",
                    "name": item.get("name") or item.get("code") or str(fid),
                    # wkid is the facility's native projection; features come
                    # back in it and 0 means "unspecified".
                    "wkid": item.get("wkid") or 0,
                    "city": item.get("city") or "",
                    "state": item.get("state") or "",
                    "country": item.get("country") or "",
                }
            )
        facilities.sort(key=lambda f: f["name"].lower())
        return facilities

    # ----------------------------------------------------------- featureserver

    def facility_layers(self, facility_id):
        """The layers published for a facility, each with an id and name."""
        data = self._get_json(
            "/api/facilities/%d/FeatureServer" % facility_id,
            {"queryParams.f": "json"},
        )
        layers = []
        for lyr in data.get("layers") or []:
            if lyr.get("id") is None:
                continue
            layers.append({"id": lyr["id"], "name": lyr.get("name") or "Layer %s" % lyr["id"]})
        service_wkid = (data.get("spatialReference") or {}).get("wkid") or 0
        return layers, service_wkid

    def layer_metadata(self, facility_id, layer_id):
        return self._get_json(
            "/api/facilities/%d/FeatureServer/%d" % (facility_id, layer_id),
            {"queryParams.f": "json"},
        )

    def layer_count(self, facility_id, layer_id, where="1=1"):
        data = self._get_json(
            "/api/facilities/%d/FeatureServer/%d/query" % (facility_id, layer_id),
            {
                "queryParams.f": "json",
                "queryParams.where": where,
                "queryParams.returnCountOnly": "true",
            },
        )
        return data.get("count", 0)

    def query_layer_page(self, facility_id, layer_id, offset, where="1=1",
                         page_size=PAGE_SIZE, out_fields="*",
                         return_geometry=True):
        """One page of features.

        returnGeometry must be sent explicitly: despite the documented default
        of true, this service omits geometry unless asked.

        It is also a filter, not just a projection: when a layer's locations
        have null coordinates the service returns *nothing* for
        returnGeometry=true rather than returning them without geometry. Pass
        return_geometry=False to read those attribute-only locations.

        `out_fields` is honoured by the service and is worth setting: asking
        for 3 of 68 fields cut a 1000-feature page from 1.4 MB to 0.2 MB.
        Unknown field names are ignored silently rather than reported.
        """
        params = {
            "queryParams.f": "json",
            "queryParams.where": where,
            "queryParams.outFields": out_fields or "*",
            "queryParams.resultRecordCount": page_size,
            "queryParams.resultOffset": offset,
        }
        if return_geometry:
            params["queryParams.returnGeometry"] = "true"
        return self._get_json(
            "/api/facilities/%d/FeatureServer/%d/query" % (facility_id, layer_id),
            params,
        )

    def iter_layer_features(self, facility_id, layer_id, where="1=1", limit=None,
                            progress=None, is_canceled=None, out_fields="*",
                            location_codes=None):
        """Yield (feature_set, features) page by page until the layer is drained.

        The first tuple element is the full response for the page so callers
        can read `fields`/`spatialReference` off it once.

        `location_codes` restricts results to those location codes. The service
        ignores both `where` and `objectIds` on this endpoint (verified: every
        clause returns the full layer), so the filter is applied here after the
        page arrives. `limit` then counts matching features, not fetched ones.
        """
        wanted = {str(c).strip().lower() for c in (location_codes or []) if str(c).strip()}

        # The service returns HTTP 500 when resultOffset + resultRecordCount
        # runs past the end of the layer instead of returning a short final
        # page, so the last page has to be clamped to what remains.
        total = None
        try:
            total = self.layer_count(facility_id, layer_id, where=where)
        except EquisError:
            total = None

        # `maxRecordCount` advertises 1000 but is not enforced: asking for the
        # whole layer in one request works and avoids the flaky tail paging
        # entirely. Try that first when the layer is a reasonable size.
        if total is not None and total <= SINGLE_REQUEST_LIMIT and limit is None:
            try:
                data = self.query_layer_page(facility_id, layer_id, 0,
                                             where=where, page_size=total,
                                             out_fields=out_fields)
                features = data.get("features") or []
                if total and len(features) < total:
                    # returnGeometry=true is a FILTER on this service, not a
                    # projection: a location with null coordinates is dropped
                    # from the response entirely. That is usually all-or-
                    # nothing, but a facility can mix - Facility_A serves 124
                    # of 326, the placed surface-water stations but none of
                    # the soil borings - and keeping only the 124 silently
                    # loses every result measured at the rest.
                    #
                    # So read again without the flag whenever the geometry
                    # request came back short, and merge: the located
                    # features keep their points, the rest arrive as
                    # attributes only. A layer built from this draws what it
                    # can and can say what it could not place.
                    placed = {}
                    for item in features:
                        code = str((item.get("attributes") or {})
                                   .get("code", "")).strip().lower()
                        if code:
                            placed[code] = item
                    plain = self.query_layer_page(
                        facility_id, layer_id, 0, where=where, page_size=total,
                        out_fields=out_fields, return_geometry=False
                    )
                    everything = plain.get("features") or []
                    if len(everything) > len(features):
                        merged = []
                        for item in everything:
                            code = str((item.get("attributes") or {})
                                       .get("code", "")).strip().lower()
                            merged.append(placed.get(code, item))
                        data = dict(plain)
                        features = merged
                        if not placed:
                            # Nothing at all could be placed, which is the
                            # case the caller warns about.
                            data["equisGeometryUnavailable"] = True
                        else:
                            data["equisGeometryPartial"] = len(everything) - len(placed)
                # returnCountOnly over-reports slightly against what the layer
                # actually serves (10,636 claimed vs 10,349 returned on one
                # layer), so treat a near-complete single read as complete
                # rather than falling back to paging that cannot do better.
                if features and len(features) >= min(total, PAGE_SIZE):
                    if wanted:
                        features = [
                            f for f in features
                            if str((f.get("attributes") or {}).get("code", ""))
                            .strip().lower() in wanted
                        ]
                    if features:
                        yield data, features
                        if progress is not None:
                            progress(len(features))
                    return
            except EquisError:
                pass  # Fall back to paging below.

        offset = 0
        kept = 0
        # Once the service has rejected a page size we stop asking for larger
        # ones, so the tail costs a handful of requests rather than dozens.
        ceiling = PAGE_SIZE
        while True:
            if is_canceled is not None and is_canceled():
                return
            # Without a local filter the page can be sized to the remaining
            # limit; with one we must read full pages and filter as we go.
            page_size = ceiling
            if limit is not None and not wanted:
                page_size = min(page_size, limit - kept)
                if page_size <= 0:
                    return
            if total is not None:
                if offset >= total:
                    return
                page_size = min(page_size, total - offset)
                if page_size <= 0:
                    return
            if offset:
                time.sleep(PAGE_PAUSE)

            # Near the end of a layer this service rejects large pages with a
            # 500 (offset 10000 + size 600 fails where size 100 succeeds), so
            # step the page size down rather than losing the tail entirely.
            data = None
            attempt_size = page_size
            while True:
                try:
                    data = self.query_layer_page(
                        facility_id, layer_id, offset,
                        where=where, page_size=attempt_size, out_fields=out_fields
                    )
                    break
                except EquisError:
                    if attempt_size <= MIN_PAGE_SIZE:
                        raise
                    attempt_size = max(MIN_PAGE_SIZE, attempt_size // 4)
                    ceiling = attempt_size
                    time.sleep(PAGE_PAUSE)
            page_size = attempt_size

            features = data.get("features") or []
            if not features:
                return
            offset += len(features)
            exhausted = len(features) < page_size

            if wanted:
                features = [
                    f for f in features
                    if str((f.get("attributes") or {}).get("code", "")).strip().lower()
                    in wanted
                ]
            if limit is not None and kept + len(features) > limit:
                features = features[: limit - kept]

            if features:
                kept += len(features)
                yield data, features
                if progress is not None:
                    progress(kept)
            if exhausted or (limit is not None and kept >= limit):
                return

    # ---------------------------------------------------------------- reports

    def list_reports(self, facility_id=None):
        """Reports, optionally narrowed to those bound to one facility.

        Reports with no facilityId are global templates and are always
        included; `spatial` marks those that can emit geometry.
        """
        data = self._get_json("/api/reports")
        if not isinstance(data, list):
            raise EquisError("Unexpected response listing reports.")
        reports = []
        for item in data:
            rid = item.get("id")
            if rid is None:
                continue
            rfid = item.get("facilityId")
            if facility_id is not None and rfid not in (None, facility_id):
                continue
            actions = item.get("actions") or []
            reports.append(
                {
                    "id": rid,
                    "code": item.get("code") or "",
                    "name": item.get("name") or item.get("code") or str(rid),
                    "type": item.get("type") or "",
                    "facility_id": rfid,
                    "spatial": "download.shp" in actions,
                    # Whether an information agent has already run this report
                    # and stored its output. Reading that stored output is far
                    # cheaper than re-running the report, and for reports that
                    # exceed the service's time limit it is the only way to
                    # get the data at all.
                    "has_eia": bool(item.get("hasEIA")),
                    # Who owns it, so a report is never adopted on name
                    # alone: LiveReport.find() checked an owner the listing
                    # never carried, so its guard did nothing (2026-10-12).
                    "owner": item.get("owner") or "",
                    # The base it is built on. A base report is its own
                    # base and is owned by .system; ids differ between
                    # tenants, so nothing assumes a number.
                    "base_report_id": item.get("baseReportId"),
                }
            )
        reports.sort(key=lambda r: r["name"].lower())
        return reports

    @property
    def username(self):
        """The account this client authenticates as, or ""."""
        return token_username(self.api_key)

    def report_detail(self, report_id):
        """One report with its full `parameters` array.

        Always call this with the POSITIVE id: saved user reports are listed
        with a negative id but addressed by the positive one.
        """
        data = self._get_json("/api/reports/%d" % abs(report_id))
        if not isinstance(data, dict):
            raise EquisError("Unexpected response reading report %s." % report_id)
        return data

    def report_parameter_values(self, report_id, name, facility_id=None):
        """Valid values for one report parameter, without running the report.

        Returns a list of (value, label) pairs. This is the picker's data
        source: 0.4-1.1 s against a facility-scoped report, where running the
        report to discover the same thing costs minutes.

        The service answers with a header row and a blank row before the real
        values, and includes a ' NULL/BLANK' pseudo-value. The header is
        dropped positionally rather than by matching its text: it is the
        column name, which differs per parameter ('cas_rn'/'chemical_name'
        for analytes, 'Code'/'Desc' for matrices), so no text test catches
        them all.
        """
        params = {"facilityId": facility_id} if facility_id else None
        try:
            data = self._get_json(
                "/api/reports/%d/parameters/%s" % (abs(report_id), name),
                params,
            )
        except EquisError:
            # An unknown parameter name is a 400 here. Treat it as "nothing to
            # offer" rather than failing the whole panel: the picker for one
            # filter should not break the others.
            return []
        if not isinstance(data, list):
            return []
        values = []
        # Rows 0 and 1 are the header and a blank; real values start at 2.
        for row in data[2:]:
            if isinstance(row, (list, tuple)):
                value = _cell_text(row[0]) if row else ""
                label = _cell_text(row[1]) if len(row) > 1 else ""
            else:
                value = label = _cell_text(row)
            value = (value or "").strip()
            if not value or value.upper().strip() == "NULL/BLANK":
                continue
            values.append((value, (label or value).strip()))
        return values

    def create_report(self, base_report_id, name, code, facility_id,
                      parameters, remark=""):
        """Create a saved user report from a base report. Returns its detail.

        The new report comes back with a NEGATIVE id in listings; address it
        by the positive one everywhere else.
        """
        body = {
            "baseReportId": base_report_id,
            "name": name,
            "code": code,
            "type": "class",
            "facilityId": facility_id,
            "statusFlag": "A",
            "remark": remark,
            "parameters": list(parameters or []),
        }
        raw, _ = self._request("/api/reports", method="POST", body=body,
                               timeout=FEATURE_TIMEOUT)
        try:
            created = json.loads(raw)
        except ValueError:
            raise EquisError("EQuIS did not describe the report it created.")
        if not isinstance(created, dict) or created.get("id") is None:
            raise EquisError("EQuIS did not return an id for the new report.")
        return created

    def save_report(self, report, timeout=FEATURE_TIMEOUT):
        """Save a report read back from `report_detail`, filters included.

        EQuIS only applies a filter that is SAVED on the report - passing one
        as a query parameter is silently ignored and the report runs
        unfiltered, which on a large facility means a 230 s timeout rather
        than a wrong answer.
        """
        report_id = abs(report.get("id") or 0)
        if not report_id:
            raise EquisError("Cannot save a report with no id.")
        self._request("/api/reports/%d" % report_id, method="PUT",
                      body=report, timeout=timeout)
        return True

    def delete_report(self, report_id):
        """Delete a saved user report, addressed by its positive id."""
        self._request("/api/reports/%d" % abs(report_id), method="DELETE",
                      timeout=FEATURE_TIMEOUT)
        return True

    # ------------------------------------------------------------ grouping

    def list_groups(self):
        """Every group, which is what the Enterprise tree shows as folders."""
        data = self._get_json("/api/groups")
        if not isinstance(data, list):
            return []
        groups = []
        for item in data:
            gid = item.get("id")
            if gid is None:
                continue
            groups.append({
                "id": gid,
                "code": item.get("code") or "",
                "name": item.get("name") or item.get("description") or "",
                "facility_id": item.get("facilityId"),
            })
        return groups

    def group_members(self, group_id):
        """Members of one group, or None when the group cannot be read.

        None and [] mean different things and the caller must not confuse
        them: this host emits sporadic connection failures, and reading one
        as "no members" would file a duplicate report on the next run.
        """
        try:
            body, _ = self._request("/api/groups/%d/members" % group_id,
                                    timeout=FEATURE_TIMEOUT)
        except EquisError:
            return None
        if not body or not body.strip():
            return []
        try:
            data = json.loads(body)
        except ValueError:
            return None
        if not isinstance(data, list):
            return []
        members = []
        for item in data:
            members.append({
                "id": item.get("id"),
                "type": item.get("type") or "",
                "name": item.get("name") or "",
            })
        return members

    def add_group_member(self, group_id, report_id):
        """File a report into a group. True if it is now a member.

        Post the POSITIVE id; the member reads back (and is deleted) by the
        negative one. A duplicate add answers 409, which means the report is
        already filed - the desired end state, so it is not an error.
        """
        item = {"id": abs(report_id), "type": "user_report_id",
                "code": str(abs(report_id))}
        try:
            self._request("/api/groups/%d/members" % group_id, method="POST",
                          body=item, timeout=FEATURE_TIMEOUT)
        except EquisError as exc:
            if "409" in str(exc):
                return True
            # Filing is tidiness, never a precondition for reading data, so a
            # failure here is reported to the caller rather than raised.
            return False
        return True

    def remove_group_member(self, group_id, member_id):
        """Remove a membership. Does not delete the report itself."""
        try:
            self._request("/api/groups/%d/members/%d" % (group_id, member_id),
                          method="DELETE", timeout=FEATURE_TIMEOUT)
        except EquisError as exc:
            # Already gone is the end state we wanted.
            return "404" in str(exc)
        return True

    def list_report_eias(self, report_id):
        """Information agents for a report, newest execution first.

        Only agents that have actually produced output are returned: one that
        has never run has nothing to read, and offering it as a data source
        would fail at the point of use.

        The service answers 204 when a report has no agents, which is a normal
        answer rather than an error - most reports have none.
        """
        try:
            body, _ = self._request("/api/reports/%d/eia" % report_id,
                                    timeout=FEATURE_TIMEOUT)
        except EquisError:
            return []
        if not body or not body.strip():
            return []
        try:
            data = json.loads(body)
        except ValueError:
            return []
        if not isinstance(data, list):
            return []

        agents = []
        for item in data:
            event = (item or {}).get("ReportEvent") or {}
            event_id = event.get("id")
            if event_id is None or not event.get("attachment_id_ReadOnly"):
                continue
            agents.append(
                {
                    "event_id": event_id,
                    "name": event.get("name") or "",
                    "report_id": event.get("userReportId"),
                    "report_name": event.get("userReportName") or "",
                    "format": event.get("outputFormatString_ReadOnly") or "",
                    "attachment": event.get("attachment_name_ReadOnly") or "",
                    "executed": event.get("lastExecutionDateUTC_ReadOnly") or "",
                }
            )
        # Newest first: the most recent run is the one worth reading.
        agents.sort(key=lambda a: a["executed"], reverse=True)
        return agents

    def eia_output(self, report_id, event_id, max_rows=None, progress=None,
                   is_canceled=None, keep_columns=None, timeout=REPORT_TIMEOUT):
        """Read the output an information agent already stored.

        This is the same report data the report endpoint serves, but read from
        the database rather than generated on demand, so it arrives in seconds
        and is not subject to the service's request time limit. Reports whose
        generation exceeds that limit answer with an HTTP 500 and cannot be
        read any other way.

        The stored output is a frozen snapshot of the run that produced it:
        `where`, `outFields` and `resultRecordCount` are all ignored here
        (verified), so filtering is left to `keep_columns` and `max_rows` as
        the response is parsed.

        `oType=csv` asks the service to convert the stored workbook on its way
        out. That is counter-intuitively the faster route - the CSV is three
        times the bytes but arrives and parses in a fraction of the time
        (8.7 s against 38.6 s for one 127,000-row agent), because a workbook
        has to be decompressed and walked as XML. Both forms are handled, so a
        service that ignores the request still reads correctly.
        """
        body, content_type = self._request(
            "/api/reports/%d/events/%d/data" % (report_id, event_id),
            {"oType": "csv"},
            timeout=timeout,
            accept="text/csv, application/zip, */*",
            # A stored read either works or does not; it is not worth the
            # minutes a retry of a large payload costs.
            retries=2,
        )
        return self._rows_from_payload(
            body, content_type, max_rows=max_rows, progress=progress,
            is_canceled=is_canceled, keep_columns=keep_columns,
        )

    @staticmethod
    def location_where(codes, column="SYS_LOC_CODE"):
        """Build a where clause restricting a report to specific locations."""
        codes = parse_code_list(codes) if isinstance(codes, str) else list(codes or [])
        if not codes:
            return None
        quoted = ",".join("'%s'" % c.replace("'", "''") for c in codes)
        return "%s IN (%s)" % (column, quoted)

    def report_csv(self, report_id, facility_id, timeout=REPORT_TIMEOUT,
                   max_rows=None, progress=None, is_canceled=None,
                   out_fields=None, where=None, keep_columns=None):
        """Run a report and return its rows as (headers, rows, truncated).

        `oType=csv` is a request, not a guarantee: depending on the report the
        service answers with a zip containing one CSV, plain-text CSV, or a
        JSON envelope of row objects. All three are handled.

        These exports get large (hundreds of MB, ~500k rows is normal), so
        `max_rows` is also passed to the server and stops the local read early
        rather than building a layer QGIS cannot draw.

        `out_fields` restricts the columns the report returns. Both it and
        `max_rows` are applied when the report genuinely runs; a report served
        from EQuIS's cache returns its full stored output regardless, so the
        local row cap below is still enforced.

        `keep_columns` discards unwanted columns while parsing. That matters
        because cached reports ignore `out_fields`: a 462,000-row report holds
        roughly 700 MB with all 31 columns, and keeping only the 8 the caller
        needs cuts that substantially.
        """
        params = {"facilityId": facility_id, "oType": "csv"}
        if max_rows is not None:
            # Ask the server to cap it too; not all reports honour this, so the
            # local limit below is still enforced.
            params["resultRecordCount"] = max_rows
        if out_fields:
            # Honoured when the report actually runs (38 columns -> 3 cut one
            # response from 21 KB to 2.8 KB). A report served from cache
            # returns its full stored output and ignores this.
            params["outFields"] = (
                out_fields if isinstance(out_fields, str) else ",".join(out_fields)
            )
        if where:
            # Genuinely filters report output, unlike on the feature service:
            # restricting to 2 of 5764 locations took one report from 461,936
            # rows in 123 s to 460 rows in 14 s.
            params["where"] = where
        # Ask for CSV in the Accept header too: sending "application/json"
        # here makes the service answer with a JSON envelope instead, even
        # though oType=csv was requested.
        body, content_type = self._request(
            "/api/reports/%d/data" % report_id,
            params,
            timeout=timeout,
            accept="text/csv, application/zip, */*",
            # A failed report run can cost minutes, so retry once at most.
            retries=2,
        )
        return self._rows_from_payload(
            body, content_type, max_rows=max_rows, progress=progress,
            is_canceled=is_canceled, keep_columns=keep_columns,
        )

    def _rows_from_payload(self, body, content_type, max_rows=None,
                           progress=None, is_canceled=None, keep_columns=None):
        """Turn a report response into (headers, rows, truncated).

        `oType=csv` is a request, not a guarantee, and the same report can
        answer with a zip, plain CSV, a JSON envelope or - when the output was
        stored by an information agent - an Excel workbook. Shared by the
        live-run and stored-output paths so both understand every format.
        """
        # These payloads run to hundreds of MB, and holding the bytes, the
        # decoded text and the parsed rows at once is what drives peak
        # memory. Decode through a stream and drop each source as it is spent.
        if "zip" in content_type.lower() or body[:2] == b"PK":
            try:
                archive = zipfile.ZipFile(io.BytesIO(body))
            except zipfile.BadZipFile:
                raise EquisError("Report output was not readable.")
            names = archive.namelist()
            if not names:
                raise EquisError("Report returned an empty archive.")
            # An .xlsx is itself a zip, so check for a workbook before
            # treating the archive as a container holding a CSV. EIA output is
            # stored as a workbook, and reading xl/workbook.xml as if it were
            # CSV fails in a way that says nothing useful.
            is_workbook = any(n.lower() == "xl/workbook.xml" for n in names)
            if is_workbook or "spreadsheetml" in content_type.lower():
                archive.close()
                headers, rows, truncated = self._rows_from_xlsx(
                    body, max_rows, progress, is_canceled, keep_columns
                )
                del body
                return headers, rows, truncated
            csvs = [n for n in names if n.lower().endswith(".csv")]
            raw = archive.read((csvs or names)[0])
            archive.close()
        elif "spreadsheetml" in content_type.lower():
            headers, rows, truncated = self._rows_from_xlsx(
                body, max_rows, progress, is_canceled, keep_columns
            )
            del body
            return headers, rows, truncated
        else:
            raw = body
        del body

        head = raw[:64].lstrip()[:1]
        # Some reports answer a CSV request with a JSON array of row objects.
        if head in (b"[", b"{"):
            return self._rows_from_json(
                raw.decode("utf-8-sig", "replace"), max_rows, progress, is_canceled
            )

        payload_size = len(raw)
        stream = io.TextIOWrapper(
            io.BytesIO(raw), encoding="utf-8-sig", errors="replace", newline=""
        )
        del raw
        reader = csv.reader(stream)
        # Find the header: skip leading blank lines, and skip a single-cell
        # title line only when a wider header follows it. Anything consumed
        # while looking is replayed as data so nothing is lost.
        headers = []
        scanned = []
        for _ in range(20):
            try:
                candidate = next(reader)
            except StopIteration:
                break
            except csv.Error as exc:
                # Raised when a single field exceeds the module's size limit,
                # which means the payload is not really row-delimited CSV.
                raise EquisError(
                    "The report output could not be read as CSV (%s)." % exc
                )
            # Trailing commas in the header are common in these exports.
            while candidate and candidate[-1].strip() == "":
                candidate.pop()
            if not any(c.strip() for c in candidate):
                continue
            scanned.append([c.strip() for c in candidate])
            if len([c for c in candidate if c.strip()]) >= 2:
                break
        if not scanned:
            # An empty response just means "no data"; a large one that yields
            # no header means we could not make sense of the format.
            if payload_size > 1024:
                raise EquisError(
                    "The report returned %s of output but no readable header "
                    "row, so it could not be mapped."
                    % _describe_size(payload_size)
                )
            return [], [], False
        # The last line scanned is the header; earlier ones were titles,
        # unless every line was single-cell (a real one-column report).
        if len(scanned[-1]) >= 2:
            headers = scanned[-1]
            replay = []
        else:
            headers = scanned[0]
            replay = scanned[1:]
        if not headers:
            return [], [], False
        if len(headers) > MAX_REPORT_COLUMNS:
            raise EquisError(
                "This report's output is not valid CSV (found %s columns in the "
                "header). It cannot be mapped." % format(len(headers), ",d")
            )

        # When the caller only needs some columns, drop the rest as each row is
        # read. The service ignores outFields on cached reports, so this is the
        # only reliable way to keep a 460,000-row report's footprint down.
        keep = None
        if keep_columns:
            wanted = {str(c).strip().upper() for c in keep_columns}
            keep = [i for i, h in enumerate(headers) if h.strip().upper() in wanted]
            if len(keep) < 2:
                keep = None  # Nothing recognisable; keep everything.
            else:
                headers = [headers[i] for i in keep]

        rows = []
        truncated = False
        source = itertools.chain(replay, reader)
        index = 0
        while True:
            try:
                row = next(source)
            except StopIteration:
                break
            except csv.Error:
                # Stop at the first unreadable row rather than losing the rows
                # already parsed.
                truncated = True
                break
            index += 1
            if is_canceled is not None and index % 5000 == 0 and is_canceled():
                return headers, rows, True
            if not any(cell.strip() for cell in row):
                continue
            if keep is not None:
                row = [row[i] if i < len(row) else "" for i in keep]
            rows.append(row)
            if progress is not None and len(rows) % 25000 == 0:
                progress(len(rows))
            if max_rows is not None and len(rows) >= max_rows:
                truncated = True
                break
        return headers, rows, truncated

    @staticmethod
    def _rows_from_xlsx(raw, max_rows=None, progress=None, is_canceled=None,
                        keep_columns=None):
        """Read an .xlsx workbook into (headers, rows, truncated).

        EIA output is stored as a workbook rather than CSV, so this is the
        only way to read it.

        An .xlsx is a zip of XML, and the sheet is streamed with iterparse
        rather than read through openpyxl. The two run at the same speed on
        these exports (29 s each for one 12 MB / 127,000-row EIA), so this is
        not a performance choice: it drops a third-party dependency from the
        one code path that must not fail. openpyxl ships with QGIS today, but
        the plugin also runs on installs where it does not, and an EIA read
        that dies on ImportError is worse than one that is merely no faster.

        Values are rendered to strings because the parsers downstream were
        written against CSV text: dates as ISO, whole numbers without the
        trailing ".0" that would turn a location code like 1200 into
        "1200.0".
        """
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            raise EquisError("The report's Excel output was not readable.")
        try:
            names = archive.namelist()
            sheets = sorted(n for n in names
                            if n.lower().startswith("xl/worksheets/sheet")
                            and n.lower().endswith(".xml"))
            if not sheets:
                raise EquisError("The Excel output contained no worksheet.")

            # Cells of type "s" hold an index into this table rather than a
            # value, so it has to be read before the sheet.
            shared = []
            if "xl/sharedStrings.xml" in names:
                with archive.open("xl/sharedStrings.xml") as handle:
                    for _, element in ElementTree.iterparse(handle):
                        if element.tag == _X_SI:
                            shared.append("".join(
                                node.text or "" for node in element.iter(_X_T)))
                            element.clear()

            # Which cells are dates is a property of their style, not their
            # type: both arrive as plain numbers.
            date_styles = _xlsx_date_styles(archive, names)

            headers = []
            keep = None
            rows = []
            truncated = False
            index = 0
            with archive.open(sheets[0]) as handle:
                # Only "end" events, and only rows: the default yields an
                # event for every element in the sheet, which on a 127,000-row
                # export is 7.1 million callbacks to discard.
                for _, element in ElementTree.iterparse(handle, events=("end",)):
                    if element.tag != _X_ROW:
                        continue
                    # Checked on the first row as well as every 5000th: a
                    # short sheet would otherwise never test cancellation at
                    # all, and a cancelled read that runs to the end is not
                    # cancelled.
                    if is_canceled is not None and index % 5000 == 0 and is_canceled():
                        return headers, rows, True
                    index += 1
                    values = _xlsx_row_values(element, shared, date_styles)
                    element.clear()
                    if not any(values):
                        continue
                    if not headers:
                        # Trailing blanks in the header are unnamed padding
                        # columns; trimming them sets the width the data rows
                        # are squared up to. Data rows are never trimmed - a
                        # row whose last value is empty is short, not narrow,
                        # and popping it would shift columns out of line.
                        while values and not values[-1]:
                            values.pop()
                        if len([v for v in values if v]) < 2:
                            continue  # A one-cell row above the header is a title.
                        headers = values
                        if len(headers) > MAX_REPORT_COLUMNS:
                            raise EquisError(
                                "This report's output has %s columns. It "
                                "cannot be mapped." % format(len(headers), ",d")
                            )
                        if keep_columns:
                            wanted = {str(c).strip().upper() for c in keep_columns}
                            keep = [i for i, h in enumerate(headers)
                                    if h.strip().upper() in wanted]
                            if len(keep) < 2:
                                keep = None  # Nothing recognisable; keep it all.
                            else:
                                headers = [headers[i] for i in keep]
                        continue
                    if keep is not None:
                        values = [values[i] if i < len(values) else ""
                                  for i in keep]
                    elif len(values) != len(headers):
                        values = (values + [""] * len(headers))[:len(headers)]
                    rows.append(values)
                    if progress is not None and len(rows) % 25000 == 0:
                        progress(len(rows))
                    if max_rows is not None and len(rows) >= max_rows:
                        truncated = True
                        break
            return headers, rows, truncated
        finally:
            archive.close()

    @staticmethod
    def _rows_from_json(text, max_rows=None, progress=None, is_canceled=None):
        """Flatten a JSON array of row objects into (headers, rows, truncated)."""
        try:
            data = json.loads(text)
        except ValueError:
            raise EquisError("The report output could not be read as CSV or JSON.")
        if isinstance(data, dict):
            # Report output is sometimes wrapped in an envelope alongside
            # metadata ({"stats":..., "schema":..., "<rows>":[...]}), and the
            # key holding the rows varies. Prefer the known names, then fall
            # back to the longest list of objects anywhere in the envelope.
            picked = None
            for key in ("rows", "data", "features", "value", "results", "items"):
                if isinstance(data.get(key), list):
                    picked = data[key]
                    break
            if picked is None:
                best = []
                for value in data.values():
                    if (
                        isinstance(value, list)
                        and len(value) > len(best)
                        and value
                        and isinstance(value[0], dict)
                    ):
                        best = value
                picked = best
            data = picked
        if not isinstance(data, list) or not data:
            return [], [], False

        headers = []
        seen = set()
        # Union the keys so rows with missing fields still line up.
        for item in data[:500]:
            if not isinstance(item, dict):
                continue
            for key in item:
                if key not in seen:
                    seen.add(key)
                    headers.append(key)
        if not headers:
            return [], [], False

        rows = []
        truncated = False
        for index, item in enumerate(data):
            if is_canceled is not None and index % 5000 == 0 and is_canceled():
                return headers, rows, True
            if not isinstance(item, dict):
                continue
            rows.append(["" if item.get(k) is None else str(item.get(k)) for k in headers])
            if progress is not None and len(rows) % 25000 == 0:
                progress(len(rows))
            if max_rows is not None and len(rows) >= max_rows:
                truncated = True
                break
        return headers, rows, truncated
