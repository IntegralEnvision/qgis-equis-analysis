# Do the QGIS plugin and ChemDuck agree? A parity measurement on one facility

**Written 2026-10-22.** Measured on Facility A (3,452,146 results, 906
locations; stations are given neutral labels here), Windows 11, DuckDB 1.5.5. Reproduce with
`tools/measure_chemduck_parity.py` in the QGIS plugin repository.

> **Status, 2026-10-28.** This measured ChemDuck 0.1's `aggregate_results`,
> before the shared rules. ChemDuck has since adopted them as
> `aggregate_points`: events by sample and timestamp, measurement points,
> selection across methods, one limit chain, refusal of mixed units. Most of
> the causes below are decided now, not open. The numbers stand as a record
> of where the two started. The re-measurement against `aggregate_points` is
> phase 1 of [chemduck-adoption-plan.md](chemduck-adoption-plan.md), where
> this script becomes a test.

## Why this exists

Three projects now read the same EQuIS Analytical Results II (ARII) parquet
and turn it into a value per location:

| Project | What it is | Where its rules live |
|---|---|---|
| **QGIS plugin** (`equis_qgis_public`) | Maps EQuIS chemistry in QGIS | Python and SQL in `store.py` / `report_data.py`, held to each other by a parity test |
| **ChemDuck** (`chemduck`) | Portable environmental-chemistry schema | SQL macros and views, chiefly `aggregate_results` |
| **Chemrooms** (`chemrooms`) | Browser workbench on SQLRooms and DuckDB-WASM | ChemDuck's SQL, loaded at startup |

The plugin also has a third copy of its non-detect rules in its parquet
pipeline (`pipeline/arcgis_export.py`), held to the store by its own parity
test. ChemDuck's rules were ported from the plugin's on 2026-09-30
(`chemduck/docs/domain-rules-remediation.md`), but nothing compared the two
afterwards. Two tools reading the same facility could map different numbers
at the same location, and nothing would notice.

This measurement asks one question: **given the same data and the same
settings, do the two give the same value, detect flag and date at each
location?** Where they don't, it names the cause. Every difference found is
attributed to a cause; none were left unexplained.

## How it was measured

1. ChemDuck's own loader built a ChemDuck database from the facility's
   `manifest.json` (199 s). It holds the same 3,452,146 results and 906
   locations the plugin sees.
2. The plugin opened the same manifest with `ReportStore.from_manifest`.
3. For the 40 most-measured analytes, in every matrix and fraction they occur
   in (231 combinations, 28,187 location cells), each tool produced its
   per-location answer under four recipes. Non-detects were valued at half
   the detection limit throughout.

