"""Print what the WebEngine probes found, one configuration at a time.

    python tools/webengine_probes/summarise.py <dir> [<dir> ...]

Reads results.json (wasm_probe.py) and every gpu_<tag>.json (gpu_probe.py)
in each directory. Each stage and run is printed on its own; nothing is
averaged, so a run that disagrees with its neighbours stays visible.
"""
import glob
import json
import os
import re
import sys


def feature_status(text):
    flat = re.sub(r"\s+", " ", text or "")
    status = {}
    for key in ("Compositing", "Rasterization", "WebGL", "WebGL2", "WebGPU"):
        m = re.search(r"\* %s: ([^*]+?)(?=\*| Version Information)" % key, flat)
        status[key] = m.group(1).strip() if m else "?"
    line = flat[flat.find("Command Line"):flat.find("Driver Information")]
    return status, line


def wasm(path):
    d = json.load(open(path))
    print("== WASM probe", path)
    print("  qt:", d["qt"])
    print("  python duckdb:", {k: v for k, v in d["python"].items()})
    for mode, r in d["pages"].items():
        print("  --", mode, r.get("fatal", ""))
        for name in r.get("order", []):
            print("    %-30s %s" % (name, json.dumps(r["stages"][name])[:240]))
    status, line = feature_status(d.get("chrome_gpu"))
    print("  chrome://gpu:", status)


def gpu(path):
    d = json.load(open(path))
    print("== GPU", d["tag"], d.get("fatal", "")[:300])
    if "wrote_setting" in d:
        print("  wrote", d["wrote_setting"])
        return
    print("  flags:", d["env"].get("QTWEBENGINE_CHROMIUM_FLAGS"))
    print("  qt:", d["qt"])
    status, line = feature_status(d.get("chrome_gpu"))
    print("  chrome://gpu:", status)
    print("  started with --disable-gpu:", "--disable-gpu " in line,
          "| use-gl:", re.findall(r"--use-gl=\S+", line))
    page = d.get("page") or {}
    for k in ("webgl", "webgl2", "webgpu"):
        v = page.get(k) or {}
        print("  %-7s ok=%s %s" % (k, v.get("ok"), v.get("renderer") or v.get("adapter") or v.get("error")))
    print("  shown:", page.get("scene"), "| pixels on screen:", (d.get("pixels") or {}).get("screen"),
          d.get("screenshot_error", "")[:200])
    print("  draw WebGL:", [(b.get("n"), b.get("drawMs"), b.get("error")) for b in page.get("benchGL", [])])
    print("  draw 2D:   ", [(b.get("n"), b.get("drawMs")) for b in page.get("bench2d", [])])
    if page.get("fps"):
        print("  displayed frames:", page["fps"])
    msgs = sorted(set(m for m in d["qt_messages"] if re.search(r"(?i)gpu|gl|d3d|graphics", m)))
    print("  qt messages:", msgs[:6])
    dead = [e for e in d.get("events", []) if "Terminated" in e[1]]
    if dead:
        print("  RENDER PROCESS DIED:", dead)


for folder in sys.argv[1:] or ["."]:
    if os.path.isfile(os.path.join(folder, "results.json")):
        wasm(os.path.join(folder, "results.json"))
    for path in sorted(glob.glob(os.path.join(folder, "gpu_*.json"))):
        gpu(path)
