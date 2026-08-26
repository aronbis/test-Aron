#!/usr/bin/env python3
"""
Vérification UNIQUE du stock / de la sortie de produits One Piece Card Game.

Sites et produits surveillés :
- Hikaru Distribution : Premium Card Collection ONE PIECE DAY'26 (Shopify, check JSON + repli HTML)
- Hikaru Distribution : tous les Double Pack Set One Piece (via la recherche Shopify,
                         pour être alerté aussi bien d'un retour en stock que de la mise
                         en ligne d'une nouvelle référence comme le Double Pack OP17 FR)
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

Règle importante : l'état n'est mis à jour QUE si l'alerte Discord
correspondante est bien partie. Sinon un échec d'envoi ferait passer le
site en "déjà signalé" et l'alerte serait perdue définitivement.

Sur les boutiques Shopify, l'alerte contient un "cart permalink" qui ajoute
directement la variante en stock au panier : un clic depuis Discord et le
panier est déjà rempli, ce qui fait gagner les quelques secondes qui comptent
un jour de drop.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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
    "hikaru_double_pack": {
        "label": "Hikaru Distribution",
        "product_name": "Double Pack Set One Piece Card Game",
        # Pas de fiche produit fixe : le Double Pack OP17 français n'existe pas
        # encore sur la boutique. On interroge le moteur de recherche Shopify à
        # chaque run pour suivre TOUS les double packs One Piece : on est donc
        # alerté aussi bien d'un retour en stock que de l'apparition d'une
        # nouvelle référence (l'OP17 le jour où elle sera mise en ligne).
        "mode": "shopify_search",
        "search_url": "https://hikarudistribution.com/search/suggest.json",
        "search_query": "double pack one piece",
        "base_url": "https://hikarudistribution.com",
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

# (délai de connexion, délai de lecture) en secondes
TIMEOUT = (10, 20)

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

# Pour filtrer les résultats de la recherche Shopify (Hikaru) : on ne garde que
# les double packs One Piece, la recherche renvoyant aussi des produits Pokémon
# dont le titre contient "Double".
ONEPIECE_PATTERN = re.compile(r"one[\s\-]?piece", re.IGNORECASE)
SHOPIFY_SEARCH_LIMIT = 10

# Quantité pré-remplie dans les liens "ajouter au panier" envoyés sur Discord.
CART_QTY = 1


# --- Session HTTP ------------------------------------------------------------

def build_session() -> requests.Session:
    """
    Session partagée : garde les connexions ouvertes entre les sites et
    réessaie automatiquement les GET qui échouent (réseau instable, 429/5xx
    passagers). Sans ça, un simple hoquet renvoie "indéterminé" et on rate
    potentiellement une fenêtre de restock.
    """
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=1,  # 0s, 1s, 2s entre les tentatives
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = build_session()


# --- Utilitaires -------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print("[!] state_stock.json illisible, on repart de zéro.")
            return {}
        if isinstance(state, dict):
            return state
        print("[!] state_stock.json n'est pas un objet JSON, on repart de zéro.")
    return {}


def save_state(state: dict) -> None:
    """
    Écriture atomique (fichier temporaire + remplacement) : le workflow
    committe ce fichier, on ne veut jamais y laisser du JSON tronqué si le
    job est interrompu en plein écriture. Trié + indenté pour que les diffs
    Git restent lisibles d'un run à l'autre.
    """
    payload = json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp_file = STATE_FILE.with_suffix(".json.tmp")
    tmp_file.write_text(payload, encoding="utf-8")
    tmp_file.replace(STATE_FILE)


def mention_prefix() -> str:
    """Retourne '<@ID> ' si DISCORD_USER_ID est défini, sinon une chaîne vide."""
    user_id = os.environ.get("DISCORD_USER_ID", "").strip()
    return f"<@{user_id}> " if user_id else ""


def post_discord(webhook_url: str, content: str, attempts: int = 3) -> bool:
    """
    Envoie un message Discord. Retourne True seulement si Discord a bien
    accusé réception : l'appelant s'en sert pour décider s'il peut marquer
    l'alerte comme "déjà envoyée" dans l'état.

    Les POST ne passent pas par le Retry de la session (un POST rejoué après
    une réponse perdue produirait un doublon) : on gère ici, en ne rejouant
    que les cas où Discord n'a manifestement rien enregistré.
    """
    payload = {"content": content}
    for attempt in range(1, attempts + 1):
        try:
            r = requests.post(webhook_url, json=payload, timeout=TIMEOUT)
        except requests.RequestException as e:
            print(f"[!] Échec de l'envoi Discord (tentative {attempt}/{attempts}) : {e}")
        else:
            if r.status_code in (200, 204):
                return True
            print(
                f"[!] Discord a répondu avec le statut {r.status_code} "
                f"(tentative {attempt}/{attempts}) : {r.text[:200]}"
            )
            if r.status_code == 429:
                # Discord indique combien de temps attendre avant de réessayer.
                try:
                    wait = float(r.json().get("retry_after", 5))
                except (ValueError, AttributeError):
                    wait = 5.0
                time.sleep(min(wait, 30))
                continue
            if 400 <= r.status_code < 500:
                # Webhook invalide/supprimé, payload refusé : réessayer n'aidera pas.
                return False
        if attempt < attempts:
            time.sleep(2 * attempt)
    return False


def build_cart_urls(product_url: str, variant_id) -> tuple:
    """
    Construit les "cart permalinks" Shopify pour une variante précise.

    - .../cart/<variant>:<qty>?storefront=true -> ajoute au panier et reste sur
      la boutique (le panier est déjà rempli en un clic depuis Discord)
    - .../cart/<variant>:<qty>                 -> saute directement au checkout

    Retourne (None, None) si on n'a pas d'identifiant de variante (repli HTML,
    site non Shopify) : l'alerte se contentera alors du lien produit.
    """
    if not variant_id:
        return None, None
    parts = urlsplit(product_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    return (
        f"{origin}/cart/{variant_id}:{CART_QTY}?storefront=true",
        f"{origin}/cart/{variant_id}:{CART_QTY}",
    )


def send_discord_alert(
    webhook_url: str,
    site_label: str,
    product_name: str,
    product_url: str,
    variant_id=None,
) -> bool:
    lines = [
        f"{mention_prefix()}🚨 **EN STOCK !**",
        f"{product_name} vient de passer disponible sur **{site_label}** !",
        f"Fiche produit : {product_url}",
    ]
    cart_url, checkout_url = build_cart_urls(product_url, variant_id)
    if cart_url:
        lines.append(f"🛒 **Ajouter au panier en 1 clic** : {cart_url}")
        lines.append(f"⚡ Commander tout de suite : {checkout_url}")
    return post_discord(webhook_url, "\n".join(lines))


def send_discord_new_product(webhook_url: str, site_label: str, title: str, product_url: str) -> bool:
    """Alerte d'apparition : une référence qu'on n'avait jamais vue est mise en ligne."""
    return post_discord(
        webhook_url,
        f"{mention_prefix()}👀 **Nouvelle référence en ligne !**\n"
        f"« {title} » vient d'apparaître sur **{site_label}** (pas encore en stock).\n"
        f"Fiche produit : {product_url}",
    )


