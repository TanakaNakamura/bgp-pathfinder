# Prototype architecture

## Lab

```
                                 10.1.0.0/30                 10.3.0.0/30
                              ┌──────────────► isp-a ◄───────────────┐
                              │  eth2 .1      AS65001        .2 eth1 │
 client ───────────────────► edge                                   inet ───────────────► target
 203.0.113.10   203.0.113.0/24  AS65000                             AS65100  198.51.100.0/24   198.51.100.10
                         eth1 │  eth3 .1      AS65002        .2 eth2 │   eth3            (httpd :80)
                              └──────────────► isp-b ◄───────────────┘
                              │  10.2.0.0/30                 10.4.0.0/30
                         eth4 │ 10.9.0.0/30, iBGP AS65000
                              ▼
                          optimizer 10.9.0.2
                          probe sources: 10.99.1.1 (always via ISP-A)
                                         10.99.2.1 (always via ISP-B)
```

| Node | Image | Role |
|---|---|---|
| edge, isp-a, isp-b, inet | `quay.io/frrouting/frr:10.5.5` | BGP (bgpd + zebra + staticd) |
| client, target | `bgpopt-host:0.1.0` (alpine 3.23.8) | end hosts; target serves HTTP on :80 |
| optimizer | `bgpopt-optimizer:0.1.0` (python 3.12 slim + ExaBGP 4.2.25) | measurement and injection |

### Policy on edge

- **Inbound.** Routes from ISP-A get `local-preference 150`, routes from ISP-B get 100. By default traffic goes via ISP-A.
- **Outbound.** Only our own prefixes are announced: ISP-A gets `203.0.113.0/24` and `10.99.1.0/24`, ISP-B gets `203.0.113.0/24` and `10.99.2.0/24`. Each probe prefix goes only to "its" provider, so probe return traffic is symmetric.
- **From the optimizer** (`RM-OPTIMIZER-IN`) a route is accepted only if all three conditions hold: the prefix is not ours and not longer than /24, the next-hop is 10.1.0.2 or 10.2.0.2, and the route carries the `no-advertise` community. Nothing is announced to the optimizer.
- **Pinning probes to a provider.** Done with kernel policy routing, independent of the BGP best path:
  ```
  ip rule from 10.99.1.0/24 lookup 101   # table 101: default via 10.1.0.2 dev eth2
  ip rule from 10.99.2.0/24 lookup 102   # table 102: default via 10.2.0.2 dev eth3
  ip route 10.99.{1,2}.0/24 via 10.9.0.2 # (staticd) return path to the optimizer
  ```

## Optimizer components

```
                ┌──────────────────────── optimizer container ─────────────────────────┐
                │                                                                       │
 config.yaml ──►│  ExaBGP 4.2 (PID 1) ◄── iBGP 10.9.0.2 ↔ 10.9.0.1 ──► edge (FRR)        │
                │     │  ▲                                                              │
                │ spawn │ stdout: announce/withdraw                                     │
                │     ▼  │      stdin: JSON state events, done/error acks               │
                │  run-optimizer.sh  (fail-safe: on exit → SIGTERM ExaBGP)              │
                │     │                                                                 │
                │     ▼                                                                 │
                │  optimizer.main → app.bootstrap → app.Daemon (asyncio, every 2 s)     │
                │   ├─ probing.Prober      IcmpProbe / TcpProbe ──► target               │
                │   ├─ app.OptimizerService                                              │
                │   │    ├─ domain.DecisionEngine  selector + guards (pure)              │
                │   │    ├─ ModeController → ExaBgpInjector | DryRunInjector             │
                │   │    └─ EventBus ──► SqliteRecorder   (SQLite, WAL)                  │
                │   │                ├─► JsonLogListener  (stderr + optimizer.jsonl)    │
                │   │                └─► PrometheusExporter (:9101/metrics)             │
                │   └─ bgp.ExaBgpMessageParser  stdin → Ack / NeighborState / Shutdown   │
                │                                                                       │
                │  optimizer CLI (status / history / mode / probe) ──► reads SQLite     │
                └───────────────────────────────────────────────────────────────────────┘
```

## Code structure (OOP / SOLID)

