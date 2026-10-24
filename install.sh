#!/usr/bin/env bash
# Tattle installer: home internet usage, per device.
#
#   curl -fsSL https://raw.githubusercontent.com/khaledsharara/tattle/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --uninstall [--purge]
#
# Run it again at any time to change mode or upgrade. Nothing on this machine's
# network changes unless you pick gateway mode, and then every change is
# explained and asked about first.
#
# Settings (all optional):
#   TATTLE_DIR          where Tattle lives (default ~/tattle)
#   TATTLE_REF          branch or tag to install from (default main; a tag like v0.2.0 also pins the image)
#   TATTLE_MODE         quota | router | gateway: skip the question
#   TATTLE_YES=1        accept every default without asking (for automation)
#   TATTLE_COMPOSE_URL  where to get docker-compose.yml (file:// works, for testing a checkout)
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/khaledsharara/tattle"
REF="${TATTLE_REF:-main}"
DIR="${TATTLE_DIR:-$HOME/tattle}"
COMPOSE_URL="${TATTLE_COMPOSE_URL:-$REPO_RAW/$REF/docker-compose.yml}"
YES="${TATTLE_YES:-0}"
SYSCTL_FILE=/etc/sysctl.d/90-tattle.conf
TMPFILES_FILE=/etc/tmpfiles.d/tattle-xtables.conf
NETPLAN_FILE=/etc/netplan/90-tattle-static.yaml

# --- output and questions ----------------------------------------------------------

say() { printf '%s\n' "$*"; }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWarning:\033[0m %s\n' "$*" >&2; }
die() {
	printf '\033[31mError:\033[0m %s\n' "$*" >&2
	exit 1
}

tty_ok() { (: </dev/tty) 2>/dev/null; }

# ask VAR "question" default. Reads from the terminal: when piped from curl,
# stdin is this script.
ask() {
	# Odd local names: printf -v must not land on a local that shadows the caller's variable.
	local __ask_var=$1 __ask_question=$2 __ask_default=${3-} __ask_reply=
	if [ "$YES" = 1 ]; then
		printf -v "$__ask_var" '%s' "$__ask_default"
		return
	fi
	tty_ok || die "No terminal to ask questions on. Run this from a terminal, or set TATTLE_YES=1 to accept the defaults."
	printf '%s [%s]: ' "$__ask_question" "$__ask_default" >/dev/tty
	IFS= read -r __ask_reply </dev/tty || __ask_reply=
	printf -v "$__ask_var" '%s' "${__ask_reply:-$__ask_default}"
}

# confirm "question" [y|n]: the default is what Enter (or TATTLE_YES=1) means.
confirm() {
	local default=${2:-y} answer
	ask answer "$1 (y/n)" "$default"
	case "$answer" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

# --- privileges ------------------------------------------------------------------------

if [ "$(id -u)" = 0 ]; then
	SUDO=""
elif command -v sudo >/dev/null 2>&1; then
	SUDO="sudo"
else
	SUDO=""
fi

need_root() {
	[ "$(id -u)" = 0 ] || [ -n "$SUDO" ] || die "This step needs root, and sudo isn't installed. Run the installer as root."
}

# --- small helpers -----------------------------------------------------------------------

ip2int() {
	local a b c d
	IFS=. read -r a b c d <<<"$1"
	echo $(((a << 24) | (b << 16) | (c << 8) | d))
}
int2ip() { echo "$((($1 >> 24) & 255)).$((($1 >> 16) & 255)).$((($1 >> 8) & 255)).$(($1 & 255))"; }

env_get() { # env_get KEY: the value in .env, or nothing
	[ -f "$DIR/.env" ] || return 0
	sed -n "s/^$1=//p" "$DIR/.env" | tail -n 1
}

env_set() { # env_set KEY VALUE: replace the line or add it, keeping everything else in .env
	local key=$1 value=$2
	if grep -q "^$key=" "$DIR/.env" 2>/dev/null; then
		sed -i "s|^$key=.*|$key=$value|" "$DIR/.env"
	else
		printf '%s=%s\n' "$key" "$value" >>"$DIR/.env"
	fi
}

compose() { (cd "$DIR" && $DOCKER compose "$@"); }

# --- checks ------------------------------------------------------------------------------

check_system() {
	[ "$(uname -s)" = Linux ] || die "Tattle runs on Linux (a Raspberry Pi, an old laptop, a mini PC...). This is $(uname -s)."
	case "$(uname -m)" in
	x86_64 | amd64) ARCH=amd64 ;;
	aarch64 | arm64) ARCH=arm64 ;;
	armv6l | armv7l | armhf | arm*)
		die "This is a 32-bit ARM system, and Tattle's image is 64-bit only. Install the 64-bit Raspberry Pi OS (or another 64-bit OS) and run this again."
		;;
	*) die "Unsupported processor: $(uname -m). Tattle runs on amd64 (x86-64) and arm64." ;;
	esac
	if [ "$ARCH" = arm64 ] && [ "$(getconf LONG_BIT 2>/dev/null || echo 64)" = 32 ]; then
		die "This Raspberry Pi runs a 64-bit kernel with a 32-bit system. Install the 64-bit Raspberry Pi OS and run this again."
	fi
	command -v curl >/dev/null || die "curl is needed. Install it (e.g. sudo apt install curl) and run this again."
	command -v ip >/dev/null || die "The ip command (iproute2) is needed. Install it and run this again."
}

