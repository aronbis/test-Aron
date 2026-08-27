#!/usr/bin/env python3
"""
Vérification UNIQUE du stock / de la sortie de produits One Piece Card Game.

Sites et produits surveillés :
- Hikaru Distribution : Premium Card Collection ONE PIECE DAY'26 (Shopify, check JSON + repli HTML)
                         C'est le SEUL produit suivi chez Hikaru : le reste de leur
                         catalogue TCG est jugé trop cher. La surveillance complète du
                         catalogue reste codée et se réactive via MONITOR_SITES
                         (clé "hikaru_one_piece"), voir le commentaire dans SITES.
- King Jouet          : Double Pack OP17 "Les Guerriers les plus puissants au monde",
                         via les données structurées schema.org de la fiche (la page est
                         rendue en JavaScript, le texte visible ne dit rien du stock)
- E.Leclerc           : toute nouvelle sortie One Piece TCG apparaissant au catalogue
- Cultura, Fnac, Smyths : configurés mais INOPÉRANTS depuis GitHub Actions, dont les
                         runners sont sur des plages Azure que ces sites refusent
                         (Cultura et Fnac répondent 403, Smyths sert une page de défi
                         en HTTP 200). Mesuré : un vrai Chromium depuis la CI est bloqué
                         exactement comme requests, alors que les mêmes pages se chargent
                         sans difficulté depuis une IP résidentielle — c'est l'adresse qui
                         est filtrée, pas l'outil. Ces sites ne redeviendraient
                         exploitables qu'en exécutant le moniteur depuis une machine
                         personnelle. Ils sont conservés (une requête chacun) pour
                         reprendre automatiquement si le blocage tombe, et chaque run
                         signale explicitement qu'ils ne sont pas surveillés.

Les alertes distinguent le stock immédiat de l'ouverture des précommandes : sur ces
enseignes, la précommande est souvent la seule vraie fenêtre d'achat.

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
        # Exception assumée au filtre FR/US : cette collector n'existe qu'en
        # édition japonaise, et c'est le produit à l'origine de ce moniteur.
        # Le filtre de langue ne s'applique qu'au balayage de catalogue.
        "mode": "shopify",
        "product_url": (
            "https://hikarudistribution.com/products/"
            "premium-card-collection-one-piece-card-game-one-piece-day-26-edition-limitee-japonais"
        ),
    },
    # DÉSACTIVÉ : le reste du catalogue Hikaru est jugé trop cher, seule la promo
    # ONE PIECE DAY'26 ci-dessus y est suivie. La configuration est conservée
    # telle quelle — il suffit de rajouter "hikaru_one_piece" à MONITOR_SITES
    # dans le workflow pour la réactiver. Son entrée a été retirée de l'état, la
    # réactivation repartira donc d'une photo neuve, sans salve de rattrapage.
    "hikaru_one_piece": {
        "label": "Hikaru Distribution",
        "product_name": "One Piece Card Game",
        # Surveillance de TOUT le catalogue One Piece en français et en anglais/US.
        # Le moteur de recherche Shopify plafonnant à 10 résultats, on balaie
        # products.json page par page en requêtes conditionnelles : voir
        # scan_shopify_catalog.
        "mode": "shopify_catalog",
        "products_url": "https://hikarudistribution.com/products.json",
        "base_url": "https://hikarudistribution.com",
    },
    "king_jouet": {
        "label": "King Jouet",
        "product_name": "Double Pack OP17 - Les Guerriers les plus puissants au monde",
        # La fiche est désormais en ligne (elle renvoyait 404/410 auparavant) mais
        # la page est rendue en JavaScript : le texte visible ne contient pas
        # l'état du stock. On lit donc le bloc schema.org, présent côté serveur.
        # Pas de détection de nouveautés ici : les pages catégorie et la recherche
        # de King Jouet répondent 403 au scraper, seules les fiches passent.
        "mode": "jsonld",
        "product_url": (
            "https://www.king-jouet.com/jeu-jouet/jeux-societes/cartes-a-collectionner/"
            "ref-1034966-cartes-one-piece-double-booster-op17-les-guerriers-les-plus-puissants-au-monde.htm"
        ),
    },
    # ATTENTION - Fnac et Smyths sont protégés par Akamai Bot Manager et ne sont
    # PAS réellement surveillés :
    #   - Fnac   : HTTP 403 sur toutes les pages, y compris l'accueil
    #   - Smyths : HTTP 200 avec une page de défi JavaScript d'1 Ko (le piège :
    #              ça ressemble à un succès, d'où looks_like_bot_challenge)
    # On les garde configurés pour reprendre automatiquement si le blocage tombe,
    # mais chaque run le signale explicitement plutôt que de conclure à tort.
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
        "product_name": "One Piece Card Game",
        # La page catégorie liste les fiches en clair et le code fonctionne, mais
        # Cultura répond 403 aux IP GitHub Actions : inopérant en production, OK
        # si le moniteur tourne un jour depuis une machine personnelle.
        "mode": "listing",
        "category_url": "https://www.cultura.com/cartes-a-jouer/cartes-one-piece.html",
        "base_url": "https://www.cultura.com",
        "product_pattern": r"/p-[a-z0-9\-]+?-(\d+)\.html",
        # Rayon déjà dédié aux cartes One Piece : pas besoin du filtre TCG, qui
        # écarterait des références légitimes. La fiche expose du JSON-LD.
        "tcg_filter": False,
        "check_new_status": True,
    },
    "granderecre": {
        "label": "La Grande Récré",
        "product_name": "One Piece Card Game",
        # Rayon dédié aux cartes à collectionner (toutes licences) : le filtre
        # One Piece du listing suffit à isoler ce qui nous intéresse. Les fiches
        # exposent du JSON-LD, on a donc aussi le stock et le prix.
        "mode": "listing",
        "category_url": "https://www.lagranderecre.fr/cartes-a-collectionner/",
        "base_url": "https://www.lagranderecre.fr",
        "product_pattern": r"/cartes-a-collectionner/([a-z0-9\-]+)\.html",
        "tcg_filter": False,
        "check_new_status": True,
    },
    "orchestra": {
        "label": "Orchestra",
        "product_name": "One Piece Card Game",
        # Enseigne de puériculture, mais qui référence bien quelques boosters
        # One Piece. Recherche généraliste, d'où le filtre TCG pour écarter les
        # vêtements et jouets qui mentionneraient la licence.
        "mode": "listing",
        "category_url": "https://fr.shop-orchestra.com/fr/search?q=one+piece",
        "base_url": "https://fr.shop-orchestra.com",
        "product_pattern": r"-([A-Z0-9]{5,})\.html",
        "tcg_filter": True,
        "check_new_status": True,
    },
    # Micromania (défi Incapsula) et Maison de la Presse (403 Cloudflare) sont
    # inaccessibles au script : non ajoutés, ils n'auraient produit que du bruit.
    "leclerc": {
        "label": "E.Leclerc",
        "product_name": "One Piece Card Game",
        # Seule enseigne de grande distribution qui vende réellement du One Piece
        # TCG et reste accessible au script. Carrefour et Rakuten répondent 403,
        # Cdiscount et Micromania ne servent qu'une coquille JavaScript, et Auchan
        # ne référence que des LEGO et des figurines One Piece — aucun jeu de cartes.
        # La page de résultats liste les produits côté serveur, mais les fiches sont
        # rendues en JavaScript : détection des nouveautés seulement, pas de stock.
        "mode": "listing",
        "category_url": "https://www.e.leclerc/recherche?q=one%20piece%20carte",
        "base_url": "https://www.e.leclerc",
        "product_pattern": r"/fp/[a-z0-9\-]+?-(\d{8,14})",
        "tcg_filter": True,
        "check_new_status": False,
    },
}

# Emplacement de l'état. Surchargeable pour l'exécution locale (launchd sur Mac),
# afin que l'état de la machine perso n'écrase pas celui committé par la CI.
STATE_FILE = Path(
    os.environ.get("MONITOR_STATE_FILE")
    or Path(__file__).resolve().parent / "state_stock.json"
).expanduser()

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

# Formulations typiques des pages de défi anti-bot (Akamai, Imperva, Cloudflare).
# Volontairement spécifiques : un marqueur trop large ferait passer un site qui
# fonctionne pour un site bloqué, et on cesserait de le surveiller sans le voir.
# "captcha" seul est proscrit — E.Leclerc expose une config reCAPTCHA légitime
# dans le JSON de ses pages de résultats.
BOT_CHALLENGE_MARKERS = (
    "pardon our interruption",
    "_incapsula_",
    "incapsula incident",
    "are you a robot",
    "verifying you are human",
    "enable javascript and cookies",
    "checking your browser before",
    "attention required! | cloudflare",
)

# schema.org/availability -> statut interne. Les fiches JSON-LD sont bien plus
# fiables que la recherche de mots-clés : King Jouet et Cultura rendent leur page
# en JavaScript, le texte visible ne contient donc pas l'état réel du stock.
AVAILABILITY_MAP = {
    "instock": "in_stock",
    "limitedavailability": "in_stock",
    "onlineonly": "in_stock",
    "preorder": "preorder",
    "presale": "preorder",
    "backorder": "out_of_stock",
    "outofstock": "out_of_stock",
    "soldout": "out_of_stock",
    "discontinued": "out_of_stock",
}
# Une précommande ouverte est une occasion d'achat : on alerte dessus, avec un
# libellé distinct pour ne pas la confondre avec du stock immédiat.
BUYABLE_STATUSES = ("in_stock", "preorder")
STATUS_LABELS = {
    "in_stock": "EN STOCK",
    "preorder": "PRÉCOMMANDE OUVERTE",
    "out_of_stock": "indisponible",
    "not_online": "fiche pas encore en ligne",
}
STATUS_HEADLINES = {
    "in_stock": "🚨 **EN STOCK !**",
    "preorder": "📦 **PRÉCOMMANDE OUVERTE !**",
}

# Pour la détection d'apparition sur les pages catégorie (Fnac, Smyths, Cultura)
OP17_PATTERN = re.compile(r"op[\s\-]?17\b", re.IGNORECASE)
DOUBLEPACK_PATTERN = re.compile(r"double|duo", re.IGNORECASE)

# Pour filtrer les résultats de la recherche Shopify (Hikaru) : on ne garde que
# les double packs One Piece, la recherche renvoyant aussi des produits Pokémon
# dont le titre contient "Double".
ONEPIECE_PATTERN = re.compile(r"one[\s\-]?piece", re.IGNORECASE)

# Balayage du catalogue Shopify : 250 produits par page, ~19 pages chez Hikaru.
# La borne haute n'est qu'un garde-fou anti-boucle infinie.
CATALOG_PAGE_SIZE = 250
CATALOG_MAX_PAGES = 40

# On ne veut que les éditions françaises et anglaises/US. Les éditions asiatiques
# sont écartées : chez Hikaru la langue est indiquée en clair dans le titre
# ("- Japonais"), parfois seulement dans le handle ou le type ("DISPLAY ONE PIECE JPN").
# Un produit SANS aucun marqueur de langue est conservé : mieux vaut une alerte à
# vérifier qu'un Double Pack OP17 FR raté parce que son titre n'était pas encore
# renseigné correctement.
EXCLUDED_LANG_PATTERN = re.compile(
    r"japonais|japanese|\bjap\b|\bjpn?\b|chinois|chinese|cor[ée]en|korean|\bkor\b|\bkr\b",
    re.IGNORECASE,
)

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
    status: str = "in_stock",
) -> bool:
    verb = "est ouvert à la précommande" if status == "preorder" else "vient de passer disponible"
    lines = [
        f"{mention_prefix()}{STATUS_HEADLINES.get(status, STATUS_HEADLINES['in_stock'])}",
        f"{product_name} {verb} sur **{site_label}** !",
        f"Fiche produit : {product_url}",
    ]
    cart_url, checkout_url = build_cart_urls(product_url, variant_id)
    if cart_url:
        lines.append(f"🛒 **Ajouter au panier en 1 clic** : {cart_url}")
        lines.append(f"⚡ Commander tout de suite : {checkout_url}")
    return post_discord(webhook_url, "\n".join(lines))


def send_discord_new_product(
    webhook_url: str, site_label: str, title: str, product_url: str, status=None
) -> bool:
    """Alerte d'apparition : une référence qu'on n'avait jamais vue est mise en ligne."""
    if status in BUYABLE_STATUSES:
        suffix = f"— **déjà {STATUS_LABELS[status].lower()}**"
    elif status == "out_of_stock":
        suffix = "— pas encore commandable"
    else:
        suffix = ""
    return post_discord(
        webhook_url,
        f"{mention_prefix()}👀 **Nouvelle sortie One Piece TCG !**\n"
        f"« {title} » vient d'apparaître sur **{site_label}** {suffix}\n"
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


def decode_html(resp) -> str:
    """
    Texte de la réponse, en corrigeant le charset.

    Quand l'en-tête Content-Type ne précise pas de charset, requests retombe sur
    ISO-8859-1 comme le veut la RFC. E.Leclerc est dans ce cas alors que ses pages
    sont en UTF-8 : sans ça, "Héritage du Maître" arrive en "HÃ©ritage du MaÃ®tre"
    jusque dans les alertes Discord.

    On force UTF-8 sans passer par resp.apparent_encoding : la détection
    automatique analyse tout le corps, ce qui prend plusieurs dizaines de
    secondes sur les pages de 1 Mo de Cultura et d'E.Leclerc.
    """
    if "charset" not in resp.headers.get("Content-Type", "").lower():
        resp.encoding = "utf-8"
    return resp.text


def looks_like_bot_challenge(resp) -> bool:
    """
    Détecte les pages de protection anti-bot.

    Smyths est le cas dangereux : il répond HTTP 200 avec une page de défi
    JavaScript. Sans cette détection, le scraper conclut "produit pas encore
    listé" et on croit surveiller un site qui ne répond jamais rien.

    La détection se fait sur des formulations, pas sur la taille : Smyths a servi
    tantôt 1 Ko d'Akamai, tantôt 6 Ko d'Imperva ("Pardon Our Interruption").
    """
    if resp.status_code in (403, 429):
        return True
    body = resp.text[:8000].lower()
    if any(marker in body for marker in BOT_CHALLENGE_MARKERS):
        return True
    # Coquille vide : quelques kilo-octets, aucun contenu, juste un script.
    return len(resp.text) < 3000 and "noindex" in body and "<script" in body


def extract_jsonld_product(html: str):
    """
    Lit le bloc schema.org/Product d'une fiche produit.

    Retourne (statut, nom, prix) où statut vaut "in_stock", "preorder",
    "out_of_stock", ou None si la page n'expose pas de données exploitables.
    """
    soup = BeautifulSoup(html, "html.parser")
    for block in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(block.string or "{}")
        except (ValueError, TypeError):
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict) or item.get("@type") != "Product":
                continue
            offers = item.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            raw = str(offers.get("availability", "")).rsplit("/", 1)[-1].lower()
            status = AVAILABILITY_MAP.get(raw)
            if status:
                return status, item.get("name", ""), offers.get("price")
    return None, "", None


