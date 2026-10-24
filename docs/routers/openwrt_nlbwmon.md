# OpenWrt (nlbwmon) — `openwrt_nlbwmon`

**Status: beta — untested on real hardware, testers wanted.** Tested against
OpenWrt 25.12.6 (x86-64) running in a container, not on a router.

## What it reads

Per-device download and upload totals from **nlbwmon**, the traffic accounting
package behind LuCI's *Bandwidth Monitor* page, for nlbwmon's current
accounting period. Tattle runs `nlbw -c json -g mac,ip` on the router and adds
up each device's IPv4 and IPv6 rows by MAC address.

No internet-side totals, internet login or line status (yet).

## What it counts

**Internet traffic only.** nlbwmon follows the router's connection tracking and
counts a connection only when one end is in its `local_network` list and the
other end is not. So:

- **Not counted:** device to device (casting, file copies; those packets never
  reach the router's connection tracking), and device to the router itself
  (DNS, the LuCI pages). We checked the second one: downloading 3 MB from the
  router's own web server added nothing.
- **Not counted:** the router's own internet traffic (package updates, NTP),
  since it belongs to no LAN device.
- **Counted:** IPv4 and IPv6, matched to a device through the router's
  neighbour table. When nlbwmon can't find a connection's MAC (it shows as
  `00:00:00:00:00:00`), Tattle can't book it on a device and leaves it out.

The default `local_network` list is `192.168.0.0/16`, `172.16.0.0/12`,
`10.0.0.0/8` and `lan`. If your LAN uses other addresses, add them in
**Network › Bandwidth Monitor › Configuration**.

Totals **start again from zero** at the start of each accounting period
(the 1st of the month by default) and when the router reboots (nlbwmon keeps
its database in RAM by default). Tattle treats that as a restart and keeps
counting. nlbwmon refreshes the totals of open connections every 30 seconds,
so a reading can be up to half a minute behind.

## Set up the router

1. **Install nlbwmon** (and its LuCI page). In LuCI: **System › Software**, or
   over SSH:

   ```sh
   # OpenWrt 25.12 and newer
   apk update && apk add nlbwmon luci-app-nlbwmon
   # OpenWrt 24.10 and older
   opkg update && opkg install nlbwmon luci-app-nlbwmon
   ```

   Check **Network › Bandwidth Monitor** shows your devices after a few minutes.

2. **Pick how Tattle signs in**: the router's web sign-in (easiest) or an SSH
   key (works without LuCI). Both run the same `nlbw` command.

### Way 1: the web sign-in

Fill in the router's address, user `root` and the LuCI password. Tattle signs
in the way LuCI does (uhttpd's `/ubus` JSON-RPC and an rpcd session) and runs
luci-app-nlbwmon's helper, which LuCI's admins may already run. If the router
redirects to HTTPS, Tattle follows; it does not check the certificate (OpenWrt
makes its own, which nothing else trusts).

**Better: an account that can only read the counters.** Tattle then never holds
your root password. Over SSH on the router (choose your own password):

```sh
cat > /usr/share/rpcd/acl.d/tattle.json <<'EOF'
{
	"tattle": {
		"description": "Tattle: read the nlbwmon traffic counters",
		"read": {
			"ubus": { "file": [ "exec" ] },
			"file": { "/usr/sbin/nlbw -c json -g mac,ip": [ "exec" ] }
		}
	}
}
EOF
uci add rpcd login
uci set rpcd.@login[-1].username='tattle'
uci set rpcd.@login[-1].password="$(uhttpd -m 'choose-a-long-password')"
uci add_list rpcd.@login[-1].read='tattle'
uci commit rpcd
/etc/init.d/rpcd restart
```

Then use user `tattle` and that password in Tattle. Through the web API this
account can run that one command with exactly those arguments and nothing
else: no other program, no settings. (rpcd checks `file exec` against the
command *and* its arguments.)

### Way 2: an SSH key

1. Make a key without a passphrase, on any computer:

   ```sh
   ssh-keygen -t ed25519 -N '' -C tattle -f tattle-openwrt
   ```

   This writes `tattle-openwrt` (private) and `tattle-openwrt.pub` (public).

2. On the router, **System › Administration › SSH-Keys**, add the public key
   with a restriction in front, so the key can only read the counters:

   ```
   command="/usr/sbin/nlbw -c json -g mac,ip",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA...rest-of-the-key... tattle
   ```

   (or append that line to `/etc/dropbear/authorized_keys`). LuCI accepts key
   lines with options like these. Dropbear then runs that command whatever the
   client asks for.

3. In Tattle, paste the whole **private** key (`tattle-openwrt`, from
   `-----BEGIN` to the `-----END` line) into **SSH private key**, user `root`.
   Leave the password empty.

The first connection trusts the router's SSH host key and remembers it in
`ssh_known_hosts` next to `tattle.db` (in `~/tattle/data`). If the key later
changes, Tattle stops and says so. If you reset or replaced the router, delete
that file and test again.

The SSH way needs the `ssh` program inside Tattle's container, and ssh refuses
to run for a user id that has no account entry. If **Save and test** says
there is no ssh program, or that its user id has no account, use the web
sign-in.

## The fields

| Field | What to put |
| --- | --- |
| Router address | `192.168.1.1`, or `https://192.168.1.1` |
| User name | `root`, or `tattle` with the restricted account |
| Router password | The web sign-in's password (way 1); leave empty for SSH |
| SSH private key | The private key (way 2); when set, Tattle uses SSH |
| SSH port | `22` unless you changed Dropbear's port |

## Verified vs unverified

**Verified** in a container running OpenWrt 25.12.6 (x86-64) with nlbwmon
2025-06-02, the release's rpcd and uhttpd, and Dropbear 2025.89, with Tattle's
adapter code reading it and a second container acting as a LAN device:

- The JSON shape: `{"columns": ["mac", "ip", "conns", "rx_bytes", "rx_pkts", "tx_bytes", "tx_pkts"], "data": [[...], ...]}`.
- **Direction:** a 5 MB download through the router landed in `rx_bytes`, a
  2 MB upload in `tx_bytes`. The source agrees: nlbwmon books the reply
  direction of a connection a LAN device opened as "in" for that device, and
  `nlbw -c show` labels `rx_bytes` "Downld.".
- Traffic between a device and the router itself is not counted.
- Web sign-in: `root` with stock ACLs (through luci-app-nlbwmon's helper), the
  restricted `tattle` account above (and that it is refused anything else),
  signing in again after the session ran out, a wrong password, uhttpd's
  redirect to HTTPS (a 307).
- SSH: key sign-in, the `command=` restriction, the remembered host key, a
  key flattened onto one line by a password box, the messages for a refused
  key and a closed port.

nlbwmon's JSON output code has not changed since 2018, so OpenWrt 21.02 to
25.12 should answer the same way.

**Unverified:**

- Real router hardware, and releases other than 25.12.6.
- **Flow offloading.** With software or hardware flow offloading on
  (**Network › Firewall › Routing/NAT Offloading**), offloaded packets may skip
  the connection-tracking counters nlbwmon reads, so devices could be
  under-counted. Testers: compare Tattle with your provider's figures with
  offloading on and off.
- Very busy networks: nlbwmon keeps at most `database_limit` entries
  (10,000 by default) per period.
- If you moved nlbwmon's database to permanent storage, a power cut makes it
  restore the last saved copy (saved daily by default): the totals drop to an
  older, still large value. Tattle's line-speed check should skip that reading
  rather than count it twice, but this is untested.

## Known limits

- No device names from the router; Tattle uses its own name sources.
- No internet-side counters (planned: `network.device status` over ubus).
- Devices behind a MAC-NAT extender count as one device; see
  [what router mode can't do](README.md#what-router-mode-cant-do-and-gateway-mode-can).

## References

- nlbwmon source, [github.com/jow-/nlbwmon](https://github.com/jow-/nlbwmon)
  (commit `29236be`, 2025-06-02): `client.c` (`handle_json`, the `-g` fields),
  `nfnetlink.c` (`parse_event`: direction, local/remote), `subnets.c`.
- OpenWrt package: [openwrt/packages `net/nlbwmon`](https://github.com/openwrt/packages/tree/master/net/nlbwmon)
  (default `local_network`, refresh and commit intervals).
- [luci-app-nlbwmon](https://github.com/openwrt/luci/tree/master/applications/luci-app-nlbwmon):
  its rpcd ACL and the `nlbwmon-action` helper.
- [rpcd](https://github.com/openwrt/rpcd) `file.c`: `file exec` checks the ACL
  against the command plus its arguments.
- OpenWrt docs: [ubus over HTTP](https://openwrt.org/docs/techref/ubus#access_to_ubus_over_http).

## Tested it?

Please tell us your router model and OpenWrt version, which way you used, and
whether the per-device numbers look right next to LuCI's Bandwidth Monitor:
[open a test report](https://github.com/khaledsharara/tattle/issues/new?template=test-report.yml).