install_docker() {
	say "Docker isn't installed. Tattle runs in Docker."
	confirm "Install Docker now with Docker's official script (get.docker.com)?" y ||
		die "Install Docker (https://docs.docker.com/engine/install/) and run this again."
	need_root
	local script
	script=$(mktemp)
	curl -fsSL https://get.docker.com -o "$script"
	$SUDO sh "$script"
	rm -f "$script"
	if [ "$(id -u)" != 0 ]; then
		$SUDO usermod -aG docker "$USER"
		say "You're in the docker group from your next login; until then this uses sudo docker."
	fi
}

find_docker() {
	command -v docker >/dev/null 2>&1 || install_docker
	case "$(readlink -f "$(command -v docker)")" in
	/snap/*) die "Docker here comes from Snap, whose confinement breaks Tattle. Remove it (sudo snap remove docker), install Docker from https://get.docker.com, and run this again." ;;
	esac
	if docker --version 2>/dev/null | grep -i podman >/dev/null; then
		die "This 'docker' is Podman. Tattle needs Docker Engine: https://docs.docker.com/engine/install/"
	fi
	if docker info >/dev/null 2>&1; then
		DOCKER="docker"
	elif [ -n "$SUDO" ] && $SUDO docker info >/dev/null 2>&1; then
		DOCKER="$SUDO docker"
	else
		die "Docker is installed but not running. Start it (sudo systemctl enable --now docker) and run this again."
	fi
	if $DOCKER info --format '{{json .SecurityOptions}}' 2>/dev/null | grep rootless >/dev/null; then
		die "This is rootless Docker. Tattle needs to share the server's network, which rootless Docker can't do. Use regular Docker (https://get.docker.com)."
	fi
	$DOCKER compose version >/dev/null 2>&1 ||
		die "Docker Compose is missing. Install the docker-compose-plugin package (https://docs.docker.com/compose/install/linux/) and run this again."
}

detect_network() {
	local route
	route=$(ip -4 route show default | awk '{m=0; d=""; g=""; for (i = 1; i < NF; i++) { if ($i == "dev") d = $(i+1); if ($i == "via") g = $(i+1); if ($i == "metric") m = $(i+1) } if (d != "") print m, d, g }' | sort -n | head -n 1)
	[ -n "$route" ] || die "This server has no default route (no internet connection?). Connect it to the home network and run this again."
	read -r _ LAN_IF ROUTER_IP <<<"$route"
	LAN_IF=$(env_get TATTLE_LAN_IF | grep . || echo "$LAN_IF")
	local cidr
	cidr=$(ip -4 -o addr show dev "$LAN_IF" scope global | awk '{print $4; exit}')
	[ -n "$cidr" ] || die "$LAN_IF has no IPv4 address. Connect it to the home network and run this again."
	SERVER_IP=${cidr%/*}
	PREFIX=${cidr#*/}
	local mask=$(((0xFFFFFFFF << (32 - PREFIX)) & 0xFFFFFFFF))
	NET_INT=$(($(ip2int "$SERVER_IP") & mask))
	BCAST_INT=$((NET_INT | (~mask & 0xFFFFFFFF)))
	LAN="$(int2ip "$NET_INT")/$PREFIX"
	ROUTER_IP=$(env_get TATTLE_ROUTER_IP | grep . || echo "${ROUTER_IP:-}")
	DYNAMIC=0
	ip -4 -o addr show dev "$LAN_IF" | grep " $SERVER_IP/.* dynamic" >/dev/null && DYNAMIC=1
	WIFI=0
	if [ -d "/sys/class/net/$LAN_IF/wireless" ] || [ -e "/sys/class/net/$LAN_IF/phy80211" ]; then WIFI=1; fi
	TZ_NAME=$(timedatectl show -p Timezone --value 2>/dev/null || true)
	[ -n "$TZ_NAME" ] || TZ_NAME=$(cat /etc/timezone 2>/dev/null || true)
	[ -n "$TZ_NAME" ] || TZ_NAME=$(readlink /etc/localtime 2>/dev/null | sed -n 's#.*/zoneinfo/##p')
	TZ_NAME=${TZ_NAME:-UTC}
	say "Network: $LAN_IF, this server $SERVER_IP, network $LAN, router ${ROUTER_IP:-unknown}, time zone $TZ_NAME"
	[ "$WIFI" = 1 ] && say "($LAN_IF is a Wi-Fi connection.)"
	return 0
}

