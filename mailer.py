"""
Email delivery module for the Amsterdam Event Digest.

Sends the generated HTML digest as a styled email via SMTP.
Uses only stdlib (smtplib + email.mime) — zero extra dependencies.

Email clients (Gmail, Outlook, Apple Mail) do NOT support:
  - CSS custom properties (var())
  - CSS Grid / display: grid
  - background-clip: text (gradient text)
  - <style> blocks (Gmail strips them entirely)

So we post-process the browser HTML into email-safe HTML by:
  1. Inlining CSS variables
  2. Injecting inline styles on every element
  3. Removing unsupported features (calendar links, gradient text)
"""
from __future__ import annotations

import logging
import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from config import OUTPUT_DIR

log = logging.getLogger(__name__)


# ── Email-safe HTML builder ────────────────────────────────────────
# Instead of trying to patch the browser HTML, we rebuild an
# email-optimized version from the same digest file.

def _prepare_email_html(raw_html: str) -> str:
    """Transform browser-optimized HTML into email-client-safe HTML."""

    # Step 1: Extract data from the browser HTML
    title_match = re.search(r"<title>(.*?)</title>", raw_html)
    title = title_match.group(1) if title_match else "Amsterdam Events Digest"

    subtitle_match = re.search(r'<p class="subtitle">(.*?)</p>', raw_html)
    subtitle = subtitle_match.group(1) if subtitle_match else ""

    # Extract stats
    stat_values = re.findall(r'<span class="stat-value">(.*?)</span>', raw_html)
    stat_labels = re.findall(r'<span class="stat-label">(.*?)</span>', raw_html)
    stats = list(zip(stat_values, stat_labels))

    # Extract date groups
    date_groups = re.findall(
        r'<div class="date-group"[^>]*>(.*?)</div>\s*</div>\s*</div>',
        raw_html,
        re.DOTALL,
    )

    # Build email HTML with inline styles
    email_parts = []

    # ── Header ──
    email_parts.append(f"""
    <div style="background: linear-gradient(135deg, #7c3aed 0%, #ec4899 50%, #06b6d4 100%);
                padding: 48px 24px 40px; text-align: center;">
        <h1 style="font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 32px;
                   font-weight: 800; color: #ffffff; margin: 0 0 8px 0;
                   letter-spacing: -0.03em;">{title}</h1>
        <p style="font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 15px;
                  color: rgba(255,255,255,0.85); margin: 0 0 20px 0;">{subtitle}</p>
        <div style="text-align: center;">
    """)
    for val, lbl in stats:
        email_parts.append(f"""
            <span style="display: inline-block; text-align: center; margin: 0 16px;">
                <span style="font-family: 'Helvetica Neue', Arial, sans-serif;
                             font-size: 28px; font-weight: 700; color: #ffffff;
                             display: block;">{val}</span>
                <span style="font-family: 'Helvetica Neue', Arial, sans-serif;
                             font-size: 11px; text-transform: uppercase;
                             letter-spacing: 0.1em; color: rgba(255,255,255,0.7);">{lbl}</span>
            </span>
        """)
    email_parts.append("</div></div>")

    # ── Event cards ──
    email_parts.append("""
    <div style="max-width: 640px; margin: 0 auto; padding: 24px 16px 60px;
                background-color: #0a0a0f;">
    """)

    for group_html in date_groups:
        # Extract date header
        day_name_m = re.search(r'<span class="day-name">(.*?)</span>', group_html)
        day_date_m = re.search(r'<span class="day-date">(.*?)</span>', group_html)
        event_count_m = re.search(r'<span class="event-count">(.*?)</span>', group_html)
        day_name = day_name_m.group(1) if day_name_m else ""
        day_date = day_date_m.group(1) if day_date_m else ""
        event_count = event_count_m.group(1) if event_count_m else ""

        email_parts.append(f"""
        <div style="margin-bottom: 32px;">
            <div style="border-bottom: 1px solid rgba(255,255,255,0.06);
                        padding-bottom: 8px; margin-bottom: 16px;">
                <span style="font-family: 'Helvetica Neue', Arial, sans-serif;
                             font-size: 20px; font-weight: 700; color: #7c3aed;">{day_name}</span>
                <span style="font-family: 'Helvetica Neue', Arial, sans-serif;
                             font-size: 14px; color: #8888a8; margin-left: 10px;">{day_date}</span>
                <span style="font-family: 'Helvetica Neue', Arial, sans-serif;
                             font-size: 12px; color: #555570; background: #1a1a2e;
                             padding: 2px 10px; border-radius: 20px;
                             margin-left: 10px;">{event_count}</span>
            </div>
        """)

        # Extract individual event cards
        cards = re.findall(
            r'<div class="event-card">(.*?)</div>\s*</div>',
            group_html,
            re.DOTALL,
        )

        for card_html in cards:
            # Parse card fields
            url_m = re.search(r'href="(https?://[^"]+)"', card_html)
            url = url_m.group(1) if url_m else "#"

            type_m = re.search(r'<span class="event-type">(.*?)</span>', card_html)
            event_type = type_m.group(1).strip() if type_m else ""

            badge_m = re.search(r'<span class="badge[^"]*">(.*?)</span>', card_html)
            badge_text = badge_m.group(1) if badge_m else ""

            title_m = re.search(r'<h3 class="event-title">(.*?)</h3>', card_html)
            event_title = title_m.group(1) if title_m else ""

            artists_m = re.search(r'<div class="artists">(.*?)</div>', card_html)
            artists = artists_m.group(1) if artists_m else ""

            genres = re.findall(r'<span class="genre-tag">(.*?)</span>', card_html)

            venue_m = re.search(r'<span class="venue">(.*?)</span>', card_html)
            venue = venue_m.group(1) if venue_m else ""

            source_m = re.search(r'<span class="source">(.*?)</span>', card_html)
            source = source_m.group(1) if source_m else ""

            # Match badges (discovery/library)
            match_m = re.search(r'<div class="match-badge[^"]*">(.*?)</div>', card_html)
            match_badge = match_m.group(1) if match_m else ""

            # Build the badge color
            badge_html = ""
            if badge_text:
                badge_color = "#ef4444" if badge_text in ("Sold Out", "Cancelled") else "#10b981"
                badge_html = f"""
                    <span style="font-size: 11px; padding: 2px 8px; border-radius: 20px;
                                 font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em;
                                 background: rgba({_hex_to_rgb(badge_color)}, 0.15);
                                 color: {badge_color};">{badge_text}</span>
                """

            # Match badge HTML
            match_html = ""
            if match_badge:
                is_library = "🔥" in match_badge
                mc = "#ef4444" if is_library else "#10b981"
                match_html = f"""
                    <div style="font-size: 12px; padding: 4px 8px; border-radius: 8px;
                                margin-bottom: 6px; font-weight: 600; display: inline-block;
                                background: rgba({_hex_to_rgb(mc)}, 0.15);
                                color: {mc}; border: 1px solid rgba({_hex_to_rgb(mc)}, 0.3);">
                        {match_badge}
                    </div>
                """

            # Genre tags
            genre_html = ""
            if genres:
                genre_spans = "".join(
                    f'<span style="font-size: 11px; padding: 2px 8px; '
                    f'background: rgba(124, 58, 237, 0.15); color: #7c3aed; '
                    f'border-radius: 20px; font-weight: 500; display: inline-block; '
                    f'margin-right: 4px; margin-bottom: 4px;">{g}</span>'
                    for g in genres[:4]
                )
                genre_html = f'<div style="margin-top: 6px;">{genre_spans}</div>'

            # Artists line
            artists_html = ""
            if artists:
                artists_html = f"""
                    <div style="font-family: 'Helvetica Neue', Arial, sans-serif;
                                font-size: 13px; color: #06b6d4; font-weight: 500;
                                margin-top: 4px;">{artists}</div>
                """

            email_parts.append(f"""
            <a href="{url}" target="_blank" rel="noopener"
               style="display: block; text-decoration: none; color: inherit;
                      background: #1a1a2e; border: 1px solid rgba(255,255,255,0.06);
                      border-radius: 12px; padding: 16px; margin-bottom: 10px;">
                <div style="margin-bottom: 6px;">
                    <span style="font-family: 'Helvetica Neue', Arial, sans-serif;
                                 font-size: 11px; font-weight: 600; color: #8888a8;
                                 text-transform: uppercase; letter-spacing: 0.05em;">{event_type}</span>
                    {badge_html}
                </div>
                {match_html}
                <div style="font-family: 'Helvetica Neue', Arial, sans-serif;
                            font-size: 16px; font-weight: 700; color: #e8e8f0;
                            line-height: 1.3; letter-spacing: -0.01em;">{event_title}</div>
                {artists_html}
                {genre_html}
                <div style="margin-top: 10px; padding-top: 8px;
                            border-top: 1px solid rgba(255,255,255,0.06);">
                    <span style="font-family: 'Helvetica Neue', Arial, sans-serif;
                                 font-size: 12px; color: #8888a8; font-weight: 500;">{venue}</span>
                    <span style="font-family: 'Helvetica Neue', Arial, sans-serif;
                                 font-size: 11px; color: #555570; font-weight: 500;
                                 float: right;">{source}</span>
                </div>
            </a>
            """)

        email_parts.append("</div>")  # close date group

    # ── Footer ──
    email_parts.append("""
    <div style="text-align: center; padding: 32px 16px; color: #555570;
                font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 13px;
                border-top: 1px solid rgba(255,255,255,0.06);">
        <p>Amsterdam Event Aggregator · Generated automatically</p>
    </div>
    </div>
    """)

    # Wrap in full HTML document
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #0a0a0f; color: #e8e8f0;
             font-family: 'Helvetica Neue', Arial, sans-serif; line-height: 1.6;
             -webkit-text-size-adjust: 100%;">
    {"".join(email_parts)}
