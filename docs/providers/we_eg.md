# WE (Telecom Egypt) — `we_eg`

**Status: beta — needs testers.** Egypt, EGP. Home internet (FBB: fibre/VDSL/ADSL
sold on a landline).

## What it reads

- The plan and any extra bundles (gaming, streaming, gift data) on the landline:
  size, how much is left, and when each renews.
- Totals are added up into "used" and "remaining" for the dashboard.

It does **not** read bills or a daily history (WE's self-care site does not
expose a per-day internet history the way e& does).

## How signing in works

You type your **home landline number** (with its area code, e.g. `0221234567`)
and your **My WE password** — the same ones you use on
[my.te.eg](https://my.te.eg).

WE almost always shows a **captcha** (a short word in a picture) at sign-in.
Tattle shows you that picture in Settings and you type the characters. Sometimes
WE skips the captcha, and then Tattle signs in with no extra step.

Because a captcha can't be solved automatically, Tattle keeps the WE session
alive between readings (it renews a token quietly). When WE finally ends the
session and wants a new captcha, Settings marks the account "signed out" and
asks you to sign in again.

## Verified vs unverified

Verified against several open-source WE readers (see references), which all make
the same calls:

- Captcha: `POST https://captcha.te.eg/api/Captcha/GenerateCaptcha` with
  `{"merchantName":"E-Care","serviceName":"Login","identifier":"<landline>"}` and
  **no** `Origin`/`Referer` header (sending them gets `InvalidMerchantRequest`).
  The reply has `status`, `token`, `requireInteraction` and a base64 `captcha`.
- Sign-in: `POST .../v1/auth/userAuthenticate` with `acctId = "FBB" + landline`
  (without the leading `0`), `password`, `imgCacheKey` (the captcha token),
  `imgCode` (what you typed), plus the `channelId: 702` / `isSelfcare` headers.
  The reply gives `body.token` (used as the `csrftoken` header afterwards),
  `body.subscriber.subscriberId` and `body.customer.custName`.
- Offerings: `POST .../cz/v1/auth/getSubscribedOfferings` →
  `offeringList[].mainOfferingId`.
- Quota: `POST .../cz/cbs/bb/queryFreeUnit` with
  `{subscriberId, mainOfferId, needQueryPoint:"true"}` → a list of offers, each
  with a `freeUnitBeanDetailList` of buckets carrying `initialAmount`,
  `currentAmount`, `measureUnit`, `expireTime`/`effectiveTime` and
  `remainingDaysForRenewal`.
- Keep-alive: `POST .../cz/v1/common/refreshAppToken` and
  `POST .../cz/v1/user/getUserRoleCz`.

**Unverified / assumed:**

- **Unit base.** WE reports the main total in GB; the per-bucket `measureUnit`
  codes map to B/KB/MB/GB. Whether WE means 1 GB = 1000 MB or 1024 MB is not
  documented, so Tattle uses **1000** (`unit_base = 1000`). If a tester finds the
  figures are off by ~7%, this is the likely cause — please say so in a report.
- **Which bucket is the "plan".** Tattle treats the bucket whose item code is
  `C_TED_Primary_Fixed_Data` as the plan; if no bucket has that code, the first
  data bucket is used. Buckets whose name mentions gaming/streaming are listed
  but not counted toward the total (they are app-specific), matching what the
  reference UIs do.
- **Router matching.** WE's PPPoE user names are usually the landline, e.g.
  `0221234567@tedata.net.eg`. Tattle matches on the digits. The exact format on
  every router model is unverified.

## Known limits

- **Captcha.** Tattle can't solve captchas for you. The session is kept alive to
  avoid asking often, but when WE ends it you have to type a captcha again.
- No bills, no daily history.
- Only one sign-in at a time per WE account (a refresh can retire the old token).

## References

Endpoints and field names were read from these open-source projects (no code was
copied):

- https://github.com/spyisme/we-quota-tracker (`headless.py`) — captcha flow
- https://github.com/mostafaz4/WEUI (`we.js`) — unit codes, refresh/role calls
- https://github.com/YousefMohiey/QuotaVPN (`we_quota.rs`) — header handling
- https://github.com/EyadSofian/we-dashboard, https://github.com/Kareem74x/we-quota-tracker,
  https://github.com/karimawi/TE-QuotaCheck — the 4-step flow

## Tested it?

Please tell us whether the numbers match your My WE page — it helps confirm the
unit base and the renewal dates:
[open a test report](https://github.com/khaledsharara/tattle/issues/new?template=test-report.yml).
