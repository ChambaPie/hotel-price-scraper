#!/usr/bin/env bash
# Replace branch $1 with a single commit holding the given files (latest snapshot only, so the repo doesn't grow).
set -euo pipefail
branch="$1"; shift
tmp="$(mktemp -d)"
cp "$@" "$tmp/"
cd "$tmp"
git init -q -b "$branch"
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A
git commit -qm "Snapshot $(date -u +%FT%H:%MZ)"
git push -q --force "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" "$branch:$branch"
echo "published $* to $branch"
