r"""A replaced temp view must not serve cached rows.

Thin wrapper so web/test/view-cache.mjs runs in the normal suite.

    python tools/test_view_cache.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from node_path import ensure_node  # noqa: E402

ensure_node()
try:
    result = subprocess.run(["node", os.path.join("test", "view-cache.mjs")],
                            cwd=os.path.join(ROOT, "web"),
                            capture_output=True, text=True)
except (OSError, FileNotFoundError):
    print("node is not on PATH; it is build-time only")
    print("SKIPPED")
    raise SystemExit(0)

print(result.stdout.strip())
if result.returncode != 0:
    print(result.stderr[:400])
    raise SystemExit(1)
