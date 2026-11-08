#!/usr/bin/env bash
# Bring the optimizer node back after `docker kill` / a fail-safe exit.
#
# Killing a container destroys its network namespace and with it the
# optimizer:eth1 <-> edge:eth4 veth. `containerlab start` only restores links
# of nodes stopped with `containerlab stop`, so we start the container and
# recreate the veth ourselves. The entrypoint waits for eth1 to appear and
# edge (FRR) re-applies its eth4 address automatically.
source "$(dirname "$0")/common.sh"

node="$(cname optimizer)"

if ! is_running optimizer; then
    log "starting ${node}"
    docker start "$node" >/dev/null
fi

if ! dx optimizer ip link show eth1 >/dev/null 2>&1; then
    log "recreating link optimizer:eth1 <-> edge:eth4"
    containerlab tools veth create -a "${node}:eth1" -b "$(cname edge):eth4" >/dev/null
fi

log "waiting for iBGP edge <-> optimizer"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-60}" wait_bgp_established edge 10.9.0.2 || die "iBGP did not come up"
log "optimizer is back"
