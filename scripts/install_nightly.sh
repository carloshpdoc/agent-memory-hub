#!/usr/bin/env bash
# Installs the nightly maintenance job (scripts/nightly.py) as a launchd agent.
# Run it on ONE machine: the Supabase database is shared by all of them.
#
# Usage:
#   scripts/install_nightly.sh              # every day at 03:30
#   NIGHTLY_HOUR=2 scripts/install_nightly.sh
#   scripts/install_nightly.sh --uninstall
#
# launchd runs with a minimal PATH, so the CLIs used as LLM fallbacks (codex, claude,
# cursor-agent) are resolved now and baked into the plist.
set -euo pipefail

LABEL="com.agent-memory-hub.nightly"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HOUR="${NIGHTLY_HOUR:-3}"
MINUTE="${NIGHTLY_MINUTE:-30}"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removido: $PLIST"
  exit 0
fi

if [[ "$(uname)" != "Darwin" ]]; then
  echo "ERROR: launchd e macOS only. Em Linux, agende: python3 $REPO/scripts/nightly.py (cron)." >&2
  exit 1
fi
case "$REPO" in
  "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*)
    echo "ERROR: $REPO fica numa pasta protegida pelo macOS; o launchd nao consegue ler." >&2
    exit 1 ;;
esac
[[ -f "$REPO/.env" ]] || { echo "ERROR: $REPO/.env nao existe" >&2; exit 1; }

PYTHON="$(command -v python3)"
path_dirs=("/usr/bin" "/bin" "/usr/sbin" "/sbin")
for bin in python3 git codex claude cursor-agent ollama; do
  p="$(command -v "$bin" 2>/dev/null || true)"
  [[ -n "$p" && "$p" == /* ]] && path_dirs+=("$(dirname "$p")")
done
PATH_VALUE="$(printf '%s\n' "${path_dirs[@]}" | awk '!seen[$0]++' | paste -sd: -)"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PYTHON</string><string>$REPO/scripts/nightly.py</string></array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$PATH_VALUE</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
  <key>StandardOutPath</key><string>$REPO/nightly.log</string>
  <key>StandardErrorPath</key><string>$REPO/nightly.log</string>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
PLIST
plutil -lint "$PLIST" >/dev/null
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "instalado: $PLIST"
echo "  horario: $(printf '%02d:%02d' "$HOUR" "$MINUTE") (se o Mac estiver dormindo, roda ao acordar)"
echo "  PATH:    $PATH_VALUE"
echo "  log:     $REPO/nightly.log · status: mem health"
echo "  rodar agora: launchctl kickstart gui/\$(id -u)/$LABEL"
