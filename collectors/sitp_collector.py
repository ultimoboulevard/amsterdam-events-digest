"""
Space is the Place collector.

SITP non e' una sala: e' un programmatore che porta jazz e musica improvvisata
DENTRO locali altrui, in tutta l'Olanda. Questo cambia due cose rispetto agli
altri collector, ed e' il motivo per cui questo file filtra invece di
raccogliere e basta:

  1. GEOGRAFIA — meta' del suo calendario e' fuori Amsterdam (Utrecht,
     Tilburg). Qui si tiene solo Amsterdam, perche' il sito e' un aggregatore
     di Amsterdam.
  2. DOPPIONI — quando SITP suona al Bimhuis, quell'evento lo raccoglie gia'
     il collector `bimhuis`. E non verrebbe assorbito a valle: dedup.py fonde
     solo a parita' di giorno E di ora, e le due fonti sull'orario non vanno
     d'accordo (il 9/10/2026 SITP dice 18:30, il Bimhuis 20:30 — porte contro
     inizio musica). Sarebbero due righe per un concerto solo. Quindi gli
     eventi nei locali che gia' copriamo si scartano qui.

Resta cio' che nessun'altra fonte vede.

DA DOVE ARRIVANO I DATI
Il punto di partenza e' stato il calendario Google pubblico
(.../ical/it9t1urti43fkluo8ljh2vu984r4159s%40import.calendar.google.com/public/basic.ics),
ma il sito e' WordPress con The Events Calendar, che espone gli stessi eventi
via REST con date gia' in ora locale, immagine e categorie. Si usa quella:
l'ICS dava DTSTART in UTC da riconvertire a mano, ed e' un passaggio in meno
in cui sbagliare.

ATTENZIONE — il campo `venue` dell'API e' inutilizzabile: su 5 eventi e'
`None` quattro volte, e la quinta contiene `{"venue": "Festival_", "city":
"Murf Murw"}`, che non e' ne' un locale ne' una citta'. Il luogo vero lo
scrivono a mano in testa alla descrizione ("18.30BIMHUIS, Amsterdam",
"02.00De Nijverheid | Utrecht", "@SEVERAL LOCATIONS - TILBURG"). Di li' si
legge, e solo dai primi caratteri: piu' avanti le biografie citano citta' a
caso ("the big cities he lived in: Paris, Berlin and Amsterdam") e un filtro
sul testo intero le prenderebbe per la sede.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime

import httpx

from collectors import BaseCollector
from config import HEADERS, REQUEST_TIMEOUT
from labels import owner_in_text
from models import Event

log = logging.getLogger(__name__)

SITP_API = "https://spaceistheplace.eu/wp-json/tribe/events/v1/events"

# Quanti caratteri di descrizione contano come "intestazione del luogo".
# Le descrizioni cominciano tutte con data, ora e sede; il resto e' racconto.
HEAD_CHARS = 220

# Se una di queste compare nell'intestazione, l'evento non e' ad Amsterdam.
# TivoliVredenburg sta in lista perche' e' l'unico caso in cui la citta' non
# viene mai nominata: la descrizione dice solo "@Tivoli Vredenburg".
ELSEWHERE = (
    "utrecht", "tilburg", "rotterdam", "den haag", "the hague", "eindhoven",
    "groningen", "nijmegen", "arnhem", "haarlem", "leiden", "maastricht",
    "delft", "breda", "tivolivredenburg", "tivoli vredenburg", "de nijverheid",
    "paradiso rotterdam", "doornroosje", "berlin", "brussels", "antwerp",
)

# Sedi di Amsterdam che nessun'altra fonte copre. Servono a due cose insieme:
# sono la prova che l'evento e' ad Amsterdam anche quando la citta' non e'
# scritta, e danno il nome del locale da mettere in `venue`.
AMS_VENUES = {
    "ndsm": "NDSM",
    "hotel de goudfazant": "Hotel de Goudfazant",
    "kerkje durgerdam": "Kerkje Durgerdam",
    "waterlandplein": "Waterlandplein",
    "muiderpoort": "Muiderpoorttheater",
    "de brakke grond": "De Brakke Grond",
    "perdu": "Perdu",
    "zaal 100": "Zaal 100",
    "badhuistheater": "Badhuistheater",
    "de ruimte": "De Ruimte",
}


def _clean(text: str) -> str:
    """Il testo dell'API arriva con tag HTML ed entita': via entrambi."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


