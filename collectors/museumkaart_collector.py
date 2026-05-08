"""
Museumkaart collector — exhibitions & events from museum.nl (Noord-Holland).

Uses the POST  /en/see-and-do/events/search  endpoint which returns
paginated HTML fragments.  We parse the rendered <li> cards via
BeautifulSoup.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from collectors import BaseCollector
from config import HEADERS, REQUEST_TIMEOUT
from models import Event

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.museum.nl/en/see-and-do/events/search"
BASE_URL = "https://www.museum.nl"
PAGE_SIZE = 20
MAX_PAGES = 10  # safety cap

# Month name → number
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


class MuseumkaartCollector(BaseCollector):
    """Scrape museum exhibitions & events for Noord-Holland."""

    name = "museumkaart"

    def collect(self) -> list[Event]:
        log.info("Collecting events from Museumkaart (Noord-Holland)…")
        events: list[Event] = []

        for page_idx in range(MAX_PAGES):
            cards = self._fetch_page(page_idx)
            if cards is None:
                break  # network error → stop

            for card in cards:
                ev = self._parse_card(card)
                if ev:
                    events.append(ev)

            if len(cards) < PAGE_SIZE:
                break  # last page

        log.info("Museumkaart: collected %d events", len(events))
        return events

    # ── HTTP ───────────────────────────────────────────────────────

    def _fetch_page(self, page_index: int) -> list | None:
        """POST for one page and return the <li> card elements."""
        headers = {
            **HEADERS,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": BASE_URL,
            "X-Requested-With": "XMLHttpRequest",
        }
        payload = {
            "Province": "Noord-Holland",
            "PageIndex": page_index,
            "PageSize": PAGE_SIZE,
        }
        try:
            resp = httpx.post(
                SEARCH_URL,
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("Museumkaart page %d fetch failed: %s", page_index, exc)
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("li.see-and-do-item")
        log.info("Museumkaart page %d: %d cards", page_index, len(cards))
        return cards

    # ── Card parsing ───────────────────────────────────────────────

    def _parse_card(self, card) -> Event | None:
        """Parse a single <li class='see-and-do-item'> into an Event."""
        try:
            # Link & slug
            a_tag = card.select_one("a[href]")
            if not a_tag:
                return None
            href = a_tag["href"]
            source_url = f"{BASE_URL}{href}" if href.startswith("/") else href

            # Title (first <component> with class heading-4)
            comps = card.select("component")
            if not comps:
                return None
            title = comps[0].get_text(strip=True)
            if not title:
                return None

            # Event type (second <component> — small-label)
            event_type = comps[1].get_text(strip=True) if len(comps) > 1 else ""

            # Date / venue from .item-attribute blocks
            date_text = ""
            venue_text = ""
            for attr_div in card.select(".item-attribute"):
                svg = attr_div.select_one("svg")
                svg_cls = (svg.get("class", [""])[0] if svg and svg.get("class") else "")
                p = attr_div.select_one("component")
                if not p:
                    continue
                txt = p.get_text(strip=True)
                if "time" in svg_cls:
                    date_text = txt
                elif "location" in svg_cls:
                    venue_text = txt
                elif "group" in svg_cls or "people" in svg_cls:
                    pass  # age group — skip

            # Parse date
            event_date, event_end = self._parse_date(date_text)
            if event_date is None:
                # Fall back to today so the event still appears
                event_date = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)

            # Image
            img = card.select_one("img")
            image_url = ""
            if img:
                image_url = img.get("data-src", "") or img.get("src", "")

            # Source ID from the card's id attribute or the URL slug
            source_id = card.get("id", "") or href.rstrip("/").rsplit("/", 1)[-1]

            # Split venue into name + city
            venue_name = venue_text

            return Event(
                source=self.name,
                source_id=source_id,
                source_url=source_url,
                title=title,
                date=event_date,
                date_end=event_end,
                venue=venue_name,
                event_type=event_type,
                image_url=image_url,
                tickets_status="available",
            )
        except Exception as exc:
            log.warning("Failed to parse Museumkaart card: %s", exc)
            return None

    # ── Date helpers ───────────────────────────────────────────────

    @staticmethod
    def _parse_date(text: str) -> tuple[datetime | None, datetime | None]:
        """
        Best-effort date parsing from the varied museum.nl formats:
          • "Until 17 May from 09:00 tot 18:00"
          • "Until 21 March 2027 from 09:00 tot 17:00"
          • "Until 21 June from 10 to 18 hours"
          • "From 10:00 tot 19:00 from 12 June until 30 August"
          • "Doorlopend, Multiple options"
          • "Weekly on Sunday, multiple options"
        Returns (event_date, date_end) — either or both can be None.
        """
        if not text:
            return None, None

        now = datetime.now()
        year = now.year

        # ── Pattern 1: "Until DD Month [YYYY] from HH:MM tot HH:MM"
        m = re.match(
            r"Until\s+(\d{1,2})\s+(\w+)(?:\s+(\d{4}))?\s+from\s+(\d{1,2}):?(\d{2})?\s+to[t ]",
            text, re.IGNORECASE,
        )
        if m:
            day, month_str, yr, hour, minute = m.groups()
            mon = _MONTHS.get(month_str.lower())
            if mon:
                y = int(yr) if yr else year
                # If the date already passed this year, bump to next year
                if not yr:
                    try:
                        candidate = datetime(y, mon, int(day))
                        if candidate.date() < now.date():
                            y += 1
                    except ValueError:
                        pass
                try:
                    return datetime(y, mon, int(day), int(hour), int(minute or 0)), None
                except ValueError:
                    pass

        # ── Pattern 1b: "Until DD Month [YYYY] from HH to HH hours"
        m = re.match(
            r"Until\s+(\d{1,2})\s+(\w+)(?:\s+(\d{4}))?\s+from\s+(\d{1,2})\s+to\s+(\d{1,2})\s+hours",
            text, re.IGNORECASE,
        )
        if m:
            day, month_str, yr, hour_start, _ = m.groups()
            mon = _MONTHS.get(month_str.lower())
            if mon:
                y = int(yr) if yr else year
                if not yr:
                    try:
                        candidate = datetime(y, mon, int(day))
                        if candidate.date() < now.date():
                            y += 1
                    except ValueError:
                        pass
                try:
                    return datetime(y, mon, int(day), int(hour_start), 0), None
                except ValueError:
                    pass

        # ── Pattern 2: "From HH:MM tot HH:MM from DD Month until DD Month"
        m = re.match(
            r"From\s+\d{1,2}:\d{2}\s+tot\s+\d{1,2}:\d{2}\s+from\s+(\d{1,2})\s+(\w+)\s+until\s+(\d{1,2})\s+(\w+)",
            text, re.IGNORECASE,
        )
        if m:
            d1, m1, d2, m2 = m.groups()
            mon1 = _MONTHS.get(m1.lower())
            mon2 = _MONTHS.get(m2.lower())
            if mon1 and mon2:
                y1 = year
                y2 = year
                try:
                    start = datetime(y1, mon1, int(d1))
                    if start.date() < now.date():
                        y1 += 1
                        y2 += 1
                    end = datetime(y2, mon2, int(d2))
                    if end < start:
                        y2 += 1
                        end = datetime(y2, mon2, int(d2))
                    return start, end
                except ValueError:
                    pass

        # ── Fallback: ongoing / recurring → return None
        return None, None