choose_mode() {
	PREV_MODE=$(env_get TATTLE_MODE)
	if [ -n "${TATTLE_MODE:-}" ]; then
		MODE=$TATTLE_MODE
	else
		local default=1 choice
		case "$PREV_MODE" in router) default=2 ;; gateway) default=3 ;; esac
		say ""
		say "How should Tattle count?"
		say "  1) Quota only  Your provider's figures: used, left, when it renews. No network changes."
		say "  2) Router      Per-device counts, read from a supported router. Still no network changes."
		say "  3) Gateway     Exact per-device counts: this server becomes the network's DHCP server"
		say "                 and gateway. Needs a wired connection and a few router settings."
		ask choice "Choose 1, 2 or 3" "$default"
		case "$choice" in 1 | quota) MODE=quota ;; 2 | router) MODE=router ;; 3 | gateway) MODE=gateway ;; *) die "Please answer 1, 2 or 3." ;; esac
	fi
	case "$MODE" in quota | router | gateway) ;; *) die "TATTLE_MODE must be quota, router or gateway." ;; esac
	if [ "$MODE" = gateway ]; then
		[ -n "$ROUTER_IP" ] || die "Couldn't find the router's address (no default gateway). Gateway mode needs it."
		if [ "$WIFI" = 1 ]; then
			warn "$LAN_IF is Wi-Fi. Gateway mode sends every device's traffic through this server twice (in and back out), which Wi-Fi can't keep up with, and many Wi-Fi drivers drop forwarded traffic. Use a wired Ethernet connection."
			confirm "Continue with Wi-Fi anyway?" n || die "Connect this server with a cable and run this again (or pick another mode)."
		fi
	fi
}

# --- gateway mode: host changes, each explained and confirmed ----------------------------

