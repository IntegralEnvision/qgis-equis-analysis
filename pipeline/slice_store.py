"""Shared pieces of the EQuIS -> parquet pipeline: bins, manifest, layout.

The pipeline is a nightly loop:

    plan  ->  EQuIS reports + EIAs  ->  stored output  ->  parquet
      ^                                                      |
      +--------------- re-weight from what actually ---------+
                       came back last night

This module holds the parts both ends need, so the planner and the nightly
job cannot disagree about how a bin is computed or where a file goes.

WHY BINS AND NOT RANGES
-----------------------
A slice is a SET of locations saved to the report's @sys_loc_codes, not an
alphabetical range. Location sizes at the first facility span 2,539x (median 23 samples,
mean 129, half the data in 50 of 900 locations), so contiguous ranges balance
badly: equal-count alphabetical ranges came out 1.20x apart at 8 bins, with
bins holding anywhere from 13 to 188 locations. Greedy largest-first packing
(LPT) came out 1.00x, because it places the monsters first and fills around
them. @sys_loc_codes takes an explicit pipe-separated list, so there is no
reason to accept the worse scheme.

WHY THE WEIGHT IS PLUGGABLE
---------------------------
Bins should be balanced by RESULT rows, which is what a report's generation
cost tracks. But result counts per location are not directly readable:
DT_RESULT carries no SYS_LOC_CODE (it joins through DT_TEST to DT_SAMPLE) and
OData cannot join. So the first run weights by samples - a proxy correlating
at r=0.831, good enough while bins are coarse - and every run after weights
by the row counts the previous night actually produced.

That matters because the proxy is BIASED, not merely noisy: results-per-sample
runs 1.8 to 18.0 across locations, and the heaviest locations have the lowest
ratios, so sample weighting systematically over-fills bins with them. Measured
result spread when balancing on samples: 1.05x at 4 bins, 1.16x at 8, 1.35x at
16, 2.60x at 32. Balancing on results holds 1.00-1.03x throughout.

GETTING EXACT WEIGHTS WITHOUT AN EXTRACTION
-------------------------------------------
Results CAN be attributed to locations before any report runs, by walking the
foreign keys OData does expose:

    DT_SAMPLE.SAMPLE_ID  <-  DT_TEST.SAMPLE_ID
    DT_TEST.TEST_ID      <-  DT_RESULT.TEST_ID

`count_results_by_location` does that walk. It is exact IF its filters mirror
the report's saved parameters, and expensive either way. A full run on the first
facility (WS+D, unfiltered by analyte) took **224.7 minutes** for 85 + 132 +
4,036 pages - not the ~94 minutes a single-page timing predicted, because the
per-page cost rises as the walk goes deeper. `$select` is rejected (HTTP 400),
so every page carries all columns whether wanted or not.

Ranked by accuracy per minute spent:

| Weight            | Correlation | Cost       | 8-bin spread |
|-------------------|-------------|------------|--------------|
| samples           | r = 0.831   | 2.3 min    | 1.16x        |
| tests             | r = 0.880   | 20 min     | 1.22x        |
| results (walked)  | r = 1.0     | ~225 min   | 1.00x        |
| results (measured)| r = 1.0     | free       | 1.00x        |

At 225 minutes the walk is a poor deal against simply extracting once with
sample weights and re-weighting from the output - which is free, equally exact,
and needs no filter bookkeeping. Prefer it only when a facility must be binned
correctly before its first extraction.

Note the tests proxy is barely better than samples at bin level despite the
better correlation, because results-per-test is itself bimodal (p25 3.0,
median 8.7) - scaling tests by one global constant misses by a median 53% per
location. Correlation is not the thing to optimise; bin balance is.

So the default stays: samples on the first run, measured rows thereafter, with
the walk available via --weight walk for the one case it suits.
"""

import datetime
import json
import os
import time

# A LAST-RESORT bound on how many locations go in one slice, not the main one.
#
# Rows are what cost time; locations are a poor proxy for rows. Measured on
# one slice report (18 metals, WS, fraction T), each size run twice:
#
#     locs      rows    cold     warm   rows/s
#     225     30,255   19.4 s    7.2 s   4,207
#     275     30,255   19.1 s    6.8 s   4,451     <- same rows as 225
#     325     54,020   31.5 s   11.7 s   4,635
#     375    129,052   71.5 s   27.7 s   4,664
#     450    360,902  220.8 s   99.4 s   3,629
#
# 225 and 275 return IDENTICAL rows - those 50 locations carry no T data at
# all - while 375 -> 450 adds 20% more locations and nearly triples the rows.
# So a location count says almost nothing about cost on its own. Throughput
# is the stable quantity: ~3,600-4,700 rows/s across a 12x range of sizes.
#
# Note also the 2.7x cold/warm spread (450 locations: 220.8 s then 99.4 s).
# A nightly job's first run of each slice is always cold, so budget against
# the cold column. An earlier single probe had 450 locations FAILING at
# 236.2 s; here it passed at 220.8 s. Nothing near the wall is decided by one
# run.
#
# This bound exists only to stop a pathological slice - one whose locations
# are nearly all empty - from being saved with hundreds of codes for no
# reason. DEFAULT_TARGET_ROWS is the bound that matters.
TARGET_LOCATIONS_PER_SLICE = 400

