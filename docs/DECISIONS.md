# Design decisions and deviations from the spec

This file lists the decisions that differ from the original spec or refine it, with the reason for each. All versions and formats were checked against the installed software: ExaBGP 4.2.25, FRR 10.5.5, containerlab 0.83.0, Docker 27.5.1.

## Deviations from the spec

### D1. The FRR image comes from `quay.io/frrouting/frr:10.5.5`, not `frrouting/frr`
On Docker Hub the newest `frrouting/frr` tag is `v8.4.1` (from 2022). The FRR project publishes 10.x images only on quay.io. `10.5.5` was chosen as the latest patch of the mature 10.5 branch (August 2026); the newer 10.7 has only two patch releases so far.

### D2. Every FRR node gets a `vtysh.conf`
The spec lists only `frr.conf` and `daemons` per node. Without `/etc/frr/vtysh.conf`, FRR 10.5.5 logs `Configuration file[/etc/frr/frr.conf] processing failure: 11` at boot and **silently drops part of the config**. On edge, `set local-preference 150`, `bgp community-list` and `match community` were lost, which broke the base policy. A one-line file, `service integrated-vtysh-config`, fixes it. With it, `vtysh -C` passes cleanly and the running config matches frr.conf.

### D3. Fail-safe on optimizer death: the `run-optimizer.sh` wrapper
The spec assumes that "if the optimizer or ExaBGP dies, the iBGP session drops". With ExaBGP 4.2.25 **that is not true**. This was checked in the source, `exabgp/reactor/api/processes.py` (`_handle_problem`), and on a live instance:
- with `respawn` (the default), ExaBGP restarts the helper and the routes stay in the Adj-RIB-Out;
- without `respawn`, ExaBGP logs `process ended` and keeps holding the session and the announcements;
- `exabgp.api.terminate` fires only on a pipe write error (EPIPE), not when the process dies.

So ExaBGP does not start Python directly; it starts a wrapper. When the optimizer exits for any reason (exception, `kill -9`, OOM, bad config), the wrapper sends `SIGTERM` to its parent, ExaBGP (PID 1 of the container). ExaBGP sends a CEASE, edge withdraws the routes, and the container exits. Respawn is disabled (`exabgp_api_respawn=false`). Verified in e2e: after `kill -9` of the Python process, edge is back on ISP-A in ~0.3 s.

### D4. Container names are `clab-bgpopt-<node>`
The spec says `docker kill optimizer`. By default containerlab prefixes names with `clab-<lab>-`. The prefix is kept so the lab cannot clash with other `client`, `edge` or `target` containers on the host. The spec's command becomes `docker kill clab-bgpopt-optimizer`.

### D5. Restarting the optimizer after `docker kill`: `make optimizer-start`
`docker kill` destroys the network namespace, and with it the `optimizer:eth1 ↔ edge:eth4` veth. `containerlab start -n optimizer` (0.83) restores links only for nodes stopped with `containerlab stop`. The `lab/scripts/optimizer-start.sh` script runs `docker start` and then `containerlab tools veth create`. The optimizer entrypoint waits for `eth1` to appear, and FRR re-applies the address on `edge:eth4` by itself.

The optimizer node has `restart-policy: "no"`. containerlab defaults to `always`, so after a fail-safe exit Docker would bring the container back **without** its link to edge, leaving it half-alive. "Once it is down, it stays down until an operator brings it back" is more predictable.

### D6. The base provider is set in the config (`base_provider`)
The spec says to "withdraw when the best path matches the base BGP path". To know the base best path dynamically, the optimizer would have to see edge's table. But edge does not send iBGP-learned routes to an iBGP neighbor, and while an override is active the best path is the injected route itself. For the MVP the base path is set statically (`base_provider: isp-a`, matching local-pref 150 on edge). The next step is BMP from edge or add-path, so the optimizer sees every path and derives the base path itself.

### D7. Switching between dry-run and live at runtime
`dry_run` in config.yaml sets the mode at startup. So that e2e can test both modes without recreating the container (which breaks the links, see D5), there is an `optimizer mode dry-run|live` command. It writes a request to SQLite (`kv.mode_request`), and the daemon picks it up at the start of the next round. Switching to dry-run first withdraws every real announcement. Any mode change resets the engine state: it starts from plain BGP with empty hysteresis counters. After a daemon restart, the value from config.yaml applies again.

