"""
offbeat.amsterdam collector.

Gira su Gancio (agenda federata, gancio.org) ed espone una API JSON pubblica,
quindi niente HTML da parsare:

    GET https://offbeat.amsterdam/api/events?start=<unix>&end=<unix>

E' la fonte piu' grossa che abbiamo: 2.323 eventi sui 220 giorni successivi,
quanto tutto il resto del DB messo insieme. Ed e' anche la piu' sovrapposta,
perche' e' un aggregatore come noi: il 72% dei suoi eventi (1.678 su 2.323) e'
in locali che gia' copriamo. Riversarli tutti dentro significherebbe
raddoppiare Paradiso, Melkweg e Tolhuistuin.

Quindi qui si salta cio' che ha gia' una fonte dedicata (labels.VENUE_OWNER).
La regola guarda a CHI possiede il locale, non a chi per caso ci ha gia' un
evento: `ra` e `amsterdam_alt` sono generaliste quanto questa e passano per
Cinetol o OT301 di sfuggita, mentre offbeat quei cartelloni li ha interi.
Cedere loro il locale vorrebbe dire buttare la parte migliore di questa fonte.

Cio' che resta e' roba che nessun altro vede: NieuwLand (330 eventi), Theater
de Richel (147), Mezrab (90), Joe's Garage (76), De Ceuvel, Pakhuis de
Zwijger, Vondelbunker, De Balie.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx

from collectors import BaseCollector
from config import HEADERS, REQUEST_TIMEOUT
from labels import venue_owner
from models import Event

log = logging.getLogger(__name__)

OFFBEAT_BASE = "https://offbeat.amsterdam"
OFFBEAT_API = f"{OFFBEAT_BASE}/api/events"

# Finestra in avanti, allineata a quella che le pagine leggono dal DB.
HORIZON_DAYS = 220

# I tag di Gancio mescolano generi ("noise", "punk") e quartieri
# ("amsterdam-noord", "nieuwmarkt/lastage"). I secondi non sono generi e nel
# filtro del sito comparirebbero come tali, quindi si scartano.
def _is_place_tag(tag: str) -> bool:
    t = tag.lower()
    return t.startswith("amsterdam") or "/" in t or t in {
        "noord", "oost", "west", "zuid", "centrum", "zuidoost", "nieuw-west",
    }


class OffbeatCollector(BaseCollector):
    name = "offbeat"
    probe_url = OFFBEAT_API

    def collect(self) -> list[Event]:
        log.info("Collecting events from Offbeat Amsterdam...")

        now = datetime.now()
        try:
            resp = httpx.get(
                OFFBEAT_API,
                params={
                    "start": int(now.timestamp()),
                    "end": int((now + timedelta(days=HORIZON_DAYS)).timestamp()),
                },
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
            )
            resp.raise_for_status()
            items = resp.json()
        except httpx.HTTPError as e:
            log.error("Failed to fetch Offbeat events: %s", e)
            return []
        except Exception as e:  # noqa: BLE001
            log.error("Failed to parse Offbeat response: %s", e)
            return []

        if not isinstance(items, list):
            log.error("Offbeat: risposta inattesa (%s)", type(items).__name__)
            return []

        log.info("Offbeat API: %d eventi nella finestra", len(items))

        events: list[Event] = []
        skipped: dict[str, int] = {}

        for item in items:
            place = item.get("place") or {}
            venue = (place.get("name") or "").strip()

            owner = venue_owner(venue, exclude=self.name)
            if owner:
                skipped[owner] = skipped.get(owner, 0) + 1
                continue

            ev = self._parse_item(item, venue, place)
            if ev:
                events.append(ev)

        if skipped:
            detail = ", ".join(f"{n} a {src}" for src, n in sorted(skipped.items()))
            log.info("Offbeat: %d eventi lasciati alla fonte dedicata (%s)",
                     sum(skipped.values()), detail)
        log.info("Offbeat Amsterdam: collected %d events total", len(events))
        return events

    def _parse_item(self, item: dict, venue: str, place: dict) -> Event | None:
        try:
            title = (item.get("title") or "").strip()
            if not title:
                return None

            start_ts = item.get("start_datetime")
            if not start_ts:
                return None
            try:
                # fromtimestamp() usa il fuso della macchina, che e' quello di
                # Amsterdam: cosi' l'ora resta giusta anche d'inverno. Fissare
                # UTC+2 a mano, come fa lo script di partenza, sbaglia di
                # un'ora da fine ottobre a marzo.
                event_date = datetime.fromtimestamp(int(start_ts))
            except (ValueError, TypeError, OSError):
                return None

            event_end = None
            end_ts = item.get("end_datetime")
            if end_ts:
                try:
                    event_end = datetime.fromtimestamp(int(end_ts))
                except (ValueError, TypeError, OSError):
                    pass

            media = (item.get("media") or [{}])[0] or {}
            image_url = f"{OFFBEAT_BASE}/media/{media['url']}" if media.get("url") else ""

            tags = [t for t in (item.get("tags") or []) if t and not _is_place_tag(t)]

            slug = item.get("slug") or ""
            source_url = f"{OFFBEAT_BASE}/event/{slug}" if slug else OFFBEAT_BASE

            # `online_locations` porta l'url ORIGINALE da cui Gancio ha preso
            # l'evento, che nella maggior parte dei casi e' la biglietteria
            # (tickets.paradiso.nl, cinetol.stager.co). Va in tickets_url; il
            # link della scheda resta quello di offbeat, che e' la fonte.
            originals = [u for u in (item.get("online_locations") or []) if u]

            return Event(
                source=self.name,
                source_id=str(item.get("id", "")),
                source_url=source_url,
                title=title,
                date=event_date,
                date_end=event_end,
                venue=venue,
                venue_address=(place.get("address") or "").strip(),
                genres=tags[:8],
                image_url=image_url,
                tickets_url=originals[0] if originals else "",
                tickets_status="available",
            )

        except Exception as e:  # noqa: BLE001
            log.warning("Failed to parse Offbeat item: %s — %s",
                        (item.get("title") or "?")[:40], e)
            return None
