#!/usr/bin/env bash
# Shared helpers for lab scripts. Source it, do not execute it.

set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$LAB_DIR/.." && pwd)"
TOPO="$LAB_DIR/topology.clab.yml"
LAB_NAME="bgpopt"
HOST_IMAGE="bgpopt-host:0.1.0"
OPTIMIZER_IMAGE="bgpopt-optimizer:0.1.0"
BGPJSON="$LAB_DIR/scripts/bgpjson.py"

# container name of a lab node
cname() { echo "clab-${LAB_NAME}-$1"; }

# run a command inside a lab node
dx() {
    local node="$1"
    shift
    docker exec "$(cname "$node")" "$@"
}

# run one or more vtysh commands on a FRR node
vtysh_on() {
    local node="$1"
    shift
    local args=()
    local cmd
    for cmd in "$@"; do
        args+=(-c "$cmd")
    done
    docker exec "$(cname "$node")" vtysh "${args[@]}"
}

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() {
    printf '\033[1;31mxx\033[0m %s\n' "$*" >&2
    exit 1
}

is_running() {
    [ "$(docker inspect -f '{{.State.Running}}' "$(cname "$1")" 2>/dev/null)" = "true" ]
}

# wait_bgp_established <node> <peer-ip>... [timeout via WAIT_TIMEOUT]
wait_bgp_established() {
    local node="$1"
    shift
    local deadline=$((SECONDS + ${WAIT_TIMEOUT:-90}))
    while ((SECONDS < deadline)); do
        if vtysh_on "$node" "show bgp ipv4 unicast summary json" 2>/dev/null |
            python3 "$BGPJSON" established "$@" >/dev/null; then
            return 0
        fi
        sleep 2
    done
    vtysh_on "$node" "show bgp ipv4 unicast summary json" | python3 "$BGPJSON" established "$@" || true
    return 1
}
