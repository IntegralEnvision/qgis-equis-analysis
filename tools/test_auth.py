"""Reading a key's expiry, so auth failures say what is actually wrong.

Pure Python - no QGIS and no network:

  python tools\\test_auth.py

An expired key fails every request with a 401, which reads like the service
being down. The expiry is carried in the key itself, so the plugin can name
the cause instead of guessing - and can refuse to connect before the round
trip that would have failed anyway.
"""

import base64
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from equis_loader.equis_api import (  # noqa: E402
    describe_token_state,
    token_expiry,
)

FAILURES = []


def check(label, condition, detail=""):
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         (" - " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


def make_token(exp=None, claims=None, segments=3):
    """A JWT-shaped string. Only the payload segment is ever read."""
    payload = dict(claims or {})
    if exp is not None:
        payload["exp"] = exp
    raw = base64.urlsafe_b64encode(
        json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    parts = ["header", raw, "signature"][:segments]
    return ".".join(parts)


def main():
    now = datetime.datetime(2026, 8, 21, 12, 0, 0)
    epoch = datetime.datetime(1970, 1, 1)

    def at(when):
        return (when - epoch).total_seconds()

    # --- reading the expiry ------------------------------------------
    future = now + datetime.timedelta(days=30)
    token = make_token(exp=at(future))
    read = token_expiry(token)
    check("expiry: read back from the token",
          read is not None and abs((read - future).total_seconds()) < 2,
          str(read))

    check("expiry: padding-free base64 still decodes",
          token_expiry(make_token(exp=at(future), claims={"jti": "a"})) is not None)

    # --- things that are not readable tokens --------------------------
    check("expiry: an opaque (non-JWT) key reads as unknown",
          token_expiry("not-a-jwt-at-all") is None)
    check("expiry: an empty key reads as unknown", token_expiry("") is None)
    check("expiry: None reads as unknown", token_expiry(None) is None)
    check("expiry: a token with no exp claim reads as unknown",
          token_expiry(make_token(claims={"jti": "x"})) is None)
    check("expiry: undecodable base64 reads as unknown",
          token_expiry("header.!!!not-base64!!!.sig") is None)
    check("expiry: a non-numeric exp reads as unknown",
          token_expiry(make_token(claims={"exp": "soon"})) is None)
    check("expiry: a two-segment token is still read",
          token_expiry(make_token(exp=at(future), segments=2)) is not None)

    # --- how the failure is described ---------------------------------
    expired_recently = make_token(exp=at(now - datetime.timedelta(hours=14)))
    text = describe_token_state(expired_recently, now=now)
    check("describe: a recently expired key says how long ago",
          "expired" in text and "14 hours" in text, text.strip())

    expired_long = make_token(exp=at(now - datetime.timedelta(days=90)))
    text = describe_token_state(expired_long, now=now)
    check("describe: a long-expired key gives the date",
          "expired on" in text and "2026-05" in text, text.strip())

    valid = make_token(exp=at(now + datetime.timedelta(days=400)))
    text = describe_token_state(valid, now=now)
    check("describe: a valid key says the expiry is NOT the cause",
          "not an expiry" in text, text.strip())

    check("describe: an unreadable key adds nothing rather than guessing",
          describe_token_state("opaque-key", now=now) == "")
    check("describe: an empty key adds nothing",
          describe_token_state("", now=now) == "")

    # A key that lapsed minutes ago should not read as "0 hours".
    just_expired = make_token(exp=at(now - datetime.timedelta(minutes=20)))
    text = describe_token_state(just_expired, now=now)
    check("describe: minutes are used below an hour",
          "minutes" in text, text.strip())

    print("\n%s" % ("ALL AUTH TESTS PASSED" if not FAILURES
                    else "FAILURES: %s" % ", ".join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
