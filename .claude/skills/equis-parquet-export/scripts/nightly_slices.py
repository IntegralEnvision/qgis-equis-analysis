"""The nightly loop: collect EIA output, write parquet, re-plan tomorrow.

    python scripts/nightly_slices.py --facility <id> --root data/<facility>

Three phases, each usable alone so a failure in one does not force the others
to be re-run:

  --collect   read each slice's stored EIA output, write it to parquet
  --rewrite   re-bin from what actually came back, update the reports
  --repack    rewrite the parquet sorted and row-grouped for fast reads

With none of those flags it does all three in that order.

WHAT IT TOUCHES
---------------
Reads EIA output and writes local parquet. With --rewrite it also EDITS the
slice reports' @sys_loc_codes (never their other parameters, never a report
it did not create). It creates nothing and deletes nothing; setting up the
reports and agents is create_slice_reports.py's job.

THE FEEDBACK LOOP HAS A BLIND SPOT
----------------------------------
Re-weighting learns only from slices that produced output. A slice that timed
out contributes no rows, so it cannot be re-weighted from its own data - and
weighting it at zero would make it look small and keep it broken. Failures are
therefore handled separately from weights: a slice that stored nothing is
SPLIT (its locations redistributed over more bins), not re-weighted.

That is also why the manifest keeps `rows: null` distinct from `rows: 0`. A
slice that genuinely has no data for its locations is correct and should be
left alone; one that failed looks identical as a count.
"""

import argparse
import collections
import datetime
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from slice_store import (  # noqa: E402
    read_env,
    DEFAULT_TARGET_ROWS, MANIFEST_NAME, TARGET_LOCATIONS_PER_SLICE,
    UNITS_NAME, Manifest, bin_count, cell_key, count_results_by_location,
    load_units, pack_bins, partition_path, slice_name, utc_now,
)


# Sorted on the columns a reader filters by, so row-group statistics can
# skip whole chunks. Measured: sorting plus 50k row groups cut a one-analyte
# read from 1.27 MiB to 0.34 MiB. Sorting WITHOUT small row groups made it
# worse (1.93 MiB) - there is nothing to skip with one row group - so the two
# always go together.
SORT_COLUMNS = ("CHEMICAL_NAME", "SYS_LOC_CODE", "SAMPLE_DATE")
ROW_GROUP_ROWS = 50000

# Columns that are genuinely NUMERIC, declared rather than inferred.
#
# pyarrow infers a type from the first block and then fails on a later row
# that does not fit: START_DEPTH held only whole numbers for the first 16 MB
# of one slice, was inferred int64, and the collect died on `0.5` a few
# hundred thousand rows in. Every one of these is a float in EQuIS, so the
# guess is never safe and the inference is not worth its risk.
FORCE_DOUBLE = (
    "START_DEPTH", "END_DEPTH", "PERCENT_MOISTURE", "DILUTION_FACTOR",
    "RESULT_NUMERIC", "REPORT_RESULT_VALUE", "REPORT_RESULT_LIMIT",
    "REPORT_METHOD_DETECTION_LIMIT", "REPORT_REPORTING_LIMIT",
    "REPORT_QUANTITATION_LIMIT", "QUANTITATION_LIMIT",
    "METHOD_DETECTION_LIMIT", "REPORTING_DETECTION_LIMIT",
    "X_COORD", "Y_COORD", "Z_COORD_AVG", "ZFROM", "ZTO",
    "LONGITUDE", "LATITUDE",
)

# Codes that look numeric but are not. Left to inference, CAS_RN becomes a
# float and loses its meaning.
FORCE_STRING = (
    "FACILITY_CODE", "SYS_LOC_CODE", "LOC_NAME", "SYS_SAMPLE_CODE",
    "SAMPLE_NAME", "SAMPLE_TYPE_CODE", "DEPTH_UNIT", "MATRIX_CODE",
    "TASK_CODE", "TASK_CODE_2", "PARENT_SAMPLE_CODE", "FIELD_SDG",
    "COMPOSITE_YN", "ANALYSIS_LOCATION", "LAB_SAMPLE_ID", "LAB_MATRIX_CODE",
    "LAB_NAME_CODE", "ANALYTIC_METHOD", "FRACTION", "TEST_TYPE",
    "PREP_METHOD", "LEACHATE_METHOD", "LAB_SDG", "CAS_RN", "CHEMICAL_NAME",
    "ORGANIC_YN", "REPORT_RESULT_TEXT", "REPORT_RESULT_UNIT",
    "REPORTABLE_RESULT", "DETECT_FLAG", "INTERPRETED_QUALIFIERS",
    "VALIDATOR_QUALIFIERS", "LAB_QUALIFIERS", "DETECTION_LIMIT_UNIT",
    "APPROVAL_CODE", "RESULT_TEXT", "RESULT_UNIT", "RESULT_TYPE_CODE",
    "COLUMN_NUMBER",
)


