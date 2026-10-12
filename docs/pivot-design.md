# Pivoted result sets — the general issues

Written 2026-10-08. The scatter (US-24) is the first view that needs more than
one variable per row, and it will not be the last: scatter-plot matrices,
parallel coordinates, ratios, congener sums, correlation and PCA all read the
same shape. This document settles that shape once, so each new view is a
client of one pivoted table rather than its own pivot with its own rules.

Every number below comes from `tools/measure_pivot.py` run on the Facility A
parquet (3,452,146 rows, 10 partitions, as published 2026-10-05), on the
development machine, 2026-10-08. Re-run it before trusting a number that
matters; it takes about 12 s.

## The shape

The store holds **long** data: one row per result. A pivot turns it **wide**:

| term | meaning |
|---|---|
| **row** | one field sample (`SYS_SAMPLE_CODE`) |
| **variable** | one measurable quantity: analyte + fraction, within one matrix, in one unit |
| **cell** | (row, variable): what the pivot must reduce to ONE value |

A pivot is only honest if every cell holds one result, or the rule that
reduced it to one is stated. Every way a cell can hold more than one result is
a decision the pivot must make — or a number it silently makes up. Most of
this document is those ways, counted.

The pivot itself is cheap: the whole of WS/T, 64,934 samples × 31 variables,
pivots in **0.19 s** with DuckDB's `PIVOT`. Cost is not the problem.
Correctness is.

## What the data does

### 1. Most cells are clean; some matrices are not

| matrix | cells | holding >1 result | % |
|---|---|---|---|
| WS | 2,579,386 | 28,465 | 1.1 |
| SE | 790,519 | 14,687 | 1.9 |
| **TAILS** | 17,593 | **10,025** | **57.0** |
| SO | 10,847 | 0 | 0 |
| all | 3,398,345 | 53,177 | 1.6 |

A 1.6% average hides a matrix where most cells are ambiguous. Any rule chosen
here gets exercised hard on TAILS and rarely elsewhere, so test it on TAILS.

### 2. Why a cell holds several results

Of the 53,177 multi-result cells (categories overlap):

| what differs between the rows | cells |
|---|---|
| analytic method | **52,259** |
| column number (`1C`/`2C`/`NA`; travels with method) | 41,855 |
| detect flag — the rows disagree about detection | 1,984 |
| unit | 168 |
| test type (`REPLICATE…`) | 96 |
| dilution | 0 |
| **none of the above** | 822 |

The 822 differ in lab sample ID and analysis date, every one of them: the same
field sample, split at the laboratory and analysed twice. 744 of those report
different values.

Inside detected-only multi-result cells, the values disagree by **≥2× in
7,156 cells and ≥10× in 1,375**, worst 25,500×. Picking "the first row" is
not a neutral default; it is choosing a number at random from a spread that
can be four orders of magnitude.

**EQuIS's own `REPORTABLE_RESULT` cannot break the tie here.** Every row in
the store is `YES`, including both rows of every multi-result cell. It may
work on other facilities. On this one it decides nothing, so the design
cannot rely on it.

### 3. A sample is not a location, or a date

Samples never span matrices (0 of 108,135). But a (location, date) often
holds several normal samples — depth intervals in sediment, sequential grabs
in water:

| matrix | (location, date) pairs | with >1 sample | worst |
|---|---|---|---|
| WS | 72,721 | 2,876 | 12 |
| SE | 13,086 | **3,594 (27%)** | 60 |
| SO | 112 | 109 | 9 |
| TAILS | 107 | 28 | 16 |

So the row must be the **sample**. Pivoting on (location, date) would put
different depth intervals in one cell. Pivoting on location — "the maximum
arsenic and the maximum lead at each station" — produces a point that
**never existed**: the two maxima can come from different samples, years
apart. Co-occurrence is the reason to plot two analytes together, and a
location-level pivot fabricates it.

### 4. Variables are clean at Facility A; units are not quite

- One name per CAS and one CAS per name, with no CAS-less analytes. That's
  the benefit of a curated compilation. Do not assume it elsewhere.
