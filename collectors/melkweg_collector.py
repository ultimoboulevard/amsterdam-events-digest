"""
Melkweg.nl event collector.

Melkweg serves a Next.js SSR page with a __NEXT_DATA__ JSON blob
containing all events with structured data: name, startDate/startTime,
endDate, profile (event type), genres, artists, ticket flags, and images.

Parsing this JSON gives us accurate start times (UTC → local)
instead of the midnight-only dates the old HTML scraper produced.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta

import httpx
from bs4 import BeautifulSoup

from collectors import BaseCollector
from config import HEADERS, MELKWEG_AGENDA_URL, REQUEST_TIMEOUT
from models import Event

log = logging.getLogger(__name__)

# Amsterdam timezone offset: CET = UTC+1, CEST = UTC+2
# We use a simple approach: the JSON gives UTC timestamps, we convert
# to local time using the offset embedded in the date itself.
_CET = timezone(timedelta(hours=1))
_CEST = timezone(timedelta(hours=2))


def _utc_to_amsterdam(dt: datetime) -> datetime:
    """Convert a UTC datetime to Amsterdam local time (CET/CEST).

    Uses a simplified DST rule: last Sunday of March → last Sunday of October.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    year = dt.year

    # Last Sunday in March
    march_last = datetime(year, 3, 31, 1, 0, tzinfo=timezone.utc)
    march_last -= timedelta(days=march_last.weekday() + 1)  # back to Sunday
    if march_last.weekday() != 6:  # ensure it's Sunday
        march_last -= timedelta(days=(march_last.weekday() + 1) % 7)

    # Last Sunday in October
    oct_last = datetime(year, 10, 31, 1, 0, tzinfo=timezone.utc)
    oct_last -= timedelta(days=oct_last.weekday() + 1)
    if oct_last.weekday() != 6:
        oct_last -= timedelta(days=(oct_last.weekday() + 1) % 7)

    # Between last Sunday of March and last Sunday of October → CEST (UTC+2)
    if march_last <= dt < oct_last:
        return dt.astimezone(_CEST).replace(tzinfo=None)
    else:
        return dt.astimezone(_CET).replace(tzinfo=None)


def _parse_iso_datetime(s: str | None) -> datetime | None:
    """Parse an ISO 8601 datetime string from the Melkweg API."""
    if not s:
        return None
    try:
        # Format: "2026-05-07T17:00:00.000000Z"
        s = s.rstrip("Z")
        if "." in s:
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f")
        else:
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc)
        return _utc_to_amsterdam(dt)
    except (ValueError, TypeError):
        return None


def _extract_ticket_status(attrs: dict) -> str:
    """Derive ticket status from boolean flags."""
    if attrs.get("isCancelled"):
        return "cancelled"
    if attrs.get("isMovedToNewDate"):
        return "rescheduled"
    if attrs.get("isSoldOut"):
        return "sold_out"
    if attrs.get("isFreeForMembers"):
        return "free"
    return "available"


def _parse_artists(raw: str | None) -> list[str]:
    """Parse the artists string into a list.

    Common formats:
      "Hüzyn / John Doe / Yan Lâle"
      "Support: Odhrán Murphy"
      "DOPE CAESAR / EMSIFLYBOKOE / GODSENDO"
    """
    if not raw:
        return []
    # Split on " / " separator
    parts = [a.strip() for a in raw.split("/") if a.strip()]
    return parts


