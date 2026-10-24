#!/usr/bin/env bash
# End-to-end test of gateway mode on a real Docker host. Built for CI
# (ubuntu-24.04, passwordless sudo); it changes the host's firewall and Docker
# settings, so don't run it on a machine you care about.
#
#   sudo tests/gateway-e2e.sh [iptables-nft|iptables-legacy|docker-nftables]
#
# Network namespaces stand in for the house; the host itself is the Tattle server:
#
#   netns cli  (00:00:5e:00:53:01, DHCP) --+
#                                          +-- br-lan 10.99.0.100/24 (host = Tattle server)
#   netns rtr  10.99.0.1 ------------------+
#        +-- 198.51.100.1/30 <-> netns inet 198.51.100.2, web server on 203.0.113.10
#
# The host's real default route stays on eth0, so the gateway's route check is
# skipped and 203.0.113.0/24 is routed via the test router instead.
#
# Environment: SKIP_BUILD=1 to use an already built ghcr.io/khaledsharara/tattle:e2e.
set -euo pipefail

VARIANT=${1:-iptables-nft}
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WORK=$(mktemp -d /tmp/tattle-e2e.XXXXXX)
IMAGE=ghcr.io/khaledsharara/tattle:e2e
PROJECT=tattle-e2e
SERVER=10.99.0.100
ROUTER=10.99.0.1
WEB=203.0.113.10
CLIENT_MAC=00:00:5e:00:53:01
BLOB_BYTES=5242880
PIDS=()
STEP=setup

# --- the dashboard API, in one place (its shape may change) ------------------------------

# Prints the bytes (rx + tx) the dashboard has booked today for the device that
# holds IP $1, or 0 if it doesn't list one yet.
api_device_bytes() {
	{ curl -fsS --max-time 5 "http://127.0.0.1:8090/api/devices?range=today" 2>/dev/null || true; } | python3 -c '
import json, sys
ip = sys.argv[1]
try:
    devices = json.load(sys.stdin).get("devices", [])
except ValueError:
    devices = []
for d in devices:
    ips = {d.get("ip"), d.get("last_ip")} | {i.get("ip") for i in d.get("identities") or []}
    if ip in ips:
        print(int(d.get("rx") or 0) + int(d.get("tx") or 0))
        break
else:
    print(0)' "$1"
}

# --- helpers -----------------------------------------------------------------------------

log() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
fail() {
	printf '\n\033[31mFAIL (%s): %s\033[0m\n' "$STEP" "$*" >&2
	exit 1
}
skip() {
	printf '\nSKIP: %s\n' "$*"
	exit 0
}
compose() { docker compose -p "$PROJECT" --project-directory "$WORK" -f "$WORK/docker-compose.yml" "$@"; }
in_ns() {
	local ns=$1
	shift
	ip netns exec "$ns" "$@"
}
wait_until() { # wait_until SECONDS DESCRIPTION COMMAND...
	local seconds=$1 what=$2
	shift 2
	local deadline=$((SECONDS + seconds))
	until "$@"; do
		[ "$SECONDS" -lt "$deadline" ] || fail "timed out after ${seconds}s waiting for: $what"
		sleep 1
	done
}
status_file() { echo "$(docker volume inspect -f '{{.Mountpoint}}' "${PROJECT}_tattle-run")/gateway.json"; }
status_get() { # status_get PYTHON-EXPRESSION over `d` (gateway.json)
	python3 -c "import json, sys; d = json.load(open(sys.argv[1])); print($1)" "$(status_file)" 2>/dev/null
}
gateway_state_is() { [ "$(status_get 'd["state"]')" = "$1" ]; }
forwarding_ok() { [ "$(status_get 'd["forwarding"]["ok"]')" = True ]; }
adverts_seen() { [ "$(status_get 'd["ipv6_adverts"]["seen"]')" = True ]; }
forward_policy() { # the FORWARD policy in the iptables backend Docker uses
	case "$VARIANT" in
	iptables-legacy) iptables-legacy -S FORWARD 2>/dev/null | sed -n 's/^-P FORWARD //p' ;;
	docker-nftables) echo "none" ;;
	*) iptables-nft -S FORWARD 2>/dev/null | sed -n 's/^-P FORWARD //p' ;;
	esac
}
client_download() { in_ns cli curl -sS --max-time "${1:-30}" -o /dev/null -w '%{size_download}' "http://$WEB:8000/blob"; }
counters() { # "<table handle> <dev_down bytes for the client>"
	nft -j list table inet tattle | python3 -c '
import json, sys
ip, handle, down = sys.argv[1], None, 0
for item in json.load(sys.stdin)["nftables"]:
    if "table" in item:
        handle = item["table"]["handle"]
    s = item.get("set")
    if s and s["name"] == "dev_down":
        for e in s.get("elem", []):
            if isinstance(e, dict) and e["elem"]["val"] == ip:
                down = e["elem"]["counter"]["bytes"]
print(handle, down)' "$CLIENT_IP"
}

