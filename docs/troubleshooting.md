# Troubleshooting

Start with the **health panel** at the bottom of the Overview (open *details*). Each line says what is wrong and what to do; this page explains the less obvious ones.

Logs: `cd ~/tattle && docker compose logs --since 1h` (add `gateway` or `tattle` to see one container).

## Setting up

| You see | What to do |
| --- | --- |
| The dashboard asks for a **setup code** | It's in `~/tattle/data/setup-code.txt`, in `docker compose logs tattle`, and the installer printed it. It's there until an admin password is set. |
| Forgot the admin password | `docker compose exec tattle python -m tattle.admin reset-password`, then `docker compose restart tattle`: the dashboard asks for a new setup code. |
| `permission denied` writing `/data/tattle.db` | The data folder must belong to user 1000: `sudo chown -R 1000:1000 ~/tattle/data`. |
| The page doesn't open from another device | Use the server's address, `http://<server IP>:8090`. A firewall on the server (firewalld, ufw) must allow port 8090; the installer opens it. |

## Gateway mode

| Health panel says | What it means and what to do |
| --- | --- |
| *The gateway container isn't running* | `docker compose --profile gateway up -d`, then `docker compose logs gateway`: it refuses to start with a sentence saying why (see below). |
| *No device has an address from Tattle yet* | The router's DHCP is still on (devices keep getting addresses from it), or devices haven't renewed yet. Turn DHCP off on the router, then reconnect Wi-Fi on a phone: it should appear under Devices. |
| *Other DHCP servers: also handing out addresses* | Something else answers DHCP: usually the router (DHCP still on) or a Wi-Fi extender whose DHCP server is set to **Auto** (it starts one when it stops seeing the router's). Set it to **Off**. Two DHCP servers make devices get random gateways. |
| *Another firewall is dropping forwarded traffic* | firewalld, ufw or an old Docker rule blocks forwarding on the server. Re-run the installer (it opens what's needed), or see [gateway-mode.md](gateway-mode.md#firewalls). |
| *The router advertises IPv6* | Devices can reach the internet over IPv6 straight through the router, past Tattle. Turn off IPv6 (or "IPv6 LAN" / router advertisements / DHCPv6) on the router, or set `TATTLE_FILTER_AAAA=1` in `.env`. |
| *Traffic Tattle sees: counted 70% of what your provider charged* | Some traffic goes around Tattle: IPv6 (above), devices with a fixed IP and gateway set by hand (set them to automatic, or give them the server as gateway), a second router or modem doing its own Wi-Fi, or the account being used elsewhere. A few percent less than the provider is normal (it also charges line overhead). |

The gateway refuses to start when:

- **IP forwarding is off**: Docker normally turns it on; with Docker's nftables firewall it doesn't. `echo net.ipv4.ip_forward=1 | sudo tee /etc/sysctl.d/90-tattle.conf && sudo sysctl --system`.
- **The server's address is a DHCP lease**: the server must keep its address. Give it a fixed address (the installer offers to); otherwise it may lose it once Tattle is the DHCP server.
- **The internet route doesn't go through `TATTLE_LAN_IF`**: on a laptop with Wi-Fi and Ethernet both connected, turn Wi-Fi off (or set `TATTLE_LAN_IF` to the interface that has the default route).
- **Something else uses port 53 or 67**: Pi-hole, AdGuard Home, another dnsmasq or a DHCP server already runs on the server. Stop it, or use quota-only/router mode.

**The house lost internet**: the server is down or the gateway stopped. Turn the router's DHCP back on and reconnect devices; then fix the server. Leases last 2 hours, so devices also go back to the router on their own. [More](gateway-mode.md#when-the-server-goes-down).

## Provider figures

| You see | What to do |
| --- | --- |
| *Signed out by …: sign in again* | The provider ended the session (password change, too many sessions, or it expires sessions). Settings › Internet provider › **Sign in again**. |
| *Could not read … last time; retrying* | The provider's site was down or slow; Tattle tries again every 15 minutes. If it lasts a day, open an issue with the error (no personal data). |
| The cycle is wrong | The cycle comes from the line the house is on. With two lines, check *Line the house is on* in Settings; pick the right one if Tattle guessed wrong. |
| Figures differ from the provider's app by a factor of ~1.07 | GB vs GiB: some providers count 1 GB = 1024 MB. Tattle uses the connected provider's unit; tell us if a provider's is wrong. |

## Router mode

| You see | What to do |
| --- | --- |
| *Choose your router in Settings to count devices* | Settings › Router: pick the adapter, fill in the address and sign-in, **Save and test**. |
| The test fails | The message says why (wrong password, unreachable, the router's API is off). See the router's page in [routers/](routers/README.md) for what to enable on the router. |
| Devices behind an extender show as one device | Router mode only sees MAC addresses, and many extenders replace their clients' MACs with their own. Gateway mode tells them apart. |

## Still stuck?

[Open an issue](https://github.com/khaledsharara/tattle/issues/new?template=bug.yml) with your setup and the health panel's text, with any personal data replaced.
