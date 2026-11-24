# Sourced by workflows: wrap <command...> runs it with a screen for a browser with a window. Linux runners have none
# (a virtual one via xvfb); macOS and Windows runners have a real desktop.
wrap() {
  if [ "${RUNNER_OS:-Linux}" = "Linux" ]; then
    command -v xvfb-run >/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq xvfb; }
    xvfb-run -a --server-args="-screen 0 1366x768x24" "$@"
  else
    "$@"
  fi
}
