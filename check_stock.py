"""
Surveille une page produit p-bandai.com et envoie une notification Discord
dès que l'objet passe de "en rupture" à "en stock".

Utilise Playwright (un vrai navigateur headless) car le statut du stock
est injecté dans la page par JavaScript après le chargement initial —
une simple requête HTTP ne suffit pas à le voir.

Variables d'environnement requises :
    DISCORD_WEBHOOK_URL : l'URL du webhook Discord
    PRODUCT_URL         : (optionnel) l'URL du produit à surveiller,
                           sinon utilise PRODUCT_URL_DEFAULT ci-dessous
"""

import json
import os
from pathlib import Path
from typing import Optional

import requests
from playwright.sync_api import sync_playwright

PRODUCT_URL_DEFAULT = "https://p-bandai.com/us/item/N2881648002"
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


def fetch_rendered_text(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            )
        )
        page.goto(url, wait_until="networkidle", timeout=30000)
        # Petite marge de sécurité si un appel réseau se déclenche juste après.
        page.wait_for_timeout(1500)
        text = page.inner_text("body")
        browser.close()
        return text.lower()


def is_in_stock(page_text: str) -> bool:
    if any(keyword in page_text for keyword in IN_STOCK_KEYWORDS):
        return True
    if any(keyword in page_text for keyword in OUT_OF_STOCK_KEYWORDS):
        return False
    # Signal ambigu : par sécurité on considère que ce n'est pas en stock,
    # pour éviter une fausse notification.
    return False


def load_previous_state() -> Optional[bool]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text()).get("in_stock")
    return None


def save_state(in_stock: bool) -> None:
    STATE_FILE.write_text(json.dumps({"in_stock": in_stock}))


def send_discord_notification(webhook_url: str, product_url: str) -> None:
    payload = {"content": f"🟢 En stock maintenant : {product_url}"}
    requests.post(webhook_url, json=payload, timeout=10)


def main() -> None:
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    product_url = os.environ.get("PRODUCT_URL", PRODUCT_URL_DEFAULT)

    if os.environ.get("TEST_NOTIFICATION", "false").lower() == "true":
        send_discord_notification(webhook_url, product_url)
        print("Notification de test envoyée.")
        return

    page_text = fetch_rendered_text(product_url)
    in_stock_now = is_in_stock(page_text)
    was_in_stock = load_previous_state()

    print(f"Stock actuel : {'oui' if in_stock_now else 'non'} (précédent : {was_in_stock})")

    if in_stock_now and was_in_stock is False:
        send_discord_notification(webhook_url, product_url)
        print("Notification envoyée.")

    save_state(in_stock_now)


if __name__ == "__main__":
    main()
