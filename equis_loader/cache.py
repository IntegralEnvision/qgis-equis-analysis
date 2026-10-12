"""On-disk cache of everything pulled from EQuIS, so the plugin works offline.

Layout under the cache root (normally the QGIS profile directory):

    <root>/
      index.json                          manifest: one record per stored file
      <site_hash>/
        facilities.json.gz
        <facility_id>/
          meta.json.gz                    layers, reports, service wkid
          locations.json.gz               raw Esri feature pages, verbatim
          reports/
            <report_id>__<param_hash>.csv.gz

Keyed by site because facility ids are only unique within a site, and hashed
from the site URL rather than the API key: keys rotate, and a key refresh must
not orphan a couple of gigabytes of reports.

Report files carry a hash of the parameters that produced them. A run capped at
500,000 rows is not the same data as an uncapped one, and serving the first
where the second was asked for would silently truncate a chemical's history.

No QGIS imports, so this is testable with a plain interpreter; the caller
supplies the root path.
"""

import gzip
import hashlib
import io
import json
import os
import shutil
import tempfile
import time

# Soft ceiling for the whole cache. Reports dominate it: a 460,000-row
# analytical report keeps roughly 15-25 MB here once trimmed to the analysis
# columns and gzipped, so this holds on the order of a hundred of them.
DEFAULT_MAX_BYTES = 2 * 1024 ** 3

# Bumped when the stored shape changes in a way older files cannot satisfy.
# A mismatch discards the entry rather than trying to migrate it.
FORMAT_VERSION = 1

INDEX_NAME = "index.json"

# Entry kinds, which double as the manifest's grouping key.
FACILITIES = "facilities"
FACILITY_META = "facility_meta"
LOCATIONS = "locations"
REPORT = "report"

# Only reports are worth evicting: the others are small, and dropping them
# would strand the reports they describe (a cached report is unusable without
# its facility's locations to join against).
EVICTABLE = (REPORT,)


class CacheError(Exception):
    """A cache operation failed in a way the caller should know about."""


def site_key(site):
    """Stable directory name for a site URL.

    Normalised first so that trailing slashes and casing do not split one
    site's cache across two directories.
    """
    text = (site or "").strip().rstrip("/").lower()
    if text.startswith("https://"):
        text = text[8:]
    elif text.startswith("http://"):
        text = text[7:]
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    # A readable prefix makes the cache directory browsable by hand.
    readable = "".join(c if c.isalnum() else "_" for c in text)[:32]
    return "%s_%s" % (readable, digest) if readable else digest