def is_excluded_language(*fields) -> bool:
    """
    True si le produit est une édition asiatique (japonaise, chinoise, coréenne).

    On ne surveille que les éditions françaises et anglaises/US. Le test porte
    sur le titre, le handle et le type de produit, la langue n'étant pas toujours
    indiquée au même endroit.
    """
    return bool(EXCLUDED_LANG_PATTERN.search(" ".join(f for f in fields if f)))


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


def check_stock_via_jsonld(product_url: str):
    """
    Statut d'une fiche produit via ses données structurées schema.org.

    Retourne "in_stock" / "preorder" / "out_of_stock", "not_online" si la fiche
    n'existe pas encore, ou None si indéterminé (réseau, blocage, pas de JSON-LD).
    """
    try:
        resp = SESSION.get(product_url, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"[!] Erreur réseau (JSON-LD) : {e}")
        return None

    if resp.status_code in (404, 410):
        print(f"[i] Fiche non disponible (HTTP {resp.status_code}) : produit pas encore en ligne.")
        return "not_online"

    if looks_like_bot_challenge(resp):
        print(f"[!] Bloqué par la protection anti-bot (HTTP {resp.status_code}) : "
              "vérification impossible, état conservé.")
        return None

    if resp.status_code != 200:
        print(f"[!] Statut HTTP inattendu : {resp.status_code}")
        return None

    status, name, price = extract_jsonld_product(decode_html(resp))
    if status:
        detail = f" ({price} €)" if price else ""
        print(f"[i] JSON-LD : {name[:55]}{detail}")
        return status

    print("[i] Pas de JSON-LD exploitable, repli sur la lecture du texte de la page...")
    available = check_stock_via_html(product_url)
    if available is None:
        return None
    return "in_stock" if available else "out_of_stock"


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

    soup = BeautifulSoup(decode_html(resp), "html.parser")
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

    if looks_like_bot_challenge(resp):
        print(f"[!] Page catégorie bloquée par la protection anti-bot (HTTP {resp.status_code}). "
              "Aucune conclusion possible : ce site n'est PAS réellement surveillé.")
        return None

    if resp.status_code != 200:
        print(f"[!] Statut HTTP inattendu sur la page catégorie : {resp.status_code}")
        return None

    soup = BeautifulSoup(decode_html(resp), "html.parser")

    for link in soup.find_all("a", href=True):
        text = link.get_text(" ", strip=True)
        href = link["href"]
        combined = f"{text} {href}".lower()

        if OP17_PATTERN.search(combined) and DOUBLEPACK_PATTERN.search(combined):
            if href.startswith("http"):
                return href
            return base_url.rstrip("/") + "/" + href.lstrip("/")

    return False


