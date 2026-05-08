"""
Splendor Amsterdam event collector.

Scrapes the agenda page at https://www.splendoramsterdam.com/agenda
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

import httpx
from bs4 import BeautifulSoup, Tag

from collectors import BaseCollector
from config import HEADERS, REQUEST_TIMEOUT
from models import Event

log = logging.getLogger(__name__)

SPLENDOR_BASE = "https://www.splendoramsterdam.com"
SPLENDOR_AGENDA_URL = SPLENDOR_BASE + "/agenda"

VENUE_NAME = "Splendor"
VENUE_ADDRESS = "Nieuwe Uilenburgerstraat 116, 1011 LX Amsterdam"

DUTCH_MONTHS = {
    "januari": 1,
    "februari": 2,
    "maart": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "augustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
}

class SplendorCollector(BaseCollector):
    name = "splendor"

    def collect(self) -> list[Event]:
        log.info("Collecting events from Splendor Amsterdam...")
        events: list[Event] = []

        try:
            resp = httpx.get(
                SPLENDOR_AGENDA_URL,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.error("Failed to fetch Splendor agenda: %s", e)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        wrappers = soup.select("div.event")
        
        if not wrappers:
            log.warning("Splendor: no div.event elements found")
            return []

        for wrapper in wrappers:
            ev = self._parse_wrapper(wrapper)
            if ev:
                events.append(ev)

        log.info("Splendor: collected %d events", len(events))
        return events

    def _parse_wrapper(self, wrapper: Tag) -> Optional[Event]:
        try:
            # Title & Subtitle & URL
            text_link = wrapper.select_one("a.text")
            if not text_link:
                return None
            
            href = text_link.get("href", "")
            if not href or "/agenda/" not in href:
                return None
            
            source_url = SPLENDOR_BASE + href if href.startswith("/") else href
            source_id = href.rstrip("/").split("/")[-1]

            title_span = text_link.select_one("span.title")
            title = title_span.get_text(strip=True) if title_span else ""
            if not title:
                return None

            # Subtitle might have artists
            sub_title_span = text_link.select_one("span.sub-title.top")
            bottom_sub_title_span = text_link.select_one("span.sub-title:not(.top)")
            
            artists_str = sub_title_span.get_text(strip=True) if sub_title_span else ""
            artists_str = artists_str.replace(" presenteert:", "").strip()
            
            artists = [artists_str] if artists_str else []
            
            desc_str = bottom_sub_title_span.get_text(strip=True) if bottom_sub_title_span else ""

            # Date
            date_span = wrapper.select_one("div.info span.date")
            event_date = self._parse_date(date_span.get_text(" ", strip=True) if date_span else "")
            if not event_date:
                return None
            
            if event_date.date() < datetime.now().date():
                return None

            # Tickets
            tickets_btn = wrapper.select_one("div.info a.button")
            tickets_url = tickets_btn.get("href", "") if tickets_btn else ""
            
            tickets_status = "available"
            if "uitverkocht" in tickets_url.lower() or (tickets_btn and "uitverkocht" in tickets_btn.get_text(strip=True).lower()):
                tickets_status = "sold_out"
            elif tickets_btn and "gratis" in tickets_btn.get_text(strip=True).lower():
                tickets_status = "free"

            return Event(
                source=self.name,
                source_id=source_id,
                source_url=source_url,
                title=title,
                date=event_date,
                venue=VENUE_NAME,
                venue_address=VENUE_ADDRESS,
                event_type="Concert",
                artists=artists,
                genres=[],
                description=desc_str,
                price_min=None,
                price_max=None,
                tickets_url=tickets_url,
                tickets_status=tickets_status,
            )
        except Exception as e:
            log.warning("Failed to parse Splendor wrapper: %s", e, exc_info=True)
            return None

    @staticmethod
    def _parse_date(date_str: str) -> Optional[datetime]:
        """Parse Dutch date string like 'Vr. 8 mei 2026 19:30 uur'."""
        if not date_str:
            return None
        
        # Lowercase and clean
        date_str = date_str.lower().replace("uur", "").strip()
        
        # Regex to find day, month word, year, optional time
        m = re.search(r'(\d{1,2})\s+([a-z]+)\s+(\d{4})(?:\s+(\d{1,2}:\d{2}))?', date_str)
        if not m:
            return None
            
        day_str, month_str, year_str, time_str = m.groups()
        month = DUTCH_MONTHS.get(month_str)
        if not month:
            return None
            
        try:
            day = int(day_str)
            year = int(year_str)
            hour, minute = 0, 0
            if time_str:
                hour, minute = map(int, time_str.split(':'))
            
            return datetime(year, month, day, hour, minute)
        except ValueError:
            return None
