"""A parquet source's URL, split into the part a project keeps and the part a
person keeps.

A published source is a folder URL, and when it is signed the credential is
its query string:

    https://acct.blob.core.windows.net/container/facility_a?sp=r&se=...&sig=...
    '------------------- address ------------------------' '---- token -----'

The address names WHICH data a project maps, so it is saved in the project
file. The token is a key to that data, so it never is: a .qgz is mailed,
attached and committed far more casually than a URL is, and anyone holding
one would hold read access until the token expired. Tokens stay in the
user's own settings, looked up by address. The person who opens a shared
project without one pastes the full URL once, and every project on that
address works for them afterwards.

No QGIS here (see CLAUDE.md), so the rules are tested under a plain
interpreter: tools/test_parquet_address.py.
"""


def is_remote(text):
    """A URL rather than a local folder. Local folders carry no token."""
    return "://" in (text or "")


def split(url):
    """`url` -> (address, token). Either may be "".

    The address loses a trailing slash so that `.../facility_a` and
    `.../facility_a/` are one source; ReportStore joins paths with the slash
    stripped anyway (`ReportStore._join`). A local path is all address:
    Windows paths cannot hold a `?`, and a token on one would mean nothing.
    """
    text = (url or "").strip()
    if not is_remote(text):
        return text, ""
    head, _sep, query = text.partition("?")
    return head.rstrip("/"), query.strip()


def join(address, token):
    """The inverse of `split`: the URL to open."""
    address = (address or "").strip()
    token = (token or "").strip().lstrip("?")
    if not address or not token or not is_remote(address):
        return address
    return "%s?%s" % (address, token)


def remember(book, url):
    """`book` with the token `url` carries recorded under its address.

    A URL without a token changes nothing: typing the bare address must not
    forget a token remembered earlier, since that is exactly what someone
    does when a project gave them the address and the book the rest.
    A new token for a known address replaces the old one, which is how an
    expired token is renewed. Returns a new dict; `book` is not changed.
    """
    address, token = split(url)
    book = dict(book or {})
    if address and token:
        book[address] = token
    return book


def resolve(book, text):
    """The URL to open for what is in the field.

    A full URL opens as typed. A bare address opens with the token the book
    holds for it, if any - so a project's address, or one picked from the
    field's completer, needs nothing more from someone who has opened that
    source before.
    """
    address, token = split(text)
    if token:
        return join(address, token)
    return join(address, (book or {}).get(address, ""))


def needs_token(book, text):
    """True when `text` is a remote address with no token, here or in `book`.

    Not a refusal: a public container needs no token, and the open itself
    says so if one was needed. This decides only whether to tell the person
    that a project's source has no token on this computer.
    """
    address, token = split(text)
    return bool(is_remote(address) and not token
                and not (book or {}).get(address))
