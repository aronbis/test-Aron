#!/bin/bash
# Installe le moniteur en tâche de fond sur le Mac.
# Relançable sans risque : met simplement à jour l'installation existante.
set -euo pipefail

BASE="${MONITOR_HOME:-$HOME/one-piece-monitor}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.aronbis.onepiece-monitor.plist"

echo "Installation dans $BASE"
mkdir -p "$BASE"

# 1. Le script et son lanceur
cp "$SRC/monitor_hikaru_once.py" "$BASE/"
cp "$SRC/local/run_monitor.sh" "$BASE/"
chmod +x "$BASE/run_monitor.sh"

# 2. Environnement Python isolé
if [ ! -x "$BASE/venv/bin/python" ]; then
  echo "Création de l'environnement Python..."
  python3 -m venv "$BASE/venv"
fi
"$BASE/venv/bin/pip" install --quiet --upgrade pip
"$BASE/venv/bin/pip" install --quiet requests beautifulsoup4

# 3. Le webhook doit être dans le trousseau — jamais en clair sur le disque
if ! security find-generic-password -s one-piece-discord-webhook >/dev/null 2>&1; then
  cat <<'MSG'

  Le webhook Discord n'est pas encore dans votre trousseau.
  Lancez ceci vous-même (l'URL ne transitera par aucun fichier) :

      security add-generic-password -s one-piece-discord-webhook \
        -a "$USER" -w

  La commande demandera l'URL sans l'afficher. Puis relancez ce script.

MSG
  exit 1
fi

# 4. Tâche launchd
mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__HOME__|$HOME|g" "$SRC/local/com.aronbis.onepiece-monitor.plist" > "$PLIST"

launchctl bootout "gui/$(id -u)/com.aronbis.onepiece-monitor" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo
echo "Installé. Le moniteur tourne toutes les 2 minutes."
echo "  journal   : tail -f $BASE/monitor.log"
echo "  état      : $BASE/state_local.json"
echo "  arrêter   : launchctl bootout gui/$(id -u)/com.aronbis.onepiece-monitor"
