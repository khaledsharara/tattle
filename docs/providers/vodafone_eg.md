# Vodafone Egypt home internet — `vodafone_eg`

**Status: beta — needs testers.** Egypt, EGP. Home internet (ADSL/VDSL sold on a
Vodafone account).

## What it reads

- The home internet bundle(s) on the account: total, how much is left, and the
  renewal date. The main bundle is counted toward "used"/"remaining".

No bills, no daily history.

## How signing in works

You type the **Vodafone mobile number** you sign in with (local form,
`01012345678`), your **Vodafone password**, and optionally your **landline**
number if Vodafone lists the home internet under it.

Tattle signs in the way the Vodafone website does and keeps the password so it
can sign in again when the session ends (self-hosted; the password stays on your
own server).

## Verified vs unverified

**Verified** (website sign-in flow, from the references):

- Keycloak realm `vf-realm` at `https://web.vodafone.com.eg/auth/realms/vf-realm/protocol/openid-connect`:
  open `/auth` with client `website`, post `username`/`password` to the form's
  action URL, read the `code` from the redirect to `/ar/KClogin?code=...`, then
  `POST /token` with `grant_type=authorization_code`. Refresh with
  `grant_type=refresh_token`.
- There is also an **Ana Vodafone app** flow (`mobile.vodafone.com.eg`,
  `grant_type=password`, client `my-vodafone-app` with a public client secret)
  used by many community bots. Tattle uses the **website** flow because it needs
  no app secret and is the one the ADSL page itself uses.
- The mobile-internet profile is read at
  `GET /services/dxl/pim/product?relatedParty.id={msisdn}&place.@referredType=Local&@type=MIProfile`
  with `Authorization: Bearer`, `msisdn` and `clientId: WebsiteConsumer` headers,
  and parsed as
  `parts.productOffering[].productOfferingTerm[].quota{consumed,total,amount,units}`
  with `validFor.endDateTime`.

**Unverified / assumed — the riskiest part of this provider:**

- **The ADSL query string.** The reference that reads ADSL captures the request
  the browser makes rather than building it, so the exact query is not in any
  source. Tattle requests `...@type=ADSLProfile` by analogy with the verified
  `MIProfile` query (`relatedParty.id`, `place.@referredType=Local`). If Vodafone
  wants a different parameter, `usage` will not find a bundle and Tattle shows a
  clear "did not return home internet usage" message rather than crashing. **This
  is the most likely thing to need fixing — a test report with what works is very
  welcome.**
- **Unit base.** The ADSL profile often omits the unit while the numbers are in
  KB of **1024** (the reference notes `209,715,200` = 200 GB). Tattle treats a
  missing unit as KB and uses `unit_base = 1024`.
- **Renewal date.** Taken from `validFor.endDateTime`; an end at the last second
  of a day is treated as renewing the next day.

## Known limits

- The ADSL read may not match every account type (see unverified above).
- Accounts that require an SMS/OTP step at sign-in are not supported.
- No bills, no daily history.

## References

- https://github.com/ahmedgomaa2004/adsl — ADSL profile parsing and unit note
- https://github.com/LOLQQ1/voda-bot (`vodafone_client.py`) — website auth-code flow
- https://github.com/grimpy/ispapi — legacy Vodafone Egypt provider
- Community bots using the app password grant (client `my-vodafone-app`): the
  `MIProfile` query string and app client id/secret.

## Tested it?

Especially: does the ADSL read return your bundle, and are the GB figures right?
[Open a test report](https://github.com/khaledsharara/tattle/issues/new?template=test-report.yml).
