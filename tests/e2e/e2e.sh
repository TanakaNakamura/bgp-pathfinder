#!/usr/bin/env bash
# End-to-end acceptance test: stages 1, 2, 4 and 5 of the prototype.
# Requires a running lab (`make lab-up`). Exit code 0 only if every check passes.
#
# Flow (~4-5 min):
#   stage 1  BGP up, base path via ISP-A, traceroute via ISP-A, outbound filters
#   stage 2  probes pinned per ISP (traceroute), degrade A changes only A's metrics
#   stage 4  dry-run logs a decision but injects nothing; live mode switches
#            edge to ISP-B (local-pref 300, no-advertise) within 60 s; nothing leaks
#   stage 5  fail-safe (docker kill, optimizer crash); restore A -> exactly one
#            return, not before hold-down, no flapping

source "$(cd "$(dirname "$0")/../.." && pwd)/lab/scripts/common.sh"
set +e  # we count failures ourselves

PREFIX=198.51.100.0/24
TARGET=198.51.100.10
HOLD_DOWN=$(awk '/hold_down_s:/ {print $2}' "$LAB_DIR/configs/optimizer/config.yaml")
SWITCH_TIMEOUT=60
STABILITY_S=30

PASS=0
FAIL=0
STARTED=$SECONDS

# ---------------------------------------------------------------- helpers
ok() {
    PASS=$((PASS + 1))
    printf '  \033[32mPASS\033[0m %s\n' "$*"
}
ko() {
    FAIL=$((FAIL + 1))
    printf '  \033[31mFAIL\033[0m %s\n' "$*"
}
check() { # check "description" command...
    local desc="$1"
    shift
    if "$@"; then ok "$desc"; else ko "$desc"; fi
}
quiet() { "$@" >/dev/null 2>&1; }
eq() { [ "$1" = "$2" ] || { echo "    expected '$2', got '$1'"; return 1; }; }
stage() { printf '\n\033[1;36m### %s\033[0m  (t=%ss)\n' "$*" "$((SECONDS - STARTED))"; }

# "<nexthop> <locpref> <peer> <aspath> best" of edge's best path to $PREFIX
edge_best() { vtysh_on edge "show ip bgp $PREFIX json" | python3 "$BGPJSON" best; }
edge_best_nh_lp() { edge_best | awk '{print $1, $2}'; }

# wait_best "<nexthop> <locpref>" <timeout_s>  -> prints elapsed seconds, rc 0 on success
wait_best() {
    local want="$1" timeout="$2" t0=$SECONDS
    while ((SECONDS - t0 < timeout)); do
        if [ "$(edge_best_nh_lp)" = "$want" ]; then
            echo $((SECONDS - t0))
            return 0
        fi
        sleep 1
    done
    echo $((SECONDS - t0))
    return 1
}

# second hop of a traceroute run inside <node>
hop2() {
    local node="$1"
    shift
    dx "$node" traceroute -n -w 1 -q 1 "$@" "$TARGET" 2>/dev/null | awk '$1 == 2 {print $2}'
}

received_from_edge() { # <isp-node> <edge-ip>
    vtysh_on "$1" "show ip bgp neighbors $2 routes json" | python3 "$BGPJSON" prefixes
}
advertised_by_edge() { # <isp-ip>
    vtysh_on edge "show ip bgp neighbors $1 advertised-routes json" | python3 "$BGPJSON" prefixes
}
from_optimizer() { vtysh_on edge "show ip bgp neighbors 10.9.0.2 routes json" | python3 "$BGPJSON" prefixes; }

opt() { dx optimizer optimizer "$@"; }