# Les vignettes de listing sont préfixées par un badge ("Nouveauté", "Précommande",
# "Meilleure vente") collé au titre : on le retire pour garder un nom lisible.
LISTING_BADGE_PATTERN = re.compile(
    r"^(nouveaut[ée]|pr[ée]commande|meilleure vente|promo|exclusivit[ée]|"
    r"liste de souhaits|aper[çc]u rapide|ajouter à la wishlist)\s*",
    re.IGNORECASE,
)

# Sur une page de résultats généraliste (E.Leclerc), "One Piece" ramène surtout
# des LEGO, figurines et mangas. On exige un marqueur propre au jeu de cartes.
TCG_PATTERN = re.compile(
    r"\b(op|st|eb|dp|prb)[\s\-]?\d{1,2}\b|starter deck|booster|display|"
    r"devil fruit collection|card game|premium card|double pack|"
    r"jeu de cartes à collectionner",
    re.IGNORECASE,
)
# Produits dérivés qui contiennent malgré tout un mot-clé du jeu de cartes
# (« Vivre Card » est un databook, « jeu de 54 cartes » un jeu classique).
NOT_TCG_PATTERN = re.compile(
    r"lego|figurine|peluche|puzzle|t-shirt|tote bag|mug|postale|broch[ée]|"
    r"dvd|blu-ray|manga|vivre card|54 cartes|roman|porte-cl",
    re.IGNORECASE,
)


