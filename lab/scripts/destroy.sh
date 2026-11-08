#!/usr/bin/env bash
# Tear the lab down.
source "$(dirname "$0")/common.sh"

log "destroying lab ${LAB_NAME}"
containerlab destroy -t "$TOPO" --cleanup
