#!/usr/bin/env python3
"""Export events DB to JSON for the static GitHub Pages site."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from db import init_db, get_upcoming_events
from discovery import DiscoveryEngine

log = logging.getLogger(__name__)

SITE_DIR = Path(__file__).resolve().parent / "site"
DATA_OUTPUT = SITE_DIR / "data"


def export_events_json(days: int = 45) -> Path:
    """Export upcoming events to a JSON file for the static site."""
    init_db()
    events = get_upcoming_events(days=days)
    discovery = DiscoveryEngine()

    events_list = []
    all_venues = set()
    all_genres = set()
    all_types = set()

    for ev in events:
        artists_to_check = ev.artists if ev.artists else [ev.title]
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
            "venue": ev.venue or "",
            "venue_address": ev.venue_address or "",
            "event_type": ev.event_type or "",
            "artists": ev.artists or [],
            "genres": ev.genres or [],
            "price_min": ev.price_min,
            "price_max": ev.price_max,
            "tickets_url": ev.tickets_url or "",
            "tickets_status": ev.tickets_status or "",
            "source": ev.source,
            "source_url": ev.source_url,
            "match": match_data,
        })

        if ev.venue:
            all_venues.add(ev.venue)
        all_genres.update(g for g in ev.genres if g)
        if ev.event_type:
            all_types.add(ev.event_type)

    output = {
        "generated_at": datetime.now().isoformat(),
        "event_count": len(events_list),
        "events": events_list,
        "venues": sorted(all_venues),
        "genres": sorted(all_genres),
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
