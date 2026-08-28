#!/usr/bin/env python3
"""
Scarica da Google Search Console cosa il sito ottiene davvero, e lo tiene.

PERCHE'
Nel repo non esisteva nessuna misura di posizionamento: si sapeva quante
pagine si generano, non per quali frasi qualcuno le trova. Ogni scelta di
discoverability — quali pagine costruire, quali titoli scrivere — era quindi
una deduzione. Questo modulo la sostituisce con un dato.

IndexNow (indexnow.py) parla a Bing e Yandex, non a Google: Search Console e'
l'unico canale verso Google, e la sua API l'unico modo di leggerlo senza
esportare CSV a mano.

PERCHE' TENERE UNA COPIA LOCALE
Search Console conserva 16 mesi mobili: quello che oggi c'e', fra 16 mesi non
c'e' piu'. data/search_console.db non scade. E il periodo che servira'
riguardare e' proprio questo, subito dopo i cambi del 19/08/2026 alla home:
un "prima e dopo" esiste solo se qualcuno stava registrando.

COSA FA E COSA NON FA
Legge, accumula, scrive logs/search_console.md. Non tocca il sito, non cambia
niente, non propone codice — come diagnose.py, produce un rapporto.

CONFIGURAZIONE
Serve un service account (non OAuth interattivo: questo gira alle 06:00 senza
nessuno davanti). Due passi, una volta sola:

  1. Google Cloud → abilita "Google Search Console API" → crea un service
     account → scarica la chiave JSON.
  2. Search Console → Impostazioni → Utenti e autorizzazioni → aggiungi
     l'email del service account. Basta "Limitato": qui si legge soltanto.

Poi in .env:

     GOOGLE_SC_KEY="/percorso/della/chiave.json"

Finche' la chiave non c'e', questo script NON e' un errore: dice che non e'
configurato ed esce 0. E' agganciato alla pipeline e non deve fermarla.

    ./.venv/bin/python search_console.py              # scarica + rapporto
    ./.venv/bin/python search_console.py --report     # solo rapporto, dal DB
    ./.venv/bin/python search_console.py --inspect 0  # niente ispezione URL
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "search_console.db"
EVENTS_DB = BASE_DIR / "data" / "events.db"
SITEMAP = BASE_DIR / "site" / "sitemap.xml"
REPORT_PATH = BASE_DIR / "logs" / "search_console.md"
ENV_FILE = BASE_DIR / ".env"

# La proprieta' e' di tipo Domain: l'identificativo non e' un URL ma
# "sc-domain:<dominio>". Con una URL-prefix sarebbe "https://events.zenobj.net/",
# e le due NON sono intercambiabili — l'API risponde 403 su quella sbagliata.
SITE_URL = "sc-domain:zenobj.net"
# ...e siccome la proprieta' Domain copre TUTTO zenobj.net, senza questo filtro
# i numeri arriverebbero mescolati con oura, carta, playlist e il gateway
# Alexa, che sono personali e non c'entrano niente.
PAGE_CONTAINS = "events.zenobj.net"

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://searchconsole.googleapis.com"
TIMEOUT = 60

# Search Console consolida i dati con qualche giorno di ritardo. Si chiede
# dataState="final" e non si va oltre questa soglia: registrare numeri ancora
# in movimento vorrebbe dire tenersi in casa una copia che diverge da Google.
LAG_DAYS = 3
# Quanto indietro al primo giro: 16 mesi e' tutto quello che l'API concede.
BACKFILL_DAYS = 490
# Il massimo per richiesta ammesso dall'API. Oltre si pagina con startRow.
ROW_LIMIT = 25000

# La finestra su cui si ragiona nel rapporto.
WINDOW_DAYS = 28
# "Occasioni": esci gia', ma sotto la piega. Sopra la 20esima posizione non e'
# un'occasione, e' un altro lavoro; sotto la quinta ci sei gia'.
OPPORTUNITY_BAND = (5.0, 20.0)
OPPORTUNITY_MIN_IMPRESSIONS = 10
# Quanti URL ispezionare per giro. La quota e' 2.000 al giorno, quindi ci
# starebbero tutti e 231, ma a rotazione bastano: lo stato di indicizzazione
# di una pagina-data non cambia ogni mattina, e il giro resta corto.
INSPECT_PER_RUN = 20


# ── Stato locale ───────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS search_analytics (
    day         TEXT NOT NULL,
    query       TEXT NOT NULL,
    page        TEXT NOT NULL,
    clicks      INTEGER NOT NULL,
    impressions INTEGER NOT NULL,
    ctr         REAL NOT NULL,
    position    REAL NOT NULL,
    PRIMARY KEY (day, query, page)
);
CREATE INDEX IF NOT EXISTS idx_sa_day ON search_analytics(day);
CREATE INDEX IF NOT EXISTS idx_sa_query ON search_analytics(query);

CREATE TABLE IF NOT EXISTS url_inspection (
    url              TEXT PRIMARY KEY,
    checked_at       TEXT,
    verdict          TEXT,
    coverage         TEXT,
    google_canonical TEXT,
    user_canonical   TEXT,
    last_crawl       TEXT,
    robots           TEXT
);
"""


