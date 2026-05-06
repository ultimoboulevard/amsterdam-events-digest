"""
Amsterdam Alternative event collector.

AmsterdamAlternative.nl loads events dynamically via a JSON API:
  GET /services/get-events-past-v25.php?month=5&months=1&image=true

Returns { daterange: "...", items: [ { id, title, venue, start, price, type, ... } ] }
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx

from collectors import BaseCollector
from config import HEADERS, REQUEST_TIMEOUT
from models import Event

log = logging.getLogger(__name__)

AA_API_BASE = "https://www.amsterdamalternative.nl/services/get-events-past-v25.php"


class AmsterdamAltCollector(BaseCollector):
    name = "amsterdam_alt"

    def collect(self) -> list[Event]:
        """Fetch events from Amsterdam Alternative's JSON API."""
        log.info("Collecting events from Amsterdam Alternative...")

        events: list[Event] = []
        now = datetime.now()

        # Fetch current month + next 2 months for good coverage
        for offset in range(3):
            month = now.month + offset
            year = now.year
            if month > 12:
                month -= 12
                year += 1

            params = {
                "month": month,
                "year": year,
                "months": 1,
                "image": "true",
            }

            try:
                resp = httpx.get(
                    AA_API_BASE,
                    params=params,
                    headers=HEADERS,
                    timeout=REQUEST_TIMEOUT,
                    follow_redirects=True,
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPError as e:
                log.error("Failed to fetch AA events (month=%d): %s", month, e)
                continue
            except Exception as e:
                log.error("Failed to parse AA response (month=%d): %s", month, e)
                continue

            items = data.get("items", [])
            log.info("AA API month=%d/%d: %d items", month, year, len(items))

            for item in items:
                ev = self._parse_item(item)
                if ev:
                    events.append(ev)

        log.info("Amsterdam Alternative: collected %d events total", len(events))
        return events

    def _parse_item(self, item: dict) -> Event | None:
        """Parse a single API item into an Event."""
        try:
            title = item.get("title", "").strip()
            if not title:
                return None

            # Parse start timestamp (Unix epoch)
            start_ts = item.get("start")
            if not start_ts:
                return None
            try:
                event_date = datetime.fromtimestamp(int(start_ts))
            except (ValueError, TypeError, OSError):
                return None

            # End timestamp
            end_ts = item.get("end")
            event_end = None
            if end_ts:
                try:
                    event_end = datetime.fromtimestamp(int(end_ts))
                except (ValueError, TypeError, OSError):
                    pass

            # Venue can be a dict {"id": ..., "name": "..."} or a string
            venue_raw = item.get("venue", "")
            if isinstance(venue_raw, dict):
                venue = venue_raw.get("name", "").strip()
            else:
                venue = str(venue_raw).strip()

            event_id = str(item.get("id", ""))

            # Type can also be a dict {"id": ..., "name": "..."} or a string
            type_raw = item.get("type", "")
            if isinstance(type_raw, dict):
                event_type = type_raw.get("name", "").strip()
            elif isinstance(type_raw, list):
                # Could be a list of type dicts
                event_type = ", ".join(
                    t.get("name", str(t)) if isinstance(t, dict) else str(t)
                    for t in type_raw
                )
            else:
                event_type = str(type_raw).strip()

            # Price can be a dict too
            price_raw = item.get("price", "")
            if isinstance(price_raw, dict):
                price = price_raw.get("name", "") or price_raw.get("label", "") or str(price_raw)
            else:
                price = str(price_raw).strip() if price_raw else ""

            image_url = item.get("header_image", "") or item.get("image", "") or ""
            if isinstance(image_url, dict):
                image_url = image_url.get("url", "") or image_url.get("src", "") or ""

            # Build source URL
            slug = item.get("slug", "")
            if slug:
                source_url = f"https://www.amsterdamalternative.nl/agenda/{event_id}/{slug}"
            else:
                source_url = f"https://www.amsterdamalternative.nl/agenda/{event_id}"

            # Parse price
            price_min = None
            tickets_status = "available"
            if price:
                price_lower = price.lower()
                if any(kw in price_lower for kw in ("free", "gratis", "donation", "donatie")):
                    tickets_status = "free"
                    price_min = 0.0
                else:
                    import re
                    nums = re.findall(r"[\d.]+", price)
                    if nums:
                        try:
                            price_min = float(nums[0])
                        except ValueError:
                            pass

            # Genres from type field
            genres = [g.strip() for g in event_type.split(",") if g.strip()] if event_type else []

            return Event(
                source=self.name,
                source_id=event_id,
                source_url=source_url,
                title=title,
                date=event_date,
                date_end=event_end,
                venue=venue,
                event_type=event_type,
                genres=genres,
                image_url=image_url,
                price_min=price_min,
                tickets_status=tickets_status,
            )

        except Exception as e:
            log.warning("Failed to parse AA item: %s — %s", item.get("title", "?"), e)
            return None
