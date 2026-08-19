#!/usr/bin/env python3
"""
Genera le pagine statiche indicizzabili sotto site/events/.

PERCHE'
Il calendario in site/ e' una SPA senza routing: giorno, mese e filtri vivono
tutti su "/", e l'HTML che il server manda contiene 536 caratteri di testo e
zero eventi (i dati arrivano dopo, via fetch). Per Google esiste una sola
pagina, e quella pagina dice "0 Events". Qui costruiamo l'atterraggio che
mancava: una pagina vera per ogni finestra temporale che la gente cerca
davvero ("tonight", "this weekend", "23 October"), con gli eventi gia' nel
sorgente e i dati strutturati Event accanto.

COSA GENERA
    site/events/index.html          elenco di tutte le pagine
    site/events/<finestra>/         today, tonight, tomorrow, this-weekend,
                                    next-weekend, this-week, next-week
    site/events/YYYY-MM-DD/         una per ogni giorno con eventi
    site/events/YYYY-MM/            una per mese, se il mese supera la soglia
    site/events/ics/*.ics           un file per evento (i bottoni Calendar
                                    delle pagine statiche non hanno JS)
    site/sitemap.xml

Legge il DB direttamente, non site/data/events.json: le pagine-mese devono
coprire l'orizzonte pieno anche quando la finestra del calendario e' piu'
corta.

SICUREZZA
Titoli, locali e artisti arrivano da 11 scraper, cioe' da terzi, e finiscono
in HTML pubblico: ogni campo passa da esc(). Non e' teorico — il digest in
html_builder.py li interpola grezzi (importa `html` a riga 7, ma una variabile
locale omonima lo copre e html.escape() non viene mai chiamato).

    ./.venv/bin/python build_pages.py
"""
from __future__ import annotations

import calendar
import html
import json
import logging
import re
import shutil
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from artists import artists_for
from db import init_db, get_events_between
from dedup import collapse_duplicates
from discovery import DiscoveryEngine
from html_builder import _status_badge, _write_ics_file
from labels import SOURCE_LABELS, TYPE_EMOJI, source_label, type_emoji
from models import Event

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
SITE_DIR = BASE_DIR / "site"
PAGES_DIR = SITE_DIR / "events"
ICS_DIR = PAGES_DIR / "ics"
APP_JS = SITE_DIR / "app.js"

BASE_URL = "https://events.zenobj.net"
# Il marchio sta in og:site_name, nell'header e nel footer — non nei <title>.
# Google il nome del sito lo prende da og:site_name e lo mostra su una riga
# sua accanto al risultato: ripeterlo in ogni title costerebbe una decina di
# caratteri alla frase che la gente digita davvero ("amsterdam events on...").
SITE_NAME = "FOMOdam"
TAGLINE = "fear of missing amsterdam"
TZ = ZoneInfo("Europe/Amsterdam")

# Quanto leggiamo dal DB in una volta sola. L'orizzonte vero delle pagine lo
# decidono i mesi che superano MONTH_PAGE_MIN_EVENTS (vedi _page_horizon).
HORIZON_DAYS = 220
# Sotto questa soglia un mese non merita una pagina propria: sarebbe una
# pagina quasi vuota che diluisce il resto.
MONTH_PAGE_MIN_EVENTS = 60
# Se nessun mese supera la soglia, le pagine-giorno si fermano qui.
FALLBACK_DAY_HORIZON = 45
# Per quanti giorni una pagina-data sopravvive alla data dell'evento.
#
# Senza questo, la pagina di ieri spariva stanotte: Google indicizzava
# /events/2026-08-18/, il giorno dopo rispondeva 404, e chi ci arrivava da un
# risultato di ricerca vecchio trovava la schermata di errore nuda di
# http.server (che non e' personalizzabile, e Custom Error Pages di Cloudflare
# non c'e' sul piano free). Due settimane bastano perche' l'indice si
# aggiorni da solo.
#
# Le pagine passate NON entrano nella sitemap e portano <meta robots=noindex>:
# esistono solo come atterraggio per i link stantii, non per essere trovate.
PAST_DAY_GRACE_DAYS = 14
# "Tonight" = da questa ora in poi.
TONIGHT_FROM_HOUR = 18
# Il venerdi' sera fa parte del weekend: su questo calendario vale quanto il
# sabato (21/08/2026: 40 eventi il venerdi', 51 il sabato, 16 la domenica).
# Metti False per tornare al weekend sabato+domenica.
WEEKEND_STARTS_FRIDAY = True
# Quanti titoli di richiamo per giorno nella griglia delle pagine-mese.
MONTH_HIGHLIGHTS = 3

WEEKDAYS_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday"]
MONTHS_FULL = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]


# ── Escaping ───────────────────────────────────────────────────────
def esc(value) -> str:
    """Ogni campo che viene dagli scraper passa da qui. Nessuna eccezione."""
    return html.escape(str(value if value is not None else ""), quote=True)


# ── Date, in inglese e senza dipendere dal locale di sistema ───────
def fmt_day_long(d: date) -> str:
    """'Saturday 22 August 2026'."""
    return f"{WEEKDAYS_FULL[d.weekday()]} {d.day} {MONTHS_FULL[d.month - 1]} {d.year}"


def fmt_day_short(d: date) -> str:
    """'Sat 22 Aug'."""
    return f"{WEEKDAYS_FULL[d.weekday()][:3]} {d.day} {MONTHS_FULL[d.month - 1][:3]}"


def fmt_month_long(year: int, month: int) -> str:
    return f"{MONTHS_FULL[month - 1]} {year}"


