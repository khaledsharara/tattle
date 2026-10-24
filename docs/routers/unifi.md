# UniFi Network — `unifi`

**Status: beta — untested on real hardware, testers wanted.** Signing in and
the error cases were tested against a self-hosted UniFi Network server
(10.0.162) with no devices adopted; UniFi OS consoles and real client counters
were not tested.

## What it reads

The connected clients list (`stat/sta`) of one site: for each client its MAC,
IP address, name and download/upload totals. Works with:

- **UniFi OS consoles**: Dream Machine, Dream Router, Cloud Gateway, Cloud Key
  Gen2 and similar (sign-in at `/api/auth/login`, the Network API under
  `/proxy/network`).
- **Self-hosted UniFi Network servers** (the Network Application on your own
  computer, usually on port 8443; sign-in at `/api/login`).

Tattle tells them apart by asking for `/`: consoles answer with their sign-in
page, self-hosted servers redirect to `/manage`.

No internet-side totals: the site's health summary only has current WAN
*rates*, not running totals.

## What it counts

UniFi's access points and switches count **everything a client sends and
receives**, so **traffic that stays in the house is included**: casting to the
TV, backups to a NAS, AirPlay, games on the LAN. Per-device numbers can add up
to more than your provider charges.

- Wireless clients: `tx_bytes` (what the access point sent the client, so its
  download) and `rx_bytes` (its upload).
- Wired clients: `wired-tx_bytes` and `wired-rx_bytes`, which UniFi's own
  client list shows as their Down and Up.

Only clients connected right now are listed. Totals restart when a client
reconnects; Tattle treats that as a restart and keeps counting. What a client
used between Tattle's last reading and leaving is lost.

## Set up UniFi

Make an account just for Tattle that can only look:

- **Console:** in UniFi OS, **Admins & Users › Create New Admin**. Tick
  **Restrict to Local Access Only** (cloud accounts with two-factor sign-in
  can't be used), give it **View Only** for the Network application and no
  access to the others.
- **Self-hosted:** **Settings › Admins & Users**, add an admin with the
  **View Only** (read-only) role.

Or, on a console with UniFi Network 9 or newer, create an **API key** under
**Settings › Control Plane › Integrations** and paste it instead of the user
name and password.

The site is the short name in the address bar when you open it
(`/manage/<site>/...`, or `/network/<site>/...` on consoles). Most homes have
only `default`. If you get it wrong, the error lists the sites the controller
has.

## The fields

| Field | What to put |
| --- | --- |
| Controller address | The console's address (`https://192.168.1.1`), or your server's with its port (`https://192.168.1.10:8443`) |
| User name / Password | The local View Only account |
| API key | Instead of user name and password, on consoles |
| Site | `default`, unless you renamed or added sites |
| Check the certificate | `no` for UniFi's own self-signed certificate; `yes` only for one this computer trusts |

## Verified vs unverified

**Verified:**

- **Direction**, from Ubiquiti's own web interface (UniFi Network Application
  10.6.109, `app.js`): the clients table's *Down* column shows `tx_bytes` for
  wireless clients and `wired-tx_bytes` for wired ones, *Up* shows `rx_bytes`
  and `wired-rx_bytes`. For wired clients, the UI labels the plain
  `tx_bytes`/`rx_bytes` "LAN Traffic", so Tattle uses the `wired-` counters.
- Against a self-hosted server 10.0.162, with Tattle's adapter code: `/`
  redirects (302); sign-in at `/api/login`; a wrong password is
  400 `api.err.Invalid`; no session is 401 `api.err.LoginRequired`; signing in
  again after the session was lost; a **View Only** admin can read `stat/sta`;
  a wrong site is 401 `api.err.NoSiteContext` (Tattle does not mistake it for
  a lost session); self-signed certificate. Also: a self-hosted server answers
  the console sign-in path with 401 `api.err.LoginRequired` rather than 404,
  so Tattle falls back to `/api/login` on that answer.

**Unverified (taken from the unpoller library, not tested):**

- Console sign-in: `/` answering 200, `POST /api/auth/login`, the
  `x-csrf-token` header sent back on later requests, the `/proxy/network`
  prefix.
- API keys (`X-API-Key`) on the classic `stat/sta` endpoint.
- The answers for too many failed sign-ins (429) and two-factor accounts.
- Real counters from real clients, and what happens to them when a client
  roams between access points.

## Known limits

- Counts in-house traffic (see above).
- Clients behind a non-UniFi switch or access point may be missing or counted
  only partly.
- Not through `unifi.ui.com` (remote access): Tattle must reach the console or
  server on your network.

## References

- [unpoller/unifi](https://github.com/unpoller/unifi) (Go): `unifi.go` (`Login`,
  `checkNewStyleAPI`, CSRF handling), `types.go` (`path`, API paths),
  `clients.go` (client fields), `site.go` (health fields: rates only).
- Ubiquiti UniFi Network Application 10.6.109 web interface (`app.js`,
  `getTxActivity` / `getRxActivity` and the clients table columns).
- [Ubiquiti Community Wiki: UniFi Controller API](https://ubntwiki.com/products/software/unifi-controller/api).

## Tested it?

Please tell us whether you have a console (which one) or a self-hosted server,
its Network version, and whether a phone's numbers look right after a known
download:
[open a test report](https://github.com/khaledsharara/tattle/issues/new?template=test-report.yml).
