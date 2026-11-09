#!/usr/bin/env bash
# Copy out/ to the "reports" branch (reports/<name>/latest/) so every run's reports and logs can be read
# through the GitHub API, not only in the web UI. Never fails the workflow.
set -uo pipefail
name="${1:?usage: publish_reports.sh <name>}"
[ -d out ] || { echo "nothing in out/"; exit 0; }
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
tmp="$(mktemp -d)"
if git fetch -q origin reports 2>/dev/null; then
  git worktree add -q --detach "$tmp" FETCH_HEAD
else
  git worktree add -q --detach "$tmp"
  (cd "$tmp" && git checkout -q --orphan "reports-new-$$" && git rm -rqf . >/dev/null 2>&1 || true)
fi
dest="$tmp/reports/$name/latest"
rm -rf "$dest" && mkdir -p "$dest"
cp -r out/. "$dest/"
printf 'run_id=%s\nsha=%s\nfinished=%s\n' "${GITHUB_RUN_ID:-local}" "${GITHUB_SHA:-}" "$(date -u +%FT%TZ)" > "$dest/RUN.txt"
cd "$tmp"
git add -A
git commit -qm "$name report run ${GITHUB_RUN_ID:-local}" || { echo "no report changes"; exit 0; }
for i in 1 2 3; do
  git push -q origin HEAD:reports && { echo "published reports/$name/latest"; exit 0; }
  git fetch -q origin reports && git rebase -q FETCH_HEAD || true
  sleep $((i * 3))
done
echo "could not publish reports" >&2
exit 0
