# Making ChemDuck fast enough for a click

ChemDuck answers the plugin's interactive questions 3-10x slower than the
plugin's own `ReportStore`. This is the plan for closing that gap before
phases 2-4 of [chemduck-adoption-plan.md](chemduck-adoption-plan.md) put
ChemDuck's macros on a click. It is a plan: nothing here has been built.

**Scope.** "Map interactivity" here means the clicks that ask ChemDuck a
question: choosing an analyte (the map), the footer count, a location's
series, the sparklines. Panning and zooming never query ChemDuck. Their cost
is drawing, ~15.5 µs a point (`tools/mosaic_poc/README.md`), and the same
whichever source answered. If a pan is what felt slow, that is section 5,
not sections 2-4.

## 1. Where it stands

**Facility A, 2026-11-05** (adoption plan, open item 2; plain Python 3.14.6,
DuckDB 1.5.5, ChemDuck `daddfb6`, 4 threads, a busy laptop at 0-75% load):

| Click | Budget | Store | ChemDuck | Ratio |
| --- | --- | --- | --- | --- |
| Map, one analyte | 0.5 s | 0.03-0.04 s | 0.26-0.35 s | 7-10x |
| Footer, `point_counts` | 0.2 s | 0.02-0.03 s | 0.08-0.09 s | 3-5x |
| One location's series | 0.2 s | 0.02-0.03 s | 0.21-0.22 s | **over budget** |
| Sparklines | 1 s | 0.05-0.07 s | 0.44-0.62 s | 7-10x |

- **At DuckDB's default threads (14) maps took 1.26-1.65 s, over budget.**
  The 4-thread cap is what brings them in, and it is not yet in the code
  (decision 8, "not yet taken").
- **ChemDuck's CPU exceeds its wall time:** 0.58-0.86 s of CPU for a
  0.26-0.35 s map.

**The parity fixture, 2026-11-06** (`tools/profile_chemduck.py --explain`,
22 Arsenic WG/T rows; Linux cloud container, 4 cores, Python 3.13.18, DuckDB
1.5.5, ChemDuck `e7139bd`). Warm medians of three:

| Click | Store | ChemDuck, 1 thread | ChemDuck, 4 threads |
| --- | --- | --- | --- |
| Map, most recent | 0.007 s | 0.063 s | 0.068 s |
| Footer | 0.004 s | 0.017 s | 0.020 s |
| One location's series | 0.005 s | 0.061 s | 0.059 s |
| Sparklines | 0.005 s | 0.066 s | 0.062 s |

**There is a floor of about 0.06 s a call that has nothing to do with the
data.** With 22 rows ChemDuck is the same ~10x behind as on Facility A. The
DuckDB profile of one map on one thread, 77 ms in all:

| Part | Time |
| --- | --- |
| Binding the macro (`planner_binding`) | 16 ms |
| Optimisers (`all_optimizers`) | 16 ms |
| Operators: 237 of them, 10 hash group-bys, 13 hash joins, 111 projections | 13 ms CPU |
| Not attributed by the profile (pipeline setup, result fetch; unmeasured) | ~30 ms |

More threads don't shorten it; they add CPU (13 ms on one thread, 23 ms on
four, same latency). On a Facility A map (0.26-0.35 s) that floor is about a
fifth. **The other four fifths scale with the data**, and the fixture can't
show them. The adoption plan's 2026-10-29 profile put them in a handful of
`GROUP BY`s over at most 63,000 rows, before ChemDuck PRs #12 and #13 cut
them by 1.3-2.4x. Where they sit now has not been profiled.

## 2. Take the thread cap (plugin, small)

Decision 8 recommends `ReportStore` set `threads = min(4, default)` where it
opens the session's connection, and nothing else set it. Measured 2026-10-29:
the store's calls were 1.06-3.64x faster capped, none slower by more than
0.022 s; ChemDuck's were 2-5x faster.

- [ ] Re-measure on an idle machine, which the decision is waiting on: 2, 4
      and 6 threads, `measure_chemduck_calls.py --threads`, both
      implementations, every call reported separately.
- [ ] Set it in one place, with the measurement in the comment.
- [ ] Background builds keep every core (their own databases), stated in
      the same comment.

Acceptance: no store call slower by more than its noise, every ChemDuck call
inside its budget at the chosen value, on an idle machine.

**Swept 2026-11-09, NOT on an idle machine, so decision 8 is still open.**
`measure_chemduck_calls.py --threads N --skip-load`, Facility A, plain
Python 3.14.6, DuckDB 1.5.5, ChemDuck `e7139bd`, 14 cores. Sidecars
republished first (18.6 s). Runs at 4, then 2, 6, 14, 4, 14, 6, 2 threads.
CPU load was sampled every 5 s by CIM. The user's other jobs held a median
60-68% for the first runs and 82-86% for the later ones, with spikes to
100%. The first 4-thread run wasn't sampled. Warm medians, ranges over the
three cases, plugin / ChemDuck:

