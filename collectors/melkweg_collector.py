"""
Melkweg.nl event collector.

Melkweg serves a full server-side-rendered agenda page.
Each event card contains: type (Concert/Club/Film/Festival), title,
artists/lineup, genres, date, and ticket status.
No JavaScript rendering needed — pure httpx + BeautifulSoup.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup, Tag

from collectors import BaseCollector
from config import HEADERS, MELKWEG_AGENDA_URL, REQUEST_TIMEOUT
from models import Event

log = logging.getLogger(__name__)

# ── Dutch month abbreviations → month number ──────────────────────
_NL_MONTHS = {
    "jan": 1, "feb": 2, "mrt": 3, "apr": 4, "mei": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}

# ── Dutch day-of-week abbreviations (for stripping) ───────────────
_NL_DAYS = {"ma", "di", "wo", "do", "vr", "za", "zo"}


def _parse_date(date_text: str) -> datetime | None:
    """
    Parse a Dutch date like 'vr 08 mei' or 'za 09 mei' into a datetime.
    Year is inferred: if the date has already passed this year, assume next year.
    """
    parts = date_text.lower().strip().split()
    if not parts:
        return None

    # Strip day-of-week prefix if present
    if parts[0] in _NL_DAYS:
        parts = parts[1:]

    if len(parts) < 2:
        return None

    try:
        day = int(parts[0])
        month = _NL_MONTHS.get(parts[1])
        if not month:
            return None

        now = datetime.now()
        year = now.year
        candidate = datetime(year, month, day)

        # If the date is more than 30 days in the past, assume next year
        if candidate < now - __import__("datetime").timedelta(days=30):
            candidate = datetime(year + 1, month, day)

        return candidate
    except (ValueError, IndexError):
        return None


def _extract_ticket_status(text: str) -> str:
    """Detect ticket status from card text."""
    t = text.lower()
    if "uitverkocht" in t:
        return "sold_out"
    if "afgelast" in t:
        return "cancelled"
    if "verplaatst" in t:
        return "rescheduled"
    if "gratis" in t:
        return "free"
    return "available"


class MelkwegCollector(BaseCollector):
    name = "melkweg"

    def collect(self) -> list[Event]:
        """Scrape Melkweg's English agenda page."""
        log.info("Collecting events from Melkweg...")

        try:
            resp = httpx.get(
                MELKWEG_AGENDA_URL,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.error("Failed to fetch Melkweg agenda: %s", e)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        events: list[Event] = []

        # The agenda is an ordered list; each <li> may contain one or more
        # event links.  Date headers appear as text within the list items.
        current_date: datetime | None = None

        # Find all list items inside the event list
        event_links = soup.select('a[href*="/agenda/"]')
        seen_urls: set[str] = set()

        for link in event_links:
            href = link.get("href", "")
            if not href or href in seen_urls:
                continue
            if "/agenda/" not in href or href.rstrip("/").endswith("/agenda"):
                continue

            # Build full URL
            if href.startswith("/"):
                full_url = f"https://www.melkweg.nl{href}"
            else:
                full_url = href
            seen_urls.add(href)

            # Extract text content from the link
            text = link.get_text(separator="\n", strip=True)
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            if not lines:
                continue

            # Parse event type + title from the text
            event_type = ""
            title = ""
            artists_raw = ""
            genres_raw = ""

            # Typical pattern:
            # Line 0: "Concert" or "Club" or "Film" etc.  (event type)
            # Line 1: Title
            # Line 2: Support / lineup info
            # Line 3+: Genres · separated by ·
            # Last lines might have status (Uitverkocht, Gratis, etc.)

            known_types = {"Concert", "Club", "Film", "Festival", "Expositie"}
            idx = 0

            if lines[idx] in known_types:
                event_type = lines[idx]
                idx += 1

            if idx < len(lines):
                title = lines[idx]
                idx += 1

            # Remaining lines: try to find genres (contain ·) and artist info
            remaining = lines[idx:]
            genre_line = ""
            extra_lines = []

            for line in remaining:
                if "·" in line or " · " in line:
                    genre_line = line
                else:
                    extra_lines.append(line)

            genres = [g.strip() for g in genre_line.split("·") if g.strip()] if genre_line else []

            # Artists: look for lines with " / " separators (lineup)
            artists = []
            for line in extra_lines:
                if " / " in line:
                    artists = [a.strip() for a in line.split("/") if a.strip()]
                    break
                elif line.startswith("Support:"):
                    artists = [a.strip() for a in line.replace("Support:", "").split("/") if a.strip()]

            # Ticket status
            full_text = " ".join(lines)
            ticket_status = _extract_ticket_status(full_text)

            # Extract date from the slug (pattern: DD-MM-YYYY at end)
            slug = href.rstrip("/").split("/")[-1]
            date_match = re.search(r'(\d{2})-(\d{2})-(\d{4})', slug)
            event_date = None
            if date_match:
                try:
                    day, month, year = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
                    event_date = datetime(year, month, day)
                except ValueError:
                    pass

            if not event_date:
                # Try to find date from surrounding context
                # Skip events without a parseable date
                continue

            if not title:
                continue

            ev = Event(
                source=self.name,
                source_id=slug,
                source_url=full_url,
                title=title,
                date=event_date,
                venue="Melkweg",
                venue_address="Lijnbaansgracht 234A, Amsterdam",
                event_type=event_type,
                artists=artists,
                genres=genres,
                tickets_status=ticket_status,
            )
            events.append(ev)

        log.info("Melkweg: collected %d events", len(events))
        return events