def is_tcg_product(title: str) -> bool:
    return bool(TCG_PATTERN.search(title)) and not NOT_TCG_PATTERN.search(title)


# Types de produits explicitement écartés du suivi, sur toutes les boutiques.
# C'est un choix de l'utilisateur, pas une limite technique : ces références
# sont bien détectées, on ne veut simplement pas en être alerté.
EXCLUDED_PRODUCT_PATTERN = re.compile(
    r"starter[\s\-]?deck|deck de d[ée]marrage|deck d[ée]butant|beginners? deck|"
    r"\bst[\s\-]?\d{1,2}\b|"
    r"devil[\s\-]?fruit|fruits? du d[ée]mon",
    re.IGNORECASE,
)


def is_wanted_product(title: str) -> bool:
    """False pour les starter decks et les collections Fruits du Démon."""
    return not EXCLUDED_PRODUCT_PATTERN.search(title)


# Le texte d'une vignette agrège titre, marque, disponibilité et nombre d'avis :
# "Booster One Piece - OP09 - Asmodee Asmodee (4) INDISPONIBLE EN LIGNE en stock
# à indisponible à 5,99 €". On coupe à la première mention de vendeur ou de
# disponibilité pour que l'alerte Discord reste lisible.
LISTING_NOISE_PATTERN = re.compile(
    r"\s*(?:indisponible en ligne|disponible en ligne|en stock|vendeur partenaire|"
    r"vendeur par|vendu et exp[ée]di[ée]|vendu par)\b.*$",
    re.IGNORECASE,
)
REPEATED_WORD_PATTERN = re.compile(r"\b(\w+)( \1\b)+", re.IGNORECASE)