# --- Détection de stock (sites Shopify + URL directe) -------------------------

def fetch_shopify_variants(product_url: str):
    """Retourne la liste des variantes d'un produit Shopify, ou None si indéterminé."""
    json_url = product_url + ".json"
    try:
        resp = SESSION.get(json_url, timeout=TIMEOUT)
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

    return variants


def first_available_variant_id(variants):
    """Identifiant de la première variante en stock, pour le lien panier."""
    for variant in variants or []:
        if variant.get("available") is True:
            return variant.get("id")
    return None


def check_stock_via_json(product_url: str):
    variants = fetch_shopify_variants(product_url)
    if variants is None:
        return None
    return any(v.get("available") is True for v in variants)


def check_stock_via_html(product_url: str):
    try:
        resp = SESSION.get(product_url, timeout=TIMEOUT)
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
    """
    Sites Shopify : JSON natif, avec repli HTML si indéterminé.

    Retourne (disponible, variant_id). variant_id vaut None quand le produit
    n'est pas en stock ou quand on a dû passer par le repli HTML : dans ce cas
    l'alerte n'aura pas de lien panier, seulement le lien produit.
    """
    variants = fetch_shopify_variants(product_url)
    if variants is not None:
        variant_id = first_available_variant_id(variants)
        return (variant_id is not None), variant_id

    print("[i] JSON indéterminé, tentative de repli via HTML...")
    return check_stock_via_html(product_url), None


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
        resp = SESSION.get(category_url, timeout=TIMEOUT)
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


