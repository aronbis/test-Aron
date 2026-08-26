#!/usr/bin/env python3
"""
Expérience jetable : un navigateur headless passe-t-il les protections anti-bot
de la Fnac et de Smyths depuis un runner GitHub Actions ?

Le doute ne porte pas sur le navigateur mais sur l'IP : les runners GitHub sont
sur des plages Azure, que les protections type Akamai/Imperva pénalisent
lourdement. Un vrai navigateur depuis une IP résidentielle passe chez la Fnac ;
il faut vérifier si c'est encore vrai depuis la CI.

À supprimer une fois la réponse obtenue.
"""

import sys

from playwright.sync_api import sync_playwright

CIBLES = [
    ("Fnac", "https://www.fnac.com/n564773/Jeux-de-recre-cartes-a-collectionner/"
             "Cartes-a-collectionner-One-Piece", "a[href*='/a']"),
    ("Smyths", "https://www.smythstoys.com/fr/fr-fr/marques/one-piece/c/SM130227", "a[href]"),
]

BLOCAGE = ("pardon our interruption", "access denied", "incapsula", "request unsuccessful")


def sonde(page, nom, url, selecteur) -> bool:
    print(f"\n=== {nom} ===")
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"  échec de navigation : {e!r}")
        return False

    page.wait_for_timeout(6000)  # laisser tourner un éventuel défi JS
    statut = resp.status if resp else "?"
    titre = (page.title() or "").strip()
    try:
        corps = page.inner_text("body")
    except Exception:
        corps = ""
    print(f"  HTTP {statut} | titre: {titre[:70]!r} | corps: {len(corps)} car.")

    bas = (titre + " " + corps[:3000]).lower()
    if any(m in bas for m in BLOCAGE):
        print("  --> BLOQUÉ (page de défi anti-bot)")
        return False

    liens = page.eval_on_selector_all(
        selecteur,
        "els => [...new Set(els.map(e => (e.innerText||'').trim().replace(/\\s+/g,' '))"
        ".filter(t => /one.?piece/i.test(t)))].slice(0, 8)",
    )
    print(f"  --> produits One Piece détectés : {len(liens)}")
    for t in liens[:5]:
        print(f"       - {t[:70]}")
    return len(liens) > 0


def main() -> int:
    with sync_playwright() as p:
        navigateur = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        contexte = navigateur.new_context(
            locale="fr-FR",
            timezone_id="Europe/Paris",
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9"},
        )
        # Masque le marqueur d'automatisation le plus grossier.
        contexte.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = contexte.new_page()
        resultats = {nom: sonde(page, nom, url, sel) for nom, url, sel in CIBLES}
        navigateur.close()

    print("\n=== BILAN ===")
    for nom, ok in resultats.items():
        print(f"  {nom:8s} : {'EXPLOITABLE' if ok else 'bloqué'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
