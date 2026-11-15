# Making ChemDuck fast enough for a click

ChemDuck answers the plugin's interactive questions 3-10x slower than the
plugin's own `ReportStore`. This is the plan for closing that gap before
phases 2-4 of [chemduck-adoption-plan.md](chemduck-adoption-plan.md) put
ChemDuck's macros on a click. It is a plan: nothing here has been built.

**Scope.** "Map interactivity" here means the clicks that ask ChemDuck a
question: choosing an analyte (the map), the footer count, a location's
series, the sparklines. Panning and zooming never query ChemDuck. Their cost
is drawing, ~15.5 Âµs a point (`tools/mosaic_poc/README.md`), and the same
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

**Swept again 2026-11-09, quiet machine.** Same command and machine, an
Intel Core Ultra 7 265U: a hybrid laptop part with 12 cores and 14
threads, only 2 of the cores performance cores. It was on mains power,
Balanced plan. Before the sweep, CIM load was 2-12% (one 49% sample) and
no other process was busy. The load sampled during the runs (median
34-92%) includes the sweep itself, so it doesn't measure competition.
The machine's stability shows instead in the two halves agreeing: each
pair at one thread count is within about 25%, where the first sweep
drifted 2x. Order 4, 2, 6, 14, 14, 6, 2, 4. Warm medians, ranges over
the three cases, plugin / ChemDuck:

| Threads (both runs) | Map | Footer, `point_counts` | One series | Sparklines | Pivot, TAILS |
| --- | --- | --- | --- | --- | --- |
| 2 | 0.03-0.07 / 0.26-0.47 s | 0.01-0.02 / 0.06-0.11 s | 0.01-0.03 / 0.16-0.28 s | 0.07-0.18 / 0.58-1.61 s | 0.31-0.33 / 0.62-0.64 s |
| 4 | 0.03-0.05 / 0.38-0.69 s | 0.01-0.02 / 0.07-0.11 s | 0.01-0.02 / 0.28-0.38 s | 0.05-0.15 / 0.93-2.55 s | 0.29-0.30 / 0.89-0.94 s |
| 6 | 0.04-0.07 / 0.60-1.17 s | 0.01-0.02 / 0.10-0.17 s | 0.02 / 0.41-0.60 s | 0.06-0.17 / 1.19-3.10 s | 0.34-0.38 / 1.05-1.23 s |
| 14 | 0.06-0.10 / 1.43-2.56 s | 0.02-0.03 / 0.19-0.23 s | 0.02-0.03 / 1.01-1.33 s | 0.07-0.17 / 2.25-4.83 s | 0.55-0.58 / 1.72-2.00 s |

Each call's time divided by its time at 4 threads in the same half,
median (range):

| Threads | Plugin | ChemDuck |
| --- | --- | --- |
| 2 | 1.17 (0.89-1.81) | **0.78** (0.50-2.00) |
| 6 | 1.23 (0.88-1.50) | 1.60 (1.00-2.23) |
| 14 | 1.59 (0.94-2.41) | 3.36 (1.00-4.57) |

- **On this machine 2 threads is best for ChemDuck,** about 1.3x faster
  than 4 at the median. It is the only count where every ChemDuck map is
  inside 0.5 s. The store is a little slower at 2 (median 1.17x), but
  that is 0.03-0.07 s maps, so far inside its budgets that it doesn't
  matter.
- **Some calls are over budget at any thread count.** At 2 threads, the
  water series take 0.22-0.28 s against 0.2 s, and the water sparklines
  1.25-1.61 s against 1 s. The sediment series and sparklines are inside.
  Section 4 has to fix these; threads can't.
