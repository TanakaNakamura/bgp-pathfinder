#!/usr/bin/env bash
# degrade.sh <a|b> <delay_ms> <loss_pct>
# Impair the edge<->ISP link with tc netem in BOTH directions
# (egress of edge towards the ISP and egress of the ISP towards edge).
source "$(dirname "$0")/common.sh"

[ $# -eq 3 ] || die "usage: $0 <a|b> <delay_ms> <loss_pct>"

case "$1" in
    a) edge_if=eth2 isp=isp-a ;;
    b) edge_if=eth3 isp=isp-b ;;
    *) die "provider must be 'a' or 'b'" ;;
esac
delay="$2"
loss="$3"

[[ "$delay" =~ ^[0-9]+$ ]] || die "delay_ms must be an integer"
[[ "$loss" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "loss_pct must be a number"

log "degrading edge<->${isp}: delay ${delay}ms loss ${loss}% (both directions)"
dx edge tc qdisc replace dev "$edge_if" root netem delay "${delay}ms" loss "${loss}%"
dx "$isp" tc qdisc replace dev eth1 root netem delay "${delay}ms" loss "${loss}%"

echo "edge/${edge_if}: $(dx edge tc qdisc show dev "$edge_if" | head -1)"
echo "${isp}/eth1: $(dx "$isp" tc qdisc show dev eth1 | head -1)"
