"""
Gallery Viewer collector — art exhibitions from galleryviewer.com (Amsterdam).

Uses the public REST API at api.galleryviewer.com which powers the
React SPA frontend.  City ID 391 = Amsterdam.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

import httpx

from collectors import BaseCollector
from config import HEADERS, REQUEST_TIMEOUT
from models import Event

log = logging.getLogger(__name__)

API_URL = "https://api.galleryviewer.com/gvapp/exhibitions"
DETAIL_URL_TEMPLATE = "https://galleryviewer.com/en/exhibition/{id}"

# Amsterdam city filter ID (discovered from the site's __PRELOADED_STATE__)
AMSTERDAM_CITY_ID = 391

DEFAULT_PARAMS = {
    "tag": "general",
    "visible": "true",
    "ordering": "+date_to",
    "zero_price": "1",
    "past": "false",
    "pagination_type": "page",
    "city": str(AMSTERDAM_CITY_ID),
    "limit": "100",
    "page": "1",
}


def _strip_html(text: str) -> str:
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


class GalleryViewerCollector(BaseCollector):
    """Fetch current art gallery exhibitions in Amsterdam."""

    name = "gallery_viewer"

    def collect(self) -> list[Event]:
        log.info("Collecting exhibitions from Gallery Viewer (Amsterdam)…")
        events: list[Event] = []

        page = 1
        while True:
            batch, has_next = self._fetch_page(page)
            if batch is None:
                break  # network error
            events.extend(batch)
            if not has_next:
                break
            page += 1

        log.info("Gallery Viewer: collected %d exhibitions", len(events))
        return events

    # ── HTTP ───────────────────────────────────────────────────────

    def _fetch_page(self, page_num: int) -> tuple[list[Event] | None, bool]:
        """Fetch one page of results. Returns (events, has_next_page)."""
        params = {**DEFAULT_PARAMS, "page": str(page_num)}
        headers = {**HEADERS, "Accept": "application/json"}
        try:
            resp = httpx.get(
                API_URL,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("Gallery Viewer page %d fetch failed: %s", page_num, exc)
            return None, False

        data = resp.json()
        results = data.get("results", [])
        has_next = data.get("next") is not None

        events = []
        for item in results:
            ev = self._parse_item(item)
            if ev:
                events.append(ev)

        log.info("Gallery Viewer page %d: %d exhibitions", page_num, len(events))
        return events, has_next

    # ── Item parsing ───────────────────────────────────────────────

    def _parse_item(self, item: dict) -> Event | None:
        """Convert a single API result dict into an Event."""
        try:
            exhibition_id = item.get("id")
            title = item.get("title", "").strip()
            if not title or not exhibition_id:
                return None

            # Dates
            date_from = self._parse_date(item.get("date_from"))
            date_to = self._parse_date(item.get("date_to"))
            if date_from is None:
                return None

            # Gallery / venue
            gallery_name = item.get("gallery_exhibition_name", "")
            city = item.get("city", "Amsterdam")
            country = item.get("country", "")
            venue_address = f"{city}, {country}" if country else city

            # Artists
            artists = []
            for artist_obj in item.get("artists", []):
                name = artist_obj.get("name", "").strip()
                if name:
                    artists.append(name)

            # Description (prefer English, fall back to Dutch)
            desc_obj = item.get("description", {})
            description = _strip_html(
                desc_obj.get("en") or desc_obj.get("nl") or ""
            )

            # Image
            image_url = item.get("image", "")
            if not image_url:
                images = item.get("images", [])
                if images:
                    image_url = images[0].get("image", "")

            source_url = DETAIL_URL_TEMPLATE.format(id=exhibition_id)

            return Event(
                source=self.name,
                source_id=str(exhibition_id),
                source_url=source_url,
                title=title,
                date=date_from,
                date_end=date_to,
                venue=gallery_name,
                venue_address=venue_address,
                event_type="Expositie",
                artists=artists,
                description=description[:500] if description else "",
                image_url=image_url,
                tickets_status="available",
            )
        except Exception as exc:
            log.warning("Failed to parse Gallery Viewer item: %s", exc)
            return None

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _parse_date(date_str: str | None) -> datetime | None:
        """Parse an ISO date string (YYYY-MM-DD) into a datetime."""
        if not date_str:
            return None
        try:
            return datetime.strptime(date_str[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return None
