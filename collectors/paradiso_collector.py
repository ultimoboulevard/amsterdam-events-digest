"""
Paradiso.nl event collector.

Uses the CraftCMS GraphQL API exposed via AWS Lambda.
Endpoint & bearer token reverse-engineered from the Next.js JS bundle.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Optional

import httpx

from collectors import BaseCollector
from config import REQUEST_TIMEOUT
from models import Event

log = logging.getLogger(__name__)

# ── CraftCMS GraphQL backend (extracted from JS bundle chunk 91-*.js) ──
PARADISO_GQL_URL = (
    "https://knwxh8dmh1.execute-api.eu-central-1.amazonaws.com/graphql"
)
PARADISO_BEARER = "qNG1MfNixLtJU_iE_nvJ3ssmMY5NZ3Nx"
PARADISO_SITE = "paradisoEnglish"

QUERY_TEMPLATE = """{{
  program(site: "{site}", size: {size}{search_after}) {{
    events {{
      title
      date
      doorsOpen
      startMain
      supportAct
      startSupportAct
      ticketUrl
      eventStatus
      remarkWebsite
      location {{ title }}
      contentCategory {{ title }}
      url
      uri
      relatedArtists {{ title }}
      areas {{ label value }}
      sort
    }}
  }}
}}"""


class ParadisoCollector(BaseCollector):
    name = "paradiso"

    def collect(self) -> list[Event]:
        log.info("Collecting events from Paradiso (CraftCMS GraphQL)...")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {PARADISO_BEARER}",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }

        all_events: list[Event] = []
        search_after = None
        max_pages = 10
        page_size = 100

        for page_num in range(1, max_pages + 1):
            # Build search_after clause
            sa_clause = ""
            if search_after:
                sa_clause = f', searchAfter: {json.dumps(search_after)}'

            query = QUERY_TEMPLATE.format(
                site=PARADISO_SITE,
                size=page_size,
                search_after=sa_clause,
            )

            try:
                resp = httpx.post(
                    PARADISO_GQL_URL,
                    json={"query": query},
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                log.error("Failed to fetch Paradiso page %d: %s", page_num, e)
                break

            if "errors" in data:
                log.warning("Paradiso GraphQL partial errors: %s",
                            [e.get("message") for e in data["errors"][:3]])
                # Only break if there's no data at all
                if not data.get("data"):
                    break

            items = (
                data.get("data", {})
                .get("program", {})
                .get("events", [])
            )
            if not items:
                break

            log.info("Paradiso page %d: %d events", page_num, len(items))

            for item in items:
                ev = self._parse_item(item)
                if ev:
                    all_events.append(ev)

            # Pagination via cursor
            if len(items) < page_size:
                break

            last = items[-1]
            sort_vals = last.get("sort")
            if sort_vals:
                search_after = sort_vals
            else:
                break

        log.info("Paradiso: collected %d events total", len(all_events))
        return all_events

    # ──────────────────────────────────────────────────────────────────
    def _parse_item(self, item: dict) -> Optional[Event]:
        """Parse one CraftCMS event object into our unified Event."""
        try:
            title = (item.get("title") or "").strip()
            if not title:
                return None

            # Skip cancelled events
            status = (item.get("eventStatus") or "").lower()
            if status == "canceled":
                return None

            # Date
            date_str = item.get("date")  # "2026-05-07"
            if not date_str:
                return None

            doors = item.get("doorsOpen") or ""   # "19:00"
            start = item.get("startMain") or doors or "20:00"

            try:
                event_date = datetime.strptime(
                    f"{date_str} {start}", "%Y-%m-%d %H:%M"
                )
            except ValueError:
                event_date = datetime.strptime(date_str, "%Y-%m-%d")

            # Skip past events
            if event_date.date() < datetime.now().date():
                return None

            # Venue / Location
            locations = item.get("location") or []
            venue_name = locations[0]["title"] if locations else "Paradiso"

            # Room / Area
            areas = item.get("areas") or []
            area_labels = [a.get("label", "") for a in areas]

            # Categories → genres
            categories = item.get("contentCategory") or []
            genres = [c["title"] for c in categories]

            # Event type heuristic
            event_type = "Concert"
            genre_lower = " ".join(genres).lower()
            if "club" in genre_lower:
                event_type = "Club"
            elif "festival" in genre_lower:
                event_type = "Festival"
            elif "literature" in genre_lower or "science" in genre_lower:
                event_type = "Talk"

            # Artists
            artists = []
            for a in item.get("relatedArtists") or []:
                name = (a.get("title") or "").strip()
                if name:
                    artists.append(name)

            # Support act
            support = (item.get("supportAct") or "").strip()
            if support and support not in artists:
                artists.append(support)

            # Price (ticketPrice field causes server errors, omitted)
            price = None

            # URL
            source_url = item.get("url") or ""
            if not source_url and item.get("uri"):
                source_url = f"https://www.paradiso.nl/en/{item['uri']}"

            # Source ID from URL (the numeric CraftCMS entry ID)
            source_id = ""
            if source_url:
                parts = source_url.rstrip("/").split("/")
                source_id = parts[-1] if parts else ""

            return Event(
                source=self.name,
                source_id=source_id,
                source_url=source_url,
                title=title,
                date=event_date,
                venue=venue_name,
                venue_address=", ".join(area_labels),
                event_type=event_type,
                artists=artists,
                genres=genres,
                tickets_url=item.get("ticketUrl") or "",
                tickets_status="available" if status == "confirmed" else status,
                price_min=price,
                description=(item.get("remarkWebsite") or "").strip(),
            )

        except Exception as e:
            log.warning("Failed to parse Paradiso item '%s': %s",
                        item.get("title", "?"), e)
            return None

    @staticmethod
    def _parse_price(raw: str | None) -> float | None:
        """Extract numeric price from strings like '€15.50' or '12,50'."""
        if not raw:
            return None
        # Remove currency symbols, whitespace
        cleaned = re.sub(r"[€$\s]", "", raw).replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None
