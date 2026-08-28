#!/usr/bin/env python3
"""
Amsterdam Event Aggregator — CLI entry point.

Usage:
    python main.py collect              # Scrape all sources → DB
    python main.py collect --source melkweg  # Scrape only Melkweg
    python main.py digest               # Generate HTML digest from DB
    python main.py digest --days 7      # Next 7 days only
    python main.py run                  # collect + digest in one shot
    python main.py send                 # collect + digest (this week) + email
    python main.py build-pages          # Rebuild the static /events/ pages
    python main.py stats                # Show DB stats
    python main.py health               # Fill rates per source + broken-scraper alerts
"""
from __future__ import annotations

import argparse
import logging
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

from config import OUTPUT_DIR
from export_json import export_events_json, SITE_DIR
from build_pages import build_pages
from dedup import collapse_duplicates
from db import init_db, upsert_many, get_upcoming_events, get_stats
from health import fill_rates, record_run, run_checks, write_report
from html_builder import build_html_digest
from mailer import send_digest

# ── Collector registry ────────────────────────────────────────────
from collectors.melkweg_collector import MelkwegCollector
from collectors.amsterdam_alt_collector import AmsterdamAltCollector
from collectors.ra_collector import ResidentAdvisorCollector
from collectors.murmur_collector import MurmurCollector
from collectors.paradiso_collector import ParadisoCollector
from collectors.museumkaart_collector import MuseumkaartCollector
from collectors.gallery_viewer_collector import GalleryViewerCollector
from collectors.muziekgebouw_collector import MuziekgebouwCollector
from collectors.concertgebouw_collector import ConcertgebouwCollector
from collectors.splendor_collector import SplendorCollector
from collectors.bimhuis_collector import BimhuisCollector
from collectors.sitp_collector import SitpCollector
from collectors.offbeat_collector import OffbeatCollector
from collectors.ndsm_collector import NdsmCollector

