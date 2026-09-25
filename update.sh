#!/usr/bin/env bash
# Holt neue Commits von GitHub (origin/main) und startet den Dienst nur bei
# TATSAECHLICHEN Aenderungen am Python-Code neu. Wird per systemd-Timer als root
# aufgerufen; Git und Tests laufen als Eigentuemer des Repos (keine Rechteprobleme).
# data/ ist per .gitignore ausgenommen und wird dabei nie angefasst.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
OWNER="$(stat -c '%U' "$REPO")"
run_git() { sudo -u "$OWNER" git -C "$REPO" "$@"; }

before="$(run_git rev-parse HEAD)"
run_git fetch --quiet origin main
run_git reset --hard --quiet origin/main   # robuster als 'pull': keine Merge-Konflikte
after="$(run_git rev-parse HEAD)"

if [ "$before" = "$after" ]; then
    echo "Keine Aenderungen ($after)"
    exit 0
fi

# index.html wird bei jeder Anfrage frisch gelesen — dafuer braucht es keinen Neustart.
if run_git diff --quiet "$before" "$after" -- '*.py'; then
    echo "Update $before -> $after (ohne Python-Aenderung), kein Neustart noetig"
    exit 0
fi

# Erst pruefen, dann neu starten: ein Syntaxfehler oder roter Test wuerde den Dienst
# sonst in eine Neustart-Schleife schicken. Dann lieber beim alten Stand bleiben.
if ! (cd "$REPO" && sudo -u "$OWNER" python3 -m py_compile server.py \
        && sudo -u "$OWNER" python3 -m unittest discover -s tests -q); then
    echo "Update $after fehlerhaft (Syntax oder Tests), bleibe bei $before" >&2
    run_git reset --hard --quiet "$before"
    exit 1
fi

echo "Update $before -> $after, starte Dienst neu"
systemctl restart aktienanalyse
