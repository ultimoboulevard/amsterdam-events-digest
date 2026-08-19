#!/usr/bin/env python3
"""
Annuncia gli URL a IndexNow (Bing, Yandex, Seznam, Naver — non Google).

PERCHE'
Il sito e' uscito da Cloudflare Access il 18/08/2026 e le 147 pagine sotto
/events/ esistono da poche ore: nessun crawler sa che ci sono. La sitemap e'
dichiarata in robots.txt, ma aspettare che qualcuno passi a leggerla sono
settimane. IndexNow e' il contrario: siamo noi a bussare.

Google NON partecipa a IndexNow. Per Google serve Search Console, e quello
deve farlo Francesco: richiede il login al suo account.

COME FUNZIONA
Il protocollo prova che il dominio e' nostro con un file: la chiave sta a
https://events.zenobj.net/<chiave>.txt e contiene la chiave stessa. Il motore
la scarica e la confronta con quella nel payload. Non e' un segreto — deve
essere pubblica per forza — quindi puo' stare tranquillamente nel repo.

    ./.venv/bin/python indexnow.py            # gli URL "caldi" (default)
    ./.venv/bin/python indexnow.py --all      # tutta la sitemap
    ./.venv/bin/python indexnow.py --dry-run  # stampa e basta, non manda niente
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import secrets
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
SITE_DIR = BASE_DIR / "site"
KEY_STORE = BASE_DIR / "data" / "indexnow.key"
SITEMAP = SITE_DIR / "sitemap.xml"

HOST = "events.zenobj.net"
BASE_URL = f"https://{HOST}"
# Endpoint condiviso: inoltra a tutti i motori aderenti, invece di doverli
# interrogare uno per uno.
ENDPOINT = "https://api.indexnow.org/indexnow"
TIMEOUT = 20

# Le pagine che cambiano DAVVERO ogni giorno. Le pagine-data no: una volta
# generata, /events/2026-10-23/ cambia solo se cambia il cartellone di quel
# giorno. Ributtare tutte e 148 le URL ogni mattina sarebbe rumore, e i motori
# lo trattano come tale.
HOT_PATHS = [
    "/",
    "/events/",
    "/events/today/",
    "/events/tonight/",
    "/events/tomorrow/",
    "/events/this-weekend/",
    "/events/next-weekend/",
    "/events/this-week/",
    "/events/next-week/",
]


def get_or_create_key() -> str:
    """
    Ritorna la chiave, generandola al primo giro.

    Vive in due posti apposta: data/indexnow.key e' la copia canonica (non
    servita), site/<chiave>.txt e' quella che i motori scaricano. Se il secondo
    sparisce — per esempio ripulendo site/ — viene riscritto da solo.
    """
    KEY_STORE.parent.mkdir(parents=True, exist_ok=True)
    if KEY_STORE.exists():
        key = KEY_STORE.read_text(encoding="utf-8").strip()
    else:
        key = secrets.token_hex(16)
        KEY_STORE.write_text(key, encoding="utf-8")
        log.info("Generata una chiave IndexNow nuova: %s", key)

    key_file = SITE_DIR / f"{key}.txt"
    if not key_file.exists():
        key_file.write_text(key, encoding="utf-8")
        log.info("Scritta la chiave pubblica in %s", key_file)
    return key


def sitemap_urls() -> list[str]:
    """Gli URL dichiarati nella sitemap, che e' gia' la lista autorevole."""
    if not SITEMAP.exists():
        return []
    return re.findall(r"<loc>([^<]+)</loc>", SITEMAP.read_text(encoding="utf-8"))


def submit(urls: list[str], dry_run: bool = False) -> int:
    """
    Manda la lista a IndexNow. Ritorna il codice HTTP (0 se non ha mandato).

    Non solleva mai: e' un annuncio, non un passo della pipeline. Se Bing e'
    giu' o la rete non va, il sito e' comunque a posto e domani si riprova.
    """
    urls = [u for u in urls if u.startswith(BASE_URL)]
    if not urls:
        log.warning("Nessun URL da annunciare.")
        return 0

    key = get_or_create_key()
    payload = {
        "host": HOST,
        "key": key,
        "keyLocation": f"{BASE_URL}/{key}.txt",
        "urlList": urls,
    }

    if dry_run:
        print(json.dumps({**payload, "urlList": urls[:5] + ["…"]}, indent=2))
        print(f"\n[dry run] {len(urls)} URL, nessuna richiesta inviata.")
        return 0

    # httpx e non urllib: e' gia' la dipendenza con cui parlano gli 11
    # collector, e si porta dietro i certificati di certifi. urllib usa il
    # keychain di sistema, che su questo Python (framework, installato a mano)
    # e' vuoto: il primo tentativo mori' con CERTIFICATE_VERIFY_FAILED.
    try:
        resp = httpx.post(
            ENDPOINT, json=payload, timeout=TIMEOUT,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        if resp.status_code in (200, 202):
            log.info("IndexNow ha risposto %s per %d URL.", resp.status_code, len(urls))
        else:
            # 403 = chiave non verificata; 422 = URL fuori dall'host dichiarato.
            log.error("IndexNow ha rifiutato (%s): %s", resp.status_code, resp.text[:200])
        return resp.status_code
    except httpx.HTTPError as e:
        log.error("IndexNow non raggiungibile: %s", e)
        return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--all", action="store_true", help="tutti gli URL della sitemap")
    ap.add_argument("--dry-run", action="store_true", help="stampa il payload, non invia")
    args = ap.parse_args()

    urls = sitemap_urls() if args.all else [BASE_URL + p for p in HOT_PATHS]
    code = submit(urls, dry_run=args.dry_run)
    if not args.dry_run:
        if code in (200, 202):
            print(f"\n✅ IndexNow: HTTP {code} · {len(urls)} URL annunciati")
            if code == 202:
                print("   202 = accettato, chiave in verifica. Normale al primo invio.")
        else:
            # Prima diceva "148 URL annunciati" anche quando non era partito
            # niente: un errore di rete stampava lo stesso numero di un successo.
            print(f"\n⚠️ IndexNow: NON inviato (HTTP {code}) · {len(urls)} URL ancora da annunciare")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(name)-12s │ %(levelname)-5s │ %(message)s",
        datefmt="%H:%M:%S",
    )
    main()
