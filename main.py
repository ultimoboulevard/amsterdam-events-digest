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
    python main.py stats                # Show DB stats
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
from db import init_db, upsert_many, get_upcoming_events, get_stats
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

COLLECTORS = {
    "melkweg": MelkwegCollector,
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


def cmd_collect(args: argparse.Namespace) -> None:
    """Run collectors and store events in DB."""
    init_db()

    sources = [args.source] if args.source else list(COLLECTORS.keys())

    total_inserted = total_updated = 0
    for source_name in sources:
        cls = COLLECTORS.get(source_name)
        if not cls:
            log.error("Unknown source: %s (available: %s)", source_name, list(COLLECTORS.keys()))
            continue

        collector = cls()
        events = collector.collect()

        if events:
            inserted, updated = upsert_many(events)
            total_inserted += inserted
            total_updated += updated
        else:
            log.warning("No events collected from %s", source_name)

    log.info("═══ Collection complete: %d new, %d updated ═══", total_inserted, total_updated)


def cmd_digest(args: argparse.Namespace) -> None:
    """Generate HTML digest from DB."""
    init_db()
    events = get_upcoming_events(days=args.days, source=args.source)

    if not events:
        log.warning("No upcoming events found in DB. Run 'collect' first.")
        return

    path = build_html_digest(events)
    log.info("Digest ready: %s", path)

    if args.open:
        webbrowser.open(f"file://{path.resolve()}")


def cmd_run(args: argparse.Namespace) -> None:
    """Collect + digest in one shot."""
    cmd_collect(args)
    cmd_digest(args)


def cmd_send(args: argparse.Namespace) -> None:
    """Collect this week's events, build digest, and email it."""
    # Force days=6 so the digest covers Monday → Sunday
    args.days = 6
    args.source = None
    args.open = False
    cmd_collect(args)
    cmd_digest(args)

    ok = send_digest()
    if ok:
        log.info("═══ Weekly digest sent successfully ═══")
    else:
        log.error("═══ Failed to send weekly digest ═══")
        sys.exit(1)


def cmd_site(args: argparse.Namespace) -> None:
    """Export events JSON and open the static site locally."""
    args.source = None
    cmd_collect(args)
    path = export_events_json(days=45)
    log.info("JSON exported: %s", path)

    index = SITE_DIR / "index.html"
    if index.exists():
        webbrowser.open(f"file://{index.resolve()}")
        log.info("Opened site in browser")


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

    # stats
    p_stats = sub.add_parser("stats", help="Show database statistics")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