- **A likely reason 2 wins here (a hypothesis, untested):** two
  performance cores. At 4 or more threads the work spills onto efficiency
  cores, and each of ChemDuck's ~30 pipeline breaks waits for its slowest
  thread (ChemDuck's `docs/duckdb-thread-cap.md`). So the best count is
  probably the machine's performance-core count, not a constant. Check on
  a second machine, ideally a desktop, before hard-coding 2.
- **Slower than 2026-11-05 because these sweeps used a different copy of
  Facility A** (found 2026-11-10). There are two copies on this machine:
  - **09-25:** `equis_qgis/data/fundao`, 3,452,146 rows. Both sweeps
    above used this one.
  - **10-23:** `CF5209/pipeline`, 3,515,468 rows. The 2026-11-05
    timings used this one, as their raw files show.

  The 10-23 export codes most water `MWS`, which the 09-25 copy holds
  under `WS`. So the same click is a much bigger question on the older
  copy:

  | Case | 09-25 copy | 10-23 copy |
  | --- | --- | --- |
  | Arsenic WS/T | 62,967 results at 219 locations | 11,937 at 170 (plus 51,030 under `MWS`) |
  | Iron WS/D | 72,132 at 397 | 17,507 at 294 |
  | Arsenic SE/N | 23,096 at 443 | 18,367 at 368 |

  On the 10-23 copy today, at 4 threads, the calls are back in
  2026-11-05's range. Two things were ruled out:
  - **The machine.** Maps took 0.30-0.48 s and sparklines 0.45-1.41 s,
    against 0.23-0.55 s and 0.37-0.87 s across 2026-11-05's three runs.
  - **The sidecars.** That day's sidecars and freshly published ones
    are the same files, apart from a few bytes in `analyte_summary` and
    `load_report`. Over 4 and 3 alternated runs, the fresh ones were a
    median 1.11x the old (0.91-1.91), with the same row counts on every
    call.

  2 threads beat 4 on this copy too, in one run: maps 0.23-0.39 s
  against 0.30-0.48 s.

  **Which copy section 4 should profile is a choice to make.** The 10-23
  copy is the current export. On the 09-25 copy, with water pooled, the
  water sparklines and series stay over budget at any thread count. That
  copy is the better stress case for a facility monitored that heavily,
  or for a map that pools `WS` and `MWS`.

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

**Profiled 2026-11-14.** The 10-23 copy (`CF5209/pipeline`), 2 threads,
plain Python 3.14.6, DuckDB 1.5.5, ChemDuck `e7139bd`. Arsenic MWS/T was
added to the three cases, because on this copy it is the pooled-water
question the 09-25 copy asks under `WS`: 51,030 results at 49 locations.
The machine was at 87-98% load from other work all day, and every time
here is about 3x a quiet day's, the store's too (a map takes 0.11 s
against 0.03 s). So each change below was measured as an A/B against the
code before it. Two runs of each were alternated, CPU was recorded beside
wall time, and row counts were compared on every call.

Where a call's time goes:

- **Planning is a fixed cost of about 0.25 s per call under this load.**
  Binding the macro takes 0.11 s and the optimizers 0.12-0.14 s, the same
  for a 248-row series and a 51,030-row one. That is about 40% of a
  one-location series. A prepared statement doesn't save it: `PREPARE`
  once and `EXECUTE` per call took the same time as a fresh call (series
  0.640 against 0.636 s, map 0.706 against 0.725 s, identical answers).
  DuckDB re-plans each `EXECUTE` with its values. So section 3's per-click
  table is still the only way for the plugin to pay this once.
- **The parquet scan reads every partition.** All 19 files are read, 7.0M
  rows touched, for any matrix and fraction. The matrix filter is on the
  samples sidecar, and `p_fraction` meets a `CASE` mapping T/D to
  Total/Dissolved that DuckDB can't invert, so `matrix=`/`fraction=`
  prunes nothing. The store reads the same files with
  `hive_partitioning` and opens one partition. The scan costs 0.07-0.14 s
  of CPU per call: smaller than planning, but on every call.
- **In series mode the `resolved` stage computed the over-time statistic
  for every row.** No most-recent, maximum or series call reads it.
- **What DuckDB's profile attributes can mislead.** It charged 1.15 s of
  a sparkline call to `ident`'s `GROUP BY`. Run alone on the same 51,030
  real rows, that `GROUP BY` takes 21 ms. The time was the `withpol` CTE
  it reads, materialised: 0.6 s, half of it the `results` view's per-row
  expressions. Cumulative timing, cutting the macro after each stage, is
  the way to localise a cost here.
- **A harness pitfall.** `--chemduck-src` needs its own `--pub`, because
  the published bundle carries the SQL. One A/B that shared a `--pub`
  compared identical code, and its runs differed by 0.7-0.8x on every
  call: the load drift on this machine.

Changed on ChemDuck branch `series-speed` (`1440b79`; local, not pushed), each
A/B against the code before it, with identical rows on every call:

| Change | Calls it moves | New / old, wall (CPU) |
| --- | --- | --- |
| `resolved` computes `s`, `any_detected`, `all_detected`, `km_lim` only for `p_event_agg := 'statistic'`; `ident`'s `list(DISTINCT .. ORDER BY ..)` and `string_agg(DISTINCT .. ORDER BY ..)` become a plain list made distinct and sorted, NULLs kept as before | sparklines | 0.86-0.95 (0.84-0.87) |
| `point_scope` filters `p_locations` with `list_contains`, not `IN (SELECT unnest(..))` | one location's series | 0.64-0.86 (0.64-0.75) |

- **With `list_contains`, the location reaches the parquet scan** through
  DuckDB's dynamic join filter. A one-location series' scan emits 248
  rows, not 12,067, and its operators' CPU fell from 0.278 to 0.179 s.
  The scan still touches 7.0M rows, and planning is unchanged.
- **Every other call is within noise of the code before.**
- **The answers did not move.** `1440b79` vendored temporarily:
  - `test_chemduck_vendor` and the parity fixture: green.
  - `test_chemduck_parity.py` on Facility A (12 analytes, 7,083 s at
    this load): PASS. Its tallies match the `e7139bd` run, count for
    count:
    - most_recent: 9,618 agree, 8,914 differ.
    - maximum: 11,949 agree, 6,583 differ.
    - Both: 18,532 cells, 3,854 points.
  - ChemDuck's own `tests/run_all.py`: all five suites pass.
- **Still over budget on Facility A:**
  - Arsenic MWS/T sparklines: 3.2-3.6 s under this load, against the
    store's 0.4-0.5 s.
  - Every map and footer at this load, though they were inside budget on
    quieter days.

  A quiet-machine run is owed before calling any of this inside or over.

**What is left for section 4, in order of expected gain:**
1. **Partition pruning in the consumer** (ChemDuck). Expose the
   partition's matrix and raw fraction code, and let `point_scope`
   filter on them so `hive_partitioning` can skip files. Every call
   gains, and the store already shows the size of it.
2. **The `withpol` stage's per-row expressions** (ChemDuck). The
   `results` view computes every column of every row, 0.3 s for 51,030
   rows. Columns a stage doesn't read could be left uncomputed.
3. **Planning** (ChemDuck). The plan has 232-236 operators; the optimizer
   cost is mostly the expression rewriter and join ordering. The six
   `chemduck_settings` subqueries in `point_scope`'s scope are the next
   candidates, as `matrix_combine_class` already found for
   `matrix_classes`.

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
maps ChemDuck's points, and then depth decides it. Section 2 was swept
twice. On this laptop 2 threads beats 4 for ChemDuck and costs the store
nothing that matters. Before decision 8 is taken, a second machine is
owed. *2026-11-10:* the apparent slowdown since 2026-11-05 was a different
copy of Facility A (see section 2), not a regression. Section 4 is next,
starting with the water series and sparklines on the pooled-water copy,
where no thread count brings them inside budget. *2026-11-14:* section 4
profiled, on a loaded machine. Two ChemDuck changes are measured on a
local branch: one location's series 0.64-0.86x, sparklines 0.84-0.87x
CPU. Section 4 lists what is left: partition pruning first, then the
`results` view's per-row work, then planning. Section 3 also matters
more now, because planning can't be prepared away.

## What this needs

- **Facility A's parquet and sidecars**, or a copy of the manifest folder,
  for sections 2, 4 and 5. The fixture shows only the floor.
- **Access to the ChemDuck repository** for section 4's requests and fixes.
- **QGIS 4.2.3** for the `python-qgis.bat` runs and section 5.
