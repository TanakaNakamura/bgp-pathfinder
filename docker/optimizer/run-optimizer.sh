#!/bin/sh
# ExaBGP helper wrapper (fail-safe).
#
# ExaBGP 4.2 does NOT drop the BGP session when its helper process dies: it
# either respawns it or just logs "process ended" and keeps announcing the
# routes. To get "optimizer down => injected routes gone" we terminate ExaBGP
# ourselves as soon as the optimizer exits, for any reason (crash, SIGKILL,
# OOM, bad config). ExaBGP then sends a CEASE notification and edge withdraws
# everything it learned from us. See docs/DECISIONS.md.

CONFIG="${OPTIMIZER_CONFIG:-/etc/optimizer/config.yaml}"

python -m optimizer.main --config "$CONFIG"
rc=$?

printf '{"ts":"%s","level":"CRITICAL","logger":"supervisor","event":"optimizer_exited","rc":%d,"action":"terminate_exabgp","exabgp_pid":%d}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$rc" "$PPID" >&2

kill -TERM "$PPID" 2>/dev/null
exit "$rc"