def clean_listing_title(raw: str) -> str:
    title = re.sub(r"\s+", " ", raw).strip()
    # Plusieurs badges peuvent s'enchaîner ("Liste de souhaits Aperçu rapide ...").
    for _ in range(4):
        nettoye = LISTING_BADGE_PATTERN.sub("", title)
        if nettoye == title:
            break
        title = nettoye
    title = LISTING_NOISE_PATTERN.sub("", title)
    title = REPEATED_WORD_PATTERN.sub(r"\1", title)       # "Asmodee Asmodee"
    title = re.sub(r"\s*\(\d+\)\s*$", "", title)          # nombre d'avis en fin
    return re.sub(r"[\s\-–—:]+$", "", title).strip()


def list_category_products(site_info: dict):
    """
    Liste les produits d'une page catégorie ou de résultats, pour repérer toute
    nouvelle sortie One Piece TCG dès son apparition au catalogue.

    Le motif d'URL des fiches est propre à chaque site et vient de sa config
    ("product_pattern", dont le premier groupe capture l'identifiant produit).

    Retourne un dict id -> {title, url}, ou None si la page n'a pas pu être lue.
    """
    base_url = site_info["base_url"].rstrip("/")
    pattern = re.compile(site_info["product_pattern"], re.IGNORECASE)
    tcg_only = site_info.get("tcg_filter", False)
    try:
        resp = SESSION.get(site_info["category_url"], timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"[!] Erreur réseau (listing catégorie) : {e}")
        return None

    if looks_like_bot_challenge(resp):
        print(f"[!] Listing bloqué par la protection anti-bot (HTTP {resp.status_code}).")
        return None

    if resp.status_code != 200:
        print(f"[!] Statut HTTP inattendu sur le listing : {resp.status_code}")
        return None

    soup = BeautifulSoup(decode_html(resp), "html.parser")
    products = {}
    for link in soup.find_all("a", href=True):
        match = pattern.search(link["href"])
        if not match:
            continue
        title = clean_listing_title(link.get_text(" ", strip=True))
        if not title or not ONEPIECE_PATTERN.search(title):
            continue
        if is_excluded_language(title) or (tcg_only and not is_tcg_product(title)):
            continue
        if not is_wanted_product(title):
            continue
        href = link["href"]
        products.setdefault(match.group(1), {
            "title": title[:120],
            "url": href if href.startswith("http") else f"{base_url}/{href.lstrip('/')}",
        })
    return products


def fetch_catalog_page(products_url: str, page: int, etag):
    """
    Récupère une page de products.json en requête conditionnelle.

    Retourne (statut, produits, etag) où statut vaut :
      - "unchanged" : le serveur a répondu 304, la page n'a pas bougé (0 octet)
      - "ok"        : page téléchargée, `produits` contient la liste brute
      - "error"     : échec réseau / HTTP
    """
    headers = {"If-None-Match": etag} if etag else {}
    params = {"limit": CATALOG_PAGE_SIZE, "page": page}
    try:
        resp = SESSION.get(products_url, params=params, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"[!] Erreur réseau (catalogue page {page}) : {e}")
        return "error", None, etag

    if resp.status_code == 304:
        return "unchanged", None, etag

    if resp.status_code != 200:
        print(f"[!] Statut HTTP inattendu sur le catalogue page {page} : {resp.status_code}")
        return "error", None, etag

    try:
        products = resp.json()["products"]
    except (ValueError, KeyError, TypeError):
        print(f"[!] Page catalogue {page} inexploitable.")
        return "error", None, etag

    return "ok", products, resp.headers.get("ETag", "")


def scan_shopify_catalog(site_info: dict, etags: dict, known_pages: int = 0):
    """
    Balaie tout le catalogue Shopify et retourne les produits One Piece FR/US.

    Le moteur de recherche Shopify plafonne à 10 résultats, insuffisant pour
    suivre l'ensemble du catalogue One Piece. On pagine donc products.json, mais
    en requêtes conditionnelles (If-None-Match) : une page inchangée répond 304
    sans corps, ce qui rend un balayage complet quasi gratuit alors qu'un
    téléchargement intégral pèse ~12 Mo.

    `known_pages` est le rang de la première page vide observée au run précédent.
    Sans lui, la page terminale répondant 304 comme les autres, on ne saurait pas
    où s'arrêter : le balayage avançait d'une page à chaque run et accumulait un
    ETag de plus, ce qui provoquait aussi un commit d'état à chaque exécution.

    Retourne (produits, nouveaux_etags, complet, pages) :
      - produits : dict handle -> {title, url, available, variant_id}, limité aux
        pages réellement téléchargées ; l'appelant fusionne avec l'état connu
      - complet  : True si toutes les pages ont été relues (aucun 304), auquel cas
        l'appelant peut remplacer l'état au lieu de le fusionner
      - pages    : rang de la première page vide, à repasser au run suivant
    Retourne (None, etags, False, known_pages) si le balayage a échoué.
    """
    base_url = site_info["base_url"].rstrip("/")
    products = {}
    new_etags = {}
    complete = True
    pages = known_pages

    for page in range(1, CATALOG_MAX_PAGES + 1):
        key = str(page)
        status, raw, etag = fetch_catalog_page(site_info["products_url"], page, etags.get(key))

        if status == "error":
            # Une page manquante fausserait la comparaison (produits vus comme
            # disparus) : on abandonne le run plutôt que d'alerter à tort.
            return None, etags, False, known_pages

        if status == "unchanged":
            complete = False
            new_etags[key] = etags.get(key)
            if known_pages and page >= known_pages:
                break  # la page terminale n'a pas bougé : fin du catalogue
            continue

        new_etags[key] = etag
        if not raw:
            pages = page
            break  # page vide : fin du catalogue

        for product in raw:
            title = product.get("title", "")
            handle = product.get("handle")
            if not handle or not ONEPIECE_PATTERN.search(title):
                continue
            if is_excluded_language(title, handle, product.get("product_type", "")):
                continue
            if not is_wanted_product(title):
                continue
            variants = product.get("variants", [])
            products[handle] = {
                "title": title,
                "url": f"{base_url}/products/{handle}",
                "available": any(v.get("available") for v in variants),
                "variant_id": first_available_variant_id(variants),
            }

    return products, new_etags, complete, pages


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