# Rows per slice. Measured on build 7_26_1_26015, 2026-09-20, five runs at
# each of three sizes:
#
#      4,415 rows   median  21.0 s
#     92,234 rows   median  57.4 s
#    245,262 rows   median 121.5 s
#
# 245k already spends half the ~230 s budget on a quiet weekend, so it is a
# best case. 92k leaves ~4x headroom, which is what absorbs a cold run or a
# busy server: a slice sized to *just* fit fails intermittently, and an agent
# that stores output on some nights and nothing on others is the worst
# outcome. Re-measure on a new tenant before raising it.
DEFAULT_TARGET_ROWS = 92000

# Slice names for a manifest with no `prefix` recorded. Every manifest this
# code creates records one; this is only the fallback for a hand-made one.
LEGACY_PREFIX = "parquet"

# Per-facility unit choices, beside the manifest. See load_units().
UNITS_NAME = "units.json"

# The PIPELINE's manifest: report ids, agent event ids, per-slice location
# lists, run history. Local bookkeeping, never published.
#
# It is not called manifest.json, and that is deliberate. The published
# manifest - the one a reader fetches over HTTP - owns that name, because it
# is the one that gets uploaded and the obvious name is the one people will
# reach for. When the two shared a name the confusion was immediate and
# repeated: every test had to back up and swap the file, and a real upload
# went out with all ten partitions and no manifest at all, because the
# published copy was sitting on disk under a name nobody thought to send.
MANIFEST_NAME = "manifest_local.json"

# What run_nightly.py needs to know about a facility. create_slice_reports.py
# writes the facility, sample types and template; a person adds the rest.
CONFIG_NAME = "pipeline.json"

# What this used to be called, so an existing project keeps working.
LEGACY_MANIFEST_NAME = "manifest.json"


# Columns every slice asks for beyond ARII's default output, through its
# @extra_fields parameter: a pipe-separated MultiSelect of TABLE.COLUMN names.
# DT_TEST.IDENTIFIER comes back as plain IDENTIFIER (read 2026-10-17 from a
# slice run asking for it), where DT_SAMPLE.CUSTOM_FIELD_1 comes back
# table-prefixed; why they differ is not known, so a reader matches the name
# as measured. Offered by the base on the first tenant, among 956 fields.
#
# DT_TEST.LAB_SDG is deliberately NOT here: the default output already
# carries LAB_SDG, and asking again appends a duplicate beside it - which is
# what the plugin found asking for depth.
EXTRA_FIELDS = ("DT_TEST.IDENTIFIER",)


def merge_extra_fields(current, wanted=EXTRA_FIELDS):
    """`current` with `wanted` appended, pipe-separated, nothing removed.

    Added to, never replaced: a template report carries columns of its own,
    and every slice must inherit them as it inherits the other filters.
    EQuIS stores an empty parameter as the literal string "None".
    """
    text = "" if current is None else str(current).strip()
    have = [] if text in ("", "None") else [
        item.strip() for item in text.split("|") if item.strip()]
    seen = {item.upper() for item in have}
    for column in wanted:
        if column.upper() not in seen:
            have.append(column)
            seen.add(column.upper())
    return "|".join(have) if have else None


def pack_bins(weights, parts, max_locations=TARGET_LOCATIONS_PER_SLICE):
    """Greedy largest-first packing. Returns [[code, ...], ...].

    Each location goes into whichever bin is currently lightest, heaviest
    first. That is the LPT heuristic; it is not optimal but lands within a
    few percent, and on this data it is 1.00x where contiguous ranges are
    1.20x.

    Locations absent from `weights` are treated as weight 0 and still placed:
    a location with no history yet must still belong to a slice, or results
    loaded for it tomorrow would appear in none of them.
    """
    if parts < 1:
        parts = 1
    bins = [[[], 0.0] for _ in range(parts)]
    for code in sorted(weights, key=lambda k: (-weights.get(k, 0), k)):
        # Lightest bin that still has room. `max_locations` is a backstop
        # rather than a real constraint (see its definition), so it only bites
        # on a slice whose locations are nearly all empty; when it does, it
        # wins over balance.
        room = [entry for entry in bins
                if len(entry[0]) < max_locations] or bins
        target = min(room, key=lambda entry: entry[1])
        target[0].append(code)
        target[1] += weights.get(code, 0)
    return [sorted(entry[0]) for entry in bins if entry[0]]


