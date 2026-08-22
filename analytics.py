#!/usr/bin/env python3
"""
Accessi al sito — Cloudflare Web Analytics.

Il beacon in site/index.html manda i pageload a Cloudflare; i numeri vivono
solo la', quindi vanno chiesti alla GraphQL Analytics API. Non c'e' nulla da
contare in locale: il DB contiene eventi, non visite.

    export CF_API_TOKEN=...        # token con permesso "Account Analytics: Read"
    export CF_ACCOUNT_ID=...       # dashboard Cloudflare, colonna destra
    python analytics.py                 # ultimi 7 giorni
    python analytics.py --days 30       # ultimi 30
    python analytics.py --json          # per farci sopra altre cose

Il site tag e' il token del beacon, gia' pubblico in site/index.html: e'
un identificatore di sito, non una credenziale. La credenziale e' CF_API_TOKEN
e non va committata.

Nota sull'orizzonte: Cloudflare Web Analytics nel piano gratuito conserva
circa 6 mesi, e il beacon e' stato installato il 18/08/2026 — prima di quella
data non esistono dati, non e' un errore dello script.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta

import httpx

API_URL = "https://api.cloudflare.com/client/v4/graphql"

# Token del beacon in site/index.html — identifica il sito, non autentica.
DEFAULT_SITE_TAG = "7d1d5dc28c4046539e1037ca65949b0a"

REQUEST_TIMEOUT = 30

# count = pageview, sum.visits = visite (sessioni: un pageload senza referrer
# interno). Le due cifre rispondono a domande diverse e le stampiamo entrambe:
# per un accredito stampa serve "visite", per capire cosa leggono i pageview.
QUERY = """
query Accessi($accountTag: String!, $siteTag: String!, $start: Date!, $end: Date!) {
  viewer {
    accounts(filter: {accountTag: $accountTag}) {
      totale: rumPageloadEventsAdaptiveGroups(
        filter: {siteTag: $siteTag, date_geq: $start, date_leq: $end}
        limit: 1
      ) {
        count
        sum { visits }
      }
      perGiorno: rumPageloadEventsAdaptiveGroups(
        filter: {siteTag: $siteTag, date_geq: $start, date_leq: $end}
        orderBy: [date_ASC]
        limit: 400
      ) {
        count
        sum { visits }
        dimensions { date }
      }
      perPagina: rumPageloadEventsAdaptiveGroups(
        filter: {siteTag: $siteTag, date_geq: $start, date_leq: $end}
        orderBy: [count_DESC]
        limit: 10
      ) {
        count
        dimensions { requestPath }
      }
      perProvenienza: rumPageloadEventsAdaptiveGroups(
        filter: {siteTag: $siteTag, date_geq: $start, date_leq: $end}
        orderBy: [count_DESC]
        limit: 10
      ) {
        count
        dimensions { refererHost }
      }
      perPaese: rumPageloadEventsAdaptiveGroups(
        filter: {siteTag: $siteTag, date_geq: $start, date_leq: $end}
        orderBy: [count_DESC]
        limit: 10
      ) {
        count
        dimensions { countryName }
      }
    }
  }
}
"""


def fetch(token: str, account_id: str, site_tag: str, days: int) -> dict:
    """Interroga la GraphQL API e restituisce il blocco dell'account."""
    end = date.today()
    start = end - timedelta(days=days - 1)

    resp = httpx.post(
        API_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "query": QUERY,
            "variables": {
                "accountTag": account_id,
                "siteTag": site_tag,
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()

    # GraphQL risponde 200 anche quando fallisce: l'errore sta nel corpo.
    if payload.get("errors"):
        for err in payload["errors"]:
            print(f"errore API: {err.get('message', err)}", file=sys.stderr)
        raise SystemExit(1)

    accounts = payload["data"]["viewer"]["accounts"]
    if not accounts:
        raise SystemExit(
            "Nessun account per questo CF_ACCOUNT_ID (o il token non lo vede)."
        )
    return accounts[0]


def _rows(groups: list[dict], key: str) -> list[tuple[str, int]]:
    """Appiattisce un gruppo GraphQL in coppie (etichetta, conteggio)."""
    out = []
    for g in groups:
        label = g["dimensions"].get(key) or "(nessuno)"
        out.append((label, g["count"]))
    return out


def _print_block(titolo: str, righe: list[tuple[str, int]]) -> None:
    if not righe:
        return
    print(f"\n{titolo}")
    larghezza = max(len(str(label)) for label, _ in righe)
    for label, count in righe:
        print(f"  {str(label):<{larghezza}}  {count:>6}")


def report(dati: dict, days: int) -> None:
    totale = dati["totale"]
    pageview = totale[0]["count"] if totale else 0
    visite = totale[0]["sum"]["visits"] if totale else 0

    print(f"events.zenobj.net — ultimi {days} giorni")
    print(f"  visite    {visite}")
    print(f"  pageview  {pageview}")

    per_giorno = [
        (g["dimensions"]["date"], g["sum"]["visits"]) for g in dati["perGiorno"]
    ]
    _print_block("Visite per giorno", per_giorno)
    _print_block("Pagine piu' viste", _rows(dati["perPagina"], "requestPath"))
    _print_block("Da dove arrivano", _rows(dati["perProvenienza"], "refererHost"))
    _print_block("Paesi", _rows(dati["perPaese"], "countryName"))

    if pageview == 0:
        print(
            "\nZero pageview: o il periodo precede l'installazione del beacon "
            "(18/08/2026), o il site tag non e' quello giusto.",
            file=sys.stderr,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="giorni indietro (default 7)")
    parser.add_argument("--site-tag", default=os.getenv("CF_SITE_TAG", DEFAULT_SITE_TAG))
    parser.add_argument("--json", action="store_true", help="output grezzo, senza tabelle")
    args = parser.parse_args()

    token = os.getenv("CF_API_TOKEN")
    account_id = os.getenv("CF_ACCOUNT_ID")
    if not token or not account_id:
        raise SystemExit(
            "Servono CF_API_TOKEN e CF_ACCOUNT_ID nell'ambiente.\n"
            "Il token si crea su dash.cloudflare.com → My Profile → API Tokens,\n"
            "con permesso Account → Account Analytics → Read."
        )

    dati = fetch(token, account_id, args.site_tag, args.days)
    if args.json:
        print(json.dumps(dati, indent=2))
    else:
        report(dati, args.days)


if __name__ == "__main__":
    main()
