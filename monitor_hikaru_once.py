#!/usr/bin/env python3
"""
Vérification UNIQUE du stock - Hikaru Distribution (boutique Shopify)
Produit : Premium Card Collection One Piece Card Game
          - ONE PIECE DAY'26 - Édition limitée - Japonais

Conçu pour tourner dans GitHub Actions, déclenché à chaque appel par
cron-job.org via l'API workflow_dispatch. Ne boucle PAS : une exécution
= une vérification, puis le process se termine.

Variable d'environnement requise :
- DISCORD_WEBHOOK_URL : URL du webhook Discord (à définir en secret GitHub,
  jamais en clair dans le repo)

Le fichier state_hikaru.json (à la racine du repo) garde en mémoire le
dernier état connu entre deux exécutions. Le workflow GitHub Actions est
responsable de le committer/pousser après chaque run si l'état a changé.
"""

import json
import os
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --- Configuration ---------------------------------------------------------

PRODUCT_URL = (
    "https://hikarudistribution.com/products/"
    "premium-card-collection-one-piece-card-game-one-piece-day-26-edition-limitee-japonais"
)
JSON_URL = PRODUCT_URL + ".json"

STATE_FILE = Path(__file__).resolve().parent / "state_hikaru.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
}

UNAVAILABLE_MARKERS = ["bientôt disponible", "épuisé", "liste d'attente", "rupture de stock"]
AVAILABLE_MARKERS = ["ajouter au panier"]


# --- Utilitaires -------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print("[!] state_hikaru.json illisible, on repart de zéro.")
    return {"available": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def send_discord_alert(webhook_url: str) -> None:
    payload = {
        "content": (
            "🚨 **EN STOCK !**\n"
            "Premium Card Collection One Piece Card Game - ONE PIECE DAY'26 "
            "(Hikaru Distribution) vient de passer disponible !\n"
            f"{PRODUCT_URL}"
        )
    }
    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        if r.status_code not in (200, 204):
            print(f"[!] Discord a répondu avec le statut {r.status_code} : {r.text}")
    except requests.RequestException as e:
        print(f"[!] Échec de l'envoi Discord : {e}")


# --- Détection de stock -------------------------------------------------------

def check_stock_via_json() -> bool | None:
    try:
        resp = requests.get(JSON_URL, headers=HEADERS, timeout=15)
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


def check_stock_via_html() -> bool | None:
    try:
        resp = requests.get(PRODUCT_URL, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        print(f"[!] Erreur réseau (HTML) : {e}")
        return None

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


def check_stock() -> bool | None:
    result = check_stock_via_json()
    if result is not None:
        return result
    print("[i] JSON indéterminé, tentative de repli via HTML...")
    return check_stock_via_html()


# --- Point d'entrée -------------------------------------------------------------

def main() -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("ERREUR : la variable d'environnement DISCORD_WEBHOOK_URL n'est pas définie.")
        sys.exit(1)

    state = load_state()
    available = check_stock()

    if available is None:
        print(f"Résultat indéterminé, état conservé ({state.get('available')}).")
        return

    label = "EN STOCK" if available else "indisponible"
    print(f"Statut : {label}")

    if available and state.get("available") is not True:
        print(">>> Passage en stock détecté, envoi de l'alerte Discord.")
        send_discord_alert(webhook_url)

    if available != state.get("available"):
        state["available"] = available
        save_state(state)
        print("État mis à jour dans state_hikaru.json.")
    else:
        print("Pas de changement d'état.")


if __name__ == "__main__":
    main()