cleanup() {
	local code=$?
	set +e
	if [ "$code" != 0 ] && [ -f "$WORK/docker-compose.yml" ]; then
		echo "--- gateway.json"
		cat "$(status_file)" 2>/dev/null
		echo "--- container logs"
		compose --profile gateway logs --no-color --tail 150
		echo "--- nft ruleset"
		nft list ruleset | head -150
		echo "--- iptables FORWARD / DOCKER-USER"
		iptables -S FORWARD
		iptables -S DOCKER-USER
	fi
	[ -f "$WORK/docker-compose.yml" ] && compose --profile gateway down --volumes --remove-orphans >/dev/null 2>&1
	for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null; done
	for ns in cli rtr inet; do ip netns del "$ns" 2>/dev/null; done
	ip link del br-lan 2>/dev/null
	nft delete table inet tattle 2>/dev/null
	rm -rf "$WORK"
	exit "$code"
}
trap cleanup EXIT

[ "$(id -u)" = 0 ] || fail "run as root (sudo)"
for tool in ip nft docker curl python3 busybox dnsmasq; do
	command -v "$tool" >/dev/null || fail "missing $tool"
done

# --- the host's firewall flavour -------------------------------------------------------------

flush_nft_xtables() { # remove Docker's iptables-nft leftovers, which would still drop traffic
	local t
	for cmd in iptables-nft ip6tables-nft; do
		$cmd -P FORWARD ACCEPT 2>/dev/null || true
		for t in filter nat mangle raw; do
			$cmd -t "$t" -F 2>/dev/null || true
			$cmd -t "$t" -X 2>/dev/null || true
		done
	done
}

STEP="firewall variant $VARIANT"
log "Host firewall: $VARIANT"
case "$VARIANT" in
iptables-nft)
	iptables-nft-save | grep DOCKER >/dev/null || fail "Docker isn't using iptables-nft here"
	;;
iptables-legacy)
	systemctl stop docker docker.socket
	flush_nft_xtables
	update-alternatives --set iptables /usr/sbin/iptables-legacy
	update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy
	systemctl start docker
	wait_until 30 "Docker's legacy chains" sh -c 'iptables-legacy-save | grep DOCKER >/dev/null'
	;;
docker-nftables)
	major=$(docker version --format '{{.Server.Version}}' | cut -d. -f1)
	[ "$major" -ge 29 ] || skip "Docker $(docker version --format '{{.Server.Version}}') has no nftables firewall backend (needs 29+)."
	sysctl -qw net.ipv4.ip_forward=1 # Docker's nftables backend doesn't turn it on
	python3 - <<'EOF'
import json, os
path = "/etc/docker/daemon.json"
data = json.load(open(path)) if os.path.exists(path) and open(path).read().strip() else {}
data["firewall-backend"] = "nftables"
json.dump(data, open(path, "w"), indent=2)
EOF
	systemctl stop docker docker.socket
	flush_nft_xtables
	systemctl start docker || skip "Docker didn't start with firewall-backend=nftables: $(journalctl -u docker -n 5 --no-pager | tail -n 3)"
	nft list tables | grep docker >/dev/null || skip "Docker ignored firewall-backend=nftables (no docker table in nftables)."
	;;
*) fail "unknown variant $VARIANT (iptables-nft, iptables-legacy, docker-nftables)" ;;
esac
POLICY=$(forward_policy)
echo "FORWARD policy: ${POLICY:-unknown}"

# --- the pretend house -----------------------------------------------------------------------

STEP=topology
log "Topology"
if [ -e /proc/sys/net/bridge/bridge-nf-call-iptables ]; then
	sysctl -qw net.bridge.bridge-nf-call-iptables=0 # bridged frames must not meet Docker's FORWARD DROP
fi
for ns in cli rtr inet; do
	ip netns add "$ns"
	in_ns "$ns" ip link set lo up
