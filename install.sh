#!/bin/bash
# Install ai-subs on this Mac: a launch agent keeps the server running at http://localhost:8787,
# and the SwiftBar menu is linked in when SwiftBar is installed.
#
#   ./install.sh              install or update (safe to re-run)
#   ./install.sh --uninstall  stop the server and remove the launch agent and links
#
# Nothing is downloaded. Your tokens stay where Claude Code and Codex keep them.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
LABEL="local.ai-subs"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
STATE="$HOME/.local/state/ai-subs"
PORT=8787
DOMAIN="gui/$(id -u)"

[[ "$(uname)" == "Darwin" ]] || { echo "ai-subs runs on macOS only."; exit 1; }
PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || { echo "python3 not found. Run: xcode-select --install"; exit 1; }

swiftbar_dir() {
  local d
  d="$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || true)"
  [[ -n "$d" && -d "$d" ]] && echo "$d"
}

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST" "$HOME/.local/bin/ai-subs-menu-action"
  if d="$(swiftbar_dir)"; then rm -f "$d/ai-subs.2m.py"; fi
  echo "Removed. Your settings and cache in ~/.ai-subs and $STATE are left in place."
  exit 0
fi

mkdir -p "$STATE" "$HOME/Library/LaunchAgents" "$HOME/.local/bin"

# The server runs `claude` and `codex` to refresh expired tokens, so it gets this shell's PATH.
"$PY" - "$PLIST" "$LABEL" "$PY" "$REPO" "$PORT" "$PATH" "$HOME" "$STATE/server.log" <<'EOF'
import plistlib, sys
plist, label, py, repo, port, path, home, log = sys.argv[1:]
with open(plist, "wb") as f:
    plistlib.dump({
        "Label": label,
        "ProgramArguments": [py, "server.py", "--port", port],
        "WorkingDirectory": repo,
        "EnvironmentVariables": {"PATH": path, "HOME": home},
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 3,
        "StandardOutPath": log, "StandardErrorPath": log,
    }, f)
EOF

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
chmod +x "$REPO/menubar/ai-subs-menu-action" "$REPO/menubar/ai-subs.2m.py" "$REPO/sessions.py"
ln -sfn "$REPO/menubar/ai-subs-menu-action" "$HOME/.local/bin/ai-subs-menu-action"

if d="$(swiftbar_dir)"; then
  ln -sfn "$REPO/menubar/ai-subs.2m.py" "$d/ai-subs.2m.py"
  menu="SwiftBar menu linked into $d"
else
  menu="SwiftBar not found, so no menu bar item (optional: https://swiftbar.app, then re-run this)"
fi

for _ in $(seq 1 20); do
  curl -s --max-time 2 "http://127.0.0.1:$PORT/" >/dev/null && break
  sleep 0.5
done
if curl -s --max-time 2 "http://127.0.0.1:$PORT/" >/dev/null; then
  echo "ai-subs is running: http://localhost:$PORT"
else
  echo "The server did not answer on port $PORT. Its log: $STATE/server.log"
  exit 1
fi
echo "$menu"
