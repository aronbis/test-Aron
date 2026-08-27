# Lancer l'inventaire depuis Discord

Écrire **`!stock`** dans le salon surveillé déclenche le même inventaire que la
case « Envoyer l'inventaire Discord » du workflow GitHub. `!dispo` et
`!inventaire` fonctionnent aussi.

## Comment ça marche

Un webhook Discord ne sait qu'**envoyer** des messages, jamais en lire. Lire une
commande demande donc un bot. Plutôt que d'en faire tourner un en permanence, le
run qui s'exécute déjà chaque minute interroge l'API Discord et regarde si un
`!stock` est apparu depuis son passage précédent.

**Cette écoute tourne sur le Mac, pas dans GitHub Actions.** Discord filtre les
IP des runners GitHub sur ses endpoints de salon. C'est mesuré, pas supposé : le
même appel avec un token volontairement invalide répond `401 Unauthorized` depuis
une IP résidentielle, mais `403` (code `40333`) depuis la CI — le rejet précède
donc l'authentification, et aucune permission de bot n'y changerait quoi que ce
soit.

Deux conséquences :

- la réponse peut prendre jusqu'à une minute, l'intervalle de la tâche launchd ;
- **`!stock` ne répond que si le Mac est allumé.** Les alertes de stock, elles,
  continuent de tomber 24 h/24 depuis GitHub Actions.

Le dernier message lu est mémorisé dans `state_stock.json`, sous la clé
`_discord` : une commande n'est donc jamais traitée deux fois, et les messages
antérieurs à l'installation sont ignorés.

## Installation

### 1. Créer l'application Discord

Sur <https://discord.com/developers/applications> → **New Application**.
Dans l'onglet **Bot** :

- activez **MESSAGE CONTENT INTENT** (sans lui, Discord renvoie un contenu vide
  et le bot ne verra jamais votre commande) ;
- cliquez **Reset Token** et copiez le token.

### 2. Ranger le token dans le trousseau du Mac

```bash
security add-generic-password -s one-piece-discord-bot -a "$USER" -w
```

La commande demande le token sans l'afficher et le range dans le trousseau
macOS. Il n'est écrit dans aucun fichier, et n'a pas sa place dans un secret
GitHub : la CI ne peut de toute façon pas s'en servir.

Ce token donne accès à votre bot : il ne doit jamais apparaître dans un fichier
du dépôt, ni dans un message. Si vous l'exposez par accident, utilisez
**Reset Token** dans le portail développeur, l'ancien devient aussitôt inutile.

### 3. Inviter le bot sur le serveur

Onglet **OAuth2 → URL Generator** : cochez le scope **bot**, puis les
permissions **View Channel** et **Read Message History**. Ouvrez l'URL générée
et choisissez votre serveur.

Le bot n'a besoin de rien d'autre : il lit, il n'écrit pas. Les alertes
continuent de passer par le webhook existant.

### 4. Donner l'identifiant du salon

Dans Discord : **Paramètres → Avancés → Mode développeur**, puis clic droit sur
le salon → **Copier l'identifiant du salon**. Rangez-le lui aussi :

```bash
security add-generic-password -s one-piece-discord-channel -a "$USER" -w
```

### 5. Installer et essayer

```bash
./local/install.sh
```

Puis écrivez `!stock` dans le salon. L'inventaire arrive dans la minute.

## Si rien ne se passe

Le journal du Mac dit précisément quoi :

```bash
tail -f ~/one-piece-monitor/monitor.log
```


| Message | Cause |
|---|---|
| `Token de bot Discord refusé (401)` | `DISCORD_BOT_TOKEN` absent ou périmé |
| `Le bot n'a pas accès à ce salon (403)` | bot non invité, ou sans permission de lecture |
| aucune ligne `Commande Discord reçue` | l'intent **Message Content** n'est pas activé |

Tant que les deux entrées de trousseau ne sont pas créées, la fonctionnalité est
simplement inactive — la surveillance, elle, continue de tourner.

En cas de doute sur la configuration du bot, le workflow GitHub garde une case
**« Diagnostiquer la configuration du bot Discord »** : elle vérifie l'identité du
bot et la liste de ses serveurs, ce qui reste valable depuis la CI. Seul l'accès
au salon y échouera, pour la raison réseau expliquée plus haut.
