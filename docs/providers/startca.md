# Start.ca — `startca`

**Status: beta — needs testers.** Canada, CAD. Home internet.

## What it reads

- The bytes used this cycle (download + upload, including any "grace" usage).
- If you give your plan's monthly cap, Tattle shows how much is left; otherwise
  it shows usage only.

No bills, no daily history.

## How signing in works

There is no password here. Start.ca gives each account a **usage API key** (in
My Account on start.ca). You paste that key into Settings. Optionally you can add:

- **Monthly cap in GB** — so Tattle can show what's left (leave empty for an
  unlimited plan).
- **Billing day of the month** — if your cycle doesn't reset on the 1st.

The key is stored on your own server and sent to Start.ca with each reading.

## Verified vs unverified

**Verified** against the Home Assistant `startca` component:

- `GET https://www.start.ca/support/usage/api?key=<API key>` returns an XML
  document: `usage > used | grace | total`, each with `download` and `upload`
  **in bytes**. No unit conversion is needed.

**Unverified / assumed:**

- **No cap in the response.** The API does not include your plan's cap, so it is
  a field you type. Without it, "remaining" is unknown.
- **No cycle dates.** The response has no cycle start/end. Start.ca bills
  monthly, so Tattle uses the **calendar month**, or a cycle anchored on the
  **billing day** you type. Which is right for your account is unverified — tell
  us if your reset day differs.
- **What counts as used.** Tattle adds `used` + `grace` for the "used" figure.
  Whether grace should count toward the cap is a judgement call; say so in a
  report if it looks wrong for you.

## Known limits

- You must supply the cap yourself to see "remaining".
- Cycle is assumed monthly (calendar month or your billing day).
- No bills, no daily history.

## References

- Home Assistant core `homeassistant/components/startca` (`sensor.py`) — the XML
  shape and the bytes-to-GB handling.

## Tested it?

Please confirm the usage matches your Start.ca page, and tell us your reset day
if it isn't the 1st:
[open a test report](https://github.com/khaledsharara/tattle/issues/new?template=test-report.yml).
