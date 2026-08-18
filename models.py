"""
Unified event data model and SQLite schema.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

from venues import venue_key


@dataclass
class Event:
    """Normalized event record from any source."""

    source: str                          # "melkweg" | "amsterdam_alt" | "ra" | ...
    source_id: str                       # ID or slug on the source platform
    source_url: str                      # Direct link to event page

    title: str
    date: datetime                       # Start date/time (UTC or local)
    date_end: Optional[datetime] = None

    venue: str = ""
    venue_address: str = ""

    event_type: str = ""                 # "Concert" | "Club" | "Film" | "Festival" | ...
    artists: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    description: str = ""
    image_url: str = ""

    price_min: Optional[float] = None
    price_max: Optional[float] = None
    tickets_url: str = ""
    tickets_status: str = ""             # "available" | "sold_out" | "cancelled" | "free"

    attending_count: Optional[int] = None

    scraped_at: Optional[datetime] = None
    dedup_key: str = ""

    def __post_init__(self):
        if not self.scraped_at:
            self.scraped_at = datetime.utcnow()
        if not self.dedup_key:
            self.dedup_key = self.compute_dedup_key()

    def compute_dedup_key(self) -> str:
        """
        Chiave deterministica: titolo + giorno + SALA CANONICA.

        Il terzo campo passa da `venue.lower()` alla forma canonica di
        venues.py, e questo e' tutto il cambiamento. Il locale grezzo e' il
        campo su cui le fonti non vanno d'accordo: la fonte `paradiso` scrive
        "Paradiso (Tolhuistuin)", RA scrive "Tolhuistuin", e "Muziekgebouw aan
        't IJ" contro "aan t' IJ" differiscono per la posizione di un
        apostrofo. Ogni grafia era una chiave diversa, quindi una riga in piu':
        misurate 185 righe doppie su 8.962, di cui 90 su eventi futuri.

        Si canonicalizza la SALA, non l'operatore. Appiattire su "Paradiso"
        sarebbe l'errore opposto: il 22/08 "SOBER in the garden" e' a
        Tolhuistuin E a Parallel, due url RA diverse, due eventi. Vedi
        venues.py per il perche' esteso.

        ATTENZIONE — questa e' la PRIMARY KEY della tabella (vedi sotto).
        Cambiare questa formula senza migrare le righe gia' salvate significa
        che il collect successivo non riconosce piu' niente, re-inserisce
        tutto e lascia le vecchie orfane. La migrazione che accompagna questa
        formula e' migrate_dedup_key.py; se la formula cambia ancora, si
        rifa' la stessa strada.
        """
        normalized = (
            self.title.lower().strip()[:60]
            + "|"
            + self.date.strftime("%Y-%m-%d")
            + "|"
            + venue_key(self.venue)[:30]
        )
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["artists"] = "|".join(self.artists)
        d["genres"] = "|".join(self.genres)
        d["date"] = self.date.isoformat() if self.date else None
        d["date_end"] = self.date_end.isoformat() if self.date_end else None
        d["scraped_at"] = self.scraped_at.isoformat() if self.scraped_at else None
        return d

    @classmethod
    def from_row(cls, row: dict) -> Event:
        """Reconstruct Event from a database row."""
        return cls(
            source=row["source"],
            source_id=row["source_id"],
            source_url=row["source_url"],
            title=row["title"],
            date=datetime.fromisoformat(row["date"]) if row["date"] else datetime.min,
            date_end=datetime.fromisoformat(row["date_end"]) if row.get("date_end") else None,
            venue=row.get("venue", ""),
            venue_address=row.get("venue_address", ""),
            event_type=row.get("event_type", ""),
            artists=row.get("artists", "").split("|") if row.get("artists") else [],
            genres=row.get("genres", "").split("|") if row.get("genres") else [],
            description=row.get("description", ""),
            image_url=row.get("image_url", ""),
            price_min=row.get("price_min"),
            price_max=row.get("price_max"),
            tickets_url=row.get("tickets_url", ""),
            tickets_status=row.get("tickets_status", ""),
            attending_count=row.get("attending_count"),
            scraped_at=datetime.fromisoformat(row["scraped_at"]) if row.get("scraped_at") else None,
            dedup_key=row.get("dedup_key", ""),
        )


# ── SQLite Schema ──────────────────────────────────────────────────
CREATE_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS events (
    dedup_key       TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    source_url      TEXT NOT NULL,
    title           TEXT NOT NULL,
    date            TEXT NOT NULL,
    date_end        TEXT,
    venue           TEXT,
    venue_address   TEXT,
    event_type      TEXT,
    artists         TEXT,
    genres          TEXT,
    description     TEXT,
    image_url       TEXT,
    price_min       REAL,
    price_max       REAL,
    tickets_url     TEXT,
    tickets_status  TEXT,
    attending_count INTEGER,
    scraped_at      TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_date ON events(date);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);
CREATE INDEX IF NOT EXISTS idx_events_venue ON events(venue);
"""