def plural(n: int, word: str = "event") -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# ── Coerenza con la copia JS delle etichette ───────────────────────
def _check_js_labels_in_sync() -> None:
    """
    Avvisa se site/app.js e labels.py non hanno le stesse chiavi.

    Le due mappe non possono importarsi a vicenda (una e' Python, l'altra gira
    nel browser): questo controllo e' l'unica cosa che tiene onesta la copia.
    Se la lettura o il parsing falliscono non si lamenta: e' una guardia, non
    un test, e non deve mai far fallire una generazione.
    """
    try:
        js = APP_JS.read_text(encoding="utf-8")
    except OSError:
        return

    for name, py_map in (("SOURCE_LABELS", SOURCE_LABELS), ("TYPE_EMOJI", TYPE_EMOJI)):
        block = re.search(rf"const\s+{name}\s*=\s*\{{(.*?)\}}\s*;", js, re.S)
        if not block:
            continue
        js_keys = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", block.group(1)))
        if not js_keys:
            continue
        only_js = js_keys - set(py_map)
        only_py = set(py_map) - js_keys
        if only_js or only_py:
            log.warning(
                "%s diverge fra app.js e labels.py — solo in JS: %s | solo in Python: %s",
                name, sorted(only_js) or "—", sorted(only_py) or "—",
            )


# ── Finestre relative ──────────────────────────────────────────────
def _weekend_days(friday: date) -> list[date]:
    start = friday if WEEKEND_STARTS_FRIDAY else friday + timedelta(days=1)
    return [start + timedelta(days=i) for i in range((friday + timedelta(days=2) - start).days + 1)]


def _window_specs(today: date) -> list[tuple[str, list[date], str, str]]:
    """
    (slug, giorni, etichetta breve, aggettivo per il testo) per ogni finestra
    relativa. I giorni gia' passati non ci entrano mai.
    """
    monday = today - timedelta(days=today.weekday())
    this_friday = monday + timedelta(days=4)
    next_monday = monday + timedelta(days=7)

    def future(days: list[date]) -> list[date]:
        return [d for d in days if d >= today]

    return [
        ("today", [today], "Today", "today"),
        ("tonight", [today], "Tonight", "tonight"),
        ("tomorrow", [today + timedelta(days=1)], "Tomorrow", "tomorrow"),
        ("this-weekend", future(_weekend_days(this_friday)), "This weekend", "this weekend"),
        ("next-weekend", future(_weekend_days(this_friday + timedelta(days=7))), "Next weekend", "next weekend"),
        ("this-week", future([monday + timedelta(days=i) for i in range(7)]), "This week", "this week"),
        ("next-week", [next_monday + timedelta(days=i) for i in range(7)], "Next week", "next week"),
    ]


# ── Carta evento (specchio di cardHTML in site/app.js:528) ─────────
def _event_card(ev: Event, match: dict | None, ics_rel: str) -> str:
    emoji = type_emoji(ev.event_type)
    src_lbl = source_label(ev.source)

    match_html = ""
    if match:
        cls = "library" if match.get("type") == "Library Match" else "discovery"
        icon = "🔥" if cls == "library" else "✨"
        match_html = f'<div class="match-badge {cls}">{icon} {esc(match.get("reason"))}</div>'

    artists_html = (
        f'<div class="artists">{esc(" · ".join(ev.artists[:6]))}</div>'
        if ev.artists else ""
    )
    genres_html = (
        '<div class="genres">'
        + "".join(f'<span class="genre-tag">{esc(g)}</span>' for g in ev.genres[:4])
        + "</div>"
    ) if ev.genres else ""

    venue_html = f'<span class="venue">{esc(ev.venue)}</span>' if ev.venue else ""
    # Non ripetere "Melkweg · Melkweg": stessa regola di app.js.
    redundant = (ev.venue or "").strip().lower() == src_lbl.strip().lower()
    src_html = "" if redundant else f'<span class="source">{esc(src_lbl)}</span>'

    # 00:00 non si mostra. Non e' un orario: e' il segnaposto che quasi tutte
    # le fonti usano quando l'ora non la sanno — mostre, mercatini, cose "tutto
    # il giorno". Scritto a video diventa "mezzanotte", che e' falso 16 volte
    # su 20 (misurato). Il prezzo e' che i 4 eventi che iniziano davvero a
    # mezzanotte perdono l'orario: meglio un dato in meno che uno sbagliato.
    time_txt = ev.date.strftime("%H:%M") if ev.date else ""
    type_txt = f"{emoji} {esc(ev.event_type)}"
    if time_txt and time_txt != "00:00":
        type_txt += f" · {time_txt}"

    # Sulle date passate non scriviamo .ics, quindi niente bottone — e nemmeno
    # il contenitore, che vuoto lascerebbe 8px di padding sotto ogni scheda.
    cal_html = (
        '<div class="card-actions">'
        f'<a class="cal-btn" href="{esc(ics_rel)}" download>📅 Calendar</a>'
        "</div>"
    ) if ics_rel else ""

    # data-start: l'orario d'inizio in ISO col fuso, cosi' lo snippet di
    # /today/ e /tonight/ puo' capire cosa e' gia' cominciato. Sta qui e non
    # solo li' perche' e' un dato dell'evento, non della pagina.
    start_attr = (
        f' data-start="{esc(ev.date.replace(tzinfo=TZ).isoformat())}"' if ev.date else ""
    )

    return f"""<article class="event-card"{start_attr}>
        <a href="{esc(ev.source_url)}" target="_blank" rel="noopener" class="card-link">
          <div class="card-top">
            <span class="event-type">{type_txt}</span>
            {_status_badge(ev.tickets_status)}
          </div>
          {match_html}
          <h3 class="event-title">{esc(ev.title)}</h3>
          {artists_html}
          {genres_html}
          <div class="card-footer">{venue_html}{src_html}</div>
        </a>
        {cal_html}
      </article>"""


# ── Dati strutturati ───────────────────────────────────────────────
_STATUS_SCHEMA = {
    "cancelled": "https://schema.org/EventCancelled",
    "rescheduled": "https://schema.org/EventRescheduled",
}


