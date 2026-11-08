#!/bin/sh
# Optimizer container entrypoint: configure the data-plane interface, then
# exec ExaBGP as PID 1 (ExaBGP starts the optimizer as its helper process).
set -eu

IFACE="${OPT_IFACE:-eth1}"
LINK_ADDR="${OPT_LINK_ADDR:-10.9.0.2/30}"
GATEWAY="${OPT_GATEWAY:-10.9.0.1}"
PROBE_ADDRS="${OPT_PROBE_ADDRS:-10.99.1.1/32 10.99.2.1/32}"
PROBE_NET="${OPT_PROBE_NET:-10.99.0.0/16}"

log() {
    printf '{"ts":"%s","level":"INFO","logger":"entrypoint","event":"%s"}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >&2
}

# containerlab may attach the veth after the container has started
i=0
until ip link show "$IFACE" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -gt 240 ]; then
        log "interface ${IFACE} did not appear, giving up"
        exit 1
    fi
    sleep 0.5
done

ip link set "$IFACE" up
ip addr replace "$LINK_ADDR" dev "$IFACE"
for a in $PROBE_ADDRS; do
    ip addr replace "$a" dev "$IFACE"
done

# Everything sourced from a probe address goes to edge, whatever the
# destination; edge then pins it to the right ISP with its own ip rules.
ip rule del priority 100 2>/dev/null || true
ip rule add from "$PROBE_NET" lookup 100 priority 100
ip route replace default via "$GATEWAY" dev "$IFACE" table 100

log "dataplane ready on ${IFACE}: ${LINK_ADDR} ${PROBE_ADDRS}"

exec exabgp /etc/exabgp/exabgp.conf
