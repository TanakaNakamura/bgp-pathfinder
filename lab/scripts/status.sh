#!/usr/bin/env bash
# Show the routing state of the lab and the optimizer status.
source "$(dirname "$0")/common.sh"

log "edge: show ip bgp 198.51.100.0/24"
vtysh_on edge "show ip bgp 198.51.100.0/24" || true

log "client -> target: traceroute"
dx client traceroute -n -w 1 -q 1 198.51.100.10 || true

log "optimizer status"
if is_running optimizer; then
    dx optimizer optimizer status || true
else
    warn "optimizer container is not running"
fi

log "dashboard: http://localhost:3000"