def _event_jsonld(ev: Event) -> dict:
    """Un oggetto schema.org/Event. Le date portano il fuso di Amsterdam."""
    # Stessa logica del display: se l'ora e' 00:00 non la conosciamo, e
    # scriverla in startDate significherebbe giurare a Google che l'evento
    # inizia a mezzanotte. schema.org accetta la sola data.
    start = ev.date.replace(tzinfo=TZ) if ev.date else None
    solo_giorno = bool(ev.date and ev.date.hour == 0 and ev.date.minute == 0)
    obj: dict = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": ev.title,
        "startDate": (start.date().isoformat() if solo_giorno
                      else start.isoformat()) if start else None,
        "eventStatus": _STATUS_SCHEMA.get(ev.tickets_status, "https://schema.org/EventScheduled"),
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "location": {
            "@type": "Place",
            "name": ev.venue or "Amsterdam",
            "address": {
                "@type": "PostalAddress",
                "streetAddress": ev.venue_address or None,
                "addressLocality": "Amsterdam",
                "addressCountry": "NL",
            },
        },
        "url": ev.source_url or None,
    }
    if ev.date_end:
        obj["endDate"] = ev.date_end.replace(tzinfo=TZ).isoformat()
    if ev.artists:
        obj["performer"] = [
            {"@type": "PerformingGroup", "name": a} for a in ev.artists[:6] if a
        ]
    if ev.price_min is not None:
        obj["offers"] = {
            "@type": "Offer",
            "price": ev.price_min,
            "priceCurrency": "EUR",
            "url": ev.tickets_url or ev.source_url or None,
            "availability": (
                "https://schema.org/SoldOut" if ev.tickets_status == "sold_out"
                else "https://schema.org/InStock"
            ),
        }
    return _drop_empty(obj)


def _drop_empty(obj):
    """Via i None e i dizionari rimasti vuoti: JSON-LD con campi nulli e' rumore."""
    if isinstance(obj, dict):
        cleaned = {k: _drop_empty(v) for k, v in obj.items()}
        return {k: v for k, v in cleaned.items() if v not in (None, "", {}, [])}
    if isinstance(obj, list):
        return [_drop_empty(v) for v in obj if v is not None]
    return obj


def _jsonld_script(events: list[Event]) -> str:
    """
    Solo per gli eventi davvero renderizzati nella pagina: i dati strutturati
    devono descrivere quello che si vede, non il database intero.
    """
    if not events:
        return ""
    payload = json.dumps(
        [_event_jsonld(ev) for ev in events],
        ensure_ascii=False, separators=(",", ":"),
    )
    # Impedisce che un "</script>" dentro un titolo chiuda il blocco.
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f'<script type="application/ld+json">{payload}</script>'


# ── Chrome della pagina ────────────────────────────────────────────
PAGE_CSS = """
a.logo { text-decoration: none; color: #fff; text-transform: none; display: inline-flex; align-items: baseline; }
.logo-dam { opacity: 0.55; }
.logo-sep { align-self: center; flex: none; width: 3px; height: 3px; border-radius: 50%; background: rgba(255,255,255,0.5); margin: 0 11px; position: relative; top: 2px; }
.logo-tag { font-size: 0.7rem; font-weight: 700; letter-spacing: 0.06em; color: rgba(255,255,255,0.78); }
@media (max-width: 900px) { .logo-sep, .logo-tag { display: none; } }
.crumbs, .page-links { display: flex; flex-wrap: wrap; gap: 8px; }
.crumbs { margin-bottom: 24px; }
.page-links { margin-top: 48px; padding-top: 24px; border-top: 1px solid var(--border-subtle); }
.crumbs a, .page-links a {
  text-decoration: none; font-size: 0.7rem; font-weight: 800;
  text-transform: uppercase; letter-spacing: 0.05em; padding: 6px 10px;
  border: 1px solid var(--border-subtle); border-radius: var(--radius);
  color: var(--text-secondary); transition: var(--transition);
}
.crumbs a:hover, .page-links a:hover { color: #fff; border-color: #fff; }
.crumbs a.current, .page-links a.current { color: #000; background: var(--text-primary); border-color: var(--text-primary); }
h1.page-h1 {
  font-size: 1.9rem; font-weight: 900; text-transform: uppercase;
  letter-spacing: 0.03em; line-height: 1.15; margin-bottom: 10px;
  background: var(--gradient-hero); -webkit-background-clip: text;
  -webkit-text-fill-color: transparent; background-clip: text;
}
.page-lede { color: var(--text-secondary); font-size: 0.9rem; max-width: 62ch; margin-bottom: 8px; }
.started-earlier { margin-top: 40px; }
.started-earlier .event-card { opacity: 0.5; }
.started-earlier .event-card:hover { opacity: 1; }
.past-notice {
  display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
  background: var(--bg-card); border-left: 3px solid var(--accent-orange);
  border-radius: 0; padding: 14px 18px; margin-bottom: 28px;
  font-size: 0.85rem; color: var(--text-secondary);
}
.past-notice strong { color: #fff; font-weight: 800; }
.past-notice a { color: var(--accent-cyan); text-decoration: none; font-weight: 700; }
.past-notice a:hover { text-decoration: underline; }
.day-heading {
  display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap;
  font-size: 1.05rem; font-weight: 900; text-transform: uppercase;
  letter-spacing: 0.05em; margin: 36px 0 16px; padding-bottom: 10px;
  border-bottom: 2px solid var(--border-subtle);
}
.day-heading a { color: inherit; text-decoration: none; }
.day-heading a:hover { color: var(--accent-cyan); }
.day-heading .count {
  font-size: 0.7rem; font-weight: 800; color: var(--bg-primary);
  background: var(--text-primary); padding: 2px 8px; border-radius: var(--radius-sm);
}
a.cal-btn { text-decoration: none; }
.month-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 12px; }
.month-day {
  display: flex; flex-direction: column; gap: 6px; padding: 12px 14px;
  background: var(--bg-card); border: 1px solid var(--border-subtle);
  border-radius: var(--radius); text-decoration: none; color: inherit;
  transition: var(--transition);
}
a.month-day:hover { background: var(--bg-card-hover); border-color: #fff; }
.md-date { font-size: 0.75rem; font-weight: 900; text-transform: uppercase; letter-spacing: 0.08em; color: #fff; }
.md-count { font-size: 0.65rem; font-weight: 800; text-transform: uppercase; letter-spacing: 0.05em; color: var(--accent-cyan); }
.md-titles { font-size: 0.75rem; color: var(--text-secondary); line-height: 1.45; }
.index-group { margin-bottom: 40px; }
.index-group h2 {
  font-size: 1.05rem; font-weight: 900; text-transform: uppercase;
  letter-spacing: 0.05em; margin-bottom: 14px; padding-bottom: 10px;
  border-bottom: 2px solid var(--border-subtle);
}
.index-list { display: flex; flex-wrap: wrap; gap: 8px; }
.index-list a {
  text-decoration: none; font-size: 0.75rem; font-weight: 800;
  padding: 8px 12px; border: 1px solid var(--border-subtle);
  border-radius: var(--radius); color: var(--text-primary); transition: var(--transition);
}
.index-list a:hover { border-color: #fff; background: var(--bg-card-hover); }
.index-list .n { color: var(--text-muted); font-weight: 700; }
@media (max-width: 768px) {
  h1.page-h1 { font-size: 1.4rem; }
  .month-grid { grid-template-columns: 1fr; }
}
"""

