"""Offline tests for run_nightly.py: the order of the night, and what stops it.

    python pipeline/test_run_nightly.py

No network beyond localhost and no EQuIS: each pipeline script is replaced
by a stand-in, and the upload goes to a fake blob store on 127.0.0.1.
"""

import base64
import hashlib
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_nightly as rn  # noqa: E402
from slice_store import MANIFEST_NAME, utc_now  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + str(detail)) if detail else ""))
    if not condition:
        FAILURES.append(label)


def make_root(config=None, slices=None):
    root = tempfile.mkdtemp(prefix="nightly_")
    if config is not None:
        with open(os.path.join(root, rn.CONFIG_NAME), "w") as handle:
            json.dump(config, handle)
    with open(os.path.join(root, MANIFEST_NAME), "w") as handle:
        json.dump({"version": 2, "runs": [], "location_rows": {},
                   "slices": slices or {
                       "S_WS_D_00": {"name": "S_WS_D_00", "report_id": 11,
                                     "matrix": "WS", "fraction": "D",
                                     "history": []}}}, handle)
    return root


class Fake(object):
    """Stands in for run_script. `codes` and `effects` are per script."""

    def __init__(self, root, codes=None, effects=None, lines=None):
        self.root, self.calls = root, []
        self.codes, self.effects, self.lines = codes or {}, effects or {}, lines or {}

    def __call__(self, log, script, args):
        self.calls.append((script, list(args)))
        effect = self.effects.get(script)
        if effect:
            effect(self.root)
        if script == "publish_manifest.py" and not self.codes.get(script):
            write_published(self.root)
        return self.codes.get(script, 0), self.lines.get(script, [])

    def scripts(self):
        return [script for script, _ in self.calls]


def write_published(root):
    part = os.path.join(root, "matrix=WS", "fraction=D")
    os.makedirs(part, exist_ok=True)
    with open(os.path.join(part, "part-0000.parquet"), "wb") as handle:
        handle.write(b"PAR1 fake partition")
    with open(os.path.join(root, "manifest.json"), "w") as handle:
        json.dump({"partitions": [{"path": "matrix=WS/fraction=D/part-0000.parquet",
                                   "matrix": "WS", "fraction": "D"}]}, handle)


def mark_run(status, rows, rebuilt=True):
    """Stands in for a check that collected; `rebuilt` dates the partition
    as nightly_slices.repack does when a cell was complete."""
    def effect(root):
        path = os.path.join(root, MANIFEST_NAME)
        with open(path) as handle:
            data = json.load(handle)
        for entry in data["slices"].values():
            entry.update(last_run=utc_now(), last_status=status, last_rows=rows)
        if rebuilt and status == "ok":
            data.setdefault("partitions", {})["WS/D"] = {"as_of": utc_now(),
                                                         "rows": rows}
        with open(path, "w") as handle:
            json.dump(data, handle)
    return effect


def night(root, fake, **kw):
    real = rn.run_script
    rn.run_script = fake
    log = rn.Log(root)
    try:
        return rn.night(root, rn.load_config(root), log, **kw)
    finally:
        log.close()
        rn.run_script = real


BASE_CONFIG = {"facility": 7, "sample_types": "N"}


# ------------------------------------------------------------ refusals
def test_config():
    root = make_root(None)
    try:
        rn.load_config(root)
        refused = False
    except rn.Refused:
        refused = True
    check("config: no pipeline.json is refused", refused)

    root = make_root({"facility": 7})
    try:
        rn.load_config(root)
        refused = ""
    except rn.Refused as exc:
        refused = str(exc)
    check("config: sample_types must be stated, even as null",
          "sample_types" in refused, refused[:60])
    check("config: null sample_types is a statement, and accepted",
          rn.load_config(make_root({"facility": 7, "sample_types": None}))
          ["sample_types"] is None)

    root = make_root(dict(BASE_CONFIG, upload={
        "base": "https://a.blob.core.windows.net/c/x?sv=1&sig=SECRET",
        "sas_env": "X"}))
    try:
        rn.load_config(root)
        refused = ""
    except rn.Refused as exc:
        refused = str(exc)
    check("config: a SAS token in the tracked config is refused",
          "SAS" in refused and "SECRET" not in refused, refused[:60])


