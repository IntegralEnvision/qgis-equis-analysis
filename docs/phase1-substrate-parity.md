# Phase 1: the report in DuckDB, with the judgement intact

R2 of [vision-linked-views.md](vision-linked-views.md): **DuckDB replaces
`ReportTable`'s storage, not its knowledge.** Most of `report_data.py` is
domain judgement rather than data structure, and the entire risk of this move
is that a rule is quietly dropped — a non-detect that stops being one, a tie
that breaks the other way, a filter on a missing column that starts excluding
every row. None of those look like bugs. They look like slightly different
numbers.

| Piece | What it is |
|---|---|
| `equis_loader/store.py` | The rows in DuckDB, with `report_data`'s rules as a SQL view. No QGIS imports; the database path comes from the caller, as `cache.py` does. |
| `tools/test_store_parity.py` | Asks both implementations everything and requires equal answers. The gate. |

```
python tools/test_store_parity.py
```

## Parity

Every question, over every filter combination, on three reports — one with
every optional column, one with none of them, and one with no rows:

- **3,456 aggregate combinations** (analyte × most-recent/maximum × three
  non-detect conventions × matrix × fraction × method × detected-only)
- **270 series combinations**, plus `series_many` against looping `series()`
- analytes, matrices, mixed fractions, methods, fractions-per-analyte,
  locations, each under six matrix filters

All equal.

## The parity test was checked for teeth

Passing first time over that much semantic surface is a reason for suspicion,
not comfort. Eight rules were broken on purpose in `store.py` to see whether
the suite noticed:

| Mutation | |
|---|---|
| non-detect plotted at the full limit instead of half | caught |
| tie-break reversed | caught |
| filter on a missing column excludes everything | caught |
| `at_limit` never set | caught |
| undated rows allowed to win most-recent | **survived at first** |
| code comparison made case-sensitive | caught |
| CAS lookup falling back to the name | caught |
| exclude-non-detects keeping the limit | caught |

Re-run 2026-09-23 after `rows_for` and `filtered_rows` were added: still
**8 of 8**, and every mutation now fails *more* checks than before — the
non-detect and `at_limit` breaks went from 5 failing checks to 7, the CAS
break from 5 to 6. The new methods read the same rules through different
paths, so the gate got deeper rather than merely wider.

The survivor was a gap in the **fixture**, not the assertions: no location
mixed dated and undated rows for one analyte, so `NULLS LAST` and
`NULLS FIRST` gave the same answer and the rule "a dated result always wins
over one we cannot place in time" was never exercised. A location with both
was added, and the mutation is now caught. **8 of 8.**

## Speed, at report scale

460,000 rows, warm:

| | `ReportTable` | `ReportStore` | |
|---|---|---|---|
| build (index / ingest) | 2.89 s, +33 MB | 5.27 s | comparable, and the ingest also parses |
| `analytes()` | 133.3 ms | 9.4 ms | **14x faster** |
| `aggregate(most recent)` | 474.3 ms | 28.6 ms | **17x faster** |
| `aggregate(maximum)` | 482.9 ms | 25.9 ms | **19x faster** |
| `series()`, one location | 0.1 ms | 5.0 ms | **36x slower** |
| 300 × `series()` | 26.9 ms | 1,443.6 ms | **54x slower** |
| 300 via `series_many()` | 26.9 ms | 52.2 ms | 1.6x slower |

The index build time confirms the README's own claim — "a few seconds and
~35 MB" — from an independent measurement.

## Two findings that changed the design

**1. `executemany` is unusable at this scale.** The Python API binds one row
at a time: **42 s for 50,000 rows, which is 6.5 minutes at report scale**
against the 2.9 s index it replaces. Writing the parsed rows to a CSV and
`COPY`ing them in does the same work in **~1.1 s** — 350x faster, and faster
than the index. The intermediate is *our* format, ISO timestamps and repr'd
floats, so nothing ambiguous is re-parsed; the report's eight date formats and
`<0.5` numbers were already resolved in Python before it was written.

