# Exécution locale sur Mac

Certains sites refusent les adresses IP des runners GitHub Actions (plages Azure)
mais répondent normalement à une connexion résidentielle. Faire tourner le
moniteur depuis le Mac récupère **Cultura**, que la CI ne peut pas atteindre.

## Répartition des sites

Les deux environnements se partagent la surveillance via `MONITOR_SITES`, pour
éviter que la même alerte parte deux fois :

| Environnement | Sites | Pourquoi |
|---|---|---|
| GitHub Actions | `hikaru`, `hikaru_one_piece`, `king_jouet`, `leclerc` | tourne 24 h/24, indépendant du Mac |
| Mac (launchd) | `cultura` | seule façon d'atteindre le site |

Le Mac garde son propre fichier d'état (`state_local.json`), séparé du
`state_stock.json` que la CI committe : les deux ne se marchent pas dessus.

## Installation

```bash
security add-generic-password -s one-piece-discord-webhook -a "$USER" -w
```

Cette commande demande l'URL du webhook Discord et la range dans le trousseau
macOS **sans l'afficher ni l'écrire sur le disque**. Elle ne doit être lancée que
par vous : l'URL ne doit transiter par aucun fichier ni aucune conversation.

Pour être mentionné dans les alertes, ajoutez aussi votre identifiant Discord :

```bash
security add-generic-password -s one-piece-discord-user -a "$USER" -w
```

Puis, depuis le dépôt :

```bash
./local/install.sh
```

Le script copie le moniteur dans `~/one-piece-monitor`, crée son environnement
Python, et enregistre la tâche launchd. Il est relançable sans risque après
chaque mise à jour du code.

## Au quotidien

```bash
tail -f ~/one-piece-monitor/monitor.log
```

Arrêter la surveillance :

```bash
launchctl bootout gui/$(id -u)/com.aronbis.onepiece-monitor
```

## Limites

**Le Mac doit être allumé.** launchd rattrape l'exécution manquée au réveil, mais
un drop survenu pendant la nuit ne sera pas détecté à temps. C'est pour ça que les
sites qui fonctionnent en CI y restent : eux tournent 24 h/24.

**La Fnac ne passe pas, même en local.** Elle filtre l'empreinte TLS en plus de
l'adresse IP : `requests` reçoit un 403 depuis le Mac aussi, alors qu'un vrai
navigateur charge la page sans difficulté. Il faudrait piloter un Chrome complet
(Playwright) pour la récupérer.

**Smyths est hors d'atteinte.** Sa protection Imperva bloque même un vrai
navigateur depuis une connexion résidentielle.
