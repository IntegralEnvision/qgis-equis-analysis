"""Connection settings, stored in QSettings with a .env fallback.

What a project maps is the exception, split across two stores on purpose
(see parquet_address.py): the source, the EQuIS site and facility, and a
parquet source's address belong to the QGIS project; the credentials - the
API key, a signed URL's token - belong to the person, looked up by site or
address, and never reach the project file.
"""

import json
import os

from qgis.core import QgsApplication, QgsProject
from qgis.PyQt.QtCore import QSettings

from . import parquet_address
from .cache import DEFAULT_MAX_BYTES, EquisCache
from .layer_builder import (
    clamp_spark_range,
    GEOPACKAGE,
    MEMORY,
    SPARK_MAX_MM,
    SPARK_MIN_MM,
)

GROUP = "equis_loader"
DEFAULT_SITE = "https://yoursite.equisonline.com"

# Where results come from. LIVE queries the EQuIS API; PARQUET reads a
# published parquet set through its manifest - the same rows, already
# extracted, with no ~230 s report ceiling and no API key.
SOURCE_LIVE = "live"
SOURCE_PARQUET = "parquet"
SOURCES = (SOURCE_LIVE, SOURCE_PARQUET)

# The cache lives in the QGIS profile, not in the temp directory: it is meant
# to survive reboots and temp sweeps, since a cached report can represent
# minutes of waiting and hundreds of MB of transfer.
CACHE_DIRNAME = "equis_cache"


def _read_env_file(path):
    """Parse the simple `key: value` .env used by this project."""
    values = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                for sep in (":", "="):
                    if sep in line:
                        key, value = line.split(sep, 1)
                        values[key.strip().lower()] = value.strip().strip("'\"")
                        break
    except OSError:
        return {}
    return values


