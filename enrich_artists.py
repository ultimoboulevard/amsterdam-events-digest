#!/usr/bin/env python3
"""
Riempie il campo `artists` quando le fonti non lo danno, estraendolo dal
titolo con una chiamata a Gemini, e lo mette in cache.

PERCHE'
741 eventi futuri su 2.068 (36%) arrivano senza artisti. DiscoveryEngine
lavora su quella lista, quindi per un terzo del cartellone i "Curators' pick"
si calcolavano sul titolo intero: 3% di pick sulla cohorte senza artisti
contro il 10% di quella con. E ogni titolo finiva come chiave in
lastfm_cache.db (1.002 righe cosi', il 47% con zero simili).

PERCHE' UN MODELLO E NON UNA REGOLA
Su 652 titoli distinti, la regola "titolo corto e senza separatori ⇒ e' un
artista" ne prende 354 ma ci infila "Workshops SDF 26 August" e "Eetcafe ADM
Noord". Il compito vero e' distinguere SHU da "Hard Techno" dentro la stessa
stringa, e saper rispondere "qui non c'e' nessun artista" — risposta che vale
quanto le altre, perche' e' quella che ferma le interrogazioni a vuoto.

Esito: i nomi estratti agganciano la libreria al 10,4%, cioe' quanto quelli
veri dello scraper (10,2%). Il conto per esteso sta nel docstring di
artists.py.

IL CANCELLO ANTI-ALLUCINAZIONE
Un nome inventato diventerebbe un falso "Curators' pick" in vetrina. Quindi:
ogni nome restituito DEVE comparire nel titolo di partenza (confronto dopo
NFKC + casefold + rimozione dei diacritici), se non compare si scarta senza
appello. L'estrazione da un titolo e' verificabile per costruzione — la
risposta deve essere contenuta nell'input — e questo riporta una chiamata
generativa a un esito deterministico.

FALLIMENTO
Non solleva mai. Chiave assente, API giu', JSON malformato, timeout ⇒ la
cache non si popola e tutto il resto della pipeline si comporta come prima.
Stessa disciplina di indexnow.py.

    ./.venv/bin/python enrich_artists.py --dry-run --limit 40   # stampa, non scrive
    ./.venv/bin/python enrich_artists.py                        # backfill vero
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import unicodedata
from pathlib import Path

import httpx

from artists import CACHE_DB_PATH, title_key
from config import DATA_DIR
from db import get_upcoming_events, init_db
from export_json import EXPORT_DAYS

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

# ── Modello ────────────────────────────────────────────────────────
# Verificato il 19/08/2026 con ListModels sulla stessa chiave: e' lo stesso
# identificativo che gatekeeper.py usa in produzione sul repo Tidal.
MODEL_NAME = "gemini-3.5-flash"
API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{MODEL_NAME}:generateContent"
)
# thinking OFF, deliberatamente. Misurato su questo prompt: con thinkingBudget=0
# la risposta e' identica a quella col thinking attivo (stessi nomi sugli stessi
# titoli) e costa 460 token di pensiero in meno per chiamata. Qui non c'e'
# giudizio da fare, si copia dall'input — il contrario del gatekeeper, dove il
# thinking e' attivo apposta.
THINKING_BUDGET = 0
TEMPERATURE = 0
BATCH_SIZE = 25
TIMEOUT = 120

# Lo stesso cap di visualizzazione gia' usato in _event_card / JSON-LD.
MAX_ARTISTS = 6

SYSTEM_INSTRUCTION = (
    "You extract performer names from event titles for an Amsterdam events "
    "listing. For each title, return the names of the musicians, bands, DJs, "
    "or performing artists that appear IN THE TITLE.\n"
    "Rules:\n"
    "- Copy each name VERBATIM from the title. Never invent, expand, correct "
    "or translate a name.\n"
    "- Do NOT return genres (techno, jazz, hard techno, ambient), venue or "
    "cafe names, city names, festival/party/series names, promoters, words "
    "like 'presents', 'invites', 'live', 'w/', or event formats (workshop, "
    "book presentation, exhibition, open mic, quiz).\n"
    "- Drop country tags in brackets: '[NL]', '[CN]'.\n"
    "- If the title contains no performer name at all, return an empty list. "
    "An empty list is a correct and useful answer — prefer it to guessing.\n"
    "Answer with JSON only: an object mapping each input number (as a string) "
    'to a list of names, e.g. {"0": ["Metrocelli"], "1": []}.'
)

# Parole che il modello restituisce ogni tanto come se fossero nomi, e che
# nomi non sono. Il locale dell'evento si aggiunge a questa lista riga per
# riga (vedi _stopwords_for): "Splendor" e' un artista plausibile ovunque
# tranne che nel titolo di un evento a Splendor.
STOPLIST = {
    "presents", "present", "presenta", "invites", "invite", "live", "dj",
    "djs", "vs", "b2b", "and friends", "friends", "amsterdam", "noord",
    "afterparty", "party", "festival", "workshop", "exhibition", "expo",
    "concert", "film", "club", "open mic", "jam", "quiz", "market",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "maandag", "dinsdag", "woensdag", "donderdag", "vrijdag",
    "zaterdag", "zondag",
}

CREATE_CACHE_TABLE = """
CREATE TABLE IF NOT EXISTS title_artists (
    title_norm  TEXT PRIMARY KEY,
    artists     TEXT,
    raw         TEXT,
    model       TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);
