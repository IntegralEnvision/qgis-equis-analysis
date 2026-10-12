"""The committed web bundle: present, shippable, and matching its sources.

Node is a BUILD-time dependency only, so this suite does not need it. What it
checks is that the artifact Node produced is actually there and actually gets
into the zip - the two ways "the bundle is checked in" quietly stops being
true.

    python tools/test_web_bundle.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

WEB = os.path.join(ROOT, "equis_loader", "web")
FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


check("the bundle directory is committed", os.path.isdir(WEB), WEB)
if not os.path.isdir(WEB):
    print("\nRun: cd web && npm ci && npm run build")
    raise SystemExit(1)

for name in ("app.js", "index.html", "BUILD.json"):
    check("%s is present" % name, os.path.isfile(os.path.join(WEB, name)))

# --- BUILD.json records what produced it -----------------------------------
# Without the versions, a bundle years from now is an opaque blob nobody can
# reproduce. This is the whole reason it is written.
with open(os.path.join(WEB, "BUILD.json"), encoding="utf-8") as handle:
    build = json.load(handle)
check("BUILD.json names the versions it was built from",
      bool(build.get("versions", {}).get("@uwdata/vgplot"))
      and bool(build.get("versions", {}).get("esbuild")),
      str(build.get("versions")))
check("BUILD.json records each file's hash",
      all("sha256" in entry for entry in build.get("files", {}).values()),
      str(sorted(build.get("files", {}))))

# --- the hashes still describe the files ------------------------------------
# Catches a hand-edited bundle, which would otherwise look fine until a
# rebuild silently reverted someone's change.
import hashlib  # noqa: E402

for name, entry in sorted(build.get("files", {}).items()):
    path = os.path.join(WEB, name)
    if not os.path.isfile(path):
        check("%s in BUILD.json exists" % name, False)
        continue
    with open(path, "rb") as handle:
        blob = handle.read()
    digest = hashlib.sha256(blob).hexdigest()[:16]
    check("%s matches its recorded hash" % name, digest == entry["sha256"],
          "%s vs %s" % (digest, entry["sha256"]))
    check("%s matches its recorded size" % name, len(blob) == entry["bytes"],
          "%d vs %d" % (len(blob), entry["bytes"]))

# --- it is a real ES module bundle -----------------------------------------
with open(os.path.join(WEB, "app.js"), "rb") as handle:
    app = handle.read()
check("app.js is substantial", len(app) > 100000, "%d bytes" % len(app))
check("app.js carries no bare specifier imports",
      b'from"@uwdata/' not in app and b'from "@uwdata/' not in app,
      "an unbundled import would fail: a browser resolves URLs, not "
      "package names")

with open(os.path.join(WEB, "index.html"), encoding="utf-8") as handle:
    page = handle.read()
check("index.html loads app.js as a module",
      'type="module"' in page and "app.js" in page)
check("index.html pulls qwebchannel.js from qrc:",
      "qrc:///qtwebchannel/qwebchannel.js" in page,
      "Qt injects it; bundling a copy would shadow the real one")

# --- packaging --------------------------------------------------------------
# The bundle being committed is only useful if it reaches the zip, and only
# safe if node_modules does not. Checked through package.py's own collector
# rather than by building a zip, so this stays fast.
sys.path.insert(0, HERE)
import package as packager  # noqa: E402

shipped = {relative.replace("\\", "/")
           for _, relative in packager.collect_files(
               os.path.join(ROOT, "equis_loader"))}
for name in ("web/app.js", "web/index.html", "web/BUILD.json"):
    check("%s is packaged" % name, name in shipped,
          str(sorted(n for n in shipped if n.startswith("web/"))))
check("nothing from node_modules is packaged",
      not any("node_modules" in name for name in shipped))

print()
if FAILURES:
    print("FAILED: %s" % ", ".join(FAILURES))
    raise SystemExit(1)
print("The committed web bundle is intact and ships with the plugin.")