def search_shopify_products(site_info: dict):
    """
    Interroge le moteur de recherche Shopify et ne garde que les double packs
    One Piece (la recherche remonte aussi des blisters Pokémon dont le titre
    contient "Double").

    Retourne une liste de dicts {handle, title, url, available}, ou None si la
    vérification n'a pas pu être faite.
    """
    params = {
        "q": site_info["search_query"],
        "resources[type]": "product",
        "resources[limit]": SHOPIFY_SEARCH_LIMIT,
    }
    try:
        resp = SESSION.get(site_info["search_url"], params=params, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"[!] Erreur réseau (recherche Shopify) : {e}")
        return None

    if resp.status_code != 200:
        print(f"[!] Statut HTTP inattendu sur la recherche : {resp.status_code}")
        return None

    try:
        results = resp.json()["resources"]["results"]["products"]
    except (ValueError, KeyError, TypeError):
        print("[!] Réponse de recherche Shopify inexploitable.")
        return None

    base_url = site_info["base_url"].rstrip("/")
    found = []
    for product in results:
        title = product.get("title", "")
        if not (DOUBLEPACK_PATTERN.search(title) and ONEPIECE_PATTERN.search(title)):
            continue
        handle = product.get("handle")
        if not handle:
            continue
        found.append({
            "handle": handle,
            "title": title,
            # On reconstruit l'URL depuis le handle : celle du JSON traîne des
            # paramètres de tracking (_pos, _psq...) qui changent à chaque appel
            # et feraient croire à un changement d'état à chaque run.
            "url": f"{base_url}/products/{handle}",
            "available": bool(product.get("available")),
        })
    return found


# --- Traitement d'un site -----------------------------------------------------

def process_direct_site(site_key: str, site_info: dict, state: dict, webhook_url: str) -> bool:
    """Sites avec fiche produit connue (Shopify / HTML direct). Retourne True si l'état a changé."""
    label = site_info["label"]
    product_url = site_info["product_url"]
    if site_info["mode"] == "shopify":
        available, variant_id = check_stock(product_url)
    else:
        available, variant_id = check_stock_via_html(product_url), None
    previous = state.get(site_key, {}).get("available")

    if available is None:
        print(f"Résultat indéterminé pour {label}, état conservé ({previous}).")
        return False

    print(f"Statut {label} : {'EN STOCK' if available else 'indisponible'}")

    if available and previous is not True:
        print(f">>> Passage en stock détecté sur {label}, envoi de l'alerte Discord.")
        if not send_discord_alert(
            webhook_url, label, site_info["product_name"], product_url, variant_id
        ):
            # Alerte perdue : on ne touche pas à l'état pour la rejouer au prochain run.
            print(f"[!] Alerte {label} non délivrée, état inchangé pour réessayer plus tard.")
            return False

    if available != previous:
        state[site_key] = {"available": available}
        return True
    return False


def process_category_site(site_key: str, site_info: dict, state: dict, webhook_url: str) -> bool:
    """Sites sans fiche produit : on guette l'apparition d'un lien. Retourne True si l'état a changé."""
    label = site_info["label"]
    result = check_category_link(site_info["category_url"], site_info["base_url"])
    previous = state.get(site_key, {}).get("found_url")

    if result is None:
        print(f"Résultat indéterminé pour {label}, état conservé.")
        return False

    if result is False:
        print(f"Statut {label} : produit pas encore listé.")
        if previous is not None:
            state[site_key] = {"found_url": None}
            return True
        return False

    found_url = result
    print(f"Statut {label} : lien détecté -> {found_url}")

    if found_url == previous:
        return False

    print(f">>> Nouveau lien détecté sur {label}, envoi de l'alerte Discord.")
    if not send_discord_alert(webhook_url, label, site_info["product_name"], found_url):
        print(f"[!] Alerte {label} non délivrée, état inchangé pour réessayer plus tard.")
        return False

    state[site_key] = {"found_url": found_url}
    return True


