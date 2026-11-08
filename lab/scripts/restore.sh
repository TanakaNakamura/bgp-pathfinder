#!/usr/bin/env bash
# restore.sh <a|b> -- remove netem from the edge<->ISP link.
source "$(dirname "$0")/common.sh"

[ $# -eq 1 ] || die "usage: $0 <a|b>"

case "$1" in
    a) edge_if=eth2 isp=isp-a ;;
    b) edge_if=eth3 isp=isp-b ;;
    *) die "provider must be 'a' or 'b'" ;;
esac

log "restoring edge<->${isp}"
dx edge tc qdisc del dev "$edge_if" root 2>/dev/null || true
dx "$isp" tc qdisc del dev eth1 root 2>/dev/null || true

echo "edge/${edge_if}: $(dx edge tc qdisc show dev "$edge_if" | head -1)"
echo "${isp}/eth1: $(dx "$isp" tc qdisc show dev eth1 | head -1)"
