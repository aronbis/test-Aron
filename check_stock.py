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
from typing import Dict, Optional

import requests
from playwright.sync_api import sync_playwright, Error as PlaywrightError

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
    #     "name": "Nom du nouvel objet",
    #     "url": "https://p-bandai.com/us/item/XXXXXXXXXX",
    # },
]

STATE_FILE = Path("state.json")

# Le texte du bouton d'achat est le signal le plus fiable.
IN_STOCK_KEYWORDS = ["place order", "place pre-order", "add to cart"]
OUT_OF_STOCK_KEYWORDS = [
    "sorry, out of stock",
    "out of stock",
    "sold out",
    "notify me",
    "waitlist",
]

# Sélecteurs des éléments cliquables où apparaît le statut d'achat.
BUTTON_SELECTOR = "button, a[role='button'], input[type='submit'], [class*='btn']"

# Ressources inutiles pour lire du texte : on les bloque pour accélérer.
BLOCKED_RESOURCES = {"image", "font", "media"}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# Attend que le statut de stock soit réellement affiché, plutôt que
# d'attendre un délai fixe ou que tout le réseau se calme.
WAIT_FOR_STATUS_JS = """
() => {
    const t = document.body.innerText.toLowerCase();
    return %s.some(k => t.includes(k));
}
""" % json.dumps(IN_STOCK_KEYWORDS + OUT_OF_STOCK_KEYWORDS)


def check_product(page, url: str) -> Optional[bool]:
    """Retourne True (en stock), False (rupture), ou None si indéterminé."""
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_function(WAIT_FOR_STATUS_JS, timeout=20000)

    # On regarde d'abord le texte des boutons : bien plus fiable que toute
    # la page, qui contient aussi les produits recommandés en bas.
    button_texts = " | ".join(page.locator(BUTTON_SELECTOR).all_inner_texts()).lower()

    if any(k in button_texts for k in IN_STOCK_KEYWORDS):
        return True
    if any(k in button_texts for k in OUT_OF_STOCK_KEYWORDS):
        return False

    # Repli sur le texte complet si aucun bouton exploitable n'a été trouvé.
    body_text = page.inner_text("body").lower()
    if any(k in body_text for k in OUT_OF_STOCK_KEYWORDS):
        return False
    if any(k in body_text for k in IN_STOCK_KEYWORDS):
        return True

    return None


def check_product_with_retry(page, url: str) -> Optional[bool]:
    for attempt in (1, 2):
        try:
            return check_product(page, url)
        except PlaywrightError as exc:
            print(f"  Tentative {attempt} échouée : {type(exc).__name__}")
            if attempt == 2:
                return None
    return None


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
        context = browser.new_context(user_agent=USER_AGENT)
        # Bloque images/polices/vidéos : inutiles ici, et c'est l'essentiel
        # du poids de la page.
        context.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in BLOCKED_RESOURCES
                else route.continue_()
            ),
        )
        page = context.new_page()

        for product in PRODUCTS:
            name = product["name"]
            url = product["url"]
            was_in_stock = state.get(url)

            in_stock_now = check_product_with_retry(page, url)

            if in_stock_now is None:
                # Statut indéterminé : on ne touche pas à l'état enregistré
                # pour ne pas provoquer de fausse notification au run suivant.
                print(f"{name} -> INDÉTERMINÉ (état conservé : {was_in_stock})")
                continue

            print(f"{name} -> {'oui' if in_stock_now else 'non'} (précédent : {was_in_stock})")

            if in_stock_now and was_in_stock is False:
                send_discord_notification(webhook_url, name, url)
                print("  Notification envoyée.")

            state[url] = in_stock_now

        browser.close()

    save_state(state)


if __name__ == "__main__":
    main()
