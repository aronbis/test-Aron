# Lancer l'inventaire depuis Discord

Écrire **`!stock`** dans le salon surveillé déclenche le même inventaire que la
case « Envoyer l'inventaire Discord » du workflow GitHub. `!dispo` et
`!inventaire` fonctionnent aussi.

## Comment ça marche

Un webhook Discord ne sait qu'**envoyer** des messages, jamais en lire. Lire une
commande demande donc un bot. Plutôt que de faire tourner un bot en permanence —
ce qui supposerait un serveur allumé en continu — le run qui s'exécute déjà
chaque minute interroge l'API Discord et regarde si un `!stock` est apparu depuis
son passage précédent.

Conséquence : **la réponse peut prendre jusqu'à une minute**. C'est le prix à
payer pour ne rien avoir à héberger.

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

### 2. Ranger le token dans GitHub

Dans le dépôt : **Settings → Secrets and variables → Actions → New repository
secret**, nommé `DISCORD_BOT_TOKEN`.

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
le salon `#stock` → **Copier l'identifiant du salon**. Ajoutez-le en secret
GitHub sous le nom `DISCORD_CHANNEL_ID`.

### 5. Essayer

Écrivez `!stock` dans `#stock`. L'inventaire arrive dans la minute.

## Si rien ne se passe

Les logs du workflow (onglet Actions) disent précisément quoi :

| Message | Cause |
|---|---|
| `Token de bot Discord refusé (401)` | `DISCORD_BOT_TOKEN` absent ou périmé |
| `Le bot n'a pas accès à ce salon (403)` | bot non invité, ou sans permission de lecture |
| aucune ligne `Commande Discord reçue` | l'intent **Message Content** n'est pas activé |

Tant que les deux secrets ne sont pas renseignés, la fonctionnalité est
simplement inactive — la surveillance, elle, continue de tourner.