# Stesso beacon di site/index.html: senza cookie, e misura solo events.zenobj.net.
ANALYTICS = (
    "<script type='module' src='https://static.cloudflareinsights.com/beacon.min.js' "
    "data-cf-beacon='{\"token\": \"7d1d5dc28c4046539e1037ca65949b0a\"}'></script>"
)


def _shell(*, title: str, description: str, canonical: str, root: str,
           h1: str, lede: str, body: str, jsonld: str = "",
           crumbs: str = "", links: str = "", extra_scripts: str = "",
           noindex: bool = False, generated: datetime) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
{'<meta name="robots" content="noindex, follow">' if noindex else ''}
<link rel="canonical" href="{esc(canonical)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{esc(SITE_NAME)}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{esc(canonical)}">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="{esc(title)}">
<meta name="twitter:description" content="{esc(description)}">
<link rel="stylesheet" href="{root}style.css?v=4">
<link rel="icon" type="image/png" href="{root}favicon.png?v=2">
<style>{PAGE_CSS}</style>
{jsonld}
</head>
<body>
<header id="site-header">
  <div class="header-inner">
    <a class="logo" href="{root}" aria-label="{esc(SITE_NAME)} — {esc(TAGLINE)}">FOMO<span class="logo-dam">dam</span><span class="logo-sep" aria-hidden="true"></span><span class="logo-tag">{esc(TAGLINE)}</span></a>
  </div>
</header>
<main class="container">
  <nav class="crumbs">{crumbs}</nav>
  <h1 class="page-h1">{esc(h1)}</h1>
  <p class="page-lede">{esc(lede)}</p>
  {body}
  <nav class="page-links">{links}</nav>
</main>
<footer id="site-footer">
  <p>{esc(SITE_NAME)} · {esc(TAGLINE)} · Updated daily at 06:00 · Data from the venues and platforms we track</p>
  <p>Independent project · Not affiliated with any venue or platform</p>
  <p class="gen-time">Generated {generated.strftime('%d %B %Y, %H:%M')}</p>
</footer>
{extra_scripts}
{ANALYTICS}
</body>
</html>
"""


def _started_earlier_script() -> str:
    """
    Su /today/ e /tonight/ sposta in fondo gli eventi gia' cominciati.

    Perche' serve JS: la pagina e' statica e viene rigenerata alle 06:00, quindi
    al momento della scrittura "cosa deve ancora iniziare" e' tutto. Solo il
    browser sa che ore sono davvero. L'HTML resta completo — nessun evento
    viene nascosto ai crawler, vengono solo riordinati a video.

    Se il JS non gira, la pagina resta la lista cronologica di prima: peggiore,
    non rotta.

    I 30 minuti di tolleranza non sono arbitrari: a un concerto iniziato da
    venti minuti si arriva ancora.
    """
    return """<script>
(function () {
  var GRACE = 30 * 60 * 1000, now = Date.now();
  var grid = document.querySelector('.events-grid');
  if (!grid) return;
  var cards = [].slice.call(grid.querySelectorAll('.event-card[data-start]'));
  var total = cards.length;
  var past = cards.filter(function (c) {
    var t = Date.parse(c.getAttribute('data-start'));
    return t && (t + GRACE) < now;
  });
  if (!past.length) return;

  if (past.length === total) {
    var note = document.createElement('p');
    note.className = 'past-notice';
    note.innerHTML = '<strong>Everything listed today has already started.</strong>' +
      '<span>Have a look at <a href="../tomorrow/">tomorrow</a> or ' +
      '<a href="../this-weekend/">this weekend</a>.</span>';
    grid.parentNode.insertBefore(note, grid);
    return;
  }

  var sec = document.createElement('section');
  sec.className = 'started-earlier';
  sec.innerHTML = '<h2 class="day-heading">Started earlier' +
    '<span class="count">' + past.length + '</span></h2>';
  var moved = document.createElement('div');
  moved.className = 'events-grid';
  past.forEach(function (c) { moved.appendChild(c); });
  sec.appendChild(moved);
  grid.parentNode.insertBefore(sec, grid.nextSibling);
})();
</script>"""


# ── Snippet di freschezza ──────────────────────────────────────────
def _freshness_script(generated_day: date, offset: int, available: list[str]) -> str:
    """
    today/tonight/tomorrow vengono rigenerate alle 06:00: fra mezzanotte e le
    06:00 mostrano il giorno prima. L'<h1> stampa sempre la data esplicita,
    quindi la pagina non mente mai; questo snippet fa il resto, mandando chi
    arriva "domani" sulla pagina-data giusta — ma solo se quella pagina esiste
    davvero, altrimenti resta dov'e'.
    """
    days_json = json.dumps(available, separators=(",", ":"))
    return f"""<script>