# ---------------------------------------------------------- the night
def test_order():
    root = make_root(BASE_CONFIG)
    fake = Fake(root, effects={"nightly_slices.py": mark_run("ok", 10)})
    steps, notes, code = night(root, fake)
    check("night: collect, then publish, nothing else when nothing waits",
          fake.scripts() == ["nightly_slices.py", "publish_manifest.py"],
          fake.scripts())
    check("night: a clean night exits 0", code == 0, (code, notes))
    check("night: steps that had nothing to do say skipped, not failed",
          steps["pending"] == rn.SKIPPED and steps["upload"] == rn.SKIPPED,
          steps)


def test_nothing_rebuilt():
    root = make_root(BASE_CONFIG)
    fake = Fake(root, lines={"nightly_slices.py": ["no new data", "",
                                                   "   WS/D     unchanged"]})
    steps, notes, code = night(root, fake)
    check("idle: a check that rebuilt nothing publishes and uploads nothing",
          fake.scripts() == ["nightly_slices.py"], fake.scripts())
    check("idle: and exits 0 - nothing to do is not a fault",
          code == 0, (code, notes))
    check("idle: publish says skipped, not failed or not run",
          steps["publish"] == rn.SKIPPED, steps)

    fake = Fake(root, lines={"nightly_slices.py": [
        "   WS/D     waiting   1 slice(s) not yet run; OVERDUE (armed over "
        "6 h ago): S_WS_D_00"]})
    steps, notes, code = night(root, fake)
    check("idle: an overdue slice is reported and exits 1",
          code == 1 and any("OVERDUE" in n for n in notes), (code, notes))

    fake = Fake(root)
    night(root, fake, trigger=True)
    check("trigger: --trigger reaches the check",
          "--trigger" in fake.calls[0][1], fake.calls)
    fake = Fake(root)
    night(root, fake)
    check("trigger: and is not passed otherwise",
          "--trigger" not in fake.calls[0][1], fake.calls)


def test_nightly_failure_stops():
    root = make_root(BASE_CONFIG)
    fake = Fake(root, codes={"nightly_slices.py": 2})
    steps, notes, code = night(root, fake)
    check("stop: a failed collect publishes nothing",
          fake.scripts() == ["nightly_slices.py"], fake.scripts())
    check("stop: and exits 2", code == 2, code)
    check("stop: the steps after it are 'not run', not 'failed'",
          steps["publish"] == rn.NOT_RUN and steps["upload"] == rn.NOT_RUN,
          steps)


def test_empty_slice_degrades():
    root = make_root(BASE_CONFIG)
    fake = Fake(root, effects={"nightly_slices.py": mark_run("no output", None)},
                lines={"nightly_slices.py": [
                    "   WS/D  KEPT as of 2026-10-16 - 1 slice(s) produced "
                    "nothing tonight: S_WS_D_00"]})
    steps, notes, code = night(root, fake)
    check("degraded: a slice that stored nothing exits 1; its cell was held, "
          "so there is nothing new to publish",
          code == 1 and "publish_manifest.py" not in fake.scripts(),
          (code, notes))
    check("degraded: the slice is named, with its status",
          any("S_WS_D_00" in n and "no output" in n for n in notes), notes)
    check("degraded: the KEPT cell is reported",
          any("KEPT" in n for n in notes), notes)


