#!/usr/bin/env bash
# Build images and bring the lab up, then wait for BGP to converge.
source "$(dirname "$0")/common.sh"

command -v containerlab >/dev/null || die "containerlab not found"
command -v docker >/dev/null || die "docker not found"

log "building ${HOST_IMAGE}"
docker build -q -t "$HOST_IMAGE" "$REPO_DIR/docker/host" >/dev/null

log "building ${OPTIMIZER_IMAGE}"
docker build -q -t "$OPTIMIZER_IMAGE" -f "$REPO_DIR/docker/optimizer/Dockerfile" "$REPO_DIR" >/dev/null

log "deploying lab ${LAB_NAME}"
containerlab deploy -t "$TOPO" --reconfigure

log "waiting for BGP sessions"
wait_bgp_established edge 10.1.0.2 10.2.0.2 || die "edge eBGP sessions did not come up"
wait_bgp_established inet 10.3.0.1 10.4.0.1 || die "inet eBGP sessions did not come up"
wait_bgp_established edge 10.9.0.2 || warn "iBGP edge<->optimizer is not Established yet"

log "waiting for Grafana"
deadline=$((SECONDS + 90))
until curl -sf http://localhost:3000/api/health >/dev/null 2>&1; do
    ((SECONDS < deadline)) || { warn "Grafana did not answer on http://localhost:3000"; break; }
    sleep 2
done

log "lab is up"
log "dashboard: http://localhost:3000  (no login; admin/admin to edit)   prometheus: http://localhost:9090"
