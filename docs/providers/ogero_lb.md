# Ogero — `ogero_lb`

**Status: beta — needs testers.** Lebanon, LBP. DSL home internet.

## What it reads

- For each DSL line on the account: the current bundle, the monthly quota, how
  much has been used, and when Ogero last updated the figure.
- **Bills:** the outstanding amount and recent bills (paid / not paid).

## How signing in works

You type your **My Ogero user name** and **password** (the same ones the Ogero
app uses). When the session runs out, Tattle signs in again with the stored
password.

## Verified vs unverified

**Verified** against [`pyogero`](https://github.com/oraad/pyogero) and its Home
Assistant integration, including their saved HTML fixtures:

- Sign-in: `POST https://ogero.gov.lb/API/Login.php` with form fields
  `Username`/`Password` → JSON `{"SessionID": "..."}`. A wrong password returns
  `400` with `{"error":{"message":...}}`.
- The app pages take the session in the query string, e.g.
  `myogero/mobileapp.dashboard.php?SessionID=..&Username=..&AppRequest&nbr=..&dsl=..`,
  `consumption.php?...&dsl=..`, `bill.php?...&nbr=..`. `AppRequest` is sent with
  no value, as the app does.
- The dashboard's `<select id="changnumber">` lists each phone line (`value`)
  with its DSL account (`value2`).
- Consumption rows live in `div.MyConsumptionGrid` with two `<span>`s; the labels
  are **Current Bundle**, **Total Quota**, **Total Consumption**,
  **Extra Consumption**, **Consumption Until**. `Unlimited` means no cap.
- Bills: the outstanding value in `div.BillOutstandingSection1 span`, and a
  `table.BillTable` of rows with `div.BillDate` (e.g. `May 2026`),
  `div.BillAmount` (e.g. `3,950,000 L.L.`) and a paid/not-paid cell.
- A logged-out page redirects via a script whose `error=` starts with
  "You are required to login"; Tattle treats that as a dead session and signs in
  again.

Tattle parses the HTML with the Python standard library only (no new
dependency), unlike the reference which uses BeautifulSoup.

**Unverified / assumed:**

- **Unit base.** Ogero shows sizes in GB; whether it means 1000- or 1024-based
  is not documented, so Tattle uses **1000** (`unit_base = 1000`).
- **Cycle dates.** Ogero gives no cycle start/end. Quotas are monthly, so Tattle
  takes the cycle to be the **calendar month**. The "Consumption Until" value is
  read but the renewal is the first of next month.
- Bill due dates and issue dates are not in the table, so they are left empty.

## Known limits

- Monthly cycle is assumed to be the calendar month.
- Prepaid/postpaid differences in the pages are not all covered; a test report
  with the HTML labels you see helps.

## References

- https://github.com/oraad/pyogero — the login + page flow and HTML fixtures
- https://github.com/oraad/ha-ogero — the Home Assistant integration

## Tested it?

Please confirm the quota and consumption labels match, and whether bills show up:
[open a test report](https://github.com/khaledsharara/tattle/issues/new?template=test-report.yml).
