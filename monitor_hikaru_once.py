#!/usr/bin/env python3
"""
Vérification UNIQUE du stock / de la sortie de produits One Piece Card Game.

Sites et produits surveillés :
- Hikaru Distribution : Premium Card Collection ONE PIECE DAY'26 (Shopify, check JSON + repli HTML)
- King Jouet          : Double Pack OP17 "Les Guerriers les plus puissants au monde"
                         (URL produit déjà existante mais actuellement en 404/410, check HTML direct)
- Fnac, Smyths Toys, Cultura : Double Pack OP17 (pas encore de fiche produit -> détection
                         d'apparition d'un lien correspondant sur la page catégorie One Piece)

Conçu pour tourner dans GitHub Actions, déclenché à chaque appel par
cron-job.org via l'API workflow_dispatch. Ne boucle PAS : une exécution
= une vérification de TOUS les sites, puis le process se termine.

Variable d'environnement requise :
- DISCORD_WEBHOOK_URL : URL du webhook Discord (à définir en secret GitHub,
  jamais en clair dans le repo)
- DISCORD_USER_ID : optionnel, pour mentionner l'utilisateur dans l'alerte

Le fichier state_stock.json (à la racine du repo) garde en mémoire le
dernier état connu de CHAQUE site entre deux exécutions. Le workflow
GitHub Actions est responsable de le committer/pousser après chaque run
si l'état a changé.
"""

import json
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --- Configuration ---------------------------------------------------------

SITES = {
    "hikaru": {
        "label": "Hikaru Distribution",
        "product_name": "Premium Card Collection One Piece Card Game - ONE PIECE DAY'26",
        "mode": "shopify",
        "product_url": (
            "https://hikarudistribution.com/products/"
            "premium-card-collection-one-piece-card-game-one-piece-day-26-edition-limitee-japonais"
        ),
    },
    "king_jouet": {
        "label": "King Jouet",
        "product_name": "Double Pack OP17 - Les Guerriers les plus puissants au monde",
        "mode": "html_direct",
        "product_url": (
            "https://www.king-jouet.com/jeu-jouet/jeux-societes/cartes-a-collectionner/"
            "ref-1034966-cartes-one-piece-double-booster-op17-les-guerriers-les-plus-puissants-au-monde.htm"
        ),
    },
    "fnac": {
        "label": "Fnac",
        "product_name": "Double Pack OP17 - Les Guerriers les plus puissants au monde",
        "mode": "category",
        "category_url": (
            "https://www.fnac.com/n564773/Jeux-de-recre-cartes-a-collectionner/"
            "Cartes-a-collectionner-One-Piece"
        ),
        "base_url": "https://www.fnac.com",
    },
    "smyths": {
        "label": "Smyths Toys",
        "product_name": "Double Pack OP17 - Les Guerriers les plus puissants au monde",
        "mode": "category",
        "category_url": "https://www.smythstoys.com/fr/fr-fr/marques/one-piece/c/SM130227",
        "base_url": "https://www.smythstoys.com",
    },
    "cultura": {
        "label": "Cultura",
        "product_name": "Double Pack OP17 - Les Guerriers les plus puissants au monde",
        "mode": "category",
        "category_url": "https://www.cultura.com/cartes-a-jouer/cartes-one-piece.html",
        "base_url": "https://www.cultura.com",
    },
}

STATE_FILE = Path(__file__).resolve().parent / "state_stock.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
}

UNAVAILABLE_MARKERS = [
    "bientôt disponible",
    "épuisé",
    "liste d'attente",
    "rupture de stock",
    "n'est plus disponible",
    "produit indisponible",
    "non disponible",
    "web non dispo",
    "hors stock",
    "actuellement indisponible",
]
AVAILABLE_MARKERS = ["ajouter au panier", "ajouter à mon panier"]

# Pour la détection d'apparition sur les pages catégorie (Fnac, Smyths, Cultura)
OP17_PATTERN = re.compile(r"op[\s\-]?17\b", re.IGNORECASE)
DOUBLEPACK_PATTERN = re.compile(r"double|duo", re.IGNORECASE)


# --- Utilitaires -------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print("[!] state_stock.json illisible, on repart de zéro.")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def mention_prefix() -> str:
    """Retourne '<@ID> ' si DISCORD_USER_ID est défini, sinon une chaîne vide."""
    user_id = os.environ.get("DISCORD_USER_ID", "").strip()
    return f"<@{user_id}> " if user_id else ""


def send_discord_alert(webhook_url: str, site_label: str, product_name: str, product_url: str) -> None:
    payload = {
        "content": (
            f"{mention_prefix()}🚨 **EN STOCK !**\n"
            f"{product_name} vient de passer disponible sur **{site_label}** !\n"
            f"Lien direct : {product_url}"
        )
    }
    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        if r.status_code not in (200, 204):
            print(f"[!] Discord a répondu avec le statut {r.status_code} : {r.text}")
    except requests.RequestException as e:
        print(f"[!] Échec de l'envoi Discord : {e}")


# --- Détection de stock (sites Shopify + URL directe) -------------------------

