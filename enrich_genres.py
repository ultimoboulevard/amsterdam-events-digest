#!/usr/bin/env python3
"""
Assegna una FAMIGLIA MUSICALE agli eventi che la fonte pubblica senza genere,
deducendola da titolo, locale e artisti, e la mette in cache.

PERCHE'
Misurato il 2026-08-20: 1.005 eventi futuri si dichiarano "Concert" o "Club" e non
portano un solo tag di genere; altri 838 non hanno nemmeno il tipo. Nel filtro Genre
sono invisibili — e sono un terzo del cartellone musicale. Non e' un buco della
mappatura in famiglie: e' che quelle fonti il campo non lo compilano proprio.
Le mostre e i workshop senza genere restano fuori: li' l'assenza e' corretta.

PERCHE' UN MODELLO E NON UNA REGOLA
Il genere quasi mai sta nel testo. "Amsterdam Techno Sessions" si prende con una
regex, ma "REDD. @Paradiso" o "Elevate w/ Siimon b2b PVDS" no: li' serve sapere chi
sono quegli artisti e cosa fa quel locale. E' conoscenza del mondo, che una regola
non ha. Serve anche saper rispondere "non lo so", che una regex non sa fare.

IL CANCELLO ANTI-ALLUCINAZIONE
Diverso da enrich_artists.py, dove la risposta doveva comparire nel titolo e il
controllo era una sottostringa. Qui la risposta NON e' nell'input, quindi il vincolo
e' un altro: deve essere UNA delle famiglie che gia' esistono (labels.GENRE_FAMILIES),
o null. Enum chiuso ⇒ il modello puo' sbagliare famiglia, non puo' inventarne una, e
non puo' far comparire nel menu una voce che nessuno ha deciso.
`null` e' una risposta legittima e va incoraggiata: meglio un evento senza genere che
un techno-party etichettato Classical. Un'etichetta sbagliata in vetrina costa piu'
di un'etichetta assente.

FALLIMENTO
Non solleva mai. Chiave assente, API giu', JSON malformato, timeout ⇒ la cache non si
popola e il sito resta identico a prima. Stessa disciplina di enrich_artists e indexnow.

    ./.venv/bin/python enrich_genres.py --dry-run --limit 30   # stampa, non scrive
    ./.venv/bin/python enrich_genres.py                        # backfill vero
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from db import get_upcoming_events, init_db
from labels import GENRE_FAMILIES, is_genre

# `api_key` e `call_gemini` si importano DENTRO le funzioni, non qui: enrich_artists
# importa export_json (per EXPORT_DAYS) ed export_json importa questo modulo per le
# famiglie dedotte — a import globale si chiuderebbe un anello e nessuno dei tre
# partirebbe piu'. Il costo e' un import per chiamata, cioe' niente.

BASE_DIR = Path(__file__).resolve().parent
CACHE_DB = BASE_DIR / "data" / "genre_cache.db"
BATCH_SIZE = 25
MODEL_LABEL = "gemini-3.5-flash"

log = logging.getLogger(__name__)

# Le famiglie ammesse, prese da labels: se domani ne nasce una nuova, il prompt la
# conosce senza che nessuno debba ricordarsi di aggiornarla anche qui.
FAMILIES = [n for n, _ in GENRE_FAMILIES if not n.startswith("_")]
FAMILIES = sorted(set(FAMILIES))

SYSTEM = (
    "Sei un classificatore di eventi musicali di Amsterdam. Per ogni evento scegli UNA "
    "famiglia musicale fra queste, esattamente come sono scritte:\n"
    + "\n".join(f"- {f}" for f in FAMILIES)
    + "\n\nRegole:\n"
    "1. Rispondi SOLO con JSON: [{\"i\": <indice>, \"family\": \"<famiglia o null>\"}]\n"
    "2. Se non sei ragionevolmente sicuro, rispondi null. Un evento senza famiglia e' "
    "un esito accettabile; una famiglia sbagliata finisce in vetrina e vale meno di niente.\n"
    "3. Usa quello che sai degli artisti e del locale. Un club di Amsterdam che annuncia "
    "un b2b di dj e' Dance; una sala da concerto con un quartetto d'archi e' Classical.\n"
    "4. Dance = musica da ballo (house, techno, disco, drum & bass). Electronic = "
    "elettronica non da ballo (ambient, IDM, synth). Non confonderle.\n"
    "5. Non inventare famiglie che non sono nell'elenco."
)


def _key(title: str) -> str:
    return " ".join((title or "").split()).strip().lower()


def init_cache(db_path: Path = CACHE_DB) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS title_family (
            title_key TEXT PRIMARY KEY,
            family    TEXT,
            model     TEXT,
            created_at TEXT
        )""")


