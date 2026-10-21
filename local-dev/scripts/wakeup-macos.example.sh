#!/usr/bin/env bash
# wakeup-macos.example.sh — example sleepwatcher wake hook (macOS only).
#
# Purpose: after the Mac wakes, the Docker VM (OrbStack or Docker Desktop) resumes and a k3d node's kubelet
# exec/streaming server can come back wedged (see heal-exec.sh). This hook runs
# the heal on every wake so `kubectl exec` is reliable without you thinking
# about it. It is a no-op when the cluster is healthy or stopped.
#
# It deliberately does NOT run heal-dns.sh. That heal compares each container's
# resolvers against this host's and probes them from here, which only means
# anything when the daemon shares our network namespace — so it self-skips
# whenever Docker runs in a VM, i.e. on every platform that runs this hook. On
# Linux, where it does apply, it is driven by a user systemd timer instead; see
# the local-dev README.
#
# Setup (Homebrew sleepwatcher runs ~/.wakeup on wake):
#
#   brew install sleepwatcher
#   ln -sf "$PWD/local-dev/scripts/wakeup-macos.example.sh" ~/.wakeup
#   chmod +x ~/.wakeup
#   brew services start sleepwatcher
#
# No editing required: the repo root is derived from this script's own path
# below, resolved through the ~/.wakeup symlink.
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

set -euo pipefail

export PATH="$HOME/.orbstack/bin:$HOME/.docker/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# Resolve this script's real location so the checkout path needs no editing —
# otherwise installing means modifying a tracked file, which leaves the working
# tree permanently dirty. Walk the symlink chain by hand rather than using
# `readlink -f`, which BSD readlink on macOS does not support.
SRC="${BASH_SOURCE[0]}"
while [[ -L "$SRC" ]]; do
	DIR="$(cd -P "$(dirname "$SRC")" && pwd)"
	SRC="$(readlink "$SRC")"
	[[ "$SRC" != /* ]] && SRC="$DIR/$SRC"
done
REPO="$(cd -P "$(dirname "$SRC")/../.." && pwd)"
LOG="$HOME/Library/Logs/local-dev-heal.log"

mkdir -p "$(dirname "$LOG")"
echo "=== wake $(date '+%Y-%m-%dT%H:%M:%S') ===" >>"$LOG"
"$REPO/local-dev/scripts/heal-exec.sh" >>"$LOG" 2>&1 || true