def env_defaults():
    """Look for a .env beside the plugin or in the project folder above it."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        os.path.join(here, ".env"),
        os.path.join(os.path.dirname(here), ".env"),
    ):
        if os.path.isfile(candidate):
            values = _read_env_file(candidate)
            if values:
                return {
                    "site": values.get("equis_site", "") or DEFAULT_SITE,
                    "api_key": values.get("api_key", ""),
                    "parquet_url": values.get("parquet_url", ""),
                }
    return {"site": DEFAULT_SITE, "api_key": "", "parquet_url": ""}


def stale_saved_key():
    """(saved_expiry, env_key) when the saved key is dead and .env has a live one.

    QSettings wins over the .env, which is right - a key pasted into the
    panel should not be undone by a file on disk. But it means a key saved
    once and since expired keeps being used while a newer one sits in the
    .env beside the plugin, and every request 401s. The plugin cannot tell
    that from a server outage without looking, so look here and let the
    caller offer the swap.

    Returns (None, None) when the saved key is fine, absent, or when the
    .env has nothing better.
    """
    # Imported lazily: settings.py is imported by the plugin entry point and
    # equis_api pulls in more than it needs at that moment.
    # utc_now() rather than datetime.now(timezone.utc): this codebase keeps
    # UTC naive throughout, and mixing the two raises on comparison.
    from .equis_api import token_expiry, utc_now

    saved = load().get("api_key") or ""
    fresh = (env_defaults() or {}).get("api_key") or ""
    if not saved or not fresh or saved == fresh:
        return None, None
    saved_expiry = token_expiry(saved)
    if saved_expiry is None:
        return None, None
    now = utc_now()
    if saved_expiry > now:
        return None, None
    fresh_expiry = token_expiry(fresh)
    if fresh_expiry is not None and fresh_expiry <= now:
        return None, None      # Both dead; nothing to offer.
    return saved_expiry, fresh


def load():
    """The person's connection settings: their defaults.

    A project that records a site, a source or a parquet address overrides
    these (`load_project_source`, `load_project_live`). `site` and `source`
    are the person's last choices, for a project that has made none, and
    `api_key` is the one they hold for that site. `parquet_url` is only the
    .env's: it used to be the last URL opened in any project in this
    profile, which put project A's source in front of project B.
    """
    _migrate_parquet_url()
    _migrate_api_key()
    settings = QSettings()
    settings.beginGroup(GROUP)
    site = settings.value("site", "", type=str)
    verify_ssl = settings.value("verify_ssl", True, type=bool)
    source = settings.value("source", SOURCE_LIVE, type=str)
    settings.endGroup()
    api_key = api_key_for(site)

    # First run: seed from .env so the plugin works out of the box.
    if not api_key:
        defaults = env_defaults()
        site = site or defaults["site"]
        api_key = defaults["api_key"]
    parquet_url = env_defaults().get("parquet_url", "")
    # An unknown value in QSettings means live, not a broken panel: this is
    # read on every start, and a profile written by a future version should
    # degrade to the source that always works.
    if source not in SOURCES:
        source = SOURCE_LIVE
    return {"site": site or DEFAULT_SITE, "api_key": api_key,
            "verify_ssl": verify_ssl, "source": source,
            "parquet_url": parquet_url}


def save(site, api_key, verify_ssl=True, source=None):
    """Persist the connection settings.

    `source` defaults to None meaning "leave as it is", so the existing
    two-argument callers keep working unchanged. The key is filed under
    `site`, so a project on another tenant does not send this one's key. A
    parquet URL is not saved here: its token goes to
    `remember_parquet_token`, its address to the project
    (`save_project_source`).
    """
    settings = QSettings()
    settings.beginGroup(GROUP)
    settings.setValue("site", site)
    settings.setValue("verify_ssl", verify_ssl)
    if source is not None:
        settings.setValue("source", source if source in SOURCES
                          else SOURCE_LIVE)
    settings.endGroup()
    remember_api_key(site, api_key)


# -------------------------------------------- credentials, by site/address

# Each book is one JSON object, {site: key} or {address: token}. One value
# rather than a key per entry because a site or address is a URL, and
# QSettings reads every "/" in a key as a group: "https://acct/.../facility_a"
# would be stored as five nested groups and come back in pieces.
TOKENS_KEY = "parquet_tokens"
API_KEYS_KEY = "api_keys"

# In the project file: <properties><equis_loader>...</equis_loader>.
PROJECT_SCOPE = GROUP
PROJECT_SOURCE = "source"
PROJECT_ADDRESS = "parquet_address"
PROJECT_SITE = "site"
PROJECT_FACILITY = "facility_id"
# The facility's name, for saying which one when it cannot be found; the id
# is what selects it.
PROJECT_FACILITY_NAME = "facility_name"


def site_key(site):
    """One spelling per site: `https://x.equisonline.com/` is `.../x...com`."""
    return (site or "").strip().rstrip("/")


def _load_book(name):
    raw = QSettings().value("%s/%s" % (GROUP, name), "", type=str)
    try:
        book = json.loads(raw) if raw else {}
    except ValueError:
        # A hand-edited or truncated value costs the person a paste, not
        # the panel.
        return {}
    if not isinstance(book, dict):
        return {}
    return {str(k): str(v) for k, v in book.items() if k and v}


def _save_book(name, book, before):
    if book != before:
        QSettings().setValue("%s/%s" % (GROUP, name),
                             json.dumps(book, sort_keys=True))


def load_parquet_tokens():
    """{address: token} for every signed source this person has opened."""
    return _load_book(TOKENS_KEY)


def remember_parquet_token(url):
    """Record the token `url` carries, if any, under its address."""
    book = load_parquet_tokens()
    _save_book(TOKENS_KEY, parquet_address.remember(book, url), book)


def api_key_for(site):
    """The API key this person holds for `site`, or ""."""
    return _load_book(API_KEYS_KEY).get(site_key(site), "")


def remember_api_key(site, api_key):
    """File `api_key` under `site`; an empty key forgets the site's.

    Forgetting on empty matches what the panel did before, when one saved
    key was overwritten by whatever the field held at Connect.
    """
    site = site_key(site)
    if not site:
        return
    book = _load_book(API_KEYS_KEY)
    updated = dict(book)
    if api_key:
        updated[site] = api_key
    else:
        updated.pop(site, None)
    _save_book(API_KEYS_KEY, updated, book)


def _migrate_api_key():
    """File a key saved by an earlier version under the site saved with it.

    Earlier versions kept one key, for whatever site was last connected.
    """
    settings = QSettings()
    key = "%s/api_key" % GROUP
    if not settings.contains(key):
        return
    legacy = settings.value(key, "", type=str)
    site = settings.value("%s/site" % GROUP, "", type=str)
    if legacy and site_key(site) and not api_key_for(site):
        remember_api_key(site, legacy)
    settings.remove(key)


def _migrate_parquet_url():
    """Move a URL saved by an earlier version into the token book.

    Earlier versions kept the whole last-opened URL, token included, in
    QSettings. Its token is kept, so a project naming that address opens
    without a paste; the URL itself is removed, so it no longer fills the
    field of a project it has nothing to do with.
    """
    settings = QSettings()
    key = "%s/parquet_url" % GROUP
    if not settings.contains(key):
        return
    legacy = settings.value(key, "", type=str)
    if legacy:
        remember_parquet_token(legacy)
    settings.remove(key)


def load_project_source(project=None):
    """(source, address) the project records; None for what it does not.

    A project that has never opened a source records neither, and the
    caller falls back to the person's defaults - not to another project's.
    """
    project = project or QgsProject.instance()
    source, ok = project.readEntry(PROJECT_SCOPE, PROJECT_SOURCE, "")
    source = source if ok and source in SOURCES else None
    address, ok = project.readEntry(PROJECT_SCOPE, PROJECT_ADDRESS, "")
    address = address.strip() if ok and address.strip() else None
    return source, address


def save_project_source(source, url=None, project=None):
    """Record in the project which source it maps and, for parquet, where.

    Only the ADDRESS of `url` is written; the token never reaches the
    project file. Re-opening the same source does not mark the project
    modified: writeEntry with the value already there is not a change to
    QGIS (4.2.3, checked by tools/test_project_source.py, which passed with
    a compare-before-write here removed).
    """
    project = project or QgsProject.instance()
    project.writeEntry(PROJECT_SCOPE, PROJECT_SOURCE,
                       source if source in SOURCES else SOURCE_LIVE)
    if url is not None:
        address, _token = parquet_address.split(url)
        if address:
            project.writeEntry(PROJECT_SCOPE, PROJECT_ADDRESS, address)


def cache_root():
    """Where cached EQuIS data lives, inside the active QGIS profile."""
    return os.path.join(QgsApplication.qgisSettingsDirPath(), CACHE_DIRNAME)


def load_storage():
    """How result layers should be stored: GEOPACKAGE or MEMORY.

    GeoPackage by default: writing 10,000 features costs about a quarter of a
    second, and it is the difference between a saved project reopening with
    its data and reopening empty.
    """
    settings = QSettings()
    settings.beginGroup(GROUP)
    value = settings.value("storage", GEOPACKAGE, type=str)
    settings.endGroup()
    return value if value in (MEMORY, GEOPACKAGE) else GEOPACKAGE


def save_storage(storage):
    settings = QSettings()
    settings.beginGroup(GROUP)
    settings.setValue("storage", storage if storage in (MEMORY, GEOPACKAGE)
                      else GEOPACKAGE)
    settings.endGroup()


def load_read_only():
    """Whether the plugin may write to the tenant. Off unless chosen.

    On, it never creates, saves or deletes a report or files one into a
    group - only existing reports and stored agent output are read. Kept
    across sessions because the reason for it (a read-only key, an
    administrator's rule) does not change between sessions either.
    """
    settings = QSettings()
    settings.beginGroup(GROUP)
    value = settings.value("read_only", False, type=bool)
    settings.endGroup()
    return bool(value)


def save_read_only(read_only):
    settings = QSettings()
    settings.beginGroup(GROUP)
    settings.setValue("read_only", bool(read_only))
    settings.endGroup()


CHEMDUCK_SHADOW = "chemduck_shadow"
CHEMDUCK_MODE = "chemduck_mode"
CHEMDUCK_ENV = "EQUIS_CHEMDUCK"
# What answers the panel's chemistry (docs/chemduck-adoption-plan.md):
#   off      the plugin's own rules (ReportStore), as always
#   shadow   the same, with ChemDuck loaded beside it and each map's two
#            answers logged to the "EQuIS ChemDuck" tab (phase 1)
#   backend  ChemDuck answers the hot path - maps, footer counts, time
#            series, sparklines - where it can be asked (chem_backend.py)
CHEMDUCK_MODES = ("off", "shadow", "backend")


def chemduck_mode_from_env():
    """The mode EQUIS_CHEMDUCK names, or None when unset or unknown.

    The command-line switch: `set EQUIS_CHEMDUCK=backend` before starting
    QGIS (or `EQUIS_CHEMDUCK=backend qgis`). It wins over the saved choice
    for that session and is not saved, so a trial leaves the profile as it
    was; the panel's Chemistry box shows it and is disabled meanwhile.
    "1" and "on" mean backend.
    """
    value = (os.environ.get(CHEMDUCK_ENV) or "").strip().lower()
    if value in ("1", "on", "true", "yes"):
        return "backend"
    return value if value in CHEMDUCK_MODES else None


def load_chemduck_mode():
    """The chemistry mode: the environment's, else the saved one, else
    "shadow" where the older debug switch (chemduck_shadow) is set, else
    "off"."""
    from_env = chemduck_mode_from_env()
    if from_env:
        return from_env
    settings = QSettings()
    settings.beginGroup(GROUP)
    saved = str(settings.value(CHEMDUCK_MODE, "", type=str) or "").strip().lower()
    legacy = settings.value(CHEMDUCK_SHADOW, False, type=bool)
    settings.endGroup()
    if saved in CHEMDUCK_MODES:
        return saved
    return "shadow" if legacy else "off"


def save_chemduck_mode(mode):
    if mode not in CHEMDUCK_MODES:
        raise ValueError("chemistry mode %r is not one of %s" % (mode, CHEMDUCK_MODES))
    settings = QSettings()
    settings.beginGroup(GROUP)
    settings.setValue(CHEMDUCK_MODE, mode)
    settings.endGroup()


def load_chemduck_shadow():
    """Whether ChemDuck is loaded beside the store: in "shadow" mode, to log
    each map's two answers, and in "backend", to answer. Off unless chosen
    (load_chemduck_mode). The log is the "EQuIS ChemDuck" tab."""
    return load_chemduck_mode() != "off"


def geopackage_for_project(project_path, facility_name=""):
    """Where a project's EQuIS layers should be written.

    Beside the .qgz, so the project and its data travel together: copying or
    zipping the folder moves a working project. Returns None when the project
    has never been saved, since there is nothing to sit beside yet.
    """
    if not project_path:
        return None
    directory = os.path.dirname(project_path)
    stem = os.path.splitext(os.path.basename(project_path))[0] or "project"
    if not directory:
        return None
    return os.path.join(directory, "%s_equis.gpkg" % stem)


def load_spark_range():
    """The (smallest, largest) millimetre size the sparkline markers scale to.

    Stored as text and parsed here for the same reason as the cache cap:
    QSettings round-trips numbers through platform types, and a value that
    comes back as something unexpected should fall back to the defaults
    rather than clamp every marker to nothing.
    """
    settings = QSettings()
    settings.beginGroup(GROUP)
    low = settings.value("spark_min_mm", "")
    high = settings.value("spark_max_mm", "")
    settings.endGroup()
    try:
        return clamp_spark_range(float(str(low).strip()),
                                 float(str(high).strip()))
    except (TypeError, ValueError):
        return SPARK_MIN_MM, SPARK_MAX_MM


def save_spark_range(min_mm, max_mm):
    low, high = clamp_spark_range(min_mm, max_mm)
    settings = QSettings()
    settings.beginGroup(GROUP)
    settings.setValue("spark_min_mm", "%g" % low)
    settings.setValue("spark_max_mm", "%g" % high)
    settings.endGroup()


def cache_max_bytes():
    """The cache size cap, in bytes.

    Read as text and converted here rather than with `type=int`: QSettings
    coerces through a 32-bit int on Windows, so the 2 GB default comes back as
    -2147483648 and every write would evict the whole cache.
    """
    settings = QSettings()
    settings.beginGroup(GROUP)
    raw = settings.value("cache_max_bytes", "")
    settings.endGroup()
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_MAX_BYTES
    # A zero or negative cap would mean "evict everything"; treat it as unset.
    return value if value > 0 else DEFAULT_MAX_BYTES


def open_cache():
    """The cache instance the plugin should use."""
    return EquisCache(cache_root(), max_bytes=cache_max_bytes())


def load_project_live(project=None):
    """(site, facility_id, facility_name) the project records, None for each
    it does not.

    The facility id is EQuIS's, so it means something only on that site:
    the caller selects it only when connected to the same one.
    """
    project = project or QgsProject.instance()
    out = []
    for key in (PROJECT_SITE, PROJECT_FACILITY, PROJECT_FACILITY_NAME):
        value, ok = project.readEntry(PROJECT_SCOPE, key, "")
        out.append(value.strip() if ok and value.strip() else None)
    site, facility_id, name = out
    return (site_key(site) or None), facility_id, name


def save_project_live(site, facility, project=None):
    """Record that the project maps `facility` on `site`, from EQuIS live.

    Called when a facility is chosen - the act that says what the project
    maps. Connecting alone records nothing new (see the dialog's
    _on_connect). The API key is never written; the site names whose key
    to use.
    """
    project = project or QgsProject.instance()
    project.writeEntry(PROJECT_SCOPE, PROJECT_SOURCE, SOURCE_LIVE)
    project.writeEntry(PROJECT_SCOPE, PROJECT_SITE, site_key(site))
    if facility and facility.get("id") is not None:
        project.writeEntry(PROJECT_SCOPE, PROJECT_FACILITY,
                           str(facility["id"]))
        project.writeEntry(PROJECT_SCOPE, PROJECT_FACILITY_NAME,
                           facility.get("name") or "")
