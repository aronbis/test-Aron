"""
Surveille plusieurs pages produits p-bandai.com et envoie une notification
Discord dès qu'un objet passe de "en rupture" à "en stock".

Variable d'environnement requise :
    DISCORD_WEBHOOK_URL : l'URL du webhook Discord
"""

import json
import os
import time
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
ALL_KEYWORDS = IN_STOCK_KEYWORDS + OUT_OF_STOCK_KEYWORDS

# Sélecteurs des éléments cliquables où apparaît le statut d'achat.
BUTTON_SELECTOR = "button, a[role='button'], input[type='submit'], [class*='btn']"

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
""" % json.dumps(ALL_KEYWORDS)


def decide(text: str) -> Optional[bool]:
    """True = en stock, False = rupture, None = rien de reconnaissable."""
    if any(k in text for k in IN_STOCK_KEYWORDS):
        return True
    if any(k in text for k in OUT_OF_STOCK_KEYWORDS):
        return False
    return None


BLOCK_MARKERS = ["page not available", "can not be displayed", "access denied"]


def warm_up(page) -> None:
    """Visite la page d'accueil d'abord, comme un vrai visiteur, pour
    récupérer les cookies de session avant d'ouvrir la fiche produit."""
    try:
        page.goto("https://p-bandai.com/us/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)
    except PlaywrightError as exc:
        print(f"  [info] échauffement ignoré : {type(exc).__name__}")


def check_product(page, url: str) -> Optional[bool]:
    t0 = time.monotonic()
    response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    t1 = time.monotonic()

    status = response.status if response else "?"

    # On attend que le statut apparaisse, mais sans faire échouer le run :
    # si le délai expire, on lit quand même ce qui est présent.
    try:
        page.wait_for_function(WAIT_FOR_STATUS_JS, timeout=25000)
        waited = f"{time.monotonic() - t1:.1f}s"
    except PlaywrightError:
        waited = f"timeout ({time.monotonic() - t1:.1f}s)"

    print(f"  [temps] HTTP {status} | chargement {t1 - t0:.1f}s | statut {waited}")

    early_text = page.inner_text("body").lower()
    if any(m in early_text for m in BLOCK_MARKERS):
        print("  [BLOQUÉ] le site a renvoyé sa page d'erreur anti-bot.")
        return None

    # Le texte des boutons est plus fiable que toute la page, qui contient
    # aussi les produits recommandés en bas.
    button_texts = " | ".join(page.locator(BUTTON_SELECTOR).all_inner_texts()).lower()
    result = decide(button_texts)
    if result is not None:
        return result

    body_text = page.inner_text("body").lower()
    result = decide(body_text)
    if result is not None:
        return result

    # Diagnostic : on montre un extrait pour comprendre ce que voit le bot.
    print(f"  [diagnostic] titre : {page.title()!r}")
    print(f"  [diagnostic] début du texte : {body_text[:300]!r}")
    return None


def check_product_with_retry(page, url: str) -> Optional[bool]:
    for attempt in (1, 2):
        try:
            return check_product(page, url)
        except PlaywrightError as exc:
            print(f"  Tentative {attempt} échouée : {type(exc).__name__} — {exc}"[:400])
            if attempt == 1:
                time.sleep(3)
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
        t_launch = time.monotonic()
        launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        try:
            browser = p.chromium.launch(channel="chrome", args=launch_args)
        except PlaywrightError:
            print("Chrome introuvable, repli sur le Chromium de Playwright.")
            browser = p.chromium.launch(args=launch_args)
        print(f"[temps] lancement du navigateur {time.monotonic() - t_launch:.1f}s")

        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1280, "height": 900},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        page = context.new_page()
        warm_up(page)

        for product in PRODUCTS:
            name = product["name"]
            url = product["url"]
            was_in_stock = state.get(url)

            in_stock_now = check_product_with_retry(page, url)

            if in_stock_now is None:
                # Statut indéterminé : on conserve l'état enregistré pour ne
                # pas provoquer de fausse notification au run suivant.
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
