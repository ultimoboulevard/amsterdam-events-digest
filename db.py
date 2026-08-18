"""
SQLite persistence layer for events.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

from config import DB_PATH, DATA_DIR
from models import Event, CREATE_EVENTS_TABLE

log = logging.getLogger(__name__)


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Return a connection with row_factory set."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def _connection(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    """
    Connessione con commit/rollback E chiusura.

    `with sqlite3.connect(...)` da solo gestisce solo la transazione: il file
    descriptor resta aperto. Aprire una connessione per riga esauriva il soft
    limit di macOS (256 fd) e SQLite lo riportava come "unable to open
    database file". Ogni accesso al DB passa da qui.
    """
    conn = _connect(db_path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    """Create the events table if it doesn't exist."""
    with _connection(db_path) as conn:
        conn.executescript(CREATE_EVENTS_TABLE)
    log.info("Database initialized at %s", db_path)


def upsert_event(
    event: Event,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """
    Insert or update an event by dedup_key.
    Returns True if a new row was inserted, False if updated.

    Passando `conn` si riusa una connessione esistente (vedi upsert_many);
    senza, ne apre e chiude una per la singola scrittura.
    """
    if conn is None:
        with _connection(db_path) as own_conn:
            return _upsert_event_on(event, own_conn)
    return _upsert_event_on(event, conn)


def _upsert_event_on(event: Event, conn: sqlite3.Connection) -> bool:
    """Corpo dell'upsert su una connessione già aperta."""
    d = event.to_dict()
    existing = conn.execute(
        "SELECT dedup_key FROM events WHERE dedup_key = ?",
        (d["dedup_key"],),
    ).fetchone()

    if existing:
        conn.execute(
            """UPDATE events SET
                source=?, source_id=?, source_url=?, title=?, date=?,
                date_end=?, venue=?, venue_address=?, event_type=?,
                artists=?, genres=?, description=?, image_url=?,
                price_min=?, price_max=?, tickets_url=?, tickets_status=?,
                attending_count=?, scraped_at=?, updated_at=datetime('now')
            WHERE dedup_key=?""",
            (
                d["source"], d["source_id"], d["source_url"], d["title"],
                d["date"], d["date_end"], d["venue"], d["venue_address"],
                d["event_type"], d["artists"], d["genres"], d["description"],
                d["image_url"], d["price_min"], d["price_max"],
                d["tickets_url"], d["tickets_status"], d["attending_count"],
                d["scraped_at"], d["dedup_key"],
            ),
        )
        return False
    else:
        conn.execute(
            """INSERT INTO events (
                dedup_key, source, source_id, source_url, title, date,
                date_end, venue, venue_address, event_type, artists, genres,
                description, image_url, price_min, price_max, tickets_url,
                tickets_status, attending_count, scraped_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                d["dedup_key"], d["source"], d["source_id"], d["source_url"],
                d["title"], d["date"], d["date_end"], d["venue"],
                d["venue_address"], d["event_type"], d["artists"],
                d["genres"], d["description"], d["image_url"],
                d["price_min"], d["price_max"], d["tickets_url"],
                d["tickets_status"], d["attending_count"], d["scraped_at"],
            ),
        )
        return True


def upsert_many(events: list[Event], db_path: Path = DB_PATH) -> tuple[int, int]:
    """Upsert a list of events. Returns (inserted, updated)."""
    inserted = updated = 0
    # Una sola connessione per l'intero batch: aprirne una per evento
    # esauriva i file descriptor (vedi _connection).
    with _connection(db_path) as conn:
        for ev in events:
            if upsert_event(ev, db_path, conn=conn):
                inserted += 1
            else:
                updated += 1
    log.info("Upserted %d events: %d new, %d updated", len(events), inserted, updated)
    return inserted, updated


def get_upcoming_events(
    days: int = 14,
    source: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> list[Event]:
    """Fetch events from today up to `days` ahead."""
    today = datetime.now().strftime("%Y-%m-%d")
    end = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

    query = "SELECT * FROM events WHERE date >= ? AND date <= ?"
    params: list = [today, end]

    if source:
        query += " AND source = ?"
        params.append(source)

    query += " ORDER BY date ASC"

    with _connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()

    return [Event.from_row(dict(r)) for r in rows]


def get_all_events(db_path: Path = DB_PATH) -> list[Event]:
    """Fetch all events ordered by date."""
    with _connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM events ORDER BY date ASC").fetchall()
    return [Event.from_row(dict(r)) for r in rows]


def get_stats(db_path: Path = DB_PATH) -> dict:
    """Return basic stats about the database."""
    with _connection(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        sources = conn.execute(
            "SELECT source, COUNT(*) as cnt FROM events GROUP BY source"
        ).fetchall()
        venues = conn.execute(
            "SELECT venue, COUNT(*) as cnt FROM events GROUP BY venue ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
    return {
        "total": total,
        "by_source": {r["source"]: r["cnt"] for r in sources},
        "top_venues": {r["venue"]: r["cnt"] for r in venues},
    }