def process_jsonld_site(site_key: str, site_info: dict, state: dict, webhook_url: str) -> bool:
    """
    Fiche produit lue via ses données structurées (King Jouet, Cultura).
    Alerte sur le stock ET sur l'ouverture des précommandes.
    """
    label = site_info["label"]
    product_url = site_info["product_url"]
    status = check_stock_via_jsonld(product_url)
    previous = state.get(site_key, {}).get("status")

    if status is None:
        print(f"Résultat indéterminé pour {label}, état conservé ({previous}).")
        return False

    print(f"Statut {label} : {STATUS_LABELS.get(status, status)}")

    if status in BUYABLE_STATUSES and status != previous:
        print(f">>> Disponibilité détectée sur {label}, envoi de l'alerte Discord.")
        if not send_discord_alert(
            webhook_url, label, site_info["product_name"], product_url, status=status
        ):
            print(f"[!] Alerte {label} non délivrée, état inchangé pour réessayer plus tard.")
            return False

    if status != previous:
        state[site_key] = {"status": status}
        return True
    return False


def process_listing_site(site_key: str, site_info: dict, state: dict, webhook_url: str) -> bool:
    """
    Page catégorie listant des produits (Cultura) : on alerte à l'apparition de
    toute nouvelle sortie One Piece TCG, en allant chercher son statut sur sa
    fiche pour préciser si elle est déjà commandable.
    """
    label = site_info["label"]
    products = list_category_products(site_info)

    if products is None:
        print(f"Résultat indéterminé pour {label}, état conservé.")
        return False

    known = state.get(site_key, {}).get("seen", {})
    baseline = not known
    if baseline:
        print(f"[i] Premier passage sur {label} : {len(products)} références "
              "enregistrées sans alerte.")
        state[site_key] = {"seen": {pid: p["title"] for pid, p in products.items()}}
        return True

    # Références enregistrées avant l'exclusion des starter decks et des Fruits
    # du Démon : on les retire pour que l'état reflète le filtre en vigueur.
    ecartes = {pid for pid, titre in known.items() if not is_wanted_product(titre)}
    if ecartes:
        print(f"[i] {len(ecartes)} référence(s) désormais exclues, retirées de l'état.")
        known = {pid: titre for pid, titre in known.items() if pid not in ecartes}

    new_seen = dict(known)
    changed = bool(ecartes)
    for pid, product in sorted(products.items()):
        if pid in known:
            continue
        print(f">>> Nouvelle sortie sur {label} : {product['title'][:60]}")
        # Une seule requête supplémentaire, et seulement pour une nouveauté :
        # le listing ne dit pas si le produit est déjà commandable. Inutile chez
        # les sites dont la fiche est rendue en JavaScript (E.Leclerc).
        status = check_stock_via_jsonld(product["url"]) if site_info.get("check_new_status") else None
        if not send_discord_new_product(
            webhook_url, label, product["title"], product["url"], status=status
        ):
            print(f"[!] Alerte {label} non délivrée, référence non enregistrée pour réessayer.")
            continue
        new_seen[pid] = product["title"]
        changed = True

    if changed:
        state[site_key] = {"seen": new_seen}
    else:
        print(f"Statut {label} : aucune nouvelle sortie ({len(products)} références suivies).")
    return changed