# metric of one provider from `optimizer probe|status --json`: provider_metric <json> <provider> <field>
provider_metric() {
    python3 -c '
import json, sys
rows = json.loads(sys.argv[1])
rows = rows["providers"] if isinstance(rows, dict) else rows
for r in rows:
    if r["provider"] == sys.argv[2]:
        v = r[sys.argv[3]]
        print("nan" if v is None else v)
' "$1" "$2" "$3"
}
num_lt() { python3 -c "import sys; sys.exit(0 if float('$1') < float('$2') else 1)"; }
num_ge() { python3 -c "import sys; sys.exit(0 if float('$1') >= float('$2') else 1)"; }

# live decisions as "ts action" lines (oldest first)
live_decisions() {
    opt history --json -n 500 | python3 -c '
import json, sys
for r in json.load(sys.stdin)["decisions"]:
    if not r["dry_run"]:
        print(r["ts"], r["action"])
'
}
# dry-run "announce -> isp-b" decisions recorded at or after epoch <since>
count_dry_run_announces() {
    opt history --json -n 500 | python3 -c '
import json, sys
since = float(sys.argv[1])
print(sum(1 for r in json.load(sys.stdin)["decisions"]
          if r["dry_run"] and r["action"] == "announce" and r["to_provider"] == "isp-b" and r["ts"] >= since))
' "$1"
}
# log_event_since <epoch> <event> [key=value...]: the optimizer JSON log has a
# matching line written at or after <epoch>
log_event_since() {
    local since="$1" event="$2"
    shift 2
    dx optimizer python3 -c '
import json, sys
from datetime import datetime
since, event = float(sys.argv[1]), sys.argv[2]
want = dict(kv.split("=", 1) for kv in sys.argv[3:])
for line in open("/var/log/optimizer/optimizer.jsonl"):
    d = json.loads(line)
    if d.get("event") != event or datetime.fromisoformat(d["ts"]).timestamp() < since:
        continue
    if all(str(d.get(k)) == v for k, v in want.items()):
        sys.exit(0)
sys.exit(1)
' "$since" "$event" "$@"
}

cleanup() {
    "$LAB_DIR/scripts/restore.sh" a >/dev/null 2>&1
    "$LAB_DIR/scripts/restore.sh" b >/dev/null 2>&1
    if ! is_running optimizer || ! dx optimizer ip link show eth1 >/dev/null 2>&1; then
        "$LAB_DIR/scripts/optimizer-start.sh" >/dev/null 2>&1
    fi
    opt mode live >/dev/null 2>&1
}
trap cleanup EXIT

# ---------------------------------------------------------------- preflight
stage "preflight"
for n in edge isp-a isp-b inet client target optimizer; do
    is_running "$n" || die "node $n is not running - run 'make lab-up' first"
done
"$LAB_DIR/scripts/restore.sh" a >/dev/null
"$LAB_DIR/scripts/restore.sh" b >/dev/null
# dry-run withdraws any live override and resets the engine -> plain BGP
check "optimizer switched to dry-run" quiet opt mode dry-run
echo "  hold-down from config: ${HOLD_DOWN}s"

# ---------------------------------------------------------------- stage 1
stage "stage 1: lab"
check "edge: eBGP isp-a, isp-b and iBGP optimizer Established" \
    wait_bgp_established edge 10.1.0.2 10.2.0.2 10.9.0.2
check "isp-a sessions Established" wait_bgp_established isp-a 10.1.0.1 10.3.0.2
check "isp-b sessions Established" wait_bgp_established isp-b 10.2.0.1 10.4.0.2
check "inet sessions Established" wait_bgp_established inet 10.3.0.1 10.4.0.1
check "edge best path to $PREFIX via ISP-A (10.1.0.2, local-pref 150)" eq "$(edge_best_nh_lp)" "10.1.0.2 150"
check "traceroute client -> target via 10.1.0.2" eq "$(hop2 client)" "10.1.0.2"
check "isp-a receives from edge only 10.99.1.0/24 203.0.113.0/24" \
    eq "$(received_from_edge isp-a 10.1.0.1)" "10.99.1.0/24 203.0.113.0/24"
