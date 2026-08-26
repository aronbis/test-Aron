#!/bin/bash
# Lance une vérification depuis le Mac, pour les sites que GitHub Actions ne peut
# pas atteindre (Cultura refuse les IP des runners GitHub, pas une connexion
# résidentielle). Appelé par launchd — voir local/README.md.
set -uo pipefail

BASE="${MONITOR_HOME:-$HOME/one-piece-monitor}"
LOG="$BASE/monitor.log"

# Le webhook Discord vit dans le trousseau macOS, jamais en clair sur le disque.
if ! DISCORD_WEBHOOK_URL="$(security find-generic-password -s one-piece-discord-webhook -w 2>/dev/null)"; then
  echo "$(date '+%F %T') [!] Webhook introuvable dans le trousseau. Voir local/README.md." >> "$LOG"
  exit 1
fi
export DISCORD_WEBHOOK_URL

# Identifiant Discord facultatif, pour être mentionné dans l'alerte.
DISCORD_USER_ID="$(security find-generic-password -s one-piece-discord-user -w 2>/dev/null || true)"
export DISCORD_USER_ID

# Sites réservés à la machine perso. GitHub Actions traite les autres : sans ce
# partage, les deux enverraient l'alerte en double sur les sites communs.
export MONITOR_SITES="${MONITOR_SITES:-cultura}"
export MONITOR_STATE_FILE="$BASE/state_local.json"

{
  echo "===== $(date '+%F %T') ====="
  "$BASE/venv/bin/python" "$BASE/monitor_hikaru_once.py"
  echo "--- code de sortie : $? ---"
} >> "$LOG" 2>&1

# Empêche le journal de grossir indéfiniment (garde les 2000 dernières lignes).
if [ "$(wc -l < "$LOG")" -gt 4000 ]; then
  tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
