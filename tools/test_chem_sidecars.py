"""The shadow through published sidecars: ChemDuck opened beside a parquet
source from the chemduck/ folder its publish wrote, over http or locally.

Phase 6 of docs/chemduck-adoption-plan.md, the plugin's side. The pipeline's
chemduck_sidecars step and integrator's release --arii put ChemDuck's
sidecars beside manifest.json; chem.Chem.shadow_published copies them and
reads the results from the partitions where they are. This holds it to:

- over http (a local server answering ranges, as Azure does), it opens
  through the sidecars and answers as ChemDuck loaded row by row answers;
- the same from a local folder;
- sidecars built from another manifest are refused, opening nothing;
- a source without sidecars says so (None), and a refused fetch is an
  error, not "no sidecars";
- the comparison the panel logs (chem_parity) works on it, every fixture
  location differing as it declares;
- closing removes the database file and the copied folder;
- a token in the source's query reaches no error message unredacted.

    python tools/test_chem_sidecars.py

Needs duckdb and pyyaml, as ChemDuck does; DuckDB reads http through its
httpfs extension, as the store does for a remote source.
"""
import functools
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import chem_fixture as fx                            # noqa: E402
from test_chemduck_parity import RECIPES, combos     # noqa: E402

from equis_loader import chem as ch                  # noqa: E402
from equis_loader import chem_parity as cp           # noqa: E402
from equis_loader import chemduck as cd              # noqa: E402
from equis_loader.parquet_address import redact      # noqa: E402
from equis_loader.store import ReportStore           # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + str(detail)) if detail != "" else ""))
    if not condition:
        FAILURES.append(label)


class Ranged(http.server.SimpleHTTPRequestHandler):
    """Static files with Range, as blob storage answers DuckDB; the query
    string (a SAS token on Azure) is ignored, as a public container would."""
    refuse = False

    def log_message(self, *_args):
        pass

    def translate_path(self, path):
        return super().translate_path(path.split("?", 1)[0])

    def send_head(self):
        if Ranged.refuse:
            self.send_error(403, "refused")
            return None
        path = self.translate_path(self.path)
        if os.path.isdir(path) or not os.path.isfile(path):
            self.send_error(404)
            return None
        size = os.path.getsize(path)
        wanted = self.headers.get("Range")
        handle = open(path, "rb")
        if wanted and wanted.startswith("bytes="):
            first, _, last = wanted[6:].partition("-")
            first = int(first)
            last = min(int(last) if last else size - 1, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (first, last, size))
            self.send_header("Content-Length", str(last - first + 1))
            handle.seek(first)
            self._remaining = last - first + 1
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self._remaining = size
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", self.date_time_string(os.path.getmtime(path)))
        self.end_headers()
        return handle

    def copyfile(self, source, outputfile):
        outputfile.write(source.read(self._remaining))


def points(chem, chemical, kwargs):
    try:
        return [{k: v for k, v in p.items() if k not in ("result_id", "x", "y")}
                for p in chem.points(chemical, **kwargs)]
    except Exception as exc:                   # noqa: BLE001 - compared as text
        return "refused: " + str(exc).splitlines()[0]


