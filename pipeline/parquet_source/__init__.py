"""The QGIS plugin's parquet source: a facility's results, written for it.

A facility too large for one report run reaches the plugin as Hive-style
partitions (`matrix=<m>/fraction=<f>/part-0000.parquet`) and a published
`manifest.json`; `docs/layout.md` beside this package specifies them. The
plugin reads that format and imports nothing from here.

Each step is a module run with `python -m parquet_source.<step>`;
`run_nightly` runs the others. Plain Python with pyarrow and numpy (pyproj
is optional); nothing here imports QGIS or the plugin.
"""