def process_catalog_site(site_key: str, site_info: dict, state: dict, webhook_url: str) -> bool:
    """
    Boutique Shopify suivie dans son ensemble : on pistes toutes les références
    One Piece FR/US par handle. Deux alertes possibles par produit : son
    apparition au catalogue, puis son passage en stock.

    Retourne True si l'état a changé.
    """
    label = site_info["label"]
    site_state = state.get(site_key, {})
    known = site_state.get("products", {})
    etags = site_state.get("etags", {})
    known_pages = site_state.get("pages", 0)

    # États écrits avant l'introduction de "pages" : ils contiennent des ETags de
    # pages vides accumulés run après run. On repart d'un balayage complet une
    # fois, pour ne pas figer une pagination trop longue.
    if etags and not known_pages:
        print("[i] État de pagination absent (ancien format) : balayage complet de remise à plat.")
        etags = {}

    products, new_etags, complete, pages = scan_shopify_catalog(site_info, etags, known_pages)

    if products is None:
        print(f"Balayage incomplet pour {label}, état conservé.")
        return False

    if not products and not complete:
        print(f"Statut {label} : aucune page modifiée depuis le dernier run.")
        # Les ETags peuvent quand même avoir bougé (page ajoutée en fin de
        # catalogue) : on les enregistre pour ne pas retélécharger inutilement.
        if new_etags != etags or pages != known_pages:
            state[site_key] = {"products": known, "etags": new_etags, "pages": pages}
            return True
        return False

    # Premier run : on enregistre la photo du catalogue sans rien notifier,
    # sinon les dizaines de références déjà en ligne déclencheraient une salve.
    baseline = not known
    if baseline:
        print(f"[i] Premier passage sur {label} : {len(products)} références "
              f"One Piece FR/US enregistrées sans alerte.")

    # Un balayage complet fait autorité : on repart de la photo fraîche, ce qui
    # purge les produits retirés du catalogue. Sinon on fusionne, les pages
    # inchangées (304) n'ayant pas été retéléchargées.
    new_known = dict(products_availability(products)) if complete else dict(known)
    changed = complete and new_known != known

    for handle, product in sorted(products.items()):
        previous = known.get(handle)  # True / False / None (jamais vu)
        available = product["available"]

        if baseline:
            new_known[handle] = available
            changed = True
            continue

        if previous is None:
            print(f">>> Nouvelle référence sur {label} : {product['title'][:60]}")
            if not send_discord_new_product(webhook_url, label, product["title"], product["url"]):
                print(f"[!] Alerte {label} non délivrée, référence non enregistrée pour réessayer.")
                new_known.pop(handle, None)
                continue

        if available and previous is not True:
            print(f">>> Passage en stock sur {label} : {product['title'][:60]}")
            if not send_discord_alert(
                webhook_url, label, product["title"], product["url"], product["variant_id"]
            ):
                print(f"[!] Alerte {label} non délivrée, état inchangé pour réessayer plus tard.")
                # On enregistre la référence comme indisponible : l'alerte
                # d'apparition ci-dessus est déjà partie (inutile de la répéter),
                # mais celle du stock sera rejouée au prochain run.
                new_known[handle] = False
                changed = True
                continue

        if previous != available:
            new_known[handle] = available
            changed = True

    if changed or new_etags != etags or pages != known_pages:
        state[site_key] = {"products": new_known, "etags": new_etags, "pages": pages}
        return True
    return False


def products_availability(products: dict) -> dict:
    """Réduit la photo du catalogue à ce qu'on conserve dans l'état : handle -> dispo."""
    return {handle: product["available"] for handle, product in products.items()}


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


def selected_sites() -> dict:
    """
    Sites à vérifier sur cette machine.

    MONITOR_SITES permet de répartir la surveillance entre GitHub Actions et une
    machine perso : Cultura et la Fnac refusent les IP des runners GitHub, mais
    répondent depuis une connexion résidentielle. Sans ce partage, les deux
    environnements alerteraient en double sur les sites communs.

    Exemple : MONITOR_SITES="cultura,fnac"
    """
    demandes = os.environ.get("MONITOR_SITES", "").strip()
    if not demandes:
        return SITES
    voulus = [k.strip() for k in demandes.split(",") if k.strip()]
    inconnus = [k for k in voulus if k not in SITES]
    if inconnus:
        print(f"[!] MONITOR_SITES : clés inconnues ignorées {inconnus} "
              f"(disponibles : {', '.join(SITES)})")
    return {k: SITES[k] for k in voulus if k in SITES}


def collect_available(sites: dict):
    """
    Inventaire ponctuel : tout le One Piece TCG commandable à cet instant.

    Retourne (trouvés, non_vérifiables) où chaque trouvé est un dict
    {label, title, url, cart_url, status}. Les sites dont on ne peut pas établir
    le stock (bloqués, ou fiche rendue en JavaScript) sont listés à part : mieux
    vaut dire qu'on ne sait pas que laisser croire à un inventaire exhaustif.
    """
    trouves, inconnus = [], []

    for site_key, site_info in sites.items():
        label = site_info["label"]
        mode = site_info["mode"]
        print(f"--- Inventaire : {label} ({site_key}) ---")

        if mode == "shopify":
            available, variant_id = check_stock(site_info["product_url"])
            if available is None:
                inconnus.append(f"{label} — vérification impossible")
            elif available:
                cart, _ = build_cart_urls(site_info["product_url"], variant_id)
                trouves.append({"label": label, "title": site_info["product_name"],
                                "url": site_info["product_url"], "cart_url": cart,
                                "status": "in_stock"})

        elif mode == "shopify_catalog":
            produits, _, _, _ = scan_shopify_catalog(site_info, {}, 0)
            if produits is None:
                inconnus.append(f"{label} — balayage du catalogue impossible")
                continue
            for produit in produits.values():
                if produit["available"]:
                    cart, _ = build_cart_urls(produit["url"], produit["variant_id"])
                    trouves.append({"label": label, "title": produit["title"],
                                    "url": produit["url"], "cart_url": cart,
                                    "status": "in_stock"})

        elif mode == "jsonld":
            status = check_stock_via_jsonld(site_info["product_url"])
            if status is None:
                inconnus.append(f"{label} — vérification impossible")
            elif status in BUYABLE_STATUSES:
                trouves.append({"label": label, "title": site_info["product_name"],
                                "url": site_info["product_url"], "cart_url": None,
                                "status": status})

        elif mode == "listing":
            produits = list_category_products(site_info)
            if produits is None:
                inconnus.append(f"{label} — listing inaccessible")
                continue
            if not site_info.get("check_new_status"):
                inconnus.append(f"{label} — {len(produits)} références listées, "
                                "mais le stock n'est pas lisible (fiches en JavaScript)")
                continue
            for produit in produits.values():
                status = check_stock_via_jsonld(produit["url"])
                if status in BUYABLE_STATUSES:
                    trouves.append({"label": label, "title": produit["title"],
                                    "url": produit["url"], "cart_url": None,
                                    "status": status})

        else:  # category : Fnac, Smyths
            inconnus.append(f"{label} — bloqué par sa protection anti-bot")

    return trouves, inconnus