</body>
</html>"""


def _hex_to_rgb(hex_color: str) -> str:
    """Convert #RRGGBB to 'R, G, B' for use in rgba()."""
    h = hex_color.lstrip("#")
    return f"{int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)}"


def _find_latest_digest() -> Path | None:
    """Find the most recent digest HTML file in the output directory."""
    if not OUTPUT_DIR.exists():
        return None
    digests = sorted(OUTPUT_DIR.glob("digest_*.html"), reverse=True)
    return digests[0] if digests else None


def send_digest(
    digest_path: Path | None = None,
    smtp_user: str | None = None,
    smtp_pass: str | None = None,
    recipient: str | None = None,
) -> bool:
    """
    Send the HTML digest via email.

    Credentials are read from env vars if not provided:
      EMAIL_USER, EMAIL_PASS, EMAIL_TO
    """
    smtp_user = smtp_user or os.getenv("EMAIL_USER", "")
    smtp_pass = smtp_pass or os.getenv("EMAIL_PASS", "")
    recipient = recipient or os.getenv("EMAIL_TO", "")

    if not all([smtp_user, smtp_pass, recipient]):
        log.error("Missing email credentials. Set EMAIL_USER, EMAIL_PASS, EMAIL_TO.")
        return False

    path = digest_path or _find_latest_digest()
    if not path or not path.exists():
        log.error("No digest HTML found to send.")
        return False

    raw_html = path.read_text(encoding="utf-8")
    email_html = _prepare_email_html(raw_html)

    # Extract subject from <title> tag
    title_match = re.search(r"<title>(.*?)</title>", raw_html)
    subject = title_match.group(1) if title_match else "Amsterdam Events Digest"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = recipient

    # Plain-text fallback
    msg.attach(MIMEText(
        f"{subject}\n\nOpen the HTML version of this email for the full digest.",
        "plain",
    ))
    # HTML body
    msg.attach(MIMEText(email_html, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        log.info("✅ Digest emailed to %s", recipient)
        return True
    except Exception as e:
        log.error("❌ Failed to send email: %s", e)
        return False
