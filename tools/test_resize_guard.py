r"""The chart's resize observer must not redraw the chart it is watching.

A thin wrapper so the node assertion runs in the normal suite. The logic
lives in web/test/resize-guard.mjs, beside the code it describes.

    python tools/test_resize_guard.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from node_path import ensure_node  # noqa: E402

ensure_node()
script = os.path.join("test", "resize-guard.mjs")
try:
    result = subprocess.run(["node", script], cwd=os.path.join(ROOT, "web"),
                            capture_output=True, text=True)
except (OSError, FileNotFoundError):
    # Node is build-time only and genuinely absent from a QGIS install.
    print("node is not on PATH; it is build-time only")
    print("SKIPPED")
    raise SystemExit(0)

print(result.stdout.strip())
if result.returncode != 0:
    print(result.stderr[:400])
    raise SystemExit(1)
