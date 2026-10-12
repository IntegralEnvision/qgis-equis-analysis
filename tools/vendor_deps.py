"""Download and unpack the plugin's bundled dependencies.

Phase 0 of docs/vision-linked-views.md. Produces the layout equis_loader
/vendor.py expects:

    equis_loader/embedded_external_libs/<platform>-<pytag>/

Run for the interpreter you are on:

    python tools/vendor_deps.py --target local

Run for a QGIS you are not on (this is the normal case - QGIS 4.2.1 ships
Python 3.12 and your shell probably does not):

    python tools/vendor_deps.py --target win
    python tools/vendor_deps.py --target all

Needs network, and pip, and nothing else. It does not build anything: every
dependency publishes wheels, which is the only reason this is tractable.

WHY THE PLATFORM LISTS ARE UGLY. pip matches --platform as a literal tag, not
as a floor, and these projects do not agree on which manylinux they target:
duckdb publishes manylinux_2_26/2_28, arro3 publishes manylinux_2_17
/manylinux2014. Passing one tag satisfies one project and fails the other with
"No matching distribution found", which reads like the package does not exist.
So every tag either project uses is passed, and pip picks per project.

WHY --abi IS ALSO A LIST. duckdb is cp312-specific; arro3-core and arro3-io
are cp311-abi3. One --abi excludes one of them.
"""

import argparse
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIBS_ROOT = os.path.join(ROOT, "equis_loader", "embedded_external_libs")

PACKAGES = ("duckdb", "arro3-io")

# The Python QGIS 4.2.x ships. Verified 4.2.1 on Windows = 3.12.13; macOS and
# Linux are unconfirmed, which is exactly what running this on them settles.
DEFAULT_PYTAG = "312"

TARGETS = {
    "win": {
        "bundle": "win_amd64",
        "platforms": ["win_amd64"],
    },
    "mac": {
        "bundle": "macosx_arm64",
        "platforms": ["macosx_11_0_arm64", "macosx_12_0_arm64"],
    },
    "mac_intel": {
        "bundle": "macosx_x86_64",
        "platforms": ["macosx_10_12_x86_64", "macosx_11_0_x86_64"],
    },
    "linux": {
        "bundle": "linux_x86_64",
        "platforms": ["manylinux_2_17_x86_64", "manylinux2014_x86_64",
                      "manylinux_2_26_x86_64", "manylinux_2_28_x86_64"],
    },
}



def human(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.1f %s" % (size, unit)
        size /= 1024.0


def tree_size(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def download(spec, pytag, into):
    """pip download for one target. Returns the wheel paths."""
    os.makedirs(into, exist_ok=True)
    command = [sys.executable, "-m", "pip", "download", "--only-binary=:all:",
               "--python-version", pytag, "--implementation", "cp",
               "-d", into]
    for plat in spec["platforms"]:
        command += ["--platform", plat]
    for abi in ["cp%s" % pytag, "abi3", "none"]:
        command += ["--abi", abi]
    command += list(PACKAGES)

    print("    pip download ...")
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-4:]
        raise RuntimeError("pip download failed:\n      "
                           + "\n      ".join(tail))
    return sorted(os.path.join(into, n) for n in os.listdir(into)
                  if n.endswith(".whl"))


def unpack(wheels, into):
    """Unzip wheels side by side, the way an installed site-packages looks."""
    if os.path.isdir(into):
        shutil.rmtree(into)
    os.makedirs(into)
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(into)
    # Bytecode for the wrong interpreter is dead weight in the zip and
    # confusing when it appears in a traceback from a different Python.
    for base, dirs, _files in os.walk(into):
        for name in list(dirs):
            if name == "__pycache__":
                shutil.rmtree(os.path.join(base, name), ignore_errors=True)
                dirs.remove(name)
    return into


def build(target, pytag, keep_wheels=False):
    spec = TARGETS[target]
    bundle = "%s-cp%s" % (spec["bundle"], pytag)
    print("  %s -> %s" % (target, bundle))
    staging = os.path.join(LIBS_ROOT, "_wheels", bundle)
    into = os.path.join(LIBS_ROOT, bundle)

    wheels = download(spec, pytag, staging)
    for path in wheels:
        print("      %s (%s)" % (os.path.basename(path),
                                 human(os.path.getsize(path))))
    unpack(wheels, into)
    if not keep_wheels:
        shutil.rmtree(staging, ignore_errors=True)

    size = tree_size(into)
    top = sorted(n for n in os.listdir(into) if not n.endswith(".dist-info"))
    print("      unpacked %s -> %s" % (human(size), bundle))
    print("      contains: %s" % ", ".join(top))
    return {"bundle": bundle, "size": size, "wheels": len(wheels)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="local",
                        choices=sorted(TARGETS) + ["local", "all"])
    parser.add_argument("--python", default=DEFAULT_PYTAG,
                        help="Python tag to build for, e.g. 312 (default) "
                             "or 313. Must match the QGIS that will load it.")
    parser.add_argument("--keep-wheels", action="store_true",
                        help="leave the downloaded .whl files in place")
    args = parser.parse_args()

    if args.target == "local":
        sys.path.insert(0, ROOT)
        from equis_loader import vendor
        tag = vendor.platform_tag()
        targets = [name for name, spec in TARGETS.items()
                   if spec["bundle"] == tag]
        if not targets:
            print("No target defined for this platform (%s)." % tag)
            return 1
        pytag = "%d%d" % sys.version_info[:2]
        print("local: %s, Python %s" % (tag, pytag))
        # This script does not import duckdb, so it runs under any Python -
        # but --target local takes its tags from whichever Python is running
        # it, and a bundle built for the wrong minor version will not load.
        # QGIS 4.2 ships 3.12; a system Python is very often something else.
        if pytag != DEFAULT_PYTAG:
            print("")
            print("  WARNING: this interpreter is cp%s, but QGIS 4.2 ships "
                  "cp%s." % (pytag, DEFAULT_PYTAG))
            print("  duckdb publishes no abi3 wheel, so a cp%s bundle will "
                  "NOT load in QGIS." % pytag)
            print("  Either run this from the QGIS Python, or build "
                  "explicitly:")
            print("      python tools/vendor_deps.py --target %s --python %s"
                  % (targets[0], DEFAULT_PYTAG))
            print("")
    else:
        targets = sorted(TARGETS) if args.target == "all" else [args.target]
        pytag = args.python

    print("Building into %s" % LIBS_ROOT)
    results, failures = [], []
    for name in targets:
        try:
            results.append(build(name, pytag, args.keep_wheels))
        except Exception as exc:
            print("      FAILED: %s" % exc)
            failures.append(name)

    print("")
    for item in results:
        print("  %-24s %8s  (%d wheels)"
              % (item["bundle"], human(item["size"]), item["wheels"]))
    if failures:
        print("  failed: %s" % ", ".join(failures))
        return 1
    print("")
    print("  Verify with:  python tools/test_phase0.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
