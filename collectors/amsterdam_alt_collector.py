"""
Amsterdam Alternative event collector.

AmsterdamAlternative.nl loads events dynamically via a JSON API:
  GET /services/get-events-past-v25.php?month=5&months=1&image=true

Returns { daterange, query, filter, items: [ { id, title, venue, start, price,
type: {id, label}, header: {id, kind, file}, lineup, ... } ] }
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

import httpx

from collectors import BaseCollector
from config import HEADERS, REQUEST_TIMEOUT
from models import Event

log = logging.getLogger(__name__)

AA_API_BASE = "https://www.amsterdamalternative.nl/services/get-events-past-v25.php"
AA_MEDIA_BASE = "https://www.amsterdamalternative.nl/media/content/"


# ── lineup ─────────────────────────────────────────────────────────
# `lineup` non e' una lista: e' un campo di testo che i redattori riempiono
# a mano, e infatti arriva in tutte le forme. Su 73 item di un mese si trova
# "Toody Cole & Her Band, Jenny Don't & the Spurs" (pulito), "Lilian Vieira
# — Vocals\r\nRemko de Landmeter — Flute" (nome + ruolo, per riga),
# "Participating artists: ..." (prefisso), "Deli Girls (synth punk, digital
# hardcore) / NYC, USA" (descrittori e provenienza), e "TBA" o "19.00- 21.00
# OPen MIc" (niente artisti). Si taglia via quello che non e' un nome
# invece di fidarsi del separatore.
_LINEUP_JUNK = {
    "tba", "tbc", "and more", "more tba", "meer", "en meer", "special guest",
    "guests", "and guests", "e mais", "more", "others", "friends", "n.n.",
    "usa", "us", "uk", "nl", "tx", "ny", "nyc", "amsterdam", "rotterdam",
    "netherlands", "the netherlands", "den haag", "utrecht", "berlin",
}
_LINEUP_ROLES = {
    "vocals", "vocal", "voice", "zang", "singer", "dancer", "dance",
    "keyboardist", "flautist", "flute", "percussionist", "percussion",
    "bassist", "bass", "double base", "composer", "compositions", "drummer",
    "drums", "guitarist", "guitar", "cello", "viola", "violin", "piano",
    "trumpet", "vibraphone", "cavaquinho", "harmonium", "producer", "dj set",
    "regie", "productie", "hosted by werepair",
}
# Dopo "and"/"en"/"e" queste parole sono la coda di UN nome, non un secondo
# nome: "Kiala & The Afroblaster", "Zea & Drumband ...".
_LINEUP_KEEP_AFTER = ("the ", "de ", "het ", "her ", "his ", "band")
_LINEUP_SPLIT_WORDS = (" and ", " en ", " e ")
_LINEUP_TAIL = re.compile(
    r"\s*(?:and|en|e)\s+(?:more|meer|guests|others|friends|mais)\s*\.?$", re.I
)


def _parse_lineup(raw: str, cap: int = 12) -> list[str]:
    """Nomi ricavati dal campo `lineup`, o lista vuota se non ce ne sono.

    Vale la pena leggerlo: senza, questi eventi finiscono in enrich_artists.py
    e gli artisti si pagano a Gemini per estrarli dal titolo, che ne ha meno.
    """
    if not raw:
        return []
    text = raw.replace("\xa0", " ")
    # I descrittori si tolgono PRIMA di spezzare: dentro le parentesi ci sono
    # virgole ("(synth punk, electro-industrial)") e spezzando prima si
    # otterrebbero generi al posto dei nomi.
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text)
    text = re.sub(r"[\r\n]+", ",", text)

    out: list[str] = []
    # Solo " / " spaziato: "sikau/půbalová" e "Boogaerdt/VanderSchoot" sono
    # nomi singoli e la barra nuda li spezzerebbe a meta'.
    for chunk in re.split(r",|\s/\s|\|", text):
        c = _LINEUP_TAIL.sub("", chunk)
        if ":" in c:                              # "Zang:", "Participating artists:"
            c = c.rsplit(":", 1)[1]
        c = re.split(r"\s+[—–-]\s+", c)[0]         # "Lilian Vieira — Vocals"
        parts = [c]
        for sep in _LINEUP_SPLIT_WORDS:
            nxt: list[str] = []
            for part in parts:
                bits = part.split(sep)
                acc = bits[0]
                for b in bits[1:]:
                    if b.strip().lower().startswith(_LINEUP_KEEP_AFTER):
                        acc += sep + b
                    else:
                        nxt.append(acc)
                        acc = b
                nxt.append(acc)
            parts = nxt
        for part in parts:
            name = re.sub(r"\s+", " ", part).strip(" .!·-–—+&").strip()
            low = name.lower()
            if not name or len(name) < 2 or len(name) > 60:
                continue
            if low in _LINEUP_JUNK or low in _LINEUP_ROLES:
                continue
            if any(ch.isdigit() for ch in name):  # orari, "day 1"
                continue
            if low.startswith(("at ", "@")):      # "at Radio Radio" e' la sede
                continue
            if name not in out:
                out.append(name)
    return out[:cap]


class AmsterdamAltCollector(BaseCollector):
    name = "amsterdam_alt"
    probe_url = AA_API_BASE

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

    def _image_url(self, item: dict) -> str:
        """URL della copertina, ricostruito da `header`.

        L'API da' `{"id": 19755, "kind": "image", "file": "19755.png"}`, ma
        `file` e' il nome dell'originale caricato e su /media/content/ non
        esiste: serve `{id}_large.jpg`, con estensione .jpg qualunque sia
        quella di `file`. Attenzione, il sito risponde **200 con una pagina
        HTML** ai percorsi mancanti invece che 404, quindi un URL sbagliato
        supera qualsiasi controllo sul codice di stato e si scopre solo
        aprendolo: verificato su 20 item, `{id}_large.jpg` da' 20 image/jpeg.
        """
        header = item.get("header")
        if isinstance(header, dict):
            if header.get("kind") != "image":
                return ""
            header_id = header.get("id")
            return f"{AA_MEDIA_BASE}{header_id}_large.jpg" if header_id else ""
        # Ripiego sulle chiavi che l'API usava prima di `header`.
        legacy = item.get("header_image", "") or item.get("image", "") or ""
        if isinstance(legacy, dict):
            legacy = legacy.get("url", "") or legacy.get("src", "") or ""
        return str(legacy)

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
            # `label` e' la chiave di oggi; `name` resta come ripiego perche'
            # e' quella che l'API usava prima e costa una riga tenerla.
            type_raw = item.get("type", "")
            if isinstance(type_raw, dict):
                event_type = (type_raw.get("label") or type_raw.get("name") or "").strip()
            elif isinstance(type_raw, list):
                # Could be a list of type dicts
                event_type = ", ".join(
                    (t.get("label") or t.get("name") or str(t)) if isinstance(t, dict) else str(t)
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

            image_url = self._image_url(item)

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
                    nums = re.findall(r"[\d.]+", price)
                    if nums:
                        try:
                            price_min = float(nums[0])
                        except ValueError:
                            pass

            # Genres from type field
            genres = [g.strip() for g in event_type.split(",") if g.strip()] if event_type else []

            # `lineup` e' testo libero, non una lista: vedi _parse_lineup.
            artists = _parse_lineup(item.get("lineup", ""))

            return Event(
                source=self.name,
                source_id=event_id,
                source_url=source_url,
                title=title,
                date=event_date,
                date_end=event_end,
                venue=venue,
                event_type=event_type,
                artists=artists,
                genres=genres,
                image_url=image_url,
                price_min=price_min,
                tickets_status=tickets_status,
            )

        except Exception as e:
            log.warning("Failed to parse AA item: %s — %s", item.get("title", "?"), e)
            return None
