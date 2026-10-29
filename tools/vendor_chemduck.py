"""Copy ChemDuck into the plugin at one commit: equis_loader/chemduck/.

ChemDuck holds the chemistry rules as SQL (docs/chemduck-adoption-plan.md).
It is pure Python and SQL, ~400 KB, needing only duckdb and pyyaml, so it is
COMMITTED here, like the web bundle - an install then needs no network and
no ChemDuck checkout, and an upgrade is a reviewable diff - rather than
built into the ignored embedded_external_libs/ as DuckDB's binaries are.

    python tools/vendor_chemduck.py --commit da0d3b5
    python tools/vendor_chemduck.py                  # re-vendor the pinned commit

WHAT IS COPIED is the `chemduck/` package as it stands IN THE COMMIT, read
with `git cat-file blob` - never the checkout's working tree, so an
uncommitted edit there can't slip in, and never through `git archive`, which
applies the source repository's CRLF conversion on Windows. The bytes are
the commit's. `.gitattributes` keeps them that way here (`-text`), and
VENDORED.json records the commit, the version and every file's hash, which
tools/test_chemduck_vendor.py holds them to.

Upgrade deliberately: a new commit changes the rules the plugin maps with,
so it goes with the parity test (adoption plan, phase 1).
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TARGET = os.path.join(ROOT, "equis_loader", "chemduck")
RECORD = os.path.join(TARGET, "VENDORED.json")
PACKAGE = "chemduck/"


def git(source, *args):
    result = subprocess.run(["git", "-C", source] + list(args), capture_output=True)
    if result.returncode:
        raise SystemExit("git %s failed: %s"
                         % (" ".join(args), result.stderr.decode(errors="replace").strip()))
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", default=os.path.join(ROOT, "..", "..", "chemduck"),
                        help="the ChemDuck checkout to read the commit from")
    parser.add_argument("--commit",
                        help="the ChemDuck commit to vendor; default: the one "
                             "VENDORED.json already pins")
    args = parser.parse_args()

    source = os.path.abspath(args.source)
    if not os.path.isdir(os.path.join(source, ".git")):
        raise SystemExit("No ChemDuck checkout at %s (--source)." % source)
    wanted = args.commit
    if not wanted:
        if not os.path.isfile(RECORD):
            raise SystemExit("Nothing is vendored yet: name a commit (--commit).")
        with open(RECORD, encoding="utf-8") as handle:
            wanted = json.load(handle)["commit"]
    commit = git(source, "rev-parse", "--verify", wanted + "^{commit}").decode().strip()

    # Replace only what this tool wrote: a directory without its record is
    # someone else's, and is not deleted on a guess.
    if os.path.isdir(TARGET) and not os.path.isfile(RECORD):
        raise SystemExit("%s exists but holds no VENDORED.json; not replacing it."
                         % TARGET)

    names = [n for n in git(source, "ls-tree", "-r", "--name-only", commit, "--",
                            PACKAGE).decode().splitlines()
             if "__pycache__/" not in n and not n.endswith((".pyc", ".pyo"))]
    if not any(n == PACKAGE + "__init__.py" for n in names):
        raise SystemExit("%s has no %s__init__.py: not a ChemDuck package commit."
                         % (commit[:7], PACKAGE))
    blobs = {n[len(PACKAGE):]: git(source, "cat-file", "blob", "%s:%s" % (commit, n))
             for n in names}
    found = re.search(rb'^__version__ = "([^"]+)"', blobs["__init__.py"], re.M)
    if not found:
        raise SystemExit("No __version__ in %s's __init__.py." % commit[:7])
    version = found.group(1).decode()

    if os.path.isdir(TARGET):
        shutil.rmtree(TARGET)
    for name, blob in blobs.items():
        path = os.path.join(TARGET, *name.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(blob)

    remote = subprocess.run(["git", "-C", source, "remote", "get-url", "origin"],
                            capture_output=True, text=True).stdout.strip()
    subject = git(source, "log", "-1", "--format=%s", commit).decode().strip()
    record = {
        "note": "Written by tools/vendor_chemduck.py. Do not edit these files: "
                "re-vendor, and tools/test_chemduck_vendor.py checks them.",
        "repository": remote,
        "commit": commit,
        "subject": subject,
        "version": version,
        "vendored": datetime.date.today().isoformat(),
        "files": {name: {"bytes": len(blob),
                         "sha256": hashlib.sha256(blob).hexdigest()}
                  for name, blob in sorted(blobs.items())},
    }
    with open(RECORD, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(record, handle, indent=1, sort_keys=False)
        handle.write("\n")
    print("Vendored ChemDuck %s at %s (%s): %d files, %s bytes, into %s"
          % (version, commit[:7], subject, len(blobs),
             format(sum(len(b) for b in blobs.values()), ","),
             os.path.relpath(TARGET, ROOT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