def test_pending():
    slices = {"S_WS_D_00": {"name": "S_WS_D_00", "report_id": 11, "history": []},
              "S_WS_D_01": {"name": "S_WS_D_01", "needs_report": True,
                            "history": []}}
    root = make_root(BASE_CONFIG, slices)
    fake = Fake(root, effects={"nightly_slices.py": mark_run("ok", 5)})
    night(root, fake)
    pending = [args for script, args in fake.calls
               if script == "create_slice_reports.py"]
    check("pending: a slice waiting for a report gets one",
          len(pending) == 1 and "--pending" in pending[0], fake.calls)
    check("pending: with the sample types the config states",
          pending and pending[0][pending[0].index("--sample-types") + 1] == "N",
          pending)

    root = make_root({"facility": 7, "sample_types": None}, slices)
    fake = Fake(root, effects={"nightly_slices.py": mark_run("ok", 5)})
    night(root, fake)
    pending = [args for script, args in fake.calls
               if script == "create_slice_reports.py"]
    check("pending: null sample types passes no filter",
          pending and "--sample-types" not in pending[0], pending)

    root = make_root(BASE_CONFIG, slices)
    fake = Fake(root, effects={"nightly_slices.py": mark_run("ok", 5)},
                codes={"create_slice_reports.py": 1})
    steps, notes, code = night(root, fake)
    check("pending: a failed provision is reported and the night goes on",
          steps["pending"] == rn.FAILED and steps["publish"] == rn.OK
          and code == 1, (steps, code))


def test_publish_failure_stops_upload():
    root = make_root(dict(BASE_CONFIG, upload={
        "base": "https://a.blob.core.windows.net/c/x", "sas_env": "NO_SUCH"}))
    fake = Fake(root, effects={"nightly_slices.py": mark_run("ok", 5)},
                codes={"publish_manifest.py": 1})
    steps, notes, code = night(root, fake)
    check("stop: a failed publish uploads nothing",
          steps["upload"] == rn.NOT_RUN and code == 2, (steps, code))


# --------------------------------------------------------------- upload
class Blobs(http.server.BaseHTTPRequestHandler):
    store, order, refuse = {}, [], False

    def log_message(self, *_a):
        pass

    def _key(self):
        return self.path.split("?", 1)[0]

    def do_HEAD(self):
        blob = Blobs.store.get(self._key())
        self.send_response(200 if blob else 404)
        if blob:
            self.send_header("Content-MD5", blob[1])
        self.end_headers()

    def do_PUT(self):
        data = self.rfile.read(int(self.headers["Content-Length"]))
        if Blobs.refuse or "sig=" not in self.path:
            self.send_response(403)
            self.end_headers()
            return
        md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
        if self.headers.get("Content-MD5") != md5:
            self.send_response(400)
            self.end_headers()
            return
        Blobs.store[self._key()] = (data, md5)
        Blobs.order.append(self._key())
        self.send_response(201)
        self.end_headers()


def test_upload():
    server = http.server.HTTPServer(("127.0.0.1", 0), Blobs)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real_blob_url = rn.blob_url
    # The fake store is plain http; everything else is the real code path.
    rn.blob_url = lambda base, rel, sas: real_blob_url(
        "http://127.0.0.1:%d/c/x" % server.server_port, rel, sas)
    os.environ["TEST_SAS"] = "sv=1&sig=SECRET"
    try:
        root = make_root(dict(BASE_CONFIG, upload={
            "base": "https://a.blob.core.windows.net/c/x", "sas_env": "TEST_SAS"}))
        fake = Fake(root, effects={"nightly_slices.py": mark_run("ok", 5)})
        steps, notes, code = night(root, fake)
        check("upload: a clean night uploads and exits 0",
              steps["upload"] == rn.OK and code == 0, (steps, notes))
        check("upload: the manifest goes up LAST",
              Blobs.order and Blobs.order[-1].endswith("/manifest.json"),
              Blobs.order)
        check("upload: the partition went up with its path",
              "/c/x/matrix%3DWS/fraction%3DD/part-0000.parquet" in Blobs.store
              or "/c/x/matrix=WS/fraction=D/part-0000.parquet" in Blobs.store,
              list(Blobs.store))

        del Blobs.order[:]
        steps, notes, code = night(root, Fake(
            root, effects={"nightly_slices.py": mark_run("ok", 5)}))
        check("upload: unchanged files are not sent again",
              steps["upload"] == rn.OK and not Blobs.order, Blobs.order)

        # Forgotten by the store, so the files must be sent - and refused.
        Blobs.refuse = True
        Blobs.store.clear()
        fake = Fake(root, effects={"nightly_slices.py": mark_run("ok", 5)})
        steps, notes, code = night(root, fake)
        check("upload: a refused upload exits 2",
              steps["upload"] == rn.FAILED and code == 2, (steps, code))
        check("upload: the SAS token never appears in what is reported",
              not any("SECRET" in n for n in notes), notes)
        log_text = open(sorted(
            os.path.join(root, rn.LOG_DIR, n)
            for n in os.listdir(os.path.join(root, rn.LOG_DIR)))[-1]).read()
        check("upload: nor in the log", "SECRET" not in log_text)
    finally:
        Blobs.refuse = False
        rn.blob_url = real_blob_url
        server.shutdown()
        del os.environ["TEST_SAS"]


