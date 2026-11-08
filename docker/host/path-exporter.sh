#!/bin/sh
# Data-plane "ground truth" for the Grafana dashboard, run on the client node.
#
# Every second: ping the target (what a user would feel) and traceroute it
# with max 2 hops (hop 2 is the ISP router edge forwarded the packet to, i.e.
# where client traffic REALLY goes, whatever the optimizer believes).
# Exposed in Prometheus text format by busybox httpd on :9100/metrics.txt.

TARGET="${TARGET:-198.51.100.10}"
PORT="${METRICS_PORT:-9100}"
DIR=/var/metrics
ISP_A_HOP="${ISP_A_HOP:-10.1.0.2}"
ISP_B_HOP="${ISP_B_HOP:-10.2.0.2}"

mkdir -p "$DIR"
echo "# starting" > "$DIR/metrics.txt"
httpd -p "$PORT" -h "$DIR"

while true; do
    rtt=$(ping -n -c 1 -W 1 "$TARGET" 2>/dev/null | sed -n 's/.*time=\([0-9.]*\) ms.*/\1/p')
    hop=$(traceroute -n -m 2 -q 1 -w 1 "$TARGET" 2>/dev/null | awk '$1 == 2 {print $2}')
    case "$hop" in
        "$ISP_A_HOP") via=1 ;;
        "$ISP_B_HOP") via=2 ;;
        *) via=0 ;;
    esac
    {
        echo "# HELP lab_client_rtt_ms Client -> target ping RTT (absent when lost)"
        echo "# TYPE lab_client_rtt_ms gauge"
        [ -n "$rtt" ] && echo "lab_client_rtt_ms $rtt"
        echo "# HELP lab_client_ping_ok 1 if the last client -> target ping got a reply"
        echo "# TYPE lab_client_ping_ok gauge"
        if [ -n "$rtt" ]; then echo "lab_client_ping_ok 1"; else echo "lab_client_ping_ok 0"; fi
        echo "# HELP lab_client_path_provider Provider the client traffic really uses: 1 ISP-A, 2 ISP-B, 0 unknown"
        echo "# TYPE lab_client_path_provider gauge"
        echo "lab_client_path_provider $via"
        echo "# HELP lab_client_path_hop Second traceroute hop seen from the client"
        echo "# TYPE lab_client_path_hop gauge"
        echo "lab_client_path_hop{hop=\"${hop:-none}\"} 1"
    } > "$DIR/metrics.tmp" && mv "$DIR/metrics.tmp" "$DIR/metrics.txt"
    sleep 1
done
