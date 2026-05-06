"""
Murmur collector.
Parses events from the landing page table.
"""
import logging
from datetime import datetime
from bs4 import BeautifulSoup
import httpx
from collectors import BaseCollector
from config import HEADERS, REQUEST_TIMEOUT
from models import Event

log = logging.getLogger(__name__)

MURMUR_URL = "https://murmurmur.nl/"

class MurmurCollector(BaseCollector):
    name = "murmur"

    def collect(self) -> list[Event]:
        log.info(f"Collecting events from {self.name}...")
        try:
            resp = httpx.get(MURMUR_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'html.parser')
        except Exception as e:
            log.error(f"Failed to fetch {self.name}: {e}")
            return []

        events = []
        # Find all event cards in the table
        cards = soup.select('tr.card')
        log.info(f"Found {len(cards)} event cards")

        for card in cards:
            try:
                # Extract data from attributes
                start_date_str = card.get('data-start') # Format: 20260528
                title = card.get('data-post-title')
                
                if not start_date_str or not title:
                    continue
                
                # Parse date
                try:
                    event_date = datetime.strptime(start_date_str, "%Y%m%d")
                except ValueError:
                    log.warning(f"Invalid date format: {start_date_str}")
                    continue

                # Filter past events (Murmur home page lists many past events)
                if event_date.date() < datetime.now().date():
                    continue

                # Extract venue and link
                venue = "Murmur" # Default as it's the venue's site
                
                # Find ticket link
                ticket_link_tag = card.select_one('a.buy-tickets-btn')
                source_url = ticket_link_tag.get('href') if ticket_link_tag else MURMUR_URL
                
                # Extract ID
                post_id = card.get('data-post-id', '')

                events.append(Event(
                    source=self.name,
                    source_id=post_id,
                    source_url=source_url,
                    title=title.title(),
                    date=event_date,
                    venue=venue,
                    event_type="Misc", # Default
                    artists=[], # We could parse this from data-post-title or details
                ))
            except Exception as e:
                log.warning(f"Error parsing Murmur event: {e}")

        log.info(f"Collected {len(events)} future events from {self.name}")
        return events