def test_sas_url():
    os.environ["TEST_SAS_URL"] = ("https://acct.blob.core.windows.net/box"
                                  "?sv=1&sp=racwdl&sig=SECRET")
    try:
        config = {"upload": {"sas_url_env": "TEST_SAS_URL", "prefix": "f1/"}}
        base, sas = rn.upload_target(config)
        check("sas url: container and token come from the one variable",
              base == "https://acct.blob.core.windows.net/box/f1"
              and sas == "sv=1&sp=racwdl&sig=SECRET", (base, sas))
        check("sas url: the blob path goes before the token",
              rn.blob_url(base, "matrix=WS/fraction=D/part-0000.parquet", sas)
              .startswith("https://acct.blob.core.windows.net/box/f1/matrix%3DWS"))
        base, _ = rn.upload_target({"upload": {"sas_url_env": "TEST_SAS_URL"}})
        check("sas url: no prefix is the container root",
              base == "https://acct.blob.core.windows.net/box", base)
        root = make_root(dict(BASE_CONFIG, upload={
            "sas_url_env": "TEST_SAS_URL", "prefix": "x?sig=LEAK"}))
        try:
            rn.load_config(root)
            refused = ""
        except rn.Refused as exc:
            refused = str(exc)
        check("sas url: a token in the tracked config is refused",
              "token" in refused and "LEAK" not in refused, refused[:60])
    finally:
        del os.environ["TEST_SAS_URL"]
    env = os.path.join(make_root(BASE_CONFIG), ".env")
    with open(env, "w") as handle:
        handle.write("equis_site=https://t.equisonline.com\n"
                     "parquet_cache_rw: https://a.blob.core.windows.net/c?sv=1\n")
    values = rn.read_env_file(env)
    check(".env: `name=https://...` splits at the =, not the colon in https:",
          values.get("equis_site") == "https://t.equisonline.com", values)
    check(".env: `name: https://...?a=b` splits at the colon",
          values.get("parquet_cache_rw")
          == "https://a.blob.core.windows.net/c?sv=1", values)


# ----------------------------------------------------------------- lock
def test_lock():
    root = make_root(BASE_CONFIG)
    with rn.Lock(root):
        try:
            with rn.Lock(root):
                pass
            refused = False
        except rn.Refused:
            refused = True
    check("lock: a second night is refused while one runs", refused)
    check("lock: released afterwards",
          not os.path.exists(os.path.join(root, rn.LOCK_NAME)))
    path = os.path.join(root, rn.LOCK_NAME)
    with open(path, "w") as handle:
        handle.write("old")
    stale = 1000000000
    os.utime(path, (stale, stale))
    with rn.Lock(root):
        taken = True
    check("lock: a lock left by a crashed night is taken over", taken)


if __name__ == "__main__":
    try:
        test_config()
        test_order()
        test_nothing_rebuilt()
        test_nightly_failure_stops()
        test_empty_slice_degrades()
        test_pending()
        test_publish_failure_stops_upload()
        test_upload()
        test_sas_url()
        test_lock()
    finally:
        for name in os.listdir(tempfile.gettempdir()):
            if name.startswith("nightly_"):
                shutil.rmtree(os.path.join(tempfile.gettempdir(), name),
                              ignore_errors=True)
    print()
    if FAILURES:
        print("%d failure(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
        raise SystemExit(1)
    print("The night runs in order, stops where it must, and says so.")
