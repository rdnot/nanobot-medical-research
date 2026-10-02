#!/bin/sh
# FORK: one-command installer for the rdnot/nanobot-medical-research fork
# (macOS / Linux). Resolves the newest wheel published by the fork-release
# workflow (release tag latest-<branch>, WebUI already bundled) and hands it to
# the regular nanobot installer via NANOBOT_INSTALL_TARGET. Re-run to update.
#
#   curl -fsSL https://raw.githubusercontent.com/rdnot/nanobot-medical-research/main/scripts/install-fork.sh | sh
#   NANOBOT_FORK_BRANCH=scrapling curl -fsSL ... | sh      # browser-tier branch
set -eu

repo="rdnot/nanobot-medical-research"
branch="${NANOBOT_FORK_BRANCH:-main}"
case "$branch" in
  main|scrapling) ;;
  *)
    printf 'Error: NANOBOT_FORK_BRANCH must be "main" or "scrapling" (got "%s").\n' "$branch" >&2
    exit 1
    ;;
esac

if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fsSL -H 'Accept: application/vnd.github+json' "$1"; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -qO- --header='Accept: application/vnd.github+json' "$1"; }
else
  printf 'Error: curl or wget is required.\n' >&2
  exit 1
fi

api="https://api.github.com/repos/$repo/releases/tags/latest-$branch"
release_json="$(fetch "$api" 2>/dev/null || true)"
wheel_url="$(printf '%s' "$release_json" \
  | grep -o '"browser_download_url": *"[^"]*\.whl"' \
  | head -n 1 \
  | sed 's/.*"\(https[^"]*\)"$/\1/')"

if [ -z "$wheel_url" ]; then
  printf 'Error: no published build found for branch "%s" (release tag latest-%s).\n' "$branch" "$branch" >&2
  printf 'The fork publishes one from GitHub Actions on every push. Until it exists, install from source:\n' >&2
  printf '  git clone https://github.com/%s.git && cd nanobot-medical-research && uv sync && uv run nanobot onboard\n' "$repo" >&2
  exit 1
fi

printf 'Installing the nanobot medical-research fork (%s branch)\n' "$branch"
printf 'Wheel: %s\n' "$wheel_url"

NANOBOT_INSTALL_TARGET="nanobot-ai @ $wheel_url"
NANOBOT_INSTALL_SOURCE="the $repo fork ($branch)"
export NANOBOT_INSTALL_TARGET NANOBOT_INSTALL_SOURCE

fetch "https://raw.githubusercontent.com/$repo/$branch/scripts/install.sh" | sh -s -- "$@"