(function () {{
  var GEN = "{generated_day.isoformat()}", OFFSET = {offset};
  var DAYS = {days_json};
  var pad = function (n) {{ return (n < 10 ? "0" : "") + n; }};
  var iso = function (d) {{ return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }};
  var now = new Date();
  if (iso(now) === GEN) return;
  var target = iso(new Date(now.getFullYear(), now.getMonth(), now.getDate() + OFFSET));
  if (DAYS.indexOf(target) !== -1) location.replace("../" + target + "/");
}})();
</script>"""


def fmt_range(days: list[date]) -> str:
    """'Saturday 22 August 2026' oppure 'Fri 21 – Sun 23 August 2026'."""
    if not days:
        return ""
    if len(days) == 1:
        return fmt_day_long(days[0])
    a, b = days[0], days[-1]
    if (a.year, a.month) == (b.year, b.month):
        return (f"{WEEKDAYS_FULL[a.weekday()][:3]} {a.day} – "
                f"{WEEKDAYS_FULL[b.weekday()][:3]} {b.day} {MONTHS_FULL[a.month - 1]} {a.year}")
    return f"{fmt_day_short(a)} – {fmt_day_short(b)} {b.year}"


# ── Orizzonte ──────────────────────────────────────────────────────
def _page_horizon(events: list[Event], today: date) -> tuple[list[str], date]:
    """
    Quali mesi meritano una pagina, e fino a che giorno arrivano le
    pagine-giorno.

    Le due cose sono legate: una pagina-mese e' fatta di link alle sue
    pagine-giorno, quindi le pagine-giorno devono coprire almeno tutti i mesi
    che hanno una pagina. Altrimenti la pagina di ottobre — il mese piu' ricco
    dell'anno, 706 eventi, e l'unico che contiene l'ADE — sarebbe un muro di
    numeri senza un solo link.
    """
    counts = Counter(ev.date.strftime("%Y-%m") for ev in events if ev.date)
    months = sorted(m for m, n in counts.items() if n >= MONTH_PAGE_MIN_EVENTS)
    if not months:
        return [], today + timedelta(days=FALLBACK_DAY_HORIZON)
    y, m = int(months[-1][:4]), int(months[-1][5:7])
    return months, date(y, m, calendar.monthrange(y, m)[1])


# ── Blocchi di eventi ──────────────────────────────────────────────
def _events_section(days: list[date], by_day: dict[date, list[Event]],
                    matches: dict[str, dict], ics_rel: dict[str, str],
                    *, headings: bool) -> str:
    out = []
    for d in days:
        evs = by_day.get(d, [])
        if not evs:
            continue
        if headings:
            out.append(
                f'<h2 class="day-heading">'
                f'<a href="../{d.isoformat()}/">{esc(fmt_day_long(d))}</a>'
                f'<span class="count">{plural(len(evs))}</span></h2>'
            )
        cards = "\n".join(
            _event_card(ev, matches.get(ev.dedup_key), ics_rel.get(ev.dedup_key, ""))
            for ev in evs
        )
        out.append(f'<div class="events-grid">{cards}</div>')
    if not out:
        return ('<div class="empty-state"><div class="emoji">🌙</div>'
                '<h4>Nothing listed yet</h4>'
                '<p>No events in our sources for these dates — try the calendar.</p></div>')
    return "\n".join(out)


def _month_body(year: int, month: int, by_day: dict[date, list[Event]],
                matches: dict[str, dict], day_slugs: set[str]) -> str:
    """
    Riassunto giorno per giorno, non 706 schede: una pagina con tutte le
    schede di ottobre pesa mezzo megabyte ed e' peggiore da leggere. Ogni
    cella porta alla pagina-giorno, che le schede ce le ha.
    """
    cells = []
    for day_num in range(1, calendar.monthrange(year, month)[1] + 1):
        d = date(year, month, day_num)
        evs = by_day.get(d, [])
        if not evs:
            continue
        # I "picks" per primi: sono l'unico segnale editoriale che abbiamo.
        picked = sorted(evs, key=lambda e: 0 if matches.get(e.dedup_key) else 1)
        titles = " · ".join(e.title for e in picked[:MONTH_HIGHLIGHTS] if e.title)
        inner = (
            f'<span class="md-date">{esc(fmt_day_short(d))}</span>'
            f'<span class="md-count">{plural(len(evs))}</span>'
            f'<span class="md-titles">{esc(titles)}</span>'
        )
        if d.isoformat() in day_slugs:
            cells.append(f'<a class="month-day" href="../{d.isoformat()}/">{inner}</a>')
        else:
            cells.append(f'<div class="month-day">{inner}</div>')
    if not cells:
        return '<div class="empty-state"><h4>Nothing listed for this month</h4></div>'
    return f'<div class="month-grid">{"".join(cells)}</div>'


# ── Link fra pagine ────────────────────────────────────────────────
def _link(href: str, label: str, current: bool = False) -> str:
    cls = ' class="current"' if current else ""
    return f'<a href="{esc(href)}"{cls}>{esc(label)}</a>'


def _window_strip(windows: list[tuple[str, str, int]], current_slug: str) -> str:
    """La riga di link alle finestre relative, con i conteggi."""
    return "".join(
        _link(f"../{slug}/", f"{label} ({n})", slug == current_slug)
        for slug, label, n in windows if n
    )


def _top_venues(evs: list[Event], n: int = 3) -> str:
    """I locali piu' rappresentati, per dare alla lede qualcosa di vero da dire."""
    common = Counter(e.venue for e in evs if e.venue).most_common(n)
    names = [v for v, _ in common]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _window_copy(slug: str, label: str, phrase: str,
                 days: list[date], n: int, venues: str) -> tuple[str, str, str]:
    """(title, h1, lede). L'h1 porta sempre la data esplicita: 'Tonight' da
    solo invecchia male, 'Tonight, Tuesday 18 August 2026' no."""
    rng = fmt_range(days)
    tail = f" Highlights at {venues}." if venues else ""

    if slug == "tonight":
        return (
            f"What's On in Amsterdam Tonight — {rng}",
            f"Amsterdam tonight, {rng}",
            f"{plural(n)} starting from {TONIGHT_FROM_HOUR}:00 on {rng}.{tail}",
        )
    if slug in ("today", "tomorrow"):
        return (
            f"Amsterdam Events {label} — {rng}",
            f"Events in Amsterdam {phrase}, {rng}",
            f"{plural(n)} in Amsterdam on {rng}.{tail}",
        )
    return (
        f"Amsterdam Events {label} — {rng}",
        f"Amsterdam {phrase}, {rng}",
        f"{plural(n)} across {rng} — concerts, club nights and culture.{tail}",
    )


