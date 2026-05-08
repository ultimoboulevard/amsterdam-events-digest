"""
HTML digest builder — generates a beautiful, self-contained HTML file
with upcoming events grouped by date.
"""
from __future__ import annotations

import html
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from config import OUTPUT_DIR, CITY
from models import Event
from discovery import DiscoveryEngine

log = logging.getLogger(__name__)


def _ics_escape(text: str) -> str:
    """Escape special characters for ICS text values."""
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _write_ics_file(ev: Event, ics_dir: Path) -> str:
    """Write a .ics file for the event and return its relative path from output/.

    End time is capped at 23:59 of the event day so the calendar entry
    never spills into the next day.
    """
    ics_dir.mkdir(parents=True, exist_ok=True)

    start = ev.date
    midnight_cap = start.replace(hour=23, minute=59, second=0)

    if ev.date_end:
        end = min(ev.date_end, midnight_cap)
    else:
        # Default: start + 3h, but never past 23:59
        end = min(start + timedelta(hours=3), midnight_cap)

    # Ensure end is always after start
    if end <= start:
        end = midnight_cap

    dt_fmt = "%Y%m%dT%H%M%S"

    # Build description
    parts = []
    if ev.event_type:
        parts.append(ev.event_type)
    if ev.artists:
        parts.append("Artists: " + ", ".join(ev.artists[:6]))
    if ev.source_url:
        parts.append(ev.source_url)
    description = " — ".join(parts)

    location = f"{ev.venue}, Amsterdam" if ev.venue else "Amsterdam"
    uid = ev.dedup_key or ev.source_id or "evt"

    ics_content = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//AmsterdamEvents//Digest//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"DTSTART:{start.strftime(dt_fmt)}",
        f"DTEND:{end.strftime(dt_fmt)}",
        f"SUMMARY:{_ics_escape(ev.title)}",
        f"LOCATION:{_ics_escape(location)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        f"URL:{ev.source_url}",
        f"UID:{uid}@amsterdamevents",
        "STATUS:CONFIRMED",
        "END:VEVENT",
        "END:VCALENDAR",
    ])

    # Sanitize filename
    safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in ev.title)[:40].strip()
    filename = f"{uid}_{safe_name}.ics"
    ics_path = ics_dir / filename
    ics_path.write_text(ics_content, encoding="utf-8")

    return f"ics/{filename}"


def _event_type_emoji(event_type: str) -> str:
    """Map event types to emojis."""
    return {
        "Concert": "🎸",
        "Club": "🎧",
        "Film": "🎬",
        "Festival": "🎪",
        "Expositie": "🖼️",
    }.get(event_type, "📌")


def _status_badge(status: str) -> str:
    """Render a ticket status badge."""
    badges = {
        "sold_out": '<span class="badge sold-out">Sold Out</span>',
        "cancelled": '<span class="badge cancelled">Cancelled</span>',
        "rescheduled": '<span class="badge rescheduled">Rescheduled</span>',
        "free": '<span class="badge free">Free</span>',
    }
    return badges.get(status, "")


def _source_label(source: str) -> str:
    """Human-readable source name."""
    return {
        "melkweg": "Melkweg",
        "amsterdam_alt": "Amsterdam Alternative",
        "ra": "Resident Advisor",
        "paradiso": "Paradiso",
        "gallery_viewer": "Gallery Viewer",
        "muziekgebouw": "Muziekgebouw",
    }.get(source, source.title())


