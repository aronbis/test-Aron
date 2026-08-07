"""
Surveille plusieurs pages produits p-bandai.com et envoie une notification
Discord dès qu'un objet passe de "en rupture" à "en stock".

Pour ajouter un objet à surveiller : ajoute un bloc dans la liste PRODUCTS
ci-dessous, avec un nom (libre, pour reconnaître l'objet dans Discord) et
l'URL du produit.

Variable d'environnement requise :
    DISCORD_WEBHOOK_URL : l'URL du webhook Discord
"""

import json
import os
from pathlib import Path
from typing import Dict

import requests
from playwright.sync_api import sync_playwright

# ----------------------------------------------------------------------
# Liste des objets à surveiller.
# Pour en ajouter un : copie un bloc { ... }, change "name" et "url",
# et ajoute une virgule après le bloc précédent.
# ----------------------------------------------------------------------
PRODUCTS = [
    {
        "name": "ONE PIECE CARD GAME Premium Card Collection -Ace & Sabo & Luffy-",
        "url": "https://p-bandai.com/us/item/N2881648002",
    },
    # {
    #     "name": "ONE PIECE CARD GAME Chinese 3rd Anniversary Set",
    #     "url": "https://p-bandai.com/us/item/N2904549002",
    # },
]

STATE_FILE = Path("state.json")

# Le bouton d'achat est le signal le plus fiable.
IN_STOCK_KEYWORDS = ["place order", "place pre-order"]
OUT_OF_STOCK_KEYWORDS = [
    "sorry, out of stock",
    "out of stock",
    "sold out",
    "notify me",
    "waitlist",
]


def fetch_rendered_text(page, url: str) -> str:
    page.goto(url, wait_until="networkidle", timeout=30000)
    # Petite marge de sécurité si un appel réseau se déclenche juste après.
    page.wait_for_timeout(1500)
    return page.inner_text("body").lower()


def is_in_stock(page_text: str) -> bool:
    if any(keyword in page_text for keyword in IN_STOCK_KEYWORDS):
        return True
    if any(keyword in page_text for keyword in OUT_OF_STOCK_KEYWORDS):
        return False
    # Signal ambigu : par sécurité on considère que ce n'est pas en stock,
    # pour éviter une fausse notification.
    return False


def load_state() -> Dict[str, bool]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: Dict[str, bool]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_discord_notification(webhook_url: str, name: str, url: str) -> None:
    payload = {"content": f"🟢 En stock maintenant : **{name}**\n{url}"}
    requests.post(webhook_url, json=payload, timeout=10)


def main() -> None:
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]

    if os.environ.get("TEST_NOTIFICATION", "false").lower() == "true":
        send_discord_notification(webhook_url, "Test", PRODUCTS[0]["url"])
        print("Notification de test envoyée.")
        return

    state = load_state()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            )
        )

        for product in PRODUCTS:
            name = product["name"]
            url = product["url"]

            page_text = fetch_rendered_text(page, url)
            in_stock_now = is_in_stock(page_text)
            was_in_stock = state.get(url)

            print(f"{name} -> {'oui' if in_stock_now else 'non'} (précédent : {was_in_stock})")

            if in_stock_now and was_in_stock is False:
                send_discord_notification(webhook_url, name, url)
                print("  Notification envoyée.")

            state[url] = in_stock_now

        browser.close()

    save_state(state)


if __name__ == "__main__":
    main()