check "isp-b receives from edge only 10.99.2.0/24 203.0.113.0/24" \
    eq "$(received_from_edge isp-b 10.2.0.1)" "10.99.2.0/24 203.0.113.0/24"

# ---------------------------------------------------------------- dashboard
stage "dashboard: Prometheus + Grafana"
# prom_value <expr>: first value of an instant query ("none" if empty)
prom_value() {
    curl -s --get localhost:9090/api/v1/query --data-urlencode "query=$1" | python3 -c '
import json, sys
r = json.load(sys.stdin)["data"]["result"]
print(r[0]["value"][1] if r else "none")'
}
prom_targets_up() {
    curl -s localhost:9090/api/v1/targets | python3 -c '
import json, sys
t = json.load(sys.stdin)["data"]["activeTargets"]
print(" ".join(sorted(x["labels"]["job"] for x in t if x["health"] == "up")))'
}
t0=$SECONDS
until [ "$(prom_targets_up)" = "client optimizer" ] || ((SECONDS - t0 > 30)); do sleep 2; done
check "Prometheus scrapes optimizer and client" eq "$(prom_targets_up)" "client optimizer"
check "Grafana is healthy" eq "$(curl -s localhost:3000/api/health | python3 -c 'import json,sys; print(json.load(sys.stdin)["database"])')" "ok"
check "dashboard 'bgp-optimizer' is provisioned and is the home page" \
    eq "$(curl -s localhost:3000/api/dashboards/home | python3 -c 'import json,sys; print("bgp-optimizer" in json.load(sys.stdin).get("redirectUri", ""))')" "True"
check "client ground-truth metric says traffic goes via ISP-A (1)" eq "$(prom_value lab_client_path_provider)" "1"

# ---------------------------------------------------------------- stage 2
stage "stage 2: probes"
check "probe from 10.99.1.1 goes via ISP-A (hop 2 = 10.1.0.2)" eq "$(hop2 optimizer -s 10.99.1.1)" "10.1.0.2"
check "probe from 10.99.2.1 goes via ISP-B (hop 2 = 10.2.0.2)" eq "$(hop2 optimizer -s 10.99.2.1)" "10.2.0.2"

base_json=$(opt probe -n 3 --json)
a0=$(provider_metric "$base_json" isp-a rtt_ms)
b0=$(provider_metric "$base_json" isp-b rtt_ms)
echo "  baseline: isp-a rtt ${a0} ms, isp-b rtt ${b0} ms"
# the daemon is in dry-run since preflight and reacts to this degradation too
# (stage 4a below checks that it only logged the decision)
T_DEGRADE_DRY=$(date +%s)
"$LAB_DIR/scripts/degrade.sh" a 150 5 >/dev/null
deg_json=$(opt probe -n 5 --json)
a1=$(provider_metric "$deg_json" isp-a rtt_ms)
a1_loss=$(provider_metric "$deg_json" isp-a loss_pct)
b1=$(provider_metric "$deg_json" isp-b rtt_ms)
b1_loss=$(provider_metric "$deg_json" isp-b loss_pct)
echo "  degraded: isp-a rtt ${a1} ms loss ${a1_loss} %, isp-b rtt ${b1} ms loss ${b1_loss} %"
check "degrade a 150 5: isp-a RTT >= 250 ms" num_ge "$a1" 250
check "degrade a 150 5: isp-b RTT unchanged (< 20 ms)" num_lt "$b1" 20
check "degrade a 150 5: isp-b loss unchanged (0 %)" num_lt "$b1_loss" 0.01

