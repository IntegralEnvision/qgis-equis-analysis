r"""A parquet source belongs to the project; its token to the person.

The field used to show the last URL opened anywhere in the profile, so
project B opened showing project A's source. Now the project file records
the source and the parquet ADDRESS, and the token lives in the person's
settings, looked up by address (equis_loader/parquet_address.py). This
checks the half that needs QGIS: what reaches the project file (never a
token), that a project's source fills the panel when it opens and goes when
it closes, that re-saving the same source does not mark the project
modified, and that the old saved URL migrates into the token book.

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
