from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional
import urllib.parse

import httpx
from bs4 import BeautifulSoup, Tag

from collectors import BaseCollector
from config import HEADERS, REQUEST_TIMEOUT
from models import Event

log = logging.getLogger(__name__)

BIMHUIS_BASE = "https://www.bimhuis.nl"
BIMHUIS_AGENDA_URL = BIMHUIS_BASE + "/en/calendar/"

VENUE_NAME = "Bimhuis"
VENUE_ADDRESS = "Piet Heinkade 3, 1019 BR Amsterdam"


class BimhuisCollector(BaseCollector):
    name = "bimhuis"

    def collect(self) -> list[Event]:
        log.info("Collecting events from Bimhuis...")

        events: list[Event] = []
        page = 1
        max_pages = 20

        while page <= max_pages:
            url = f"{BIMHUIS_AGENDA_URL}?page={page}"
            try:
                resp = httpx.get(
                    url,
                    headers=HEADERS,
                    timeout=REQUEST_TIMEOUT,
                    follow_redirects=True,
                )
                resp.raise_for_status()
            except httpx.HTTPError as e:
                log.error("Failed to fetch Bimhuis agenda page %d: %s", page, e)
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            
            items = soup.select("div.agenda-tile__content")
            
            # The structure often has empty placeholder items or repeats. We need to filter valid ones.
            valid_items = [i for i in items if i.select_one('a.agenda-tile__link')]
            
            if not valid_items:
                log.info("Bimhuis: No valid events found on page %d, stopping.", page)
                break

            for item in valid_items:
                # Also try to find the image in the sibling or parent if possible.
                # Actually, the image is in <div class="agenda-tile__img-tile">, which is a sibling of <div class="agenda-tile__content">
                parent_tile = item.parent
                ev = self._parse_item(item, parent_tile)
                if ev:
                    events.append(ev)

            log.debug("Bimhuis: collected %d events from page %d", len(valid_items), page)

            # If there are less than 20 events on a page, it's the last page.
            if len(valid_items) < 20:
                break
                
            page += 1

        log.info("Bimhuis: collected %d events total", len(events))
        return events

    def _parse_item(self, item: Tag, parent_tile: Tag) -> Optional[Event]:
        try:
            link_el = item.select_one("a.agenda-tile__link")
            if not link_el:
                return None
                
            title_el = link_el.select_one("h3")
            title = title_el.get_text(strip=True) if title_el else link_el.get_text(strip=True)
            if not title:
                return None

            href = link_el.get("href", "")
            source_url = href if href.startswith("http") else BIMHUIS_BASE + href

            # Extract source_id from URL
            parts = [p for p in urllib.parse.urlparse(source_url).path.split("/") if p]
            source_id = parts[-1] if parts else href

            date_el = item.select_one("time.agenda-tile__dates")
            if not date_el:
                return None

            date_str = date_el.get("datetime", "")  # e.g., "2026-05-26"
            time_span = date_el.select_one("span")
            time_str = time_span.get_text(strip=True) if time_span else "00:00"

            event_date = None
            try:
                # E.g., "2026-05-26 21:30"
                event_date = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            except ValueError:
                # Fallback to date only
                try:
                    event_date = datetime.strptime(date_str, "%Y-%m-%d")
                except ValueError:
                    return None

            # Description
            p_el = item.select_one("p")
            description = p_el.get_text(strip=True) if p_el else ""

            # Tags / Genres
            tags = [tag.get_text(strip=True) for tag in item.select("ul.agenda-tile__tags a div") if tag.get_text(strip=True)]
            if not tags:
                tags = [tag.get_text(strip=True) for tag in item.select("ul.agenda-tile__tags div") if tag.get_text(strip=True)]

            # Price & Tickets
            tickets_url = ""
            tickets_status = "available"
            price_min = None
            
            # Button states
            btn = item.select_one(".agenda-tile__btn")
            disabled_btn = item.select_one("button[disabled]")
            
            if btn:
                tickets_url = btn.get("href", "")
            elif disabled_btn:
                btn_text = disabled_btn.get_text(strip=True).lower()
                if "sold out" in btn_text or "uitverkocht" in btn_text:
                    tickets_status = "sold_out"
                elif "free" in btn_text or "gratis" in btn_text:
                    tickets_status = "free"
                    price_min = 0.0

            # Image
            image_url = ""
            if parent_tile:
                img_el = parent_tile.select_one("img")
                if img_el:
                    # Next.js images have src="/_next/image?url=..."
                    raw_src = img_el.get("src", "")
                    if raw_src.startswith("/_next/image"):
                        parsed = urllib.parse.urlparse(BIMHUIS_BASE + raw_src)
                        qs = urllib.parse.parse_qs(parsed.query)
                        if "url" in qs:
                            image_url = qs["url"][0]
                    else:
                        image_url = raw_src

            return Event(
                source=self.name,
                source_id=source_id,
                source_url=source_url,
                title=title,
                date=event_date,
                venue=VENUE_NAME,
                venue_address=VENUE_ADDRESS,
                event_type="Concert",
                genres=tags,
                description=description,
                image_url=image_url,
                tickets_url=tickets_url,
                tickets_status=tickets_status,
                price_min=price_min
            )

        except Exception as e:
            log.warning("Failed to parse Bimhuis item: %s", e, exc_info=True)
            return None