"""


# ── Normalizzazione e cancello ─────────────────────────────────────
def _fold(s: str) -> str:
    """NFKC + casefold + via i diacritici: la forma su cui si confronta.

    Serve per far coincidere 'Bjork' con 'Björk' e per i titoli scritti in
    unicode decorativo, che altrimenti non matcherebbero mai il proprio
    contenuto.
    """
    s = unicodedata.normalize("NFKC", s or "").casefold()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.split())


def _stopwords_for(venues: set[str]) -> set[str]:
    """La stoplist fissa piu' i locali in cui quel titolo va in scena."""
    stop = set(STOPLIST)
    for v in venues:
        folded = _fold(v)
        if folded:
            stop.add(folded)
            # "Paradiso (Tolhuistuin)" ⇒ anche "paradiso" e "tolhuistuin".
            for part in re.split(r"[()\-,/|]", folded):
                part = part.strip()
                if len(part) > 2:
                    stop.add(part)
    return stop


def accepted_names(title: str, names: list, venues: set[str]) -> list[str]:
    """I filtri, in ordine. Ritorna solo cio' che sopravvive a tutti.

    1. il nome deve comparire NEL TITOLO (confronto su forma foldata)
    2. almeno 2 caratteri
    3. non e' nella stoplist (locale dell'evento, 'presents', giorni...)
    4. niente doppioni, e al massimo MAX_ARTISTS nomi
    """
    folded_title = _fold(title)
    stop = _stopwords_for(venues)

    out: list[str] = []
    seen: set[str] = set()
    for raw in names:
        if not isinstance(raw, str):
            continue
        name = raw.strip().strip("-–—·,;:/|").strip()
        if len(name) < 2:
            continue
        folded = _fold(name)
        if len(folded) < 2 or folded in stop or folded in seen:
            continue
        if folded not in folded_title:
            log.debug("Scartato (non e' nel titolo): %r ∉ %r", name, title)
            continue
        seen.add(folded)
        out.append(name)
        if len(out) >= MAX_ARTISTS:
            break
    return out


# ── Cache ──────────────────────────────────────────────────────────
def init_cache(db_path: Path = CACHE_DB_PATH) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            conn.executescript(CREATE_CACHE_TABLE)
    finally:
        conn.close()


def cached_titles(db_path: Path = CACHE_DB_PATH) -> set[str]:
    """Le chiavi gia' viste. Una voce in cache non si richiede mai piu'."""
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[0] for r in conn.execute("SELECT title_norm FROM title_artists")}
    except sqlite3.Error as e:
        log.warning("Cache artisti non leggibile (%s).", e)
        return set()
    finally:
        conn.close()


def store(rows: list[tuple[str, list[str], list]], db_path: Path = CACHE_DB_PATH) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO title_artists (title_norm, artists, raw, model) "
                "VALUES (?, ?, ?, ?)",
                [
                    (key, json.dumps(names, ensure_ascii=False),
                     json.dumps(raw, ensure_ascii=False), MODEL_NAME)
                    for key, names, raw in rows
                ],
            )
    finally:
        conn.close()


