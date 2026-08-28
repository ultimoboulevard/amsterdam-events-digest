#!/usr/bin/env python3
"""
Confronta il sito pubblicato con il codice in questo repo.

La pipeline gira su capitan e il repo non e' garantito allineato: il JSON
pubblicato il 28/08/2026 conteneva quattro fonti — offbeat, ndsm, sitp,
manual — che qui non esistono, e offbeat da sola valeva il 40% degli eventi.
Codice che regge il sito e vive solo su una macchina e' codice che si perde.

Questo script scarica il JSON pubblico, ne estrae le fonti e le confronta con
il registro COLLECTORS di main.py. Non serve nessuna credenziale.

    python check_drift.py
    python check_drift.py --url https://events.zenobj.net/data/events.json

Esce 1 se trova disallineamenti, cosi' si puo' mettere in un cron o in CI.
"""
from __future__ import annotations

import argparse
import collections
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx

DEFAULT_URL = "https://events.zenobj.net/data/events.json"
MAIN_PY = Path(__file__).resolve().parent / "main.py"
REQUEST_TIMEOUT = 60

# Soglia oltre la quale il JSON pubblicato e' vecchio: il collect gira ogni
# mattina alle 06:00, quindi oltre 36 ore c'e' qualcosa di rotto.
STALE_HOURS = 36

# Fonti che il JSON pubblica e che NON sono collector, per costruzione.
# `manual`: righe inserite a mano nel DB quando nessuna fonte copre un evento
# (watch_adm.py lo documenta e ne aggiunge). Senza questa riga il confronto le
# segnalava come "codice che esiste solo su capitan", e lo script non poteva
# uscire 0 nemmeno con il repo perfettamente allineato — cioe' proprio il caso
# che deve saper riconoscere.
SORGENTI_NON_COLLECTOR = {"manual"}


def collectors_nel_repo() -> set[str]:
    """Chiavi del dizionario COLLECTORS in main.py, lette senza importarlo."""
    testo = MAIN_PY.read_text()
    blocco = re.search(r"COLLECTORS\s*=\s*\{(.*?)\n\}", testo, re.S)
    if not blocco:
        raise SystemExit("COLLECTORS non trovato in main.py: il file e' cambiato.")
    return set(re.findall(r'"([^"]+)"\s*:', blocco.group(1)))


def scarica(url: str) -> dict:
    resp = httpx.get(url, timeout=REQUEST_TIMEOUT, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args()

    dati = scarica(args.url)
    eventi = dati.get("events", [])
    conteggi = collections.Counter(e.get("source", "") for e in eventi)
    pubblicate = set(conteggi)
    nel_repo = collectors_nel_repo()

    generato = datetime.fromisoformat(dati["generated_at"])
    eta = datetime.now() - generato
    date = [e["date"][:10] for e in eventi if e.get("date")]

    print(f"pubblicato   {args.url}")
    print(f"generato     {generato:%Y-%m-%d %H:%M} ({eta.days}g {eta.seconds // 3600}h fa)")
    print(f"eventi       {dati.get('event_count', len(eventi))}")
    if date:
        print(f"orizzonte    {min(date)} → {max(date)}")

    problemi = []

    if eta > timedelta(hours=STALE_HOURS):
        problemi.append(f"JSON fermo da {eta.days}g: il collect su capitan non gira.")

    # Fonti che il sito pubblica e che qui non esistono: codice solo su capitan.
    solo_in_produzione = pubblicate - nel_repo - SORGENTI_NON_COLLECTOR - {""}
    if solo_in_produzione:
        problemi.append("collector presenti in produzione ma non nel repo")
        print("\nnon versionati — il codice esiste solo su capitan:")
        for s in sorted(solo_in_produzione, key=lambda s: -conteggi[s]):
            quota = 100 * conteggi[s] / max(len(eventi), 1)
            print(f"  {s:<16}{conteggi[s]:>5}  ({quota:.0f}% del pubblicato)")

    # Il contrario: un collector che esiste qui ma non produce nulla e' rotto,
    # oppure e' stato rimosso in produzione senza toglierlo dal registro.
    muti = sorted(nel_repo - pubblicate)
    if muti:
        problemi.append("collector nel repo senza eventi pubblicati")
        print("\nmuti — nel repo ma zero eventi sul sito:")
        for s in muti:
            print(f"  {s}")

    print("\nfonti pubblicate:")
    for s, n in conteggi.most_common():
        if s in nel_repo:
            marca = " ·"
        elif s in SORGENTI_NON_COLLECTOR:
            marca = " · (inserita a mano, nessun collector)"
        else:
            marca = " ← non nel repo"
        print(f"  {s or '(vuota)':<16}{n:>5}{marca}")

    if problemi:
        print("\ndisallineamenti:")
        for p in problemi:
            print(f"  - {p}")
        sys.exit(1)

    print("\nrepo e produzione allineati.")


if __name__ == "__main__":
    main()
