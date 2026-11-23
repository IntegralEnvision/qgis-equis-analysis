"""A parquet source's address (kept in the project) and token (kept by the
person): the split, the join, and the token book.

Runs on a plain interpreter (no QGIS, no network):

  python tools/test_parquet_address.py
"""
import datetime
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# parquet_address imports no QGIS, so load it without the package (whose
# __init__ does).
_spec = importlib.util.spec_from_file_location(
    "parquet_address", os.path.join(ROOT, "equis_loader", "parquet_address.py"))
pa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pa)

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


ADDRESS = "https://acct.blob.core.windows.net/container/facility_a"
TOKEN = "sp=r&st=2026-11-01T00:00:00Z&se=2027-11-01T00:00:00Z&sv=2022-11-02&sr=c&sig=abc%3D"
SIGNED = ADDRESS + "?" + TOKEN

# --- split / join
check("a signed URL splits at the ?", pa.split(SIGNED) == (ADDRESS, TOKEN),
      repr(pa.split(SIGNED)))
check("a trailing slash is one source with none",
      pa.split(ADDRESS + "/?" + TOKEN) == (ADDRESS, TOKEN))
check("an unsigned URL is all address", pa.split(ADDRESS) == (ADDRESS, ""))
check("whitespace around a pasted URL is dropped",
      pa.split("  %s \n" % SIGNED) == (ADDRESS, TOKEN))
check("a bare ? is no token", pa.split(ADDRESS + "?") == (ADDRESS, ""))
for local in (r"C:\data\facility_a", "data/facility_a", "/srv/facility_a/"):
    check("a local folder is all address, untouched: %s" % local,
          pa.split(local) == (local, ""))
check("nothing splits to nothing", pa.split("") == ("", "")
      and pa.split(None) == ("", ""))
check("join undoes split", pa.join(*pa.split(SIGNED)) == SIGNED)
check("join without a token is the address", pa.join(ADDRESS, "") == ADDRESS)
check("join tolerates a token given with its ?",
      pa.join(ADDRESS, "?" + TOKEN) == SIGNED)
check("a token never joins onto a local folder",
      pa.join("data/facility_a", TOKEN) == "data/facility_a")
# The token must go AFTER the path, never before it: a path appended after
# the signature is a different resource to Azure and answers 403
# (ReportStore._join's docstring). The address here is the folder, so the
# token ends the URL.
check("the token ends the URL", pa.join(ADDRESS, TOKEN).endswith("sig=abc%3D"))

# --- the book
book = pa.remember({}, SIGNED)
check("remembering a signed URL records its token by address",
      book == {ADDRESS: TOKEN}, repr(book))
check("remembering a bare address forgets nothing",
      pa.remember(book, ADDRESS) == book)
check("remembering a local folder records nothing",
      pa.remember({}, "data/facility_a") == {})
renewed = pa.remember(book, ADDRESS + "?sp=r&sig=new")
check("a new token replaces the old one", renewed == {ADDRESS: "sp=r&sig=new"})
check("remember returns a new dict", book == {ADDRESS: TOKEN})
other = pa.remember(book, "https://acct.blob.core.windows.net/container/facility_b?sig=b")
check("two addresses keep two tokens", len(other) == 2
      and other[ADDRESS] == TOKEN)

# --- resolve: what the field opens
check("a bare address opens with the book's token",
      pa.resolve(book, ADDRESS) == SIGNED)
check("so does one with a trailing slash",
      pa.resolve(book, ADDRESS + "/") == SIGNED)
check("a full URL opens as typed, whatever the book holds",
      pa.resolve(book, ADDRESS + "?sig=typed") == ADDRESS + "?sig=typed")
check("an address the book lacks opens bare",
      pa.resolve(book, ADDRESS + "_x") == ADDRESS + "_x")
check("a local folder opens as itself",
      pa.resolve(book, "data/facility_a") == "data/facility_a")
check("an empty book is fine", pa.resolve(None, ADDRESS) == ADDRESS)

# --- needs_token: only a remote address nobody here has a token for
check("a bare remote address with no token on file needs one",
      pa.needs_token({}, ADDRESS))
check("not when the book has it", not pa.needs_token(book, ADDRESS))
check("not when the URL carries it", not pa.needs_token({}, SIGNED))
check("never for a local folder", not pa.needs_token({}, "data/facility_a"))
check("never for nothing", not pa.needs_token({}, ""))

# --- token_expiry: the SAS's own `se`, naive UTC
D = datetime.datetime
check("se with seconds", pa.token_expiry(TOKEN) == D(2027, 11, 1),
      repr(pa.token_expiry(TOKEN)))
check("se percent-encoded, as Azure's portal writes it",
      pa.token_expiry("sp=r&se=2027-11-01T13%3A45%3A10Z&sig=x")
      == D(2027, 11, 1, 13, 45, 10))
check("se without seconds",
      pa.token_expiry("se=2027-11-01T13:45Z") == D(2027, 11, 1, 13, 45))
check("se as a date", pa.token_expiry("se=2027-11-01") == D(2027, 11, 1))
check("se with a seven-digit fraction",
      pa.token_expiry("se=2027-11-01T13:45:10.0000000Z")
      == D(2027, 11, 1, 13, 45, 10))
check("a leading ? is tolerated",
      pa.token_expiry("?se=2027-11-01") == D(2027, 11, 1))
check("naive, like the rest of the plugin's UTC",
      pa.token_expiry(TOKEN).tzinfo is None)
check("no se, no expiry", pa.token_expiry("sp=r&sig=x") is None)
check("no token, no expiry",
      pa.token_expiry("") is None and pa.token_expiry(None) is None)
check("an se Azure would refuse is not guessed at",
      pa.token_expiry("se=11/01/2027") is None
      and pa.token_expiry("se=2027-11-01T13:45:10+02:00") is None)

print("\n%d failure(s)" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
