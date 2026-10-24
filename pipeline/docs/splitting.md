# Splitting a facility into slices

Measurements are from the first facility exported this way (3.45M result
rows, ~900 locations, build 7_26_1_26015, September 2026).
Re-measure on a new tenant before trusting a number that matters.

## Two stages, and only the second is a search

**Cells, `(matrix, fraction)`, are chosen, not found.** Both are saved
report parameters (`@matrix_codes`, `@fractions`), both become Hive
partition keys a reader prunes on, and dissolved/total must never be
aggregated together anyway. The survey only has to say which exist.
Matrix is on `DT_SAMPLE` and fraction on `DT_TEST`, OData cannot join, so
the candidate list is a cross product; the calibration probe finds the
empty cells and `create_slice_reports.py` refuses them.

**Locations within a cell are the search.** Location is the one axis that
subdivides almost without limit and is still a saved parameter
(`@sys_loc_codes`, pipe-separated, an explicit list). Date windows were
the first attempt and were abandoned: they were cut through `where=`,
which report runs ignore.

## Packing is easy; weights are hard

Bins are filled largest-first into the lightest bin (LPT). Location sizes
span 2,539x (median 23 samples, half the data in 50 of 900 locations), so
alphabetical ranges balance badly (1.20x at 8 bins) where LPT gives 1.00x.

The weight should be **result rows**, because generation time tracks rows
(~3,600-4,700 rows/s across a 12x size range; location count predicts
almost nothing - 225 and 275 locations of one report returned identical
rows). But result rows per location cannot be read directly:
`DT_RESULT` has no location column and OData cannot join.

| Weight | Correlation | Cost | 8-bin spread |
|---|---|---|---|
| samples (`DT_SAMPLE`) | r = 0.831 | 2.3 min | 1.16x |
| tests | r = 0.880 | 20 min | 1.22x |
| results, walked | exact | ~225 min | 1.00x |
| results, measured from last night | exact | free | 1.00x |

The sample proxy is **biased, not noisy**: results-per-sample runs 1.8 to
18.0 and the heaviest locations have the lowest ratios, so balancing on
samples spreads 1.05x at 4 bins but 2.60x at 32.

So: **first plan on samples, then re-bin on measured rows** - by hand
since 2026-10-23, after a load large enough to move the bins, rather than
every night.
The exact walk (`nightly_slices.py --weight walk`) attributes every result
to its cell in one pass; use it only to seed a facility that must be right
before its first extraction, and make its filters mirror the slices'
saved filters (`@sample_types`; `@cas_rns` is always cleared) or it overstates - an
unfiltered walk counted 701,780 rows where the report returned 355,440.

## The probe

Sample counts are per matrix, so they cannot see how much of a matrix a
fraction carries: a same-sized bin returned 18,214 rows in WS/N and 164,463
in SE/N. One throwaway report of the first bin (`ZZ_cellprobe_*`, deleted
in a `finally`, only if its name still starts `ZZ_`) replaces that guess
with the cell's real rows-per-location and re-sizes the bin count. The same
run's units propose the cell's entry in `units.json`.

## Size

`DEFAULT_TARGET_ROWS = 92,000`. Medians: 4,415 rows 21 s, 92,234 rows 57 s,
245,262 rows 122 s - on a quiet weekend. Cold runs take up to 2.7x warm
(220.8 s then 99.4 s for one slice), and a nightly first run is always
cold. A slice sized to *just* fit fails on some nights and not others,
which is the worst outcome. Raise the target only with weekday numbers.

A 400-location cap exists only to stop a slice of near-empty locations
carrying hundreds of codes; rows are the bound that matters.

## Re-binning

`nightly_slices.py --rewrite`, run by hand, per cell. Each slice whose list
changed is **armed**, and its cell is not rebuilt until every armed slice
has run: a cell built from all old lists or all new ones covers each
location once, but one built half from each can double or drop a location.

- **Skipped** when the cell has no measured weights yet - re-binning on
  all-zero weights would collapse it into one or two slices that then time
  out.
- **Left alone** when the slice count is unchanged, nothing failed, balance
  is under 1.25x and nothing is over target. LPT reshuffles equal-weight
  locations, so re-binning "because the bins differ" would rewrite (and
  re-run) every report on a shared tenant.
- **Split** when a slice stored nothing. A failure has no rows to re-weight
  from, and weighting it at zero keeps it broken; more, smaller slices is
  the only lever that reliably helps.
- A new slice is recorded with `needs_report`; its cell's partition is not
  refreshed until `create_slice_reports.py --pending` gives it a report.
