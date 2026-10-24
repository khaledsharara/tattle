# Telenet / BASE — `telenet_be`

**Status: beta — needs testers.** Belgium, EUR. Cable/fibre home internet
(Telenet and its BASE brand share the same back end).

## What it reads

- The internet product on the plan: the data allocation and how much is left
  this bill cycle, with the cycle's renewal date.
- A **daily history** of data used per day in the current bill cycle.

The quota behaviour depends on the plan category:

- **CAP** plans have a hard allocation (shown as total + remaining).
- **FUP / TURBO** plans slow down past the allocation rather than stopping; the
  allocation is still shown as the total.
- **UNLIMITED** plans have no allocation; Tattle shows usage only.

No bills.

## How signing in works

You type your **My Telenet / My BASE user name** and **password**, and choose
**telenet** or **base** (leave empty for Telenet). Tattle signs in through
Telenet's hosted Okta login and keeps the session cookies; when they stop
working it signs in again with the stored password.

## Verified vs unverified

**Verified** against two independent clients — the Home Assistant
[`telenet_telemeter`](https://github.com/myTselectionPublic/telenet_telemeter)
and the VikingBar macOS client — which agree on the flow:

- Okta identity-engine sign-in on `secure.telenet.be` / `secure.base.be`: the
  authorization page carries a `stateToken`; `POST /idp/idx/introspect` →
  `stateHandle`; `POST /idp/idx/identify` (user name) names the `password`
  authenticator; `POST /idp/idx/challenge` then `/challenge/answer` (password);
  the `success.href` link is followed back to the API.
- Products: `GET /ocapi/public/api/product-service/v1/product-subscriptions?producttypes=PLAN`
  (internet products, including the ones inside a bundle).
- Bill cycle: `GET /ocapi/public/api/billing-service/v1/account/products/{id}/billcycle-details?producttype=internet&count=3`
  → `billCycles[0].startDate/endDate`.
- Usage: `GET .../products/internet/{id}/usage?fromDate=..&toDate=..` →
  `internet.category`, `internet.totalUsage.units`, `internet.allocatedUsage.units`
  (all in GB).
- Daily: `GET .../products/internet/{id}/dailyusage?billcycle=CURRENT&fromDate=..&toDate=..`
  → `internetUsage[0].dailyUsages[]` with `date`, `total`, `peak`, `offPeak`.

**Unverified / assumed:**

- **Unit base.** The reference converts Telenet's KB counters to GB dividing by
  1024 twice, so Tattle uses `unit_base = 1024` and treats the GB figures as
  GiB. Not formally documented.
- **Daily field names.** The current HA version fetches the daily endpoint but
  does not parse per-day rows; the `dailyUsages[].{date,total,peak,offPeak}`
  shape comes from the VikingBar client. If your account's daily history is
  empty, the shape may differ — please report it.
- Which product in a bundle is "the internet one" is chosen by `productType`.

## Known limits

- **Password sign-in only.** Accounts that sign in with a one-time code or an
  authenticator app (instead of a password) are not supported yet; Tattle says
  so plainly.
- The daily history covers the current bill cycle only (what Telenet keeps).
- No bills.

## References

- https://github.com/myTselectionPublic/telenet_telemeter — the Okta flow and
  the `/ocapi` endpoints
- VikingBar (BramVR/VikingBar) — a second, independent Telenet client; the
  daily-usage row shape and the category values

## Tested it?

Please tell us your plan category (CAP/FUP/TURBO/UNLIMITED), whether the daily
history filled in, and whether BASE works as well as Telenet:
[open a test report](https://github.com/khaledsharara/tattle/issues/new?template=test-report.yml).
