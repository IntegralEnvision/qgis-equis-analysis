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

- [ ] Count first: features on the layer for the same click, store vs
      ChemDuck. ChemDuck returns a point per method class (Arsenic WS/T at
      Facility A: `UNK` and `lab`), so a map can carry more points than the
      store's. If so, the fix is choosing the class (adoption plan, phase 2),
      not speed.
- [ ] Then time a pan on each layer with `QgsMapRendererParallelJob`, as
      `tools/test_roll.py` does. Equal point counts should give equal pans;
      if they don't, the layer's fields (every report column since 8e9f319)
      are the next suspect.

## Order

2 first: it is small and already measured. Then 5's count, because it
decides whether the pan is this plan's problem at all. Then 4 on Facility A,
because it holds four fifths of the time. 3 last: it saves the floor, about
0.06 s a click, and costs an invalidation rule.

## What this needs

- **Facility A's parquet and sidecars**, or a copy of the manifest folder,
  for sections 2, 4 and 5. The fixture shows only the floor.
- **Access to the ChemDuck repository** for section 4's requests and fixes.
- **QGIS 4.2.3** for the `python-qgis.bat` runs and section 5.
