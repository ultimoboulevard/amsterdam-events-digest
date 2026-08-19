"""
Gli artisti di un evento: quelli dello scraper, se ci sono; se no, quelli
estratti dal titolo e messi in cache da enrich_artists.py.

PERCHE' ESISTE
`ev.artists` e' vuoto su 741 eventi futuri su 2.068 (36%), e DiscoveryEngine
lavora proprio su quella lista: senza, export_json.py e build_pages.py
ripiegavano su `[ev.title]` e davano in pasto al matching il titolo intero
("book presentation: dreaming of dreams") come se fosse il nome di un
artista. Misurato: 10% di "Curators' pick" sulla cohorte con artisti contro
3% su quella senza — e 1.002 titoli-non-artisti finiti dentro
lastfm_cache.db, il 47% dei quali torna zero simili.

ESITO, misurato il 19/08/2026 sul JSON rigenerato
Sulla cohorte senza `artists` il tasso di "Curators' pick" e' passato dal 2,9%
al 5,9%. Quel 5,9% pero' e' una media che nasconde il risultato: spaccando la
cohorte in due,

    artisti ESTRATTI dal titolo      42/404  = 10,4%
    nessun artista nel titolo         0/308  =  0,0%
    (riferimento) dallo scraper     118/1161 = 10,2%

i nomi estratti agganciano la libreria alla STESSA frequenza di quelli veri
dello scraper. Gli altri 308 sono nomi di serata ("source x weekdays",
"raving charlie"): un artista non ce l'hanno, e li' nessun metodo fa meglio
di zero. Chi legge solo il 5,9% conclude che il lavoro e' andato a meta'.

DISCIPLINA
Questo modulo LEGGE E BASTA: apre la cache in sola lettura, non fa rete, non
solleva. Se data/artist_cache.db non c'e', o e' vuoto, o e' corrotto, si
comporta come il codice di prima e le pagine escono identiche. La rete la fa
solo enrich_artists.py, in uno step suo della pipeline.

Modulo condiviso per lo stesso motivo di labels.py, dedup.py e venues.py: la
riga `ev.artists if ev.artists else [ev.title]` esisteva gia' in due copie, e
due copie divergono.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import unicodedata

from config import DATA_DIR

log = logging.getLogger(__name__)

CACHE_DB_PATH = DATA_DIR / "artist_cache.db"

# Cache in memoria: la tabella e' di qualche centinaio di righe e viene letta
# una volta sola per processo. export_json.py e build_pages.py ciclano su
# ~2.000 eventi: una connessione per evento sarebbe il bug dei file
# descriptor gia' visto in db.py.
_CACHE: dict[str, list[str]] | None = None


def title_key(title: str) -> str:
    """La chiave della cache: NFKC + casefold + spazi normalizzati.

    NFKC serve davvero, non e' pedanteria: i titoli in unicode matematico
    (𝗥&𝗕 𝗖𝗔𝗥𝗜𝗕𝗕𝗘𝗔𝗡 ...) altrimenti non
    coinciderebbero mai con la loro forma normale.
    """
    return " ".join(unicodedata.normalize("NFKC", title or "").casefold().split())


def _load() -> dict[str, list[str]]:
    """Carica la cache. Qualunque cosa vada storta ⇒ dizionario vuoto."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    cache: dict[str, list[str]] = {}
    if CACHE_DB_PATH.exists():
        try:
            conn = sqlite3.connect(f"file:{CACHE_DB_PATH}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT title_norm, artists FROM title_artists"
                ).fetchall()
            finally:
                conn.close()
            for title_norm, artists_json in rows:
                try:
                    names = json.loads(artists_json or "[]")
                except (TypeError, ValueError):
                    continue
                if isinstance(names, list):
                    # Anche la lista VUOTA si tiene: "in questo titolo non
                    # c'e' nessun artista" e' una risposta, e vale quanto le
                    # altre — e' quella che impedisce di rimandare il titolo
                    # intero a Last.fm.
                    cache[title_norm] = [str(n) for n in names]
        except sqlite3.Error as e:
            log.warning("Cache artisti non leggibile (%s): si prosegue senza.", e)
            cache = {}
        else:
            log.info("Cache artisti: %d titoli noti.", len(cache))

    _CACHE = cache
    return _CACHE


def artists_from_title(title: str) -> list[str] | None:
    """Gli artisti estratti dal titolo. Mai rete.

    Tre esiti distinti, e la differenza fra gli ultimi due e' il punto:
      lista non vuota → i nomi trovati nel titolo
      lista vuota     → il modello ha guardato e artisti non ce ne sono
      None            → il titolo non e' ancora passato da enrich_artists.py
    """
    return _load().get(title_key(title))


def artists_for(ev) -> list[str]:
    """Gli artisti su cui fare il matching di DiscoveryEngine.

    Ordine: quelli dello scraper; se mancano, quelli estratti dal titolo; se
    il titolo non e' ancora in cache, il titolo intero — cioe' esattamente il
    comportamento di prima, che resta il fondo su cui si atterra.

    Se invece la cache dice "in questo titolo non c'e' nessun artista"
    (lista vuota) si ritorna la lista vuota, NON il titolo: e' quella
    risposta che risparmia le ~300 interrogazioni Last.fm a vuoto e smette
    di riempire lastfm_cache.db di righe come 'eetcafe adm noord'.

    NON e' cio' che si mostra: le schede continuano a stampare `ev.artists`.
    Il nome estratto e' gia' dentro il titolo, ristamparlo sotto sarebbe una
    ripetizione.
    """
    if ev.artists:
        return ev.artists
    extracted = artists_from_title(ev.title)
    return list(extracted) if extracted is not None else [ev.title]
