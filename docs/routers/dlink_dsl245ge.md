# D-Link DSL-245GE (e& Egypt) — `dlink_dsl245ge`

**Status: stable.** The VDSL modem-router e& Egypt hands out, firmware
ET_1.02b01; in daily use with Tattle.

## What it reads

- **The internet login**: the PPPoE user name the router signs in to e& with,
  e.g. `ETIS_0221234567`. It carries the landline number, so with two lines on
  one e& account Tattle knows which line the house is using right now.
- **The DSL line**: whether it is up, and the upload and download speeds it
  synced at.
- **The router's own internet counters** (bytes received and sent). Tattle
  shows them next to its own count as a cross-check. They are 32-bit and wrap
  around after 4 GiB; Tattle accounts for that.
- **Whether the router's DHCP server is on.** Gateway mode needs it off, and
  Tattle checks that it is.

## What it can't do

**No per-device counts.** The router's web interface keeps no traffic counters
per device, so this router does not give you router mode. For traffic per
device with this router, use gateway mode (see the
[main README](../../README.md#pick-a-mode)).

## Set up

Nothing to change on the router. In Tattle, fill in:

| Field | What to put |
| --- | --- |
| Router address | `http://192.168.1.1` |
| User name | The account you sign in to the router's web page with |
| Password | Its password |

## How it signs in

The router has no session cookie: it remembers signed-in clients by IP address.
Tattle signs in with the password's MD5 hash in the sign-in request, the way
the router's own page does. Opening the router's web page from the Tattle
server's address ends that sign-in, so Tattle signs in again whenever a page
comes back without its data. That is harmless; you may see Tattle's sign-ins
in the router's log.

## Verified vs unverified

**Verified** on the router (firmware ET_1.02b01): all four readings above, and
signing in again after the web page ended the session.

**Unverified:** other firmware versions, and the same model sold by other
providers.

## Tested it?

On a different firmware, or the same router from another provider? Please
[open a test report](https://github.com/khaledsharara/tattle/issues/new?template=test-report.yml).
