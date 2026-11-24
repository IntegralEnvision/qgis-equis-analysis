r"""What a project maps belongs to the project; credentials to the person.

The panel used to show the last parquet URL, site and key used anywhere in
the profile, so project B opened showing project A's source. Now the
project file records the source, the EQuIS site and facility, and the
parquet ADDRESS; the API key (by site) and the token (by address) live in
the person's settings (equis_loader/parquet_address.py says why). This
checks the half that needs QGIS: what reaches the project file (never a key
or a token), that a project fills the panel when it opens and goes when it
closes, that connecting chooses the project's facility only on its own
site and never over the person's choice, that re-saving the same values
does not mark the project modified, that a token's expiry is shown, and
that the old saved URL and key migrate.

Needs QGIS:

    "C:\Program Files\QGIS 4.2.3\bin\python-qgis.bat" tools\test_project_source.py

Its settings go to a temporary folder, never the real profile: the test
refuses to run if QSettings still points anywhere else.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.chdir(ROOT)

FAILURES = []


def _slot_raised(kind, value, tb):
    """PyQt6 aborts the process on an exception in a slot (0xC0000409 on
    Windows, with the output lost) unless an excepthook is set. Set, it is
    a failure that says where."""
    import traceback
    traceback.print_exception(kind, value, tb)
    FAILURES.append("an exception in a slot: %s" % value)


sys.excepthook = _slot_raised


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


try:
    from qgis.core import QgsApplication, QgsProject
except ImportError as exc:
    print("needs QGIS: %s" % exc)
    print("SKIPPED")
    raise SystemExit(0)

from qgis.PyQt.QtCore import QSettings  # noqa: E402

QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", ""), True)
app = QgsApplication([], False)
app.initQgis()

# After initQgis, which sets its own settings path; set before, this would
# be overwritten and the test would write to the real profile.
SANDBOX = tempfile.mkdtemp(prefix="equis_project_source_")
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, SANDBOX)
where = os.path.normcase(os.path.abspath(QSettings().fileName()))
if not where.startswith(os.path.normcase(os.path.abspath(SANDBOX))):
    print("QSettings writes to %s, not the sandbox; refusing to run" % where)
    raise SystemExit(1)

from test_offline import FakeIface            # noqa: E402
from equis_loader import settings as settings_store  # noqa: E402
from equis_loader.dialog import EquisDialog   # noqa: E402

ADDRESS = "https://acct.blob.core.windows.net/container/facility_a"
TOKEN = "sp=r&se=2027-11-01T00:00:00Z&sv=2022-11-02&sr=c&sig=abc%3D"
SIGNED = ADDRESS + "?" + TOKEN
PARQUET = settings_store.SOURCE_PARQUET
LIVE = settings_store.SOURCE_LIVE
project = QgsProject.instance()
qgs = os.path.join(SANDBOX, "facility_a.qgs")

try:
    # --- the old saved URL becomes a token, and stops filling the field
    QSettings().setValue("equis_loader/parquet_url", SIGNED)
    cfg = settings_store.load()
    check("the old saved URL is removed",
          not QSettings().contains("equis_loader/parquet_url"))
    check("its token is kept under its address",
          settings_store.load_parquet_tokens() == {ADDRESS: TOKEN},
          repr(settings_store.load_parquet_tokens()))
    check("and it is no longer the field's default",
          cfg["parquet_url"] != SIGNED, cfg["parquet_url"])
    check("a book that is not JSON reads as empty, not as a crash",
          (QSettings().setValue("equis_loader/parquet_tokens", "{oops"),
           settings_store.load_parquet_tokens())[1] == {})
    settings_store.remember_parquet_token(SIGNED)

    # --- what the project file holds
    project.clear()
    check("a new project records nothing",
          settings_store.load_project_source() == (None, None))
    settings_store.save_project_source(PARQUET, SIGNED)
    check("the project records the source and the address",
          settings_store.load_project_source() == (PARQUET, ADDRESS),
          repr(settings_store.load_project_source()))
    check("writing it marks the project modified", project.isDirty())
    check("the project saves", project.write(qgs))
    with open(qgs, "r", encoding="utf-8") as handle:
        text = handle.read()
    check("the saved file names the address", ADDRESS in text)
    check("the saved file holds no token",
          "sig=" not in text and "abc%3D" not in text and TOKEN not in text)

    project.clear()
    check("closing the project forgets it",
          settings_store.load_project_source() == (None, None))
    check("the project reads back", project.read(qgs))
    check("reopened, it records the same source and address",
          settings_store.load_project_source() == (PARQUET, ADDRESS))
    check("a project just read is not modified", not project.isDirty())
    settings_store.save_project_source(PARQUET, SIGNED)
    check("re-saving the same source leaves it unmodified",
          not project.isDirty())
    settings_store.save_project_source(PARQUET, ADDRESS + "/")
    check("so does the same address with a trailing slash",
          not project.isDirty())
    settings_store.save_project_source(LIVE)
    check("a different source marks it modified", project.isDirty())
    check("and keeps the address for switching back",
          settings_store.load_project_source() == (LIVE, ADDRESS))

    # --- the panel follows the project
    project.clear()
    panel = EquisDialog(FakeIface())
    blank = panel.parquet_edit.text()
    check("with no project source the field is not the old URL",
          blank != SIGNED and ADDRESS not in blank, blank)

    project.read(qgs)          # still the parquet version on disk
    check("opening a project sets its source",
          panel._data_source() == PARQUET)
    check("and fills the field with its address and this person's token",
          panel.parquet_edit.text() == SIGNED, panel.parquet_edit.text())
    check("the completer offers the address, never the token",
          panel._parquet_completer.model().stringList() == [ADDRESS])

    project.clear()
    check("closing it empties the field again",
          panel.parquet_edit.text() == blank, panel.parquet_edit.text())

    # Someone else's computer: the project, and no token for it.
    QSettings().remove("equis_loader/parquet_tokens")
    project.read(qgs)
    check("without a token the field shows the bare address",
          panel.parquet_edit.text() == ADDRESS, panel.parquet_edit.text())
    check("and the status says a token is needed",
          "no token" in panel.status_label.text(), panel.status_label.text())
    check("which is a warning, not a refusal",
          getattr(panel, "_status_level", "") == "warning")

    # The auto-connect on a parquet project with nothing to open says
    # nothing, rather than "enter the URL" the moment the panel appears.
    project.clear()
    panel.source_combo.setCurrentIndex(panel.source_combo.findData(PARQUET))
    panel.parquet_edit.setText("")
    panel._set_status("")
    panel._auto_connect()
    check("the auto-connect leaves an empty parquet field alone",
          panel.status_label.text() == "", panel.status_label.text())

    # --- a signed URL's expiry, beside the field
    expired = ADDRESS + "?sp=r&se=2020-01-01T00:00:00Z&sig=old"
    settings_store.remember_parquet_token(expired)
    project.read(qgs)
    check("an expired token is said beside the field",
          panel.parquet_hint.text().startswith("This token expired on 2020-01-01"),
          panel.parquet_hint.text())
    check("in red", "#a01010" in panel.parquet_hint.styleSheet())
    panel._on_parquet_failed("HTTP Error 403: Server failed to authenticate")
    check("and named as the likely cause when the open fails",
          "token expired on 2020-01-01" in panel.status_label.text(),
          panel.status_label.text())
    # A failure's own words name the URL, signature and all (DuckDB's do:
    # tools/test_parquet_source.py). The status line - and the overlay,
    # which reads it - shows the address, never the token.
    panel._on_parquet_failed(
        "HTTP GET error on '%s/matrix=WS/fraction=N/part-0000.parquet?%s'"
        % (ADDRESS, expired.split("?", 1)[1]))
    check("a failure naming the signed URL shows no token",
          "sig=" not in panel.status_label.text()
          and ADDRESS + "/matrix=WS/fraction=N/part-0000.parquet?<token>"
          in panel.status_label.text(), panel.status_label.text())
    settings_store.remember_parquet_token(SIGNED)
    project.read(qgs)
    check("a live token says until when",
          panel.parquet_hint.text() == "Token valid until 2027-11-01 00:00 UTC.",
          panel.parquet_hint.text())
    panel.parquet_edit.setText(ADDRESS)
    check("a bare address shows the expiry of the token Open would use",
          panel.parquet_hint.text().startswith("Token valid until 2027"),
          panel.parquet_hint.text())
    panel.parquet_edit.setText("data/facility_a")
    check("a local folder has no expiry to show", panel.parquet_hint.text() == "")

    # --- the live path: the site and facility in the project, the key not
    SITE = "https://tenant.equisonline.com"
    OTHER = "https://other.equisonline.com"
    QSettings().setValue("equis_loader/site", SITE)
    QSettings().setValue("equis_loader/api_key", "KEY-1")
    settings_store.load()
    check("the old single key is filed under the site saved with it",
          settings_store.api_key_for(SITE) == "KEY-1"
          and settings_store.api_key_for(SITE + "/") == "KEY-1")
    check("and its old setting removed",
          not QSettings().contains("equis_loader/api_key"))
    settings_store.save(OTHER, "KEY-2")
    check("a key for another site is kept apart",
          settings_store.api_key_for(OTHER) == "KEY-2"
          and settings_store.api_key_for(SITE) == "KEY-1")
    settings_store.save(OTHER, "")
    check("an empty key forgets that site's", settings_store.api_key_for(OTHER) == "")
    settings_store.save(SITE, "KEY-1")

    qgs_live = os.path.join(SANDBOX, "facility_live.qgs")
    project.clear()
    settings_store.save_project_live(SITE + "/", {"id": 42, "name": "Test Site"})
    check("the project records the site, the facility and live",
          settings_store.load_project_live() == (SITE, "42", "Test Site")
          and settings_store.load_project_source()[0] == LIVE,
          repr(settings_store.load_project_live()))
    project.write(qgs_live)
    with open(qgs_live, "r", encoding="utf-8") as handle:
        text = handle.read()
    check("the saved file names the site", SITE in text)
    check("the saved file holds no API key", "KEY-1" not in text)

    project.clear()
    project.read(qgs_live)
    check("opening it sets live", panel._data_source() == LIVE)
    check("and the site", panel.site_edit.text() == SITE, panel.site_edit.text())
    check("and this person's key for that site",
          panel.key_edit.text() == "KEY-1", panel.key_edit.text())

    class FacilitiesDone:
        """What _on_facilities_loaded reads from a finished task."""
        source = None
        def __init__(self, facilities):
            self._result = facilities

    LIST = [{"id": 7, "code": "B", "name": "Another", "wkid": 0},
            {"id": 42, "code": "TS", "name": "Test Site", "wkid": 0}]
    launched = []
    held_start, held_client = panel._start, panel._client
    panel._start = lambda task, replace=False: launched.append(task) or True
    class StubClient:
        """Connected, as far as the panel asks; nothing is ever sent."""
        username = "tester"
        read_only = False

    panel._client = StubClient()
    try:
        project.setDirty(False)
        panel._on_facilities_loaded(FacilitiesDone(LIST))
        chosen = panel._current_facility() or {}
        check("connecting chooses the project's facility",
              chosen.get("id") == 42, repr(chosen))
        check("which loads it, as a click would", len(launched) == 1,
              repr(launched))
        check("choosing the project's own facility leaves it unmodified",
              not project.isDirty())

        panel.facility_combo.setCurrentIndex(
            panel.facility_combo.findText("Another  (B)"))
        check("choosing another facility records it",
              settings_store.load_project_live()[1:] == ("7", "Another")
              and project.isDirty(), repr(settings_store.load_project_live()))
        panel._on_facilities_loaded(FacilitiesDone(LIST))
        check("a reconnect keeps the person's choice",
              (panel._current_facility() or {}).get("id") == 7)

        project.read(qgs_live)            # back to 42, on SITE
        panel.site_edit.setText(OTHER)
        panel.facility_combo.setCurrentIndex(0)
        launched.clear()
        panel._on_facilities_loaded(FacilitiesDone(LIST))
        check("on another site the project's facility id is not chosen",
              panel._current_facility() is None and not launched)

        panel.site_edit.setText(SITE)
        panel.facility_combo.setCurrentIndex(0)
        panel._on_facilities_loaded(FacilitiesDone(LIST[:1]))
        check("a facility the list lacks is named, not silently skipped",
              "This project maps Test Site" in panel.status_label.text(),
              panel.status_label.text())
    finally:
        panel._start, panel._client = held_start, held_client

    QSettings().remove("equis_loader/api_keys")
    project.read(qgs_live)
    check("without a key for the project's site the field is empty",
          panel.key_edit.text() == "", panel.key_edit.text())
    check("and the status says so",
          "no API key for it on this computer" in panel.status_label.text(),
          panel.status_label.text())

    panel.shutdown()
    panel.parquet_edit.setText("left alone")
    project.read(qgs)          # must not reach the shut-down panel
    check("after shutdown the panel no longer follows the project",
          panel.parquet_edit.text() == "left alone", panel.parquet_edit.text())
finally:
    project.clear()
    shutil.rmtree(SANDBOX, ignore_errors=True)

print("\n%d failure(s)" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
