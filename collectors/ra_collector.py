"""
Resident Advisor (RA.co) event collector.

Uses RA's GraphQL API directly via httpx.
No Playwright/browser required. Bypasses Cloudflare if headers are correct.
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx

from collectors import BaseCollector
from config import HEADERS, REQUEST_TIMEOUT
from models import Event

log = logging.getLogger(__name__)

RA_GRAPHQL_URL = "https://ra.co/graphql"
# RA area ID for Amsterdam
AMSTERDAM_AREA_ID = 29


class ResidentAdvisorCollector(BaseCollector):
    name = "ra"

    def collect(self) -> list[Event]:
        log.info("Collecting events from Resident Advisor (Amsterdam)...")

        events: list[Event] = []
        today = datetime.now().strftime("%Y-%m-%dT00:00:00.000Z")

        # GraphQL Query for RA Event Listings
        query = """
        query GET_EVENTS($filters: FilterInputDtoInput, $pageSize: Int, $page: Int) {
          eventListings(filters: $filters, pageSize: $pageSize, page: $page) {
            data {
              event {
                id
                title
                date
                startTime
                endTime
                venue {
                  name
                  address
                }
                artists {
                  name
                }
                images {
                  filename
                }
                attending
              }
            }
            totalResults
          }
        }
        """

        # We'll paginate until we get 0 results or hit a reasonable limit
        page = 1
        page_size = 100
        max_pages = 10  # Max 1000 events

        # Add Origin to headers to appease RA's CORS/Cloudflare rules
        ra_headers = HEADERS.copy()
        ra_headers["Origin"] = "https://ra.co"
        ra_headers["Referer"] = "https://ra.co/events/nl/amsterdam"
        ra_headers["Content-Type"] = "application/json"

        while page <= max_pages:
            variables = {
                "filters": {
                    "areas": {"eq": AMSTERDAM_AREA_ID},
                    "listingDate": {"gte": today}
                },
                "pageSize": page_size,
                "page": page
            }

            try:
                resp = httpx.post(
                    RA_GRAPHQL_URL,
                    json={"query": query, "variables": variables},
                    headers=ra_headers,
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                log.error("Failed to fetch RA events page %d: %s", page, e)
                break

            # Check for GraphQL errors
            if "errors" in data:
                log.error("RA GraphQL returned errors: %s", data["errors"])
                break

            listings = data.get("data", {}).get("eventListings", {}).get("data", [])
            if not listings:
                break

            log.info("RA API page %d: found %d items", page, len(listings))

            for listing in listings:
                ev_data = listing.get("event")
                if not ev_data:
                    continue
                ev = self._parse_item(ev_data)
                if ev:
                    events.append(ev)

            # If we got fewer items than the page size, we've hit the end
            if len(listings) < page_size:
                break

            page += 1

        log.info("Resident Advisor: collected %d events total", len(events))
        return events

    def _parse_item(self, item: dict) -> Event | None:
        """Parse a single RA GraphQL event object."""
        try:
            event_id = str(item.get("id", ""))
            title = item.get("title", "").strip()
            if not event_id or not title:
                return None

            # Date parsing
            date_str = item.get("date")
            start_str = item.get("startTime")
            end_str = item.get("endTime")

            event_date = None
            if date_str:
                # RA dates usually look like "2026-05-06T00:00:00.000"
                # Sometimes startTime is provided as well
                parse_str = start_str if start_str else date_str
                try:
                    # Strip fractional seconds and 'Z' if present for easier parsing
                    clean_str = parse_str.split(".")[0].rstrip("Z")
                    event_date = datetime.fromisoformat(clean_str)
                except ValueError:
                    pass

            if not event_date:
                return None

            event_end = None
            if end_str:
                try:
                    clean_end = end_str.split(".")[0].rstrip("Z")
                    event_end = datetime.fromisoformat(clean_end)
                except ValueError:
                    pass

            # Venue
            venue_obj = item.get("venue") or {}
            venue_name_raw = venue_obj.get("name")
            venue_name = venue_name_raw.strip() if venue_name_raw else "TBA"
            
            venue_address_raw = venue_obj.get("address")
            venue_address = venue_address_raw.strip() if venue_address_raw else ""

            # Artists
            artists = []
            for artist_obj in item.get("artists", []):
                name = artist_obj.get("name")
                if name:
                    artists.append(name.strip())

            # Image
            image_url = ""
            images = item.get("images", [])
            if images and isinstance(images, list):
                filename = images[0].get("filename")
                if filename:
                    image_url = filename

            # Attendees
            attending_count = item.get("attending", 0)

            source_url = f"https://ra.co/events/{event_id}"

            # RA is almost entirely Club/Electronic
            event_type = "Club"

            return Event(
                source=self.name,
                source_id=event_id,
                source_url=source_url,
                title=title,
                date=event_date,
                date_end=event_end,
                venue=venue_name,
                venue_address=venue_address,
                event_type=event_type,
                artists=artists,
                image_url=image_url,
                attending_count=attending_count,
                tickets_status="available", # Default, RA API requires deeper querying for ticket status
            )

        except Exception as e:
            log.warning("Failed to parse RA item %s: %s", item.get("id"), e)
            return None
