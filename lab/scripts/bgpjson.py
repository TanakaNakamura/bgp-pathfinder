#!/usr/bin/env python3
"""Tiny helper to parse FRR vtysh JSON output in shell scripts (no jq needed).

Usage (JSON on stdin):
  bgpjson.py established PEER...      exit 0 if every PEER is Established
  bgpjson.py best                     print "<nexthop> <locpref> <peer> <aspath> best" of the best path
  bgpjson.py best-community           print the community string of the best path ("-" if none)
  bgpjson.py paths                    print every path, best first
  bgpjson.py prefixes                 print the prefixes of a "... routes json" / "advertised-routes json" dump
"""

import json
import sys


def _load() -> dict:
    raw = sys.stdin.read().strip()
    return json.loads(raw) if raw else {}


def _peers(summary: dict) -> dict:
    # "show bgp ipv4 unicast summary json" -> {"peers": {...}}
    # "show bgp summary json"              -> {"ipv4Unicast": {"peers": {...}}}
    if "peers" in summary:
        return summary["peers"]
    return summary.get("ipv4Unicast", {}).get("peers", {})


def established(peers: list[str]) -> int:
    known = _peers(_load())
    ok = True
    for peer in peers:
        state = known.get(peer, {}).get("state", "absent")
        print(f"{peer} {state}")
        ok = ok and state == "Established"
    return 0 if ok else 1


def _path_line(path: dict) -> str:
    nexthops = path.get("nexthops") or [{}]
    nexthop = nexthops[0].get("ip", "?")
    locpref = path.get("locPrf", path.get("localpref", 100))
    peer = path.get("peer", {}).get("peerId", "?")
    aspath = path.get("aspath", {}).get("string", "") or "-"
    best = "best" if path.get("bestpath", {}).get("overall") else "-"
    return f"{nexthop} {locpref} {peer} {aspath.replace(' ', '_')} {best}"


def best() -> int:
    for path in _load().get("paths", []):
        if path.get("bestpath", {}).get("overall"):
            print(_path_line(path))
            return 0
    return 1


def paths() -> int:
    items = _load().get("paths", [])
    items.sort(key=lambda p: not p.get("bestpath", {}).get("overall"))
    for path in items:
        print(_path_line(path))
    return 0 if items else 1


def best_community() -> int:
    for path in _load().get("paths", []):
        if path.get("bestpath", {}).get("overall"):
            print((path.get("community") or {}).get("string") or "-")
            return 0
    return 1


def prefixes() -> int:
    data = _load()
    routes = data.get("routes", data.get("advertisedRoutes", data.get("receivedRoutes", {})))
    print(" ".join(sorted(routes)))
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "established":
        return established(args)
    if cmd == "best":
        return best()
    if cmd == "paths":
        return paths()
    if cmd == "best-community":
        return best_community()
    if cmd == "prefixes":
        return prefixes()
    print(f"unknown command {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