# ---------------------------------------------------------------- stage 4
stage "stage 4a: dry-run (ISP-A degraded)"
t0=$SECONDS
while ((SECONDS - t0 < SWITCH_TIMEOUT)) && [ "$(count_dry_run_announces "$T_DEGRADE_DRY")" -lt 1 ]; do sleep 2; done
dry_ts=$(opt history --json -n 500 | python3 -c '
import json, sys
rows = [r["ts"] for r in json.load(sys.stdin)["decisions"] if r["dry_run"] and r["ts"] >= float(sys.argv[1])]
print(round(rows[0] - float(sys.argv[1]), 1) if rows else "none")
' "$T_DEGRADE_DRY")
check "dry-run decision 'announce isp-a -> isp-b' in history (${dry_ts}s after degrade)" \
    num_ge "$(count_dry_run_announces "$T_DEGRADE_DRY")" 1
check "dry-run decision is in the JSON log (would announce, to isp-b, dry_run=True)" \
    log_event_since "$T_DEGRADE_DRY" "dry-run: would announce" to=isp-b dry_run=True
sleep 4
check "dry-run: edge best path still via ISP-A" eq "$(edge_best_nh_lp)" "10.1.0.2 150"
check "dry-run: edge received nothing from optimizer" eq "$(from_optimizer)" ""

stage "stage 4b: live mode"
"$LAB_DIR/scripts/restore.sh" a >/dev/null
echo "  waiting for isp-a metrics to recover before going live..."
t0=$SECONDS
while ((SECONDS - t0 < 60)); do
    num_lt "$(provider_metric "$(opt status --json)" isp-a rtt_ms)" 5 2>/dev/null && break
    sleep 2
done
check "optimizer switched to live" quiet opt mode live
sleep 6
check "live, healthy links: edge still on ISP-A" eq "$(edge_best_nh_lp)" "10.1.0.2 150"

"$LAB_DIR/scripts/degrade.sh" a 150 5 >/dev/null
elapsed=$(wait_best "10.2.0.2 300" "$SWITCH_TIMEOUT")
check "after degrade a 150 5 edge best path via 10.2.0.2 with local-pref 300 (${elapsed}s)" \
    eq "$(edge_best_nh_lp)" "10.2.0.2 300"
check "best path learned from optimizer (10.9.0.2)" eq "$(edge_best | awk '{print $3}')" "10.9.0.2"
check "injected route carries no-export no-advertise" eq "$(vtysh_on edge "show ip bgp $PREFIX json" | python3 "$BGPJSON" best-community)" "no-export no-advertise"
check "traceroute client -> target via ISP-B (hop 2 = 10.2.0.2)" eq "$(hop2 client)" "10.2.0.2"
t0=$SECONDS
until [ "$(prom_value lab_client_path_provider)" = "2" ] || ((SECONDS - t0 > 15)); do sleep 1; done
check "dashboard: client traffic shown via ISP-B" eq "$(prom_value lab_client_path_provider)" "2"
check "dashboard: optimizer decision shown as ISP-B" eq "$(prom_value 'max(optimizer_traffic_provider_index)')" "2"
check "edge does not advertise $PREFIX to isp-a" eq "$(advertised_by_edge 10.1.0.2)" "10.99.1.0/24 203.0.113.0/24"
check "edge does not advertise $PREFIX to isp-b" eq "$(advertised_by_edge 10.2.0.2)" "10.99.2.0/24 203.0.113.0/24"
check "isp-a has no $PREFIX from edge" eq "$(received_from_edge isp-a 10.1.0.1)" "10.99.1.0/24 203.0.113.0/24"
check "isp-b has no $PREFIX from edge" eq "$(received_from_edge isp-b 10.2.0.1)" "10.99.2.0/24 203.0.113.0/24"
check "optimizer status shows the active override" \
    eq "$(opt status --json | python3 -c 'import json,sys; print(" ".join(o["provider"] for o in json.load(sys.stdin)["overrides"]))')" "isp-b"

# ---------------------------------------------------------------- stage 5
stage "stage 5a: fail-safe - docker kill optimizer"
docker kill "$(cname optimizer)" >/dev/null
elapsed=$(wait_best "10.1.0.2 150" 15)
check "edge back on ISP-A after docker kill (${elapsed}s)" eq "$(edge_best_nh_lp)" "10.1.0.2 150"
OPT_STAT='(max(up{job="optimizer"}) == 0) or on() (1 + max(optimizer_dry_run)) or on() vector(0)'
t0=$SECONDS
until [ "$(prom_value "$OPT_STAT")" = "0" ] || ((SECONDS - t0 > 15)); do sleep 1; done
check "dashboard: optimizer shown DOWN" eq "$(prom_value "$OPT_STAT")" "0"
check "no routes from optimizer left on edge" eq "$(from_optimizer)" ""
check "traceroute client -> target via ISP-A again" eq "$(hop2 client)" "10.1.0.2"

check "optimizer node restarted" quiet "$LAB_DIR/scripts/optimizer-start.sh"
elapsed=$(wait_best "10.2.0.2 300" "$SWITCH_TIMEOUT")
check "restarted optimizer re-injects (ISP-A still degraded) (${elapsed}s)" eq "$(edge_best_nh_lp)" "10.2.0.2 300"

stage "stage 5b: fail-safe - optimizer process crash (kill -9), ExaBGP alive"
dx optimizer pkill -9 -f optimizer.main
elapsed=$(wait_best "10.1.0.2 150" 15)
check "wrapper terminated ExaBGP, edge back on ISP-A (${elapsed}s)" eq "$(edge_best_nh_lp)" "10.1.0.2 150"
check "no routes from optimizer left on edge" eq "$(from_optimizer)" ""
check "optimizer node restarted" quiet "$LAB_DIR/scripts/optimizer-start.sh"
elapsed=$(wait_best "10.2.0.2 300" "$SWITCH_TIMEOUT")
check "restarted optimizer re-injects (${elapsed}s)" eq "$(edge_best_nh_lp)" "10.2.0.2 300"

stage "stage 5c: restore -> one return, not before hold-down, no flapping"
"$LAB_DIR/scripts/restore.sh" a >/dev/null
T_RESTORE=$(date +%s)
elapsed=$(wait_best "10.1.0.2 150" $((HOLD_DOWN + 60)))
check "edge back on ISP-A after restore (${elapsed}s after restore)" eq "$(edge_best_nh_lp)" "10.1.0.2 150"
echo "  watching ${STABILITY_S}s for flapping..."
sleep "$STABILITY_S"

decisions=$(live_decisions)
echo "$decisions" | tail -4 | sed 's/^/    /'
last_announce=$(echo "$decisions" | awk '$2 == "announce" {t = $1} END {print t}')
after_announce=$(echo "$decisions" | awk -v t="$last_announce" '$1 > t')
withdraws=$(echo "$after_announce" | awk '$2 == "withdraw"' | grep -c . )
announces=$(echo "$after_announce" | awk '$2 == "announce"' | grep -c . )
check "exactly one return (withdraw) after the last announce" eq "$withdraws" "1"
check "no new announce after the return (no flap)" eq "$announces" "0"
withdraw_ts=$(echo "$after_announce" | awk '$2 == "withdraw" {print $1; exit}')
gap=$(python3 -c "print(round(${withdraw_ts:-0} - ${last_announce:-0}, 1))")
check "return happened ${gap}s after announce (>= hold-down ${HOLD_DOWN}s)" num_ge "$gap" "$HOLD_DOWN"
check "hold-down actually delayed the return (engine logged hold-down after restore)" \
    log_event_since "$T_RESTORE" "decision: no change" code=hold-down wanted=isp-a
check "edge best path stays on ISP-A" eq "$(edge_best_nh_lp)" "10.1.0.2 150"
check "traceroute client -> target via ISP-A" eq "$(hop2 client)" "10.1.0.2"

# ---------------------------------------------------------------- summary
printf '\n\033[1m%d passed, %d failed in %ss\033[0m\n' "$PASS" "$FAIL" "$((SECONDS - STARTED))"
[ "$FAIL" -eq 0 ]
