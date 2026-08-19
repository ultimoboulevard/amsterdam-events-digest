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


def source_label(source: str) -> str:
    """Nome leggibile della fonte. Fallback: il codice con l'iniziale maiuscola."""
    return SOURCE_LABELS.get(source, (source or "").replace("_", " ").title())


def type_emoji(event_type: str) -> str:
    """Emoji del tipo di evento. Fallback: 📌, come in app.js."""
    return TYPE_EMOJI.get(event_type, "📌")
