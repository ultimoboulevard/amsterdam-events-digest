"""
Muziekgebouw aan 't IJ event collector.

Scrapes the server-side rendered agenda page (Peppered CMS).
The English agenda supports date-range filtering via query params:
    https://www.muziekgebouw.nl/en/agenda?start=YYYY-MM-DD&end=YYYY-MM-DD

DOM structure per event (within div.listItemWrapper):
    a.desc[href="/en/agenda/<slug>"]
        h3.title           → concert title
        div.subtitle       → artist/ensemble names
        div.top-date
            span.start     → "Fri 8 May 2026"
            span.time      → "20:15"
        div.tagline        → short description
        div.venue          → room name ("Grote Zaal")
    button.pricePopoverBtn → price summary ("€ 31,00–€ 39,00")
    a.status-uitverkocht   → sold-out indicator
    a.btn-order            → ticket purchase link
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

import httpx
from bs4 import BeautifulSoup, Tag

from collectors import BaseCollector
from config import HEADERS, REQUEST_TIMEOUT
from models import Event

log = logging.getLogger(__name__)

MUZIEKGEBOUW_BASE = "https://www.muziekgebouw.nl"
MUZIEKGEBOUW_AGENDA_URL = MUZIEKGEBOUW_BASE + "/en/agenda"

# Venue constants
VENUE_NAME = "Muziekgebouw aan 't IJ"
VENUE_ADDRESS = "Piet Heinkade 1, 1019 BR Amsterdam"


class MuziekgebouwCollector(BaseCollector):
    name = "muziekgebouw"

    def collect(self) -> list[Event]:
        """Scrape upcoming events from the Muziekgebouw English agenda."""
        log.info("Collecting events from Muziekgebouw aan 't IJ...")

        today = datetime.now().strftime("%Y-%m-%d")
        end_date = (datetime.now() + timedelta(days=45)).strftime("%Y-%m-%d")
        
        events: list[Event] = []
        page = 1
        max_pages = 20
        
        while page <= max_pages:
            url = f"{MUZIEKGEBOUW_AGENDA_URL}?start={today}&end={end_date}&page={page}"
            try:
                resp = httpx.get(
                    url,
                    headers=HEADERS,
                    timeout=REQUEST_TIMEOUT,
                    follow_redirects=True,
                )
                resp.raise_for_status()
            except httpx.HTTPError as e:
                log.error("Failed to fetch Muziekgebouw agenda page %d: %s", page, e)
                break

            soup = BeautifulSoup(resp.text, "html.parser")

            # Each event lives inside a div.listItemWrapper
            wrappers = soup.select("div.listItemWrapper")
            if not wrappers:
                if page == 1:
                    log.warning("Muziekgebouw: no listItemWrapper divs found in HTML")
                break

            for wrapper in wrappers:
                ev = self._parse_wrapper(wrapper)
                if ev:
                    events.append(ev)
                    
            log.debug("Muziekgebouw: collected %d events from page %d", len(wrappers), page)
            
            # If there's no 'Next' pagination link, stop
            next_link = soup.select_one("a.next")
            if not next_link:
                break
                
            page += 1

        log.info("Muziekgebouw: collected %d events total", len(events))
        return events

    # ──────────────────────────────────────────────────────────────────
    def _parse_wrapper(self, wrapper: Tag) -> Optional[Event]:
        """Parse one div.listItemWrapper into an Event."""
        try:
            # ── Link & URL ──────────────────────────────────────────
            desc_link = wrapper.select_one("a.desc")
            if not desc_link:
                return None

            href = desc_link.get("href", "")
            if not href or "/agenda/" not in href:
                return None

            if href.startswith("/"):
                source_url = MUZIEKGEBOUW_BASE + href
            else:
                source_url = href

            # Source ID = slug from URL (e.g. "diamanda-la-berge-dramm-nwj2")
            source_id = href.rstrip("/").split("/")[-1]

            # ── Title ───────────────────────────────────────────────
            h3 = desc_link.select_one("h3.title")
            title = h3.get_text(strip=True) if h3 else ""
            if not title:
                return None

            # ── Subtitle (artists / ensemble) ───────────────────────
            subtitle_div = desc_link.select_one("div.subtitle")
            subtitle = subtitle_div.get_text(strip=True) if subtitle_div else ""

            # Build artists list from subtitle
            artists = self._parse_artists(subtitle)

            # ── Date & Time ─────────────────────────────────────────
            top_date = desc_link.select_one("div.top-date")
            event_date = self._parse_date_time(top_date)
            if not event_date:
                return None

            # Skip past events
            if event_date.date() < datetime.now().date():
                return None

            # ── Room / Location ─────────────────────────────────────
            venue_div = desc_link.select_one("div.venue")
            room = venue_div.get_text(strip=True) if venue_div else ""

            # ── Description (tagline) ───────────────────────────────
            tagline_div = desc_link.select_one("div.tagline")
            description = tagline_div.get_text(strip=True) if tagline_div else ""

            # ── Price ───────────────────────────────────────────────
            price_min, price_max = self._parse_price(wrapper)

            # ── Ticket status ───────────────────────────────────────
            tickets_url, tickets_status = self._parse_tickets(wrapper)

            return Event(
                source=self.name,
                source_id=source_id,
                source_url=source_url,
                title=title,
                date=event_date,
                venue=VENUE_NAME,
                venue_address=f"{room}, {VENUE_ADDRESS}" if room else VENUE_ADDRESS,
                event_type="Concert",
                artists=artists,
                genres=[],  # Genre info not reliably available on listing page
                description=description,
                price_min=price_min,
                price_max=price_max,
                tickets_url=tickets_url,
                tickets_status=tickets_status,
            )

        except Exception as e:
            log.warning(
                "Failed to parse Muziekgebouw wrapper: %s",
                e,
                exc_info=True,
            )
            return None

    # ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _parse_date_time(top_date: Tag | None) -> Optional[datetime]:
        """Extract date and time from div.top-date.

        Structure:
            <div class="top-date">
                <span class="start">Fri 8 May 2026</span>
                <span class="time">20:15</span>
            </div>
        """
        if not top_date:
            return None

        date_span = top_date.select_one("span.start")
        time_span = top_date.select_one("span.time")

        date_str = date_span.get_text(strip=True) if date_span else ""
        time_str = time_span.get_text(strip=True) if time_span else ""

        if not date_str:
            return None

        # Try parsing with time first, then date-only fallback
        candidates = []
        if time_str:
            candidates.append(f"{date_str} {time_str}")
        candidates.append(date_str)

        for fmt_str in candidates:
            for pattern in [
                "%a %d %B %Y %H:%M",    # "Fri 8 May 2026 20:15"
                "%a %d %b %Y %H:%M",    # "Fri 8 May 2026 20:15" (short month)
                "%a %d %B %Y",           # "Fri 8 May 2026" (no time)
                "%a %d %b %Y",           # short month, no time
                "%d %B %Y %H:%M",        # no weekday
                "%d %B %Y",              # no weekday, no time
            ]:
                try:
                    return datetime.strptime(fmt_str.strip(), pattern)
                except ValueError:
                    continue

        log.debug("Muziekgebouw: could not parse date '%s %s'", date_str, time_str)
        return None

    # ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _parse_artists(subtitle: str) -> list[str]:
        """Extract artist/ensemble names from subtitle text.

        Common patterns:
          "Orchestra of the Eighteenth Century + Elisabeth Hetherington"
          "Collegium Vocale Gent + Philippe Herreweghe + lecture by ..."
          "+ free lunchtime concert"  (not an artist)
        """
        if not subtitle:
            return []

        # Strip leading "+" which sometimes prefixes subtitles
        cleaned = subtitle.lstrip("+").strip()
        if not cleaned:
            return []

        # Split on " + " separator
        parts = [p.strip() for p in cleaned.split("+") if p.strip()]

        # Filter out non-artist fragments
        artists = []
        skip_words = {"lecture by", "free", "lunchtime concert", "in collaboration"}
        for part in parts:
            lower = part.lower()
            if any(skip in lower for skip in skip_words):
                continue
            artists.append(part)

        return artists

    # ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _parse_price(wrapper: Tag) -> tuple[Optional[float], Optional[float]]:
        """Extract price range from the pricePopoverBtn button.

        Formats: "€ 31,00–€ 39,00" or "€ 6,50" or "Gratis" / "Free"
        """
        btn = wrapper.select_one("button.pricePopoverBtn")
        if not btn:
            return None, None

        text = btn.get_text(strip=True)
        if not text:
            return None, None

        lower = text.lower()
        if "gratis" in lower or "free" in lower:
            return 0.0, 0.0

        # Find all price amounts: "€ 31,00" → 31.00
        amounts = re.findall(r"€\s*([\d.,]+)", text)
        prices = []
        for amt in amounts:
            # "31,00" → "31.00" (Dutch decimal comma)
            cleaned = amt.replace(".", "").replace(",", ".")
            try:
                prices.append(float(cleaned))
            except ValueError:
                continue

        if len(prices) >= 2:
            return min(prices), max(prices)
        elif len(prices) == 1:
            return prices[0], prices[0]
        return None, None

    # ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _parse_tickets(wrapper: Tag) -> tuple[str, str]:
        """Extract ticket URL and status from the wrapper.

        Returns (tickets_url, tickets_status).
        """
        # Check for sold out: <a class="status-info status-uitverkocht">
        sold_out = wrapper.select_one("a.status-uitverkocht")
        if sold_out:
            return "", "sold_out"

        # Check for ticket buy link: <a class="btn-order" href="https://tickets.muziekgebouw.nl/...">
        ticket_link = wrapper.select_one("a.btn-order")
        if ticket_link:
            href = ticket_link.get("href", "")
            return href, "available"

        # Fallback: any link to tickets.muziekgebouw.nl
        ticket_fallback = wrapper.select_one("a[href*='tickets.muziekgebouw']")
        if ticket_fallback:
            href = ticket_fallback.get("href", "")
            text = ticket_fallback.get_text(strip=True).lower()
            if "sold" in text or "uitverkocht" in text:
                return "", "sold_out"
            return href, "available"

        return "", "available"