# ── Costruzione ────────────────────────────────────────────────────
def build_pages(days: int = HORIZON_DAYS) -> dict:
    """Rigenera site/events/ da zero e riscrive site/sitemap.xml."""
    init_db()
    _check_js_labels_in_sync()

    generated = datetime.now()
    today = generated.date()

    # Si legge anche INDIETRO di PAST_DAY_GRACE_DAYS: le pagine gia' indicizzate
    # devono sopravvivere alla data dell'evento invece di diventare 404.
    raw = get_events_between(today - timedelta(days=PAST_DAY_GRACE_DAYS),
                             today + timedelta(days=days))
    # Prima di qualunque conteggio: i mesi che meritano una pagina e i numeri
    # stampati nelle lede devono contare eventi, non righe.
    events = collapse_duplicates(raw)
    log.info("Letti %d eventi dal DB (da -%d a +%d giorni), %d dopo il dedup.",
             len(raw), PAST_DAY_GRACE_DAYS, days, len(events))

    by_day: dict[date, list[Event]] = defaultdict(list)
    for ev in events:
        if ev.date:
            by_day[ev.date.date()].append(ev)
    for evs in by_day.values():
        evs.sort(key=lambda e: (e.date, (e.title or "").lower()))

    # I mesi si contano SOLO sul futuro: includere i giorni di grazia gonfierebbe
    # il mese corrente e falserebbe la soglia di MONTH_PAGE_MIN_EVENTS.
    future = [ev for ev in events if ev.date and ev.date.date() >= today]
    month_slugs, day_end = _page_horizon(future, today)

    day_list = sorted(d for d in by_day if today <= d <= day_end)
    past_days = sorted(d for d in by_day if d < today)
    day_slugs = {d.isoformat() for d in day_list}
    day_iso = [d.isoformat() for d in day_list]

    # I "picks" si calcolano SOLO sugli eventi che finiscono davvero in pagina.
    # DiscoveryEngine, per un artista che non e' in libreria e non e' in cache,
    # interroga Last.fm: valutare anche la coda oltre l'orizzonte (un pugno di
    # date del 2027, che nessuna pagina mostra) sarebbe un centinaio di
    # richieste di rete al giorno per niente.
    discovery = DiscoveryEngine()
    matches: dict[str, dict] = {}
    for d in day_list + past_days:
        for ev in by_day[d]:
            res = discovery.evaluate_event(artists_for(ev))
            if res.get("match"):
                matches[ev.dedup_key] = res

    # Ricomincia da zero: senza questo, la pagina di ieri resterebbe online per
    # sempre e la sitemap continuerebbe a dichiararla.
    if PAGES_DIR.exists():
        shutil.rmtree(PAGES_DIR)
    PAGES_DIR.mkdir(parents=True, exist_ok=True)

    # Un .ics per evento: i bottoni "Calendar" delle pagine statiche sono <a>,
    # non <button>, e non hanno il JS che li genera al volo nella SPA.
    # Solo per il futuro: un invito al calendario per una serata gia' passata
    # non serve a nessuno, e sono ~1900 file scritti ogni mattina.
    ics_rel: dict[str, str] = {}
    for d in day_list:
        for ev in by_day[d]:
            if ev.dedup_key not in ics_rel:
                # quote(): _write_ics_file costruisce il nome dal titolo e ci
                # lascia dentro gli spazi ("a1b2_Live At The Bimhuis.ics").
                ics_rel[ev.dedup_key] = "../" + quote(_write_ics_file(ev, ICS_DIR))

    # http.server, se una cartella non ha index.html, ne serve il listato: senza
    # questo file /events/ics/ sarebbe una pagina con dentro duemila link .ics,
    # navigabile e indicizzabile. Non e' un segreto, e' solo rumore.
    if ics_rel:
        (ICS_DIR / "index.html").write_text(
            '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
            '<meta name="robots" content="noindex"><title>Calendar files</title>'
            '</head><body><p>Calendar (.ics) files for individual events. '
            '<a href="../">Browse events by date</a>.</p></body></html>',
            encoding="utf-8",
        )

    written: list[tuple[str, str, float]] = []   # (slug, url, priority)

    def write_page(slug: str, markup: str) -> None:
        out_dir = PAGES_DIR / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_text(markup, encoding="utf-8")

    # ── Finestre relative ──────────────────────────────────────────
    specs = _window_specs(today)
    win_view: dict[str, dict[date, list[Event]]] = {}
    for slug, wdays, _label, _phrase in specs:
        if slug == "tonight":
            view = {today: [e for e in by_day.get(today, [])
                            if e.date and e.date.hour >= TONIGHT_FROM_HOUR]}
        else:
            view = {d: by_day.get(d, []) for d in wdays}
        win_view[slug] = {d: evs for d, evs in view.items() if evs}

    win_counts = [
        (slug, label, sum(len(v) for v in win_view[slug].values()))
        for slug, _wdays, label, _phrase in specs
    ]

    for slug, wdays, label, phrase in specs:
        view = win_view[slug]
        shown = [d for d in sorted(view)]
        flat = [ev for d in shown for ev in view[d]]
        n = len(flat)
        title, h1, lede = _window_copy(slug, label, phrase, wdays, n, _top_venues(flat))

        month_key = wdays[0].strftime("%Y-%m") if wdays else ""
        crumbs = _link("../../", "◀ Calendar") + _link("../", "All dates")
        if month_key in month_slugs:
            crumbs += _link(f"../{month_key}/", fmt_month_long(int(month_key[:4]), int(month_key[5:7])))

        links = _window_strip(win_counts, slug)
        links += "".join(_link(f"../{d.isoformat()}/", fmt_day_short(d))
                         for d in shown if d.isoformat() in day_slugs)

        extra = ""
        if slug in ("today", "tonight", "tomorrow"):
            extra = _freshness_script(today, 1 if slug == "tomorrow" else 0, day_iso)
        if slug in ("today", "tonight"):
            # Solo qui: su /tomorrow/ non e' ancora iniziato niente, e sulle
            # finestre lunghe la lista e' gia' spezzata per giorno.
            extra += _started_earlier_script()

        write_page(slug, _shell(
            title=title, description=lede, canonical=f"{BASE_URL}/events/{slug}/",
            root="../../", h1=h1, lede=lede,
            body=_events_section(shown, view, matches, ics_rel, headings=len(shown) > 1),
            jsonld=_jsonld_script(flat), crumbs=crumbs, links=links,
            extra_scripts=extra, generated=generated,
        ))
        written.append((slug, f"{BASE_URL}/events/{slug}/", 0.9))

    # ── Una pagina per giorno ──────────────────────────────────────
    for i, d in enumerate(day_list):
        evs = by_day[d]
        slug = d.isoformat()
        long_day = fmt_day_long(d)

        # La pagina di OGGI e quella di DOMANI hanno lo stesso identico
        # contenuto di /today/ e /tomorrow/ — misurato: sovrapposizione 100%.
        # Due URL con la stessa roba si fanno concorrenza, e il rischio e' che
        # Google tenga la pagina-data e scarti la finestra, cioe' proprio
        # l'indirizzo che intercetta "amsterdam events today". Nessuno cerca
        # "amsterdam events 19 august" per oggi: quella data ha valore solo da
        # domani in poi. Quindi finche' sono gemelle il canonical le consolida
        # sulla finestra, e la pagina-data resta fuori dalla sitemap.
        twin = ("today" if d == today
                else "tomorrow" if d == today + timedelta(days=1) else None)
        canonical = f"{BASE_URL}/events/{twin or slug}/"
        venues = _top_venues(evs)
        lede = (f"{plural(len(evs))} in Amsterdam on {long_day}: concerts, club nights, "
                f"exhibitions and more."
                + (f" Including {venues}." if venues else ""))

        month_key = d.strftime("%Y-%m")
        crumbs = _link("../../", "◀ Calendar") + _link("../", "All dates")
        if month_key in month_slugs:
            crumbs += _link(f"../{month_key}/", fmt_month_long(d.year, d.month))

        links = ""
        if i:
            links += _link(f"../{day_list[i - 1].isoformat()}/", f"◀ {fmt_day_short(day_list[i - 1])}")
        if i + 1 < len(day_list):
            links += _link(f"../{day_list[i + 1].isoformat()}/", f"{fmt_day_short(day_list[i + 1])} ▶")
        links += _window_strip(win_counts, "")

        write_page(slug, _shell(
            title=f"Amsterdam Events on {long_day} — {plural(len(evs))}",
            description=lede, canonical=canonical, root="../../",
            h1=f"Events in Amsterdam on {long_day}", lede=lede,
            body=_events_section([d], by_day, matches, ics_rel, headings=False),
            jsonld=_jsonld_script(evs), crumbs=crumbs, links=links, generated=generated,
        ))
        if not twin:
            written.append((slug, f"{BASE_URL}/events/{slug}/", 0.8))

    # ── Le date gia' passate, in scadenza ──────────────────────────
    # Restano in piedi PAST_DAY_GRACE_DAYS giorni per non lasciare un 404 a chi
    # arriva da un risultato di ricerca vecchio. Non entrano nella sitemap, non
    # sono linkate dall'indice ne' dalle pagine-mese, e portano noindex: sono
    # atterraggi per link stantii, non pagine da far trovare.
    for i, d in enumerate(past_days):
        evs = by_day[d]
        slug = d.isoformat()
        long_day = fmt_day_long(d)
        lede = (f"{plural(len(evs))} took place in Amsterdam on {long_day}. "
                f"This date has passed.")

        avanti = day_list[0] if day_list else None
        notice = (
            '<div class="past-notice">'
            f'<strong>This date has passed.</strong>'
            '<span>See what\'s on '
            f'<a href="../today/">today</a>, '
            f'<a href="../tonight/">tonight</a> or '
            f'<a href="../this-weekend/">this weekend</a>.</span>'
            "</div>"
        )

        links = ""
        if i:
            links += _link(f"../{past_days[i - 1].isoformat()}/", f"◀ {fmt_day_short(past_days[i - 1])}")
        if i + 1 < len(past_days):
            links += _link(f"../{past_days[i + 1].isoformat()}/", f"{fmt_day_short(past_days[i + 1])} ▶")
        elif avanti:
            links += _link(f"../{avanti.isoformat()}/", f"{fmt_day_short(avanti)} ▶")
        links += _window_strip(win_counts, "")

        write_page(slug, _shell(
            title=f"Amsterdam Events on {long_day}",
            description=lede, canonical=f"{BASE_URL}/events/{slug}/", root="../../",
            h1=f"Events in Amsterdam on {long_day}", lede=lede,
            body=notice + _events_section([d], by_day, matches, ics_rel, headings=False),
            crumbs=_link("../../", "\u25c0 Calendar") + _link("../", "All dates"),
            links=links, noindex=True, generated=generated,
        ))
        # NIENTE written.append(): fuori dalla sitemap, apposta.

    # ── Una pagina per mese ────────────────────────────────────────
    for mslug in month_slugs:
        y, m = int(mslug[:4]), int(mslug[5:7])
        mdays = [d for d in sorted(by_day) if (d.year, d.month) == (y, m) and d >= today]
        n = sum(len(by_day[d]) for d in mdays)
        name = fmt_month_long(y, m)
        lede = (f"{plural(n)} in Amsterdam across {len(mdays)} days in {name}. "
                f"Pick a date for the full listing.")

        idx = month_slugs.index(mslug)
        links = ""
        if idx:
            py, pm = int(month_slugs[idx - 1][:4]), int(month_slugs[idx - 1][5:7])
            links += _link(f"../{month_slugs[idx - 1]}/", f"◀ {fmt_month_long(py, pm)}")
        if idx + 1 < len(month_slugs):
            ny, nm = int(month_slugs[idx + 1][:4]), int(month_slugs[idx + 1][5:7])
            links += _link(f"../{month_slugs[idx + 1]}/", f"{fmt_month_long(ny, nm)} ▶")
        links += _window_strip(win_counts, "")

        write_page(mslug, _shell(
            title=f"Amsterdam Events {name} — {plural(n)}, day by day",
            description=lede, canonical=f"{BASE_URL}/events/{mslug}/", root="../../",
            h1=f"Events in Amsterdam, {name}", lede=lede,
            body=_month_body(y, m, by_day, matches, day_slugs),
            crumbs=_link("../../", "◀ Calendar") + _link("../", "All dates"),
            links=links, generated=generated,
        ))
        written.append((mslug, f"{BASE_URL}/events/{mslug}/", 0.7))

    # ── Indice ─────────────────────────────────────────────────────
    # len(future), non len(events): il conteggio in pagina deve escludere
    # i giorni di grazia, che sono passati.
    _write_index(day_list, by_day, month_slugs, win_counts, generated, len(future))
    written.append(("index", f"{BASE_URL}/events/", 0.6))

    _write_sitemap(written, generated)

    log.info("Scritte %d pagine sotto %s (+ %d in scadenza, fuori sitemap; %d file .ics).",
             len(written), PAGES_DIR, len(past_days), len(ics_rel))
    return {
        "pages": len(written),
        "past": len(past_days),
        "days": len(day_list),
        "months": len(month_slugs),
        "ics": len(ics_rel),
        "events": len(events),
    }


