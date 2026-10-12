# Phase 0: the bundled substrate

Can DuckDB and an Arrow implementation reach a stock QGIS install with no
compiler, no network at install time and no help from IT — and once there, are
they fast enough to make US-11's promise true?

[docs/vision-linked-views.md](vision-linked-views.md) sequences this first,
before anything is built on it, because it is the least interesting part and
the most able to stop everything.

**Verdict: clear on Windows/cp312 and Linux/cp311. macOS still to run.**

## Results

`python tools/test_phase0.py`, run in the QGIS Python console on Windows.

| Date | OS | Python | Bundle | duckdb | Slowest query | Arrow IPC | On disk |
|---|---|---|---|---|---|---|---|
| 2026-09-19 | **Windows** | **3.12.13** | `win_amd64-cp312` | 1.5.5 | **13.8 ms** | 1.5 ms | **74 MB** |
| 2026-09-19 | Linux | 3.11.15 | `linux_x86_64-cp311` | 1.5.5 | 6.9 ms | 2.9 ms | 91.5 MB |

**QGIS 4.2.1 on Windows runs Python 3.12.13**, matching
[webengine-support.md](webengine-support.md). If macOS and Linux 4.2 builds
agree, the build matrix is one cp312 bundle per platform — three, not a grid.

Windows is the smaller bundle (74 MB against Linux's 91.5 MB) because the
Windows DuckDB binary is 35.6 MB where the Linux one is 57.5 MB.

The timings below are from the Linux run; the Windows run's slowest operation
was 13.8 ms, against the same 100 ms budget.

Every post-fetch operation against a **460,000-row** report-shaped table,
warm, on the budget US-11 asks for:

| Operation | Time |
|---|---|
| distinct chemicals with counts | 6.1 ms |
| histogram, 40 bins | 4.8 ms |
| brush: filter a value range, count locations | 6.2 ms |
| aggregate: maximum per location | 6.6 ms |
| aggregate: most recent per location | 8.6 ms |
| one location's time series | 3.4 ms |
| build the whole 460,000-row table | 0.23 s |
| serialize 20,000 rows to Arrow IPC | 1.5 ms (193 KB) |

**US-11 asked for under 100 ms and got under 10.** The existing
`ReportTable` index costs "a few seconds and ~35 MB" once and answers in
microseconds; this replaces it with something that needs no index, answers in
single-digit milliseconds, and can be asked questions nobody wrote a method
for.

## Four things this turned up

**0. A test can be wrong in the direction that matters.** The first Windows
run reported `BLOCKED` on a single check: `pyarrow never imported`. The
install was fine. The assertion was `"pyarrow" not in sys.modules`, which is a
statement about the whole process — and QGIS ships pyarrow and imports it for
its own reasons long before this test runs. The claim actually being protected
is narrower: *we* do not need pyarrow, so the bundle does not have to carry
it. It is now tested as the two things it really is — the bundle contains
neither `pyarrow` nor `numpy`, and our serialization did not import pyarrow if
the host had not already — so a host that happens to load it no longer reads
as a failure.

**1a. Amended 2026-09-23: on QGIS 4.2.1 pyarrow is ALREADY THERE.**

Finding 1 is still true - `.arrow()` does import pyarrow, re-verified by
watching `sys.modules` flip - but the conclusion drawn from it was too
strong. Checked on this machine:

| package | QGIS 4.2.1 ships it? | size |
|---|---|---|
| duckdb | **yes**, its own site-packages | 1.0 MB |
| pyarrow 25.0.0 | **yes** | 84.5 MB |
| numpy | **yes** | 30.3 MB |
| arro3-core + arro3-io | no | 37.3 MB |

So avoiding pyarrow does not avoid the 40 MB - QGIS already loaded it - and
vendoring arro3 would add a SECOND Arrow runtime beside one that is present.
`bridge_payload.py` therefore prefers pyarrow and falls back to arro3, which
means **a stock QGIS 4.2 install needs nothing vendored for the web view**.
Verified by blocking arro3 at the import hook and running the payload path
through on QGIS's own pyarrow.

Finding 1 still governs anywhere duckdb exists without pyarrow, which is why
the arro3 path is kept and tested rather than deleted.

DuckDB has no Arrow library of its own, incidentally - it hands off to
whatever Python Arrow implementation is installed. There was nothing to reuse.

**1. `.arrow()` quietly pulls in pyarrow — and does not need to.**
`DuckDBPyRelation.arrow()` is the obvious call and it imports `pyarrow`:
~40 MB plus numpy, and a second Arrow runtime beside the one `arro3` already
carries. DuckDB exposes `__arrow_c_stream__`, so the Arrow PyCapsule interface
hands data straight to `arro3.core.Table.from_arrow(relation)` with pyarrow
never loaded. This is why `boro` depends on `arro3-io`, and the test now
asserts `"pyarrow" not in sys.modules` so the win cannot be lost by someone
reaching for the obvious call later.

**2. The dependency closure is bigger than the two packages named.**
`arro3-io` pulls `arro3-core`; `duckdb` pulls `typing_extensions`. Four
distributions, not two.

**3. `pip --platform` matches literally, and these projects disagree on
manylinux.** duckdb publishes `manylinux_2_26/2_28`, arro3 publishes
`manylinux_2_17/manylinux2014`. One `--platform` satisfies one and fails the
other with "No matching distribution found", which reads like the package does
not exist. `--abi` has the same problem: duckdb is cp312-specific, arro3 is
cp311-abi3. `tools/vendor_deps.py` passes every tag either project uses and
lets pip choose per project.

**4. 91.5 MB unpacked, and it is irreducible.** Almost all of it is native
code that is *already stripped*:

| | |
|---|---|
| `_duckdb…so` | 57.5 MB |
| `arro3/io/_io.abi3.so` | 22.5 MB |
| `arro3/core/_core.abi3.so` | 10.3 MB |
| everything else | ~1 MB |

Stubs, `.dist-info` and SBOMs are under a megabyte together, so pruning is not
worth the risk. The wheels compress to ~25 MB on Windows and ~32 MB on Linux,
which is what a plugin ZIP would carry. That is large for a QGIS plugin but
well within what `Install from ZIP` handles.

⚑ **If the size ever has to come down**, the lever is `arro3-io` (22.5 MB) —
DuckDB's `arrow` community extension can write IPC from SQL directly, which
would remove it. That trades a bundled Python package for a bundled DuckDB
extension, so it is a real option rather than a free saving, and it is not
worth taking until someone objects to the size.

## Still to run

**macOS on QGIS 4.2's Python.** Windows is confirmed at 3.12.13. If macOS
agrees, the matrix is **one cp312 bundle per platform** — three, not a grid.
`duckdb` publishes no abi3 wheel, so if it disagrees it is one bundle per
(platform × Python minor).

⚑ **Worth knowing: QGIS appears to ship pyarrow already** (finding 0 above).
If that holds across platforms, `arro3-io` — 25.5 MB of the Windows bundle —
could in principle be dropped in favour of the host's pyarrow. I would not
take it: it couples the plugin to whatever Arrow version QGIS happens to
carry, across versions we do not control, to save a quarter of a bundle we
have already shown installs fine. Recorded because it is a real option, not
because it is a good one.

### Which Python runs which script

They are not the same, and the distinction is the whole reason `duckdb`
publishing no abi3 wheel matters.

| Script | Where it runs | Why |
|---|---|---|
| `tools/vendor_deps.py` | **any** Python 3 with pip and network | It never imports duckdb. It shells out to `pip download` with explicit `--platform` / `--python-version` / `--abi` flags and unzips the result, so the interpreter running it is irrelevant — *as long as the target is explicit*. |
| `tools/test_phase0.py` | **the QGIS Python** | It actually loads the native extensions. Run anywhere else and it proves nothing about QGIS. |

The one trap is `--target local`, which takes its platform and Python tags
from whichever interpreter is running it. From a system Python 3.13 that
builds a `cp313` bundle QGIS's 3.12 cannot load. So either:

```
# explicit - run from anywhere
python tools/vendor_deps.py --target win --python 312
```

or run `--target local` **from the QGIS Python**, where it cannot mismatch by
construction. The script warns when the running interpreter is not cp312 and
prints the explicit command to use instead.

Then, in the QGIS Python console — the interpreter that has to load it:

```python
exec(open(r"C:\path\to\equis_qgis\tools\test_phase0.py").read())
```

## Building an installable ZIP

```
python tools/vendor_deps.py --target win --python 312
python tools/package.py --bundle win_amd64-cp312
```

Produces `dist/equis_loader-<version>-win_amd64-cp312.zip`, **24.1 MB**, 113
files, installable through *Install from ZIP*. `--list-bundles` shows what has
been built. Without `--bundle` the archive carries no vendored dependency at
all and is the same lean plugin as before.

Two things `--bundle` changes, both because a bundled archive is not the same
artefact as the plugin:

- **`.pyd` stops being a build artefact.** `package.py` skips `.pyd` as an
  editor leftover, which is right for plugin source and catastrophic for a
  vendored bundle: DuckDB's Windows binary *is*
  `_duckdb.cp312-win_amd64.pyd`. Ship without it and the plugin installs
  cleanly, then cannot import. The vendor directory is now exempt from the
  skip rules.
- **`qgisMinimumVersion` is raised to 4.2 in the archive**, and only in the
  archive. A cp312 binary cannot load on 3.34 whatever the metadata claims,
  and the version QGIS checks before installing should match what is
  actually inside. The working copy keeps 3.34, because it still gates the
  unbundled plugin, which genuinely does run there.

Verified in the built archive: all three native binaries present
(`_duckdb…pyd` 35.6 MB, `_io.pyd` 25.5 MB, `_core.pyd` 11.7 MB), no other
platform's bundle leaked in, no `.env`, zip integrity clean.

## How it is put together

| Piece | Job |
|---|---|
| `tools/vendor_deps.py` | `pip download` + unpack into `equis_loader/embedded_external_libs/<platform>-<pytag>/`. Cross-builds for a QGIS you are not running. |
| `equis_loader/vendor.py` | Puts the matching bundle at the front of `sys.path`, and takes it off again. No QGIS imports, so it is testable anywhere. |
| `tools/test_phase0.py` | The verification above. Runs under a plain interpreter or the QGIS console. |

The bundle is **not committed** — 90 MB per platform, rebuildable from PyPI in
seconds, so CI builds it into the release ZIP rather than git carrying three
copies of DuckDB forever. This is QDuckDB's arrangement.

`vendor.py` names the failure that will actually happen in the field. A
bundle built for one Python minor version cannot be loaded by another, so
"right plugin, wrong Python" is reported as exactly that — naming the Python
that is running, the bundle it wanted, and the bundles present — rather than
as `No module named 'duckdb'`.

It also checks *which copy answered*: a system-wide DuckDB satisfying the
import would make the bundle look like it worked when it had done nothing.