def process_search_site(site_key: str, site_info: dict, state: dict, webhook_url: str) -> bool:
    """
    Boutique Shopify sans fiche produit fixe : on suit toutes les références qui
    correspondent à la recherche, par handle. Deux alertes possibles par produit :
    son apparition dans le catalogue, puis son passage en stock.

    Retourne True si l'état a changé.
    """
    label = site_info["label"]
    products = search_shopify_products(site_info)

    if products is None:
        print(f"Résultat indéterminé pour {label} (double packs), état conservé.")
        return False

    if not products:
        print(f"Statut {label} (double packs) : aucune référence correspondante.")
        return False

    known = state.get(site_key, {}).get("products", {})
    # Premier run : on enregistre la photo du catalogue sans rien notifier,
    # sinon chaque référence déjà en ligne déclencherait une alerte d'apparition.
    baseline = not known
    if baseline:
        print(f"[i] Premier passage sur {label} (double packs) : enregistrement sans alerte.")

    new_known = dict(known)
    changed = False

    for product in products:
        handle = product["handle"]
        previous = known.get(handle)  # True / False / None (jamais vu)
        available = product["available"]
        print(f"  - {product['title'][:60]} : {'EN STOCK' if available else 'rupture'}")

        if baseline:
            new_known[handle] = available
            changed = True
            continue

        if previous is None:
            print(f">>> Nouvelle référence détectée sur {label}, envoi de l'alerte Discord.")
            if not send_discord_new_product(webhook_url, label, product["title"], product["url"]):
                print(f"[!] Alerte {label} non délivrée, référence non enregistrée pour réessayer.")
                continue

        if available and previous is not True:
            print(f">>> Passage en stock détecté sur {label}, envoi de l'alerte Discord.")
            # Le JSON de recherche ne donne pas les identifiants de variantes :
            # on va les chercher sur la fiche produit pour le lien panier.
            variant_id = first_available_variant_id(fetch_shopify_variants(product["url"]))
            if not send_discord_alert(
                webhook_url, label, product["title"], product["url"], variant_id
            ):
                print(f"[!] Alerte {label} non délivrée, état inchangé pour réessayer plus tard.")
                continue

        if previous != available:
            new_known[handle] = available
            changed = True

    if changed:
        state[site_key] = {"products": new_known}
    return changed


# --- Point d'entrée -------------------------------------------------------------

def run_test_alert(webhook_url: str) -> int:
    """
    Mode test : déclenché manuellement depuis GitHub Actions (case à cocher
    "test_alert" sur le bouton Run workflow). Envoie un message Discord
    factice pour vérifier le pipeline complet, sans toucher à l'état réel.
    """
    print("Mode test activé : envoi d'une alerte Discord factice.")
    ok = post_discord(
        webhook_url,
        f"{mention_prefix()}🧪 **Ceci est un message de TEST.**\n"
        "Le pipeline GitHub Actions → Discord fonctionne correctement. "
        "Cette alerte ne signifie PAS que le produit est réellement en stock.",
    )
    if ok:
        print("Message de test envoyé avec succès.")
        return 0
    print("[!] Le message de test n'a pas pu être envoyé.")
    return 1


def main() -> int:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("ERREUR : la variable d'environnement DISCORD_WEBHOOK_URL n'est pas définie.")
        return 1

    if os.environ.get("TEST_ALERT", "").strip().lower() == "true":
        return run_test_alert(webhook_url)

    state = load_state()

    # Purge des sites qui ne sont plus surveillés (ex. un site retiré de SITES),
    # pour que state_stock.json reste le miroir exact de la config.
    obsolete = [key for key in state if key not in SITES]
    for key in obsolete:
        print(f"[i] Purge de l'entrée obsolète '{key}' dans l'état.")
        del state[key]
    state_changed = bool(obsolete)

    failures = 0
    for site_key, site_info in SITES.items():
        print(f"--- Vérification : {site_info['label']} ---")
        try:
            if site_info["mode"] in ("shopify", "html_direct"):
                changed = process_direct_site(site_key, site_info, state, webhook_url)
            elif site_info["mode"] == "shopify_search":
                changed = process_search_site(site_key, site_info, state, webhook_url)
            else:
                changed = process_category_site(site_key, site_info, state, webhook_url)
        except Exception as e:  # noqa: BLE001 - un site cassé ne doit pas bloquer les autres
            failures += 1
            print(f"[!] Erreur inattendue sur {site_info['label']} : {e!r}")
            continue
        state_changed = state_changed or changed

    if state_changed:
        save_state(state)
        print("État mis à jour dans state_stock.json.")
    else:
        print("Pas de changement d'état.")

    # On sort en erreur si un site a planté, pour que le run soit visible en
    # rouge dans GitHub Actions — mais seulement après avoir tout tenté et
    # sauvegardé l'état des sites qui ont fonctionné.
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
