#!/bin/sh
# busybox udhcpc hook for the gateway e2e test. It only sets the address and
# default route on the test interface and records what the DHCP server sent in
# $UDHCPC_OUT. It never touches resolv.conf or anything outside its netns.
# shellcheck disable=SC2154 # udhcpc passes interface, ip, router... in the environment
out="${UDHCPC_OUT:-/tmp/udhcpc-lease.env}"

case "$1" in
deconfig)
	ip -4 addr flush dev "$interface"
	ip link set "$interface" up
	;;
bound | renew)
	ip -4 addr flush dev "$interface"
	ip -4 addr add "$ip/${mask:-24}" dev "$interface"
	for gw in $router; do
		ip -4 route replace default via "$gw" dev "$interface"
		break
	done
	{
		echo "ip=$ip"
		echo "mask=$mask"
		echo "router=$router"
		echo "dns=\"$dns\""
		echo "serverid=$serverid"
		echo "lease=$lease"
	} >"$out.tmp"
	mv "$out.tmp" "$out"
	;;
esac
exit 0