done
ip link add br-lan type bridge
ip link set br-lan up
ip addr add "$SERVER/24" dev br-lan
sysctl -qw net.ipv6.conf.br-lan.accept_ra=0 # the test router's adverts are for Tattle to notice, not to route by
ip link add rtr-lan type veth peer name br-rtr
ip link set br-rtr master br-lan up
ip link set rtr-lan netns rtr
ip link add rtr-wan type veth peer name inet-wan
ip link set rtr-wan netns rtr
ip link set inet-wan netns inet
in_ns rtr ip addr add "$ROUTER/24" dev rtr-lan
in_ns rtr ip link set rtr-lan up
in_ns rtr ip addr add 198.51.100.1/30 dev rtr-wan
in_ns rtr ip link set rtr-wan up
in_ns rtr ip route add 203.0.113.0/24 via 198.51.100.2
in_ns rtr sysctl -qw net.ipv4.ip_forward=1
in_ns rtr sysctl -qw net.ipv6.conf.all.forwarding=1 # a router: joins ff02::2, answers solicitations
in_ns rtr nft -f - <<'EOF'
table ip nat {
	chain post { type nat hook postrouting priority srcnat; policy accept; oifname "rtr-wan" masquerade; }
}
EOF
in_ns inet ip addr add "$WEB/32" dev lo
in_ns inet ip addr add 198.51.100.2/30 dev inet-wan
in_ns inet ip link set inet-wan up
in_ns inet ip route add default via 198.51.100.1
ip link add cli0 type veth peer name br-cli
ip link set br-cli master br-lan up
ip link set cli0 netns cli
in_ns cli ip link set cli0 address "$CLIENT_MAC"
in_ns cli ip link set cli0 up
ip route add 203.0.113.0/24 via "$ROUTER" dev br-lan

mkdir -p "$WORK/www"
head -c "$BLOB_BYTES" /dev/urandom >"$WORK/www/blob"
in_ns inet python3 -m http.server 8000 --bind "$WEB" --directory "$WORK/www" >/dev/null 2>&1 &
PIDS+=($!)
sleep 2 # IPv6 duplicate address detection on the new links
in_ns rtr python3 "$REPO/tests/e2e/ra_responder.py" rtr-lan &
PIDS+=($!)
wait_until 10 "the test web server" in_ns inet curl -fs -o /dev/null "http://$WEB:8000/"

STEP="before the gateway"
log "Before the gateway: Docker's firewall blocks forwarded traffic"
in_ns cli ip addr add 10.99.0.50/24 dev cli0
in_ns cli ip route add default via "$SERVER"
if [ "$POLICY" = DROP ]; then
	if client_download 5 >/dev/null 2>&1; then fail "the download worked before the gateway ran: FORWARD DROP should block it"; fi
	echo "Blocked, as expected (FORWARD policy DROP)."
else
	echo "FORWARD isn't DROP with $VARIANT, so there's nothing to block yet."
fi
in_ns cli ip -4 addr flush dev cli0

# --- Tattle ------------------------------------------------------------------------------------

STEP="start"
log "Starting Tattle in gateway mode"
if [ "${SKIP_BUILD:-0}" != 1 ]; then
	docker build -t "$IMAGE" "$REPO"
fi
cp "$REPO/docker-compose.yml" "$WORK/"
cat >"$WORK/.env" <<EOF
TATTLE_MODE=gateway
TZ=UTC
TATTLE_PORT=8090
TATTLE_VERSION=e2e
TATTLE_LAN_IF=br-lan
TATTLE_SERVER_IP=$SERVER
TATTLE_LAN=10.99.0.0/24
TATTLE_ROUTER_IP=$ROUTER
TATTLE_DHCP_RANGE=10.99.0.150,10.99.0.199
TATTLE_DNS=$ROUTER
TATTLE_SKIP_ROUTE_CHECK=1
TATTLE_PROBE_INTERVAL=20
EOF
mkdir -p "$WORK/data"
chown 1000:1000 "$WORK/data"
touch /run/xtables.lock
compose --profile gateway up -d --pull never
wait_until 60 "the gateway to report running" gateway_state_is running

STEP=dhcp
log "The client gets a lease that points at Tattle"
in_ns cli env UDHCPC_OUT="$WORK/lease.env" busybox udhcpc -i cli0 -f -q -n -t 5 -T 3 \
	-s "$REPO/tests/e2e/udhcpc.sh" -x hostname:e2e-client