**2. A database charges per query, and that changes the API's shape.**
`series()` is 36x slower than a dict lookup because a few milliseconds of
parse-plan-execute is nothing beside a large scan and everything beside a tiny
one. Looping it over 300 locations — which is what drawing sparklines does —
cost 1.44 s against 27 ms: a **54x regression on the exact case the row index
was built for**. The same work as one grouped scan is 52 ms.

So the store imposes a rule on its callers: **ask once for many, never many
times for one.** `series_many()` is that, and `series()` stays for the
click-a-point case where a human just clicked and 5 ms is invisible. This is
the mirror image of R9 — there the lesson was to spend queries freely, and it
still holds, but *freely* means large queries, not numerous ones.

## What is parsed in Python, and why

Dates and numbers are parsed at ingest with `report_data`'s own `parse_date`
and `parse_number` rather than by SQL casts. The report carries eight date
formats and numbers written `<0.5`; re-expressing those in SQL would be a
second implementation of a rule that already exists — and the one kind of
divergence a parity test cannot catch, because it would be comparing two
things that were never the same. The query semantics move to SQL; the parsing
stays where it was proven.

## `rows_for`, `filtered_rows`, and the raw columns

Added 2026-09-23, because the panel calls both and neither existed. Between
them they were the only thing standing between the store and a complete
swap.

**`rows_for`** drives the unaggregated "every result" layer and the depth
profiles. Its row cap is the interesting part: it drops *whole locations*,
greedily, most history first, and a location that would overflow the cap is
**skipped so a smaller one after it can still fit**. That is sequential, not
a running-sum prefix, so expressing it as a window function would either
change the answer or become unreadable. It is applied in Python over one row
per location — thousands at most — after a query costing milliseconds. The
parity test covers seven different caps.

**`filtered_rows`** is the export, and it returns the *report's* columns in
the report's own order — not the canonical 18 the store reasons over. So the
store can keep a second `raw` table, holding the original cells positionally.
Measured at 460,000 rows x 31 columns:

| | ingest | on disk |
|---|---|---|
| canonical only | 7.35 s | 9.5 MB |
| with raw columns | 14.73 s | 63.5 MB |

**Re-measured on real cached reports, 2026-09-23, and the first run was 9x
slower than this.** 222,571 rows x 22 columns took 32.1 s, of which 31.9 s
was `_csv_row` and only 0.6 s was DuckDB's `COPY`. The cause was in
`parse_date`, not the store: Analytical Results II emits `4/24/2025 12:37:00
PM`, a 12-hour form absent from `DATE_FORMATS`, so every row raised 8
exceptions, fell back to splitting the string, and raised 8 more. Adding the
format and remembering the last one that worked took ingest to 6.7 s for the
same report, which is ~13.8 s extrapolated to 460,000 rows.

Two lessons worth keeping. The benchmark above was taken on synthetic rows
whose dates happened to match the first format tried, so it measured the best
case and read as the typical one. And the bug was not only slow: the time of
day was being discarded, collapsing samples twelve hours apart onto the same
instant.

**It is off by default.** Doubling the load for a feature used occasionally
is the wrong trade when the load is the one moment a user waits — a cached
report reopens in under a second today. `filtered_rows` raises a clear error
saying so rather than silently returning the wrong columns. Where an export
should get its raw columns from is 1b.5 in the implementation plan; re-reading
the cached report on demand is the obvious answer.

## Still to do

- Point the panel's reads at the store and delete `_build_row_index`. The
  parity gate is what makes that safe, and nothing should come out until a
  caller has been switched over and the offline tests still pass.
- `tools/test_parsing.py` is untouched so far; it exercises `ReportTable`
  directly and will need the same dual-implementation treatment where its
  assertions are about analysis rather than parsing.
