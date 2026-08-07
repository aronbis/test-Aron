"""
Surveille une page produit p-bandai.com et envoie une notification Discord
dès que l'objet passe de "en rupture" à "en stock".

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
from bs4 import BeautifulSoup

PRODUCT_URL_DEFAULT = "https://p-bandai.com/us/item/N2881648002"
STATE_FILE = Path("state.json")

# Mots-clés indiquant que le produit est INDISPONIBLE.
# Ouvre la page dans ton navigateur, clic droit > Inspecter sur le bouton
# d'achat ou la mention de stock, et ajuste cette liste si besoin.
OUT_OF_STOCK_KEYWORDS = [
    "sold out",
    "out of stock",
    "notify me",
    "waitlist",
    "currently unavailable",
]


def fetch_page(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        )
    }
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    return response.text


def is_in_stock(html: str) -> bool:
    text = BeautifulSoup(html, "html.parser").get_text(separator=" ").lower()
    return not any(keyword in text for keyword in OUT_OF_STOCK_KEYWORDS)


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

    html = fetch_page(product_url)
    in_stock_now = is_in_stock(html)
    was_in_stock = load_previous_state()

    print(f"Stock actuel : {'oui' if in_stock_now else 'non'} (précédent : {was_in_stock})")

    if in_stock_now and was_in_stock is False:
        send_discord_notification(webhook_url, product_url)
        print("Notification envoyée.")

    save_state(in_stock_now)


if __name__ == "__main__":
    main()
