#!/usr/bin/env bash
# Holt neue Commits von GitHub (origin/main) und startet den Dienst nur bei
# TATSAECHLICHEN Aenderungen neu. Wird per systemd-Timer als root aufgerufen;
# die Git-Befehle laufen als Eigentuemer des Repos (sauber, keine Rechteprobleme).
# data/ ist per .gitignore ausgenommen und wird dabei nie angefasst.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
OWNER="$(stat -c '%U' "$REPO")"
run_git() { sudo -u "$OWNER" git -C "$REPO" "$@"; }

before="$(run_git rev-parse HEAD)"
run_git fetch --quiet origin main
run_git reset --hard --quiet origin/main   # robuster als 'pull': keine Merge-Konflikte
after="$(run_git rev-parse HEAD)"

if [ "$before" != "$after" ]; then
    echo "Update $before -> $after, starte Dienst neu"
    systemctl restart aktienanalyse
else
    echo "Keine Aenderungen ($after)"
fi