def check_stock_via_json(product_url: str):
    json_url = product_url + ".json"
    try:
        resp = requests.get(json_url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        print(f"[!] Erreur réseau (JSON) : {e}")
        return None

    if resp.status_code != 200:
        print(f"[!] Statut HTTP inattendu sur le JSON : {resp.status_code}")
        return None

    try:
        data = resp.json()
    except ValueError:
        print("[!] Réponse JSON invalide.")
        return None

    variants = data.get("product", {}).get("variants", [])
    if not variants:
        print("[!] Aucune variante trouvée dans le JSON.")
        return None

    return any(v.get("available") is True for v in variants)


def check_stock_via_html(product_url: str):
    try:
        resp = requests.get(product_url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        print(f"[!] Erreur réseau (HTML) : {e}")
        return None

    if resp.status_code in (404, 410):
        print(f"[i] Page non disponible (HTTP {resp.status_code}) : produit pas encore en ligne.")
        return False

    if resp.status_code != 200:
        print(f"[!] Statut HTTP inattendu sur le HTML : {resp.status_code}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    page_text = soup.get_text(separator=" ").lower()

    has_unavailable = any(marker in page_text for marker in UNAVAILABLE_MARKERS)
    has_available = any(marker in page_text for marker in AVAILABLE_MARKERS)

    if has_available and not has_unavailable:
        return True
    if has_unavailable and not has_available:
        return False
    return None


def check_stock(product_url: str):
    """Utilisé pour les sites Shopify (JSON natif, avec repli HTML si indéterminé)."""
    result = check_stock_via_json(product_url)
    if result is not None:
        return result
    print("[i] JSON indéterminé, tentative de repli via HTML...")
    return check_stock_via_html(product_url)


# --- Détection d'apparition (pages catégorie sans fiche produit dédiée) -------

def check_category_link(category_url: str, base_url: str):
    """
    Cherche sur une page catégorie un lien correspondant au Double Pack OP17.

    Retourne :
      - l'URL complète du produit (str) si un lien correspondant est trouvé
      - False si la page a été chargée avec succès mais que rien ne correspond encore
      - None si la vérification n'a pas pu être faite (erreur réseau / HTTP)
    """
    try:
        resp = requests.get(category_url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        print(f"[!] Erreur réseau (catégorie) : {e}")
        return None

    if resp.status_code != 200:
        print(f"[!] Statut HTTP inattendu sur la page catégorie : {resp.status_code}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    for link in soup.find_all("a", href=True):
        text = link.get_text(" ", strip=True)
        href = link["href"]
        combined = f"{text} {href}".lower()

        if OP17_PATTERN.search(combined) and DOUBLEPACK_PATTERN.search(combined):
            if href.startswith("http"):
                return href
            return base_url.rstrip("/") + "/" + href.lstrip("/")

    return False


# --- Point d'entrée -------------------------------------------------------------

def main() -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("ERREUR : la variable d'environnement DISCORD_WEBHOOK_URL n'est pas définie.")
        sys.exit(1)

    # Mode test : déclenché manuellement depuis GitHub Actions (case à cocher
    # "test_alert" sur le bouton Run workflow). Envoie un message Discord
    # factice pour vérifier le pipeline complet, sans toucher à l'état réel.
    if os.environ.get("TEST_ALERT", "").strip().lower() == "true":
        print("Mode test activé : envoi d'une alerte Discord factice.")
        test_payload = {
            "content": (
                f"{mention_prefix()}🧪 **Ceci est un message de TEST.**\n"
                "Le pipeline GitHub Actions → Discord fonctionne correctement. "
                "Cette alerte ne signifie PAS que le produit est réellement en stock."
            )
        }
        try:
            r = requests.post(webhook_url, json=test_payload, timeout=10)
            if r.status_code not in (200, 204):
                print(f"[!] Discord a répondu avec le statut {r.status_code} : {r.text}")
            else:
                print("Message de test envoyé avec succès.")
        except requests.RequestException as e:
            print(f"[!] Échec de l'envoi Discord : {e}")
        return

    state = load_state()
    state_changed = False

    for site_key, site_info in SITES.items():
        label = site_info["label"]
        product_name = site_info["product_name"]
        mode = site_info["mode"]

        print(f"--- Vérification : {label} ---")

        if mode in ("shopify", "html_direct"):
            product_url = site_info["product_url"]
            available = check_stock(product_url) if mode == "shopify" else check_stock_via_html(product_url)
            previous = state.get(site_key, {}).get("available")

            if available is None:
                print(f"Résultat indéterminé pour {label}, état conservé ({previous}).")
                continue

            status_label = "EN STOCK" if available else "indisponible"
            print(f"Statut {label} : {status_label}")

            if available and previous is not True:
                print(f">>> Passage en stock détecté sur {label}, envoi de l'alerte Discord.")
                send_discord_alert(webhook_url, label, product_name, product_url)

            if available != previous:
                state[site_key] = {"available": available}
                state_changed = True

        elif mode == "category":
            category_url = site_info["category_url"]
            base_url = site_info["base_url"]
            result = check_category_link(category_url, base_url)
            previous = state.get(site_key, {}).get("found_url")

            if result is None:
                print(f"Résultat indéterminé pour {label}, état conservé.")
                continue

            if result is False:
                print(f"Statut {label} : produit pas encore listé.")
                if previous is not None:
                    state[site_key] = {"found_url": None}
                    state_changed = True
                continue

            found_url = result
            print(f"Statut {label} : lien détecté -> {found_url}")

            if found_url != previous:
                print(f">>> Nouveau lien détecté sur {label}, envoi de l'alerte Discord.")
                send_discord_alert(webhook_url, label, product_name, found_url)
                state[site_key] = {"found_url": found_url}
                state_changed = True

    if state_changed:
        save_state(state)
        print("État mis à jour dans state_stock.json.")
    else:
        print("Pas de changement d'état.")


if __name__ == "__main__":
    main()