def main():
    folder = tempfile.mkdtemp(prefix="chem_sidecars_")
    server = None
    opened = []
    try:
        facility = os.path.join(folder, "facility")
        manifest_path = fx.write(facility)
        ch._quiet(cd.publish, manifest_path, os.path.join(facility, ch.SIDECARS),
                  x_col="LONGITUDE", y_col="LATITUDE", quiet=True)
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(Ranged, directory=facility))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        # A token-shaped query, ignored by the server, to see it redacted.
        url = "http://127.0.0.1:%d?sv=1&sp=r&sig=NOT-A-REAL-TOKEN" % server.server_address[1]

        store = ReportStore.from_manifest(url)
        store.con.execute("SET threads = 1")
        work = os.path.join(folder, "work")
        os.makedirs(work)

        # --- over http, through the sidecars -----------------------------------
        chem = ch.Chem.shadow_published(url, store.manifest, os.path.join(work, "http.duckdb"),
                                        os.path.join(work, "http_sidecars"), threads=1)
        opened.append(chem)
        check("an http source with sidecars opens through them",
              chem is not None and chem.sidecars["format"] == "chemduck-arii-sidecars")
        check("... with every result, read from the partitions",
              chem.cur.execute("SELECT count(*) FROM results").fetchall()[0][0]
              == len(fx.rows()))
        loaded = ch.Chem.shadow(manifest_path, os.path.join(work, "loaded.duckdb"), threads=1)
        opened.append(loaded)
        differ = []
        for how, nd in RECIPES:
            for chemical, matrix, fraction in combos(store):
                if "_" in (matrix, fraction):
                    continue                   # the fixture's blank codes; see test_chem_shadow
                kwargs = dict(how=how, non_detect=nd, matrix=(matrix,), fraction=(fraction,))
                if points(chem, chemical, kwargs) != points(loaded, chemical, kwargs):
                    differ.append("%s %s/%s %s %s" % (chemical, matrix, fraction, how, nd))
        check("it answers as ChemDuck loaded row by row answers, every recipe", not differ,
              "; ".join(differ[:3]))

        # --- the panel's comparison on it ---------------------------------------
        expected = {loc: expect for loc, _what, _rows, expect in fx.CASES}
        wrong = []
        for how, nd in RECIPES:
            for chemical, matrix, fraction in combos(store):
                comparison = cp.Comparison(store, chem, chemical, matrix, fraction, how, nd)
                for location in comparison.locations():
                    causes = comparison.explain(location)
                    if set(causes) != set(expected[location][how]):
                        wrong.append("%s %s: %s" % (location, how, sorted(causes)))
        check("the panel's comparison through it: every location differs as it declares",
              not wrong, "; ".join(wrong[:3]))

        # --- locally --------------------------------------------------------------
        local = ch.Chem.shadow_published(facility, store.manifest,
                                         os.path.join(work, "local.duckdb"),
                                         os.path.join(work, "local_sidecars"), threads=1)
        opened.append(local)
        kwargs = dict(how="maximum", non_detect="half", matrix=("WG",), fraction=("T",))
        check("a local source with sidecars opens through them too, and answers alike",
              local is not None and points(local, "Arsenic", kwargs) == points(chem, "Arsenic",
                                                                            kwargs))

        # --- stale sidecars are refused -------------------------------------------
        with open(manifest_path, "rb") as handle:
            original = handle.read()
        try:
            with open(manifest_path, "wb") as handle:
                handle.write(original + b"\n")     # same manifest, other bytes
            try:
                ch.Chem.shadow_published(url, store.manifest, os.path.join(work, "s.duckdb"),
                                         os.path.join(work, "s_sidecars"))
                refused = ""
            except ch.SidecarsStale as exc:
                refused = str(exc)
            check("sidecars built from another manifest are refused, opening nothing",
                  "another manifest" in refused
                  and not os.path.exists(os.path.join(work, "s.duckdb"))
                  and not os.path.exists(os.path.join(work, "s_sidecars")), refused)
            changed = dict(store.manifest, rows=store.manifest["rows"] + 1)
            with open(manifest_path, "wb") as handle:
                handle.write(json.dumps(changed).encode("utf-8"))
            try:
                ch.Chem.shadow_published(url, store.manifest, os.path.join(work, "s.duckdb"),
                                         os.path.join(work, "s_sidecars"))
                refused = ""
            except ch.SidecarsStale as exc:
                refused = str(exc)
            check("a manifest changed since the store opened is refused too",
                  "changed since the source opened" in refused, refused)
        finally:
            with open(manifest_path, "wb") as handle:
                handle.write(original)

        # --- no sidecars, and a refused fetch --------------------------------------
        moved = os.path.join(folder, "moved_sidecars")
        shutil.move(os.path.join(facility, ch.SIDECARS), moved)
        try:
            none = ch.Chem.shadow_published(url, store.manifest, os.path.join(work, "n.duckdb"),
                                            os.path.join(work, "n_sidecars"))
            check("a source without sidecars says so (None), creating nothing",
                  none is None and not os.path.exists(os.path.join(work, "n_sidecars")))
        finally:
            shutil.move(moved, os.path.join(facility, ch.SIDECARS))
        Ranged.refuse = True
        try:
            ch.Chem.shadow_published(url, store.manifest, os.path.join(work, "r.duckdb"),
                                     os.path.join(work, "r_sidecars"))
            message = ""
        except ch.SidecarsStale:
            message = "stale"
        except Exception as exc:               # noqa: BLE001 - the point
            message = "%s: %s" % (type(exc).__name__, exc)
        finally:
            Ranged.refuse = False
        check("a refused fetch is an error, not 'no sidecars'", "403" in message, message)
        check("... and its message, redacted as the panel logs it, holds no token",
              "NOT-A-REAL-TOKEN" not in redact(message), redact(message))

        # --- closing ----------------------------------------------------------------
        chem.close()
        opened.remove(chem)
        check("closing removes the database file and the copied sidecars",
              not os.path.exists(os.path.join(work, "http.duckdb"))
              and not os.path.exists(os.path.join(work, "http_sidecars")))
        store.close()
    finally:
        for c in opened:
            try:
                c.close()
            except Exception:                  # noqa: BLE001
                pass
        if server is not None:
            server.shutdown()
        shutil.rmtree(folder, ignore_errors=True)
    print()
    if FAILURES:
        print("FAILED: %d" % len(FAILURES))
        return 1
    print("The shadow opens through published sidecars, and refuses stale ones.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