def _write_index(day_list: list[date], by_day: dict[date, list[Event]],
                 month_slugs: list[str], win_counts: list[tuple[str, str, int]],
                 generated: datetime, total: int) -> None:
    """
    L'elenco di tutte le pagine. Serve ai crawler (un solo hop da "/" a
    qualunque URL generato) e a chi arriva da una pagina e vuole guardarsi
    intorno senza tornare alla SPA.
    """
    groups = []

    windows = "".join(
        f'<a href="{esc(slug)}/">{esc(label)} <span class="n">{n}</span></a>'
        for slug, label, n in win_counts if n
    )
    groups.append(f'<section class="index-group"><h2>Right now</h2>'
                  f'<div class="index-list">{windows}</div></section>')

    if month_slugs:
        months = "".join(
            f'<a href="{esc(m)}/">{esc(fmt_month_long(int(m[:4]), int(m[5:7])))} '
            f'<span class="n">{sum(len(by_day[d]) for d in by_day if d.strftime("%Y-%m") == m)}</span></a>'
            for m in month_slugs
        )
        groups.append(f'<section class="index-group"><h2>By month</h2>'
                      f'<div class="index-list">{months}</div></section>')

    by_month: dict[str, list[date]] = defaultdict(list)
    for d in day_list:
        by_month[d.strftime("%Y-%m")].append(d)
    for mkey in sorted(by_month):
        y, m = int(mkey[:4]), int(mkey[5:7])
        items = "".join(
            f'<a href="{d.isoformat()}/">{esc(fmt_day_short(d))} '
            f'<span class="n">{len(by_day[d])}</span></a>'
            for d in by_month[mkey]
        )
        groups.append(f'<section class="index-group"><h2>{esc(fmt_month_long(y, m))}</h2>'
                      f'<div class="index-list">{items}</div></section>')

    lede = (f"Every date we have listings for: {plural(total)} in Amsterdam across "
            f"{len(day_list)} days. Pick a day, a weekend or a whole month.")

    (PAGES_DIR / "index.html").write_text(_shell(
        title="Amsterdam Events by Date — Every Day, Weekend and Month",
        description=lede, canonical=f"{BASE_URL}/events/", root="../",
        h1="Browse Amsterdam events by date", lede=lede,
        body="".join(groups),
        crumbs=_link("../", "◀ Calendar"),
        links=_link("../", "◀ Back to the calendar"),
        generated=generated,
    ), encoding="utf-8")


def _write_sitemap(written: list[tuple[str, str, float]], generated: datetime) -> None:
    lastmod = generated.date().isoformat()
    entries = [(f"{BASE_URL}/", 1.0)] + [(url, prio) for _slug, url, prio in written]
    body = "\n".join(
        f"  <url><loc>{esc(url)}</loc><lastmod>{lastmod}</lastmod>"
        f"<changefreq>daily</changefreq><priority>{prio}</priority></url>"
        for url, prio in entries
    )
    (SITE_DIR / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{body}\n</urlset>\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(name)-25s │ %(levelname)-5s │ %(message)s",
        datefmt="%H:%M:%S",
    )
    stats = build_pages()
    print(
        f"\n✅ {stats['pages']} pagine sotto {PAGES_DIR}"
        f"\n   {stats['days']} giorni · {stats['months']} mesi · 7 finestre · 1 indice"
        f"\n   + {stats['past']} date passate in scadenza (noindex, fuori sitemap)"
        f"\n   {stats['ics']} file .ics · {stats['events']} eventi letti dal DB"
        f"\n   sitemap: {SITE_DIR / 'sitemap.xml'}"
    )
