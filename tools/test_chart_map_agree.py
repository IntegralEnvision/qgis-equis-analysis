r"""US-12: the chart's count and the map's feature count are one number.

*As a user, I want the count in the chart caption and the feature count of
the layer to be the same number, so that I can trust either one.*

Both come from the same store, so a difference is drift between two code
paths rather than a data question - which is exactly the kind of thing that
survives review and shows up as a user asking why a chart says 296 and a
layer says 906.

THE CHART'S SQL IS THE REAL ONE, taken from web/src/histogram.js through
node, not reimplemented here. A Python copy of that query would agree with
itself forever and prove nothing; the whole point is to catch the two
implementations diverging.

    "C:\Program Files\QGIS 4.2.1\bin\python-qgis.bat" tools\test_chart_map_agree.py

Needs QGIS (for the layer builder), node (for the chart's SQL) and the real
facility parquet. Skips cleanly without any of them.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from node_path import ensure_node  # noqa: E402
os.chdir(ROOT)

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


try:
    from qgis.core import QgsApplication
except ImportError as exc:
    print("needs QGIS: %s" % exc)
    print("SKIPPED")
    raise SystemExit(0)

PUBLISHED = os.path.join("data", "facility_a", "manifest.json")
if not os.path.isfile(PUBLISHED):
    print("no published manifest at %s" % PUBLISHED)
    print("SKIPPED")
    raise SystemExit(0)

QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", ""), True)
app = QgsApplication([], False)
app.initQgis()

import equis_loader.report_data as rd                       # noqa: E402
from equis_loader.layer_builder import build_joined_layer   # noqa: E402
from equis_loader.store import ND_COLUMN, ReportStore       # noqa: E402

# Each case is the SAME filter expressed twice: once as the chart's options
# and once as the arguments the map build takes.
CASES = [
    ("Arsenic, unfiltered",
     {"chemical": "Arsenic", "casRn": "7440-38-2"},
     {}),
    ("Arsenic in WS",
     {"chemical": "Arsenic", "casRn": "7440-38-2", "matrix": "WS"},
     {"matrix": ["WS"]}),
    ("Arsenic in WS, dissolved",
     {"chemical": "Arsenic", "casRn": "7440-38-2", "matrix": "WS",
      "fraction": "D"},
     {"matrix": ["WS"], "fraction": ["D"]}),
    ("Lead in SE",
     {"chemical": "Lead", "casRn": "7439-92-1", "matrix": "SE"},
     {"matrix": ["SE"]}),
    # A filter that matches nothing must agree at ZERO, not disagree about
    # which kind of nothing. Chloride here is WS/T and WS/N only.
    ("Chloride in WS/D, which has no data",
     {"chemical": "Chloride", "matrix": "WS", "fraction": "D"},
     {"matrix": ["WS"], "fraction": ["D"]}),
]

GENERATOR = """
import { histogramSql } from "./src/histogram.js";
const cases = %s;
console.log(JSON.stringify(cases.map((c) => histogramSql(c))));
"""


def chart_sql(option_sets):
    """The real histogramSql output, or None when node is unavailable."""
    web = os.path.join(ROOT, "web")
    path = os.path.join(web, "_agreecheck.mjs")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(GENERATOR % json.dumps(option_sets))
    ensure_node()
    try:
        result = subprocess.run(["node", "_agreecheck.mjs"], cwd=web,
                                capture_output=True, text=True)
    except (OSError, FileNotFoundError):
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if result.returncode != 0:
        print("node failed: %s" % result.stderr[:300])
        return None
    return json.loads(result.stdout)


store = ReportStore.from_manifest("data/facility_a")
print("store: %s rows" % format(store.count(), ",d"))

# The chart bins what the MAP shows, so both sides use the aggregation the
# layer was built with. `most_recent` is the panel's default.
for options, _ in ((c[1], c[2]) for c in CASES):
    options["aggregate"] = "most_recent"
    options.setdefault("ndColumn", ND_COLUMN[rd.ND_HALF])

statements = chart_sql([c[1] for c in CASES])
if statements is None:
    print("node is not on PATH; it is build-time only")
    print("SKIPPED")
    raise SystemExit(0)

pages = store.location_features()
print()

for (label, options, build), sql in zip(CASES, statements):
    # THE CHART: run its own SQL and count what it would bin.
    charted = store.con.execute(
        "SELECT count(*) FROM (%s)" % sql).fetchone()[0]

    # THE MAP: the aggregate the layer is built from, then the layer.
    summary = store.aggregate(options["chemical"],
                              options.get("casRn"),
                              how=rd.MOST_RECENT,
                              matrix=build.get("matrix", rd.ALL_MATRICES),
                              fraction=build.get("fraction", rd.ANY_FRACTION),
                              method=build.get("method", rd.ALL_METHODS))
    layer, matched, _unmatched, _warning = build_joined_layer(
        label, pages, summary, options["chemical"])
    mapped = layer.featureCount() if layer is not None else 0

    check("%s: chart %s == map %s" % (label, format(charted, ",d"),
                                      format(mapped, ",d")),
          charted == mapped,
          "chart=%d map=%d summary=%d" % (charted, mapped, len(summary)))

print()
# The join fix of 2026-09-26 is what makes the above possible: before it,
# every location joined onto every layer and the map reported 906 features
# against a chart of 296. Assert the mechanism, not just the numbers.
summary = store.aggregate("Chloride", how=rd.MOST_RECENT,
                          matrix=["WS"], fraction=["D"])
layer, _m, _u, _w = build_joined_layer("empty", pages, summary, "Chloride")
check("a filter with no data maps no features, rather than all of them",
      (layer.featureCount() if layer is not None else 0) == 0,
      str(layer.featureCount() if layer is not None else "no layer"))

print()
if FAILURES:
    print("FAILED: %s" % ", ".join(FAILURES))
    raise SystemExit(1)
print("The chart and the map report one number.")
