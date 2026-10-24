# Routers

A router adapter lets Tattle read what your router already knows. Routers that
keep **per-device traffic counters** give you *router mode*: traffic per device
without making the Tattle server your network's gateway. Others can still tell
Tattle which internet line is in use, how fast the line synced, or whether the
router's DHCP server is on.

Choose yours in **Settings › Router**, fill in the fields and press **Save and
test**. The test reads everything the adapter can and tells you what it found,
or says in plain words what went wrong.

| Adapter | Status | What it reads | Router mode | Counts traffic that stays in the house? |
| --- | --- | --- | --- | --- |
| [D-Link DSL-245GE (e& Egypt)](dlink_dsl245ge.md) | Stable | Internet login (which e& line), DSL sync speeds, the router's internet counters, DHCP on/off | No: no per-device counters | — |
| [OpenWrt (nlbwmon)](openwrt_nlbwmon.md) | Beta | Per-device totals for nlbwmon's accounting period | Yes | **No**: internet only |
| [MikroTik RouterOS 7 (REST API)](mikrotik_rest.md) | Beta | Per-device totals (Kid Control), the internet interface's counters, the PPPoE user name | Yes | **Partly**: includes traffic to the router itself (DNS, its web pages) |
| [UniFi Network](unifi.md) | Beta | Per-client totals from your access points and switches | Yes | **Yes**: casting, backups to a NAS and so on are counted too |

**Beta** means the adapter was built from the router's documentation and source
code and tested against real router *software* (an OpenWrt container, a
RouterOS virtual machine, a UniFi Network server), but **not yet on real
hardware**. Testers wanted: if you run one of these, please
[send a test report](https://github.com/khaledsharara/tattle/issues/new?template=test-report.yml),
even if it just works. Never paste real MAC addresses, public IPs or passwords
into an issue.

Your router isn't listed? [Ask for it](https://github.com/khaledsharara/tattle/issues/new?template=router-request.yml),
or [write the adapter](../adding-a-router.md): it is one Python file.

## How router mode counts

About every 60 seconds Tattle reads each device's running totals (bytes it
downloaded and uploaded, keyed by MAC address) and books the difference since
the last reading on that device.

- **The first reading** is only a starting point: what a device used before
  Tattle watched is not booked.
- **A total that goes down** means the router restarted counting (a reboot, a
  device reconnecting, a new accounting period). Tattle counts the new total as
  new traffic. What the device used between the last reading and the restart
  is lost, usually well under a minute's worth.
- **A jump larger than your line could carry** in the time since the last
  reading is skipped rather than booked.
- A device that disappears (switched off, out of range) keeps its last totals;
  when it comes back, counting continues.

## What router mode can't do (and gateway mode can)

Router mode only sees what the router keeps: a MAC address, sometimes an IP
address and a name, and byte totals. Gateway mode sees every DHCP request and
every packet, so it knows more. In router mode:

- **Devices behind a Wi-Fi extender may collapse into one device.** Most
  range extenders do *MAC-NAT*: on the network, every device behind the
  extender wears the extender's own MAC address, so the router counts them as
  one. Gateway mode tells them apart by the DHCP client ID each device sends;
  router adapters don't get the client ID.
- **A phone's private Wi-Fi addresses are linked by name only.** Phones use a
  different random MAC per network and change it now and then. Gateway mode
  links them using DHCP fingerprints; router mode falls back to the device
  name (from the router or from mDNS), which is weaker. Until linked, each
  private address shows as its own device.
- **Some counters include traffic that stays in the house.** Counters kept by
  access points and switches (UniFi) count everything a device sends or
  receives: casting to the TV, a backup to a NAS, AirPlay. None of that uses
  your internet quota, so the per-device numbers can add up to more than your
  provider charges. MikroTik's counters include traffic to the router itself.
  nlbwmon counts internet traffic only. Each router's page says which.
- **Counters are read about once a minute**, not every few seconds, so live
  speeds and "online now" are coarser than in gateway mode.
- **Only traffic the counting device sees.** A device plugged into a switch or
  access point the adapter doesn't read (a non-UniFi switch on a UniFi
  network, an OpenWrt box used only as an access point behind another router)
  may be missing or partly counted.

For exact, internet-only numbers per device on any router, use gateway mode
(see the [main README](../../README.md#pick-a-mode)).