### D8. OS packages are not pinned individually
Images are pinned by tag (`alpine:3.23.8`, `python:3.12.16-slim-bookworm`, `quay.io/frrouting/frr:10.5.5`), Python packages by version (`exabgp==4.2.25`, `PyYAML==6.0.2`, `setuptools==75.8.0`, `pytest==8.3.4`, `coverage==7.6.10`). apk/apt packages are installed from the pinned distro release without per-package versions: mirrors drop old versions after security updates, so the build would break over time.

### D9. A dedicated image for client/target (`bgpopt-host:0.1.0`)
The tools (ping, traceroute, curl, iperf3, busybox httpd) are baked into the image instead of being installed with `apk add` at deploy time. The lab comes up without internet access on the nodes, and deterministically.

## Implementation details

### D10. Probe routing on the optimizer side
On the optimizer: `ip rule from 10.99.0.0/16 lookup 100`, with `default via 10.9.0.1` in table 100. Everything sent from a probe address goes to edge, whatever the destination, while the mgmt network's default route stays untouched. Probe addresses are `/32` on `eth1`. Client and target also keep the mgmt default route; routes to the lab prefixes are added explicitly.

### D11. Edge protects itself from bad injections (defence in depth)
Edge accepts a route from the optimizer only if all three conditions hold:
- the prefix is not our own address space, not a default route, and not longer than `/24`;
- the next-hop is one of the ISP routers (`10.1.0.2` or `10.2.0.2`);
- the route carries the `no-advertise` community.

Optimizer routes cannot leave edge, for two independent reasons: the `no-advertise`/`no-export` communities and the strict outbound prefix-lists. Edge announces nothing to the optimizer (`route-map RM-OPTIMIZER-OUT deny`).

### D12. Short iBGP timers (3/9 s) and `passive` on edge
If the TCP session dies "dirty" (FIN/RST never arrives), the routes disappear within 9 s at most. In practice, on `docker kill` the kernel closes the socket and edge sees the session drop in ~0.2 s. `passive` on edge means ExaBGP always initiates the connection, so there are no open collisions.

### D13. `rp_filter=0` on edge
Probe replies via ISP-A arrive on `eth2` even when the best path to their source goes via ISP-B. Strict rp_filter would drop those packets.

### D14. netem via `tc` inside the FRR containers
`degrade.sh` puts netem on the egress of both ends of the link (`edge:ethX` and `isp:eth1`), so the delay and loss apply in both directions. `degrade.sh a 150 5` adds ≈ 300 ms of RTT. The FRR image already ships `tc`.

### D15. Metrics and score
- The window is the last `window_rounds` rounds. Each round produces 5 ICMP echoes and one TCP connect for every (prefix, provider) pair.
- Loss and RTT are computed over all probes (ICMP+TCP). Jitter is the mean absolute difference between consecutive RTTs. It is computed **per method** (otherwise the ICMP/TCP difference would show up as fake jitter) and then combined as a weighted average.
- A TCP RST (connection refused) counts as a successful probe: the path works.
- `score = rtt + jitter_weight·jitter + loss_weight_ms·loss%` (25 ms per 1 % of loss by default).

### D16. Decision rules
- Switching: the alternative has a better score **and** wins by ≥ `rtt_threshold_ms` (20 ms) of RTT or ≥ `loss_threshold_pct` (2 %) of loss, for `confirm_rounds` rounds in a row.
- Returning to the base path uses a **narrower band** (`return_ratio` = 0.5): we go back as soon as the override's advantage falls below 10 ms / 1 %. The two-threshold hysteresis plus N rounds prevents oscillation around the threshold. On a tie the base path, i.e. the plain BGP policy, always wins.
- The hold-down applies after **any** change, including a return.
- The change limit is global across all prefixes (it protects the network from churn).
- A "bad" provider has loss ≥ 30 % or RTT ≥ 1000 ms. The engine switches only to a provider that is not bad and has enough probes (`min_samples`). If every provider is bad or there is too little data, nothing changes, and an active override is not removed either.