lease() { sed -n "s/^$1=//p" "$WORK/lease.env" | tr -d '"'; }
CLIENT_IP=$(lease ip)
lease_router=$(lease router)
lease_dns=$(lease dns)
echo "client $CLIENT_IP, router $lease_router, dns $lease_dns"
[ "$lease_router" = "$SERVER" ] || fail "router is $lease_router, expected $SERVER"
[ "$lease_dns" = "$SERVER $ROUTER" ] || fail "DNS servers are '$lease_dns', expected '$SERVER $ROUTER' (Tattle, then the router as backup)"

STEP=download
log "A 5 MB download through Tattle"
size=$(client_download 60)
[ "$size" = "$BLOB_BYTES" ] || fail "downloaded $size of $BLOB_BYTES bytes"
echo "Downloaded $size bytes."
in_ns cli ping -q -i 0.2 "$WEB" >/dev/null 2>&1 & # steady traffic for the forwarding self-test
PIDS+=($!)

STEP=api
log "The dashboard books it on the client"
api_has_usage() { [ "$(api_device_bytes "$CLIENT_IP")" -ge "$BLOB_BYTES" ]; }
wait_until 90 "the API to show >= $BLOB_BYTES bytes for $CLIENT_IP" api_has_usage
echo "API: $(api_device_bytes "$CLIENT_IP") bytes for $CLIENT_IP."

STEP=fingerprint
[ -s "$(docker volume inspect -f '{{.Mountpoint}}' "${PROJECT}_tattle-state")/dhcp/00005e005301.json" ] ||
	fail "no DHCP fingerprint file for the client"

STEP=forwarding
log "The forwarding self-test says traffic passes"
wait_until 150 "forwarding.ok to be true" forwarding_ok
status_get 'd["forwarding"]["detail"]'

STEP=ipv6
if [ "$(cat /proc/sys/net/ipv6/conf/br-lan/disable_ipv6 2>/dev/null || echo 1)" = 0 ]; then
	log "The router's IPv6 adverts are noticed"
	wait_until 60 "ipv6_adverts.seen to be true" adverts_seen
	status_get 'd["ipv6_adverts"]["detail"]'
else
	echo "IPv6 is off on br-lan; skipping the advert check."
fi

STEP=restart
log "Counters survive a gateway restart"
read -r handle_before down_before < <(counters)
compose --profile gateway restart gateway
wait_until 60 "the gateway to come back" gateway_state_is running
read -r handle_after down_after < <(counters)
echo "table handle $handle_before -> $handle_after, client download bytes $down_before -> $down_after"
[ "$handle_before" = "$handle_after" ] || fail "the table was reloaded (handle changed)"
[ "$down_after" -ge "$down_before" ] || fail "counters went backwards"
compose logs gateway | grep "kept with its counters" >/dev/null || fail "no 'kept with its counters' in the gateway log"

STEP="rogue dhcp"
log "A second DHCP server is reported"
in_ns rtr dnsmasq --keep-in-foreground --conf-file=/dev/null --port=0 --interface=rtr-lan --bind-interfaces \
	--dhcp-range=10.99.0.20,10.99.0.30,1h --leasefile-ro --pid-file= --log-facility=- --user=root --group= \
	>"$WORK/rogue.log" 2>&1 &
ROGUE=$!
PIDS+=("$ROGUE")
sleep 1
docker kill -s USR1 "$(compose ps -q gateway)" >/dev/null # probe now
rogue_seen() { status_get '",".join(s["ip"] for s in d["dhcp"]["foreign_servers"])' | grep -x "$ROUTER" >/dev/null; }
wait_until 30 "the rogue server in gateway.json" rogue_seen
echo "Reported: $(status_get 'd["dhcp"]["foreign_servers"]')"
kill "$ROGUE"

STEP=status
compose exec -T gateway python -m tattle.gateway status || true

STEP=off
log "off removes the table and the Docker rule"
compose --profile gateway rm -sf gateway
compose run --rm gateway off
if nft list table inet tattle >/dev/null 2>&1; then fail "table inet tattle is still there"; fi
if iptables-nft-save 2>/dev/null | grep tattle-gateway >/dev/null; then fail "an iptables-nft rule is still there"; fi
if iptables-legacy-save 2>/dev/null | grep tattle-gateway >/dev/null; then fail "an iptables-legacy rule is still there"; fi
if [ "$POLICY" = DROP ]; then
	if client_download 5 >/dev/null 2>&1; then fail "traffic still passes after off"; fi
	echo "Forwarded traffic is blocked again."
fi

log "PASSED ($VARIANT)"
