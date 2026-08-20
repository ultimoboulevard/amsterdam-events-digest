#!/usr/bin/env python3
"""Export events DB to JSON for the static GitHub Pages site."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from artists import artists_for
import collections
from labels import GENRE_MIN_COUNT, canonical_type, is_genre
import venues as venues_mod
from db import init_db, get_upcoming_events
from dedup import collapse_duplicates
from discovery import DiscoveryEngine

log = logging.getLogger(__name__)

SITE_DIR = Path(__file__).resolve().parent / "site"
DATA_OUTPUT = SITE_DIR / "data"


# 135 giorni copre fino a fine dicembre. Con 45 il calendario si fermava al
# 1 ottobre e teneva fuori 1.211 eventi, fra cui i 403 della settimana
# dell'Amsterdam Dance Event (19-26 ottobre, picco 103 il giorno 23) —
# in un mese, ottobre, che da solo ne ha 706.
EXPORT_DAYS = 135


def export_events_json(days: int = EXPORT_DAYS) -> Path:
    """Export upcoming events to a JSON file for the static site."""
    init_db()
    events = collapse_duplicates(get_upcoming_events(days=days))
    discovery = DiscoveryEngine()

    # Un locale scritto in due modi resta due voci nel menu a tendina: il dedup degli
    # EVENTI usa venue_key(), ma il filtro legge la stringa grezza. Qui si sceglie UNA
    # scrittura per locale (venues.display_map), cosi' "50 HERTZ" e "50:Hertz" diventano
    # una voce sola. Il database conserva il grezzo: quella resta la verita' della fonte,
    # e la normalizzazione e' una scelta di presentazione, reversibile.
    venue_display = venues_mod.display_map(ev.venue for ev in events)

    events_list = []
    all_venues = set()
    all_genres = set()
    genre_counts: collections.Counter = collections.Counter()
    all_types = set()

    for ev in events:
        # Il titolo intero NON e' piu' il ripiego: se lo scraper non da'
        # artisti, artists_for() prova quelli estratti dal titolo e messi
        # in cache da enrich_artists.py. Sola lettura, nessuna rete.
        artists_to_check = artists_for(ev)
        match_res = discovery.evaluate_event(artists_to_check)
        match_data = None
        if match_res["match"]:
            match_data = {
                "type": match_res["type"],
                "reason": match_res["reason"],
                "score": match_res.get("score", 0),
            }

        events_list.append({
            "id": ev.dedup_key,
            "title": ev.title,
            "date": ev.date.isoformat() if ev.date else None,
            "date_end": ev.date_end.isoformat() if ev.date_end else None,
            "venue": venue_display.get(ev.venue, ev.venue) or "",
            "event_type": canonical_type(ev.event_type or ""),
            "artists": ev.artists or [],
            "genres": [g for g in (ev.genres or []) if is_genre(g)],
            # venue_address, price_min, price_max e tickets_url NON sono qui:
            # site/app.js non li legge (zero riferimenti), e triplicare la
            # finestra da 45 a 135 giorni triplicava anche il JSON che ogni
            # visitatore scarica prima di vedere un evento. Le pagine statiche
            # in build_pages.py leggono dal DB, quindi il prezzo lo hanno
            # comunque per il JSON-LD. Rimetterli e' una riga.
            "tickets_status": ev.tickets_status or "",
            "source": ev.source,
            "source_url": ev.source_url,
            "match": match_data,
        })

        if ev.venue:
            all_venues.add(venue_display.get(ev.venue, ev.venue))
        all_genres.update(g for g in ev.genres if is_genre(g))
        genre_counts.update(g.strip().lower() for g in ev.genres if is_genre(g))
        if ev.event_type:
            all_types.add(canonical_type(ev.event_type))

    output = {
        "generated_at": datetime.now().isoformat(),
        "event_count": len(events_list),
        "events": events_list,
        "venues": sorted(all_venues),
        "genres": sorted(g for g in all_genres
                             if genre_counts[g.strip().lower()] >= GENRE_MIN_COUNT),
        "event_types": sorted(all_types),
    }

    DATA_OUTPUT.mkdir(parents=True, exist_ok=True)
    out_path = DATA_OUTPUT / "events.json"
    out_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    log.info("Exported %d events to %s", len(events_list), out_path)
    return out_path


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(name)-25s │ %(levelname)-5s │ %(message)s",
        datefmt="%H:%M:%S",
    )
    path = export_events_json()
    print(f"\n✅ Exported to {path}")
