#!/usr/bin/env python3
"""watch_adm.py — vigila l'annuncio del programma ADM Festival.

L'evento e' inserito a mano in DB (source='manual') perche' nessuna delle 14 fonti
regolari lo pubblica. Questo script gira una volta a settimana: quando una fonte
tracciabile finalmente annuncia il programma, lascia un marker perche' io lo pulisca
a mano — non si aggiorna il DB automaticamente per evitare che un falso match
sovrascriva l'evento reale.
"""
from __future__ import annotations
import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent
LOG = BASE / "logs" / "watch_adm.log"
STATE = BASE / "data" / "watch_adm.state"
MARKER = BASE / "logs" / "watch_adm_ANNOUNCED.md"

SOURCES = [
    ("amsterdam_alternative", "https://www.amsterdamalternative.nl/agenda"),
    ("admfestival",           "https://admfestival.nl/"),
    ("admnoord",              "https://admnoord.nl/"),
]

PATTERNS = [
    r"chaos magic",
    r"adm festival",
    r"29th birthday",
    r"ruimte voor chaos",
    r"9[-\s/–]+10[-\s/–]+11\s*(october|oktober|okt|oct)",
]

def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")

def load_state() -> set[str]:
    if not STATE.exists():
        return set()
    return set(STATE.read_text(encoding="utf-8").split())

def save_state(hashes: set[str]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text("\n".join(sorted(hashes)), encoding="utf-8")

def scan(name: str, url: str) -> list[tuple[str, str]]:
    """Ritorna [(pattern, snippet)] per ogni match; [] se la fonte tace."""
    try:
        r = httpx.get(url, timeout=15, follow_redirects=True,
                      headers={"User-Agent": "FOMOdam-ADM-watch/1.0"})
        r.raise_for_status()
    except Exception as e:
        log(f"{name}: fetch failed ({e})")
        return []
    text = r.text.lower()
    hits = []
    for p in PATTERNS:
        for m in re.finditer(p, text, re.I):
            i = max(0, m.start() - 60)
            j = min(len(text), m.end() + 60)
            snip = text[i:j].replace("\n", " ").strip()
            # admfestival.nl e' un placeholder fermo al 2021: filtra il rumore
            # e accetta il match solo se lo snippet accenna a 2026 o al 29th.
            if "2021" in snip and not any(k in snip for k in ("2026", "29th", "chaos magic", "ruimte voor")):
                continue
            hits.append((p, snip))
    return hits

def main() -> int:
    seen = load_state()
    new = []
    for name, url in SOURCES:
        for pattern, snippet in scan(name, url):
            h = hashlib.sha1(f"{name}|{pattern}|{snippet}".encode()).hexdigest()[:12]
            if h in seen:
                continue
            seen.add(h)
            new.append((name, url, pattern, snippet))
            log(f"{name}: NEW match [{pattern}] -> {snippet[:120]!r}")
    save_state(seen)
    if new:
        MARKER.write_text(
            "# ADM Festival — programme announced?\n\n"
            + f"Detected {len(new)} new match(es) at {datetime.now().isoformat(timespec='seconds')}.\n\n"
            + "Verify manually and upgrade `manual_adm_2026_10_*` rows in `data/events.db` with the real programme, then delete this marker.\n\n"
            + "\n".join(f"- **{n}** ({u}) — pattern `{p}` — …{s}…" for n,u,p,s in new),
            encoding="utf-8"
        )
        log(f"MARKER written with {len(new)} hits")
        print(f"ADM watch: {len(new)} new match(es) — see {MARKER}")
        return 0
    log("no new matches")
    print("ADM watch: no new matches")
    return 0

if __name__ == "__main__":
    sys.exit(main())
