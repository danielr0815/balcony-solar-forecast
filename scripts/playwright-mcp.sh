#!/usr/bin/env bash
# Keep MCP/browser versions together and the HA login outside tracked files.
set -euo pipefail
umask 077

task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Independent VS Code windows must explicitly choose different instance names.
# Preserve the existing profile for users who already logged in there.
task_instance="${BSF_PLAYWRIGHT_INSTANCE:-default}"
if [[ ! "$task_instance" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo 'BSF_PLAYWRIGHT_INSTANCE: nur Buchstaben, Zahlen, _ und - erlaubt.' >&2
    exit 1
fi
task_profile="$task_root/.ha-dev/playwright-profile"
task_output="$task_root/.playwright-mcp"
if [[ "$task_instance" != default ]]; then
    task_profile="$task_root/.ha-dev/playwright-profile-$task_instance"
    task_output="$task_root/.playwright-mcp/$task_instance"
fi
task_runtime="$task_root/.ha-dev/playwright-runtime"
export PLAYWRIGHT_BROWSERS_PATH="$task_root/.ha-dev/playwright-browsers"
task_cli="$task_runtime/node_modules/@playwright/mcp/cli.js"
task_playwright="$task_runtime/node_modules/playwright/cli.js"

if [[ "${1:-}" == "install" ]]; then
    npm install --prefix "$task_runtime" --save-exact --no-audit --no-fund @playwright/mcp@0.0.82
    node "$task_playwright" install --with-deps chromium
    exit 0
fi

if [[ ! -f "$task_cli" ]]; then
    echo 'Playwright MCP fehlt: bash scripts/playwright-mcp.sh install' >&2
    exit 1
fi
if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    echo 'Kein VM-Desktop verbunden. VS Code in der grafischen VM-Sitzung öffnen.' >&2
    exit 1
fi

cd -- "$task_root"
task_browser="$(node -e 'console.log(require(process.argv[1]).chromium.executablePath())' "$task_runtime/node_modules/playwright")"
task_options=()
# The VM blocks nested user namespaces (Chromium reports "No usable sandbox").
# Keep this workaround confined to the devcontainer, not a native host install.
if [[ -f /.dockerenv ]]; then
    task_options+=(--no-sandbox)
fi
# VS Code mounts the Wayland socket under /tmp, not XDG_RUNTIME_DIR.
# Its forwarded X11 DISPLAY can be present but fail Xauthority checks.
if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
    if [[ -S "/tmp/$WAYLAND_DISPLAY" ]]; then
        export WAYLAND_DISPLAY="/tmp/$WAYLAND_DISPLAY"
    fi
    task_options+=(--config "$task_root/scripts/playwright-wayland.json")
fi
exec node "$task_cli" \
    --executable-path "$task_browser" \
    --user-data-dir "$task_profile" \
    --output-dir "$task_output" \
    --codegen none "${task_options[@]}" "$@"