def bin_count(weights, target_rows, max_locations=TARGET_LOCATIONS_PER_SLICE,
              rows_per_unit=1.0):
    """How many bins a location set needs.

    The ROW target is the real bound - generation time tracks rows at a fairly
    stable ~3,600-4,700 rows/s, while a location count predicts almost nothing
    (225 and 275 locations of one report returned the identical 30,255 rows,
    and 375 -> 450 nearly tripled them).

    The location bound only catches the pathological case of a slice whose
    locations are nearly all empty, which would otherwise be saved with
    hundreds of codes to no purpose.

    `rows_per_unit` converts the weights into rows when they are NOT already
    rows. The first plan weights by samples, and one sample is several result
    rows - 5.36 in the extract measured here - so comparing sample counts
    against a row target sized every bin ~5x too big. Pass the ratio when
    weighting by samples; leave it 1.0 for measured or walked row counts.
    """
    total = (sum(weights.values()) or 0) * max(rows_per_unit, 0.0001)
    by_rows = int(total // max(target_rows, 1)) + 1
    by_locations = (len(weights) + max_locations - 1) // max(max_locations, 1)
    return max(1, by_rows, by_locations)


def count_results_by_location(api, facility_id, progress=None,
                              matrix=None, fraction=None, checkpoint=None,
                              cas_rns=None, sample_types=None):
    """Exact result rows per CELL and location, by walking the foreign keys.

    Returns {cell_key: {location: rows}}. Per cell because that is how slices
    are binned: one walk attributes each result to its sample's matrix and
    its test's fraction as it goes, so every cell's weights come from a
    single pass rather than one ~225-minute walk per cell.

    DT_RESULT carries no SYS_LOC_CODE, so the attribution goes
    result -> TEST_ID -> SAMPLE_ID -> SYS_LOC_CODE. OData cannot join, so the
    three tables are paged separately and joined here.

    THE FILTERS MUST MIRROR THE REPORT'S SAVED PARAMETERS. A weight is only
    useful if it counts the rows the slice will actually fetch, and a report on
    the Analytical Results II base typically saves @cas_rns and @sample_types as well as matrix and
    fraction. Counting without them overstates every location - measured on
    the first facility, an unfiltered walk of WS+D returned 701,780 results where
    the report's own output was 355,440, because its 18 saved analytes are 45%
    of all results and @sample_types=N drops the QC samples on top.

    The overstatement is NOT a constant factor (per-location ratios ran 1.00 to
    11.22), so it cannot be scaled away afterwards: locations differ in which
    analytes they carry. Pass the report's parameters here.

      matrix         -> DT_SAMPLE.MATRIX_CODE      (@matrix_codes)
      sample_types   -> DT_SAMPLE.SAMPLE_TYPE_CODE (@sample_types)
      fraction       -> DT_TEST.FRACTION           (@fractions)
      cas_rns        -> DT_RESULT.CAS_RN           (@cas_rns)

    Narrowing the earlier tables also shrinks the later walks, since only tests
    of kept samples and results of kept tests are counted.

    Slow - about 94 minutes and 6.7 GB on a 4-million-result facility - so it
    is not the default. See the module docstring for the cheaper proxies and
    what they cost in bin balance.

    `api` needs a .json(path) returning (status, parsed) and honouring
    @odata.nextLink; `progress(stage, pages, rows)` is called as it goes.
    """
    def walk(entity, order, where, on_page=None):
        path = ("/api/odata/%s?$filter=%s&$orderby=%s"
                % (entity, where, order))
        # The first path is raw and needs quoting; every nextLink after it
        # comes back ALREADY encoded, and quoting that again turns %20 into
        # %2520 and the server rejects the url. So each page says which it is.
        raw = True
        pages = 0
        stalls = 0
        while path:
            status, body = api.json(path, quote=raw)
            if status != 200 or not isinstance(body, dict):
                # An hour-long walk WILL meet a DNS hiccup or a dropped
                # connection, and dying at page 1,728 of 4,036 throws away an
                # hour. A transient failure (no status) is retried with a long
                # backoff; only an actual HTTP error, which will not fix
                # itself, gives up.
                if status is None and stalls < 5:
                    stalls += 1
                    time.sleep(min(30 * stalls, 120))
                    continue
                raise RuntimeError("%s walk failed at page %d: %s"
                                   % (entity, pages, str(body)[:200]))
            raw = False
            stalls = 0
            rows = body.get("value") or []
            pages += 1
            notify = on_page or progress
            if notify:
                notify(entity, pages, len(rows))
            for row in rows:
                yield row
            nxt = body.get("@odata.nextLink")
            # nextLink comes back absolute; api.json wants a site-relative
            # path, so strip the base it was issued against.
            if nxt and "/api/odata/" in nxt:
                path = "/api/odata/" + nxt.split("/api/odata/", 1)[1]
            else:
                path = None

    base = "FACILITY_ID eq %d" % facility_id

    # sample -> location. Only the mapping is kept, not the rows.
    def any_of(column, values):
        """An OData `or` chain for a multi-value parameter.

        EQuIS's own parameters are pipe-separated; OData needs a disjunction.
        A long chain is fine here (unlike in a report's `where=`, which hits a
        URL length limit around 40 terms) because these lists are analyte and
        type codes, not hundreds of locations.
        """
        if not values:
            return ""
        items = ([values] if isinstance(values, str)
                 else [v for v in values if v])
        if not items:
            return ""
        return " and (%s)" % " or ".join(
            "%s eq '%s'" % (column, str(v).replace("'", "''"))
            for v in items)

    sample_where = base
    if matrix:
        sample_where += " and MATRIX_CODE eq '%s'" % matrix
    sample_where += any_of("SAMPLE_TYPE_CODE", sample_types)
    sample_location = {}
    for row in walk("DT_SAMPLE", "SAMPLE_ID", sample_where):
        code = (row.get("SYS_LOC_CODE") or "").strip()
        if code and row.get("SAMPLE_ID") is not None:
            sample_location[row["SAMPLE_ID"]] = (code, row.get("MATRIX_CODE"))

    # test -> location, via the sample map. Tests whose sample was filtered
    # out are dropped here, which is what confines the count to the cell.
    test_where = base
    if fraction:
        test_where += " and FRACTION eq '%s'" % fraction
    # Keyed "<cell>\t<location>": one flat key keeps the checkpoint plain
    # JSON and the counting loop a single dict lookup.
    test_location = {}
    for row in walk("DT_TEST", "TEST_ID", test_where):
        found = sample_location.get(row.get("SAMPLE_ID"))
        if found and row.get("TEST_ID") is not None:
            code, sample_matrix = found
            test_location[row["TEST_ID"]] = "%s\t%s" % (
                cell_key(sample_matrix, row.get("FRACTION")), code)

    # result -> location, via the test map. Counted as they stream past;
    # nothing accumulates except the counter.
    #
    # Checkpointed because this leg is 4,036 of the 5,052 pages: a failure at
    # page 1,728 (which happened) otherwise throws away an hour. The resume
    # key is the last TEST_ID seen, since the walk is ordered by it.
    counts = {}
    resume = None
    if checkpoint and os.path.isfile(checkpoint):
        try:
            with open(checkpoint, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            if saved.get("facility_id") == facility_id:
                counts = {k: int(v) for k, v in (saved.get("counts")
                                                 or {}).items()}
                resume = saved.get("after_test_id")
                if resume is not None:
                    print("resuming result walk after TEST_ID %s (%s counted)"
                          % (resume, "{:,}".format(sum(counts.values()))))
        except (ValueError, OSError):
            counts, resume = {}, None

    result_where = base + any_of("CAS_RN", cas_rns)
    if resume is not None:
        result_where += " and TEST_ID gt %d" % int(resume)

    # One test's results can straddle a page boundary, so the checkpoint must
    # record a test id that is entirely COUNTED, never the one in progress.
    # `safe` lags `current` by one distinct id: everything up to and including
    # `safe` is complete, so a resume asks for TEST_ID gt safe and re-reads
    # only the partial test, whose rows were never counted.
    current_test = [None]
    safe_test = [resume]
    pending = {}
    page_marker = [0]

    def commit_pending():
        for code, count in pending.items():
            counts[code] = counts.get(code, 0) + count
        pending.clear()

    def checkpoint_progress(stage, pages, rows):
        if progress:
            progress(stage, pages, rows)
        if (checkpoint and stage == "DT_RESULT"
                and pages - page_marker[0] >= 50):
            page_marker[0] = pages
            temporary = checkpoint + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({"facility_id": facility_id, "counts": counts,
                           "after_test_id": safe_test[0]}, handle)
            if os.path.exists(checkpoint):
                os.remove(checkpoint)
            os.rename(temporary, checkpoint)

    for row in walk("DT_RESULT", "TEST_ID", result_where,
                    on_page=checkpoint_progress):
        test_id = row.get("TEST_ID")
        if test_id != current_test[0]:
            # The previous test is finished: fold it in and make it the safe
            # resume point.
            commit_pending()
            if current_test[0] is not None:
                safe_test[0] = current_test[0]
            current_test[0] = test_id
        code = test_location.get(test_id)
        if code:
            pending[code] = pending.get(code, 0) + 1
    commit_pending()
    if checkpoint and os.path.isfile(checkpoint):
        os.remove(checkpoint)
    by_cell = {}
    for key, count in counts.items():
        cell, code = key.split("\t", 1)
        by_cell.setdefault(cell, {})[code] = count
    # Locations with samples but no results still need a bin, or data loaded
    # for them tomorrow would fall outside every slice. Which cell they will
    # land in is unknowable without a test, so they join every cell of their
    # matrix at weight 0 - placed, and costing nothing.
    for code, sample_matrix in set(sample_location.values()):
        prefix = "%s/" % (sample_matrix or "ALL").upper()
        for cell, weights in by_cell.items():
            if cell.startswith(prefix):
                weights.setdefault(code, 0)
    return by_cell


def find_env_file(start=None):
    """The nearest `.env` at or above `start` (default: the working directory).

    The WORKING directory, not this file's: the pipeline is run from the
    directory holding a facility's data, and the credentials belong to
    whichever tenant that data comes from. Looking beside the script would
    pick up one tenant's key while working on another.
    """
    directory = os.path.abspath(start or os.getcwd())
    while True:
        candidate = os.path.join(directory, ".env")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def read_env_file(path):
    """{name (lower-case): value} from a `.env` of `name: value` lines.

    `name = value` works as well. The line splits at whichever separator
    comes FIRST: it used to try `:` before `=`, so `equis_site=https://...`
    split at the colon in `https:` and read as a name nobody asked for -
    the file looked as if it lacked the site (found 2026-10-23). The one
    parser for both the EQuIS credentials and run_nightly's upload secret.
    """
    values = {}
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cuts = [i for i in (line.find(":"), line.find("=")) if i > 0]
            if not cuts:
                continue
            cut = min(cuts)
            values[line[:cut].strip().lower()] = (
                line[cut + 1:].strip().strip("'\""))
    return values


def read_env():
    """(site, api_key) for EQuIS, or exit saying where it looked.

    In order: the EQUIS_SITE and EQUIS_API_KEY environment variables; the file
    named by EQUIS_ENV; the nearest `.env` at or above the working directory.
    A `.env` holds `equis_site: https://...` and `api_key: <key>` lines
    (`=` works as well as `:`).
    """
    site = os.environ.get("EQUIS_SITE")
    key = os.environ.get("EQUIS_API_KEY")
    if site and key:
        return site.rstrip("/"), key
    path = os.environ.get("EQUIS_ENV") or find_env_file()
    if path and os.path.isfile(path):
        values = read_env_file(path)
        site = values.get("equis_site") or values.get("site")
        key = values.get("api_key") or values.get("key")
        if site and key:
            return site.rstrip("/"), key
    import sys
    sys.stderr.write(
        "\nNo EQuIS credentials. Set EQUIS_SITE and EQUIS_API_KEY, or put a "
        ".env with\n\n    equis_site: https://<tenant>.equisonline.com\n"
        "    api_key: <key>\n\nin the working directory or above it%s.\n\n"
        % (" (found %s, but it lacks one of them)" % path if path else ""))
    raise SystemExit(2)


def default_prefix(facility_id):
    """The slice-name prefix for a NEW facility.

    Carries the facility id because reports are listed per user, not per
    facility: two facilities sliced with one bare prefix would both own a
    `..._WS_D_00`, and find-by-name would hand one facility's report to the
    other.
    """
    return "parquet_%d" % int(facility_id)


def slice_name(matrix, fraction, index, prefix=LEGACY_PREFIX):
    """A stable, sortable name. Used for the report, the EIA and the file."""
    return "%s_%s_%s_%02d" % (prefix, matrix or "ALL", fraction or "ALL",
                              index)


def cell_key(matrix, fraction):
    """`WS/D`: how a (matrix, fraction) cell is named in JSON and on the CLI."""
    return "%s/%s" % ((matrix or "ALL").upper(), (fraction or "ALL").upper())


def load_units(root):
    """{cell_key: unit} from `<root>/units.json`, or {} if there is none.

    WHY A FILE AND NOT A TABLE IN CODE. The unit a cell is converted to is a
    per-facility decision, and it depends on the FRACTION as much as the
    matrix: a dissolved fraction on a solid is a leachate, measured in mg/L,
    and converting it to mg/kg killed a whole slice at the first facility
    ("MISSING UNIT CONVERSION FACTOR - cannot convert 'mg/L' to 'mg/kg'").
    A table of matrix codes in Python encoded one facility's answers and
    silently converted nothing at a facility whose codes differ.

    create_slice_reports.py proposes each cell's entry from what its
    calibration probe actually returned - the cell's dominant unit - and the
    file is meant to be read and edited by a person. A cell with no entry is
    NOT converted: silence is the right default for a transformation on
    someone's data.

    Shape: {"cells": {"WS/N": {"unit": "ug/L", "source": "..."}, ...}}.
    A bare string in place of the object is accepted too. `"unit": null`
    says "deliberately none": the cell is neither converted nor dropped
    from, and no later proposal fills it in. The first facility's WS/PRT
    carries particulate both per volume and per mass, both legitimate,
    and any target would drop one of them (2026-10-23).
    """
    path = os.path.join(root, UNITS_NAME)
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    out = {}
    for key, entry in (data.get("cells") or {}).items():
        unit = entry.get("unit") if isinstance(entry, dict) else entry
        if unit:
            out[key.upper()] = unit
    return out


def propose_unit(root, matrix, fraction, unit, source):
    """Record a proposed unit for a cell that has none. Never overwrites.

    A person's edit to units.json must survive every later run, so an
    existing entry - proposed or chosen - is left exactly as it is.
    Returns True when an entry was written.
    """
    path = os.path.join(root, UNITS_NAME)
    data = {"cells": {}}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    cells = data.setdefault("cells", {})
    key = cell_key(matrix, fraction)
    if key in {k.upper() for k in cells}:
        return False
    cells[key] = {"unit": unit, "source": source}
    if not os.path.isdir(root):
        os.makedirs(root)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
    if os.path.exists(path):
        os.remove(path)
    os.rename(temporary, path)
    return True


def partition_path(root, matrix, fraction):
    """Hive-style directory for one (matrix, fraction) cell.

    Partitioned on matrix and fraction only. Those two are in EVERY query a
    results reader issues (a sensible reader never aggregates across them), and their cardinality is tiny (4 x 3). Analyte is filtered
    just as often but has ~218 values at this facility, which would mean
    thousands of directories; it is handled by sorting within the file so row
    group statistics prune it instead. Location is not a partition key at all:
    a reader filters by it only after picking a chemical.
    """
    return os.path.join(root, "matrix=%s" % (matrix or "ALL"),
                        "fraction=%s" % (fraction or "ALL"))


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text):
    """An aware UTC datetime from either clock's format, or None.

    The manifest writes `2026-10-23T10:00:00Z`; EQuIS reports an agent's
    last run as `2026-10-19T20:35:19.82`, UTC without saying so. Compared as
    strings the two sort wrongly, so they are compared as times.
    """
    if not text:
        return None
    text = str(text).strip().rstrip("Z")
    if "." in text:
        text = text.split(".", 1)[0]
    try:
        moment = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return moment.replace(tzinfo=datetime.timezone.utc)


# The tables Analytical Results II reads a facility's data from. Each row
# carries the EBATCH of the EDD that last wrote it, so the newest EBATCH per
# table moves whenever a load touches the facility.
SIGNAL_TABLES = ("DT_RESULT", "DT_TEST", "DT_SAMPLE", "DT_LOCATION")


def read_load_signal(api, facility_id):
    """{table: newest EBATCH} for one facility, or None if any read failed.

    Four reads of one row each: about 1 s apiece on the tenant (2026-10-23,
    two facilities). There is no cheap row count to add to
    it - `/$count` and `$apply=aggregate` both answer with pages of rows.

    What it sees: any EDD that writes to the facility, and a rollback of the
    newest one (the number goes DOWN, which is a change too). What it cannot
    see: edits made in the EQuIS UI, which do not set EBATCH, and deleting or
    rolling back an older load. Those need `--trigger` by hand.
    """
    signal = {}
    for table in SIGNAL_TABLES:
        status, body = api.json(
            "/api/odata/%s?$filter=FACILITY_ID eq %d and EBATCH ne null"
            "&$select=EBATCH&$orderby=EBATCH desc&$top=1"
            % (table, int(facility_id)))
        if status != 200 or not isinstance(body, dict):
            return None
        rows = body.get("value") or []
        signal[table] = rows[0].get("EBATCH") if rows else None
    return signal


def read_agent_runs(api):
    """{event_id: last run (EQuIS's UTC text) or None}, in ONE request.

    One listing of every agent rather than one request per slice: a facility
    has dozens of slices and this is asked on every check.
    """
    status, body = api.json("/api/eia")
    if status != 200:
        return None
    records = body if isinstance(body, list) else (body or {}).get("value") or []
    runs = {}
    for record in records:
        event = record.get("ReportEvent") or {}
        if event.get("id") is not None:
            runs[int(event["id"])] = event.get("lastExecutionDateUTC_ReadOnly")
    return runs


def run_once_schedule(user_id=None, existing=None):
    """A one-time schedule that EQuIS evaluates today.

    `effective` is MIDNIGHT today, Z-suffixed, with the window the whole day:
    a runOnce agent effective at the moment it was made is never evaluated
    (three agents sat for 28 hours). Expect 20-90 minutes to the run.

    `existing` is an agent's current schedule row, edited in place so its id
    is kept: a PUT can change a schedule row but not create one.
    """
    schedule = dict(existing or {"id": 0, "statusFlag": "A",
                                 "createdBy": user_id})
    schedule.update({
        "timeStart": 0, "timeEnd": 1439, "timeInterval": 0,
        "effective": datetime.date.today().isoformat() + "T00:00:00.000Z",
        "expiration": None, "lastEvaluated": None,
        "recurrenceType": 1,                       # once
        "recurrenceDay": 0, "recurrenceInterval": None,
        "recurrenceOrdinal": 0, "title": "runOnce:"})
    return schedule


def arm_agent(api, event_id):
    """Ask one existing agent to run once more, today. Returns (ok, problem).

    There is no "run now" endpoint (7_26_1_26015's spec has none), so this
    re-arms the agent's one-time schedule. A daily agent made before agents
    became one-time is converted by the same edit.

Seen to work 2026-10-23 on a ZZ_ agent (the first facility, one location):
    it fired 9 minutes after creation; this same edit, made after that run,
    cleared `lastEvaluated`, was evaluated 45 s later and fired again 6
    minutes after, storing a new attachment of the same 1,142 rows. Editing
    `effective` back to today and clearing `lastEvaluated` is the whole of
    it; nothing else needed changing.
    """
    status, record = api.json("/api/eia/%d" % int(event_id))
    if status != 200 or not isinstance(record, dict):
        return False, "could not read agent %s (HTTP %s)" % (event_id, status)
    if not record.get("Schedule"):
        return False, "agent %s has no schedule row to re-arm" % event_id
    schedule = run_once_schedule(existing=record["Schedule"])
    status, answer = api.json(
        "/api/eia/%d" % int(event_id), "PUT",
        {"ReportEvent": record.get("ReportEvent"), "Schedule": schedule,
         "NoticeTemplate": record.get("NoticeTemplate"),
         "Recipients": record.get("Recipients") or [],
         "HookInfo": record.get("HookInfo")})
    if status not in (200, 201, 204):
        return False, "HTTP %s %s" % (status, str(answer)[:120])
    # A 200 does not mean it applied. Read back the two fields that matter.
    status, check = api.json("/api/eia/%d" % int(event_id))
    applied = (check or {}).get("Schedule") or {}
    if (str(applied.get("effective") or "")[:10] != schedule["effective"][:10]
            or applied.get("recurrenceType") != 1):
        return False, "PUT answered %s but the schedule did not change" % status
    return True, None


def output_problem(path):
    """Why a slice's stored output is not data, or None if it looks like data.

    A failed run still stores something: "Report did not generate output.
    Please try again." under a header of "Errors" (64 bytes, read
    2026-10-23), or EQuIS's "MISSING UNIT CONVERSION FACTOR" (78 bytes).
    Taken as data, either became a one-row slice with no locations, its cell
    counted as complete, and every location the slice should have carried
    vanished from the rebuilt partition without an error.
    """
    with open(path, "rb") as handle:
        head = handle.read(4096).decode("utf-8-sig", "replace")
    first = head.splitlines()[0] if head else ""
    if "SYS_LOC_CODE" in first.upper():
        return None
    detail = " ".join(line.strip().strip('"') for line in head.splitlines()[:3])
    return "not report output: %s" % detail[:100]


class Manifest(object):
    """What the nightly job knows about each slice, carried between runs.

    Kept as JSON beside the parquet rather than in a database: it is small,
    it is read by hand when something looks wrong, and versioning it with the
    data means a copied directory carries its own provenance.

    The important field is `history` - the last few runs per slice. A single
    night cannot distinguish a slice that is too big from one that hit the ~4x
    generation variance on a bad night, so the rewrite step needs to see
    whether a failure repeated.
    """

    def __init__(self, path):
        self.path = path
        self.data = {"version": 1, "slices": {}, "runs": []}
        source = path
        if not os.path.isfile(source):
            # This file used to be called manifest.json, which the PUBLISHED
            # manifest now owns. Pick up the old one where it still exists,
            # but only when it really is the pipeline's - a published
            # manifest has `partitions` and no `slices`, and reading one as
            # the other would look like a project with no slices at all and
            # invite a rewrite that re-plans everything from nothing.
            legacy = os.path.join(os.path.dirname(path), LEGACY_MANIFEST_NAME)
            if os.path.isfile(legacy):
                try:
                    with open(legacy, "r", encoding="utf-8") as handle:
                        peek = json.load(handle)
                except ValueError:
                    peek = {}
                if peek.get("slices") and "partitions" not in peek:
                    source = legacy
        if os.path.isfile(source):
            with open(source, "r", encoding="utf-8") as handle:
                self.data = json.load(handle)
        self.data.setdefault("slices", {})
        self.data.setdefault("runs", [])

    def save(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        # Write beside and rename: a half-written manifest would lose the
        # mapping from slices to their reports, which cannot be rebuilt from
        # the parquet alone.
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=1, sort_keys=True)
        if os.path.exists(self.path):
            os.remove(self.path)
        os.rename(temporary, self.path)

    def slice(self, name):
        return self.data["slices"].get(name)

    def put_slice(self, name, **fields):
        entry = self.data["slices"].setdefault(name, {"name": name,
                                                      "history": []})
        entry.update(fields)
        return entry

    def record_run(self, name, rows, seconds, status, when=None):
        """Append one night's outcome for a slice.

        `rows` is None when the run produced nothing. That is kept rather than
        stored as 0: a slice that failed and a slice that legitimately has no
        data look identical as a count, and only the first should be split.
        """
        entry = self.put_slice(name)
        history = entry.setdefault("history", [])
        history.append({"when": when or utc_now(), "rows": rows,
                        "seconds": round(seconds, 1) if seconds else None,
                        "status": status})
        # A few nights is enough to tell a repeat failure from a bad night.
        del history[:-10]
        entry["last_rows"] = rows
        entry["last_status"] = status
        entry["last_run"] = history[-1]["when"]
        return entry

    def consecutive_failures(self, name):
        """How many runs in a row produced nothing, most recent first."""
        entry = self.slice(name) or {}
        count = 0
        for run in reversed(entry.get("history") or []):
            if run.get("rows") is None:
                count += 1
            else:
                break
        return count

    @property
    def prefix(self):
        """The slice-name prefix, fixed when the facility was first sliced.

        Stored rather than recomputed: the names are already on reports and
        agents in the tenant, and a changed default must never rename them.
        """
        return self.data.get("prefix") or LEGACY_PREFIX

    def set_prefix(self, prefix):
        self.data.setdefault("prefix", prefix)

    def location_weights(self, matrix, fraction):
        """Per-location row counts for ONE cell, learned from previous runs.

        Empty on the first run, which is why the planner falls back to sample
        counts. Once a night has produced parquet, these are real result rows
        and the bins stop being a guess.

        PER CELL. These used to be one dict for the whole facility, summed
        across every slice, so a location's weight in WS/D also carried its
        WS/N and WS/T rows and every cell was balanced on the wrong numbers.
        The old facility-wide `location_rows` is not read: it is wrong for
        every cell, and one night re-measures it correctly.
        """
        cells = self.data.get("location_rows_by_cell") or {}
        return dict(cells.get(cell_key(matrix, fraction)) or {})

    def update_location_weights(self, matrix, fraction, weights):
        """Merge measured counts into one cell's weights.

        Merged, not replaced, so one bad night - a slice that stored nothing
        - does not erase what earlier nights measured for its locations.
        """
        cells = self.data.setdefault("location_rows_by_cell", {})
        current = cells.setdefault(cell_key(matrix, fraction), {})
        current.update({code: int(count) for code, count in weights.items()})

    def set_location_weights(self, matrix, fraction, weights):
        """Replace one cell's weights outright (the exact walk does this)."""
        cells = self.data.setdefault("location_rows_by_cell", {})
        cells[cell_key(matrix, fraction)] = {code: int(count)
                                            for code, count in weights.items()}

    def cells(self):
        """{(matrix, fraction): [slice entry, ...]} for every slice."""
        out = {}
        for entry in self.data["slices"].values():
            out.setdefault((entry.get("matrix"), entry.get("fraction")),
                           []).append(entry)
        return out

    def armed(self, name, when=None):
        """Record that this slice's agent was asked to run."""
        self.put_slice(name, armed_at=when or utc_now())

    def cell_states(self, runs):
        """{(matrix, fraction): (state, [slice names])} from agents' last runs.

        `runs` is read_agent_runs()'s {event_id: last run}. A slice's output
        is CURRENT when its agent has run since the pipeline last armed it.
        Per cell, one of:

          waiting    some slice is not current (named): armed and not yet
                     run, never run, or without a report or agent. A cell
                     built now would mix new output with old.
          ready      every slice current, and at least one ran since its
                     output was last collected (named): rebuild it.
          failed     every slice current, but a slice's current output was
                     collected and was not data (named). Nothing will change
                     until it runs again, so it is said, not waited on.
          unchanged  every slice current and already collected.

        Mixing is the one wrong state. Every cell's old location lists cover
        each location exactly once, and so do its new ones, so a cell built
        from all-old or all-new output is right; one built half from each,
        after a re-bin, can carry a location twice or not at all.
        """
        out = {}
        for cell, entries in self.cells().items():
            waiting, fresh, failed = [], [], []
            for entry in entries:
                event_id = entry.get("event_id")
                ran = parse_utc((runs or {}).get(int(event_id))) \
                    if event_id else None
                armed = parse_utc(entry.get("armed_at"))
                if (not entry.get("report_id") or ran is None
                        or (armed is not None and ran < armed)):
                    waiting.append(entry["name"])
                elif parse_utc(entry.get("collected_run")) != ran:
                    fresh.append(entry["name"])
                elif entry.get("last_status") != "ok":
                    failed.append(entry["name"])
            if waiting:
                out[cell] = ("waiting", sorted(waiting))
            elif fresh:
                out[cell] = ("ready", sorted(fresh))
            elif failed:
                out[cell] = ("failed", sorted(failed))
            else:
                out[cell] = ("unchanged", [])
        return out

    def mark_partition(self, matrix, fraction, rows, when=None):
        """Record that a cell's partition was rebuilt from a COMPLETE run.

        With what its slices DROPPED for a unit on the wrong basis, summed,
        so the partition says what it is missing for as long as it stands.
        """
        dropped = {}
        for entry in self.cells_by_key().get(cell_key(matrix, fraction), []):
            for key, n in (entry.get("dropped") or {}).items():
                dropped[key] = dropped.get(key, 0) + int(n)
        parts = self.data.setdefault("partitions", {})
        parts[cell_key(matrix, fraction)] = {"as_of": when or utc_now(),
                                             "rows": int(rows)}
        if dropped:
            parts[cell_key(matrix, fraction)]["dropped"] = dropped

    def cells_by_key(self):
        """cells(), keyed "WS/D" as the partitions are."""
        return {cell_key(*cell): entries
                for cell, entries in self.cells().items()}

    def partition_dropped(self, matrix, fraction):
        entry = (self.data.get("partitions") or {}).get(
            cell_key(matrix, fraction)) or {}
        return dict(entry.get("dropped") or {})

    def partition_as_of(self, matrix, fraction):
        entry = (self.data.get("partitions") or {}).get(
            cell_key(matrix, fraction)) or {}
        return entry.get("as_of")