- **38 of 1,382** (analyte, matrix, fraction) variables carry more than one
  unit. The map and the chart already refuse to aggregate those; the pivot must
  refuse to fill a column from them, or convert by EQuIS's factors upstream
  (as the parquet pipeline does).
- Different variables MAY have different units. A scatter of µg/L against
  mg/kg is legitimate; a SUM across them is not.

### 5. Non-detects dominate pairs

The two most-measured variables in WS/T are Manganese and Lead. Of the 63,222
samples carrying both:

| | samples | % |
|---|---|---|
| both detected | 1,573 | 2.5 |
| Mn detected, Pb not | 19,770 | 31.3 |
| Pb detected, Mn not | 58 | 0.1 |
| **neither** | **41,821** | **66.1** |

A scatter of these is **97.5% limit values**. The non-detect rule that
matters in a histogram decides the whole picture in a scatter:
- Drawn at ½ DL as if measured, two thirds of the points become a grid of
  laboratory sensitivities.
- Drawn not at all, the chart shows 2.5% of the data and calls it the
  relationship.

### 6. Complete cases collapse as variables are added

Normal samples carrying ALL of the top-k most-measured variables:

| | k=1 | 2 | 3 | 5 | 8 | 12 |
|---|---|---|---|---|---|---|
| WS/T (64,934 samples) | 63,952 | 63,222 | 63,031 | 62,170 | 53,103 | **378** |
| WS/D (76,856) | 72,132 | 63,471 | 62,360 | **18,430** | 18,429 | 11,029 |
| SE/N (27,617) | 23,406 | 23,404 | 23,401 | 23,377 | 23,041 | 16,842 |

The collapse isn't gradual. It falls off a cliff when one variable belonging
to a different analytical suite enters the set. A multivariate view that
quietly drops incomplete rows shows a different population for every choice
of variables, and never says so.

### 7. What this facility does NOT exercise

- **QC samples.** The parquet export was cut with `@sample_types=N`, so every
  sample here is normal and none has a parent. The live path can carry field
  duplicates and blanks when the QC filter is off.
- **Rejected results.** No row carries an `R` qualifier or an approval code.
  A validated dataset will.
- **Dilution reruns.** Zero cells differ by dilution.

Each is a rule the pivot must have, and this facility cannot test. The parity
fixture needs synthetic rows for them, as it did for the row scope.

## The decisions

### D1. Rows are samples, and normal ones by default

- A row is a `SYS_SAMPLE_CODE`, carrying its location, date, depth interval,
  matrix and sample type as ordinary columns. Those columns become predicates
  in the shared selection: the depth slider of US-24 criterion 7 is just
  another clause.
- **QC samples are excluded from pivot rows** (decided 2026-10-08). A field
  duplicate is not an independent sample: it would double-weight its
  parent's location in every correlation. A later option may fold a
  duplicate into its parent through `PARENT_SAMPLE_CODE` (for example by
  averaging the pair), but that is a separate, stated rule. It is never a
  peer row.
- **Scatters are always per sample** (decision 6). No location-aggregated
  multivariate view is built. Per-location summaries stay what they are on
  the map: one variable at a time.

### D2. A variable is (analyte, fraction, matrix-unit group)

- Fraction is always part of the identity: dissolved and total never share a
  column (CLAUDE.md).
- Matrix: rows are already single-matrix. Matrices pivot together only when
  the unit guard passes, as on the map (WS+WG yes, SE+WS no).
- Unit: a variable whose rows carry two units is refused, with the units and
  counts named, as the chart does today.
- **Method and lab do NOT split a variable by default** (decided
  2026-10-08). Arsenic-T is one column whatever method produced it, and a
  cell holding results from two methods resolves by D3. That is what a
  chemist expects to see first.
- **Splitting by method (or by lab) is an option**, for when the methods
  disagree and that disagreement is the question. Then Arsenic-T measured
  two ways offers two columns, `Arsenic T [6020]` and `Arsenic T [200.8]`.
  Two methods are real data (CLAUDE.md), and 52,259 cells here carry two.
  The default resolves them by a rule it states and counts. It never hides
  that they existed.

