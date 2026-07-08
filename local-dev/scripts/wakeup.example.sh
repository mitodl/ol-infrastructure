#!/usr/bin/env bash
# wakeup.example.sh — example sleepwatcher wake hook (macOS only).
#
# Purpose: after the Mac wakes, the Docker VM (OrbStack or Docker Desktop) resumes and a k3d node's kubelet
# exec/streaming server can come back wedged (see heal-exec.sh). This hook runs
# the heal on every wake so `kubectl exec` is reliable without you thinking
# about it. It is a no-op when the cluster is healthy or stopped.
#
# Setup (Homebrew sleepwatcher runs ~/.wakeup on wake):
#
#   brew install sleepwatcher
#   # point ~/.wakeup at this script (edit REPO below to your checkout first),
#   # or symlink it:
#   ln -sf "$HOME/dev/ol-infrastructure/local-dev/scripts/wakeup.example.sh" ~/.wakeup
#   chmod +x ~/.wakeup
#   brew services start sleepwatcher
#
# Notes:
# - sleepwatcher runs hooks with a minimal PATH, so we add the tool locations
#   below. This covers both Docker backends: OrbStack's `docker` CLI lives in
#   ~/.orbstack/bin and Docker Desktop's in ~/.docker/bin (neither is in the
#   Homebrew dirs). Non-existent dirs on PATH are harmless, so listing both is
#   safe regardless of which backend you run.
# - heal-exec.sh targets the `local-dev` kube context; that context must exist
#   in your default kubeconfig (~/.kube/config). If yours lives elsewhere, set
#   KUBECONFIG here.
# - Output is appended to ~/Library/Logs/local-dev-heal.log for debugging.

export PATH="$HOME/.orbstack/bin:$HOME/.docker/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

REPO="$HOME/dev/ol-infrastructure"
LOG="$HOME/Library/Logs/local-dev-heal.log"

mkdir -p "$(dirname "$LOG")"
echo "=== wake $(date '+%Y-%m-%dT%H:%M:%S') ===" >>"$LOG"
"$REPO/local-dev/scripts/heal-exec.sh" >>"$LOG" 2>&1 || true