def cached_families(db_path: Path = CACHE_DB) -> dict[str, str]:
    """`{titolo normalizzato: famiglia}` per i soli titoli con una famiglia vera."""
    if not db_path.exists():
        return {}
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            return {k: v for k, v in conn.execute(
                "SELECT title_key, family FROM title_family WHERE family IS NOT NULL AND family<>''")}
    except sqlite3.Error as exc:
        log.warning("genre_cache illeggibile (%s): si prosegue senza.", exc)
        return {}


def cached_keys(db_path: Path = CACHE_DB) -> set[str]:
    """Titoli gia' interrogati, famiglia o null che sia: non si richiedono due volte."""
    if not db_path.exists():
        return set()
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            return {r[0] for r in conn.execute("SELECT title_key FROM title_family")}
    except sqlite3.Error:
        return set()


def store(rows: list[tuple[str, str | None]], db_path: Path = CACHE_DB) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(str(db_path)) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO title_family (title_key, family, model, created_at) "
            "VALUES (?,?,?,?)",
            [(k, f, MODEL_LABEL, now) for k, f in rows])


def pending_events(limit: int | None = None) -> list:
    """Eventi musicali senza genere e non ancora interrogati.

    Solo Concert/Club e quelli senza tipo: una mostra senza genere musicale non e'
    un buco da riempire. E si tiene UN evento per titolo — le serate ricorrenti
    hanno lo stesso nome e la stessa risposta.
    """
    init_db()
    visti, out = cached_keys(), []
    for ev in get_upcoming_events(days=400):
        if any(is_genre(g) for g in (ev.genres or [])):
            continue
        if (ev.event_type or "") not in ("", "Concert", "Club"):
            continue
        k = _key(ev.title)
        if not k or k in visti:
            continue
        visti.add(k)
        out.append(ev)
        if limit and len(out) >= limit:
            break
    return out


def classify(events: list, key: str) -> list[tuple[str, str | None]]:
    from enrich_artists import call_gemini
    righe: list[tuple[str, str | None]] = []
    ammesse = set(FAMILIES)
    for i in range(0, len(events), BATCH_SIZE):
        lotto = events[i:i + BATCH_SIZE]
        voci = [
            {"i": n, "title": ev.title, "venue": ev.venue or "",
             "type": ev.event_type or "", "artists": (ev.artists or [])[:4]}
            for n, ev in enumerate(lotto)
        ]
        raw = call_gemini(SYSTEM, json.dumps(voci, ensure_ascii=False), key,
                          thinking_budget=0)
        if not raw:
            log.warning("lotto %d: nessuna risposta, lo salto", i // BATCH_SIZE + 1)
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("lotto %d: JSON malformato, lo salto", i // BATCH_SIZE + 1)
            continue
        for item in data if isinstance(data, list) else []:
            try:
                ev = lotto[int(item.get("i"))]
            except (TypeError, ValueError, IndexError):
                continue
            fam = item.get("family")
            fam = fam if isinstance(fam, str) and fam in ammesse else None  # il cancello
            righe.append((_key(ev.title), fam))
    return righe


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[1])
    ap.add_argument("--dry-run", action="store_true", help="stampa, non scrive in cache")
    ap.add_argument("--limit", type=int, help="quanti titoli al massimo")
    a = ap.parse_args()

    from enrich_artists import api_key

    key = api_key()
    if not key:
        print("GEMINI_API_KEY assente: non faccio nulla (il sito resta com'e').")
        return
    init_cache()
    todo = pending_events(a.limit)
    if not todo:
        print("Niente da classificare: tutti i titoli musicali hanno gia' un genere o una risposta.")
        return
    print(f"{len(todo)} titoli da classificare · lotti da {BATCH_SIZE} · {MODEL_LABEL}")
    righe = classify(todo, key)
    con_fam = [r for r in righe if r[1]]
    print(f"→ {len(righe)} risposte · {len(con_fam)} con famiglia · {len(righe)-len(con_fam)} lasciate vuote")
    if a.dry_run:
        for k, f in righe[:30]:
            print(f"   {f or '—':18} {k[:60]}")
        print("(dry-run: niente scritto)")
        return
    store(righe)
    print(f"✅ cache aggiornata: {CACHE_DB}")


if __name__ == "__main__":
    main()
