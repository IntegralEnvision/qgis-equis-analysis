r"""Can a plugin page get a GPU in QGIS's QtWebEngine? One configuration per run.

Not a console probe: QGIS runs this at startup (`--code`) in a THROWAWAY
profile and it quits QGIS hard when done. Launch it with run_gpu.ps1, once
per configuration: Chromium reads QTWEBENGINE_CHROMIUM_FLAGS once per
process, so every configuration needs its own QGIS. Results are recorded in
docs/webengine-support.md, "The GPU can be turned on".

The proof of a GPU is a drawn pixel read back (WebGL), a compute shader's
output (WebGPU) and a SCREENSHOT of the view, not a context object or
chrome://gpu's word. With GPU compositing, WebGL works, chrome://gpu says
"Hardware accelerated" and the view shows blank white (2026-10-29): only the
screenshot catches that.

Environment (the working directory must be the repository root: --code runs
this with no __file__):

  PROBE_OUT    where gpu_<tag>.json and shot_<tag>_*.png go
  PROBE_TAG    names this configuration's files
  PROBE_FPS    set: also measure displayed frame intervals (about 10 s more)
  PROBE_WRITE_SETTING   set: write these flags to the profile's custom
               environment variables (Options > System > Environment) and
               quit before WebEngine starts. The NEXT launch of that profile
               then tests whether QGIS applies them early enough.
"""
import json
import os
import time
import traceback

from qgis.PyQt.QtCore import (QBuffer, QByteArray, QCoreApplication, QPoint,
                              Qt, QTimer, QUrl, qInstallMessageHandler)

SOURCE = os.path.join(os.getcwd(), "tools", "webengine_probes")
OUT_DIR = os.environ["PROBE_OUT"]
TAG = os.environ.get("PROBE_TAG", "untagged")
OUT = {"tag": TAG, "env": {k: v for k, v in os.environ.items()
                           if k.startswith(("QTWEBENGINE", "QSG", "QT_OPENGL", "QT_QUICK"))},
       "qt_messages": [], "qt": {}, "events": []}
KEEP = {}
T0 = time.monotonic()

with open(os.path.join(SOURCE, "gpu_page.html"), encoding="utf-8") as _page:
    PAGE = _page.read()
os.makedirs(OUT_DIR, exist_ok=True)


def on_message(mode, context, message):
    # Qt WebEngine's own verdicts arrive here: "--use-gl=angle is set with
    # unsupported SceneGraph Backend", "D3D: No D3D device."
    OUT["qt_messages"].append(str(message)[:500])


def event(*parts):
    OUT["events"].append([round(time.monotonic() - T0, 1)] + [str(p) for p in parts])


def finish():
    with open(os.path.join(OUT_DIR, "gpu_%s.json" % TAG), "w") as f:
        json.dump(OUT, f, indent=1)
    QTimer.singleShot(300, lambda: os._exit(0))


def write_setting(flags):
    from qgis.core import QgsSettings
    s = QgsSettings()
    s.setValue("qgis/customEnvVarsUse", True)
    s.setValue("qgis/customEnvVars", ["overwrite|QTWEBENGINE_CHROMIUM_FLAGS=" + flags])
    s.sync()
    OUT["wrote_setting"] = {"file": s.fileName(), "value": s.value("qgis/customEnvVars")}
    finish()


