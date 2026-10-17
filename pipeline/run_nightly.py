r"""One facility's whole night, unattended: collect, provision, publish, upload.

    python pipeline/run_nightly.py --root data/<facility>
    python pipeline/run_nightly.py --root data/<facility> --no-upload

Run it from the directory whose `.env` holds the tenant's credentials, as
for every other script here. It reads `<root>/pipeline.json`:

    {
      "facility": 12345,
      "sample_types": "N",
      "template": null,
      "provision_pending": true,
      "arcgis": false,
      "upload": {"base": "https://<account>.blob.core.windows.net/<container>/<name>",
                 "sas_env": "PARQUET_SAS"}
    }

`sample_types` is REQUIRED, even as null (every type). New slices are
created with it, and the manifest does not record what the first ones were
created with: a guess here would quietly put QC samples into some slices of
a cell and not others. `upload` is optional, and never holds the SAS token
itself - pipeline.json is tracked; the token is read from the environment
variable `sas_env` names, or from that key (lower-cased) in the `.env`.

THE STEPS, AND WHAT A FAILURE STOPS
-----------------------------------
  1. nightly_slices.py             collect, re-bin, repack. A failure stops
                                   the night: the manifest may be mid-change.
  2. create_slice_reports.py --pending
                                   reports for slices step 1 added. Without
                                   this their cells stay KEPT (yesterday's)
                                   until someone notices. A failure is
                                   reported and the night goes on.
  3. arcgis_export.py              when configured. Reported, goes on.
  4. publish_manifest.py           a failure stops the upload.
  5. upload                        partitions, then arcgis/, then
                                   manifest.json LAST, so a reader never
                                   follows a manifest to a file not yet
                                   there. Unchanged files are skipped by MD5.

EXIT CODE: 0 everything ran and every slice produced output; 1 the night
completed but something needs a look (a slice stored nothing, a cell was
KEPT, a non-stopping step failed); 2 a stopping step failed or the run was
refused. Each step's outcome is reported separately - a step that did not
run is "not run", never "failed".

Output is mirrored to <root>/_logs/nightly-<UTC time>.log (the last 30 are
kept), and a lock file stops two nights overlapping.
"""

import argparse
import base64
import datetime
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from slice_store import MANIFEST_NAME, Manifest, find_env_file, utc_now  # noqa: E402

CONFIG_NAME = "pipeline.json"
LOCK_NAME = "_nightly.lock"
LOG_DIR = "_logs"
KEEP_LOGS = 30
# A lock older than this is a crashed run, not a running one: a night takes
# minutes to a couple of hours, and the next is due in 24.
STALE_LOCK_HOURS = 20
# Put Blob takes one request up to 5000 MiB; well past any partition so far
# (41 MiB was the largest on the first facility).
MAX_SINGLE_PUT = 4 * 1024 ** 3
AZURE_VERSION = "2021-08-06"

OK, FAILED, NOT_RUN, SKIPPED = "ok", "FAILED", "not run", "skipped"


class Refused(Exception):
    """The run cannot start; the message says why."""


# ----------------------------------------------------------------- config

def load_config(root):
    path = os.path.join(root, CONFIG_NAME)
    if not os.path.isfile(path):
        raise Refused("No %s in %s. It names the facility and how its slices "
                      "were made; see this script's docstring." % (CONFIG_NAME, root))
    with open(path, encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config.get("facility"), int):
        raise Refused("%s needs \"facility\": <id>." % CONFIG_NAME)
    if "sample_types" not in config:
        raise Refused(
            "%s must say \"sample_types\" - \"N\" if the slices were created "
            "with --sample-types N, or null for every type. New slices are "
            "made with it, and a guess would mix QC into some of a cell's "
            "slices and not others." % CONFIG_NAME)
    upload = config.get("upload")
    if upload:
        base = upload.get("base") or ""
        if not base.startswith("https://"):
            raise Refused("upload.base must be an https:// URL.")
        if "?" in base or "sig=" in base:
            raise Refused("upload.base carries a query string. The SAS token "
                          "does not belong in %s, which is tracked; put it in "
                          "the variable upload.sas_env names." % CONFIG_NAME)
        if not upload.get("sas_env"):
            raise Refused("upload needs \"sas_env\", the variable holding the "
                          "SAS token.")
    config.setdefault("provision_pending", True)
    config.setdefault("template", None)
    if "arcgis" not in config:
        config["arcgis"] = os.path.isfile(os.path.join(root, "arcgis.json"))
    return config


def read_secret(name):
    """A value from the environment, else from the nearest .env, else None."""
    value = os.environ.get(name)
    if value:
        return value.strip()
    path = find_env_file()
    if not path:
        return None
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for sep in (":", "="):
                if sep in line:
                    key, value = line.split(sep, 1)
                    if key.strip().lower() == name.lower():
                        return value.strip().strip("'\"")
                    break
    return None


