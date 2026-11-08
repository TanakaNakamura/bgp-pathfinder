# Prototype report

## What works

| Stage | Spec criterion | Result |
|---|---|---|
| 1. Lab | `make lab-up` brings everything up; all BGP sessions Established; best path via ISP-A; traceroute via 10.1.0.2; outbound filters | ✅ 9 sessions Established; best `10.1.0.2`, local-pref 150; ISP-A sees only `10.99.1.0/24` and `203.0.113.0/24` from edge, ISP-B only `10.99.2.0/24` and `203.0.113.0/24` |
| 2. Probes | ping/traceroute from 10.99.1.1 goes via ISP-A, from 10.99.2.1 via ISP-B; `degrade.sh a 150 5` changes only A's metrics | ✅ hop 2 = `10.1.0.2` / `10.2.0.2`; ISP-A RTT 1 → ~300 ms, loss 0 → ~5-10 %; ISP-B unchanged |
| 3. Decision engine | unit tests: degradation, noise, hysteresis, hold-down, limit, "both bad", return | ✅ 28 engine tests (91 in total), `decision.py` at 100 % line and branch coverage |
| 4. Injection | iBGP Established; dry-run only logs; live: within ~1 min best path via 10.2.0.2 with local-pref 300, traceroute via ISP-B, nothing announced outside | ✅ switch **6-9 s** after `degrade.sh`; community `no-export no-advertise`; "Not advertised to any peer"; ISP-A/B do not receive the route from edge |
| 5. Stability and fail-safe | return no earlier than the hold-down, exactly one return; `docker kill` → route gone | ✅ return ~61-63 s after the announce (hold-down 60 s), exactly 1 withdraw, no flapping; after `docker kill` edge is on ISP-A in **0.2 s**, after `kill -9` of the Python process in **0.3 s** |
| 6. e2e | `tests/e2e/e2e.sh` runs stages 1, 2, 4, 5 with asserts and exits 0 | ✅ `make lab-down && make lab-up && make e2e` from scratch: **52 passed, 0 failed, exit 0** in 206 s (45 routing checks + 7 dashboard checks); a second `make e2e` on the same lab also passes |

Numbers from the final from-scratch e2e run:

| Metric | Value |
|---|---|
| ISP-A before and after `degrade.sh a 150 5` | RTT 0.75 → 301.6 ms, loss 0 → 6.7 % (ISP-B: 0.3 ms, 0 %) |
| dry-run: decision recorded after the degradation | 5.1 s; nothing appeared on edge |
| live: edge on 10.2.0.2 / local-pref 300 after the degradation | 8 s |
| `docker kill` → edge back on ISP-A | < 1 s |
| `kill -9` of the Python process → edge back on ISP-A | < 1 s |
| restarted optimizer moved traffic away again | 10 s |
| return after `restore.sh a` | 60.4 s after the announce (hold-down 60 s), exactly 1 withdraw, 30 s with no flapping |

One problem surfaced on the first from-scratch run: 44/45, the dry-run check failed. The cause was in the test, not in the optimizer. Dry-run is on from the preflight step, so the daemon made its decision already during stage 2, before the test took its "count before" snapshot. The checks are now tied to the time of the degradation, and every run since has been green.

## Added after the spec: a dashboard

`make lab-up` also starts Prometheus and Grafana. http://localhost:3000 opens a dashboard without a login. It shows which provider carries the traffic (measured on the client and decided by the optimizer), the RTT and loss through each provider, what a user feels, the optimizer's state, and markers at every route change. e2e checks that the dashboard data follows reality (ISP-B after the switch, "optimizer DOWN" after the kill). Screenshots: [img/](img/). Details: ARCHITECTURE.md "Observability", DECISIONS.md D20-D23.

## What changed compared to the spec, and why

Full reasoning is in [DECISIONS.md](DECISIONS.md). The main points:

1. **FRR image: `quay.io/frrouting/frr:10.5.5`.** On Docker Hub, `frrouting/frr` stops at 8.4.1.
2. **A `vtysh.conf` in every FRR node.** Without it, FRR 10.5 silently lost part of `frr.conf` at boot, including `set local-preference 150`. The base policy would have been broken even though the sessions looked fine.
3. **Fail-safe via the `run-optimizer.sh` wrapper.** The spec's premise, "the optimizer dies → the session drops", is wrong for ExaBGP 4.2: ExaBGP either restarts the helper or keeps holding the routes. The wrapper stops ExaBGP when the optimizer exits, so the session really drops.
4. **`docker kill clab-bgpopt-optimizer` instead of `docker kill optimizer`.** This is containerlab's name prefix; it prevents name clashes on the host. To bring the node back, use `make optimizer-start`, because the kill also removes the veth to edge. Auto-restart is disabled for the optimizer, otherwise it would come back without a link.
5. **The base provider is set in the config** (`base_provider`) instead of being derived from edge's table. Enough for the MVP; the next step is BMP or add-path.
6. **Runtime dry-run/live switching** (`optimizer mode`). Needed so a single e2e run can check both modes without recreating the container.
7. **Two-threshold hysteresis.** On top of N rounds in a row there is a narrower return threshold (`return_ratio`). The return to the base path happens as soon as the override stops delivering at least half of the threshold. This prevents oscillation at the boundary.

## Issues found along the way (worth remembering in production)

- FRR: a missing `vtysh.conf` gives a partially applied config with no visible error in the session status. The running config needs to be checked against a reference.
- ExaBGP: the death of the helper process does not mean the routes are withdrawn. Without an external supervisor, stale injections live forever.
- containerlab: the default `restart: always`, combined with the veth being destroyed when a container stops, gives a node that "runs" but is cut off from the network.
- Inbound traffic is not controlled. In the lab the traceroute shows it: traffic goes out via ISP-B and comes back via ISP-A. The real RTT gain was 300 → 150 ms, not 300 → 0.

## Ideas for moving to Go + GoBGP

1. **GoBGP as an embedded library** (`github.com/osrg/gobgp/v3/pkg/server`) instead of an external ExaBGP with a text protocol over stdin/stdout:
   - typed `AddPath`/`DeletePath` that return errors, instead of parsing `done`/`error`;
   - `WatchEvent` for peer state and best path changes;
   - a single process: when the optimizer dies, the BGP session dies with it. The supervisor wrapper and the class of bugs behind it (D3) disappear on their own.
2. **Visibility of all paths.** Edge sends all paths to the optimizer via add-path (GoBGP can receive it), or the optimizer runs a BMP collector. The base best path is then computed rather than configured. It also becomes possible to check automatically that the injection really became the best path on edge.
3. **Probes without subprocesses.** Use `golang.org/x/net/icmp` (raw or ICMP datagram sockets bound to the source), `net.Dialer{LocalAddr}` for TCP, and hardware or kernel timestamps (`SO_TIMESTAMPING`) for accurate RTT. One goroutine per (prefix, provider) pair. This scales to thousands of prefixes instead of one `ping` per pair.
4. **Port the decision engine 1:1 as a pure package.** Turn the current unit tests into Go table-driven tests: they are effectively the behavior spec (hysteresis, hold-down, limits, "touch nothing").
5. **Storage and observability.** Export metrics to Prometheus or Timescale, decision events to Postgres. A gRPC or REST API used by both the CLI and the future Web UI.
6. **Scale.** Several edge routers (one GoBGP peer each), IPv6, GoBGP export policies that guarantee `no-advertise` is attached. Deployment: a single static binary in a distroless image.