| Recipe | Plugin call | ChemDuck call |
|---|---|---|
| **Most recent, averaged duplicates** (both tools' default) | `aggregate(how=most_recent)` | `aggregate_results(..., 'most_recent', 'avg', 'half_dl')` |
| Most recent, maximum of duplicates | same | `'most_recent', 'max'` |
| **Maximum, averaged duplicates** | `aggregate(how=maximum)` | `'maximum', 'avg'` |
| Maximum, maximum of duplicates | same | `'maximum', 'max'` |

4. ChemDuck answers per **depth interval**; the plugin answers per
   **location**. ChemDuck's answers were rolled up to one per location by the
   recipe's own rule before comparing: the latest date for most recent, or
   detections first and then the highest value for maximum. 9,265 of the
   28,187 cells (33%) are at locations with more than one depth interval.
5. A cell **agrees** when the value (to 1 part in 10⁹), the detect flag and
   the date all match. Each differing cell was classified by looking at the
   raw rows on both tools' deciding dates.

## Results

### How many location cells agree

| Recipe | Agree | Differ | Differ in value | Differ in detect flag only | Differ in date only |
|---|---|---|---|---|---|
| **Most recent / avg** (default) | 20,205 (72%) | 7,982 | 7,982 | 0 | 0 |
| Most recent / max | 23,074 (82%) | 5,113 | 5,109 | 4 | 0 |
| **Maximum / avg** | 15,959 (57%) | 12,228 | 9,851 | 0 | 2,377 |
| Maximum / max | 25,722 (91%) | 2,465 | 24 | 0 | 2,441 |

With the defaults both tools ship, **28% of location cells show a different
value** for the same analyte at the same location.

### How far apart

For the cells that differ in value and where both values are positive, the
ratio between the two tools' answers (larger over smaller):

| Recipe | Cells | Median | 90th percentile | 99th percentile | Largest | Over 2× | Over 10× |
|---|---|---|---|---|---|---|---|
| Most recent / avg (default) | 7,965 | 1.28× | 3.0× | 424× | 102,401× | 1,402 | 303 |
| Maximum / avg | 9,845 | 1.35× | 2.2× | 4.7× | 31× | 1,318 | 14 |

Most differences are modest, but the default recipe has a long tail: 303
location cells differ by more than tenfold.

(These come from a second run of those two recipes, which counted 7,984 and
12,227 differing cells against the first run's 7,982 and 12,228. The few
cells that move between runs are most likely ties in this script's own
roll-up across depth intervals, which have no stated order; they aren't
attributed to either tool.)

### Why they differ

Counts are differing cells, by the first cause that explains each.

| Cause | Most recent / avg | Most recent / max | Maximum / avg | Maximum / max |
|---|---|---|---|---|
| A. Several **samples** on one date at one depth, combined | 5,474 | 3,356 | 7,902 | 545 |
| A′. …with detects and non-detects combined | 664 | 424 | 1,286 | 46 |
| B. One sample, several **methods or tests**, combined | 856 | 569 | 774 | 10 |
| B′. …with detects and non-detects combined | 276 | 83 | 285 | 0 |
| C. A **depth profile**: one date, several depths | 468 | 481 | — | — |
| D. A non-detect only ChemDuck can value | 244 | 200 | 125 | 23 |
| E. Equal values on different dates, tie broken differently | — | — | 1,856 | 1,841 |

### What agrees

- **The inputs.** On every one of the 1,836,971 detected results, the lab
  value (`RESULT_NUMERIC`, `RESULT_UNIT`) equals the reported one
  (`REPORT_RESULT_VALUE`, `REPORT_RESULT_UNIT`). Where both tools find a
  number for a non-detect, it's the same number. The report's Non-Detect
  Multiplier was 1.0 on all 1,563,324 non-detects that carry a limit, so
  neither tool halves an already-halved value here.
- **The rule that a non-detect never wins a maximum over a detection.** Both
  enforce it at the event stage. With duplicates collapsed by maximum, the
  two tools disagree on the maximum value in only 24 of 28,187 cells; every
  other difference in that recipe is the date shown.
- **Fractions.** Both keep dissolved and total apart.

## The causes, and which answer is right

### A. Several samples on one date — what is an "event"?

This is most of the disagreement. At Facility A, 400,287 water groups
(location, fraction, analyte, date, depth) hold results from more than one
sample: 299,932 of them samples taken at different times of day, 93,724 at
the same timestamp. Sediment has 44,758 such groups and soil 1,409.

- **The plugin** treats each sample as an event. Its `sample_date` keeps the
  time of day, so "most recent" is the latest sample. Where several samples
  share a timestamp it keeps the first row, which is arbitrary but
  repeatable.
- **ChemDuck** treats each date as an event: its `sample_date` is a date,
  with the time kept separately as an attribute. All of a day's samples at
  one depth are first collapsed by `dup_agg` (Chemrooms' default: `avg`)
  into one value.

Example: pH at water station W1 on 2017-08-30. The plugin maps 8.41, the last
sample that day; ChemDuck maps 8.245, the mean of the day's samples. (A mean
of pH values is also a questionable number in its own right, since pH is a
logarithm.)

**A′ is the case that breaks a rule both projects have written down.** When
the day's samples include detects and non-detects, ChemDuck averages the
detected values with substituted half-limits and labels the average
"detected" (`bool_or(detected)`). Iron (dissolved) at water station W2 on 2024-07-15:
the plugin maps 2.03, detected, from the latest sample. ChemDuck maps 6.10,
"detected", the mean of 12 samples, some of which were non-detects valued at
half their limit. That number is partly a statement about laboratory
sensitivity, which is exactly what "a non-detect is not a small measurement"
forbids. ChemDuck's remediation plan names this (§2) and hasn't fixed it yet.

### B. One sample, several methods or tests

ChemDuck's duplicate collapse groups by date, so two methods on one sample
are averaged like duplicates (1,132 cells at the default). The plugin keeps
them as separate results and, for "most recent", takes the first row, which
is an arbitrary choice between methods. Nickel (total) in tailings at
station T1: two methods on one sample disagree about detection. The plugin
happened to take the non-detect and maps 0.25 at its limit; ChemDuck
averages the methods and maps 2.95, "detected". The sample *had* a
detection, so the plugin's map understates it, and ChemDuck's number isn't
any method's result. **Both are weak.** The plugin's rule
is that two methods on one sample are real data, not duplicates; ChemDuck's
remediation §2 (Layer 0) says selecting across methods is fine but averaging
across them isn't. Neither tool yet states which method a "most recent" value
came from.

### C. Depth profiles — the plugin's fault

111 of 512 water locations are sampled at several depths, up to 105 of them,
and 2,475 (location, date) pairs in water span several depths. ChemDuck keeps
each depth interval as its own measurement point. The plugin merges them, so
its "most recent" on a profile's latest date is whichever depth's row comes
first. Conductivity at water station W3 on 2026-01-27: the plugin maps 80.9 from one of
105 depths; ChemDuck reports each depth, and its latest-date roll-up gives
76.8. It can flip the detect flag too: dissolved iron at water station W4 on
2026-01-07 is a non-detect (2.5) at the depth the plugin happened to take,
and a detection of 26 at another. **ChemDuck is right to keep depths apart; the plugin needs a stated
rule** — a depth chosen, per-interval features, or the maximum over depth,
said on the layer — instead of an arbitrary row.

### D. Non-detects only ChemDuck can value

51,844 water field-parameter non-detects (fraction `N`: Color, Chlorophyll a,
settleable solids, turbidity, salinity, oxygen) carry no
`REPORT_RESULT_VALUE` and no method detection limit, only a quantitation
limit, with `REPORT_RESULT_TEXT` = "< ". ChemDuck's limit chain
(`COALESCE(reporting, method detection, quantitation)`) reaches the
quantitation limit. The plugin's doesn't: it takes the first limit *column*
present in the source (`REPORT_METHOD_DETECTION_LIMIT`), so these rows count
but can never win. ChemDuck's answer is better here, but the two chains
still differ in order, which remediation §5 says should become one chain,
defined once, preferring `REPORT_RESULT_LIMIT`.

### E. Ties

When two dates hold the same maximum value, ChemDuck reports the later date
and the plugin the first row. The value agrees; the date shown doesn't
(about 1,850 cells). Harmless to the number, but each tool should state its
rule, and they should share one.

### Also found

- **Mixed units.** 38 (analyte, matrix, fraction) combinations hold more than
  one unit; ChemDuck counts 60 measurement identities with more than one. The
  plugin refuses to map a mixed-unit selection. ChemDuck computes the
  aggregate anyway and sets `units_conflict`, which the caller must check.
- **Dates in the future.** 293 results at 8 locations are dated after
  2026-10-22, up to 2027-07-06. Both tools would map them as the most recent.
  This is a data problem to fix upstream, but neither tool flags it.
- **Sample types and reportability.** Every row in this export is a normal
  sample (`N`) and marked reportable, so neither tool's handling of QC samples
  or `REPORTABLE_RESULT` was exercised. Another facility's export could
  differ.

## Decisions for both projects

These are the rules this measurement says must be **one rule across the
projects**, each with a recommendation:

1. **An event is a sample, not a day.** Same-day samples are distinct events.
   "Most recent" is the latest sample by timestamp, with a stated tie-break
   for identical timestamps. Combining a day's samples, if wanted at all, is
   an explicit recipe, never the default.
2. **Never average a detect with a substituted non-detect and call it
   detected.** If combining is allowed, a combination containing any
   non-detect must say so: not detected, or a third state. Selecting one real
   result (maximum, most recent) is always safe; averaging isn't.
3. **Methods are selected across, never averaged across** (ChemDuck §2,
   Layer 0), and the selected value carries its method.
4. **Depth intervals are distinct measurement points.** A per-location map
   value from a profile needs a stated rule; the plugin has to adopt one.
5. **One limit chain** for a non-detect, defined once and applied per row:
   `REPORT_RESULT_LIMIT`, then the individual limit columns in a documented
   order including the quantitation limit. Record the report's Non-Detect
   Multiplier, and refuse to halve a value it has already halved.
6. **One tie-break**: the later date wins.
7. **Mixed units: refuse or flag?** The plugin refuses; ChemDuck flags.
   Pick one. Refusing is the plugin's rule 4, and the safer default.

Once these are agreed, the natural home for them is ChemDuck's SQL, with the
plugin calling it. This script then becomes a gate with a fixture, the way
`test_arcgis_parity.py` holds the plugin's exporter, and both projects'
existing rules tests gain the cases above.

## Reproducing

```
python tools/measure_chemduck_parity.py \
    --manifest <facility>/manifest.json --chemduck ../chemduck \
    --out <dir> [--db <dir>/chemduck.duckdb] [--analytes 40] \
    [--recipes most_recent/avg,maximum/avg]
```

It writes `<dir>/chemduck_parity.json` with the tallies, the worst analytes
per recipe, value-ratio percentiles and three example cells per cause. It is
read-only towards both repositories. Building ChemDuck's database takes
about 3 minutes and 3 GB of disk; reuse it with `--db`. The four recipes over
40 analytes take about 16 minutes.