# ── Chiave ─────────────────────────────────────────────────────────
def api_key() -> str:
    """GEMINI_API_KEY dall'ambiente; in mancanza, dal .env del progetto.

    run_pipeline.sh fa il source del .env e la esporta, ma questo script si
    lancia anche a mano (il --dry-run del collaudo): senza questo fallback il
    collaudo direbbe "chiave assente" pur avendola nel file accanto.
    """
    key = os.environ.get("GEMINI_API_KEY", "").strip().strip('"').strip("'")
    if key:
        return key
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# ── Modello ────────────────────────────────────────────────────────
def call_gemini(system: str, user: str, key: str,
                thinking_budget: int | None = THINKING_BUDGET,
                json_out: bool = True, timeout: int = TIMEOUT) -> str | None:
    """Una richiesta a Gemini. Ritorna il testo della risposta, o None.

    E' il client condiviso: lo usa anche diagnose.py, che diagnostica gli
    scraper rotti. Non solleva MAI — rete, 4xx, risposta senza candidati ⇒
    None, e chi chiama decide come cavarsela senza.

    `thinking_budget=None` lascia il default del modello (pensiero attivo);
    0 lo spegne, che e' cio' che serve qui dove si copia dall'input.

    httpx e non urllib: su questo Python (framework, installato a mano)
    urllib non ha i certificati di sistema e muore con
    CERTIFICATE_VERIFY_FAILED — gia' successo con IndexNow.
    """
    generation: dict = {"temperature": TEMPERATURE}
    if json_out:
        generation["responseMimeType"] = "application/json"
    if thinking_budget is not None:
        generation["thinkingConfig"] = {"thinkingBudget": thinking_budget}

    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": user}]}],
        "generationConfig": generation,
    }
    try:
        resp = httpx.post(
            API_URL, json=body, timeout=timeout,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            log.error("Gemini ha risposto %s: %s", resp.status_code, resp.text[:300])
            return None
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as e:
        log.error("Chiamata saltata (%s: %s)", type(e).__name__, e)
        return None


def ask_model(titles: list[str], key: str) -> dict[int, list]:
    """Un lotto di titoli ⇒ {indice: lista grezza di nomi}.

    Non solleva: qualunque guaio (rete, 4xx, JSON storto) ⇒ dizionario vuoto,
    e quei titoli restano fuori dalla cache. Il giorno dopo si riprova.
    """
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(titles))
    text = call_gemini(SYSTEM_INSTRUCTION, numbered, key)
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        log.error("Lotto saltato, JSON illeggibile: %s", e)
        return {}

    out: dict[int, list] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            try:
                idx = int(k)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(titles) and isinstance(v, list):
                out[idx] = v
    return out


# ── Raccolta dei titoli da arricchire ──────────────────────────────
def titles_to_enrich(days: int = EXPORT_DAYS) -> list[tuple[str, str, set[str]]]:
    """(chiave, titolo, locali) per ogni titolo distinto senza artisti.

    Le righe che gli artisti ce l'hanno non vengono mai mandate. La chiave e'
    il TITOLO, non dedup_key: le serate ricorrenti hanno lo stesso titolo su
    piu' giorni, e cosi' si paga una volta sola.
    """
    init_db()
    seen: dict[str, tuple[str, set[str]]] = {}
    for ev in get_upcoming_events(days=days):
        if ev.artists or not (ev.title or "").strip():
            continue
        key = title_key(ev.title)
        if not key:
            continue
        if key not in seen:
            seen[key] = (ev.title, set())
        if ev.venue:
            seen[key][1].add(ev.venue)
    return [(k, t, v) for k, (t, v) in seen.items()]


def run(dry_run: bool = False, limit: int | None = None, days: int = EXPORT_DAYS) -> int:
    """Ritorna il numero di titoli scritti in cache (0 in dry run)."""
    init_cache()
    already = cached_titles()
    pending = [row for row in titles_to_enrich(days) if row[0] not in already]
    if limit:
        pending = pending[:limit]

    if not pending:
        print("Niente da fare: tutti i titoli senza artisti sono gia' in cache.")
        return 0

    key = api_key()
    if not key:
        # Fail-soft, come indexnow.py: si stampa e si esce con calma. Il resto
        # della pipeline non se ne accorge.
        log.error("GEMINI_API_KEY assente: nessuna chiamata, cache invariata.")
        print("⚠️ GEMINI_API_KEY assente — %d titoli restano senza artisti." % len(pending))
        return 0

    written = 0
    print(f"{len(pending)} titoli da arricchire · lotti da {BATCH_SIZE} · {MODEL_NAME}")
    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start:start + BATCH_SIZE]
        answers = ask_model([t for _, t, _ in batch], key)
        rows = []
        for i, (tkey, title, venues) in enumerate(batch):
            raw = answers.get(i)
            if raw is None:
                continue  # lotto fallito o indice mancante: si riprova domani
            names = accepted_names(title, raw, venues)
            rows.append((tkey, names, raw))
            if dry_run:
                dropped = [r for r in raw if isinstance(r, str)
                           and r.strip() not in names]
                extra = f"   (scartati: {dropped})" if dropped else ""
                print(f"  {title}\n    → {names}{extra}")
        if rows and not dry_run:
            store(rows)
            written += len(rows)

    if dry_run:
        print(f"\n[dry run] {len(pending)} titoli interrogati, cache non toccata.")
    else:
        print(f"\n✅ {written} titoli scritti in {CACHE_DB_PATH.name}")
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--dry-run", action="store_true",
                    help="stampa le estrazioni, non scrive in cache")
    ap.add_argument("--limit", type=int, default=None,
                    help="ferma dopo N titoli (collaudo)")
    args = ap.parse_args()
    run(dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(name)-15s │ %(levelname)-5s │ %(message)s",
        datefmt="%H:%M:%S",
    )
    main()