def fail(message):
    sys.stderr.write("\n%s\n\n" % message)
    raise SystemExit(2)



class Api(object):
    def __init__(self, base, api_key):
        self.base, self.api_key = base, api_key

    def json(self, path, method="GET", body=None, timeout=180, tries=3,
             quote=True):
        # `quote=False` for an OData @odata.nextLink, which arrives already
        # encoded - quoting it again makes %20 into %2520 and the server
        # answers "Failed to parse OData url".
        url = self.base + (urllib.parse.quote(path, safe="/?$=&'()*,")
                           if quote else path)
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(tries):
            request = urllib.request.Request(
                url, data=data, method=method,
                headers={"Authorization": "Bearer " + self.api_key,
                         "Accept": "application/json",
                         "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=timeout) as resp:
                    raw = resp.read()
                    return resp.status, (json.loads(raw) if raw else None)
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()[:400]
            except Exception as exc:  # noqa: BLE001
                if attempt == tries - 1:
                    return None, repr(exc)[:200]
                time.sleep(2 * (attempt + 1))
        return None, "unreachable"

    def eia_output(self, report_id, event_id, out_path, timeout=600):
        """Download one agent's stored output. Returns (bytes, seconds).

        `where`, `outFields` and `resultRecordCount` are ignored on stored
        output - it is a frozen snapshot and only `oType` is honoured - so
        everything is filtered locally after this.
        """
        url = ("%s/api/reports/%d/events/%d/data?oType=csv"
               % (self.base, report_id, event_id))
        request = urllib.request.Request(url)
        request.add_header("Authorization", "Bearer " + self.api_key)
        request.add_header("Accept", "text/csv, application/zip, */*")
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                if resp.status == 204:
                    return None, time.time() - started
                written = 0
                with open(out_path, "wb") as handle:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        handle.write(chunk)
                        written += len(chunk)
                return written, time.time() - started
        except urllib.error.HTTPError as exc:
            return None, time.time() - started
        except Exception:  # noqa: BLE001
            return None, time.time() - started


def csv_to_parquet(csv_path, parquet_path, target=None, factors=None):
    """Convert one slice's CSV to parquet. Returns (rows, counts, units).

    `target` and `factors` turn on unit conversion - see unit_convert.py for
    why it happens here rather than in EQuIS. Without them nothing is
    converted and the third return value is empty, which is what a facility
    whose conversion table is complete should get.
    """
    import pyarrow as pa
    import pyarrow.csv as pacsv
    import unit_convert
    import pyarrow.parquet as pq

    types = {name: pa.string() for name in FORCE_STRING}
    types.update({name: pa.float64() for name in FORCE_DOUBLE})
    convert = pacsv.ConvertOptions(column_types=types,
                                   strings_can_be_null=True)
    reader = pacsv.open_csv(
        csv_path, read_options=pacsv.ReadOptions(block_size=1 << 24),
        convert_options=convert)
    writer = None
    rows = 0
    counts = collections.Counter()
    units = {}
    try:
        for batch in reader:
            if writer is None and not (target and factors):
                directory = os.path.dirname(parquet_path)
                if directory and not os.path.isdir(directory):
                    os.makedirs(directory)
                writer = pq.ParquetWriter(parquet_path, batch.schema,
                                          compression="zstd",
                                          compression_level=9,
                                          use_dictionary=True)
            if target and factors:
                # Per BATCH, not per file: a slice can be hundreds of MB and
                # holding it whole to multiply one column would undo the
                # reason this streams at all.
                table = pa.Table.from_batches([batch])
                table, moved = unit_convert.convert_table(
                    table, target, factors)
                for pair, n in moved.items():
                    units[pair] = units.get(pair, 0) + n
                if writer is None:
                    directory = os.path.dirname(parquet_path)
                    if directory and not os.path.isdir(directory):
                        os.makedirs(directory)
                    writer = pq.ParquetWriter(parquet_path, table.schema,
                                              compression="zstd",
                                              compression_level=9,
                                              use_dictionary=True)
                writer.write_table(table)
                rows += table.num_rows
                if "SYS_LOC_CODE" in table.column_names:
                    counts.update(table.column("SYS_LOC_CODE").to_pylist())
                continue
            writer.write_batch(batch)
            rows += batch.num_rows
            if "SYS_LOC_CODE" in batch.schema.names:
                counts.update(batch.column("SYS_LOC_CODE").to_pylist())
    finally:
        if writer is not None:
            writer.close()
    return rows, counts, units


def collect(api, manifest, root, only=None):
    """Phase 1: pull each slice's stored output into parquet."""
    staging = os.path.join(root, "_staging")
    if not os.path.isdir(staging):
        os.makedirs(staging)
    # Per CELL: a location's rows in WS/D say nothing about its rows in WS/N,
    # and summing them once balanced every cell on the wrong numbers.
    weights = collections.defaultdict(collections.Counter)
    produced = 0
    failures = []
    choices = load_units(root)
    if not choices:
        print("no %s in %s: no cell has a target unit, so nothing converts"
              % (UNITS_NAME, root))

    # ONE fetch of EQuIS's conversion table for the whole collect. Read
    # rather than hardcoded, so the day the tenant's `percent` factors land
    # this starts converting them without anyone editing Python - see
    # unit_convert.py.
    import unit_convert
    try:
        factors = unit_convert.load_factors(api.base, api.api_key)
        print("unit conversion: %d factor(s) from EQuIS" % len(factors))
    except Exception as exc:                    # noqa: BLE001
        # A stopgap must never cost the collect. Without factors nothing is
        # converted and the units stay mixed, which is where they were.
        print("unit conversion unavailable (%s); units left as reported"
              % type(exc).__name__)
        factors = {}
    print("collecting stored output")
    for name in sorted(manifest.data["slices"]):
        if only and name not in only:
            continue
        entry = manifest.slice(name)
        report_id, event_id = entry.get("report_id"), entry.get("event_id")
        if not report_id or not event_id:
            print("   %-32s no report/agent recorded - skipped" % name)
            continue
        csv_path = os.path.join(staging, name + ".csv")
        size, seconds = api.eia_output(report_id, event_id, csv_path)
        if not size:
            # 204, a 500, or an empty body. All mean "nothing stored", which
            # is what --rewrite acts on.
            manifest.record_run(name, None, seconds, "no output")
            manifest.save()
            print("   %-32s NOTHING STORED (%.0fs)" % (name, seconds))
            continue
        parquet_path = os.path.join(
            partition_path(staging, entry.get("matrix"),
                           entry.get("fraction")), name + ".parquet")
        try:
            rows, counts, moved = csv_to_parquet(
                csv_path, parquet_path,
                target=unit_convert.target_for(entry.get("matrix"),
                                               entry.get("fraction"), choices),
                factors=factors)
        except Exception as exc:  # noqa: BLE001 - one unconvertible slice
            # must not cost the other 35. The download is the expensive part
            # and it already succeeded, so a conversion failure is recorded
            # loudly and the run carries on. The first real collect died
            # entirely on one column's inferred type, 2 slices in.
            manifest.record_run(name, None, seconds, "convert failed")
            manifest.save()
            print("   %-32s CONVERT FAILED: %s: %s"
                  % (name, type(exc).__name__, str(exc)[:80]))
            failures.append(name)
            try:
                os.remove(csv_path)
            except OSError:
                pass
            continue
        os.remove(csv_path)
        weights[(entry.get("matrix"), entry.get("fraction"))].update(counts)
        produced += rows
        manifest.record_run(name, rows, seconds, "ok")
        manifest.put_slice(name, parquet=os.path.relpath(parquet_path, root))
        # SAVED PER SLICE, not once at the end. main() also saves when it
        # finishes, but a collect is 36 downloads over many minutes and
        # anything that ends the process before that last line - a crash on
        # one unconvertible column, a dropped connection, Ctrl-C - used to
        # discard the accounting for every slice that had already succeeded.
        # The downloads are the expensive part; losing what they measured
        # because a later one failed is the one cost worth avoiding here.
        manifest.save()
        note = unit_convert.describe(moved)
        print("   %-32s %9s rows  %5.0fs%s"
              % (name, "{:,}".format(rows), seconds,
                 "  [" + note + "]" if note else ""))
        if moved:
            # Recorded, not just printed: a converted number that nobody can
            # trace back to what the lab reported is not an audit trail.
            manifest.put_slice(name, converted={"%s->%s" % k: v
                                                for k, v in moved.items()})
            manifest.save()
    for (matrix, fraction), counts in weights.items():
        # Merged per cell, and only for what came back, so one bad night does
        # not erase what previous nights measured.
        manifest.update_location_weights(matrix, fraction, counts)
    print("   total %s rows" % "{:,}".format(produced))
    if failures:
        print("   %d slice(s) downloaded but could not be converted: %s"
              % (len(failures), ", ".join(failures)))
    print()
    return produced


def rewrite(api, manifest, target_rows, dry_run=False):
    """Phase 2: re-bin from measured rows, and split what failed.

    Two different corrections, deliberately kept apart:

      - slices that produced output are re-balanced using their real row
        counts, which replaces the sample-count proxy the first plan used;
      - slices that produced NOTHING are split, because a failure carries no
        rows to re-weight from and weighting it at zero would keep it broken.
    """
    print("re-planning from measured rows")
    changes = []
    for (matrix, fraction), entries in sorted(
            manifest.cells().items(),
            key=lambda kv: (kv[0][0] or "", kv[0][1] or "")):
        locations = []
        failed = 0
        for entry in entries:
            locations.extend(entry.get("locations") or [])
            if manifest.consecutive_failures(entry["name"]):
                failed += 1
        if not locations:
            continue
        weights = manifest.location_weights(matrix, fraction)
        if not weights:
            # NOTHING MEASURED FOR THIS CELL YET. Re-binning on all-zero
            # weights would pack the whole cell into one or two slices and
            # rewrite every report in it - the slices that time out next.
            # A cell is only re-binned once a collect has measured it.
            print("   %-6s %-2s  no measured rows yet - left as it is"
                  % (matrix or "-", fraction or "-"))
            continue
        cell_weights = {code: weights.get(code, 0) for code in locations}
        # rows_per_unit=1.0 because these weights ARE row counts - either
        # measured from a previous night's output or walked from DT_RESULT.
        # Only create_slice_reports.py's first plan works in samples and has
        # to convert. Stated rather than defaulted, because the two units look
        # identical and mixing them sized every bin ~5x wrong once already.
        parts = bin_count(cell_weights, target_rows, rows_per_unit=1.0)
        if failed:
            # Something in this cell could not generate. More, smaller slices
            # is the only lever that reliably helps.
            parts = max(parts + failed, parts + 1)
        new_bins = pack_bins(cell_weights, parts)
        old = [entry.get("locations") or [] for entry in entries]

        # Only re-bin when it BUYS something. LPT interleaves equal-weight
        # locations, so a re-pack almost always returns different membership
        # even when the balance is identical - acting on that would rewrite
        # every report every night, churning a shared tenant for no gain.
        # Re-bin when the slice count changes, when something failed, or when
        # the current split is materially lopsided.
        old_sizes = [sum(cell_weights.get(code, 0) for code in b) or 0
                     for b in old if b]
        imbalance = (max(old_sizes) / max(min(old_sizes), 1)
                     if old_sizes else 1.0)
        oversized = any(size > target_rows for size in old_sizes)
        if (len(new_bins) == len(old) and not failed and imbalance < 1.25
                and not oversized):
            print("   %-6s %-2s  unchanged (%d slices, %.2fx balance)"
                  % (matrix or "-", fraction or "-", len(old), imbalance))
            continue
        sizes = [sum(cell_weights.get(code, 0) for code in b)
                 for b in new_bins]
        print("   %-6s %-2s  %d -> %d slices%s  rows %s..%s"
              % (matrix or "-", fraction or "-", len(old), len(new_bins),
                 "  (%d failed)" % failed if failed else "",
                 "{:,}".format(int(min(sizes))) if sizes else "0",
                 "{:,}".format(int(max(sizes))) if sizes else "0"))
        changes.append((matrix, fraction, entries, new_bins))

    if dry_run:
        print("\n   --dry-run: no reports edited\n")
        return changes

    for matrix, fraction, entries, new_bins in changes:
        by_index = {}
        for entry in entries:
            by_index[entry.get("index")] = entry
        for index, codes in enumerate(new_bins):
            entry = by_index.get(index)
            if entry is None:
                # A new slice this cell did not have before. It needs a report
                # of its own, which this script does not create - flagged so
                # create_slice_reports.py picks it up.
                name = slice_name(matrix, fraction, index, manifest.prefix)
                manifest.put_slice(
                    name, matrix=matrix, fraction=fraction, index=index,
                    locations=codes, needs_report=True)
                print("   NEW slice %s needs a report; until it has one, "
                      "this cell's partition is not refreshed" % name)
                continue
            if (entry.get("locations") or []) == codes:
                continue
            ok = save_locations(api, entry.get("report_id"), codes)
            manifest.put_slice(entry["name"], locations=codes,
                               locations_saved=ok)
            if not ok:
                print("   %-32s FAILED to save new locations" % entry["name"])
    return changes


def save_locations(api, report_id, codes):
    """Write @sys_loc_codes onto a report and verify it stuck.

    Pipe-separated: a comma-separated MultiSelect returns 200 and zero rows,
    which is a silent wrong answer rather than an error. A 200 from the PUT
    does not mean the value applied, so this re-reads.
    """
    if not report_id:
        return False
    status, report = api.json("/api/reports/%d" % report_id)
    if status != 200 or not isinstance(report, dict):
        return False
    wanted = "|".join(codes)
    for param in report.get("parameters") or []:
        if param.get("name") == "@sys_loc_codes":
            param["value"] = wanted
            param["displayValue"] = wanted
    api.json("/api/reports/%d" % report_id, "PUT", report)
    time.sleep(1)
    status, check = api.json("/api/reports/%d" % report_id)
    if status != 200 or not isinstance(check, dict):
        return False
    for param in check.get("parameters") or []:
        if param.get("name") == "@sys_loc_codes":
            return param.get("value") == wanted
    return False


def incomplete_cells(manifest, staging):
    """{(matrix, fraction): [missing slice names]} for cells not fully staged.

    A cell's partition is the UNION of its slices. If one slice stored
    nothing tonight, rebuilding the partition from the rest drops that
    slice's locations from the published data with no error - which is the
    failure this whole pipeline exists to prevent, because missing data and
    absent data look identical to a reader. It used to do exactly that.
    """
    out = {}
    for (matrix, fraction), entries in manifest.cells().items():
        directory = partition_path(staging, matrix, fraction)
        missing = [entry["name"] for entry in entries
                   if not os.path.isfile(os.path.join(
                       directory, entry["name"] + ".parquet"))]
        if missing:
            out[(matrix, fraction)] = sorted(missing)
    return out


def repack(root, manifest):
    """Phase 3: rewrite each partition sorted, with small row groups.

    One file per (matrix, fraction) rather than per slice: slice boundaries
    are an artefact of how the data had to be fetched, and leaving them in the
    layout would make the reader open several files for one query.

    Only a cell whose EVERY slice was staged is rebuilt. Any other keeps its
    previous partition, whose date the manifest records, and is named here -
    yesterday's complete data is honest about its age; today's partial data
    is not honest about anything.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    staging = os.path.join(root, "_staging")
    if not os.path.isdir(staging):
        print("nothing staged to repack\n")
        return
    print("repacking partitions")
    held = incomplete_cells(manifest, staging)
    for (matrix, fraction), missing in sorted(
            held.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")):
        print("   %s  KEPT %s - %d slice(s) produced nothing tonight: %s"
              % (cell_key(matrix, fraction),
                 "as of %s" % manifest.partition_as_of(matrix, fraction)
                 if manifest.partition_as_of(matrix, fraction)
                 else "absent (never complete)",
                 len(missing), ", ".join(missing)))
    held_dirs = {os.path.normcase(partition_path(staging, m, f))
                 for m, f in held}
    for matrix_dir in sorted(os.listdir(staging)):
        if not matrix_dir.startswith("matrix="):
            continue
        for fraction_dir in sorted(os.listdir(os.path.join(staging,
                                                           matrix_dir))):
            source = os.path.join(staging, matrix_dir, fraction_dir)
            if os.path.normcase(source) in held_dirs:
                continue
            parts = [os.path.join(source, f) for f in sorted(os.listdir(source))
                     if f.endswith(".parquet")]
            if not parts:
                continue
            table = pa.concat_tables([pq.read_table(p) for p in parts],
                                     promote_options="permissive")
            keys = [(c, "ascending") for c in SORT_COLUMNS
                    if c in table.column_names]
            if keys:
                table = table.take(pc.sort_indices(table, sort_keys=keys))
            destination = os.path.join(root, matrix_dir, fraction_dir)
            if not os.path.isdir(destination):
                os.makedirs(destination)
            out = os.path.join(destination, "part-0000.parquet")
            # Beside, then renamed over: a repack interrupted mid-write would
            # otherwise leave a truncated partition where a complete one was.
            temporary = out + ".tmp"
            pq.write_table(table, temporary, compression="zstd",
                           compression_level=9, use_dictionary=True,
                           row_group_size=ROW_GROUP_ROWS,
                           write_statistics=True)
            os.replace(temporary, out)
            manifest.mark_partition(matrix_dir.split("=", 1)[1],
                                    fraction_dir.split("=", 1)[1],
                                    table.num_rows)
            size = os.path.getsize(out) / 1048576.0
            print("   %s/%s  %9s rows  %6.1f MiB  %d row groups"
                  % (matrix_dir, fraction_dir,
                     "{:,}".format(table.num_rows), size,
                     pq.ParquetFile(out).metadata.num_row_groups))
    # STAGING GOES, and this is not tidiness. A reader globbing
    # `<root>/**/*.parquet` would otherwise match the repacked partitions AND
    # the per-slice files they were built from, and count every row twice -
    # measured at about 6.9M rows for a facility holding about 3.45M. Silent
    # doubling is worse than a crash: every count, every aggregate and every
    # histogram would simply be wrong.
    shutil.rmtree(staging, ignore_errors=True)
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Collect EIA output, repack parquet, re-plan tomorrow.")
    parser.add_argument("--facility", type=int, required=True)
    parser.add_argument("--root", required=True,
                        help="directory holding the parquet and manifest")
    parser.add_argument("--target-rows", type=int, default=DEFAULT_TARGET_ROWS)
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--rewrite", action="store_true")
    parser.add_argument("--repack", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --rewrite, show the new bins without "
                             "editing any report")
    parser.add_argument("--weight", choices=("measured", "walk"),
                        default="measured",
                        help="'measured' (default) bins on the rows previous "
                             "runs produced. 'walk' joins DT_SAMPLE -> "
                             "DT_TEST -> DT_RESULT for exact counts before "
                             "any extraction: ~94 min and ~6.7 GB on a "
                             "4M-result facility, so use it to seed a new "
                             "facility, not nightly.")
    args = parser.parse_args()

    phases = (args.collect, args.rewrite, args.repack)
    if not any(phases):
        args.collect = args.rewrite = args.repack = True

    site, key = read_env()
    api = Api(site, key)
    manifest = Manifest(os.path.join(args.root, MANIFEST_NAME))
    if not manifest.data["slices"]:
        fail("No slices in %s. Run create_slice_reports.py first."
             % os.path.join(args.root, MANIFEST_NAME))

    started = utc_now()
    print("facility %d  root %s  started %s\n"
          % (args.facility, args.root, started))

    rows = None
    if args.collect:
        rows = collect(api, manifest, args.root)
    if args.weight == "walk":
        # Exact weights straight from the database, for seeding a facility
        # that has never produced output. Overwrites what previous runs
        # measured, which is correct: both are true result counts, and this
        # one also covers locations no slice has successfully fetched yet.
        print("walking DT_SAMPLE -> DT_TEST -> DT_RESULT (slow) ...")
        started_walk = time.time()

        def progress(stage, pages, _rows):
            if pages % 100 == 0:
                print("   %-10s %5d pages  %5.1f min"
                      % (stage, pages, (time.time() - started_walk) / 60))
                sys.stdout.flush()

        try:
            counts = count_results_by_location(
                api, args.facility, progress=progress,
                checkpoint=os.path.join(args.root, "_walk_checkpoint.json"))
        except RuntimeError as exc:
            fail("The walk failed, so the bins would be built on partial "
                 "counts:\n   %s" % exc)
        for cell, weights in counts.items():
            matrix, fraction = cell.split("/", 1)
            manifest.set_location_weights(matrix, fraction, weights)
        print("   %d cell(s), %s results, %.1f min\n"
              % (len(counts), "{:,}".format(sum(
                  sum(w.values()) for w in counts.values())),
                 (time.time() - started_walk) / 60))
    if args.rewrite:
        rewrite(api, manifest, args.target_rows, dry_run=args.dry_run)
    if args.repack:
        repack(args.root, manifest)

    manifest.data["runs"].append({"when": started, "finished": utc_now(),
                                  "rows": rows})
    del manifest.data["runs"][:-30]
    manifest.save()
    print("manifest: %s" % manifest.path)

    stale = [name for name in manifest.data["slices"]
             if manifest.consecutive_failures(name) >= 2]
    if stale:
        print("\n%d slice(s) have produced nothing twice or more running - "
              "they are being split, but check them:" % len(stale))
        for name in sorted(stale):
            print("   %s (%d consecutive)"
                  % (name, manifest.consecutive_failures(name)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