@contextmanager
def _connection(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    """Stessa forma di db.py: transazione E chiusura, sempre."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with _connection() as conn:
        conn.executescript(SCHEMA)


# ── Autenticazione ─────────────────────────────────────────────────
def _from_env_file(name: str) -> str:
    """
    Ripiego per quando si lancia a mano.

    run_pipeline.sh fa `. .env` e poi esporta: dentro la pipeline la variabile
    c'e'. Dal terminale no — e senza questo ripiego lo script direbbe "non
    configurato" con la chiave regolarmente al suo posto. E' esattamente il
    guasto silenzioso di cui avverte il commento in testa a run_pipeline.sh,
    solo al contrario: li' la variabile manca al processo, qui manca alla shell.
    """
    try:
        text = ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(rf'^\s*(?:export\s+)?{re.escape(name)}\s*=\s*(.+?)\s*$', text, re.M)
    return m.group(1).strip().strip('"').strip("'") if m else ""


def _raise_with_body(resp: httpx.Response) -> None:
    """
    raise_for_status() dice solo il codice. Google mette il motivo nel corpo
    ("invalid_grant", "SERVICE_DISABLED", "User does not have sufficient
    permission for site..."), ed e' l'unica cosa che serve per capire quale
    dei passi di configurazione manca. Senza, un 403 e un 400 si assomigliano.
    """
    if resp.is_success:
        return
    raise httpx.HTTPStatusError(
        f"{resp.status_code} da {resp.request.url} — {resp.text[:400]}",
        request=resp.request, response=resp,
    )


def _key_path() -> Path | None:
    raw = os.getenv("GOOGLE_SC_KEY", "").strip() or _from_env_file("GOOGLE_SC_KEY")
    if not raw:
        return None
    p = Path(raw).expanduser()
    if not p.exists():
        log.warning("GOOGLE_SC_KEY punta a %s, che non esiste.", p)
        return None
    return p


def access_token(key_file: Path) -> str:
    """
    Un token dal service account, senza flusso interattivo.

    Si firma un JWT con la chiave privata e lo si scambia al token endpoint.
    Non si usa google.auth.transport.requests apposta: si porterebbe dietro
    `requests`, mentre la rete di questo repo la fa httpx da undici collector.
    Di google-auth serve solo la firma RS256.
    """
    from google.auth import crypt, jwt   # import qui: senza chiave non serve

    info = json.loads(key_file.read_text(encoding="utf-8"))
    signer = crypt.RSASigner.from_service_account_info(info)
    now = int(time.time())
    assertion = jwt.encode(signer, {
        "iss": info["client_email"],
        "scope": SCOPE,
        "aud": TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    })
    resp = httpx.post(TOKEN_URL, timeout=TIMEOUT, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        # .decode() NON e' cosmetico: jwt.encode() torna bytes, e httpx
        # form-encoda i bytes col loro repr Python. Partiva
        # "assertion=b%27eyJ..." — il JWT con "b'" incollato davanti — e il
        # token endpoint rispondeva 400 invalid_request senza dire altro.
        "assertion": assertion.decode(),
    })
    _raise_with_body(resp)
    return resp.json()["access_token"]


# ── Scarico ────────────────────────────────────────────────────────
def _last_day_stored() -> str | None:
    with _connection() as conn:
        return conn.execute("SELECT MAX(day) FROM search_analytics").fetchone()[0]


def fetch_analytics(token: str, start: date, end: date) -> int:
    """
    Righe (giorno × query × pagina) da `start` a `end`, paginate. Ritorna
    quante ne sono state scritte.

    Una sola richiesta per l'intero intervallo, non una al giorno: con la
    dimensione "date" fra quelle chieste, l'API spezza gia' per giornata e il
    backfill di 16 mesi costa una manciata di chiamate invece di 490.
    """
    url = f"{API}/webmasters/v3/sites/{quote(SITE_URL, safe='')}/searchAnalytics/query"
    headers = {"Authorization": f"Bearer {token}"}
    written, start_row = 0, 0

    while True:
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": ["date", "query", "page"],
            "type": "web",
            "dataState": "final",
            "rowLimit": ROW_LIMIT,
            "startRow": start_row,
            "dimensionFilterGroups": [{"filters": [{
                "dimension": "page",
                "operator": "contains",
                "expression": PAGE_CONTAINS,
            }]}],
        }
        resp = httpx.post(url, headers=headers, json=body, timeout=TIMEOUT)
        _raise_with_body(resp)
        rows = resp.json().get("rows", [])
        if not rows:
            break

        with _connection() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO search_analytics "
                "(day, query, page, clicks, impressions, ctr, position) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(r["keys"][0], r["keys"][1], r["keys"][2],
                  int(r.get("clicks", 0)), int(r.get("impressions", 0)),
                  float(r.get("ctr", 0.0)), float(r.get("position", 0.0)))
                 for r in rows],
            )
        written += len(rows)
        if len(rows) < ROW_LIMIT:
            break
        start_row += ROW_LIMIT

    return written


def sitemap_status(token: str) -> list[dict]:
    """Cosa dice Google delle sitemap dichiarate: errori, avvisi, ultima lettura."""
    url = f"{API}/webmasters/v3/sites/{quote(SITE_URL, safe='')}/sitemaps"
    resp = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
    _raise_with_body(resp)
    return resp.json().get("sitemap", [])


def _sitemap_urls() -> list[str]:
    if not SITEMAP.exists():
        return []
    return re.findall(r"<loc>([^<]+)</loc>", SITEMAP.read_text(encoding="utf-8"))


def inspect_urls(token: str, n: int) -> int:
    """
    Chiede a Google, per n URL, se sono indicizzati e con quale canonical.

    Il canonical scelto da Google e' il dato che qui interessa piu' degli
    altri: build_pages.py consolida apposta /events/<oggi>/ e <domani> sulle
    finestre /today/ e /tomorrow/, e finora nessuno aveva verificato che
    Google rispetti quella indicazione invece di sceglierne una sua.

    A rotazione: prima gli URL mai visti, poi i piu' vecchi.
    """
    urls = _sitemap_urls()
    if not urls or n <= 0:
        return 0

    with _connection() as conn:
        seen = {r["url"]: r["checked_at"] or ""
                for r in conn.execute("SELECT url, checked_at FROM url_inspection")}
    ordered = sorted(urls, key=lambda u: seen.get(u, ""))[:n]

    endpoint = f"{API}/v1/urlInspection/index:inspect"
    headers = {"Authorization": f"Bearer {token}"}
    done = 0
    for u in ordered:
        try:
            resp = httpx.post(endpoint, headers=headers, timeout=TIMEOUT, json={
                "inspectionUrl": u, "siteUrl": SITE_URL, "languageCode": "en-GB",
            })
            _raise_with_body(resp)
            idx = resp.json().get("inspectionResult", {}).get("indexStatusResult", {})
        except httpx.HTTPError as e:
            # Un URL che non risponde non deve interrompere il giro.
            log.warning("Ispezione fallita per %s: %s", u, e)
            continue
        with _connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO url_inspection (url, checked_at, verdict, "
                "coverage, google_canonical, user_canonical, last_crawl, robots) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (u, datetime.now().isoformat(timespec="seconds"),
                 idx.get("verdict", ""), idx.get("coverageState", ""),
                 idx.get("googleCanonical", ""), idx.get("userCanonical", ""),
                 idx.get("lastCrawlTime", ""), idx.get("robotsTxtState", "")),
            )
        done += 1
    return done


# ── Vocabolario del sito, per l'incrocio ───────────────────────────
def _site_vocabulary(min_events: int = 8) -> tuple[dict[str, str], dict[str, str]]:
    """
    Locali e generi che il DB conosce, per capire COME la gente cerca.

    Serve alla sezione piu' utile del rapporto: se le query nominano locali o
    generi, e il sito ha solo pagine per data e per finestra temporale, allora
    mancano pagine — e la prova non e' un'opinione sulla SEO, e' il traffico.

    min_events tiene fuori i nomi visti una volta sola, e la lunghezza minima
    i nomi troppo corti per non pescare falsi positivi dentro altre parole.
    """
    venues: dict[str, str] = {}
    genres: dict[str, str] = {}
    if not EVENTS_DB.exists():
        return venues, genres
    conn = sqlite3.connect(str(EVENTS_DB))
    try:
        for name, n in conn.execute(
                "SELECT venue, COUNT(*) FROM events WHERE venue IS NOT NULL "
                "AND venue != '' GROUP BY venue"):
            if n >= min_events and len(name) >= 4:
                venues[name.lower()] = name
        counts: dict[str, int] = defaultdict(int)
        for (blob,) in conn.execute(
                "SELECT genres FROM events WHERE genres IS NOT NULL AND genres != ''"):
            for g in blob.split("|"):
                if g:
                    counts[g] += 1
        genres = {g.lower(): g for g, n in counts.items() if n >= min_events and len(g) >= 4}
    finally:
        conn.close()
    return venues, genres


# ── Rapporto ───────────────────────────────────────────────────────
def _rows(since: date, until: date) -> list[sqlite3.Row]:
    with _connection() as conn:
        return conn.execute(
            "SELECT * FROM search_analytics WHERE day BETWEEN ? AND ?",
            (since.isoformat(), until.isoformat()),
        ).fetchall()


def _agg(rows, key) -> dict:
    """Somma clic e impression per chiave; la posizione e' una media pesata
    sulle impression, non aritmetica: una frase vista 900 volte in posizione 8
    non pesa quanto una vista 2 volte in posizione 90."""
    acc: dict = defaultdict(lambda: {"clicks": 0, "impr": 0, "pos_w": 0.0})
    for r in rows:
        a = acc[key(r)]
        a["clicks"] += r["clicks"]
        a["impr"] += r["impressions"]
        a["pos_w"] += r["position"] * r["impressions"]
    for a in acc.values():
        a["pos"] = a["pos_w"] / a["impr"] if a["impr"] else 0.0
    return acc


def _tot(rows) -> tuple[int, int, float]:
    clicks = sum(r["clicks"] for r in rows)
    impr = sum(r["impressions"] for r in rows)
    pos = (sum(r["position"] * r["impressions"] for r in rows) / impr) if impr else 0.0
    return clicks, impr, pos


def write_report(sitemaps: list[dict] | None = None) -> Path:
    today = date.today()
    until = today - timedelta(days=LAG_DAYS)
    since = until - timedelta(days=WINDOW_DAYS - 1)
    rows = _rows(since, until)

    L = [
        "# Cosa trova chi cerca",
        "",
        f"Generato: {datetime.now().isoformat(timespec='seconds')}",
        f"Finestra: {since} → {until} ({WINDOW_DAYS} giorni, "
        f"esclusi gli ultimi {LAG_DAYS} che Google non ha ancora consolidato)",
        f"Proprieta': `{SITE_URL}`, filtrata sulle pagine che contengono "
        f"`{PAGE_CONTAINS}`",
        "",
    ]

    if not rows:
        with _connection() as conn:
            n = conn.execute("SELECT COUNT(*) FROM search_analytics").fetchone()[0]
        L += ["## Nessun dato in questa finestra", ""]
        L.append(f"Righe in archivio, in tutto: **{n}**.")
        L.append("")
        L.append("Se la proprieta' e' stata creata da poco e' normale: Search "
                 "Console impiega qualche giorno prima di avere qualcosa da "
                 "dire, e i dati arrivano comunque con "
                 f"{LAG_DAYS} giorni di ritardo.")
        L.append("")
        # Le query mancano, ma indicizzazione e canonical no: quelle si sanno
        # gia', ed e' la parte utile del rapporto in questi primi giorni.
        L += _coverage_lines(sitemaps)
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text("\n".join(L) + "\n", encoding="utf-8")
        return REPORT_PATH

    clicks, impr, pos = _tot(rows)
    L += [f"**{clicks} clic · {impr} impression · posizione media {pos:.1f}**", ""]

    # ── Su cosa esci gia' ──
    by_query = _agg(rows, lambda r: r["query"])
    top = sorted(by_query.items(), key=lambda kv: -kv[1]["impr"])[:25]
    L += ["## Su cosa esci gia'", "",
          "| query | impression | clic | posizione |",
          "|---|---:|---:|---:|"]
    for q, a in top:
        L.append(f"| {q} | {a['impr']} | {a['clicks']} | {a['pos']:.1f} |")
    L.append("")

    # ── Le occasioni ──
    lo, hi = OPPORTUNITY_BAND
    opp = sorted(
        ((q, a) for q, a in by_query.items()
         if lo <= a["pos"] <= hi and a["impr"] >= OPPORTUNITY_MIN_IMPRESSIONS),
        key=lambda kv: -kv[1]["impr"],
    )[:25]
    L += [f"## Dove c'e' da guadagnare — posizione fra {lo:.0f} e {hi:.0f}", "",
          "Frasi per cui esci gia', ma sotto la piega: qui due posizioni "
          "valgono clic veri, mentre in trentesima non valgono niente.", ""]
    if opp:
        L += ["| query | impression | clic | posizione | pagina |", "|---|---:|---:|---:|---|"]
        for q, a in opp:
            best = max((r for r in rows if r["query"] == q),
                       key=lambda r: r["impressions"])["page"]
            L.append(f"| {q} | {a['impr']} | {a['clicks']} | {a['pos']:.1f} | "
                     f"{best.replace('https://' + PAGE_CONTAINS, '') or '/'} |")
    else:
        L.append("Nessuna frase in questa fascia, per ora.")
    L.append("")

    # ── Quali pagine portano ──
    by_page = _agg(rows, lambda r: r["page"])
    L += ["## Quali pagine portano", "",
          "| pagina | impression | clic | posizione |", "|---|---:|---:|---:|"]
    for p, a in sorted(by_page.items(), key=lambda kv: -kv[1]["impr"])[:20]:
        L.append(f"| {p.replace('https://' + PAGE_CONTAINS, '') or '/'} | "
                 f"{a['impr']} | {a['clicks']} | {a['pos']:.1f} |")
    smap = _sitemap_urls()
    if smap:
        mute = len(set(smap) - set(by_page))
        L += ["", f"**{mute} delle {len(smap)} pagine in sitemap non hanno "
                  f"nessuna impression in questa finestra.** Se il numero non "
                  f"scende col tempo, sono pagine che esistono e non servono."]
    L.append("")

    # ── Cosa e' cambiato ──
    a_since, a_until = until - timedelta(days=6), until
    b_since, b_until = until - timedelta(days=13), until - timedelta(days=7)
    now7, prev7 = _tot(_rows(a_since, a_until)), _tot(_rows(b_since, b_until))
    L += ["## Cosa e' cambiato", "",
          "| | clic | impression | posizione |", "|---|---:|---:|---:|",
          f"| ultimi 7 giorni | {now7[0]} | {now7[1]} | {now7[2]:.1f} |",
          f"| 7 precedenti | {prev7[0]} | {prev7[1]} | {prev7[2]:.1f} |",
          f"| differenza | {now7[0] - prev7[0]:+d} | {now7[1] - prev7[1]:+d} | "
          f"{now7[2] - prev7[2]:+.1f} |", ""]

    # ── Come cerca la gente ──
    venues, genres = _site_vocabulary()
    def _hits(vocab: dict[str, str]) -> list[tuple[str, int, int]]:
        acc: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in rows:
            q = r["query"].lower()
            for low, name in vocab.items():
                if re.search(rf"\b{re.escape(low)}\b", q):
                    acc[name][0] += r["impressions"]
                    acc[name][1] += r["clicks"]
        return sorted(((n, v[0], v[1]) for n, v in acc.items()), key=lambda t: -t[1])[:12]

    v_hits, g_hits = _hits(venues), _hits(genres)
    L += ["## Come cerca la gente", "",
          "Il sito ha pagine per **data** e per **finestra temporale**. Se le "
          "frasi qui sotto portano volume, la gente sta cercando per **locale** "
          "o per **genere**, cioe' due tagli che nessuna pagina serve — e i "
          "dati per costruirli sono gia' tutti in `data/events.db`.", ""]
    for titolo, hits in (("Locali nominati nelle query", v_hits),
                         ("Generi nominati nelle query", g_hits)):
        L += [f"### {titolo}", ""]
        if hits:
            L += ["| nome | impression | clic |", "|---|---:|---:|"]
            L += [f"| {n} | {i} | {c} |" for n, i, c in hits]
        else:
            L.append("Nessuno, in questa finestra.")
        L.append("")

    L += _coverage_lines(sitemaps)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(L) + "\n", encoding="utf-8")
    return REPORT_PATH


def _coverage_lines(sitemaps: list[dict] | None) -> list[str]:
    """
    Copertura e stato di indicizzazione.

    Sta in una funzione sua perche' serve in ENTRAMBI i rami del rapporto: nei
    primi giorni le query non ci sono ancora, ma "Google ci ha indicizzati?" e
    "quale canonical ha scelto?" hanno gia' una risposta — ed e' l'unica cosa
    che il rapporto puo' dire di utile finche' i dati di ricerca non arrivano.
    """
    L = ["## Copertura", ""]
    if sitemaps:
        L += ["| sitemap | ultima lettura | URL | errori | avvisi |",
              "|---|---|---:|---:|---:|"]
        for s in sitemaps:
            n = sum(int(c.get("submitted", 0)) for c in s.get("contents", []))
            L.append(f"| {s.get('path', '')} | {(s.get('lastDownloaded') or '—')[:10]} | "
                     f"{n} | {s.get('errors', 0)} | {s.get('warnings', 0)} |")
        L.append("")

    with _connection() as conn:
        insp = conn.execute(
            "SELECT * FROM url_inspection ORDER BY checked_at DESC LIMIT 500"
        ).fetchall()
    if insp:
        # "quanti indicizzati" da solo nasconde la differenza che conta nei
        # primi giorni: fra un URL che Google ha guardato e scartato e uno che
        # non ha ancora aperto c'e' di mezzo tutto il lavoro da fare (o non
        # farci niente e aspettare).
        on_google = sum(1 for r in insp if r["verdict"] == "PASS")
        L += [f"Su **{len(insp)} URL ispezionati**, {on_google} sono su Google.", "",
              "| stato secondo Google | URL |", "|---|---:|"]
        L += [f"| {k} | {v}" + " |"
              for k, v in Counter((r["coverage"] or "—") for r in insp).most_common()]
        L += ["",
              "*Unknown* = mai vista. *Discovered* = conosce l'indirizzo (dalla "
              "sitemap) ma non l'ha ancora aperta. *Crawled – not indexed* = "
              "l'ha letta e non l'ha ancora messa nell'indice.", ""]

        crawled = sorted((r for r in insp if r["last_crawl"]),
                         key=lambda r: r["last_crawl"], reverse=True)
        if crawled:
            L += ["Quando Google e' passato davvero:", "",
                  "| URL | ultima scansione (UTC) |", "|---|---|"]
            for r in crawled[:15]:
                L.append(f"| {r['url'].replace('https://' + PAGE_CONTAINS, '') or '/'} "
                         f"| {r['last_crawl']} |")
            L.append("")
        diversi = [r for r in insp if r["google_canonical"]
                   and r["user_canonical"]
                   and r["google_canonical"] != r["user_canonical"]]
        if diversi:
            L += ["Google ha scelto un canonical diverso dal nostro su "
                  f"{len(diversi)} URL:", "",
                  "| URL | il nostro | quello di Google |", "|---|---|---|"]
            for r in diversi[:15]:
                L.append(f"| {r['url']} | {r['user_canonical']} | {r['google_canonical']} |")
            L.append("")
        else:
            L.append("Nessun disaccordo sul canonical fra noi e Google.")
            L.append("")
    else:
        L.append("Nessun URL ancora ispezionato.")
        L.append("")
    return L


# ── Giro completo ──────────────────────────────────────────────────
def run(inspect: int = INSPECT_PER_RUN, report_only: bool = False) -> dict:
    init_db()
    stats = {"configurato": False, "righe": 0, "ispezionati": 0, "giorni": ""}

    if report_only:
        write_report()
        stats["configurato"] = True
        return stats

    key = _key_path()
    if key is None:
        # Non e' un guasto: e' il caso in cui la chiave non e' ancora stata
        # creata. Lo script e' agganciato alla pipeline e non deve fermarla.
        log.warning("GOOGLE_SC_KEY assente o file inesistente: niente scarico. "
                    "Vedi le istruzioni in testa a questo file.")
        write_report()
        return stats

    stats["configurato"] = True
    token = access_token(key)

    until = date.today() - timedelta(days=LAG_DAYS)
    last = _last_day_stored()
    start = (date.fromisoformat(last) + timedelta(days=1)) if last \
        else until - timedelta(days=BACKFILL_DAYS)
    if start <= until:
        stats["righe"] = fetch_analytics(token, start, until)
        stats["giorni"] = f"{start} → {until}"
        log.info("Search Console: %d righe da %s a %s.", stats["righe"], start, until)
    else:
        log.info("Search Console: niente di nuovo (ultimo giorno in archivio: %s).", last)

    stats["ispezionati"] = inspect_urls(token, inspect)
    write_report(sitemap_status(token))
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--report", action="store_true",
                    help="riscrive solo il rapporto, senza chiamare l'API")
    ap.add_argument("--inspect", type=int, default=INSPECT_PER_RUN,
                    help=f"quanti URL ispezionare (default {INSPECT_PER_RUN}, 0 per saltare)")
    args = ap.parse_args()

    try:
        stats = run(inspect=args.inspect, report_only=args.report)
    except Exception as e:  # noqa: BLE001
        # Fail-soft come enrich_artists e indexnow: una chiave scaduta o
        # l'API giu' non devono costare il collect. Resta nel log.
        log.error("Search Console saltato (%s: %s)", type(e).__name__, e)
        print(f"\n⚠️  Search Console: saltato ({type(e).__name__}). Il resto della "
              f"pipeline non e' toccato.")
        return

    if not stats["configurato"]:
        print("\n⚠️  Search Console: non configurato (GOOGLE_SC_KEY). "
              "Istruzioni in testa a search_console.py")
        return
    print(f"\n✅ Search Console: {stats['righe']} righe"
          + (f" ({stats['giorni']})" if stats["giorni"] else " nuove")
          + f" · {stats['ispezionati']} URL ispezionati")
    print(f"   rapporto: {REPORT_PATH}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(name)-14s │ %(levelname)-5s │ %(message)s",
        datefmt="%H:%M:%S",
    )
    main()
