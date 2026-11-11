#!/usr/bin/env bash
# Install Playwright's Chromium without apt (GitHub's Ubuntu images already have the system libraries; on 2026-11-11 the
# apt step failed for 10 minutes with "Temporary failure resolving azure.archive.ubuntu.com"). Retries the download;
# if it still fails, the code falls back to the Google Chrome preinstalled on GitHub's runners.
for i in 1 2 3; do
  python -m playwright install chromium && exit 0
  echo "browser download failed (attempt $i)"; sleep $((i * 20))
done
echo "using the runner's preinstalled Chrome instead"
exit 0
