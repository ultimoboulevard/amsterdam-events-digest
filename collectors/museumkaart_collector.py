"""
Mostre e attività dei musei di Amsterdam, da museum.nl (Museumkaart).

PERCHE' IL SITO OLANDESE E NON QUELLO INGLESE
Fino al 19/08/2026 questo collector interrogava /en/see-and-do/events/search
con il filtro Province="Noord-Holland", e raccoglieva UN evento al giorno.
Non era rotto: quel filtro funziona ed e' il sito inglese ad essere quasi
vuoto — 189 elementi in tutta l'Olanda contro i 1427 della versione olandese,
perche' l'inglese porta solo cio' che e' stato tradotto. Di quei 189, uno solo
risultava taggato Noord-Holland.

L'endpoint olandese si chiama /zoeken, non /search: cercarlo per simmetria da'
405 e manda fuori strada.

PERCHE' SI PARTE DAI MUSEI
Gli eventi non hanno coordinate: passare Lat/Lng alla ricerca eventi non
filtra niente (torna il totale) e aggiungendoci il raggio torna zero. I musei
invece sono geolocalizzati. Quindi si fa il giro largo:

    1. i musei entro RADIUS_KM da Amsterdam            → ~47 slug
    2. tutti gli eventi (paginati, 100 per volta)      → ~1427
    3. si tengono quelli il cui URL porta uno di quegli slug

Il filtro e' gratis: l'URL di ogni evento e' /nl/<slug-museo>/<tipo>/<titolo>,
quindi il confronto e' in memoria e non costa richieste.

I TITOLI SONO IN OLANDESE
Scelta consapevole (19/08/2026): il sito e' in inglese, ma l'alternativa era
tenere 30 eventi tradotti invece di ~170 reali.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from collectors import BaseCollector
from config import HEADERS, REQUEST_TIMEOUT
from models import Event

log = logging.getLogger(__name__)

BASE_URL = "https://www.museum.nl"
EVENTS_URL = f"{BASE_URL}/nl/zien-en-doen/evenementen/zoeken"
MUSEUMS_URL = f"{BASE_URL}/nl/zien-en-doen/musea/zoeken"

# Piazza Dam, Amsterdam.
AMSTERDAM_LAT, AMSTERDAM_LNG = 52.37403, 4.88969
# 10 km tiene Amsterdam e i suoi bordi (47 musei). A 25 km ne arrivano 87, ma
# entrano Haarlem e Zaandam, che su un sito di Amsterdam non c'entrano.
RADIUS_KM = 10

PAGE_SIZE = 100          # il server lo rispetta fino a 100: 1427 eventi in 15 richieste
MAX_EVENT_PAGES = 20
MAX_MUSEUM_PAGES = 6

_MONTHS_NL = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11,
    "december": 12,
}
_MONTH_RE = "|".join(_MONTHS_NL)


class MuseumkaartCollector(BaseCollector):
    """Eventi dei musei entro RADIUS_KM da Amsterdam."""

    name = "museumkaart"
    probe_url = EVENTS_URL

    def collect(self) -> list[Event]:
        log.info("Collecting events from Museumkaart (Amsterdam, %d km)…", RADIUS_KM)

        musei = self._fetch_museums()
        if not musei:
            log.error("Museumkaart: nessun museo trovato, niente da filtrare")
            return []
        log.info("Museumkaart: %d musei entro %d km", len(musei), RADIUS_KM)

        events: list[Event] = []
        senza_data = 0
        for page_idx in range(MAX_EVENT_PAGES):
            cards = self._fetch_events_page(page_idx)
            if cards is None:
                break
            for card in cards:
                slug = self._museum_slug(card)
                if slug not in musei:
                    continue
                ev = self._parse_card(card, musei[slug])
                if ev is None:
                    senza_data += 1
                else:
                    events.append(ev)
            if len(cards) < PAGE_SIZE:
                break

        if senza_data:
            # Non e' un errore: molte schede sono ricorrenze senza una data
            # leggibile. Ma se il numero esplode, il formato e' cambiato — ed e'
            # l'unico segnale che lo dice, perche' il conteggio totale regge.
            log.info("Museumkaart: %d schede scartate perche' senza data leggibile", senza_data)
        log.info("Museumkaart: collected %d events", len(events))
        return events

    # ── HTTP ───────────────────────────────────────────────────────

    @staticmethod
    def _post(url: str, payload: dict) -> BeautifulSoup | None:
        headers = {
            **HEADERS,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": BASE_URL,
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            resp = httpx.post(url, json=payload, headers=headers,
                              timeout=REQUEST_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("Museumkaart: richiesta fallita (%s): %s", url, exc)
            return None
        return BeautifulSoup(resp.text, "html.parser")

    def _fetch_museums(self) -> dict[str, str]:
        """{slug: nome} dei musei entro RADIUS_KM. Il nome diventa il `venue`."""
        musei: dict[str, str] = {}
        for page in range(MAX_MUSEUM_PAGES):
            soup = self._post(MUSEUMS_URL, {
                "BoekenweekSpecial": False,
                "Lat": AMSTERDAM_LAT,
                "Lng": AMSTERDAM_LNG,
                "MaxSearchRadiusFromLocationInKM": RADIUS_KM,
                "PageIndex": page,
                "PageSize": 20,
            })
            if soup is None:
                break
            trovati = 0
            for a in soup.select("li a[href^='/nl/']"):
                parti = a["href"].strip("/").split("/")
                if len(parti) < 2:
                    continue
                slug = parti[1]
                trovati += 1
                if slug not in musei:
                    comp = a.select_one("component")
                    musei[slug] = (comp.get_text(strip=True) if comp
                                   else slug.replace("-", " ").title())
            if trovati < 20:
                break
        return musei

    def _fetch_events_page(self, page_index: int) -> list | None:
        soup = self._post(EVENTS_URL, {"PageIndex": page_index, "PageSize": PAGE_SIZE})
        if soup is None:
            return None
        cards = soup.select("li.see-and-do-item")
        log.info("Museumkaart page %d: %d cards", page_index, len(cards))
        return cards

    # ── Parsing ────────────────────────────────────────────────────

    @staticmethod
    def _museum_slug(card) -> str:
        """Lo slug del museo sta nell'URL: /nl/<slug>/<tipo>/<titolo>."""
        a = card.select_one("a[href^='/nl/']")
        if not a:
            return ""
        parti = a["href"].strip("/").split("/")
        return parti[1] if len(parti) > 1 else ""

    def _parse_card(self, card, venue: str) -> Event | None:
        try:
            a_tag = card.select_one("a[href]")
            if not a_tag:
                return None
            href = a_tag["href"]
            source_url = f"{BASE_URL}{href}" if href.startswith("/") else href

            comps = card.select("component")
            if not comps:
                return None
            title = comps[0].get_text(strip=True)
            if not title:
                return None
            event_type = comps[1].get_text(strip=True) if len(comps) > 1 else ""

            # Il blocco della data si riconosce dall'ICONA, non dal contenuto.
            # Cercarlo per "contiene cifre o t/m" pesca anche la fascia d'eta'
            # ("Voor 5 t/m 12 jaar"), che ha entrambe: costava 40 schede su 103.
            date_text = ""
            for attr_div in card.select(".item-attribute"):
                svg = attr_div.select_one("svg")
                classi = " ".join(svg.get("class") or []) if svg else ""
                if "time" not in classi:
                    continue
                date_text = re.sub(r"\s+", " ", attr_div.get_text(" ", strip=True))
                break

            event_date, event_end = self._parse_date(date_text)
            if event_date is None:
                # NIENTE ripiego su datetime.now(). La versione precedente lo
                # faceva, e siccome dedup_key contiene il giorno, ogni raccolta
                # creava una riga nuova: 3.559 righe per 96 mostre, tutte
                # datate al giorno in cui erano state raccolte, e quindi mai
                # visibili fra gli eventi futuri. Meglio nessuna riga che una
                # riga che si moltiplica e mente sulla data.
                return None

            img = card.select_one("img")
            image_url = (img.get("data-src", "") or img.get("src", "")) if img else ""
            source_id = card.get("id", "") or href.rstrip("/").rsplit("/", 1)[-1]

            return Event(
                source=self.name,
                source_id=source_id,
                source_url=source_url,
                title=title,
                date=event_date,
                date_end=event_end,
                venue=venue,
                event_type=event_type,
                image_url=image_url,
                tickets_status="available",
            )
        except Exception as exc:
            log.warning("Museumkaart: scheda non interpretabile: %s", exc)
            return None

    # ── Date ───────────────────────────────────────────────────────

    @staticmethod
    def _anno_per(mese: int, giorno: int, riferimento: datetime) -> int:
        """L'anno non e' quasi mai scritto: se la data e' gia' passata, e' l'anno dopo."""
        anno = riferimento.year
        try:
            if datetime(anno, mese, giorno).date() < riferimento.date():
                anno += 1
        except ValueError:
            pass
        return anno

    @classmethod
    def _parse_date(cls, text: str) -> tuple[datetime | None, datetime | None]:
        """
        Formati olandesi visti sul campo, in ordine di frequenza:

            T/m 1 september                                  (mostra in corso)
            Zaterdag 12 september van 18 tot 21 uur          (data singola)
            T/m 13 september van 09 tot 17 uur               (in corso, con orari)
            5 september t/m 12 december, meerdere opties     (intervallo)

        Ritorna (inizio, fine); la fine puo' essere None.

        Sulle mostre "t/m X" si conosce solo la data di CHIUSURA, e quella si
        usa come data dell'evento: e' l'unico ancoraggio stabile che abbiamo.
        Usare "oggi" farebbe cambiare la chiave a ogni raccolta (vedi il
        commento in _parse_card). La mostra compare quindi nel suo ultimo
        giorno — un "ultima occasione", che e' vero e utile.
        """
        if not text:
            return None, None
        t = re.sub(r"\s+", " ", text.strip().lower())
        now = datetime.now()

        def ora(h: str | None, m: str | None) -> tuple[int, int]:
            # Senza orario si mette 00:00, che in tutto il progetto vuol dire
            # "ora ignota" e a video non viene mostrato. Il 10:00 di prima era
            # un default inventato: sembrava un dato ed era un riempitivo.
            return (int(h) if h else 0, int(m) if m else 0)

        # "5 september t/m 12 december" — intervallo con inizio e fine
        m = re.search(rf"(\d{{1,2}}) ({_MONTH_RE}) t/m (\d{{1,2}}) ({_MONTH_RE})", t)
        if m:
            d1, mese1, d2, mese2 = m.group(1), m.group(2), m.group(3), m.group(4)
            mo1, mo2 = _MONTHS_NL[mese1], _MONTHS_NL[mese2]
            try:
                y1 = cls._anno_per(mo1, int(d1), now)
                # 00:00 = ora ignota, come nel resto del progetto: qui il
                # sito da' solo le date, non gli orari. Il 10:00 che c'era
                # prima era un riempitivo che a video sembrava un dato.
                inizio = datetime(y1, mo1, int(d1), 0, 0)
                y2 = y1 if mo2 >= mo1 else y1 + 1
                return inizio, datetime(y2, mo2, int(d2), 23, 59)
            except ValueError:
                return None, None

        # "t/m 1 september [van 09 tot 17 uur]" — si conosce solo la chiusura
        m = re.search(rf"t/m (\d{{1,2}}) ({_MONTH_RE})(?: van (\d{{1,2}})(?::(\d{{2}}))?)?", t)
        if m:
            giorno, mese, h, mi = m.group(1), m.group(2), m.group(3), m.group(4)
            mo = _MONTHS_NL[mese]
            try:
                y = cls._anno_per(mo, int(giorno), now)
                hh, mm = ora(h, mi)
                fine = datetime(y, mo, int(giorno), hh, mm)
                return fine, fine
            except ValueError:
                return None, None

        # "zaterdag 12 september van 18[:30] tot 21 uur" — data singola
        m = re.search(
            rf"(?:\w+dag )?(\d{{1,2}}) ({_MONTH_RE})(?: van (\d{{1,2}})(?::(\d{{2}}))?)?", t)
        if m:
            giorno, mese, h, mi = m.group(1), m.group(2), m.group(3), m.group(4)
            mo = _MONTHS_NL[mese]
            try:
                y = cls._anno_per(mo, int(giorno), now)
                hh, mm = ora(h, mi)
                return datetime(y, mo, int(giorno), hh, mm), None
            except ValueError:
                return None, None

        # "doorlopend", "dagelijks", "meerdere opties" senza date: non c'e'
        # niente da ancorare, e inventarsi un giorno e' proprio il bug di prima.
        return None, None
