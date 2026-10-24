# Xfinity (Comcast) — `xfinity_us`

**Status: beta — needs testers.** USA, USD. Xfinity home internet.

## What it reads

- The current month's internet data: how much was used, how much is allowed,
  the days left, and the renewal date.
- **Many newer Xfinity plans are unlimited.** Then there is no allowance and
  Tattle shows usage only ("No quota (unlimited plan)").

No bills, no daily history.

## How signing in works

Xfinity signs in through its own hosted login, so Tattle can't take your
password directly. Instead:

1. Settings shows a **link**. Open it in your own browser and sign in to Xfinity.
2. The page then tries to redirect to an app address that starts with
   `xfinitydigitalhome://` — your browser can't open it, so it stops there.
3. Copy that whole address (or the `oi-…` code inside it) and paste it back into
   Settings.

Tattle trades the code for tokens and from then on refreshes quietly with the
refresh token. If the refresh token ever stops working, Settings asks you to do
the paste step again.

> Tip: open your browser's developer tools (Network tab, "preserve log") before
> signing in, so you can read the redirect's `Location` header. Use the `oi-…`
> code there, **not** the `0.ac…` code in the address bar (that one is an
> intermediate step).

## Verified vs unverified

**Verified** against [`hassio-xfinity-usage`](https://github.com/thor0215/hassio-xfinity-usage)
and [`csobrinho/xfinity-usage`](https://github.com/csobrinho/xfinity-usage):

- OAuth with PKCE. Authorize at
  `https://xerxes-sub.xerxessecure.com/xerxes-ctrl/oauth/authorize` with
  `client_id=xfinity-android-application`, `redirect_uri=xfinitydigitalhome://auth`,
  `response_type=code`, `code_challenge`/`code_challenge_method=S256`,
  `activity_id`, `state`, `scope=profile`.
- Token exchange / refresh at `.../oauth/token` (`grant_type=authorization_code`
  with the `code_verifier`, then `grant_type=refresh_token`).
- Usage: `POST https://gw.api.dh.comcast.com/galileo/graphql`, operation
  `InternetDataUsage`, with the Android app's `client`/`x-apollo-*` headers →
  `monthlyUsage[]` with `policy`, `startDate`, `endDate`, `daysRemaining`,
  `currentUsage{value,unit}`, `allowableUsage{value,unit}`. The last entry is
  the current month.
- Units are decimal: `MB`/`GB`/`TB` convert by 1000 (`unit_base = 1000`).

**Unverified / assumed — the riskiest part:**

- **Client secret at the token endpoint.** Both references send a client secret
  with the token request (the Android app's secret, which they ship encrypted).
  Tattle relies on the PKCE `code_verifier` instead and sends **no** secret,
  which is how a public PKCE client is supposed to work. If Xfinity still
  requires the secret, the token exchange will fail with a clear message. This
  is the most likely thing to need adjusting — a test report saying whether the
  paste step completes is the key signal.
- Exactly what you can paste back (full redirect URL vs bare code) depends on
  your browser; Tattle accepts either and looks for the `oi-<32 hex>` code.

## Known limits

- The sign-in needs a manual browser step and a paste; it can't be fully
  automatic because Xfinity controls the login.
- New unlimited plans show usage only.
- No bills, no daily history.

## References

- https://github.com/thor0215/hassio-xfinity-usage — current OAuth + GraphQL flow
- https://github.com/csobrinho/xfinity-usage — the same flow in Go, incl. unit conversion

## Tested it?

Please tell us whether the paste step completed (and whether a client secret was
needed), and whether your plan is capped or unlimited:
[open a test report](https://github.com/khaledsharara/tattle/issues/new?template=test-report.yml).
