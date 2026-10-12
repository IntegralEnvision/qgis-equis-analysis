"""Put the plugin's bundled dependencies on sys.path, and take them off again.

Phase 0 of the linked-views plan (docs/vision-linked-views.md): DuckDB and an
Arrow implementation have to reach a stock QGIS install with no compiler, no
network at install time, and no help from IT. QDuckDB solved the same problem
by embedding the wheels in the plugin directory and activating them only while
the plugin is loaded; this is that, for this plugin.

No QGIS imports, so it is testable with a plain interpreter.

THE PART THAT BITES. `duckdb` publishes no abi3 wheel, so its binary is
specific to a Python MINOR VERSION as well as a platform - a cp312 build
simply will not import under 3.13. `arro3-core` and `arro3-io` do publish
abi3 (cp311+), so one of those per platform covers every Python. The bundle is
therefore named for the platform AND the Python it was built for, and the
failure this produces in the field - right plugin, wrong Python - is named
explicitly rather than surfacing as "No module named 'duckdb'".

Layout, built by tools/vendor_deps.py and NOT committed:

    equis_loader/embedded_external_libs/
        win_amd64-cp312/
        macosx_arm64-cp312/
        linux_x86_64-cp312/
"""

import os
import platform
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIBS_ROOT = os.path.join(HERE, "embedded_external_libs")

# What the plugin needs out of the bundle. Order matters only for reporting.
REQUIRED = ("duckdb", "arro3.core", "arro3.io")


def python_tag(info=None):
    """cp312 for CPython 3.12, and so on."""
    info = info or sys.version_info
    return "cp%d%d" % (info[0], info[1])


def platform_tag(system=None, machine=None):
    """A short, stable name for this OS and CPU.

    Deliberately coarser than a wheel tag: the bundle holds already-unpacked
    wheels, so it only has to distinguish builds we actually ship, not satisfy
    pip's matching rules.
    """
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    if machine in ("amd64", "x86_64"):
        arch = "x86_64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        arch = machine or "unknown"

    if system.startswith("win"):
        # Windows is x86_64 in every QGIS build we support; keep the name pip
        # uses so the mapping to a wheel is obvious when debugging.
        return "win_amd64" if arch == "x86_64" else "win_%s" % arch
    if system == "darwin":
        return "macosx_%s" % arch
    return "linux_%s" % arch


def bundle_name(system=None, machine=None, info=None):
    return "%s-%s" % (platform_tag(system, machine), python_tag(info))


def bundle_path(root=LIBS_ROOT, **kwargs):
    return os.path.join(root, bundle_name(**kwargs))


def available_bundles(root=LIBS_ROOT):
    """Bundles present on disk, whether or not they match this interpreter."""
    try:
        return sorted(name for name in os.listdir(root)
                      # Leading underscore is the build's own scratch space
                      # (_wheels); listing it as a bundle makes a machine with
                      # nothing usable look provisioned.
                      if not name.startswith("_")
                      and os.path.isdir(os.path.join(root, name)))
    except OSError:
        return []


def describe_environment():
    """What we are, for an error message that lets someone act."""
    return {
        "python": "%d.%d.%d" % sys.version_info[:3],
        "python_tag": python_tag(),
        "platform": platform.system(),
        "machine": platform.machine(),
        "bundle": bundle_name(),
    }


class VendorError(RuntimeError):
    """Raised with a message that names what was wanted and what was found."""


def activate(root=LIBS_ROOT, strict=False):
    """Put the matching bundle at the front of sys.path.

    Returns the path activated, or None when there is no bundle for this
    interpreter and `strict` is false. Front rather than back on purpose: the
    plugin should get the version it was tested against, not whichever copy
    happens to be installed elsewhere in the QGIS profile.

    Idempotent - activating twice does not stack duplicate entries.
    """
    wanted = bundle_path(root)
    if not os.path.isdir(wanted):
        if not strict:
            return None
        env = describe_environment()
        found = available_bundles(root)
        raise VendorError(
            "No bundled dependencies for this interpreter.\n"
            "  This QGIS runs Python %s (%s) on %s %s, so it needs the "
            "'%s' bundle.\n"
            "  Bundles present: %s\n"
            "  duckdb ships no abi3 wheel, so a bundle built for one Python "
            "minor version cannot be used by another. Rebuild with "
            "tools/vendor_deps.py --target %s."
            % (env["python"], env["python_tag"], env["platform"],
               env["machine"], env["bundle"],
               ", ".join(found) if found else "none",
               platform_tag()))
    if wanted not in sys.path:
        sys.path.insert(0, wanted)
    return wanted


def deactivate(root=LIBS_ROOT):
    """Take the bundle back off sys.path.

    Does NOT unload already-imported modules: a native extension cannot be
    meaningfully unloaded, and pretending otherwise would be worse than
    leaving it. This exists so a disabled plugin stops shadowing other
    people's imports, not to undo the import.
    """
    wanted = bundle_path(root)
    while wanted in sys.path:
        sys.path.remove(wanted)
    return wanted


def check(root=LIBS_ROOT):
    """Try to import everything the plugin needs. Never raises.

    Returns {name: version-or-error}, plus the environment, so one call
    produces the whole picture for a report.
    """
    result = {
        "environment": describe_environment(),
        "bundle_path": bundle_path(root),
        "bundle_present": os.path.isdir(bundle_path(root)),
        "available": available_bundles(root),
        "modules": {},
    }
    activate(root)
    for name in REQUIRED:
        try:
            module = __import__(name, fromlist=["__version__"])
            version = getattr(module, "__version__", "?")
            where = getattr(module, "__file__", "?")
            result["modules"][name] = {
                "ok": True,
                "version": version,
                # Which copy answered matters: a system-wide duckdb silently
                # winning would make the bundle look like it worked.
                "from_bundle": bool(where and where.startswith(
                    result["bundle_path"])),
                "path": where,
            }
        except Exception as exc:
            result["modules"][name] = {
                "ok": False,
                "error": "%s: %s" % (type(exc).__name__, exc),
            }
    result["ok"] = all(m.get("ok") for m in result["modules"].values())
    return result
