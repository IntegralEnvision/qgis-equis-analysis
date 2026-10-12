# Auth and permissions

## Reading the token

The key is a JWT. Decode it locally — no request needed — so an expired key
reports as expired instead of surfacing as a puzzling 401.

```python
def token_claims(api_key):
    parts = (api_key or "").split(".")
    if len(parts) < 2:
        return {}                       # opaque key, not a JWT
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}

claims = token_claims(key)
who = claims.get("jti")                 # the username
exp = claims.get("exp")                 # epoch seconds
```

`jti` is the **caller's identity** — use it, not `/api/users`.

Lifetimes vary enormously (14 days and 833 days both observed). Always read
`exp`; never assume a duration.

## Identifying the caller

`GET /api/users` returns **one record without admin rights and the whole
directory with them** (99 on one tenant). **Never take `[0]`** — with admin
that is the `administrator` account (id 3), not the caller. This misled a whole
debugging session into thinking the token had changed identity.

Get the username from the JWT `jti`, then match it if you need the full record.

## Do not trust `CreatorOf`

It looks like a per-object-type bitmask. It is not usable as one:

| User (one tenant) | Reports owned | `CreatorOf` |
|---|---|---|
| user A | **459** | **0** |
| user B | 194 | 1 |
| user C | 23 | 1 |

Reports are `objectType: 2`, so a "reports bit" reading predicts the exact
opposite of reality. `permission: 4` on an object is likewise the *effective
permission on that object*, not a capability to create new ones — it was
uniformly 4 across every report, facility, dashboard and group.

Per EarthSoft's docs, creating reports needs **Creator on the Reports object
type**, which is deliberately *not* implied by Owner or Editor. Nothing exposed
in the API reliably reports it.

## The only reliable test is a write

A `POST` with a deliberately invalid body is a safe probe: authorisation is
checked before body validation, so **401/403 means no rights** and **400/500
means the request got through to validation** without creating anything.

For a real answer, create and delete a throwaway. `tools/check_report_permissions.py`
in this repo does exactly that — it creates one obviously-named report, tests
edit, deletes it, and prints a summary. Give it to a colleague to run when you
need to know whether a feature will work for non-admins.

**Test edit separately from create.** They are different rights, and the
distinction matters: if a user can edit but not create, an admin can pre-make a
scratch report per person and a client tool only needs to rewrite its filters. That
is a much lower bar.

## Working in a shared tenant

Everything created is visible to everyone and owned by the token's user.

- Name throwaways `ZZ_<purpose>` and delete them in the same session
- Prefer per-user, per-facility naming for anything persistent:
  `<tool> scratch [user@example.com] [FacilityName]`
- A shared scratch object that two people rewrite concurrently will fight —
  scope it per user
- Reports can be organised into folders via `/api/groups`
  (`POST /api/groups/{groupId}/members` adds an existing report). Groups are
  `objectType: 32`; an empty one returns 204. The `memberOf` field on reports
  is null everywhere, so walk the groups to find membership.
