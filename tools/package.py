"""Package the plugin as a ZIP for QGIS's "Install from ZIP" tab.

  python tools\\package.py [--output DIR] [--name NAME] [--no-version]

QGIS finds the shortest path ending in metadata.txt and uses that file's parent
directory as the installed plugin folder name, so the archive must contain a
single top-level `equis_loader/` directory. That name has to match the package
name too, because metadata/classFactory resolution imports it by that name.
"""

import argparse
import configparser
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PACKAGE = "equis_loader"
SOURCE = os.path.join(ROOT, PACKAGE)

# Directories never worth shipping.
SKIP_DIRS = {"__pycache__", ".git", ".idea", ".vscode", ".pytest_cache", ".mypy_cache"}

# Build artefacts and editor leftovers.
#
# .pyd is here because a stray one in the plugin source is a build leftover.
# Inside a vendored dependency bundle it is the OPPOSITE: DuckDB's Windows
# binary IS _duckdb.cp312-win_amd64.pyd, and dropping it ships a plugin that
# installs cleanly and then cannot import. VENDOR_DIR is therefore exempt from
# every rule below except __pycache__.
SKIP_SUFFIXES = (".pyc", ".pyo", ".pyd", ".orig", ".rej", ".bak", ".swp", "~")

# Vendored native dependencies, built by tools/vendor_deps.py. Excluded by
# default - the plugin runs without them until the substrate work lands, and
# a bundle is ~75 MB - and included one platform at a time with --bundle.
VENDOR_DIR = "embedded_external_libs"

# A bundled archive carries a cp312 binary, so it cannot run on any QGIS older
# than 4.2 whatever metadata.txt says. The working copy keeps its own minimum
# because it still gates the unbundled plugin, which does run on 3.34; the
# archive's copy is rewritten on the way in, so the one thing QGIS checks
# before installing matches what is actually inside.
BUNDLED_MIN_QGIS = "4.2"

# Credentials must never reach a shareable archive. settings.py deliberately
# reads a .env sitting beside the plugin, so one can plausibly be present in a
# working copy; shipping it would leak an API key to whoever installs the ZIP.
SKIP_NAMES = {".env", ".env.local", "secrets.json", ".ds_store", "thumbs.db"}

# Files the plugin cannot run without.
REQUIRED = ("metadata.txt", "__init__.py", "plugin.py")


def _should_skip(name):
    lowered = name.lower()
    if lowered in SKIP_NAMES:
        return True
    if lowered.endswith(SKIP_SUFFIXES):
        return True
    # Any dotfile is more likely local configuration than plugin content.
    return name.startswith(".")


def _in_vendor(relative):
    return relative.replace("\\", "/").startswith(VENDOR_DIR + "/")


def collect_files(source, bundle=None):
    """Every file to ship, as (absolute path, path relative to the package).

    `bundle` names one directory under embedded_external_libs to include, e.g.
    "win_amd64-cp312". Without it no vendored dependency is shipped at all.
    """
    collected = []
    for directory, subdirs, files in os.walk(source):
        subdirs[:] = sorted(d for d in subdirs if d not in SKIP_DIRS
                            and not d.startswith("."))
        relative_dir = os.path.relpath(directory, source)
        relative_dir = "" if relative_dir == "." else relative_dir

        if relative_dir.replace("\\", "/").split("/")[0] == VENDOR_DIR:
            # Only descend into the one bundle that was asked for.
            parts = relative_dir.replace("\\", "/").split("/")
            if len(parts) == 1:
                subdirs[:] = [d for d in subdirs if d == bundle] if bundle else []
                continue
            if bundle is None or parts[1] != bundle:
                subdirs[:] = []
                continue

        for name in sorted(files):
            absolute = os.path.join(directory, name)
            relative = os.path.relpath(absolute, source)
            # A vendored wheel's own files are shipped as they are: the skip
            # rules describe plugin source, and applying them to a native
            # dependency removes the dependency.
            if not _in_vendor(relative) and _should_skip(name):
                continue
            collected.append((absolute, relative))
    return collected


def _bundled_metadata(path):
    """metadata.txt with the minimum QGIS raised, for a bundled archive.

    Line-wise rather than via configparser: rewriting the file through a
    parser reorders keys and drops the multi-line changelog's formatting, and
    the changelog is the plugin's release history.
    """
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines(True)
    out, seen = [], False
    for line in lines:
        if line.lower().startswith("qgisminimumversion="):
            out.append("qgisMinimumVersion=%s\n" % BUNDLED_MIN_QGIS)
            seen = True
        else:
            out.append(line)
    if not seen:
        out.insert(1, "qgisMinimumVersion=%s\n" % BUNDLED_MIN_QGIS)
    return "".join(out)


