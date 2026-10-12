"""Serving the plugin's web assets over a custom URL scheme.

Phase 2.2. `file://` forbids ES module imports, so a bundled vgplot build
cannot simply be loaded off disk. The documented way round it is a custom
scheme, and `tools/test_webengine.py` stage 7 proved on QGIS 4.2.1 that one
can be registered from inside a plugin even though Qt's documentation says
registration must happen before the application exists.

That measurement is why there is no local HTTP server here: no port to pick,
nothing for a corporate firewall or an endpoint agent to object to.

THREE HAZARDS, ALL FOUND BY THE PROBE BEFORE THIS FILE EXISTED:

1. A scheme cannot be registered twice in one process, and a plugin is
   reloaded often during development. `ensure_scheme()` is idempotent and
   checks `schemeByName` first.
2. Qt refuses to install a second handler over an existing one - it keeps
   the FIRST. So a reload that built a new handler would leave an orphan
   serving stale files while the live object was ignored. The handler is
   therefore held in a module-level registry that survives a reload, and its
   root is repointed instead of the handler being replaced.
3. The reply device must outlive `requestStarted()`. A QBuffer parented to
   the job survives; a local one is collected and the page gets nothing.

PATHS ARE CONFINED TO THE ROOT. `requestStarted` resolves what it is asked
for and refuses anything that escapes, because the page is the one part of
this plugin that could be asked to fetch an arbitrary path.
"""

import os

from qgis.PyQt.QtCore import QBuffer, QByteArray, QUrl

try:
    from qgis.PyQt.QtWebEngineCore import (QWebEngineProfile,
                                           QWebEngineUrlScheme,
                                           QWebEngineUrlSchemeHandler)
except ImportError:  # pragma: no cover - Qt5 layout, or no WebEngine at all
    try:
        from qgis.PyQt.QtWebEngineWidgets import (QWebEngineProfile,
                                                  QWebEngineUrlScheme,
                                                  QWebEngineUrlSchemeHandler)
    except ImportError:
        QWebEngineProfile = QWebEngineUrlScheme = None
        QWebEngineUrlSchemeHandler = object

SCHEME = "equis"
INDEX = "index.html"

# Kept at module scope on purpose: a plugin reload rebuilds classes but does
# not restart the process, and both the scheme registration and the installed
# handler are process-wide. See hazards 1 and 2 above.
_HANDLERS = {}

MIME = {
    ".html": b"text/html",
    ".js": b"text/javascript",
    ".mjs": b"text/javascript",
    ".css": b"text/css",
    ".json": b"application/json",
    ".svg": b"image/svg+xml",
    ".wasm": b"application/wasm",
    ".woff2": b"font/woff2",
    ".map": b"application/json",
}


class WebAssetsError(RuntimeError):
    pass


def available():
    """Whether this QGIS build has the classes at all."""
    return QWebEngineUrlScheme is not None


def ensure_scheme(name=SCHEME):
    """Register the scheme if it is not already. Safe to call repeatedly.

    Returns True when the scheme is usable afterwards. Registration must
    happen before any profile has been created for it to take effect, which
    in practice means as early as the plugin can manage.
    """
    if not available():
        return False
    existing = QWebEngineUrlScheme.schemeByName(QByteArray(name.encode()))
    if bytes(existing.name()):
        return True
    scheme = QWebEngineUrlScheme(QByteArray(name.encode()))
    # Qt6 scopes these enums; Qt5 hangs them off the class.
    flags = getattr(QWebEngineUrlScheme, "Flag", QWebEngineUrlScheme)
    # SecureScheme so the page counts as a secure context - ES modules and
    # several Web APIs are refused otherwise. LocalAccessAllowed so it may
    # fetch its own sibling files. CorsEnabled so module imports resolve.
    scheme.setFlags(flags.SecureScheme | flags.LocalAccessAllowed
                    | flags.CorsEnabled)
    syntax = getattr(QWebEngineUrlScheme, "Syntax", QWebEngineUrlScheme)
    try:
        scheme.setSyntax(syntax.Host)
    except Exception:  # pragma: no cover - older Qt without Syntax.Host
        pass
    QWebEngineUrlScheme.registerScheme(scheme)
    return True


def resolve(root, path):
    """The file `path` names inside `root`, or None if it escapes.

    Separate from the handler so the containment rule is testable without Qt:
    this is the only place deciding what the page is allowed to read.
    """
    cleaned = (path or "/").split("?")[0].split("#")[0]
    if cleaned.endswith("/") or not cleaned.strip("/"):
        cleaned = cleaned + INDEX
    relative = cleaned.lstrip("/")
    root_full = os.path.abspath(root)
    target = os.path.abspath(os.path.join(root_full, relative))
    # commonpath rather than startswith: "/rootevil" starts with "/root".
    try:
        if os.path.commonpath([root_full, target]) != root_full:
            return None
    except ValueError:                      # different drives on Windows
        return None
    return target


def mime_for(path):
    return MIME.get(os.path.splitext(path)[1].lower(), b"application/octet-stream")


class _Handler(QWebEngineUrlSchemeHandler):
    """Serves files under `root`. The root is mutable; the handler is not."""

    def __init__(self, root):
        super().__init__()
        self.root = root
        self.misses = []

    def requestStarted(self, job):
        target = resolve(self.root, job.requestUrl().path())
        if target is None or not os.path.isfile(target):
            self.misses.append(job.requestUrl().path())
            fail = getattr(job, "Error", job)
            job.fail(fail.UrlNotFound)
            return
        with open(target, "rb") as handle:
            body = handle.read()
        # Parented to the job: a buffer owned by this frame is collected as
        # soon as it returns, and the page receives nothing at all.
        buffer = QBuffer(job)
        buffer.setData(QByteArray(body))
        mode = getattr(QBuffer, "OpenModeFlag", QBuffer)
        buffer.open(mode.ReadOnly)
        job.reply(QByteArray(mime_for(target)), buffer)


def install(root, name=SCHEME):
    """Serve `root` over `name://`. Returns the handler.

    Repointing an existing handler rather than installing a second one:
    Qt keeps the first handler and ignores the newcomer, so a plugin reload
    that installed a fresh one would serve stale files for the rest of the
    session with no error anywhere.
    """
    if not available():
        raise WebAssetsError("this QGIS build has no QtWebEngine scheme "
                             "classes; the web view cannot be served")
    # QWebEngineProfile.defaultProfile() ABORTS THE PROCESS when no
    # QApplication exists - not an exception, a hard exit with no traceback,
    # which is how this first showed up as an empty test run. Checked here so
    # a headless caller gets a message instead of taking QGIS down with it.
    from qgis.PyQt.QtWidgets import QApplication
    if QApplication.instance() is None:
        raise WebAssetsError(
            "a QApplication must exist before the scheme handler is "
            "installed; QWebEngineProfile.defaultProfile() aborts the "
            "process without one")
    if not ensure_scheme(name):
        raise WebAssetsError("could not register the %s:// scheme" % name)
    root = os.path.abspath(root)
    existing = _HANDLERS.get(name)
    if existing is not None:
        existing.root = root
        return existing
    handler = _Handler(root)
    _HANDLERS[name] = handler
    QWebEngineProfile.defaultProfile().installUrlSchemeHandler(
        QByteArray(name.encode()), handler)
    return handler


def url(path=INDEX, name=SCHEME):
    """The URL the view should load. Host form: equis://app/index.html."""
    return QUrl("%s://app/%s" % (name, path.lstrip("/")))
