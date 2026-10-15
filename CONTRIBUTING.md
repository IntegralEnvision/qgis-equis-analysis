# Contributing

Issues and pull requests are welcome, from anyone using EQuIS Enterprise with
QGIS.

## Filing an issue

Open one on the [issue tracker](https://github.com/integralenvision/qgis-equis-analysis/issues).
The most useful reports say:

- the plugin version (*Plugins → Manage and Install Plugins*), the QGIS
  version, and the EQuIS Enterprise version if you know it;
- what you did, what you expected, and what happened, with the status line's
  message or the *Log Messages* panel's text;
- whether the data was live, cached (offline) or a parquet source.

**Leave out API keys, site URLs and real data.** A key is a bearer token for
your account, and a site URL or location code can identify a client. Replace
them with placeholders; a description of the data's shape is enough.

## Changing the code

[CLAUDE.md](CLAUDE.md) is the guide for anyone changing the code, whether a
person or a coding agent. Read it first. It holds the commands, the one
structural rule, and the domain rules that exist because breaking them
produces a plausible number rather than an error. In short:

- Run the tests that need no QGIS before opening a pull request. Most of
  the suite runs under a plain Python 3.12 interpreter:

  ```
  python tools/test_parsing.py
  python tools/test_state.py
  python tools/test_live.py
  python tools/test_tasks.py
  python tools/test_overlay_payload.py
  ```

  The same five run on every release tag. If you change the DuckDB store,
  run `tools/test_store_parity.py` too (it needs `duckdb`). If you change
  the panel, run `tools/test_offline.py` under QGIS's own Python.
- Keep `equis_api.py`, `report_data.py`, `sparkline.py`, `cache.py` and
  `store.py` free of QGIS imports. That is what lets the tests above run
  without QGIS.
- Call only the EQuIS REST API's documented endpoints, and add no write
  that bypasses `EquisClient._request`. It enforces read-only mode.
- Comments and commit messages say why: what changed, what it was before,
  and what failure or measurement justified it.

## Releases

Pushing a tag `v<version>` that matches `version=` in
`equis_loader/metadata.txt` runs the tests above, builds the plugin ZIP and
publishes it on the Releases page (`.github/workflows/release.yml`).