| Threads, run (load median) | Map | Footer, `point_counts` | One series | Sparklines |
| --- | --- | --- | --- | --- |
| 2 (68%) | 0.04-0.08 / 0.41-0.60 s | 0.02-0.03 / 0.10-0.14 s | 0.02-0.03 / 0.23-0.33 s | 0.11-0.23 / 0.77-2.14 s |
| 4 (not sampled) | 0.04-0.06 / 0.53-0.80 s | 0.01-0.03 / 0.10-0.14 s | 0.02-0.03 / 0.34-0.42 s | 0.09-0.22 / 1.21-3.08 s |
| 6 (60%) | 0.05-0.07 / 0.59-1.72 s | 0.02-0.07 / 0.15-0.34 s | 0.02-0.07 / 0.60-0.99 s | 0.13-0.27 / 1.57-4.42 s |
| 14 (62%) | 0.07-0.19 / 2.65-3.71 s | 0.02-0.03 / 0.26-0.34 s | 0.03 / 1.30-1.56 s | 0.11-0.31 / 5.16-6.85 s |
| 2 (82%) | 0.10-0.19 / 0.70-1.32 s | 0.03-0.16 / 0.21-0.54 s | 0.03-0.20 / 0.43-1.15 s | 0.28-0.46 / 1.74-3.70 s |
| 4 (84%) | 0.06-0.16 / 0.52-1.75 s | 0.02-0.05 / 0.15-0.18 s | 0.03-0.07 / 0.43-0.81 s | 0.18-0.40 / 1.71-5.64 s |
| 6 (86%) | 0.08-0.17 / 1.40-2.46 s | 0.02-0.05 / 0.22-0.71 s | 0.04-0.06 / 1.10-1.26 s | 0.17-0.36 / 3.87-6.54 s |
| 14 (86%) | 0.13-0.34 / 3.30-6.61 s | 0.03-0.14 / 0.37-0.84 s | 0.04-0.06 / 2.10-2.87 s | 0.28-0.50 / 6.31-12.43 s |

- **On a busy machine, fewer threads is faster for both sides.** Within
  each half of the sweep, ChemDuck's calls get slower at every step from 2
  or 4 threads to 14: 4-6x on a map at the lighter load. The store is
  fastest at 2-4 as well.
- **Load moves ChemDuck more than the store.** From the first 2-thread run
  to the second, the store's maps went 0.04-0.08 -> 0.10-0.19 s and
  ChemDuck's 0.41-0.60 -> 0.70-1.32 s.
- **Under this load ChemDuck is over budget at every thread count:** maps
  over 0.5 s, one series over 0.2 s, and WS sparklines 2-3 s against 1 s.
  The store is inside every budget except sparklines at 14 threads under
  86% load (0.50 s, still within 1 s). These calls are 1.5-4x slower
  than 2026-11-05's at 4 threads (maps 0.26-0.35 s, sparklines
  0.44-0.62 s). The pin differs only in the open (PR #14), so load is the
  likely cause, but that wasn't isolated.
- **Still to do:** the same sweep with nothing else running. The question
  it has to answer is now 2 against 4. Both are clearly better than 14,
  and 6 isn't better than 4 in either half.

## 3. Pay the fixed cost once per analyte, not once per question (plugin)

The floor is per statement, so the repo's own rule applies: ask once for
many. A map click, its footer and its sparklines are three ChemDuck calls
over the same (analyte, matrix, fraction, scope), each paying ~32 ms of
binding and optimising and repeating the same resolution of events into
points.

- [ ] Measure first: how much of `point_counts` and `point_series` is the
      same work `aggregate_points` already did. If ChemDuck exposes the
      stage that resolves events (or can), the adapter materialises it once
      per click into a temp table in `chemduck`, and the footer, series and
      sparklines read that table.
- [ ] One authority for invalidation: the table is keyed by the full row
      scope (`_apply_row_scope`) and the call's parameters, and dropped when
      either changes. A stale table answering a new scope is the plausible-
      number failure CLAUDE.md warns about, so the parity fixture gets a
      case that changes scope between two calls.
- [ ] No rule re-implemented in the adapter. If the footer or series can't
      be read off ChemDuck's own output without restating a rule, this step
      stops and goes upstream (section 4).
- [ ] Remember the pending-result trap: a worker's cursor making the table
      means the reader runs a no-op `execute()` first (CLAUDE.md,
      "A pending result hides other connections' tables").

Acceptance: footer + one series + sparklines after a map cost less than the
three separately, by an amount stated; `test_chemduck_parity.py` green,
including the new scope-change case.

## 4. Profile the data-scaled part on Facility A (upstream, ChemDuck)

Most of a real map's time scales with rows, and only Facility A (or a
facility of its size) shows where. The fixes belong in ChemDuck's macros,
not in the vendored copy, which is never edited (`VENDORED.json`).