make_static() {
	[ "$DYNAMIC" = 1 ] || return 0
	step "Fixed address"
	say "This server got $SERVER_IP from the router's DHCP. Once the router's DHCP is off, that lease would"
	say "run out and the whole house would lose its gateway. Tattle keeps the same address, but as a fixed one:"
	say "  address $SERVER_IP/$PREFIX, gateway $ROUTER_IP, DNS $ROUTER_IP on $LAN_IF."
	[ -n "${SSH_CONNECTION:-}" ] && say "(The network may blink for a second; the address doesn't change, so SSH reconnects.)"
	if command -v nmcli >/dev/null 2>&1 && nmcli -t -f RUNNING general 2>/dev/null | grep running >/dev/null &&
		[ -n "$(nmcli -g GENERAL.CONNECTION device show "$LAN_IF" 2>/dev/null)" ]; then
		local con
		con=$(nmcli -g GENERAL.CONNECTION device show "$LAN_IF")
		say "NetworkManager manages $LAN_IF (connection \"$con\")."
		confirm "Make the address fixed with nmcli?" y || return 0
		need_root
		$SUDO nmcli connection modify "$con" ipv4.method manual ipv4.addresses "$SERVER_IP/$PREFIX" \
			ipv4.gateway "$ROUTER_IP" ipv4.dns "$ROUTER_IP"
		$SUDO nmcli device reapply "$LAN_IF" || $SUDO nmcli connection up "$con"
	elif command -v netplan >/dev/null 2>&1 && ls /etc/netplan/*.yaml >/dev/null 2>&1; then
		say "netplan manages the network. Tattle would add $NETPLAN_FILE:"
		local yaml
		yaml=$(printf 'network:\n  version: 2\n  ethernets:\n    %s:\n      dhcp4: false\n      addresses: [%s/%s]\n      routes:\n        - to: default\n          via: %s\n      nameservers:\n        addresses: [%s]\n' \
			"$LAN_IF" "$SERVER_IP" "$PREFIX" "$ROUTER_IP" "$ROUTER_IP")
		say "$yaml" | sed 's/^/    /'
		confirm "Write it and apply it (netplan apply)?" y || return 0
		need_root
		printf '%s\n' "$yaml" | $SUDO tee "$NETPLAN_FILE" >/dev/null
		$SUDO chmod 600 "$NETPLAN_FILE"
		$SUDO netplan generate || {
			$SUDO rm -f "$NETPLAN_FILE"
			die "netplan rejected the change (removed again). Set a fixed address by hand and run this again."
		}
		$SUDO netplan apply
	elif [ -f /etc/dhcpcd.conf ] && systemctl is-active -q dhcpcd 2>/dev/null; then
		say "dhcpcd manages the network. Tattle would add to /etc/dhcpcd.conf:"
		local block
		block=$(printf '\n# Tattle: fixed address for gateway mode\ninterface %s\nstatic ip_address=%s/%s\nstatic routers=%s\nstatic domain_name_servers=%s\n' \
			"$LAN_IF" "$SERVER_IP" "$PREFIX" "$ROUTER_IP" "$ROUTER_IP")
		say "$block" | sed 's/^/    /'
		confirm "Add it and restart dhcpcd?" y || return 0
		need_root
		printf '%s\n' "$block" | $SUDO tee -a /etc/dhcpcd.conf >/dev/null
		$SUDO systemctl restart dhcpcd
	else
		say "Tattle can't tell how this system configures its network, so please set it by hand, as a manual"
		say "(static) IPv4 address: $SERVER_IP/$PREFIX, gateway $ROUTER_IP, DNS $ROUTER_IP on $LAN_IF."
		say "  systemd-networkd: in /etc/systemd/network/<file>.network set DHCP=no and add"
		say "      Address=$SERVER_IP/$PREFIX, Gateway=$ROUTER_IP, DNS=$ROUTER_IP; then: sudo networkctl reload"
		say "  /etc/network/interfaces: iface $LAN_IF inet static / address $SERVER_IP/$PREFIX / gateway $ROUTER_IP"
		confirm "Continue anyway? (The gateway refuses to start until the address is fixed.)" n || exit 1
		return 0
	fi
	sleep 3
	if ip -4 -o addr show dev "$LAN_IF" | grep " $SERVER_IP/.* dynamic" >/dev/null; then
		warn "$SERVER_IP still looks like a DHCP lease. Check the network settings; the gateway won't start until it's fixed."
	elif ! ip -4 -o addr show dev "$LAN_IF" | grep " $SERVER_IP/" >/dev/null; then
		warn "$LAN_IF no longer has $SERVER_IP. Check the network settings before going on."
	else
		say "$SERVER_IP is now a fixed address."
	fi
}

enable_forwarding() {
	step "Forwarding"
	if [ "$(cat /proc/sys/net/ipv4/ip_forward)" = 1 ] && [ -f "$SYSCTL_FILE" ]; then
		say "IP forwarding is already on for good."
		return 0
	fi
	say "Devices' traffic passes through this server, so Linux must forward it. Tattle would write"
	say "$SYSCTL_FILE (net.ipv4.ip_forward=1) and apply it now."
	confirm "Turn on IP forwarding?" y || {
		say "The gateway won't start without it."
		return 0
	}
	need_root
	printf '# Tattle gateway mode: pass devices traffic on to the router.\nnet.ipv4.ip_forward=1\n' | $SUDO tee "$SYSCTL_FILE" >/dev/null
	$SUDO sysctl -q -p "$SYSCTL_FILE"
	[ "$(cat /proc/sys/net/ipv4/ip_forward)" = 1 ] || warn "IP forwarding is still off; another setting overrides it (check /etc/sysctl.conf)."
}

open_firewall() {
	local port=$1 gateway=$2
	if systemctl is-active -q firewalld 2>/dev/null; then
		local zone
		zone=$($SUDO firewall-cmd --get-zone-of-interface="$LAN_IF" 2>/dev/null || $SUDO firewall-cmd --get-default-zone)
		step "Firewall (firewalld, zone $zone)"
		local args=(--add-port="$port/tcp")
		if [ "$gateway" = 1 ]; then
			args+=(--add-service=dhcp --add-service=dns)
			if $SUDO firewall-cmd --help 2>/dev/null | grep -- '--add-forward' >/dev/null; then
				args+=(--add-forward)
			else
				warn "This firewalld is too old for --add-forward (needs 1.0+); it may block devices' traffic. See docs/gateway-mode.md."
			fi
			say "Devices must reach this server's DHCP and DNS, and their traffic must pass through it:"
		else
			say "Devices must reach the dashboard:"
		fi
		say "  firewall-cmd --permanent --zone=$zone ${args[*]} && firewall-cmd --reload"
		confirm "Open these in firewalld?" y || return 0
		need_root
		$SUDO firewall-cmd --permanent --zone="$zone" "${args[@]}" >/dev/null
		$SUDO firewall-cmd --reload >/dev/null
	elif command -v ufw >/dev/null 2>&1 && $SUDO ufw status 2>/dev/null | grep "Status: active" >/dev/null; then
		step "Firewall (ufw)"
		local rules=("allow $port/tcp")
		if [ "$gateway" = 1 ]; then
			rules+=("allow in on $LAN_IF to any port 67 proto udp" "allow in on $LAN_IF to any port 53" "route allow in on $LAN_IF out on $LAN_IF")
		fi
		say "Tattle would add these ufw rules:"
		printf '  ufw %s\n' "${rules[@]}"
		confirm "Add them?" y || return 0
		need_root
		local rule
		for rule in "${rules[@]}"; do
			# shellcheck disable=SC2086 # each rule is several words on purpose
			$SUDO ufw $rule >/dev/null
		done
	fi
	return 0
}

ensure_xtables_lock() {
	step "Firewall lock"
	if [ -d /run/xtables.lock ]; then
		warn "/run/xtables.lock is a folder (a container created it while it was missing), which breaks iptables. Fix: sudo docker compose --profile gateway rm -sf gateway; sudo rmdir /run/xtables.lock; then run this again."
		return 0
	fi
	say "The gateway and Docker share the host's iptables lock file (/run/xtables.lock). Tattle would make"
	say "sure it exists at every boot ($TMPFILES_FILE), so Docker never creates it as a folder."
	confirm "Add it?" y || return 0
	need_root
	printf '# Tattle gateway: the iptables lock the gateway container shares with the host.\nf /run/xtables.lock 0600 root root -\n' |
		$SUDO tee "$TMPFILES_FILE" >/dev/null
	$SUDO systemd-tmpfiles --create "$TMPFILES_FILE" 2>/dev/null || $SUDO touch /run/xtables.lock
}

merge_json() { # stdin: daemon.json (maybe empty); stdout: the same with live-restore on
	local code='import json, sys
raw = sys.stdin.read().strip() or "{}"
data = json.loads(raw)
if not isinstance(data, dict):
    raise SystemExit("not a JSON object")
data["live-restore"] = True
print(json.dumps(data, indent=2))'
	if command -v python3 >/dev/null 2>&1; then
		python3 -c "$code"
	else
		$DOCKER run --rm -i --network none --entrypoint python "ghcr.io/khaledsharara/tattle:$VERSION" -c "$code"
	fi
}

enable_live_restore() {
	[ "$($DOCKER info --format '{{.LiveRestoreEnabled}}' 2>/dev/null)" = true ] && return 0
	[ "$($DOCKER info --format '{{.Swarm.LocalNodeState}}' 2>/dev/null)" = active ] && return 0 # not with Swarm
	step "Docker live-restore"
	say "With live-restore, containers keep running while Docker itself restarts or updates, so the"
	say "house keeps its gateway during a Docker upgrade. Tattle would add \"live-restore\": true to"
	say "/etc/docker/daemon.json (keeping everything else) and reload Docker's settings; running"
	say "containers aren't stopped."
	confirm "Turn on live-restore?" y || return 0
	need_root
	local current merged
	current=$($SUDO cat /etc/docker/daemon.json 2>/dev/null || true)
	if ! merged=$(printf '%s' "$current" | merge_json); then
		warn "/etc/docker/daemon.json isn't valid JSON, so it was left alone."
		return 0
	fi
	$SUDO mkdir -p /etc/docker
	[ -n "$current" ] && $SUDO cp /etc/docker/daemon.json /etc/docker/daemon.json.before-tattle
	printf '%s\n' "$merged" | $SUDO tee /etc/docker/daemon.json >/dev/null
	$SUDO systemctl reload docker 2>/dev/null || $SUDO kill -HUP "$(pidof dockerd)"
	sleep 2
	if [ "$($DOCKER info --format '{{.LiveRestoreEnabled}}' 2>/dev/null)" = true ]; then
		say "live-restore is on."
	else
		warn "Docker didn't pick up live-restore; it applies after Docker's next restart."
	fi
}

pick_dhcp_range() {
	step "Addresses to hand out"
	local first=$((NET_INT + 1)) last=$((BCAST_INT - 1)) n
	local window=$((last - 199 > first ? last - 199 : first))
	declare -A used=()
	used[$(ip2int "$SERVER_IP")]=1
	used[$(ip2int "$ROUTER_IP")]=1
	say "Looking for addresses already in use (a few seconds)..."
	if command -v ping >/dev/null 2>&1; then
		local ip pids=0
		for ((n = window; n <= last; n++)); do
			ip=$(int2ip "$n")
			{ ping -c 1 -W 1 -q "$ip" >/dev/null 2>&1 && echo "$ip"; } >>"$SCAN_FILE" &
			pids=$((pids + 1))
			if [ $((pids % 64)) = 0 ]; then wait; fi
		done
		wait
	fi
	ip -4 neigh show dev "$LAN_IF" | awk '$NF != "FAILED" && $NF != "INCOMPLETE" {print $1}' >>"$SCAN_FILE"
	while read -r ip; do
		[ -n "$ip" ] && used[$(ip2int "$ip")]=1
	done <"$SCAN_FILE"
	local best_start=0 best_len=0 run_start=-1
	for ((n = window; n <= last + 1; n++)); do
		if [ "$n" -le "$last" ] && [ -z "${used[$n]:-}" ]; then
			[ "$run_start" -ge 0 ] || run_start=$n
			continue
		fi
		if [ "$run_start" -ge 0 ] && [ $((n - run_start)) -ge "$best_len" ]; then
			best_start=$run_start
			best_len=$((n - run_start))
		fi
		run_start=-1
	done
	[ "$best_len" -gt 0 ] || die "No free addresses found near the top of $LAN. Free some up, or set TATTLE_DHCP_RANGE in $DIR/.env."
	local suggested
	suggested="$(int2ip "$best_start"),$(int2ip $((best_start + best_len - 1)))"
	local in_use=$((${#used[@]} - 2))
	say "Found $in_use other address(es) in use; the longest free stretch near the top is $best_len addresses."
	[ "$best_len" -lt 50 ] && warn "That's a short range. Devices with a fixed address may be scattered; dnsmasq also checks an address is free before handing it out."
	local previous
	previous=$(env_get TATTLE_DHCP_RANGE)
	ask DHCP_RANGE "Range to hand out (first,last)" "${previous:-$suggested}"
	[[ "$DHCP_RANGE" =~ ^[0-9.]+,[0-9.]+$ ]] || die "The range should look like 192.168.1.50,192.168.1.250."
}

leave_gateway_mode() {
	step "Leaving gateway mode"
	say "Stopping the gateway and removing its firewall rules..."
	compose --profile gateway rm -sf gateway >/dev/null 2>&1 || true
	compose run --rm gateway off || warn "Couldn't remove the gateway's rules; run: cd $DIR && $DOCKER compose run --rm gateway off"
}

# --- the dashboard's first-run code ---------------------------------------------------------

show_setup_code() {
	local file="$DIR/data/setup-code.txt" cid started=0 code="" reader=""
	[ -r "$DIR/data" ] && [ "$(id -u)" = "$(stat -c %u "$DIR/data")" ] || reader=$SUDO
	cid=$(compose ps -q tattle 2>/dev/null || true)
	if [ -n "$cid" ]; then
		started=$(date -d "$($DOCKER inspect -f '{{.State.StartedAt}}' "$cid" 2>/dev/null)" +%s 2>/dev/null || echo 0)
	fi
	for _ in $(seq 30); do
		if $reader test -f "$file" && [ "$($reader stat -c %Y "$file")" -ge $((started - 2)) ]; then
			code=$($reader cat "$file" | tr -dc '0-9')
			break
		fi
		sleep 1
	done
	say ""
	say "Dashboard: http://$SERVER_IP:$PORT"
	if [ -n "$code" ]; then
		say "Open http://$SERVER_IP:$PORT and enter setup code $code"
	else
		say "No setup code was written, so an admin password is already set: sign in with it."
		say "(Or check: cd $DIR && $DOCKER compose logs tattle | grep -i 'setup code')"
	fi
}

gateway_started() {
	local state="" detail
	for _ in $(seq 20); do
		state=$(compose exec -T tattle python -c 'import json; print(json.load(open("/run/tattle/gateway.json"))["state"])' 2>/dev/null || true)
		[ "$state" = running ] || [ "$state" = refused ] && break
		sleep 1
	done
	if [ "$state" = running ]; then
		say "The gateway is running."
		return 0
	fi
	detail=$(compose exec -T tattle python -c 'import json
for c in json.load(open("/run/tattle/gateway.json"))["preflight"]:
    if not c["ok"]: print("  - " + c["detail"])' 2>/dev/null || true)
	warn "The gateway isn't running yet${detail:+. It says:}"
	[ -n "$detail" ] && say "$detail"
	say "It keeps retrying on its own. Logs: cd $DIR && $DOCKER compose logs gateway"
	return 1
}

router_dhcp_check() {
	[ "$YES" = 1 ] && return 0
	tty_ok || return 0
	say ""
	local reply cid foreign
	ask reply "Press Enter once the router's DHCP is off, to check (or type skip)" ""
	[ "$reply" = skip ] && return 0
	cid=$(compose ps -q gateway)
	$DOCKER kill -s USR1 "$cid" >/dev/null # probe now
	sleep 6
	foreign=$(compose exec -T tattle python -c 'import json
for s in json.load(open("/run/tattle/gateway.json"))["dhcp"]["foreign_servers"]:
    print("  - " + s["ip"] + " (" + s["mac"] + ")")' 2>/dev/null || true)
	if [ -n "$foreign" ]; then
		warn "Another DHCP server still answers:"
		say "$foreign"
		say "Turn off DHCP there (on an extender: set it to Off, not Auto). The dashboard keeps checking."
	else
		say "Only Tattle hands out addresses now."
	fi
}

checklist() {
	cat <<EOF

Tattle is now ready to be the network's gateway. Finish on the router and extenders:

  1. Turn OFF the router's DHCP server (usually under LAN, Network or DHCP settings,
     at http://$ROUTER_IP). Tattle hands out addresses from now on.
  2. If the router has IPv6 settings, turn off IPv6 on the LAN, or at least its router
     advertisements (SLAAC/RA) and DHCPv6. Tattle counts IPv4 traffic only.
  3. Set every Wi-Fi extender's DHCP server to Off (not "Auto": Auto starts a second
     DHCP server whenever the extender stops hearing the router's).
  4. Reconnect devices (Wi-Fi off and on), or wait: each moves over within 2 hours.

Devices appear on the dashboard as they reconnect; its health panel says if anything's wrong.
If the internet ever stops: turn the router's DHCP back on and reconnect Wi-Fi.
More: https://github.com/khaledsharara/tattle/blob/main/docs/gateway-mode.md
EOF
}

# --- install / uninstall -------------------------------------------------------------------

install() {
	check_system
	find_docker
	mkdir -p "$DIR"
	detect_network
	choose_mode

	step "Files in $DIR"
	curl -fsSL "$COMPOSE_URL" -o "$DIR/docker-compose.yml.new" || die "Couldn't download $COMPOSE_URL"
	mv "$DIR/docker-compose.yml.new" "$DIR/docker-compose.yml"
	if [ ! -f "$DIR/.env" ]; then
		printf '# Tattle settings, written by install.sh. See .env.example in the repository.\n' >"$DIR/.env"
		chmod 600 "$DIR/.env"
	fi
	VERSION=$(env_get TATTLE_VERSION)
	case "$REF" in v[0-9]*) VERSION=${REF#v} ;; esac
	VERSION=${VERSION:-latest}
	PORT=$(env_get TATTLE_PORT)
	PORT=${PORT:-8090}
	env_set TATTLE_MODE "$MODE"
	env_set TZ "$TZ_NAME"
	env_set TATTLE_PORT "$PORT"
	env_set TATTLE_VERSION "$VERSION"
	env_set TATTLE_LAN_IF "$LAN_IF"
	env_set TATTLE_SERVER_IP "$SERVER_IP"
	env_set TATTLE_LAN "$LAN"
	[ -n "$ROUTER_IP" ] && env_set TATTLE_ROUTER_IP "$ROUTER_IP"
	mkdir -p "$DIR/data"
	if [ "$(stat -c %u:%g "$DIR/data")" != 1000:1000 ]; then
		need_root
		$SUDO chown 1000:1000 "$DIR/data" # the dashboard runs as uid 1000
	fi
	say "Wrote docker-compose.yml and .env; your data lives in $DIR/data."

	step "Downloading Tattle $VERSION"
	compose pull tattle

	[ "$PREV_MODE" = gateway ] && [ "$MODE" != gateway ] && leave_gateway_mode

	if [ "$MODE" = gateway ]; then
		make_static
		enable_forwarding
		open_firewall "$PORT" 1
		ensure_xtables_lock
		enable_live_restore
		SCAN_FILE=$(mktemp)
		pick_dhcp_range
		rm -f "$SCAN_FILE"
		env_set TATTLE_DHCP_RANGE "$DHCP_RANGE"
		[ -n "$(env_get TATTLE_DNS)" ] || env_set TATTLE_DNS "$ROUTER_IP"
		step "Starting Tattle (gateway mode)"
		compose --profile gateway up -d --remove-orphans
	else
		open_firewall "$PORT" 0
		step "Starting Tattle ($MODE mode)"
		compose up -d --remove-orphans
	fi
	show_setup_code
	if [ "$MODE" = gateway ]; then
		gateway_started && { checklist; router_dhcp_check; }
	elif [ "$PREV_MODE" = gateway ]; then
		say ""
		say "Gateway mode is off. Turn the router's DHCP server back on (at http://$ROUTER_IP), then reconnect"
		say "devices' Wi-Fi; devices you don't touch switch back within 2 hours."
	fi
	if [ "$MODE" = router ]; then
		say "Pick your router and sign in under Settings in the dashboard."
	fi
}

uninstall() {
	local purge=$1
	[ -f "$DIR/docker-compose.yml" ] || die "No Tattle found in $DIR (set TATTLE_DIR if it's elsewhere)."
	find_docker
	local mode
	mode=$(env_get TATTLE_MODE)
	step "Removing Tattle from $DIR"
	if [ "$mode" = gateway ] || [ -n "$(compose --profile gateway ps -aq gateway 2>/dev/null)" ]; then
		leave_gateway_mode
	fi
	if [ "$purge" = 1 ]; then
		compose --profile gateway down --remove-orphans --volumes --rmi all || true
	else
		compose --profile gateway down --remove-orphans || true
	fi
	if [ "$mode" = gateway ]; then
		local files=()
		for f in "$SYSCTL_FILE" "$TMPFILES_FILE" "$NETPLAN_FILE"; do [ -f "$f" ] && files+=("$f"); done
		if [ "${#files[@]}" -gt 0 ]; then
			say "Gateway mode added: ${files[*]}"
			say "(Removing them leaves the running system as it is; a fixed address set with netplan reverts to DHCP.)"
			confirm "Remove them too?" n && {
				need_root
				$SUDO rm -f "${files[@]}"
			}
		fi
		say "Turn the router's DHCP server back on, then reconnect devices' Wi-Fi."
		say "Firewall openings for DHCP/DNS/forwarding (firewalld/ufw) were left in place; remove them if you like."
	fi
	if [ "$purge" = 1 ]; then
		if confirm "Delete $DIR, including your usage history?" n; then
			need_root
			$SUDO rm -rf "$DIR"
			say "Tattle and its data are gone."
			return 0
		fi
	fi
	say "Tattle is removed. Your data and settings stay in $DIR (run with --uninstall --purge to delete them)."
}

usage() {
	sed -n '2,19p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//' || true
}

main() {
	local action=install purge=0
	while [ $# -gt 0 ]; do
		case "$1" in
		--uninstall) action=uninstall ;;
		--purge) purge=1 ;;
		-h | --help)
			usage
			exit 0
			;;
		*) die "Unknown option: $1 (try --help)" ;;
		esac
		shift
	done
	if [ "$action" = uninstall ]; then
		uninstall "$purge"
	else
		install
	fi
}

main "$@"