```
optimizer/
  domain/          pure decision logic, no I/O, no clock           ← depends on nothing
    models.py        value objects: Sample, ProviderMetrics, Policy, PrefixState, Proposal, Decision
    scoring.py       Scorer (protocol), WeightedScorer, ProviderClassifier, ImprovementCriterion
    selection.py     PathSelector: which provider should carry the prefix
    guards.py        ChangeGuard (protocol), ConfirmationGuard, HoldDownGuard, RateLimitGuard
    engine.py        DecisionEngine: selector proposal → guard chain → Decision + new state
  ports.py         every interface the application depends on (typing.Protocol)
  app/             application layer                                 ← depends on domain + ports
    events.py        Started, RoundMeasured, PrefixEvaluated, RouteChanged, ModeChanged, ... + EventBus
    mode.py          ModeController: current mode → active RouteInjector
    service.py       OptimizerService: one round in, decisions + events out (sync, no direct I/O)
    daemon.py        Daemon: asyncio loop, signals, ExaBGP stdin reader
    bootstrap.py     composition root: the only place that creates concrete classes
  probing/         adapter: Probe (protocol), IcmpProbe, TcpProbe, PingOutputParser, SampleWindow,
                   MetricsCalculator, Prober (implements MetricsSource + RoundRunner)
  bgp/             adapter: ExaBgpCommands, ExaBgpInjector / DryRunInjector, ExaBgpMessageParser
  storage/         adapter: SqliteStore (read ports for the CLI), SqliteRecorder (event listener)
  observability/   adapter: JsonLogListener, PrometheusExporter (event listeners), logjson
  config.py        ConfigParser: one method per config section; load() / parse()
  cli.py           Command objects: StatusCommand, HistoryCommand, ModeCommand, ProbeCommand; Cli
  main.py          daemon entry point (python -m optimizer.main)
```

Dependencies point inwards: adapters → app → domain. `domain` imports nothing but the standard `dataclasses`/`enum`/`typing`, and only `app/bootstrap.py` imports concrete adapters. `tests/unit/test_architecture.py` enforces both rules.

| Principle | Where it shows |
|---|---|
| **S**ingle responsibility | Scoring, usability, significance, selection and each timing rule are separate classes. `OptimizerService` decides and publishes; it does not persist, log or export. The parser, the command formatter and the injector are split. |
| **O**pen/closed | New rule: a `ChangeGuard` passed to `DecisionEngine.from_policy(extra_guards=...)` (tested with a maintenance-window guard). New probe method: a `Probe`. New output: an event listener. New BGP speaker (GoBGP): a `RouteInjector`. None of these modify existing classes. |
| **L**iskov substitution | `ExaBgpInjector` and `DryRunInjector` honour the same contract (return the command used, never raise in normal operation). Dry-run is therefore a swap in `ModeController`, not `if dry_run:` branches. A test runs the same assertions against both. Any object with `.method` and `async measure()` is a `Probe`. |
| **I**nterface segregation | `ports.py` holds small protocols. The service needs `MetricsSource`, `EventPublisher`, `AckSink`. The CLI's status command needs `StatusQuery`; the mode command needs `ModeControl`; the mode controller needs only `ModeRequestSource`. `SqliteStore` implements several of them, but no consumer sees more than it uses. |
| **D**ependency inversion | `OptimizerService`, `ModeController` and `Daemon` receive their collaborators through constructors and depend on protocols. Concrete classes are created only in `app/bootstrap.py`, and tests build the real service there with fakes (`FakeProber`, an in-memory ExaBGP pipe, a temp SQLite file). |

The decision engine is still "pure" in the sense of the original spec: `DecisionEngine.decide()` is deterministic, has no side effects and no I/O, and takes `now` as an argument. Being a class only means its collaborators (scorer, selector, guards) are injected instead of hard-coded.

## Data flow of one round

```
every 2 s
  │
  ├─ service.poll_mode()            ← kv.mode_request from `optimizer mode` (via ModeController)
  │
  ├─ prober.run_round()              in parallel for every (prefix × provider) × Probe:
  │     5× ICMP + 1× TCP  ──►  SampleWindow (last 10 rounds ≈ 60 probes)
  │
  └─ service.process_round()
        publish RoundMeasured                     → SqliteRecorder: measurements
        for each prefix:
          metrics = prober.metrics(prefix)        MetricsCalculator: rtt, loss, jitter, sent
          decision, state' = engine.decide(state, metrics, now, recent_changes)
          if decision.changed:
            mode.injector.announce/withdraw       ExaBgpInjector → stdout → ExaBGP → edge
                                                  DryRunInjector → command text only
            publish RouteChanged                  → history row, override, "route …" /
                                                    "dry-run: would …" log, change counter
          publish PrefixEvaluated                 → provider_status, prefix_state, gauges,
                                                    "decision: no change" log on transitions
        publish RoundCompleted                    → heartbeat, periodic prune
```

## Decision state machine (per prefix)

```
                 wanted == current
             ┌──────────────────────┐
             ▼                      │
        ┌─────────┐  wanted≠current ┌─────────┐ rounds ≥ confirm ┌───────────┐
 ──────►│ STEADY  ├────────────────►│ PENDING ├─────────────────►│ hold-down │─┐ expired
        └─────────┘   (round 1/N)   └────┬────┘                  │ / rate    │ │ and budget
             ▲                           │ target changed        │ limit?    │ │ not used up
             │                           └─► counter back to 1   └───────────┘ │
             │                                                                  ▼
             │                                          ANNOUNCE (target ≠ base) / WITHDRAW (target = base)
             └────────────────────────── changed_at = now ─────────────────────┘

 Touch nothing:  INSUFFICIENT_DATA (current provider has < min_samples probes)
                 ALL_BAD           (no usable provider at all)
```

