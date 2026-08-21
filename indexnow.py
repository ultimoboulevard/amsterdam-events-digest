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
import datetime
import hashlib
import json
import logging
import re
import secrets
import sqlite3
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
SITE_DIR = BASE_DIR / "site"
KEY_STORE = BASE_DIR / "data" / "indexnow.key"
STATE_DB = BASE_DIR / "data" / "indexnow_state.db"
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

# Quanti giorni-avanti considerare per le pagine-data /events/YYYY-MM-DD/.
# Trenta e' il compromesso: copre il "prossimo mese" che le persone cercano,
# senza gonfiare ogni ping con URL di giorni che nessuno chiede ancora.
DATE_LOOKAHEAD_DAYS = 30


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


def _state_conn() -> sqlite3.Connection:
    """Apre (e crea al primo giro) il DB dove teniamo l'hash dell'ultimo ping."""
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ping_state ("
        "url TEXT PRIMARY KEY, "
        "content_hash TEXT NOT NULL, "
        "last_pinged TEXT NOT NULL)"
    )
    return conn


def _sha256_file(path: Path) -> str:
    """Digest dei byte: cosi' due giri identici non buttano lo stesso URL."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def date_page_candidates() -> list[tuple[str, str]]:
    """
    Le pagine-data per oggi + prossimi 30 giorni, dopo due cancelli che
    contano: (a) l'URL deve stare nella sitemap, (b) l'HTML deve esistere su
    disco. Ritorna coppie (url, hash_corrente) — l'hash lo calcoliamo qui una
    volta sola, cosi' lo riusiamo sia per il diff sia per l'UPSERT post-ping.
    """
    in_sitemap = set(sitemap_urls())
    today = datetime.date.today()
    out: list[tuple[str, str]] = []
    for i in range(DATE_LOOKAHEAD_DAYS + 1):
        d = today + datetime.timedelta(days=i)
        url = f"{BASE_URL}/events/{d.isoformat()}/"
        html = SITE_DIR / "events" / d.isoformat() / "index.html"
        if url not in in_sitemap:
            continue
        if not html.exists():
            # Se manca l'HTML, non ha senso annunciarlo: 404 garantito.
            log.warning("HTML mancante, salto %s", url)
            continue
        out.append((url, _sha256_file(html)))
    return out


def filter_changed(candidates: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """
    Confronta l'hash corrente con quello nel DB. Tiene solo gli URL nuovi o
    cambiati: gli identici li lasciamo in pace, cosi' non facciamo rumore.
    """
    if not candidates:
        return []
    with _state_conn() as conn:
        seen = dict(conn.execute("SELECT url, content_hash FROM ping_state").fetchall())
    return [(u, h) for (u, h) in candidates if seen.get(u) != h]


def record_pings(pairs: list[tuple[str, str]]) -> None:
    """UPSERT dello stato — solo dopo che IndexNow ha risposto 200/202."""
    if not pairs:
        return
    now = datetime.datetime.now(datetime.UTC).isoformat()
    with _state_conn() as conn:
        conn.executemany(
            "INSERT INTO ping_state(url, content_hash, last_pinged) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET "
            "content_hash=excluded.content_hash, last_pinged=excluded.last_pinged",
            [(u, h, now) for u, h in pairs],
        )
        conn.commit()


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

    # --all resta la "bomba idempotente": tutta la sitemap, niente filtri,
    # niente stato. Serve quando si vuole rifare il pieno a mano.
    hot_urls = [BASE_URL + p for p in HOT_PATHS]
    to_record: list[tuple[str, str]] = []
    if args.all:
        urls = sitemap_urls()
    else:
        # Le HOT_PATHS partono sempre, senza passare dal DB: sono viste
        # scorrevoli — "oggi" per definizione cambia ogni giorno.
        to_record = filter_changed(date_page_candidates())
        urls = hot_urls + [u for u, _ in to_record]

    code = submit(urls, dry_run=args.dry_run)

    # Lo stato lo tocchiamo solo se il ping e' andato davvero a buon fine.
    # Su errore, --dry-run o --all lasciamo il DB fermo: cosi' il prossimo
    # giro riprova per davvero.
    if not args.dry_run and not args.all and code in (200, 202):
        record_pings(to_record)

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
