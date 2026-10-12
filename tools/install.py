"""Copy the plugin into the QGIS plugin directory.

  python tools\\install.py [--profile default] [--dest <path>]
"""

import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SOURCE = os.path.join(ROOT, "equis_loader")

SKIP_DIRS = {"__pycache__", ".git"}
SKIP_SUFFIXES = (".pyc", ".pyo")


def default_destination(profile):
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    # QGIS 4 uses a QGIS4 profile root; QGIS 3 used QGIS3.
    for version in ("QGIS4", "QGIS3"):
        root = os.path.join(appdata, "QGIS", version, "profiles", profile)
        if os.path.isdir(root):
            return os.path.join(root, "python", "plugins", "equis_loader")
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="default")
    parser.add_argument("--dest")
    args = parser.parse_args()

    dest = args.dest or default_destination(args.profile)
    if not dest:
        print("Could not locate a QGIS profile. Pass --dest explicitly.")
        return 1

    if not os.path.isdir(SOURCE):
        print("Plugin source not found: %s" % SOURCE)
        return 1

    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)

    copied = 0
    for root, dirs, files in os.walk(SOURCE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel = os.path.relpath(root, SOURCE)
        target_dir = dest if rel == "." else os.path.join(dest, rel)
        os.makedirs(target_dir, exist_ok=True)
        for name in files:
            if name.endswith(SKIP_SUFFIXES):
                continue
            shutil.copy2(os.path.join(root, name), os.path.join(target_dir, name))
            copied += 1

    print("Installed %d files to:\n  %s" % (copied, dest))
    print("\nRestart QGIS, then enable 'EQuIS Data Loader' in the Plugin Manager.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