Choosing the target (`wanted_provider`):
1. Too little data for the current provider → no opinion.
2. No usable providers (all "bad" or without data) → no opinion.
3. An override is active, base is usable, and the override beats base by less than 50 % of the threshold → target = base (WITHDRAW).
4. The best usable alternative by score wins by ≥ 20 ms or ≥ 2 % loss → target = that provider.
5. Otherwise → the current provider (STEADY).

## Lab timing

| Event | How it adds up | In the lab |
|---|---|---|
| Degradation detected | one round shifts the window average by +30 ms | ~2 s |
| Switch | + `confirm_rounds` (3 × 2 s) | 6-8 s after `degrade.sh` |
| Return after `restore.sh` | push the bad probes out of the window (~20 s) + 3 rounds, but no earlier than `hold_down_s` (60 s) after the switch | 60-65 s after the announce |
| Fail-safe | CEASE / TCP close → edge withdraws the route; backstop: hold-time 9 s | 0.2-0.3 s |

## Fail-safe paths

| What died | What happens | Result on edge |
|---|---|---|
| The optimizer Python process (exception, `kill -9`, OOM) | `run-optimizer.sh` → `SIGTERM` to ExaBGP → CEASE → the container exits | iBGP down, route withdrawn in <0.5 s |
| ExaBGP | the kernel closes the TCP socket; the helper gets EOF on stdin and exits | iBGP down immediately |
| The whole container (`docker kill`) | processes killed, socket closed, veth removed | iBGP down in ~0.2 s; backstop: hold-time 9 s |
| The optimizer↔edge link goes silent | keepalives stop arriving | hold-time 9 s |

In every case edge falls back to the plain BGP policy (ISP-A, local-pref 150). To bring the optimizer back: `make optimizer-start`.

## Observability (Prometheus + Grafana)

The dashboard is meant for people who do not know BGP, so it shows two independent sources side by side: what the optimizer **decided** and what client traffic **really does**.

```
 optimizer container                      client container
 ┌───────────────────────────┐            ┌──────────────────────────────────────┐
 │ optimizer.metrics          │            │ path-exporter.sh (main process)      │
 │ prometheus_client :9101    │            │  every 1 s: ping target -> RTT       │
 │ /metrics                   │            │             traceroute -m 2 -> hop 2 │
 └─────────────┬──────────────┘            │  busybox httpd :9100/metrics.txt     │
               │                           └──────────────────┬───────────────────┘
               │      containerlab management network (Docker DNS)
               ▼                                              ▼
        ┌──────────────────────────────────────────────────────────┐
        │ prometheus  (scrape every 2 s, 1 day retention)  :9090   │
        └───────────────────────────┬──────────────────────────────┘
                                    ▼
        ┌──────────────────────────────────────────────────────────┐
        │ grafana  provisioned datasource + "BGP Optimizer" home   │
        │ dashboard, anonymous viewer                       :3000  │
        └──────────────────────────────────────────────────────────┘
```

Both observability nodes are attached only to the management network; they have no data-plane links and cannot affect routing.

| Metric | Source | Meaning |
|---|---|---|
| `optimizer_rtt_ms`, `optimizer_loss_percent`, `optimizer_jitter_ms`, `optimizer_score` `{prefix,provider}` | optimizer | the sliding-window metrics the engine decides on |
| `optimizer_traffic_provider_index{prefix}` | optimizer | provider carrying the prefix as decided (1-based config order: 1 = isp-a, 2 = isp-b) |
| `optimizer_override_active{prefix}` | optimizer | 1 while an injected route is active |
| `optimizer_engine_state{prefix}` | optimizer | 0 steady, 1 pending, 2 hold-down, 3 rate-limit, 4 insufficient-data, 5 all-bad, 6 changing |
| `optimizer_route_changes_total{prefix,action,mode}` | optimizer | announce/withdraw counters (live and dry-run); drive the dashboard's change markers |
| `optimizer_bgp_session_up`, `optimizer_dry_run`, `optimizer_info` | optimizer | session state, mode, version |
| `lab_client_path_provider` | client | the provider the client's packets really pass through (1 ISP-A, 2 ISP-B, 0 unknown) |
| `lab_client_rtt_ms`, `lab_client_ping_ok` | client | what a user of the network feels |

When the optimizer is down, its series go stale. The dashboard queries add `or on() vector(-1)` (or `0`), so the tiles show "optimizer down" instead of an empty "No data".

## SQLite schema

| Table | Contents |
|---|---|
| `measurements` | raw round results: ts, prefix, provider, method, sent, received, rtt avg/min/max, loss (kept for 24 h) |
| `decisions` | every change: ts, prefix, action, from→to, next_hop, code, reason, metrics (JSON), command, dry_run |
| `events` | daemon start/stop, mode changes, BGP session states |
| `provider_status` | latest window metrics and score per (prefix, provider) |
| `prefix_state` | engine state: active, pending, rounds, changed_at, last code/reason |
| `overrides` | what is injected right now (or "injected" in dry-run) |
| `kv` | mode, mode_request, bgp_state, heartbeat |
