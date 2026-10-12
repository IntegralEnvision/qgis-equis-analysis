r"""The chart's mixed-unit refusal, asserted in node.

Thin wrapper so web/test/mixed-units.mjs runs in the normal suite. The
logic lives beside the code it describes; tools/test_chart_units.py checks
the same rule against the real facility.

    python tools/test_chart_mixed_units.py
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
    result = subprocess.run(["node", os.path.join("test", "mixed-units.mjs")],
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