- [ ] `profile_chemduck.py --manifest <Facility A> --explain` at the chosen
      thread count, under plain Python and `python-qgis.bat`. Rank operators
      by time per call, map, series and sparklines separately.
- [ ] For each of the top operators, name the macro stage it belongs to and
      whether it is reading rows the click doesn't need (another analyte, a
      matrix outside the call) or recomputing what the open could compute
      once (a sidecar).
- [ ] Write it up as a ChemDuck request in the same form as
      `docs/qgis-plugin-request-interactive-speed.md`: numbers, the stage,
      the proposed change, the budget it buys.
- [ ] One location's series first: it is the one call still over budget.
- [ ] Re-vendor what lands (`vendor_chemduck.py`), re-run the parity gate
      and the profile, record both pins' numbers side by side, alternated,
      as 2026-11-05 did.

Acceptance: every call inside budget at the chosen thread count on Facility
A, in QGIS's Python, with the parity gate green.

## 5. If it was the pan

None of the above moves a pan. A pan redraws the layer's points; the source
that answered the click doesn't enter into it, unless ChemDuck's map gives
the layer more points than the store's would.

- [x] Count first: features on the layer for the same click, store vs
      ChemDuck.
- [x] Then time a pan on each layer with `QgsMapRendererParallelJob`, as
      `tools/test_roll.py` does.

**Measured 2026-11-09**, Facility A, same machine and load as section 2's
sweep, 4 threads. **ChemDuck's map has 3.5-8.6x the store's features, and
depth is the whole reason:**

| Case | Store (one per location) | ChemDuck (one per point) | Points per location, median / max |
| --- | --- | --- | --- |
| Arsenic WS/T | 219 | 1,882 | 2 / 104 |
| Iron WS/D | 397 | 2,059 | 1 / 104 |
| Arsenic SE/N | 443 | 1,565 | 1 / 16 |

- **Depth splits them.** At 111 water locations, and 77 sediment ones,
  the results sit at several depths, up to 104 at one water location.
  ChemDuck keeps each depth as its own point (shared rule D1). Taking
  depth out of the key gives exactly the store's counts.
- **Method class splits nothing here,** contrary to this plan's first
  guess. Every location's points have one class, `lab` or `UNK`. The
  adoption plan's two variables for Arsenic WS/T are two sets of
  locations, not two points at one location.

The pan was timed with a throwaway script, not one committed. It builds
two layers from ChemDuck's own points, so both have the same coordinates
and only the count differs: every point, and the highest point per
location. Each was styled by `style_by_result` and rendered by
`QgsMapRendererParallelJob` at 1400x900, EPSG:3857, over the points'
extent. QGIS 4.2.3, two runs, load about 80%. Rows with no value or no
coordinates were left off, so the per-location counts are lower than the
store's.

| Case | Every point | Per location |
| --- | --- | --- |
| Arsenic WS/T | 1,882 features: 43-83 ms | 219: 19-29 ms |
| Iron WS/D | 2,004: 59-67 ms | 342: 29-30 ms |
| Arsenic SE/N | 1,390: 45-54 ms | 268: 20-23 ms |

- **A pan stays inside 100 ms either way at these counts.** It is 2-3x
  slower per point, which is roughly the feature ratio less a fixed cost
  per render.
- **The pan isn't a problem today.** The panel still maps the store's
  answer: ChemDuck is a shadow in phase 1.
- **It becomes one in phase 2** if ChemDuck's points are mapped as they
  are. Depth multiplies features 3.5-8.6x, so the 25,000 cap is reached
  at an eighth of the locations. Sparkline markers (about 52x a circle,
  one per feature) would turn 219 into 1,882. So how a location with
  several depths is drawn (adoption plan, decision 2) decides pan cost
  more than anything in sections 2-4.

## Order

2 first: it is small and already measured. Then 5's count, because it
decides whether the pan is this plan's problem at all. Then 4 on Facility A,
because it holds four fifths of the time. 3 last: it saves the floor, about
0.06 s a click, and costs an invalidation rule.

*2026-11-09:* 5 is done. The pan is not this plan's problem until phase 2
maps ChemDuck's points, and then depth decides it. Section 2 was swept but
needs an idle machine to settle 2 against 4 threads. Section 4 is next.

## What this needs

- **Facility A's parquet and sidecars**, or a copy of the manifest folder,
  for sections 2, 4 and 5. The fixture shows only the floor.
- **Access to the ChemDuck repository** for section 4's requests and fixes.
- **QGIS 4.2.3** for the `python-qgis.bat` runs and section 5.