### D3. A cell resolves by a stated rule, and the count is reported

Within one method, cells can still hold several results: lab splits (822)
and replicates (96). The rule, in order:
1. Prefer `TEST_TYPE = INITIAL` over replicates.
2. Prefer a detected result over a non-detect. This matches the store's
   `maximum` rule, which exists because a limit is not a measurement.
3. Then the **higher** value. Conservative, and the same ordering the
   store's `maximum` aggregate uses, so a pivot cell and a mapped maximum
   cannot pick different rows for the same sample.

Never average: a split pair disagreeing 25,500× has no meaningful mean. Never
`first()`: row order is not a rule. **Every column carries the count of cells
it resolved**, and the view states it ("Arsenic: 412 cells had two results;
kept the detected, higher one").

**This is the default, and it applies across methods** (decided
2026-10-08), since method does not split a variable unless asked (D2).

### D4. Every analyte is four columns: `_value`, `_detect`, `_qualifier`, `_detectlimit`

Decided 2026-10-08. For each variable the pivot carries:

| column | holds | from |
|---|---|---|
| `<analyte>_value` | the result: the measured value, or for a non-detect the limit, per the store's fallback chain | `RESULT_NUMERIC`, then `REPORT_RESULT_VALUE`, then the limit columns |
| `<analyte>_detect` | `Y` / `N` exactly as the database records it; NULL when it was left blank | `DETECT_FLAG` |
| `<analyte>_qualifier` | the qualifier as stored, untouched | `INTERPRETED_QUALIFIERS`, falling back to `LAB_QUALIFIERS` when empty (decided 2026-10-08) |
| `<analyte>_detectlimit` | the limit reported with the result | the store's own limit column, in its order (`REPORT_METHOD_DETECTION_LIMIT`, `METHOD_DETECTION_LIMIT`, `REPORTING_DETECTION_LIMIT`, ...; `rd.COLUMNS["limit"]`), so the pivot and the map read the same limit |

The number of results each cell resolved from goes in the column dictionary
as a per-variable count, not as a fifth column.

**Detection comes from `_detect`, never from the qualifier.** Qualifiers are
a lab's or validator's vocabulary. Many labs write `U` for a non-detect, but
an unstandardised load can carry `<`, and at Facility A 35,662 non-detect rows
have no qualifier at all. Inferring detection from qualifier text would
hard-code one loader's conventions into the plugin. A standardised database
has a populated `DETECT_FLAG`, and the pivot relies on it:

- **A source with no detect column is refused**, by name, with the reason. It
  is not guessed at. The plugin as a whole now does the same; see "The
  detect flag was assumed" below.
- **A row whose flag is blank** gets `_detect` NULL. It's counted, stated,
  and left out of the four detection symbols, because "unknown" is not one
  of them. Facility A has none: 1,836,971 `Y` and 1,615,175 `N`, zero blank.

#### The detect flag was assumed (fixed 2026-10-08)

Found while writing D4, 2026-10-08. `ReportTable.is_detected` and the store's
view both count a result as detected only when `DETECT_FLAG` is
`Y`/`YES`/`TRUE`/`1`. A report **without that column**, or a row with it
blank, is therefore a non-detect. Every measured value then becomes "a
limit", and under the default ½ DL convention it is **halved**. Reproduced
on both implementations with a two-row report, Arsenic 10 and 40 µg/L, no
`DETECT_FLAG`: the map would show 5 and 20, marked as limits, with no warning.
The parity gate cannot catch it, because both implementations agree.

Analytical Results II always carries the flag, so no source in use was
affected. **Fixed 2026-10-08**: `DETECT_FLAG` joined
`report_data.REQUIRED_FOR_ANALYSIS`, the one list both implementations read.
A report without it is refused in the panel, through the existing
`missing_for_analysis` gate. A parquet source is refused by
`ReportStore.from_parquet`, which never sees a ReportTable. Both give the
same message, from `missing_message`, saying why detection is not guessed.
The scope is EQuIS and EQuIS-derived extractions, which carry the flag; a
source without it is fixed upstream. Covered by `test_parsing.py`,
`test_store_parity.py` (both implementations agree on what is missing) and
`test_parquet_source.py`. Removing the flag from the list fails all three
(3, 1 and 1 checks).

The rule stops at the COLUMN. A row whose flag is blank in a source that
has the column is still read as a non-detect by the map; the pivot gives it
`_detect` NULL instead. Facility A has no blank flags.

A single numeric column cannot carry a non-detect honestly, and every
downstream view needs to tell the four detection classes apart:
- **Scatter:** four symbols, one per detection class (decision 5): both
  detected; A detected, B not; B detected, A not; neither. Non-detects sit at
  their limits, and stacking is expected. Nothing is hidden by default.
- **Log axes:** non-detects have a real positive limit, so they can be drawn;
  zeros and negatives cannot, and are counted and stated, never dropped.
- **Statistics** over the pivot (correlation, PCA) need censored-data
  methods, or must state that they ran on detected pairs only and how many
  that is. A Pearson r over substituted ½ DL values is a number about
  laboratory sensitivity.

### D5. Three kinds of absence, kept apart

| absence | cell holds | means |
|---|---|---|
| not analysed | NULL | no information |
| non-detect | limit, `detected = false` | "below this" — information |
| rejected | NULL, flagged | the lab measured it; the validator said do not use it |

These must not collapse into one "missing". Rejected is NULL for value but
not for meaning, and must be countable.

### D6. Complete-case vs pairwise is chosen, and visible

- Each view states the n it draws, per panel.
- A scatter-plot matrix uses **pairwise** completeness, so each panel shows
  its own n.
- A method that needs complete rows (PCA, parallel coordinates) states how
  many rows survived out of how many, and which variable cost the most.

The table in §6 should be something the panel can compute live: it's a
`GROUP BY sample HAVING count(DISTINCT variable) = k`, and costs
milliseconds.

### D7. Derived variables are SQL over the pivot, named with their rule

Sums (total PCBs, total PAHs) and ratios (As/Fe) are columns computed from
the pivot. Each one needs a non-detect rule, and **the rule is part of the
name** shown on the axis ("ΣPAH16, ND = 0"). A sum of congeners where all are
non-detect is itself a non-detect at the summed limit; it isn't zero.
Different units refuse, as with any sum.

### D8. Rows are samples, map features are locations

Brushing samples highlights every location holding at least one brushed
sample. Selecting locations on the map filters pivot rows by `loc`. That's
the existing US-6 machinery, unchanged. The status line gives **both
counts** ("118 samples at 23 locations"), because one location can hold 60
samples. Dim-not-remove applies in both directions, as now.

## Engineering

- **A new name per build**, never a replaced object. The histogram already
  showed why: Mosaic caches query results keyed on the SQL string, and
  replacing the view `h` under byte-identical SQL served one analyte's bars
  under another's name (00afe8d, 59328f7). A new name makes stale cache
  entries unreachable, and they can't be wrong.
- **Wide by map, read by view, charted from a pair table** (2026-10-10,
  measured on this laptop). Every analyte in one matrix is the shape the
  Analysis chooser needs, and each way of building it was timed on
  tailings (644 variables over 28,242 rows):
  - one `max(...) FILTER (WHERE var = k)` per cell, 2,576 of them: past
    five minutes and 18 GB without finishing;
  - DuckDB's own `PIVOT` with four aggregates: 72-93 s. With ONE it is
    fast - 0.2 s for 628 columns, 1.2 s for 2,512 - and the plans are the
    same (PIVOT > HASH_GROUP_BY). The cost is several aggregates times many
    values, superlinear in both (synthetic, 628 values x 1-4 aggregates:
    0.4 / 3.7 / 8.6 / 23.5 s), and it is not where the profiler looks: 92.7
    s of wall clock against 4.0 s of operator CPU and 0.02 s planning, 2.8
    GB peak against 0.57 GB (2026-10-11). Four single-aggregate pivots
    joined would likely work; the map is simpler and faster still;
  - one map per sample (cell key -> winning row), read out by key: 0.26 s,
    identical answers;
  - the same, with the maps built in a subquery of the read-out: 89 s.
    The optimiser works over every reference to `m`, so the maps get a
    statement of their own (`pm_` table).

  The pivot itself (`p_`) is a **view** over the maps. Materialising every
  cell cost 3.6 s of water's 7.2 s, for views that read two analytes at a
  time. A chart, though, asks of its two columns many times, and through
  the view each reference repeats a lookup: water's Mn x Pb draw took 3.8 s
  with 1.2 s in its first query. So a chosen pair is copied into a small
  `pp_` table first (`pivot.pair_table`), and the same draw took 1.1 s.
  The pair counts behind Y's list read the map entries directly: 0.2 s
  against 1.4 s through the view, with identical counts.
- **Column names follow D4** (`<analyte>_value`, `_detect`, `_qualifier`,
  `_detectlimit`), with the analyte part made a safe identifier: lower-case,
  non-alphanumerics to `_`, and the fraction appended when the pivot spans
  more than one (`arsenic_t_value`, `arsenic_d_value`). Names collide
  (isomers and long names can slug alike), so a dictionary table maps each
  column back to (analyte, CAS, fraction, unit, method/lab split if any,
  resolution rule, resolved-cell count). The page reads labels from the
  dictionary, never from the column name.
- **The row scope applies first.** Dates and QC are already a `WHERE` on the
  store's single view (`_apply_row_scope`), so a pivot built from that view
  inherits them, with no second implementation.
- **Store-only.** The pivot exists only in DuckDB; `ReportTable` gets no
  equivalent. The linked views already require the store, and a second
  implementation of a wide reshape would double the parity surface for no
  user. The parity gate still matters for the *inputs*: the cell rule (D3)
  must pick the same row the store's `aggregate` picks. That's a test
  comparing the two, written like `test_chart_map_agree.py`.
- **Drawing is the cost, not the query** (CLAUDE.md, R9). A 6-variable
  scatter-plot matrix is 15 panels; at 60,000 samples that's 900,000 marks.
  Past roughly 25,000 points per panel, draw a density raster, not dots
  (Phase 3.2).

## Decisions

Taken 2026-10-08:
1. **The cell rule (D3) is the default**, applied across methods.
2. **Method and lab do not split a variable by default.** Splitting is an
   option (D2).
3. **Four columns per analyte**: `_value`, `_detect`, `_qualifier`,
   `_detectlimit` (D4). Detection comes from the database's `DETECT_FLAG`,
   never inferred from qualifier text. A source without the flag is refused.
4. **QC samples are excluded** from pivots. Folding a duplicate into its
   parent may come later as an explicit rule (D1).
5. **A scatter draws four detection classes as four symbols**: both
   detected; A detected and B not; B detected and A not; neither. Each
   non-detect sits at its limit. Stacking is accepted and understood: at
   Mn×Pb, 41,821 both-non-detect samples land on 19 limit pairs, 80% of them
   on (10, 10), and the symbol says what they are. Decided 2026-10-08.
6. **Scatters are always per sample.** No location-aggregated scatter is
   offered: per-location maxima come from different samples at 174 of 216
   multi-sample WS/T stations (81%), so a location point would plot a
   combination no sample had (NF_2N: max-Mn sample Mn 2,432 / Pb 44, max-Pb
   sample Mn 896 / Pb 640, location point 2,432 / 640). Decided 2026-10-08.

No pivot decisions remain open. `_qualifier` reads interpreted
qualifiers, falling back to lab qualifiers (D4).

## Status

**Built 2026-10-08: the data layer.** `equis_loader/pivot.py`, QGIS-free,
reading the store's one view. Rules D1–D4 as decided, the counts each view
must state, a new table name per build, and refusals by name.
`tools/test_pivot.py` covers it with a hand-worked fixture: 33 checks,
including the cell rule agreeing with the store's mapped maximum. Each rule
was broken on purpose and caught (6 mutants). On Facility A, the Mn×Pb WS/T
pivot builds in 0.8 s and its detection classes match `measure_pivot.py`
exactly.

**Built 2026-10-08: the scatter (first increment).** `web/src/scatter.js`
holds the decisions: SQL, four classes, log or linear axes, the caption
stating everything not drawn. `web/src/scatterplot.js` is the vgplot wiring:
every paired sample faint as context, the map's selection and the brush
solid on top, and a brush that sends locations to the map. The panel's
`open_scatter(x, y, fraction, matrix)` opens it from the console. A map
selection redraws it dimmed, without rebuilding the pivot, so the axes stay
put. `tools/test_scatter_sql.py` runs the page's real SQL against real pivots
and reproduces the measured Mn×Pb classes exactly. **Not yet seen in a live
QGIS**: `tools/try_scatter.py` is the console probe that will show it.

**A smaller default would not help water** (measured 2026-10-11). Metals
only (69 columns, half the rows) built in 3.4 s against 3.1 s for all 215
analytes; SE went 2.4 s to 1.5 s, TAILS 0.7 s to 0.4 s. So a default
analyte group buys little; a worker buys it all.

**Where water's build goes, and what an index would not fix** (2026-10-11).
Not finding the rows: the results are a view over parquet split by matrix,
and filtering the whole facility (3,452,146 rows) to water takes 13 ms.
The view's CASTs do not get in the way - they are casts to the type each
column already has, DuckDB drops them, and EXPLAIN shows every filter
(matrix, chemical, location) inside the parquet scan, with or without them;
timings match within a millisecond. (An earlier note here said the reverse,
from the profiler's `cumulative_rows_scanned`, which is not rows read from
disk.) Every query has a floor of ~6 ms, met even by a filter that matches
nothing - the files' metadata, read each time; `SET parquet_metadata_cache
= true` cut it to ~2.5 ms, not adopted until its behaviour when the nightly
export rewrites a file is checked. DuckDB cannot index a view, and an index
serves selective lookups, not a filter keeping 76% of the rows. The time is per matched row: copying the 2.6 M matched rows with
every column (0.6-1.0 s), ranking them for the cell rule (0.5-0.9 s), then
ranking them AGAIN and packing the maps (1.4-2.2 s). Those are the levers:
carry only the columns the pivot reads, and rank once. (Timings vary run
to run on this laptop; compare within a run.)

- *Carry only the columns read*: adopted (2f45ff4). The copy went 2.44 s
  to 1.42 s on water, 2.16 s to 1.23 s on sediment, with identical pivots.
- *Rank once*: tried and NOT adopted (2026-10-11). Keeping the winners in
  a temp table so the counts and the maps share one ranking moved the
  time rather than saving it. Medians of four alternating builds: water's
  counts + maps went 4.02 s (1.20 + 2.82) to 3.69 s (winners 2.15,
  fractions and unit 0.32, maps 1.22); sediment's went 1.68 s to 1.71 s.
  Whole builds were slower in both, within the noise. The ranking is
  cheap; writing ~1.2 M winning rows to a table costs what the second
  ranking did. The pivots were identical, so the idea was sound, just not
  faster.

**Built 2026-10-11: the build runs in a worker.** `tasks.AnalysisTask` runs
`Session.respond` in QGIS's task manager on the Session's own cursor
(`store.thread_view()`); DuckDB releases the GIL while it works, and the
main thread's worst gap through a 4.0 s water build was 19 ms
(`tools/test_analysis_task.py`, under QGIS 4.2.1). Choices made while it
runs collapse to the last. The map layer is built in `finished()`, on the
main thread. Tables the worker makes are read by the page on the main
connection, which is why `bridge_payload.answer` refreshes its snapshot
first (CLAUDE.md, "A pending result hides other connections' tables").

**Built 2026-10-10: the Analysis tab's chooser.** `equis_loader/analysis.py`
(QGIS-free) holds a `Session` per store. It pivots every analyte in a
matrix once per row scope (`on_mixed="skip"`, so one analyte in two units
does not cost the rest) and offers the page a catalogue. It counts what
each analyte shares with X, and turns an (X, Y) choice into the scatter's
options, breaks and map stations: one authority, shared with the console's
`open_scatter`. The page (`web/src/chooser.js`) holds the selection and
nothing else. `tools/test_analysis.py` covers the session against a
hand-worked fixture, and against Facility A: As x Cr in SO gives 112 pairs, the
two-analyte scatter's breaks and its 10 stations. `tools/test_pivot.py`
grew a ninth mutation (a cell read from another variable's key); all 9 are
caught.

## Ternary plots (decided 2026-10-12)

Three analytes as shares of their sum, from the same every-analyte pivot,
chosen in the Analysis tab (Chart: Ternary, then X, Y and Z). One point is
one sample; the Points modes and the time window apply as to the scatter.
Decided with the user:

- **Non-detects are plotted at their limit, grey and hollow**, with a
  switch to hide them. Their true value is at most the limit, so the true
  share is smaller and the sample lies further from that analyte's corner
  than drawn; the caption says so. It decides some plots: As + Pb + Zn in
  Facility A water is 62,288 samples, 624 detected on all three.
- **One unit or refused.** A share of ug/L beside mg/L is a share of
  nothing; units are not translated at presentation (CLAUDE.md).
- **Not a Piper diagram** (no meq/L, no ion balance) - not yet.

`equis_loader/ternary.py` decides shares, positions and colours (each
corner a ColorBrewer Dark2 hue; a point is its shares' mix; grey for any
non-detect). The page restates it in SQL (`web/src/ternary.js`), and
`tools/test_ternary.py` holds every Facility A Ca : Mg : Na sample's page
colour to ternary.py's: 6,432 placed, 6,411 detected on all three, 21 with
a non-detect, none differing. The scatter map's ternary counterpart
(`build_ternary_layer`) colours each station from the same function.

## Points per location, and the map's time window (decided 2026-10-12)

The Analysis tab's **Points** control: every sample; most recent per
location; highest X per location; highest Y per location. Each of the last
three draws ONE REAL SAMPLE per location - asked for as "aggregate (max vs
most recent)", and resolved so as not to break the per-sample rule: X's
maximum beside Y's from another sample was offered and declined. "Highest"
ranks a DETECTED value first, then the value, then the latest date, then
the sample code, so a non-detect is never a location's maximum while a
detection exists there (CLAUDE.md). `bivariate.picks_sql` is the one rule;
the scatter's solid points and the scatter map are both its output.

The scatter follows QGIS's **Temporal Controller**. Samples outside the
map's time window are dimmed, the axes and the colour breaks fixed (the
breaks stay over every sample, so a colour means the same in every
window), and most recent/highest are taken within the window. Whole days,
inclusive; a frame ending at midnight ends the day before; an undated
sample is outside any window. A burst of window changes settles (150 ms)
into one re-pick in the worker. On water, Mn x Pb most recent per location
is 219 points of 63,222 samples, and 2014 alone is 64
(`tools/test_analysis_task.py`, under QGIS 4.2.1).

Not yet: the scatter map is rebuilt as a new layer on each re-pick, so
styling a user applied to it does not survive a window move.

## The scatter's map (decided 2026-10-09)

A scatter needs its own map: the panel's result layer shows a different
variable, so highlighting stations on it said nothing about the two
analytes being brushed. Modelled on the R package biscale.

7. **A station is coloured by its most recent sample carrying both
   analytes.** One real sample, never the max of one beside the max of the
   other. The wider UI questions (where the scatter and its map live in the
   panel) are deliberately deferred.
8. **Only a both-detected sample gets a colour; the three classes involving
   a non-detect are grey**, in the scatter's symbols. A non-detect is
   "below this limit", and whether that is class 1 depends on the limit: on
   Facility A Mn×Pb WS/T, 84% of Mn non-detects have limits below Mn's first
   tercile break but only 17% of Pb's do. A plain classification would have
   called about 51,000 Pb non-detects "low" that might not be.
9. **Jenks breaks, per axis, over the both-detected samples, on log10
   values when all are positive.** Measured: Jenks on raw values gave
   classes of 1,567 / 4 / 2 (Mn) and 1,550 / 21 / 2 (Pb), the extremes
   taking two classes. On log10: 466 / 742 / 365 and 366 / 878 / 329.
   Exact Jenks is O(k n²), so inputs over 3,000 are evenly sampled, as
   QGIS's own Jenks does. Each break is returned as the data value itself,
   since a 10**log round trip moved a value sitting on a break up a class.

Palette: Joshua Stevens' 3×3 as biscale ships it under "GrPink", keys
"x-y". The exact key orientation was taken from biscale's documented
convention, not checked byte for byte against the package's data.

`equis_loader/bivariate.py` is the one authority for breaks, classes and
colours. The page colours points from its answers and
`layer_builder.build_bivariate_layer` colours the map from the same ones.
`tools/test_scatter_sql.py` holds them together: all 63,222 Facility A points
are coloured alike by both. Changing the page's `<=` at a break to `<`
breaks the test on exactly the on-break points.

## Field duplicates and replicates are not plotted (decided 2026-10-09)

10. **A field replicate is left out; its primary is plotted.** At Facility A EQuIS
    does not mark replicates. They are typed N with no parent, and the only
    signal is the code: the primary's code plus `_R2`, `_R3` and so on. There
    is no `_R1`; the un-suffixed code is replicate 1. Measured: 7,868 normal
    samples carry the suffix, in 3,915 series up to `_R30`, and every
    series' primary exists. A sample counts as a replicate only when BOTH
    hold: its code ends `_R<n>` with n >= 2, AND the code without the suffix
    is itself a sample. So a code that merely ends in `_R2` stays.
    - **Not "same place, same time".** About half of Facility A's multi-sample
      events are different samples taken together: water-column positions
      `_F`, `_M`, `_S`, and sediment sub-samples `_A` ... `_D`, `_SQ1`, `_BD`.
      Keeping one per event would discard real samples.
    - **This is a site naming convention, not EQuIS data.** The proper fix is
      upstream: type replicates and give them a `PARENT_SAMPLE_CODE`. Until
      then the rule lives in one place (`pivot._ensure_replicates`) and the
      caption says how many were left out.
    - **Effect on the test pair** (As×Cr, SO): 836 samples become 112
      primaries, with 724 replicates left out. Every station's latest date
      now holds one sample, so the map's "most recent" pick is no longer
      arbitrary. The two As values reported as a detected 0 were both
      replicates.
    - **Cost:** the replicate set is built once per store (0.26 s) and joined
      against after. Asked per pivot as a subquery it added 1.3–3.0 s.
    - **Extended the same day:** `_BD` counts as a duplicate (508 at
      Facility A; median RPD 30%, meaning unconfirmed), and `-R<n>` as a
      replicate (5). Other suffixes (`_SQ`, `_PC`, `_RD`, `_TR`, `_PT`)
      measured as separate preparations, not duplicates. They stay in as
      ordinary samples by decision: "I don't want trivia about this
      particular dataset trip us up for the big picture." The rule is one
      regex (`QC_SUFFIX`) and one SQL function (`field_qc_sql`) shared with
      `tools/survey_sample_suffixes.py`. It is a stopgap for data that does
      not type its QC, not a catalogue to grow. See
      `data/facility_a/qc_review/README.md`.

## The first increment

US-24 at its smallest:
- two variables, normal samples, one matrix;
- D2 (method does not split the variable), D3 (with counts), D4 (the four
  columns, and refusing a source without a detect flag), and the four
  detection symbols (decision 5);
- a scatter with a brush to the map and a map selection back to the scatter.

That needs the pivot builder, its dictionary, the cell-rule test against
`aggregate`, and one vgplot view. Everything after it — the matrix, parallel
coordinates, sums, statistics — reads the same table.
