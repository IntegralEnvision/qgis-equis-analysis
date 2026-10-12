"""Connection settings, stored in QSettings with a .env fallback."""

import os

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QSettings

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
    settings = QSettings()
    settings.beginGroup(GROUP)
    site = settings.value("site", "", type=str)
    api_key = settings.value("api_key", "", type=str)
    verify_ssl = settings.value("verify_ssl", True, type=bool)
    source = settings.value("source", SOURCE_LIVE, type=str)
    parquet_url = settings.value("parquet_url", "", type=str)
    settings.endGroup()

    # First run: seed from .env so the plugin works out of the box.
    if not api_key:
        defaults = env_defaults()
        site = site or defaults["site"]
        api_key = defaults["api_key"]
    if not parquet_url:
        parquet_url = env_defaults().get("parquet_url", "")
    # An unknown value in QSettings means live, not a broken panel: this is
    # read on every start, and a profile written by a future version should
    # degrade to the source that always works.
    if source not in SOURCES:
        source = SOURCE_LIVE
    return {"site": site or DEFAULT_SITE, "api_key": api_key,
            "verify_ssl": verify_ssl, "source": source,
            "parquet_url": parquet_url}


def save(site, api_key, verify_ssl=True, source=None, parquet_url=None):
    """Persist the connection settings.

    `source` and `parquet_url` default to None meaning "leave as they are",
    so the existing two-argument callers keep working unchanged.
    """
    settings = QSettings()
    settings.beginGroup(GROUP)
    settings.setValue("site", site)
    settings.setValue("api_key", api_key)
    settings.setValue("verify_ssl", verify_ssl)
    if source is not None:
        settings.setValue("source", source if source in SOURCES
                          else SOURCE_LIVE)
    if parquet_url is not None:
        settings.setValue("parquet_url", parquet_url)
    settings.endGroup()


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