class MelkwegCollector(BaseCollector):
    name = "melkweg"

    def collect(self) -> list[Event]:
        """Collect events from Melkweg via the __NEXT_DATA__ JSON blob."""
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

        # ── Extract structured JSON from __NEXT_DATA__ ────────────
        next_data_script = soup.find("script", id="__NEXT_DATA__")
        if not next_data_script or not next_data_script.string:
            log.error("Melkweg: __NEXT_DATA__ script tag not found, "
                       "falling back to HTML scraping")
            return self._collect_html_fallback(soup)

        try:
            data = json.loads(next_data_script.string)
            raw_events = (
                data["props"]["pageProps"]["pageData"]
                ["attributes"]["content"][0]["attributes"]["initialEvents"]
            )
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
            log.error("Melkweg: Failed to parse __NEXT_DATA__: %s", e)
            return self._collect_html_fallback(soup)

        # ── Build genre lookup (id → English name) ────────────────
        genre_map: dict[str, str] = {}
        for g in data.get("props", {}).get("pageProps", {}).get("genres", []):
            gid = str(g.get("id", ""))
            name_obj = g.get("attributes", {}).get("name", {})
            if isinstance(name_obj, dict):
                genre_map[gid] = name_obj.get("en", name_obj.get("nl", ""))
            elif isinstance(name_obj, str):
                genre_map[gid] = name_obj

        # ── Convert raw events to Event objects ───────────────────
        events: list[Event] = []

        for raw in raw_events:
            attrs = raw.get("attributes", {})

            name = attrs.get("name", "").strip()
            if not name:
                continue

            # Parse dates (UTC → Amsterdam local)
            start_dt = _parse_iso_datetime(attrs.get("startDate"))
            if not start_dt:
                continue

            end_dt = _parse_iso_datetime(attrs.get("endDate"))

            # Skip multi-day exhibition-type events that span months
            if attrs.get("isMultiDayEvent") and end_dt:
                delta = (end_dt - start_dt).days
                if delta > 7:
                    log.debug("Melkweg: skipping long-running event: %s (%d days)",
                              name, delta)
                    continue

            # Event type (profile)
            event_type = attrs.get("profile", "")

            # Build URL
            url_path = attrs.get("url", "")
            if url_path.startswith("/"):
                source_url = f"https://www.melkweg.nl{url_path}"
            else:
                source_url = url_path or MELKWEG_AGENDA_URL

            # Slug for source_id
            slug = url_path.rstrip("/").split("/")[-1] if url_path else str(raw.get("id", ""))

            # Genres: resolve IDs → names, then append tags
            genre_ids = attrs.get("genres", [])
            genres_resolved = [genre_map[str(gid)] for gid in genre_ids if str(gid) in genre_map]
            tags = attrs.get("tags", [])
            # Tags are the more specific genre labels (e.g. "Postpunk", "Experimental")
            # Use tags as primary genres; add resolved genre category if not redundant
            all_genres = list(tags)
            for g in genres_resolved:
                if g and g not in all_genres:
                    all_genres.append(g)

            # Artists
            artists_raw = attrs.get("artists", "")
            if isinstance(artists_raw, str):
                artists = _parse_artists(artists_raw)
            elif isinstance(artists_raw, list):
                artists = [str(a).strip() for a in artists_raw if str(a).strip()]
            else:
                artists = []

            # Ticket status
            ticket_status = _extract_ticket_status(attrs)

            # Image URL
            image_url = ""
            media = attrs.get("media", {})
            if isinstance(media, dict):
                featured = media.get("featuredImage", [])
                if featured and isinstance(featured, list) and featured[0]:
                    image_url = featured[0].get("filename", "")

            ev = Event(
                source=self.name,
                source_id=slug,
                source_url=source_url,
                title=name,
                date=start_dt,
                date_end=end_dt,
                venue="Melkweg",
                venue_address="Lijnbaansgracht 234A, Amsterdam",
                event_type=event_type,
                artists=artists,
                genres=all_genres,
                tickets_status=ticket_status,
                image_url=image_url,
            )
            events.append(ev)

        log.info("Melkweg: collected %d events (via __NEXT_DATA__)", len(events))
        return events

    # ── Fallback: original HTML scraping (no time data) ───────────
    def _collect_html_fallback(self, soup: BeautifulSoup) -> list[Event]:
        """Fallback HTML scraper if __NEXT_DATA__ is unavailable."""
        import re

        log.warning("Melkweg: using HTML fallback — event times will be midnight")
        events: list[Event] = []
        event_links = soup.select('a[href*="/agenda/"]')
        seen_urls: set[str] = set()

        for link in event_links:
            href = link.get("href", "")
            if not href or href in seen_urls:
                continue
            if "/agenda/" not in href or href.rstrip("/").endswith("/agenda"):
                continue

            if href.startswith("/"):
                full_url = f"https://www.melkweg.nl{href}"
            else:
                full_url = href
            seen_urls.add(href)

            text = link.get_text(separator="\n", strip=True)
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            if not lines:
                continue

            event_type = ""
            title = ""
            known_types = {"Concert", "Club", "Film", "Festival", "Expositie"}
            idx = 0

            if lines[idx] in known_types:
                event_type = lines[idx]
                idx += 1

            if idx < len(lines):
                title = lines[idx]
                idx += 1

            remaining = lines[idx:]
            genre_line = ""
            extra_lines = []

            for line in remaining:
                if "·" in line:
                    genre_line = line
                else:
                    extra_lines.append(line)

            genres = [g.strip() for g in genre_line.split("·") if g.strip()] if genre_line else []

            artists = []
            for line in extra_lines:
                if " / " in line:
                    artists = [a.strip() for a in line.split("/") if a.strip()]
                    break

            full_text = " ".join(lines)
            t = full_text.lower()
            if "uitverkocht" in t:
                ticket_status = "sold_out"
            elif "afgelast" in t:
                ticket_status = "cancelled"
            elif "verplaatst" in t:
                ticket_status = "rescheduled"
            elif "gratis" in t:
                ticket_status = "free"
            else:
                ticket_status = "available"

            slug = href.rstrip("/").split("/")[-1]
            date_match = re.search(r'(\d{2})-(\d{2})-(\d{4})', slug)
            event_date = None
            if date_match:
                try:
                    day, month, year = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
                    event_date = datetime(year, month, day)
                except ValueError:
                    pass

            if not event_date or not title:
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

        log.info("Melkweg (fallback): collected %d events", len(events))
        return events
