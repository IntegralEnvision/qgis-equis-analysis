"""Find node, which the QGIS launcher removes from PATH.

`python-qgis.bat` rebuilds PATH from the OSGeo4W environment, so a node
installed for the user is not on it - which made every node-dependent test
SKIP under QGIS while passing under the plain interpreter. Neither
interpreter has both QGIS and node by default, so a test needing both could
never run anywhere.

Node is build-time only and genuinely absent from a plain QGIS install, so
"not found" stays a skip rather than a failure. This only finds it where it
IS installed.
"""

import os
import shutil

# Where a Windows node install lands. Checked in order; the first hit wins.
CANDIDATES = (
    r"C:\Program Files\nodejs",
    r"C:\Program Files (x86)\nodejs",
    os.path.join(os.environ.get("APPDATA", ""), "npm"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "nodejs"),
)


def ensure_node():
    """Put node on PATH if it can be found. Returns its path, or None."""
    found = shutil.which("node")
    if found:
        return found
    for directory in CANDIDATES:
        if directory and os.path.isfile(os.path.join(directory, "node.exe")):
            os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
            return shutil.which("node")
    return None
