"""
Etichette condivise: fonte → nome leggibile, tipo di evento → emoji.

Fino al 18/08/2026 le stesse due mappe esistevano in due copie divergenti:
site/app.js (12 fonti, completa) e html_builder.py (6 fonti). La copia corta
non produceva un bug visibile solo per fortuna: le 5 fonti mancanti
(bimhuis, concertgebouw, splendor, murmur, museumkaart) sono parole singole,
e il fallback `source.title()` le rendeva comunque bene. La prossima fonte con
un underscore nel nome sarebbe uscita come "Amsterdam_Alt".

Questo modulo e' la mappa unica lato Python: html_builder e build_pages
importano da qui. La copia in site/app.js resta dov'e' (il browser non puo'
importare Python), ma build_pages la rilegge a ogni run e avvisa se le due
divergono — vedi _check_js_labels_in_sync().
"""
from __future__ import annotations

import re

# Deve restare identica a SOURCE_LABELS in site/app.js.
SOURCE_LABELS: dict[str, str] = {
    "melkweg": "Melkweg",
    "amsterdam_alt": "Amsterdam Alternative",
    "ra": "Resident Advisor",
    "paradiso": "Paradiso",
    "murmur": "Murmur",
    "museumkaart": "Museumkaart",
    "gallery_viewer": "Gallery Viewer",
    "concertgebouw": "Concertgebouw",
    "muziekgebouw": "Muziekgebouw",
    "sitp": "Space is the Place",
    "offbeat": "Offbeat Amsterdam",
    "ndsm": "NDSM",
    "splendor": "Splendor",
    "bimhuis": "Bimhuis",
}

# Deve restare identica a TYPE_EMOJI in site/app.js.
TYPE_EMOJI: dict[str, str] = {
    "Concert": "🎸",
    "Club": "🎧",
    "Film": "🎬",
    "Festival": "🎪",
    "Expositie": "🖼️",
}


# ── Chi possiede un locale ─────────────────────────────────────────
# Le fonti si dividono in due specie. Alcune coprono UN locale e lo coprono
# tutto (paradiso, melkweg, bimhuis...); altre sono generaliste e rastrellano
# mezza citta' (ra, amsterdam_alt, offbeat, sitp). Quando una generalista
# incontra un locale che ha gia' la sua fonte dedicata, quegli eventi sono
# doppioni e li salta: la fonte dedicata li ha piu' completi e li aggiorna.
#
# Non e' un dettaglio estetico. dedup.py collassa solo a parita' di giorno E
# di ora, e due fonti sullo stesso concerto l'ora la scrivono diversa (porte
# contro inizio musica), quindi il doppione a valle NON viene assorbito: si
# vede sul sito. Va evitato qui.
#
# Le chiavi sono in forma `venue_key()`, che canonicalizza la SALA: cosi'
# "Paradiso (Tolhuistuin)" e "Tolhuistuin" cadono sulla stessa voce.
#
# NDSM di proposito NON sta qui. Il collector `ndsm` legge il cartellone di
# ndsm.nl, ma sulla werf succedono cose che quel cartellone non elenca —
# verificato: WIEL KLANKEN, raccolto da `sitp`, su ndsm.nl non c'e'. Dare a
# `ndsm` la proprieta' dell'intera werf farebbe sparire quegli eventi.
VENUE_OWNER: dict[str, str] = {
    "paradiso": "paradiso",
    "tolhuistuin": "paradiso",
    "bitterzoet": "paradiso",
    "parallel": "paradiso",
    "melkweg": "melkweg",
    "concertgebouw": "concertgebouw",
    "het concertgebouw": "concertgebouw",
    "muziekgebouw": "muziekgebouw",
    "muziekgebouw aan t ij": "muziekgebouw",
    "splendor": "splendor",
    "bimhuis": "bimhuis",
}


def venue_owner(venue: str, exclude: str = "") -> str:
    """La fonte dedicata che copre gia' questo locale, o '' se e' libero.

    `exclude` e' la fonte che sta chiedendo: un collector non e' doppione di
    se stesso.
    """
    from venues import venue_key  # import qui: venues non deve dipendere da labels

    owner = VENUE_OWNER.get(venue_key(venue or ""))
    return "" if not owner or owner == exclude else owner


def owner_in_text(text: str, exclude: str = "") -> str:
    """Come venue_owner(), ma per le fonti che il locale non lo danno.

    Alcune fonti scrivono la sede solo dentro un testo libero. Qui si cerca
    il nome del locale nel testo, su confine di parola.

    venues.py mette in guardia dal match per sottostringa, e ha ragione: un
    `LIKE '%ITA%'` durante l'analisi aveva agganciato "De Nieuwe Anita". La
    differenza e' che qui gli aghi non sono arbitrari, sono gli undici nomi
    qui sopra — tutti lunghi e distintivi — e il confine di parola impedisce
    che "parallel" si prenda "parallelweg".

    Il confine e' sulle sole LETTERE, non `\\b`: le descrizioni incollano
    l'ora al locale senza spazio ("18.30BIMHUIS, Amsterdam"), e fra "0" e "b"
    `\\b` non vede nessun confine.
    """
    low = (text or "").lower()
    for key, owner in VENUE_OWNER.items():
        if owner == exclude:
            continue
        if re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", low):
            return owner
    return ""


def source_label(source: str) -> str:
    """Nome leggibile della fonte. Fallback: il codice con l'iniziale maiuscola."""
    return SOURCE_LABELS.get(source, (source or "").replace("_", " ").title())


def type_emoji(event_type: str) -> str:
    """Emoji del tipo di evento. Fallback: 📌, come in app.js."""
    return TYPE_EMOJI.get(event_type, "📌")
