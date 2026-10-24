# MikroTik RouterOS 7 (REST API) — `mikrotik_rest`

**Status: beta — untested on real hardware, testers wanted.** Tested against
RouterOS 7.25 CHR (the x86 virtual machine image), not on a MikroTik box.

## What it reads

Through the REST API (`https://<router>/rest/...`, RouterOS 7.1 or newer):

- **Per-device totals** from Kid Control (`/ip/kid-control/device`):
  `bytes-down` (what the device downloaded) and `bytes-up`, with the device's
  IP address. Names come from the device entry or the DHCP lease's host name.
- **Internet counters**: `rx-byte` / `tx-byte` of the interface you name as
  the internet interface (`ether1` by default, or `pppoe-out1`).
- **Internet login**: the PPPoE client's user name, when the router dials
  PPPoE itself. A read-only account sees the user name; RouterOS hides the
  PPPoE password from it.

## What it counts

Kid Control counts what each device exchanges *through the router*:

- **Counted:** internet traffic, and also **traffic between the device and the
  router itself** (DNS queries to the router, its web pages, WinBox). We
  checked this: 85 KB of requests to the router's own web server showed up on
  the device. Usually small, but it is not internet traffic.
- **Probably counted:** traffic routed between your own subnets or VLANs.
- **Probably not counted:** device to device on the same bridge (switched, not
  routed). Unverified.

Kid Control lists every neighbour in the ARP table, including your modem or
the ISP's equipment on the internet side. Tattle leaves out devices it sees
on the internet interface (and, behind PPPoE, on the port the PPPoE client
dials over), so the modem doesn't show up as a device.

Totals restart when the router reboots and when a device's entry is dropped
and comes back; Tattle treats that as a restart and keeps counting.

## Set up the router

RouterOS **7.12 or newer** is recommended: 7.1 to 7.11 had an access-control
flaw in the REST API (CVE-2023-41570). Run these in **Terminal** (WinBox,
WebFig or SSH).

1. **Give Kid Control a profile.** RouterOS keeps per-device counters only once
   at least one kid profile exists; without one, the device list is empty. This
   profile has no devices and allows every hour of every day, so it restricts
   nobody:

   ```
   /ip kid-control add name=tattle mon=0s-1d tue=0s-1d wed=0s-1d thu=0s-1d fri=0s-1d sat=0s-1d sun=0s-1d
   ```

   (A profile made with just `name=tattle` gets default hours, for example
   7h–21h on Mondays. That restricts nobody either while no device is assigned
   to it, but the line above stays harmless even if one is.) Check with
   `/ip kid-control device print stats`: your devices should be listed.

2. **Make a read-only user for Tattle** (choose your own password). REST needs
   the `api` and `rest-api` policies as well as `read`:

   ```
   /user group add name=tattle-read policy=read,api,rest-api
   /user add name=tattle group=tattle-read password="choose-a-long-password"
   ```

   Optional: let that user in only from the Tattle server, e.g.
   `/user set tattle address=192.168.88.10/32`. If your RouterOS rejects the
   `rest-api` policy, it predates it: use `policy=read,api`.

3. **Turn on HTTPS** (the `www-ssl` service) with a certificate the router
   makes for itself:

   ```
   /certificate add name=tattle-https common-name=router days-valid=3650
   /certificate sign tattle-https
   /ip service set www-ssl certificate=tattle-https disabled=no
   ```

   Plain `http://` also works from RouterOS 7.9 (the `www` service), but sends
   the password unencrypted across your network.

## The fields

| Field | What to put |
| --- | --- |
| Router address | `https://192.168.88.1` (your router's address) |
| User name / Password | The read-only user from step 2 |
| Internet interface | The interface facing the modem: `ether1`, or `pppoe-out1` when the router dials PPPoE |
| Check the router's certificate | `no` for the router's own certificate; `yes` only for one this computer trusts |

## Verified vs unverified

**Verified** on RouterOS 7.25 CHR with Tattle's adapter code, a LAN device
routed through the router and a NAT to the internet:

- Field names, and that REST sends **every value as a string**
  (`"bytes-down": "13656815"`), MACs in upper case.
- With no kid profile, `/ip/kid-control/device` is empty; with one, every
  ARP neighbour is listed dynamically, including the internet-side gateway and
  IPv6-only neighbours. A bare profile adds no firewall rules or queues.
- **Direction:** 5 MB downloaded by the LAN device raised `bytes-down` by about
  5 MB; a 1.5 MB upload raised `bytes-up` by about 1.5 MB.
- Traffic between a device and the router itself is counted (see above).
- The read-only group: reads everything Tattle needs, writes are refused
  ("not enough permissions"), `read,rest-api` without `api` is refused, the
  `address=` limit works. Wrong password → 401.
- `www-ssl` with a self-signed certificate; `?name=` and `.proplist` filters;
  the PPPoE client's `user`; interface `rx-byte` / `tx-byte`; the setup
  commands above, exactly as written.

**Unverified:**

- Real hardware, and RouterOS versions other than 7.25.
- **FastTrack.** Home MikroTik configs enable FastTrack by default. On the CHR
  the fast path never engaged, so we could not tell whether fast-tracked
  packets still reach Kid Control's counters. If they don't, devices are
  under-counted. Testers: compare with **Interfaces** totals or your
  provider's figures, with the FastTrack rule enabled and disabled.
- Hardware offloading (L3HW on switch chips) moves traffic off the CPU and
  very likely past these counters.
- Device-to-device traffic on the same bridge (believed not counted).
- DHCP lease `host-name` as a device name (taken from the RouterOS manual; our
  test network had no DHCP client sending a name).

## Known limits

- Kid Control must have a profile (step 1).
- Devices behind a MAC-NAT extender count as one device; see
  [what router mode can't do](README.md#what-router-mode-cant-do-and-gateway-mode-can).
- With several PPPoE clients, Tattle reports the one named as the internet
  interface, else the running one.

## References

- RouterOS manual: [Kid Control](https://manual.mikrotik.com/docs/firewall-and-quality-of-service/kid-control)
  (`bytes-down`, `bytes-up`, devices from the ARP table),
  [REST API](https://manual.mikrotik.com/docs/7.25/developer-guides/rest-api)
  (Basic auth, `www-ssl`, `www` from 7.9, the `api` + `rest-api` policies),
  [User](https://help.mikrotik.com/docs/spaces/ROS/pages/8978504/User) (groups and policies).
- [CVE-2023-41570](https://nvd.nist.gov/vuln/detail/CVE-2023-41570) (REST API
  access control, RouterOS 7.1–7.11).

## Tested it?

Please tell us your model and RouterOS version, whether FastTrack is on, and
whether the per-device numbers look right:
[open a test report](https://github.com/khaledsharara/tattle/issues/new?template=test-report.yml).