# L'ORDINE QUI CONTA, e non e' estetico.
# `dedup_key` e' la PRIMARY KEY (models.py): due fonti che descrivono lo
# stesso evento producono la stessa chiave, e l'upsert fa vincere CHI SCRIVE
# PER ULTIMO — riga sovrascritta, campo `source` compreso.
# Quindi le fonti generaliste, che rastrellano mezza citta', vanno PRIMA di
# quelle specifiche, che sui loro eventi hanno i dati migliori.
# Misurato: lanciando `offbeat` da sola dopo le altre si prende 133 righe
# altrui, `murmur` scende da 9 eventi a 1, `amsterdam_alt` da 125 a 72, e
# health accende due allarmi temporali. Nell'ordine qui sotto non succede.
COLLECTORS = {
    "melkweg": MelkwegCollector,
    "sitp": SitpCollector,
    "offbeat": OffbeatCollector,
    "ndsm": NdsmCollector,
    "amsterdam_alt": AmsterdamAltCollector,
    "ra": ResidentAdvisorCollector,
    "murmur": MurmurCollector,
    "paradiso": ParadisoCollector,
    "museumkaart": MuseumkaartCollector,
    "gallery_viewer": GalleryViewerCollector,
    "muziekgebouw": MuziekgebouwCollector,
    "concertgebouw": ConcertgebouwCollector,
    "splendor": SplendorCollector,
    "bimhuis": BimhuisCollector,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(name)-25s │ %(levelname)-5s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


def cmd_collect(args: argparse.Namespace) -> list[str]:
    """
    Run collectors and store events in DB.

    Returns the list of sources that collapsed to zero events despite already
    having events in the DB — i.e. a scraper that used to work and just broke.
    Callers propagate this so the process exits non-zero and CI goes red.
    """
    init_db()

    sources = [args.source] if args.source else list(COLLECTORS.keys())
    known_counts = get_stats()["by_source"]

    broken: list[str] = []
    fresh: dict[str, tuple[int, dict[str, float]]] = {}
    total_inserted = total_updated = 0
    for source_name in sources:
        cls = COLLECTORS.get(source_name)
        if not cls:
            log.error("Unknown source: %s (available: %s)", source_name, list(COLLECTORS.keys()))
            continue

        collector = cls()
        try:
            events = collector.collect()
        except Exception as e:  # noqa: BLE001 — one raising scraper must not kill the others
            log.exception(
                "BROKEN SOURCE: %s raised %s: %s — treating as 0 events, continuing",
                source_name, type(e).__name__, e,
            )
            broken.append(source_name)
            events = []

        # L'impronta si prende sull'output del collector, non sul DB: il DB
        # conserva le righe buone di ieri, quindi una rottura di oggi ci si
        # vedrebbe solo diluita.
        record_run(source_name, events)
        if events:
            fresh[source_name] = (len(events), fill_rates(events))

        if events:
            inserted, updated = upsert_many(events)
            total_inserted += inserted
            total_updated += updated
        elif known_counts.get(source_name):
            # Historically active source that now yields nothing: the site
            # almost certainly changed its markup. Never let this pass quietly.
            log.error(
                "BROKEN SOURCE: %s returned 0 events but has %d in the DB — scraper likely stale",
                source_name,
                known_counts[source_name],
            )
            broken.append(source_name)
        else:
            log.warning("No events collected from %s", source_name)

    log.info("═══ Collection complete: %d new, %d updated ═══", total_inserted, total_updated)

    if broken:
        log.error("═══ %d broken source(s): %s ═══", len(broken), ", ".join(broken))

    _check_health(fresh)

    return broken


def _check_health(fresh: dict[str, tuple[int, dict[str, float]]]) -> None:
    """Controlli di salute + diagnosi, subito dopo la raccolta.

    Non solleva e NON cambia il codice d'uscita. Il non-zero resta riservato
    alla fonte crollata a zero, come prima: run_pipeline.sh incatena i passi
    con `&&`, quindi un exit 1 qui fermerebbe export_json, build_pages e
    indexnow — e il sito smetterebbe di aggiornarsi per una rottura parziale
    che riguarda un campo su nove di una fonte su undici. amsterdam_alt e'
    rotto ADESSO: sarebbe successo dal primo giorno.
    """
    try:
        alerts = run_checks(fresh or None)
    except Exception as e:  # noqa: BLE001 — la salute non fa fallire la raccolta
        log.warning("Controlli di salute saltati (%s: %s)", type(e).__name__, e)
        return

    if not alerts:
        log.info("Salute degli scraper: nessun allarme.")
        return

    for a in alerts:
        log.error("SALUTE: %s", a.line())

    # La diagnosi col modello e' opzionale per costruzione: se manca la chiave
    # o l'API e' giu', restano gli allarmi qui sopra, che sono il punto.
    diagnoses = ""
    try:
        from diagnose import diagnose_alerts
        diagnoses = diagnose_alerts(alerts)
    except Exception as e:  # noqa: BLE001
        log.warning("Diagnosi saltata (%s: %s)", type(e).__name__, e)

    try:
        path = write_report(alerts, diagnoses=diagnoses)
        log.error("═══ %d allarme/i di salute · report in %s ═══", len(alerts), path)
    except Exception as e:  # noqa: BLE001
        log.warning("Report di salute non scritto (%s: %s)", type(e).__name__, e)


def cmd_digest(args: argparse.Namespace) -> None:
    """Generate HTML digest from DB."""
    init_db()
    # Stesso dedup del sito: il digest pesca dallo stesso DB e finora mandava
    # le stesse righe doppie, per esempio "Cheeky Monday" del Melkweg accanto a
    # "Cheeky Monday: Ray Keith" di Resident Advisor.
    events = collapse_duplicates(get_upcoming_events(days=args.days, source=args.source))

    if not events:
        log.warning("No upcoming events found in DB. Run 'collect' first.")
        return

    path = build_html_digest(events)
    log.info("Digest ready: %s", path)

    if args.open:
        webbrowser.open(f"file://{path.resolve()}")


def cmd_run(args: argparse.Namespace) -> list[str]:
    """Collect + digest in one shot."""
    broken = cmd_collect(args)
    cmd_digest(args)
    return broken


def cmd_send(args: argparse.Namespace) -> list[str]:
    """Collect this week's events, build digest, and email it."""
    # Force days=6 so the digest covers Monday → Sunday
    args.days = 6
    args.source = None
    args.open = False
    broken = cmd_collect(args)
    cmd_digest(args)

    # A broken source must not stop the digest — send first, fail loudly after.
    ok = send_digest()
    if ok:
        log.info("═══ Weekly digest sent successfully ═══")
    else:
        log.error("═══ Failed to send weekly digest ═══")
        sys.exit(1)

    return broken


def cmd_site(args: argparse.Namespace) -> list[str]:
    """Export events JSON and open the static site locally."""
    args.source = None
    broken = cmd_collect(args)
    path = export_events_json()
    log.info("JSON exported: %s", path)
    stats = build_pages()
    log.info("Static pages: %d under site/events/", stats["pages"])

    index = SITE_DIR / "index.html"
    if index.exists():
        webbrowser.open(f"file://{index.resolve()}")
        log.info("Opened site in browser")

    return broken


def cmd_build_pages(args: argparse.Namespace) -> None:
    """Rebuild the indexable static pages under site/events/."""
    stats = build_pages()
    log.info(
        "%d pagine: %d giorni, %d mesi, 7 finestre, 1 indice (%d eventi letti)",
        stats["pages"], stats["days"], stats["months"], stats["events"],
    )


def cmd_health(args: argparse.Namespace) -> None:
    """Stato di salute degli scraper, senza scaricare niente."""
    from health import current_state, print_report

    state = current_state()
    alerts = run_checks(state)
    print_report(state, alerts)
    if args.write:
        print(f"\nReport: {write_report(alerts, state)}")


def cmd_stats(args: argparse.Namespace) -> None:
    """Show database stats."""
    init_db()
    stats = get_stats()
    print(f"\n📊 Database Stats")
    print(f"   Total events: {stats['total']}")
    print(f"   By source:")
    for src, cnt in stats["by_source"].items():
        print(f"      {src}: {cnt}")
    print(f"   Top venues:")
    for venue, cnt in stats["top_venues"].items():
        print(f"      {venue}: {cnt}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="🎛️ Amsterdam Event Aggregator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # collect
    p_collect = sub.add_parser("collect", help="Scrape events from sources")
    p_collect.add_argument("--source", choices=list(COLLECTORS.keys()), help="Scrape only this source")
    p_collect.set_defaults(func=cmd_collect)

    # digest
    p_digest = sub.add_parser("digest", help="Generate HTML digest")
    p_digest.add_argument("--days", type=int, default=14, help="Number of days ahead (default: 14)")
    p_digest.add_argument("--source", choices=list(COLLECTORS.keys()), help="Filter by source")
    p_digest.add_argument("--open", action="store_true", help="Open digest in browser")
    p_digest.set_defaults(func=cmd_digest)

    # run
    p_run = sub.add_parser("run", help="Collect + generate digest")
    p_run.add_argument("--days", type=int, default=14, help="Number of days ahead")
    p_run.add_argument("--source", default=None, help="Filter by source")
    p_run.add_argument("--open", action="store_true", help="Open digest in browser")
    p_run.set_defaults(func=cmd_run)

    # send (used by GitHub Actions)
    p_send = sub.add_parser("send", help="Collect this week + digest + email")
    p_send.set_defaults(func=cmd_send)

    # site
    p_site = sub.add_parser("site", help="Collect + export JSON + open calendar site")
    p_site.set_defaults(func=cmd_site)

    # build-pages
    p_pages = sub.add_parser(
        "build-pages",
        help="Rebuild the static, indexable pages under site/events/ (no scraping)",
    )
    p_pages.set_defaults(func=cmd_build_pages)

    # health
    p_health = sub.add_parser(
        "health",
        help="Riempimento dei campi per fonte + allarmi (nessuno scraping)",
    )
    p_health.add_argument("--write", action="store_true",
                          help="scrive anche logs/health_report.md")
    p_health.set_defaults(func=cmd_health)

    # stats
    p_stats = sub.add_parser("stats", help="Show database statistics")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    broken = args.func(args)

    # Exit non-zero so a silently-broken scraper turns the CI run red.
    if broken:
        sys.exit(1)


if __name__ == "__main__":
    main()