def report_params_key(max_rows=None, where=None, keep_columns=None,
                      out_fields=None, eia=None, selection=None):
    """Hash the parameters that determine a report's stored output.

    `out_fields` is deliberately excluded: the service ignores it on cached
    report runs, so two calls differing only there produce identical output.
    `keep_columns` is what actually decides which columns survive parsing.

    `eia` is the information agent event the rows were read from, or None for
    a live run. It belongs in the key because the two are different datasets:
    an agent's output is frozen at its last execution and scoped to whatever
    it was scheduled to produce, so letting one satisfy a read for the other
    would serve stale or differently-filtered rows as though they were fresh.

    `selection` is the filters saved onto the report for a live run. They
    decide which rows the report generates at all, so two selections of one
    report are two datasets: without this, asking for dissolved results would
    be answered from a total-fraction run stored under the same key.
    """
    columns = sorted(str(c).strip().upper() for c in (keep_columns or []))
    payload = json.dumps(
        {
            "max_rows": max_rows,
            "where": (where or "").strip(),
            "keep_columns": columns,
            "eia": eia,
            # Sorted so that two equal selections built in different orders
            # hash the same; a dict would otherwise key on insertion order.
            "selection": (sorted((str(k), str(v))
                                 for k, v in selection.items())
                          if isinstance(selection, dict) else selection),
        },
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def describe_age(seconds):
    """Human phrasing for how old an entry is, for status text."""
    if seconds < 0:
        seconds = 0
    minutes = seconds / 60.0
    if minutes < 1:
        return "just now"
    if minutes < 60:
        count = int(minutes)
        return "%d minute%s ago" % (count, "" if count == 1 else "s")
    hours = minutes / 60.0
    if hours < 24:
        count = int(hours)
        return "%d hour%s ago" % (count, "" if count == 1 else "s")
    days = hours / 24.0
    count = int(days)
    return "%d day%s ago" % (count, "" if count == 1 else "s")


def describe_size(count):
    """Human-readable byte count, matching equis_api's phrasing."""
    value = float(count)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "bytes":
                return "%d bytes" % value
            return "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%d bytes" % count


class CacheEntry:
    """What the manifest knows about one stored file.

    The UI needs more than "is it cached": how old it is decides whether to
    warn, and `truncated` has to travel with the data so a partial report can
    never be presented as a complete one.
    """

    def __init__(self, record):
        self.kind = record.get("kind", "")
        self.site = record.get("site", "")
        self.facility_id = record.get("facility_id")
        self.report_id = record.get("report_id")
        self.path = record.get("path", "")
        self.bytes = record.get("bytes", 0)
        self.fetched_at = record.get("fetched_at", 0)
        self.row_count = record.get("row_count", 0)
        self.truncated = bool(record.get("truncated", False))
        self.params = record.get("params") or {}
        self.label = record.get("label", "")

    @property
    def age_seconds(self):
        return max(0.0, time.time() - (self.fetched_at or 0))

    @property
    def age_text(self):
        return describe_age(self.age_seconds)

    def fetched_text(self, fmt="%d %b %H:%M"):
        if not self.fetched_at:
            return "unknown"
        return time.strftime(fmt, time.localtime(self.fetched_at))

    def as_record(self):
        return {
            "kind": self.kind,
            "site": self.site,
            "facility_id": self.facility_id,
            "report_id": self.report_id,
            "path": self.path,
            "bytes": self.bytes,
            "fetched_at": self.fetched_at,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "params": self.params,
            "label": self.label,
        }


class EquisCache:
    """Read/write access to the on-disk cache.

    Every write is atomic (temp file then replace) so an interrupted QGIS
    cannot leave a half-written report that later reads as valid, and every
    read failure degrades to a miss rather than an exception: a corrupt cache
    must never be worse than an empty one.
    """

    def __init__(self, root, max_bytes=DEFAULT_MAX_BYTES):
        self.root = root
        # A negative cap would make evict_to_fit() delete everything on the
        # first write. That is exactly what a 32-bit overflow produces when
        # the 2 GB default round-trips through QSettings on Windows, so guard
        # it here too rather than trusting the caller.
        self.max_bytes = max_bytes if (max_bytes or 0) > 0 else DEFAULT_MAX_BYTES
        self._index = None

    # ------------------------------------------------------------- manifest

    @property
    def index_path(self):
        return os.path.join(self.root, INDEX_NAME)

    def _load_index(self):
        if self._index is not None:
            return self._index
        records = {}
        try:
            with open(self.index_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if data.get("version") == FORMAT_VERSION:
                for key, record in (data.get("entries") or {}).items():
                    records[key] = record
        except (OSError, ValueError):
            # No manifest, or an unreadable one. Rebuilding from the files on
            # disk is possible but not worth it: the payloads are re-fetchable
            # and a stale manifest is what caused the problem.
            records = {}
        self._index = records
        return records

    def _save_index(self):
        if self._index is None:
            return
        payload = {"version": FORMAT_VERSION, "entries": self._index}
        try:
            os.makedirs(self.root, exist_ok=True)
            _atomic_write(self.index_path, json.dumps(payload).encode("utf-8"))
        except OSError as exc:
            raise CacheError("Could not write the cache index: %s" % exc)

    def _put_record(self, key, record):
        index = self._load_index()
        index[key] = record
        self._save_index()

    def _drop_record(self, key):
        index = self._load_index()
        if index.pop(key, None) is not None:
            self._save_index()

    # ------------------------------------------------------------ locations

    def _site_dir(self, site):
        return os.path.join(self.root, site_key(site))

    def _facility_dir(self, site, facility_id):
        return os.path.join(self._site_dir(site), str(facility_id))

    def _report_path(self, site, facility_id, report_id, params_key):
        return os.path.join(
            self._facility_dir(site, facility_id), "reports",
            "%s__%s.csv.gz" % (report_id, params_key),
        )

    @staticmethod
    def _key(kind, site, facility_id=None, report_id=None, params_key=None):
        parts = [kind, site_key(site)]
        if facility_id is not None:
            parts.append(str(facility_id))
        if report_id is not None:
            parts.append(str(report_id))
        if params_key:
            parts.append(params_key)
        return "|".join(parts)

    # --------------------------------------------------------------- writes

    def put_facilities(self, site, facilities):
        """Store the facility list for a site."""
        path = os.path.join(self._site_dir(site), "facilities.json.gz")
        size = _write_json_gz(path, facilities)
        self._put_record(
            self._key(FACILITIES, site),
            {
                "kind": FACILITIES, "site": site, "path": path, "bytes": size,
                "fetched_at": time.time(), "row_count": len(facilities or []),
                "label": "Facility list",
            },
        )
        return size

    def put_facility_meta(self, site, facility_id, layers, reports,
                          service_wkid, facility=None):
        """Store a facility's layer list, report list and projection.

        `facility` is the facility record itself; keeping a copy means offline
        mode can populate the facility combo entry for a site whose facility
        list was never cached.
        """
        path = os.path.join(self._facility_dir(site, facility_id), "meta.json.gz")
        payload = {
            "layers": layers or [],
            "reports": reports or [],
            "service_wkid": service_wkid or 0,
            "facility": facility or {},
        }
        size = _write_json_gz(path, payload)
        self._put_record(
            self._key(FACILITY_META, site, facility_id),
            {
                "kind": FACILITY_META, "site": site, "facility_id": facility_id,
                "path": path, "bytes": size, "fetched_at": time.time(),
                "row_count": len(reports or []),
                "label": (facility or {}).get("name", "Facility %s" % facility_id),
            },
        )
        return size

    def put_locations(self, site, facility_id, pages, has_geometry=None):
        """Store the raw Esri feature pages for a facility's locations.

        Stored verbatim, including `fields`, `spatialReference` and the
        `equisGeometryUnavailable` marker, so layer_builder consumes a cached
        read exactly as it consumes a live one.
        """
        serialisable = [
            {"feature_set": feature_set, "features": features}
            for feature_set, features in (pages or [])
        ]
        count = sum(len(entry["features"]) for entry in serialisable)
        if has_geometry is None:
            has_geometry = any(
                f.get("geometry")
                for entry in serialisable for f in entry["features"]
            )
        path = os.path.join(self._facility_dir(site, facility_id),
                            "locations.json.gz")
        size = _write_json_gz(
            path, {"pages": serialisable, "has_geometry": bool(has_geometry)}
        )
        self._put_record(
            self._key(LOCATIONS, site, facility_id),
            {
                "kind": LOCATIONS, "site": site, "facility_id": facility_id,
                "path": path, "bytes": size, "fetched_at": time.time(),
                "row_count": count, "label": "Locations",
            },
        )
        return size

    def put_report(self, site, facility_id, report_id, headers, rows,
                   truncated=False, params=None, label=""):
        """Store one report run's parsed rows.

        The parsed rows are stored rather than the raw response: by this point
        the unzipping, header sniffing and column trimming are already done,
        and keeping only the retained columns is what makes a 460,000-row
        report affordable to cache at all.
        """
        params = params or {}
        key = report_params_key(**params)
        path = self._report_path(site, facility_id, report_id, key)
        size = _write_csv_gz(path, headers, rows)
        self._put_record(
            self._key(REPORT, site, facility_id, report_id, key),
            {
                "kind": REPORT, "site": site, "facility_id": facility_id,
                "report_id": report_id, "path": path, "bytes": size,
                "fetched_at": time.time(), "row_count": len(rows or []),
                "truncated": bool(truncated), "params": params,
                "label": label or "Report %s" % report_id,
            },
        )
        self.evict_to_fit()
        return size

    # ---------------------------------------------------------------- reads

    def _read(self, key, loader):
        """Look a key up in the manifest and load its payload.

        Returns (payload, CacheEntry) or (None, None). A file that has gone
        missing or will not parse is dropped from the manifest so the next
        read is a clean miss rather than a repeated failure.
        """
        index = self._load_index()
        record = index.get(key)
        if not record:
            return None, None
        path = record.get("path", "")
        try:
            payload = loader(path)
        except (OSError, ValueError, EOFError):
            self._drop_record(key)
            return None, None
        if payload is None:
            self._drop_record(key)
            return None, None
        entry = CacheEntry(record)
        self._touch(key)
        return payload, entry

    def _touch(self, key):
        """Record that an entry was used, so eviction can favour the rest.

        Best-effort by design: this runs on every read, and losing the usage
        stamp only makes eviction slightly less well informed. Letting it
        raise would turn a locked index file into a failed report load.
        """
        index = self._load_index()
        record = index.get(key)
        if record is None:
            return
        record["used_at"] = time.time()
        try:
            self._save_index()
        except CacheError:
            pass

    def get_facilities(self, site):
        return self._read(self._key(FACILITIES, site), _read_json_gz)

    def get_facility_meta(self, site, facility_id):
        return self._read(self._key(FACILITY_META, site, facility_id),
                          _read_json_gz)

    def get_locations(self, site, facility_id):
        """Cached location pages, in the (feature_set, features) shape."""
        payload, entry = self._read(self._key(LOCATIONS, site, facility_id),
                                    _read_json_gz)
        if payload is None:
            return None, None
        pages = [
            (item.get("feature_set") or {}, item.get("features") or [])
            for item in (payload.get("pages") or [])
        ]
        return {"pages": pages,
                "has_geometry": bool(payload.get("has_geometry"))}, entry

    def get_report(self, site, facility_id, report_id, params=None,
                   allow_truncated=True):
        """Cached rows for one report run, or (None, None).

        A truncated entry is refused when the caller asked for an unbounded
        run: serving it would present a knowingly incomplete report as the
        whole history, which is the failure this cache must not introduce.
        """
        params = params or {}
        key = self._key(REPORT, site, facility_id, report_id,
                        report_params_key(**params))
        payload, entry = self._read(key, _read_csv_gz)
        if payload is None:
            return None, None
        if entry.truncated and not allow_truncated:
            return None, None
        headers, rows = payload
        return {"headers": headers, "rows": rows,
                "truncated": entry.truncated}, entry

    def report_entry(self, site, facility_id, report_id, params=None):
        """The manifest record for a report, without loading its rows.

        Lets the UI mark which reports are cached, and how old they are,
        without paying to decompress a few hundred thousand rows.
        """
        params = params or {}
        key = self._key(REPORT, site, facility_id, report_id,
                        report_params_key(**params))
        record = self._load_index().get(key)
        if not record:
            return None
        if not os.path.exists(record.get("path", "")):
            self._drop_record(key)
            return None
        return CacheEntry(record)

    # ----------------------------------------------------------- management

    def list_entries(self, site=None, facility_id=None, kind=None):
        """Manifest records, newest first, for the cache browser."""
        entries = []
        for record in self._load_index().values():
            if site is not None and site_key(record.get("site")) != site_key(site):
                continue
            if facility_id is not None and record.get("facility_id") != facility_id:
                continue
            if kind is not None and record.get("kind") != kind:
                continue
            entries.append(CacheEntry(record))
        entries.sort(key=lambda e: e.fetched_at or 0, reverse=True)
        return entries

    def total_bytes(self, site=None):
        return sum(e.bytes or 0 for e in self.list_entries(site=site))

    def cached_facility_ids(self, site):
        """Facilities with enough cached to be usable offline.

        A facility needs its metadata; without locations its results cannot be
        mapped, but the report is still worth opening, so metadata is the bar.
        """
        ids = []
        for entry in self.list_entries(site=site, kind=FACILITY_META):
            if entry.facility_id is not None:
                ids.append(entry.facility_id)
        return ids

    def evict_to_fit(self):
        """Drop least-recently-used reports until the cache fits its cap.

        Returns the number of entries removed. Only reports are evicted: the
        metadata entries are tiny, and removing them would strand the reports
        that depend on them.
        """
        if not self.max_bytes:
            return 0
        index = self._load_index()
        total = sum(record.get("bytes") or 0 for record in index.values())
        if total <= self.max_bytes:
            return 0

        candidates = [
            (key, record) for key, record in index.items()
            if record.get("kind") in EVICTABLE
        ]
        # Least recently used first, falling back to fetch time for entries
        # that have never been read back.
        candidates.sort(
            key=lambda item: item[1].get("used_at") or item[1].get("fetched_at") or 0
        )
        removed = 0
        for key, record in candidates:
            if total <= self.max_bytes:
                break
            _unlink(record.get("path", ""))
            index.pop(key, None)
            total -= record.get("bytes") or 0
            removed += 1
        if removed:
            self._save_index()
        return removed

    def clear(self, site=None, facility_id=None):
        """Delete cached files, optionally narrowed to a site or facility."""
        index = self._load_index()
        doomed = []
        for key, record in index.items():
            if site is not None and site_key(record.get("site")) != site_key(site):
                continue
            if facility_id is not None and record.get("facility_id") != facility_id:
                continue
            doomed.append(key)
        for key in doomed:
            _unlink(index[key].get("path", ""))
            index.pop(key, None)
        self._save_index()

        # Sweep the now-empty directories so the cache root does not fill with
        # husks. A whole-cache clear takes the tree with it.
        if site is None and facility_id is None:
            for name in os.listdir(self.root) if os.path.isdir(self.root) else []:
                if name == INDEX_NAME:
                    continue
                shutil.rmtree(os.path.join(self.root, name), ignore_errors=True)
        elif site is not None and facility_id is not None:
            shutil.rmtree(self._facility_dir(site, facility_id), ignore_errors=True)
        elif site is not None:
            shutil.rmtree(self._site_dir(site), ignore_errors=True)
        return len(doomed)


# --------------------------------------------------------------------- io


def _atomic_write(path, data, retries=3):
    """Write bytes via a temp file in the same directory, then replace.

    QGIS can be killed mid-write, and a half-written report that still parses
    is worse than no cache at all.

    On Windows os.replace fails with a sharing violation whenever anything
    else holds the destination open - an antivirus scanner mid-file, a backup
    agent, or a second QGIS window using the same profile. Those clear in
    milliseconds, so a couple of short retries turn a hard failure back into a
    successful write.
    """
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        for attempt in range(max(1, retries)):
            try:
                os.replace(temp, path)
                break
            except OSError:
                if attempt == retries - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        _unlink(temp)
        raise
    return len(data)


def _unlink(path):
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _write_json_gz(path, payload):
    buffer = io.BytesIO()
    # mtime=0 keeps the bytes deterministic, which makes the tests meaningful.
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
        gz.write(json.dumps(payload).encode("utf-8"))
    return _atomic_write(path, buffer.getvalue())


def _read_json_gz(path):
    with gzip.open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def _write_csv_gz(path, headers, rows):
    """Store a report as gzipped CSV.

    csv rather than json: at a few hundred thousand rows the per-row list
    overhead of JSON is real, and the report is already rectangular.
    """
    import csv

    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
        text = io.TextIOWrapper(gz, encoding="utf-8", newline="")
        writer = csv.writer(text)
        writer.writerow(list(headers or []))
        for row in rows or []:
            writer.writerow(row)
        text.flush()
        text.detach()
    return _atomic_write(path, buffer.getvalue())


def _read_csv_gz(path):
    import csv

    with gzip.open(path, "rb") as handle:
        text = io.TextIOWrapper(handle, encoding="utf-8", newline="")
        reader = csv.reader(text)
        try:
            headers = next(reader)
        except StopIteration:
            return None
        rows = [row for row in reader]
    return headers, rows