def qt_state():
    """Why Qt WebEngine decides what it does: the scene graph it sees."""
    try:
        from qgis.PyQt.QtQuick import QQuickWindow
        OUT["qt"]["quick_graphicsApi"] = str(QQuickWindow.graphicsApi())
        OUT["qt"]["quick_sceneGraphBackend"] = QQuickWindow.sceneGraphBackend()
    except Exception as exc:  # noqa: BLE001
        OUT["qt"]["quick"] = repr(exc)
    OUT["qt"]["AA_ShareOpenGLContexts"] = QCoreApplication.testAttribute(
        Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    try:
        from qgis.PyQt.QtGui import QOpenGLContext
        OUT["qt"]["globalShareContext"] = QOpenGLContext.globalShareContext() is not None
    except Exception as exc:  # noqa: BLE001
        OUT["qt"]["globalShareContext"] = repr(exc)


def start():
    try:
        if os.environ.get("PROBE_WRITE_SETTING"):
            return write_setting(os.environ["PROBE_WRITE_SETTING"])
        qInstallMessageHandler(on_message)      # before WebEngine initialises
        from qgis.PyQt.QtWebEngineCore import (QWebEngineProfile, QWebEngineUrlScheme,
                                               QWebEngineUrlSchemeHandler)
        from qgis.PyQt.QtWebEngineWidgets import QWebEngineView
        qt_state()

        class Handler(QWebEngineUrlSchemeHandler):
            def requestStarted(self, job):
                buf = QBuffer(job)
                buf.setData(QByteArray(PAGE.encode()))
                buf.open(QBuffer.OpenModeFlag.ReadOnly)
                job.reply(QByteArray(b"text/html"), buf)

        # A secure context, or WebGPU is not offered at all.
        F = QWebEngineUrlScheme.Flag
        s = QWebEngineUrlScheme(QByteArray(b"gpuprobe"))
        s.setFlags(F.SecureScheme | F.LocalAccessAllowed | F.CorsEnabled | F.FetchApiAllowed)
        s.setSyntax(QWebEngineUrlScheme.Syntax.Host)
        QWebEngineUrlScheme.registerScheme(s)
        KEEP["handler"] = Handler()
        QWebEngineProfile.defaultProfile().installUrlSchemeHandler(QByteArray(b"gpuprobe"), KEEP["handler"])
        view = QWebEngineView()
        view.resize(800, 600)
        # Above every other window: the screen grab records whatever is on
        # top at the view's coordinates, and on 2026-10-29 that was another
        # application - a check of the wrong pixels, and a picture of the
        # user's other work saved to disk.
        view.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        view.show()          # on screen: GPU features are not judged off-screen
        KEEP["view"] = view
        view.loadFinished.connect(lambda ok: event("loadFinished", ok, view.url().toString()))
        view.page().renderProcessTerminated.connect(
            lambda status, code: event("renderProcessTerminated", status, code))
        view.load(QUrl("gpuprobe://app/index.html" + ("?fps" if os.environ.get("PROBE_FPS") else "")))
        state = {"t0": time.monotonic(), "done": False}

        def got(res):
            if not state["done"] and res and res != "null":
                state["done"] = True
                OUT["page"] = json.loads(res)
                QTimer.singleShot(1500, screenshot)

        # Polled on a timer: a dead renderer never calls back.
        def tick():
            if state["done"]:
                return
            if time.monotonic() - state["t0"] > 60:
                state["done"] = True
                OUT["page"] = {"fatal": "no report in 60 s"}
                return gpu_page()
            view.page().runJavaScript("JSON.stringify(window.__probe || null)", 0, got)
            QTimer.singleShot(500, tick)
        QTimer.singleShot(1000, tick)
    except Exception:  # noqa: BLE001
        OUT["fatal"] = traceback.format_exc()
        finish()


def screenshot():
    """From the screen and from QWidget.grab: what was actually composited."""
    try:
        from qgis.PyQt.QtWidgets import QApplication
        view = KEEP["view"]
        QApplication.processEvents()
        tl = view.mapToGlobal(QPoint(0, 0))
        shots = {"screen": view.screen().grabWindow(0, tl.x(), tl.y(), view.width(), view.height()),
                 "widget": view.grab()}
        OUT["pixels"] = {}
        for name, pm in shots.items():
            pm.save(os.path.join(OUT_DIR, "shot_%s_%s.png" % (TAG, name)))
            img = pm.toImage()
            fx, fy = img.width() / view.width(), img.height() / view.height()
            # gpu_page.html: a 400x300 canvas (magenta if WebGL drew it, cyan
            # if only 2D could) and a yellow HTML box at 420,20.
            OUT["pixels"][name] = {
                where: img.pixelColor(int(x * fx), int(y * fy)).name()
                for where, (x, y) in {"canvas": (200, 150), "html_div": (570, 150),
                                      "page_bg": (600, 450)}.items()}
    except Exception:  # noqa: BLE001
        OUT["screenshot_error"] = traceback.format_exc()
    gpu_page()


def gpu_page():
    """chrome://gpu: the flags Chromium was started with, and its feature table."""
    view = KEEP["view"]
    view.load(QUrl("chrome://gpu"))
    state = {"n": 0}

    def got(text):
        if text and "Graphics Feature Status" in text and "chrome_gpu" not in OUT:
            OUT["chrome_gpu"] = text
            finish()

    def tick():
        if "chrome_gpu" in OUT:
            return
        state["n"] += 1
        if state["n"] > 30:
            OUT["chrome_gpu_error"] = "no chrome://gpu text in 30 s"
            return finish()
        view.page().runJavaScript(
            "(function(){var b=document.querySelector('info-view');"
            "var r=b&&b.shadowRoot?b.shadowRoot.textContent:document.body.innerText;"
            "return (r||'');})()", 0, got)
        QTimer.singleShot(1000, tick)
    QTimer.singleShot(3000, tick)


QTimer.singleShot(3 * 60 * 1000, finish)
QTimer.singleShot(3000, start)