def _head(description: str) -> str:
    return _clean(description)[:HEAD_CHARS].lower()


def _match(head: str, needles) -> str:
    for n in needles:
        if n in head:
            return n
    return ""


class SitpCollector(BaseCollector):
    name = "sitp"
    probe_url = SITP_API

    def collect(self) -> list[Event]:
        log.info("Collecting events from Space is the Place...")

        try:
            resp = httpx.get(
                SITP_API,
                params={"per_page": 50},
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
            )
            resp.raise_for_status()
            items = resp.json().get("events", [])
        except httpx.HTTPError as e:
            log.error("Failed to fetch SITP events: %s", e)
            return []
        except Exception as e:  # noqa: BLE001
            log.error("Failed to parse SITP response: %s", e)
            return []

        log.info("SITP API: %d eventi nel calendario", len(items))

        events: list[Event] = []
        skipped_elsewhere = skipped_covered = skipped_unclear = 0

        for item in items:
            head = _head(item.get("description") or item.get("excerpt") or "")
            title = _clean(item.get("title", ""))

            where = _match(head, ELSEWHERE)
            if where:
                skipped_elsewhere += 1
                log.debug("SITP: '%s' fuori Amsterdam (%s)", title[:40], where)
                continue

            # La lista dei locali gia' coperti sta in labels.VENUE_OWNER, una
            # sola volta: questo collector ne teneva una copia sua, ed e'
            # esattamente il caso che ha fatto nascere labels.py.
            covered = owner_in_text(head, exclude=self.name)
            if covered:
                skipped_covered += 1
                log.debug("SITP: '%s' gia' coperto da %s", title[:40], covered)
                continue

            venue_token = _match(head, AMS_VENUES)
            if not venue_token and "amsterdam" not in head:
                # Nessun segnale di Amsterdam nell'intestazione. Si scarta
                # invece di indovinare: un evento perso costa meno di un
                # evento nella citta' sbagliata, e la riga di log lo rende
                # visibile se il formato delle descrizioni cambia.
                skipped_unclear += 1
                log.info("SITP: luogo non riconosciuto, saltato — '%s'", title[:50])
                continue

            ev = self._parse_item(item, title, AMS_VENUES.get(venue_token, ""))
            if ev:
                events.append(ev)

        log.info(
            "Space is the Place: %d eventi tenuti (%d fuori Amsterdam, "
            "%d gia' coperti altrove, %d luogo non chiaro)",
            len(events), skipped_elsewhere, skipped_covered, skipped_unclear,
        )
        return events

    def _parse_item(self, item: dict, title: str, venue: str) -> Event | None:
        try:
            if not title:
                return None

            # start_date e' gia' in ora locale di Amsterdam ("2026-10-09
            # 18:30:00"), a differenza del DTSTART dell'ICS che e' in UTC.
            start_raw = item.get("start_date")
            if not start_raw:
                return None
            try:
                event_date = datetime.strptime(start_raw, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                log.warning("SITP: data illeggibile '%s' su '%s'", start_raw, title[:40])
                return None

            event_end = None
            end_raw = item.get("end_date")
            if end_raw:
                try:
                    event_end = datetime.strptime(end_raw, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass

            image = item.get("image") or {}
            image_url = image.get("url", "") if isinstance(image, dict) else ""

            # Si usano le categorie del sito, non un genere inventato: SITP
            # programma jazz e improvvisazione, ma marcarlo a mano su ogni
            # evento sarebbe scriverci sopra. Chi vuole il taglio jazz lo ha
            # gia' da CATEGORIES.jazz, che filtra per fonte.
            cats = [_clean(c.get("name", "")) for c in item.get("categories") or []]
            cats = [c for c in cats if c]

            return Event(
                source=self.name,
                source_id=str(item.get("id", "")),
                source_url=item.get("url", "") or "",
                title=title,
                date=event_date,
                date_end=event_end,
                venue=venue,
                event_type=cats[0] if cats else "Concert",
                genres=cats,
                description=_clean(item.get("excerpt", ""))[:500],
                image_url=image_url,
                tickets_status="available",
            )

        except Exception as e:  # noqa: BLE001
            log.warning("Failed to parse SITP item: %s — %s", title[:40], e)
            return None
