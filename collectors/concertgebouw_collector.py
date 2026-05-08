"""
Concertgebouw.nl event collector.

Scrapes the concert calendar HTML pages from concertgebouw.nl/en/concerts-and-tickets.
The site is Nuxt.js-based but server-side renders all concert data into <article> tags,
so no headless browser is needed.

Pagination uses ?page=N query parameter (1-indexed).
Each page contains ~15 concert articles.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Optional

import httpx
from bs4 import BeautifulSoup, Tag

from collectors import BaseCollector
from config import HEADERS, REQUEST_TIMEOUT
from models import Event

log = logging.getLogger(__name__)

BASE_URL = "https://www.concertgebouw.nl"
CALENDAR_URL = f"{BASE_URL}/en/concerts-and-tickets"
MAX_PAGES = 10          # ~150 events, covers several months
PAGE_DELAY = 0.5        # polite delay between page requests


class ConcertgebouwCollector(BaseCollector):
    """Scrape concerts from Het Concertgebouw calendar."""

    name = "concertgebouw"

    def collect(self) -> list[Event]:
        log.info("Collecting events from Concertgebouw…")
        all_events: list[Event] = []
        seen_urls: set[str] = set()

        for page_num in range(1, MAX_PAGES + 1):
            articles = self._fetch_page(page_num)
            if articles is None:
                break  # network error → stop

            page_count = 0
            for art in articles:
                ev = self._parse_article(art)
                if ev and ev.source_url not in seen_urls:
                    all_events.append(ev)
                    seen_urls.add(ev.source_url)
                    page_count += 1

            log.info("Concertgebouw page %d: %d events", page_num, page_count)

            if page_count == 0:
                break  # last page or empty page

            if page_num < MAX_PAGES:
                time.sleep(PAGE_DELAY)

        log.info("Concertgebouw: collected %d events total", len(all_events))
        return all_events

    # ── HTTP ───────────────────────────────────────────────────────

    def _fetch_page(self, page: int) -> list[Tag] | None:
        """Fetch one page and return a list of <article> tags."""
        url = f"{CALENDAR_URL}?page={page}"
        try:
            resp = httpx.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("Concertgebouw page %d fetch failed: %s", page, exc)
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        return soup.find_all("article")

    # ── Article parsing ────────────────────────────────────────────

    def _parse_article(self, article: Tag) -> Optional[Event]:
        """Parse a single <article> element into an Event."""
        try:
            # ── Title ──────────────────────────────────────────────
            title_el = article.select_one("h3.c-content__title")
            if not title_el:
                return None  # banner / promo card — skip
            title = title_el.get_text(strip=True)
            if not title:
                return None

            # ── Link / source URL ──────────────────────────────────
            link = article.select_one("a[href]")
            if not link:
                return None
            href = link["href"]
            source_url = f"{BASE_URL}{href}" if href.startswith("/") else href

            # Source ID from URL slug  (e.g. "8507468-shane-van-neerden...")
            source_id = href.rstrip("/").rsplit("/", 1)[-1]

            # ── Date badge ─────────────────────────────────────────
            date_badge = article.select_one(
                "span.bg-app-primary-1-red--fire-brick"
            )
            date_text = date_badge.get_text(strip=True) if date_badge else ""

            # ── Metadata from <ul> list items ──────────────────────
            lis = article.select("section.c-content ul li")
            time_text = ""
            hall = ""
            price_text = ""
            composers: list[str] = []

            for li in lis:
                txt = li.get_text(strip=True)
                if not txt:
                    continue
                # Time comes from <time> element or text like "8:15 PM–10:00 PM"
                time_el = li.select_one("time")
                if time_el:
                    time_text = time_el.get_text(strip=True)
                elif txt in ("Main Hall", "Recital Hall", "Choir Hall"):
                    hall = txt
                elif txt.startswith("From €"):
                    price_text = txt
                elif "–" not in txt and "PM" not in txt and "AM" not in txt:
                    # Likely a composer + piece entry
                    composers.append(txt)

            # ── Parse date + time ──────────────────────────────────
            event_date = self._parse_datetime(date_text, time_text)
            if event_date is None:
                return None

            # Skip past events
            if event_date.date() < datetime.now().date():
                return None

            # ── Parse end time ─────────────────────────────────────
            event_end = self._parse_end_time(date_text, time_text)

            # ── Price ──────────────────────────────────────────────
            price_min = self._parse_price(price_text)

            # ── Ticket status ──────────────────────────────────────
            full_text = article.get_text()
            if "Sold out" in full_text:
                tickets_status = "sold_out"
            elif "Last tickets" in full_text:
                tickets_status = "last_tickets"
            elif price_min is None or price_min == 0:
                tickets_status = "free"
            else:
                tickets_status = "available"

            # ── Image ──────────────────────────────────────────────
            img = article.select_one("img")
            image_url = ""
            if img:
                image_url = img.get("src", "") or img.get("data-src", "")

            # ── Genre heuristic ────────────────────────────────────
            genres = self._detect_genres(title, composers)

            return Event(
                source=self.name,
                source_id=source_id,
                source_url=source_url,
                title=title,
                date=event_date,
                date_end=event_end,
                venue="Het Concertgebouw",
                venue_address=hall,
                event_type="Concert",
                artists=composers[:5],  # cap to first 5 program entries
                genres=genres,
                image_url=image_url,
                price_min=price_min,
                tickets_url=source_url,
                tickets_status=tickets_status,
            )
        except Exception as exc:
            log.warning("Failed to parse Concertgebouw article: %s", exc)
            return None

    # ── Date / time helpers ────────────────────────────────────────

    @staticmethod
    def _parse_datetime(date_text: str, time_text: str) -> Optional[datetime]:
        """
        Parse date + time from Concertgebouw format.
        date_text: "Fri, May 8, 2026"
        time_text: "8:15 PM–10:00 PM"  (we take the start time)
        """
        if not date_text:
            return None

        # Extract start time from "8:15 PM–10:00 PM" or "8:15 PM"
        start_time = ""
        if time_text:
            # Split on dash variants (–, -, —)
            parts = re.split(r"[–\-—]", time_text)
            start_time = parts[0].strip()

        # Try parsing with time
        for fmt in (
            "%a, %B %d, %Y %I:%M %p",
            "%a, %B %d, %Y %I:%M%p",
            "%a, %B %d, %Y",
        ):
            combined = f"{date_text} {start_time}".strip() if start_time else date_text
            try:
                return datetime.strptime(combined, fmt)
            except ValueError:
                continue

        # Fallback: date only
        try:
            return datetime.strptime(date_text, "%a, %B %d, %Y")
        except ValueError:
            return None

    @staticmethod
    def _parse_end_time(date_text: str, time_text: str) -> Optional[datetime]:
        """Parse the end time from a time range like '8:15 PM–10:00 PM'."""
        if not date_text or not time_text:
            return None

        parts = re.split(r"[–\-—]", time_text)
        if len(parts) < 2:
            return None

        end_time = parts[1].strip()
        for fmt in (
            "%a, %B %d, %Y %I:%M %p",
            "%a, %B %d, %Y %I:%M%p",
        ):
            try:
                return datetime.strptime(f"{date_text} {end_time}", fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def _parse_price(text: str) -> Optional[float]:
        """Extract numeric price from 'From €19.00'."""
        if not text:
            return None
        m = re.search(r"€\s*([\d.,]+)", text)
        if m:
            cleaned = m.group(1).replace(",", ".")
            try:
                return float(cleaned)
            except ValueError:
                pass
        return None

    @staticmethod
    def _detect_genres(title: str, composers: list[str]) -> list[str]:
        """Simple keyword-based genre tagging for classical music events."""
        genres: list[str] = []
        combined = (title + " " + " ".join(composers)).lower()

        if any(kw in combined for kw in ("orchestra", "symphony", "philharmonic", "concerto")):
            genres.append("Orchestra")
        if any(kw in combined for kw in ("chamber", "trio", "quartet", "quintet", "sonata")):
            genres.append("Chamber Music")
        if any(kw in combined for kw in ("jazz",)):
            genres.append("Jazz")
        if any(kw in combined for kw in ("piano", "pianists")):
            genres.append("Piano")
        if any(kw in combined for kw in ("organ",)):
            genres.append("Organ")
        if any(kw in combined for kw in ("choir", "vocal", "choral")):
            genres.append("Vocal Music")
        if any(kw in combined for kw in ("family concert",)):
            genres.append("Family")
        if any(kw in combined for kw in ("lunchtime concert", "free lunchtime")):
            genres.append("Free Lunchtime")
        if any(kw in combined for kw in ("film", "game music")):
            genres.append("Film/Game Music")

        if not genres:
            genres.append("Classical")

        return genres
