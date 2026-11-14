# Spike, QGIS half: the same page in QGIS's QtWebEngine, through the plugin's
# real WebBridge and QWebChannel, answering from ChemDuck in its own schema of
# the plugin's ReportStore. Run by QGIS at startup in a throwaway profile:
#
#   $env:SPIKE_MANIFEST = "<facility>\manifest.json"
#   $env:SPIKE_CHEMDUCK = "<checkout>\chemduck"
#   $env:SPIKE_WORK     = "<work>"        # holds page\ (build.mjs) and pub\
#   $env:SPIKE_REPO     = "<checkout>\equis_qgis_public"
#   qgis.bat --profiles-path <root> --profile try --code tools\spike_chemduck\qgis_run.py
#
# Writes <work>\qgis_result.json and quits QGIS.
import json
import os
import sys
import time
import traceback

from qgis.PyQt.QtCore import QTimer

REPO = os.environ["SPIKE_REPO"]
WORK = os.environ["SPIKE_WORK"]
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools", "spike_chemduck"))
OUT = os.path.join(WORK, "qgis_result.json")
STATE = {"began": time.time()}


def finish(result):
    with open(OUT, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=1, default=str)
    QTimer.singleShot(300, lambda: os._exit(0))


def start():
    try:
        from equis_loader.store import ReportStore
        from equis_loader.web_view import LinkedViews
        import host
        manifest = os.environ["SPIKE_MANIFEST"]
        store = ReportStore.from_manifest(os.path.dirname(os.path.abspath(manifest)))
        cursor, timings = host.open_chemduck(store, manifest, os.environ["SPIKE_CHEMDUCK"], WORK)
        STATE.update(store=store, cursor=cursor, timings=timings)
        # The plugin's own web view class, pointed at the spike's bundle and
        # at the ChemDuck cursor: the real QWebChannel, the real WebBridge.
        STATE["views"] = LinkedViews(lambda: cursor, bundle=os.path.join(WORK, "page"),
                                     on_ready=lambda: QTimer.singleShot(200, read))
        QTimer.singleShot(120000, lambda: finish({"error": "the page never called ready()"}))
    except Exception:  # noqa: BLE001
        finish({"error": traceback.format_exc()})


def read():
    def got(text):
        try:
            import host
            spike = json.loads(text) if text else {}
            result = host.check(STATE["store"], STATE["cursor"], spike)
            result["chemduck_open"] = STATE["timings"]
            result["seconds_to_answer"] = round(time.time() - STATE["began"], 1)
            result["where"] = "QGIS QtWebEngine, real QWebChannel"
            finish(result)
        except Exception:  # noqa: BLE001
            finish({"error": traceback.format_exc()})
    STATE["views"].view.page().runJavaScript("JSON.stringify(window.__spike || null)", got)


QTimer.singleShot(2000, start)
