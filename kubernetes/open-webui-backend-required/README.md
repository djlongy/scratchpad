# Open WebUI 0.12.0: "Backend Required" when the session cookie names a user with no active auth row

Findings from reproducing a report that SSO (Keycloak OIDC) stopped working after moving
from Open WebUI 0.11.4 to 0.12.0 while rolling back to 0.11.4 fixed it. The screen shown
was "Open WebUI Backend Required".

Status: the screen and its trigger are reproduced and the source lines are identified.
Whether that trigger is what the original deployment hit is **not confirmed**; see
[What is not proven](#what-is-not-proven).

## Symptom

After the browser is redirected back from the identity provider, the page shows
"Open WebUI Backend Required / Oops! You're using an unsupported method (frontend only)".
That text is misleading: the backend is up.

## Where the message comes from

- `src/routes/error/+page.svelte` renders the string.
- It is reached from exactly one place, `src/routes/+layout.svelte` (v0.12.0, around line
  1372): the root layout calls `getBackendConfig()`, which is `GET /api/config`. If that
  request is not a 2xx (or fails at network level) the layout does `goto('/error')`.

So "Backend Required" means "`GET /api/config` failed in this browser", nothing more.

## Root cause (reproduced)

In 0.11.4, `/api/config` decodes the `token` cookie and, if it yields a user id, looks the
user up. Anything unusable gives a logged-out config (HTTP 200).

In 0.12.0, `backend/open_webui/main.py` (`get_app_config`) additionally calls
`is_valid_token(data, redis)` (commit `2062231f9`, "apply the usual login check when the
app loads its settings"). Since the MFA/session work in the same release,
`is_valid_token` in `backend/open_webui/utils/auth.py` starts with
`get_auth(decoded['id'])` (`backend/open_webui/utils/mfa.py`):

```python
async def get_auth(user_id: str) -> AuthModel:
    try:
        auth = await Auths.get_auth_by_id(user_id)
    except (SQLAlchemyError, ValidationError):
        raise HTTPException(503, 'Authentication is temporarily unavailable.') from None
    if auth is None or not auth.active:
        raise HTTPException(401, 'Invalid authentication request.')
    return auth
```

That is an exception, not a "treat as logged out". It propagates out of `/api/config`, the
browser gets `401 {"detail":"Invalid authentication request."}`, and the layout shows
"Backend Required". Because the login page itself loads `/api/config` first, the browser
cannot reach the login form either until the cookie is cleared or expires (default
`JWT_EXPIRES_IN` is weeks).

The cookie only has to be correctly signed and carry an `id`. An unknown signature, or
garbage, is fine (`decode_token` returns `None`). What breaks it is a **valid** token for a
user that has no `auth` row, or an `auth` row with `active` false, or (503) an `auth` row
that fails the `AuthModel` validation (for example NULL in `email`, `password` or `active`).

Ways to end up there: the user was deleted or recreated while a browser still holds its
cookie, the database was restored from an older copy, or the user was created by a path
that does not create an `auth` row (the SCIM router uses `Users.insert_new_user`).

The same missing row also breaks the SSO callback itself, with a different symptom: for an
existing OAuth user with no `auth` row, `GET /oauth/oidc/callback` returns the raw JSON
`{"detail":"Invalid authentication request."}` (`create_signin_response` calls `get_auth`).
0.11.4 let such a user in.

## Reproduction

Environment: Kubernetes, chart `open-webui/open-webui` 16.6.0 (app 0.11.4) then 17.0.0
(app 0.12.0; the two charts differ only in `appVersion`), 3 replicas, CloudNativePG
database, a dedicated Redis for the websocket manager and token revocation, one
`LoadBalancer` Service, Keycloak OIDC with `OAUTH_SUB_CLAIM=preferred_username`, a shared
`WEBUI_SECRET_KEY`.

1. Sign in once through Keycloak so a user and its `auth` row exist.
2. Mint a token for an id that has no row (or delete the `auth` row of a real user):

   ```python
   # HS256, key = WEBUI_SECRET_KEY, payload {"id": "<uuid not in auth>", "exp": ..., "jti": "x"}
   ```

3. `curl -b "token=$T" http://<host>/api/config`

| Version | `GET /api/config` with that cookie | Browser |
|---|---|---|
| 0.11.4 | `200` | login page |
| 0.12.0 | `401 {"detail":"Invalid authentication request."}` | "Backend Required" |
| 0.12.0, cookie is garbage | `200` | login page |
| 0.12.0, real user, `auth` row deleted, SSO login | callback `401` raw JSON | JSON page |

A real SSO login of a user that has an `auth` row works on both versions, with 3 replicas
and websockets on.

## Check on your own deployment

```sql
-- users that 0.12.0 will reject (no auth row, inactive, or NULL fields that fail validation)
SELECT u.id, u.email
FROM "user" u LEFT JOIN auth a ON a.id = u.id
WHERE a.id IS NULL OR a.active IS NOT TRUE OR a.email IS NULL OR a.password IS NULL;
```

And from a browser that shows the screen, the response of `GET /api/config` in the network
tab: `401` with `Invalid authentication request.` confirms this cause. Any other status
does not.

Workaround: clear the site's cookies; give affected users an `auth` row (or recreate them);
or stay on 0.11.4. Upstream fix direction: have `/api/config` treat a failing
`is_valid_token` as "no user" instead of raising.

## What was ruled out (each tested on 0.12.0 with 3 replicas)

| Hypothesis | Result |
|---|---|
| Authorization request scope differs between versions | Identical: `scope=openid email profile` |
| Keycloak access token loses `scope` when the profile and email client scopes have "Include in token scope" off | Claim becomes `openid`; login still works. The ID token never carries a `scope` claim |
| Redis unavailable | Login still works (`is_valid_token` catches `RedisError` only, but a refused connection is one) |
| Websocket handling | Socket upgrades and handshakes succeed through the Service; not involved in `/api/config` |
| Chart 17.0.0 vs 16.6.0 | Only `appVersion` differs |

## Other behaviours seen on the way

- **Replicas without a shared `WEBUI_SECRET_KEY`** (simulated by setting the key to the pod
  name): 0.12.0 login bounced back to the login form on 3 of 4 attempts. Not tested on
  0.11.4, so this is not shown to be a regression. Set one key for all replicas.
- **No readiness probe in the chart.** Pods report Ready immediately but take about three
  minutes to listen, so during a rollout a share of requests to the Service are refused.
  A failed `/api/config` fetch at that moment also shows "Backend Required", transiently.
- **Rolling back the image alone does not work after 0.12.0 has started once:** it applies
  migrations (`auth.mfa`, `auth.session_stamp`, others), and 0.11.4 does not know the
  resulting revision. Restore a database dump taken before the upgrade.
- On a plain-HTTP origin the 0.12.0 skills editor needs `crypto.randomUUID`, which browsers
  expose only on secure origins (upstream issue 32238). Unrelated to login.

## What is not proven

- That the original deployment's failing `/api/config` returned this 401. That needs the
  network tab (or the SQL above).
- The reported "token has no `scope` claim" observation. Nothing in this reproduction makes
  the version change alter the Keycloak token; it may be a different token from the one
  that matters, or an unrelated client-scope setting.
- Whether older rows (very old releases) have NULL `active` values; the SQL above finds them.
