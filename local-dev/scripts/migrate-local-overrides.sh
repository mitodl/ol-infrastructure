#!/usr/bin/env bash
# migrate-local-overrides.sh — move pre-existing per-developer override files
# into each app's local/ overlay directory, after showing the plan and asking.
#
# DELETE ANYTIME AFTER 2026-11-14, together with the matching check in
# local-dev/tiltlib.star and the call in start.sh. start.sh runs this on every
# start; with nothing to do it prints nothing and exits 0.
#
# Per app under local-dev/apps/:
#   1. Move configmaps/app-env.local.yaml to local/app-env.local.yaml. That is
#      where the env-override ConfigMap used to live; nothing reads it there
#      any more.
#   2. Create local/kustomization.yaml from its .example when the app has a
#      local/app-env.local.yaml but no overlay. The ConfigMap only reaches the
#      cluster as a resource of the overlay.
#
# Never overwrites: if both the old and new env file exist it says so and
# leaves both. Without a terminal on stdin it only prints the plan.
#
# Usage:
#   ./local-dev/scripts/migrate-local-overrides.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
APPS_DIR="${REPO_ROOT}/local-dev/apps"

ok() { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }
rel() { echo "${1#"${REPO_ROOT}"/}"; }

moves=()   # "old<TAB>new"
copies=()  # "example<TAB>overlay"

for old in "${APPS_DIR}"/*/configmaps/app-env.local.yaml; do
	[ -f "$old" ] || continue
	app_dir="$(dirname "$(dirname "$old")")"
	new="${app_dir}/local/app-env.local.yaml"
	if [ -f "$new" ]; then
		warn "$(rel "$old") and $(rel "$new") both exist; only local/ is applied. Delete the configmaps/ one when you are sure."
		continue
	fi
	moves+=("${old}	${new}")
done

# Overlays needed for env files that exist now or will exist after the moves.
env_files=()
for f in "${APPS_DIR}"/*/local/app-env.local.yaml; do
	[ -f "$f" ] && env_files+=("$f")
done
for m in "${moves[@]+"${moves[@]}"}"; do
	[ -n "$m" ] && env_files+=("${m#*	}")
done
for env_file in "${env_files[@]+"${env_files[@]}"}"; do
	[ -n "$env_file" ] || continue
	overlay="$(dirname "$env_file")/kustomization.yaml"
	[ -f "$overlay" ] && continue
	[ -f "${overlay}.example" ] || { warn "$(rel "$env_file") has no overlay and no $(rel "${overlay}.example") to copy; it will not be applied."; continue; }
	copies+=("${overlay}.example	${overlay}")
done

[ ${#moves[@]} -eq 0 ] && [ ${#copies[@]} -eq 0 ] && exit 0

echo "Per-developer override files need to move into each app's local/ overlay:"
for m in "${moves[@]+"${moves[@]}"}"; do
	[ -n "$m" ] && echo "  mv $(rel "${m%%	*}")  →  $(rel "${m#*	}")"
done
for c in "${copies[@]+"${copies[@]}"}"; do
	[ -n "$c" ] && echo "  cp $(rel "${c%%	*}")  →  $(rel "${c#*	}")"
done

if [ ! -t 0 ]; then
	warn "Not a terminal; nothing changed. Run ./local-dev/scripts/migrate-local-overrides.sh to apply."
	exit 0
fi
read -r -p "Apply? [y/N] " answer
if [[ ! "$answer" =~ ^[Yy]$ ]]; then
	warn "Nothing changed. Files left in configmaps/ are not applied; an env file without its overlay fails the Tiltfile."
	exit 0
fi

for m in "${moves[@]+"${moves[@]}"}"; do
	[ -n "$m" ] || continue
	old="${m%%	*}"; new="${m#*	}"
	mkdir -p "$(dirname "$new")"
	mv "$old" "$new"
	rmdir "$(dirname "$old")" 2>/dev/null || true
	ok "moved $(rel "$old")"
done
for c in "${copies[@]+"${copies[@]}"}"; do
	[ -n "$c" ] || continue
	cp "${c%%	*}" "${c#*	}"
	ok "created $(rel "${c#*	}")"
done