def build_html_digest(
    events: list[Event],
    output_path: Path | None = None,
    title: str | None = None,
) -> Path:
    """
    Generate a self-contained HTML digest file.
    Events are grouped by date, then sorted by time.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    discovery_engine = DiscoveryEngine()

    if not output_path:
        timestamp = datetime.now().strftime("%Y-%m-%d")
        output_path = OUTPUT_DIR / f"digest_{timestamp}.html"

    if not title:
        title = f"🎛️ {CITY} Events Digest"

    # Group events by date
    by_date: dict[str, list[Event]] = defaultdict(list)
    for ev in events:
        date_key = ev.date.strftime("%Y-%m-%d")
        by_date[date_key].append(ev)

    # Sort dates and events within each date
    sorted_dates = sorted(by_date.keys())
    for date_key in sorted_dates:
        by_date[date_key].sort(key=lambda e: (e.date, e.title))

    # Count stats
    total = len(events)
    sources = set(ev.source for ev in events)
    venues = set(ev.venue for ev in events if ev.venue)

    generated_at = datetime.now().strftime("%B %d, %Y at %H:%M")

    # Build filter UI
    filter_html = '    <div class="day-filters">\n        <button class="filter-btn active" data-date="all">All Days</button>\n'
    for date_key in sorted_dates:
        date_obj = datetime.strptime(date_key, "%Y-%m-%d")
        short_display = date_obj.strftime("%a, %b %d")
        filter_html += f'        <button class="filter-btn" data-date="{date_key}">{short_display}</button>\n'
    filter_html += '    </div>\n'

    # Build HTML
    event_cards_html = ""
    for date_key in sorted_dates:
        date_obj = datetime.strptime(date_key, "%Y-%m-%d")
        day_name = date_obj.strftime("%A")
        day_display = date_obj.strftime("%B %d, %Y")

        event_cards_html += f"""
        <div class="date-group" data-date="{date_key}">
            <div class="date-header">
                <span class="day-name">{day_name}</span>
                <span class="day-date">{day_display}</span>
                <span class="event-count">{len(by_date[date_key])} event{"s" if len(by_date[date_key]) != 1 else ""}</span>
            </div>
            <div class="events-grid">
        """

        for ev in by_date[date_key]:
            emoji = _event_type_emoji(ev.event_type)
            badge = _status_badge(ev.tickets_status)
            genres_html = ""
            if ev.genres:
                genres_html = "".join(
                    f'<span class="genre-tag">{g}</span>' for g in ev.genres[:4]
                )
            artists_html = ""
            if ev.artists:
                artists_html = f'<div class="artists">{" · ".join(ev.artists[:6])}</div>'

            source_lbl = _source_label(ev.source)
            venue_html = f'<span class="venue">{ev.venue}</span>' if ev.venue else ""

            ics_rel_path = _write_ics_file(ev, OUTPUT_DIR / "ics")

            match_html = ""
            artists_to_check = ev.artists if ev.artists else [ev.title]
            match_res = discovery_engine.evaluate_event(artists_to_check)
            if match_res["match"]:
                if match_res["type"] == "Library Match":
                    match_html = f'<div class="match-badge library-match">🔥 {match_res["reason"]}</div>'
                else:
                    match_html = f'<div class="match-badge discovery-match">✨ {match_res["reason"]}</div>'

            event_cards_html += f"""
                <div class="event-card">
                    <a href="{ev.source_url}" target="_blank" rel="noopener" class="card-link">
                        <div class="card-top">
                            <span class="event-type">{emoji} {ev.event_type}</span>
                            {badge}
                        </div>
                        {match_html}
                        <h3 class="event-title">{ev.title}</h3>
                        {artists_html}
                        <div class="genres">{genres_html}</div>
                        <div class="card-footer">
                            {venue_html}
                            <span class="source">{source_lbl}</span>
                        </div>
                    </a>
                    <div class="card-actions">
                        <a href="{ics_rel_path}" class="cal-btn" title="Add to Apple Calendar">📅 Add to Calendar</a>
                    </div>
                </div>
            """

        event_cards_html += """
            </div>
        </div>
        """

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-primary: #0a0a0f;
            --bg-secondary: #12121a;
            --bg-card: #1a1a2e;
            --bg-card-hover: #222240;
            --text-primary: #e8e8f0;
            --text-secondary: #8888a8;
            --text-muted: #555570;
            --accent-purple: #7c3aed;
            --accent-pink: #ec4899;
            --accent-cyan: #06b6d4;
            --accent-green: #10b981;
            --accent-orange: #f97316;
            --accent-red: #ef4444;
            --gradient-hero: linear-gradient(135deg, #7c3aed 0%, #ec4899 50%, #06b6d4 100%);
            --border-subtle: rgba(255, 255, 255, 0.06);
            --radius: 12px;
            --radius-sm: 8px;
        }}

        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.6;
            min-height: 100vh;
        }}

        .hero {{
            background: var(--gradient-hero);
            padding: 60px 24px 48px;
            text-align: center;
            position: relative;
            overflow: hidden;
        }}

        .hero::before {{
            content: '';
            position: absolute;
            inset: 0;
            background: radial-gradient(ellipse at 30% 50%, rgba(124, 58, 237, 0.3) 0%, transparent 70%),
                        radial-gradient(ellipse at 70% 50%, rgba(236, 72, 153, 0.2) 0%, transparent 70%);
        }}

        .hero-content {{
            position: relative;
            z-index: 1;
            max-width: 800px;
            margin: 0 auto;
        }}

        .hero h1 {{
            font-size: clamp(2rem, 5vw, 3.5rem);
            font-weight: 800;
            letter-spacing: -0.03em;
            margin-bottom: 8px;
            text-shadow: 0 2px 20px rgba(0, 0, 0, 0.3);
        }}

        .hero .subtitle {{
            font-size: 1.1rem;
            opacity: 0.85;
            font-weight: 400;
        }}

        .stats-bar {{
            display: flex;
            justify-content: center;
            gap: 32px;
            margin-top: 24px;
            flex-wrap: wrap;
        }}

        .stat {{
            text-align: center;
        }}

        .stat-value {{
            font-size: 1.8rem;
            font-weight: 700;
            display: block;
        }}

        .stat-label {{
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            opacity: 0.7;
        }}

        .container {{
            max-width: 900px;
            margin: 0 auto;
            padding: 32px 16px 80px;
        }}

        .date-group {{
            margin-bottom: 40px;
        }}

        .date-header {{
            display: flex;
            align-items: baseline;
            gap: 12px;
            margin-bottom: 16px;
            padding-bottom: 8px;
            border-bottom: 1px solid var(--border-subtle);
            flex-wrap: wrap;
        }}

        .day-name {{
            font-size: 1.3rem;
            font-weight: 700;
            background: var(--gradient-hero);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }}

        .day-date {{
            font-size: 0.9rem;
            color: var(--text-secondary);
        }}

        .event-count {{
            font-size: 0.75rem;
            color: var(--text-muted);
            background: var(--bg-card);
            padding: 2px 10px;
            border-radius: 20px;
            margin-left: auto;
        }}

        .events-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
            gap: 12px;
        }}

        .event-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius);
            padding: 16px;
            color: inherit;
            transition: all 0.2s ease;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }}

        .event-card:hover {{
            background: var(--bg-card-hover);
            border-color: var(--accent-purple);
            transform: translateY(-2px);
            box-shadow: 0 8px 24px rgba(124, 58, 237, 0.15);
        }}

        .card-link {{
            text-decoration: none;
            color: inherit;
            display: flex;
            flex-direction: column;
            gap: 8px;
            flex: 1;
        }}

        .card-top {{
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .event-type {{
            font-size: 0.75rem;
            font-weight: 600;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}

        .event-title {{
            font-size: 1rem;
            font-weight: 700;
            line-height: 1.3;
            letter-spacing: -0.01em;
        }}

        .artists {{
            font-size: 0.8rem;
            color: var(--accent-cyan);
            font-weight: 500;
        }}

        .genres {{
            display: flex;
            flex-wrap: wrap;
            gap: 4px;
        }}

        .genre-tag {{
            font-size: 0.65rem;
            padding: 2px 8px;
            background: rgba(124, 58, 237, 0.15);
            color: var(--accent-purple);
            border-radius: 20px;
            font-weight: 500;
        }}

        .card-footer {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-top: auto;
            padding-top: 8px;
            border-top: 1px solid var(--border-subtle);
        }}

        .venue {{
            font-size: 0.75rem;
            color: var(--text-secondary);
            font-weight: 500;
        }}

        .card-actions {{
            padding-top: 6px;
        }}

        .cal-btn {{
            display: inline-block;
            text-decoration: none;
            background: rgba(124, 58, 237, 0.12);
            border: 1px solid rgba(124, 58, 237, 0.25);
            border-radius: var(--radius-sm);
            padding: 5px 12px;
            font-size: 0.72rem;
            font-family: inherit;
            font-weight: 600;
            cursor: pointer;
            color: var(--accent-purple);
            transition: all 0.2s ease;
            letter-spacing: 0.02em;
        }}

        .cal-btn:hover {{
            background: rgba(124, 58, 237, 0.3);
            border-color: var(--accent-purple);
            transform: scale(1.04);
            box-shadow: 0 0 14px rgba(124, 58, 237, 0.25);
        }}

        .cal-btn:active {{
            transform: scale(0.97);
        }}

        .source {{
            font-size: 0.65rem;
            color: var(--text-muted);
            font-weight: 500;
        }}

        .badge {{
            font-size: 0.65rem;
            padding: 2px 8px;
            border-radius: 20px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}

        .badge.sold-out {{
            background: rgba(239, 68, 68, 0.15);
            color: var(--accent-red);
        }}

        .badge.cancelled {{
            background: rgba(239, 68, 68, 0.15);
            color: var(--accent-red);
        }}

        .badge.rescheduled {{
            background: rgba(249, 115, 22, 0.15);
            color: var(--accent-orange);
        }}

        .badge.free {{
            background: rgba(16, 185, 129, 0.15);
            color: var(--accent-green);
        }}

        .match-badge {{
            font-size: 0.7rem;
            padding: 4px 8px;
            border-radius: var(--radius-sm);
            margin-bottom: 8px;
            font-weight: 600;
            display: inline-block;
            align-self: flex-start;
        }}
        .library-match {{
            background: rgba(239, 68, 68, 0.15);
            color: var(--accent-red);
            border: 1px solid rgba(239, 68, 68, 0.3);
        }}
        .discovery-match {{
            background: rgba(16, 185, 129, 0.15);
            color: var(--accent-green);
            border: 1px solid rgba(16, 185, 129, 0.3);
        }}

        .footer {{
            text-align: center;
            padding: 40px 16px;
            color: var(--text-muted);
            font-size: 0.8rem;
            border-top: 1px solid var(--border-subtle);
        }}

        .footer a {{
            color: var(--accent-purple);
            text-decoration: none;
        }}

        .no-events {{
            text-align: center;
            padding: 80px 16px;
            color: var(--text-secondary);
        }}

        .no-events h2 {{
            font-size: 1.5rem;
            margin-bottom: 8px;
        }}

        .day-filters {{
            display: flex;
            gap: 8px;
            overflow-x: auto;
            padding-bottom: 16px;
            margin-bottom: 24px;
            scrollbar-width: none;
        }}
        .day-filters::-webkit-scrollbar {{
            display: none;
        }}
        .filter-btn {{
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            color: var(--text-secondary);
            padding: 8px 16px;
            border-radius: 20px;
            cursor: pointer;
            font-family: inherit;
            font-size: 0.85rem;
            font-weight: 600;
            white-space: nowrap;
            transition: all 0.2s ease;
        }}
        .filter-btn:hover {{
            border-color: var(--accent-purple);
            color: var(--text-primary);
        }}
        .filter-btn.active {{
            background: rgba(124, 58, 237, 0.2);
            border-color: var(--accent-purple);
            color: var(--accent-purple);
        }}
        .date-group.hidden {{
            display: none;
        }}

        @media (max-width: 600px) {{
            .hero {{
                padding: 40px 16px 32px;
            }}
            .events-grid {{
                grid-template-columns: 1fr;
            }}
            .stats-bar {{
                gap: 20px;
            }}
        }}
    </style>
</head>
<body>
    <header class="hero">
        <div class="hero-content">
            <h1>{title}</h1>
            <p class="subtitle">Generated {generated_at} · {total} events across {len(venues)} venues</p>
            <div class="stats-bar">
                <div class="stat">
                    <span class="stat-value">{total}</span>
                    <span class="stat-label">Events</span>
                </div>
                <div class="stat">
                    <span class="stat-value">{len(sorted_dates)}</span>
                    <span class="stat-label">Days</span>
                </div>
                <div class="stat">
                    <span class="stat-value">{len(venues)}</span>
                    <span class="stat-label">Venues</span>
                </div>
                <div class="stat">
                    <span class="stat-value">{len(sources)}</span>
                    <span class="stat-label">Sources</span>
                </div>
            </div>
        </div>
    </header>

    <main class="container">
        {filter_html if event_cards_html else ''}
        {event_cards_html if event_cards_html else '<div class="no-events"><h2>No upcoming events found</h2><p>Try running the collectors first.</p></div>'}
    </main>

    <footer class="footer">
        <p>Amsterdam Event Aggregator · Data from {", ".join(_source_label(s) for s in sorted(sources))}</p>
        <p>Generated automatically · Not affiliated with any venue or platform</p>
    </footer>

    <script>
        document.addEventListener('DOMContentLoaded', () => {{
            const filterBtns = document.querySelectorAll('.filter-btn');
            const dateGroups = document.querySelectorAll('.date-group');

            filterBtns.forEach(btn => {{
                btn.addEventListener('click', () => {{
                    // Remove active class from all
                    filterBtns.forEach(b => b.classList.remove('active'));
                    // Add active to clicked
                    btn.classList.add('active');

                    const selectedDate = btn.getAttribute('data-date');

                    dateGroups.forEach(group => {{
                        if (selectedDate === 'all' || group.getAttribute('data-date') === selectedDate) {{
                            group.classList.remove('hidden');
                        }} else {{
                            group.classList.add('hidden');
                        }}
                    }});
                }});
            }});
        }});
    </script>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")
    log.info("HTML digest written to %s (%d events)", output_path, total)
    return output_path
