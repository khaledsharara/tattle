# e& (Etisalat) Egypt

**Status: stable.** Used every day on a real home line since October 2026.

## What Tattle reads

For every home internet line on the account (and lines linked to it):

- each bundle: the plan and any extra bundles bought on top, with what is left and the plan's renewal date;
- the line's **daily usage** for about the last month (Tattle keeps every day it reads, so its record grows past e&'s month);
- the billing account's **open amount and recent bills** (every few hours).

Mobile numbers on the same account are skipped; only home lines (landlines) are read.

## Signing in

1. In **Settings › Internet provider**, choose *e& (Etisalat) Egypt* and type the home landline, starting with 0 (for example `0221234567`).
2. e& texts a sign-in code to **the mobile number on that line's contract**. Type it into Tattle.

That's it: Tattle keeps itself signed in with e&'s refresh token, the way the self-care website does. If e& ever signs it out (rare; for example after a password change on the account), the Overview says so and Settings shows **Sign in again**.

One sign-in shows every line the account can see, so a household with two lines on one contract signs in once.

## Good to know

- **1 GB = 1024 MB.** e& counts in binary units: a used-up 750 GB plan's daily history adds up to about 768,000 MB. Tattle uses the same unit everywhere once e& is connected, so its figures match the My Etisalat app.
- **Extra bundles** come with what is left but not their size; Tattle shows what is left.
- **Two lines, one connection.** If you switch the router's internet login between two lines (two cheaper plans instead of a big one), Tattle follows the switch: with the D-Link DSL-245GE adapter it reads the router's login (`ETIS_<landline>`); without it, it sees which line's usage is moving. The quota meter and "This cycle" always follow the line in use.
- **Renewal day.** Nothing to type: the cycle runs from the date the plan last renewed to the renewal date e& shows. On renewal day, until e& shows the new date, the meter says *Renewing…* instead of showing last cycle's figures.
- The D-Link DSL-245GE that e& hands out has an adapter ([docs/routers/dlink_dsl245ge.md](../routers/dlink_dsl245ge.md)) that adds the line's sync speed and which line the router is signed in with.

## How it works

Tattle talks to `www.eand.com.eg` the way the self-care site at `/selfcare/` does: `sendOTP` and `login` under `/eshopapi/selfcare/api/v1/auth` for the tokens, then the XML endpoints under `/Saytar/rest/selfCare/` for the account profile (`user/account`), usage (`servicemanagement/getGenericConsumptionsV2`), daily history (`services/accountHistoryV2`) and bills (`customerprofile/openAmount`, `customerprofile/customerbillV2`). The code is [backend/tattle/providers/eand_eg.py](../../backend/tattle/providers/eand_eg.py).

To see e&'s raw answers for your own account (for example to find a field Tattle doesn't read yet):

```bash
docker compose exec tattle python -m tattle.providers.eand_eg dump
```

The output contains your numbers and account details: don't paste it anywhere public.