def read_metadata(source):
    path = os.path.join(source, "metadata.txt")
    # interpolation=None because the changelog contains bare % signs ("about
    # 20% larger"), and the default interpolation treats those as syntax.
    # Reading the section is lazy, so this only bites when a field carrying a
    # % is fetched - it has been latent rather than harmless.
    parser = configparser.ConfigParser(interpolation=None)
    # Keep option case; QGIS metadata keys are case-sensitive.
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    if not parser.has_section("general"):
        raise SystemExit("metadata.txt has no [general] section: %s" % path)
    return parser["general"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=os.path.join(ROOT, "dist"),
                        help="directory to write the archive into (default: dist/)")
    parser.add_argument("--name", help="archive filename, without .zip")
    parser.add_argument("--no-version", action="store_true",
                        help="omit the version from the filename")
    parser.add_argument("--bundle",
                        help="include one vendored dependency bundle, e.g. "
                             "win_amd64-cp312 (see tools/vendor_deps.py). "
                             "The archive is then platform-specific and its "
                             "name says so.")
    parser.add_argument("--list-bundles", action="store_true",
                        help="show which bundles have been built, and exit")
    args = parser.parse_args()

    vendor_root = os.path.join(SOURCE, VENDOR_DIR)
    built = sorted(n for n in os.listdir(vendor_root)
                   if not n.startswith("_")
                   and os.path.isdir(os.path.join(vendor_root, n))) \
        if os.path.isdir(vendor_root) else []
    if args.list_bundles:
        print("Bundles built: %s" % (", ".join(built) or "none"))
        print("Build one with: python tools/vendor_deps.py --target win "
              "--python 312")
        return 0
    if args.bundle and args.bundle not in built:
        raise SystemExit(
            "No such bundle: %s\n  Built: %s\n  Build it with: python "
            "tools/vendor_deps.py --target <win|mac|linux> --python 312"
            % (args.bundle, ", ".join(built) or "none"))

    if not os.path.isdir(SOURCE):
        raise SystemExit("Plugin source not found: %s" % SOURCE)

    metadata = read_metadata(SOURCE)
    version = metadata.get("version", "0.0.0").strip()

    files = collect_files(SOURCE, bundle=args.bundle)
    shipped = {relative.replace("\\", "/") for _, relative in files}
    missing = [name for name in REQUIRED if name not in shipped]
    if missing:
        raise SystemExit("Refusing to package, missing: %s" % ", ".join(missing))

    # The icon is declared in metadata; a broken reference is easy to miss.
    icon = (metadata.get("icon") or "").strip()
    if icon and icon not in shipped:
        print("warning: metadata icon %r is not in the package" % icon)

    if args.name:
        stem = args.name
    elif args.no_version:
        stem = PACKAGE if not args.bundle else "%s-%s" % (PACKAGE, args.bundle)
    elif args.bundle:
        # A bundled archive only works on one platform and one Python, so the
        # filename has to say which - handing someone the wrong one otherwise
        # fails at import time with nothing to explain why.
        stem = "%s-%s-%s" % (PACKAGE, version, args.bundle)
    else:
        stem = "%s-%s" % (PACKAGE, version)

    os.makedirs(args.output, exist_ok=True)
    target = os.path.join(args.output, stem + ".zip")
    if os.path.exists(target):
        os.remove(target)

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for absolute, relative in files:
            # Forward slashes and the package prefix: QGIS derives the install
            # directory from the folder containing metadata.txt.
            arcname = "%s/%s" % (PACKAGE, relative.replace("\\", "/"))
            if args.bundle and relative.replace("\\", "/") == "metadata.txt":
                archive.writestr(arcname, _bundled_metadata(absolute))
                continue
            archive.write(absolute, arcname)

    size = os.path.getsize(target)
    print("%s  (%d files, %.1f MB)" % (target, len(files), size / 1048576.0))
    if args.bundle:
        vendored = sum(1 for _a, r in files if _in_vendor(r))
        print("  bundle: %s  (%d files)" % (args.bundle, vendored))
        print("  NOTE: this archive runs ONLY on that platform and that "
              "Python.")
        print("  qgisMinimumVersion raised to %s in the archive (the working "
              "copy is unchanged)." % BUNDLED_MIN_QGIS)
    else:
        print("  bundle: none (built: %s)" % (", ".join(built) or "none"))
    print("  plugin: %s %s" % (metadata.get("name", PACKAGE), version))
    print("  QGIS:   %s - %s" % (metadata.get("qgisMinimumVersion", "?"),
                                 metadata.get("qgisMaximumVersion", "?")))
    print("\nInstall in QGIS via Plugins > Manage and Install Plugins >")
    print("Install from ZIP, then enable '%s'." % metadata.get("name", PACKAGE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