### D17. A daemon crash fails closed
Any unhandled exception ends the process, and then D3 kicks in: the network goes back to plain BGP. At startup the daemon clears the active overrides in SQLite: ExaBGP starts with an empty Adj-RIB-Out, and the table should reflect that.

### D18. stdout is reserved for ExaBGP commands
The daemon replaces `sys.stdout` with stderr, and only the injector writes to the pipe. Logs go to stderr as JSON. ExaBGP starts the helper without capturing stderr (checked in `processes.py`: `Popen(..., stdin=PIPE, stdout=PIPE)`), so the logs end up in `docker logs`. A copy is written to `/var/log/optimizer/optimizer.jsonl`.

### D19. No `jq` on the host
JSON from `vtysh ... json` is parsed by the small `lab/scripts/bgpjson.py`, which needs only the system python3.

## Dashboard (added after the original spec)

### D20. Prometheus + Grafana, not Grafana on top of SQLite
The dashboard was added so the prototype can be shown to people who do not read BGP tables. The optimizer exports metrics with `prometheus_client` (`metrics` section in config.yaml, port 9101), Prometheus scrapes them every 2 s, and Grafana reads Prometheus.

A SQLite datasource plugin was rejected. It would need a third-party plugin, a shared volume, and concurrent readers of a WAL database across containers. Prometheus is the standard path and is also the planned next step in REPORT.md. SQLite stays the source of truth for `optimizer status/history`.

### D21. "Ground truth" from the client, not only the optimizer's opinion
The client runs `path-exporter.sh` as its main process: every second it pings the target and runs `traceroute -m 2`. Hop 2 tells which ISP router edge actually forwarded the packet to. The dashboard shows this next to the optimizer's decision, so the viewer sees that traffic really moved. It also makes the inbound-path limitation visible: user RTT drops from 300 to 150 ms, not to 0.

The exporter is served by busybox httpd as `metrics.txt` (`text/plain` without a version). Prometheus 3 rejects such responses unless `fallback_scrape_protocol: PrometheusText0.0.4` is set, so the client job sets it.

### D22. Demo-friendly Grafana defaults
Anonymous Viewer access and `GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH`: opening http://localhost:3000 shows the dashboard immediately, with no login. The admin account is `admin`/`admin`. Telemetry and update checks are off. Grafana and Prometheus are containerlab nodes on the management network only (no data-plane links), published on host ports 3000 and 9090. Images are pinned: `grafana/grafana:13.2.4`, `prom/prometheus:v3.15.1`, `prometheus-client==0.26.0`; all were at least two weeks old when chosen.

### D23. State timelines use the "fixed" colour mode
With `color.mode: thresholds` and `mergeValues`, Grafana's state-timeline panel buckets numeric values into threshold ranges (it shows "-∞+") and ignores value mappings. The panels use `fixed` colour mode, and colours come from the value mappings. This was found from a headless-browser screenshot of the dashboard, not from the API, which returned correct data.

### D24. The optimizer was rewritten as layered OOP code following SOLID
The first version mixed styles. The decision engine was a set of functions, and one `Daemon` class decided, persisted, logged, exported metrics and branched on `if dry_run`. The rewrite kept every behavior and changed only the structure (see ARCHITECTURE.md "Code structure"):
- the decision logic is split into small classes (scorer, classifier, improvement criterion, path selector, change guards) composed by `DecisionEngine`;
- dry-run is a substitutable `RouteInjector`, not a branch;
- persistence, logs and metrics are event listeners;
- every dependency is a `Protocol` in `ports.py`;
- concrete classes are created only in `app/bootstrap.py`.

Kept unchanged on purpose:
- the engine's behavior: the same decision test scenarios pass unmodified apart from how the engine is called;
- the CLI output, the JSON log event names and fields, and the Prometheus metric names, because e2e, operators and the Grafana dashboard rely on them;
- the entry points `python -m optimizer.main` and the `optimizer` console script.

The spec asked for the decision engine as "pure functions". `DecisionEngine.decide()` is still pure: deterministic, no side effects, no I/O, `now` passed in. Making it a class only lets its collaborators be injected and extended (for example a custom guard), which a unit test demonstrates.