def format_report(trouves: list, inconnus: list) -> list:
    """Met l'inventaire en messages Discord de moins de 2000 caractères."""
    lignes = [f"{mention_prefix()}📋 **Inventaire One Piece TCG — "
              f"{len(trouves)} produit(s) commandable(s)**"]
    for item in sorted(trouves, key=lambda x: (x["label"], x["title"])):
        etiquette = "🚨 en stock" if item["status"] == "in_stock" else "📦 précommande"
        lignes.append(f"\n**{item['title'][:90]}**\n{etiquette} — {item['label']}\n{item['url']}")
        if item["cart_url"]:
            lignes.append(f"🛒 Panier en 1 clic : {item['cart_url']}")
    if not trouves:
        lignes.append("\nAucun produit commandable pour l'instant.")
    if inconnus:
        lignes.append("\n⚠️ **Non vérifiable :**")
        lignes.extend(f"• {raison}" for raison in inconnus)

    messages, courant = [], ""
    for ligne in lignes:
        if len(courant) + len(ligne) + 1 > 1900:
            messages.append(courant)
            courant = ligne
        else:
            courant = f"{courant}\n{ligne}" if courant else ligne
    if courant:
        messages.append(courant)
    return messages


def run_report(webhook_url: str, sites: dict) -> int:
    trouves, inconnus = collect_available(sites)
    messages = format_report(trouves, inconnus)
    print(f"\n{len(trouves)} produit(s) commandable(s), "
          f"{len(inconnus)} site(s) non vérifiable(s), {len(messages)} message(s).")
    for numero, message in enumerate(messages, 1):
        if not post_discord(webhook_url, message):
            print(f"[!] Envoi du message {numero}/{len(messages)} échoué.")
            return 1
        print(f"Message {numero}/{len(messages)} envoyé.")
    return 0


def main() -> int:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("ERREUR : la variable d'environnement DISCORD_WEBHOOK_URL n'est pas définie.")
        return 1

    if os.environ.get("TEST_ALERT", "").strip().lower() == "true":
        return run_test_alert(webhook_url)

    sites = selected_sites()
    if not sites:
        print("ERREUR : MONITOR_SITES ne correspond à aucun site connu.")
        return 1

    # Inventaire à la demande : envoie la liste de tout ce qui est commandable
    # maintenant, sans rien changer à l'état ni au suivi des alertes.
    if os.environ.get("REPORT", "").strip().lower() == "true":
        return run_report(webhook_url, sites)

    state = load_state()

    # Purge des sites qui ne sont plus surveillés (ex. un site retiré de SITES),
    # pour que state_stock.json reste le miroir exact de la config.
    obsolete = [key for key in state if key not in SITES]
    for key in obsolete:
        print(f"[i] Purge de l'entrée obsolète '{key}' dans l'état.")
        del state[key]
    state_changed = bool(obsolete)

    failures = 0
    for site_key, site_info in sites.items():
        print(f"--- Vérification : {site_info['label']} ---")
        try:
            mode = site_info["mode"]
            if mode in ("shopify", "html_direct"):
                changed = process_direct_site(site_key, site_info, state, webhook_url)
            elif mode == "shopify_catalog":
                changed = process_catalog_site(site_key, site_info, state, webhook_url)
            elif mode == "jsonld":
                changed = process_jsonld_site(site_key, site_info, state, webhook_url)
            elif mode == "listing":
                changed = process_listing_site(site_key, site_info, state, webhook_url)
            else:
                changed = process_category_site(site_key, site_info, state, webhook_url)
        except Exception as e:  # noqa: BLE001 - un site cassé ne doit pas bloquer les autres
            failures += 1
            print(f"[!] Erreur inattendue sur {site_info['label']} : {e!r}")
            continue
        state_changed = state_changed or changed

    if state_changed:
        save_state(state)
        print(f"État mis à jour dans {STATE_FILE}.")
    else:
        print("Pas de changement d'état.")

    # On sort en erreur si un site a planté, pour que le run soit visible en
    # rouge dans GitHub Actions — mais seulement après avoir tout tenté et
    # sauvegardé l'état des sites qui ont fonctionné.
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