# ------------------------------------------------------------- lock, log

class Lock(object):
    def __init__(self, root):
        self.path = os.path.join(root, LOCK_NAME)
        self.held = False

    def __enter__(self):
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = (time.time() - os.path.getmtime(self.path)) / 3600.0
            if age < STALE_LOCK_HOURS:
                raise Refused("Another night is running (%s, %.1f h old). If "
                              "it is not, delete the file." % (self.path, age))
            os.remove(self.path)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, ("%s pid %d\n" % (utc_now(), os.getpid())).encode())
        os.close(fd)
        self.held = True
        return self

    def __exit__(self, *_exc):
        if self.held and os.path.exists(self.path):
            os.remove(self.path)
        return False


class Log(object):
    """Everything printed, also written to <root>/_logs/."""

    def __init__(self, root):
        directory = os.path.join(root, LOG_DIR)
        os.makedirs(directory, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ")
        self.path = os.path.join(directory, "nightly-%s.log" % stamp)
        self.handle = open(self.path, "w", encoding="utf-8")
        logs = sorted(n for n in os.listdir(directory)
                      if n.startswith("nightly-") and n.endswith(".log"))
        for old in logs[:-KEEP_LOGS]:
            os.remove(os.path.join(directory, old))

    def line(self, text=""):
        print(text)
        sys.stdout.flush()
        self.handle.write(text + "\n")
        self.handle.flush()

    def close(self):
        self.handle.close()


# ------------------------------------------------------------------ steps

def run_script(log, script, args):
    """Run one pipeline script; (exit code, its output lines)."""
    argv = [sys.executable, os.path.join(HERE, script)] + list(args)
    log.line("$ %s" % " ".join([os.path.basename(sys.executable), "pipeline/" + script] + list(args)))
    process = subprocess.Popen(argv, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True,
                               encoding="utf-8", errors="replace")
    lines = []
    for raw in process.stdout:
        text = raw.rstrip("\n")
        lines.append(text)
        log.line("  | " + text)
    return process.wait(), lines


def slices_without_output(root, since):
    """Slice names whose run tonight stored nothing, with the reason."""
    manifest = Manifest(os.path.join(root, MANIFEST_NAME))
    out = []
    for name, entry in sorted((manifest.data.get("slices") or {}).items()):
        if (entry.get("last_run") or "") >= since and entry.get("last_status") != "ok":
            out.append((name, entry.get("last_status")))
    return out


def pending_slices(root):
    manifest = Manifest(os.path.join(root, MANIFEST_NAME))
    return sorted(name for name, entry in (manifest.data.get("slices") or {}).items()
                  if entry.get("needs_report") or not entry.get("report_id"))


# ----------------------------------------------------------------- upload

def md5_b64(path):
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def blob_url(base, relative, sas):
    # The path goes before the query string (docs/failures.md): appended
    # after it, the path is read as part of the signature and refused.
    url = base.rstrip("/") + "/" + urllib.parse.quote(relative)
    return url + ("?" + sas.lstrip("?") if sas else "")


def _request(url, method, data=None, headers=None, tries=3):
    for attempt in range(tries):
        request = urllib.request.Request(url, data=data, method=method,
                                         headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                return response.status, dict(response.headers)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 or exc.code < 500 or attempt == tries - 1:
                return exc.code, dict(exc.headers or {})
        except urllib.error.URLError:
            if attempt == tries - 1:
                raise
        time.sleep(2 * (attempt + 1))
    return None, {}


def upload_file(base, sas, relative, path, content_type):
    """'uploaded', 'unchanged' or raises. Skips a blob whose MD5 matches."""
    size = os.path.getsize(path)
    if size > MAX_SINGLE_PUT:
        raise RuntimeError("%s is %.1f GiB, past a single Put Blob"
                           % (relative, size / 1024.0 ** 3))
    local = md5_b64(path)
    url = blob_url(base, relative, sas)
    status, headers = _request(url, "HEAD", headers={"x-ms-version": AZURE_VERSION})
    remote = {k.lower(): v for k, v in headers.items()}.get("content-md5")
    if status == 200 and remote == local:
        return "unchanged"
    with open(path, "rb") as handle:
        data = handle.read()
    status, _ = _request(url, "PUT", data=data, headers={
        "x-ms-blob-type": "BlockBlob",
        "x-ms-version": AZURE_VERSION,
        "Content-Type": content_type,
        # Azure verifies it and keeps it as the blob's Content-MD5, which is
        # what the HEAD above compares against next night.
        "Content-MD5": local,
    })
    if status not in (200, 201):
        raise RuntimeError("PUT %s answered %s" % (relative, status))
    return "uploaded"


def upload(log, root, config, sas):
    base = config["upload"]["base"]
    with open(os.path.join(root, "manifest.json"), encoding="utf-8") as handle:
        published = json.load(handle)
    files = [(p["path"], "application/octet-stream") for p in published["partitions"]]
    arcgis_dir = os.path.join(root, "arcgis")
    if config.get("arcgis") and os.path.isdir(arcgis_dir):
        for directory, _dirs, names in os.walk(arcgis_dir):
            for name in sorted(names):
                relative = os.path.relpath(os.path.join(directory, name), root)
                files.append((relative.replace(os.sep, "/"),
                              "application/json" if name.endswith(".json")
                              else "application/octet-stream"))
    files.append(("manifest.json", "application/json"))     # LAST
    counts = {"uploaded": 0, "unchanged": 0}
    for relative, content_type in files:
        outcome = upload_file(base, sas, relative, os.path.join(root, relative),
                              content_type)
        counts[outcome] += 1
        log.line("   %-9s %s" % (outcome, relative))
    return counts


# ------------------------------------------------------------------- main

def night(root, config, log, no_upload=False):
    """Run the steps; (steps, notes, exit code). Never raises for a step."""
    facility = str(config["facility"])
    steps = {name: NOT_RUN for name in
             ("nightly", "pending", "arcgis", "publish", "upload")}
    notes = []
    started = utc_now()

    code, lines = run_script(log, "nightly_slices.py",
                             ["--facility", facility, "--root", root])
    steps["nightly"] = OK if code == 0 else FAILED
    if code != 0:
        notes.append("nightly_slices.py exited %d; nothing was published." % code)
        return steps, notes, 2
    kept = [line.strip() for line in lines if " KEPT " in line]
    empty = slices_without_output(root, started)
    for name, status in empty:
        notes.append("slice %s: %s" % (name, status))
    for line in kept:
        notes.append(line)

    waiting = pending_slices(root)
    if not waiting:
        steps["pending"] = SKIPPED
    elif not config["provision_pending"]:
        steps["pending"] = SKIPPED
        notes.append("%d slice(s) wait for a report; provision_pending is off."
                     % len(waiting))
    else:
        args = ["--facility", facility, "--root", root, "--pending"]
        if config["sample_types"]:
            args += ["--sample-types", str(config["sample_types"])]
        if config["template"]:
            args += ["--template", str(config["template"])]
        code, _ = run_script(log, "create_slice_reports.py", args)
        steps["pending"] = OK if code == 0 else FAILED
        if code != 0:
            notes.append("provisioning %d slice(s) failed; their cells stay KEPT."
                         % len(waiting))

    if config["arcgis"]:
        code, _ = run_script(log, "arcgis_export.py", ["--root", root])
        steps["arcgis"] = OK if code == 0 else FAILED
        if code != 0:
            notes.append("arcgis_export.py exited %d." % code)
    else:
        steps["arcgis"] = SKIPPED

    code, _ = run_script(log, "publish_manifest.py", ["--root", root])
    steps["publish"] = OK if code == 0 else FAILED
    if code != 0:
        notes.append("publish_manifest.py exited %d; nothing was uploaded." % code)
        return steps, notes, 2

    if no_upload or not config.get("upload"):
        steps["upload"] = SKIPPED
    else:
        sas = read_secret(config["upload"]["sas_env"])
        if not sas:
            steps["upload"] = FAILED
            notes.append("no SAS token in %s (environment or .env)."
                         % config["upload"]["sas_env"])
            return steps, notes, 2
        log.line("$ upload to %s" % config["upload"]["base"])
        try:
            counts = upload(log, root, config, sas)
            steps["upload"] = OK
            log.line("   %d uploaded, %d unchanged"
                     % (counts["uploaded"], counts["unchanged"]))
        except Exception as exc:                   # noqa: BLE001
            steps["upload"] = FAILED
            # Never echo the token: a URL in an error carries it.
            notes.append("upload failed: %s" % str(exc).replace(sas, "<sas>"))
            return steps, notes, 2

    degraded = any(v == FAILED for v in steps.values()) or bool(empty) or bool(kept)
    return steps, notes, 1 if degraded else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, help="data/<facility>")
    parser.add_argument("--no-upload", action="store_true",
                        help="run everything except the upload")
    args = parser.parse_args(argv)
    root = args.root
    try:
        config = load_config(root)
        with Lock(root):
            log = Log(root)
            try:
                log.line("night for facility %d, root %s, started %s"
                         % (config["facility"], root, utc_now()))
                steps, notes, code = night(root, config, log, args.no_upload)
                log.line()
                log.line("summary")
                for name in ("nightly", "pending", "arcgis", "publish", "upload"):
                    log.line("   %-8s %s" % (name, steps[name]))
                for note in notes:
                    log.line("   - %s" % note)
                log.line("exit %d   log %s" % (code, log.path))
            finally:
                log.close()
            return code
    except Refused as exc:
        sys.stderr.write("\nrun_nightly: %s\n\n" % exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
