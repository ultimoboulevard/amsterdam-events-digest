"""
ndsm.nl collector — il cartellone della NDSM-werf.

Sito Webflow: niente API, niente JSON-LD, niente RSS. Si scrapa l'HTML.

DUE SORGENTI, E SOLO UNA SERVE
Il sito ha /agenda (la lista) e /sitemap.xml (676 url, di cui 144 sotto
/agenda/). Verrebbe voglia di usare la sitemap, che ha 24 volte piu' voci:
sbagliato, e' l'archivio. Su 12 slug estratti a caso dalla sitemap, 12 sono
eventi passati. La lista, che di voci ne ha 6, e' il cartellone vivo — ed e'
quello che vogliamo.

La paginazione Webflow (?<collection-id>_page=N) qui non aggiunge niente:
i tre parametri presenti restituiscono sempre gli stessi 6 slug. Si legge la
prima pagina e basta.

LA DATA SI PRENDE DALLA LISTA, NON DALLA SCHEDA
Sono due valori diversi e non e' un caso: per i ricorrenti (ij-hallen, NDSM
Kunst Toer) la scheda mostra la data di UNA edizione, la lista quella in
programma. La lista e' la fonte autorevole per "cosa succede adesso", la
scheda serve per titolo, immagine e descrizione.

NIENTE CLASSI CSS COME ANCORAGGIO
Webflow le rigenera a ogni ripubblicazione. Qui si va di og: meta tag e di
struttura del testo della card, che e' regolare:

    "23 Aug — 23 Aug   NDSM Kunst Toer   NDSM-werf   Lees meer"
     ^ intervallo       ^ titolo          ^ sala
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from collectors import BaseCollector
from config import HEADERS, REQUEST_TIMEOUT
from models import Event

log = logging.getLogger(__name__)

NDSM_BASE = "https://www.ndsm.nl"
NDSM_AGENDA = f"{NDSM_BASE}/agenda"

MONTHS = {
    "jan": 1, "feb": 2, "mrt": 3, "mar": 3, "apr": 4, "mei": 5, "may": 5,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "okt": 10, "oct": 10,
    "nov": 11, "dec": 12,
}

# "23 Aug — 23 Aug", "20 Sep — 3 Oct", "11 Apr 2026 — 12 Apr 2026".
# L'anno sulla lista non c'e' quasi mai, e va dedotto (vedi _year_for).
RANGE_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]{3,10})\.?\s*(\d{4})?"
    r"\s*[—–-]\s*"
    r"(\d{1,2})\s+([A-Za-z]{3,10})\.?\s*(\d{4})?"
)
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})")
# Meta' delle schede l'ora la scrivono da sola ("... 2026 16:00 NDSM-WERF")
# invece che come intervallo. Senza questo ripiego quegli eventi finivano a
# 00:00, che qui dentro vuol dire "ora ignota" e non si mostra: si perdeva
# un dato che c'era.
LONE_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
TRAILING = re.compile(r"\s*lees meer\s*$", re.I)


def _year_for(month: int, today: datetime) -> int:
    """L'anno mancante sulla lista.

    Un mese gia' passato vuol dire l'anno prossimo: a dicembre "3 Jan" e'
    gennaio dell'anno dopo, non gennaio appena trascorso. Il margine di un
    mese evita di spingere in avanti un evento cominciato ieri.
    """
    year = today.year
    if month < today.month - 1:
        year += 1
    return year


def _parse_day(day: str, mon: str, year: str | None, today: datetime):
    month = MONTHS.get(mon.lower()[:3])
    if not month:
        return None
    try:
        return datetime(int(year) if year else _year_for(month, today),
                        month, int(day))
    except ValueError:
        return None


def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


class NdsmCollector(BaseCollector):
    name = "ndsm"
    probe_url = NDSM_AGENDA

    def collect(self) -> list[Event]:
        log.info("Collecting events from NDSM...")

        try:
            resp = httpx.get(NDSM_AGENDA, headers=HEADERS,
                             timeout=REQUEST_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as e:  # noqa: BLE001
            log.error("Failed to fetch NDSM agenda: %s", e)
            return []

        for tag in soup(["script", "style"]):
            tag.decompose()

        cards: list[tuple[str, str]] = []
        seen: set[str] = set()
        for a in soup.select('a[href*="/agenda/"]'):
            href = urljoin(NDSM_BASE, (a.get("href") or "").split("?")[0])
            if href in seen or href.rstrip("/").endswith("/agenda"):
                continue
            seen.add(href)
            cards.append((href, _text(a)))

        log.info("NDSM: %d voci in cartellone", len(cards))

        today = datetime.now()
        events: list[Event] = []
        for url, card_text in cards:
            ev = self._build(url, card_text, today)
            if ev:
                events.append(ev)

        log.info("NDSM: collected %d events total", len(events))
        return events

    def _build(self, url: str, card_text: str, today: datetime) -> Event | None:
        try:
            m = RANGE_RE.search(card_text)
            if not m:
                log.info("NDSM: nessuna data nella card, saltato — %s",
                         card_text[:60])
                return None

            start = _parse_day(m.group(1), m.group(2), m.group(3), today)
            if not start:
                return None
            end = _parse_day(m.group(4), m.group(5), m.group(6) or m.group(3), today)
            if end and end < start:      # intervallo a cavallo di capodanno
                end = end.replace(year=end.year + 1)

            detail = self._detail(url)
            title = detail.get("title") or ""
            if not title:
                # Ripiego: la card e' "<date> <titolo> <sala> Lees meer".
                title = TRAILING.sub("", card_text[m.end():]).strip()
            if not title:
                return None

            # La sala e' cio' che sulla card avanza dopo le date e il titolo.
            tail = TRAILING.sub("", card_text[m.end():]).strip()
            venue = tail[len(title):].strip() if tail.startswith(title) else ""
            venue = venue or "NDSM-werf"

            # L'orario sta sulla scheda, non sulla card.
            if detail.get("start_time"):
                try:
                    hh, mm = detail["start_time"].split(":")
                    start = start.replace(hour=int(hh), minute=int(mm))
                except ValueError:
                    pass

            slug = url.rstrip("/").rsplit("/", 1)[-1]
            cancelled = "geannuleerd" in slug or "geannuleerd" in title.lower()

            return Event(
                source=self.name,
                source_id=slug,
                source_url=url,
                title=title,
                date=start,
                date_end=end if end and end != start else None,
                venue=venue,
                description=detail.get("description", "")[:500],
                image_url=detail.get("image", ""),
                tickets_status="cancelled" if cancelled else "available",
            )

        except Exception as e:  # noqa: BLE001
            log.warning("Failed to parse NDSM card '%s' — %s", card_text[:40], e)
            return None

    def _detail(self, url: str) -> dict:
        """Titolo, immagine, descrizione e orario dalla scheda. Mai la data."""
        out: dict[str, str] = {}
        try:
            r = httpx.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT,
                          follow_redirects=True)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as e:  # noqa: BLE001
            log.debug("NDSM: scheda non letta (%s): %s", url, e)
            return out

        def og(prop: str) -> str:
            tag = soup.find("meta", property=prop)
            return (tag.get("content") or "").strip() if tag else ""

        # og:title e' "<titolo> | NDSM Amsterdam": il suffisso del sito va via.
        title = og("og:title")
        out["title"] = re.sub(r"\s*\|\s*NDSM Amsterdam\s*$", "", title).strip()
        out["description"] = og("og:description")
        out["image"] = og("og:image")

        for tag in soup(["script", "style"]):
            tag.decompose()
        text = _text(soup)
        t = TIME_RE.search(text)
        if t:
            out["start_time"] = t.group(1)
        else:
            lone = LONE_TIME_RE.search(text)
            if lone:
                out["start_time"] = lone.group(0)
        return out
