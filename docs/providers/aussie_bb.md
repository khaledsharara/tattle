# Aussie Broadband — `aussie_bb`

**Status: beta — needs testers.** Australia, AUD. NBN / Opticomm home internet.

## What it reads

- For each NBN/Opticomm service on the account: the data used this billing
  period and the days left in the period.
- If the plan has a quota, "remaining" is shown too. **Most Aussie plans are
  unlimited**, so usually there is no quota and Tattle shows only what you used
  (the plan is listed as "No quota (unlimited plan)").

No bills, no daily history.

## How signing in works

You type your **My Aussie user name** (usually your e-mail) and **password**.
Tattle signs in and keeps the session's cookie; when it runs out Tattle signs in
again with the stored password.

Some accounts have **two-factor** turned on. If Aussie asks for it at sign-in,
Tattle sends a code by SMS and asks you to type it in Settings. (Usage reads
themselves do not need a code; two-factor mainly guards changes.)

## Verified vs unverified

**Verified** against [`pyaussiebb`](https://github.com/yaleman/pyaussiebb), the
best-maintained client, and the Home Assistant `aussie_broadband` integration:

- Sign-in: `POST https://myaussie-auth.aussiebroadband.com.au/login` with
  `{"username","password"}`. The reply has `expiresIn` (session lifetime) and a
  `myaussie_cookie` cookie that the API accepts.
- Services: `GET https://myaussie-api.aussiebroadband.com.au/services` → `data[]`
  with `service_id`, `type` (`NBN`, `Opticomm`, `PhoneMobile`, …), `plan`.
- Usage: `GET .../broadband/{service_id}/usage` → `usedMb`, `downloadedMb`,
  `uploadedMb`, `daysTotal`, `daysRemaining`.
- Two-factor: `POST /2fa/send` (`{"method":"sms"}`) and `POST /2fa/verify`
  (`{"token":"..."}`) with header `x-two-factor-auth-capable-client: true`.

**Unverified / assumed:**

- **Unit base.** `usedMb` is in MB; Tattle treats 1 MB as 1,000,000 bytes
  (`unit_base = 1000`). Whether Aussie means MiB is not documented.
- **Quota field.** On the rare capped plan, Tattle reads `remainingMb` to work
  out the total. The exact field name on a capped plan is unverified (unlimited
  plans, which are the common case, simply omit it).
- **Two-factor at sign-in.** `pyaussiebb` exercises 2FA mainly for plan changes.
  Whether a usage-only sign-in ever triggers it is unverified; Tattle models it
  defensively (it only asks if the login reply mentions two-factor).
- **Cycle dates.** Derived from `daysTotal`/`daysRemaining` relative to today,
  not from explicit dates.

## Known limits

- Unlimited plans show usage only, not a percentage.
- Aussie rate-limits its API. A reading every ~15 minutes is well within it;
  Tattle signs in again only when the session has expired. If you see a "slow
  down" message, it will clear on the next round.
- No bills, no daily history.

## References

- https://github.com/yaleman/pyaussiebb — endpoints, fields, MFA
- Home Assistant `aussie_broadband` integration — service/usage shapes

## Tested it?

Please confirm the service list and the MB figures, and tell us your plan type
(unlimited vs capped):
[open a test report](https://github.com/khaledsharara/tattle/issues/new?template=test-report.yml).
