#!/usr/bin/env python3
"""
Diagnosi di uno scraper rotto: cosa e' cambiato nella pagina, e dove sono
finiti i dati adesso.

QUANDO GIRA
Solo su una fonte gia' in allarme secondo health.py. E' rarissima per
costruzione: oggi produrrebbe UNA chiamata (amsterdam_alt), e la stessa
rottura non si ri-diagnostica per COOLDOWN_DAYS giorni — altrimenti la
diagnosi ripartirebbe ogni mattina finche' nessuno ripara.

COSA VEDE IL MODELLO
Nome e URL della fonte, quali campi sono caduti, il pezzo di collector che
prova a estrarli, e una fetta della pagina riscaricata: via <script>/<style>,
troncata a ~40 KB (se e' JSON, il JSON indentato).

COSA RESTITUISCE
Un REPORT, non codice: cosa e' cambiato, dove stanno i dati adesso (chiave
JSON o selettore), un valore d'esempio preso da quell'HTML, una confidenza.
Il modello non tocca i collector — la correzione la scrive una persona, con
la diagnosi davanti. Riparare in automatico codice che gira su undici siti di
terzi senza che nessuno guardi e' fuori perimetro per scelta.

FALLIMENTO
Non solleva mai. Chiave assente, API giu', pagina non scaricabile, JSON
storto ⇒ nessuna diagnosi, e l'allarme deterministico di health.py resta
comunque — che e' quello che conta.

    ./.venv/bin/python diagnose.py --source amsterdam_alt
    ./.venv/bin/python diagnose.py --source amsterdam_alt --force   # ignora il cooldown
"""
from __future__ import annotations

import argparse
import ast
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from config import HEADERS, REQUEST_TIMEOUT
from enrich_artists import api_key, call_gemini
from health import (
    COLLECTOR_FILES, FIELDS, HEALTH_DB, Alert, init_health_db, run_checks,
)

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

# Quanto della pagina si manda. 40 KB bastano per riconoscere la struttura e
# tengono il prompt su un ordine di grandezza ragionevole.
PAGE_CHARS = 40_000
COLLECTOR_CHARS = 8_000
COOLDOWN_DAYS = 7
# Qui il pensiero del modello resta ACCESO (default del modello), al contrario
# di enrich_artists.py: la' si copia un nome dall'input, qui si confronta un
# codice con una pagina e si deduce dove sono finiti i dati — e' giudizio, la
# categoria di task in cui il thinking paga.
THINKING_BUDGET = None

SYSTEM_INSTRUCTION = (
    "You are debugging a web scraper that stopped extracting some fields. "
    "You are given: the parsing code, and the CURRENT response of the page or "
    "API it reads.\n"
    "For each broken field, work out where that piece of information lives in "
    "the current response, and how the code fails to reach it.\n"
    "Rules:\n"
    "- Ground every answer in the response you were given. Quote a real key "
    "path (e.g. items[].type.label) or a real CSS selector, and a real "
    "example value copied from it.\n"
    "- If the information is simply NOT in the response any more, say so "
    "explicitly and set where_now to \"\" — that is a useful answer.\n"
    "- Never write replacement code, and never guess a key you cannot see.\n"
    'Answer with JSON only: {"findings": [{"field": "...", "what_changed": '
    '"...", "where_now": "...", "example_value": "...", "confidence": '
    '"alta|media|bassa"}], "summary": "..."}'
)


# ── La pagina, adesso ──────────────────────────────────────────────
def fetch_probe(url: str) -> tuple[str, str]:
    """(testo, nota). Mai solleva: in caso di guai il testo e' vuoto.

    Gli endpoint che il collector interroga in POST (ra, paradiso,
    museumkaart) rispondono male a una GET: si annota e si va avanti, il
    modello lavora sul solo codice. Rifare la POST con il payload giusto
    vorrebbe dire duplicare il collector qui dentro.
    """
    if not url:
        return "", "il collector non dichiara probe_url"
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT,
                         follow_redirects=True)
    except httpx.HTTPError as e:
        return "", f"pagina non scaricabile: {e}"

    note = f"HTTP {resp.status_code}, {resp.headers.get('content-type', '?')}"
    if resp.status_code != 200:
        return "", note + " — nessun contenuto utilizzabile"

    ctype = resp.headers.get("content-type", "")
    text = resp.text
    if "json" in ctype:
        try:
            text = json.dumps(resp.json(), indent=1, ensure_ascii=False)
        except ValueError:
            pass
    else:
        # Via gli script e gli stili: sono la maggior parte del peso di una
        # pagina e non contengono i dati che si stanno cercando — tranne i
        # blob tipo __NEXT_DATA__, che si tengono apposta.
        text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text,
                      flags=re.S | re.I)
        text = re.sub(
            r"<script\b(?![^>]*(?:application/json|__NEXT_DATA__))[^>]*>.*?</script>",
            " ", text, flags=re.S | re.I)
        text = re.sub(r"\n\s*\n+", "\n", text)

    if len(text) > PAGE_CHARS:
        text = text[:PAGE_CHARS] + "\n…[troncato]"
    return text, note


# ── Il pezzo di collector che prova a estrarre quei campi ──────────
def collector_excerpt(source: str, fields: list[str]) -> str:
    """Le funzioni del collector che assegnano quei campi.

    Non tutto il file: si manda il punto dove il parsing avviene, che e'
    quello su cui il modello deve pronunciarsi. Se non si riesce a isolarlo,
    si manda il file intero (sono piccoli).
    """
    fname = COLLECTOR_FILES.get(source)
    if not fname:
        return ""
    path = BASE_DIR / "collectors" / fname
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return ""

    lines = src.splitlines()
    wanted = set(fields)
    keep: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = {
            t.id
            for sub in ast.walk(node)
            if isinstance(sub, ast.Assign)
            for t in sub.targets
            if isinstance(t, ast.Name)
        } | {
            sub.arg for sub in ast.walk(node)
            if isinstance(sub, ast.keyword) and sub.arg
        }
        if names & wanted:
            keep.append((node.lineno, getattr(node, "end_lineno", node.lineno), node.name))

    if not keep:
        return src[:COLLECTOR_CHARS]

    out = []
    for start, end, name in sorted(keep):
        out.append(f"# --- {fname}:{start} · {name}() ---")
        out.append("\n".join(lines[start - 1:end]))
    text = "\n".join(out)
    return text[:COLLECTOR_CHARS]


# ── Cooldown ───────────────────────────────────────────────────────
def _fields_key(fields: list[str]) -> str:
    return ",".join(sorted(fields))


def recent_diagnosis(source: str, fields: list[str],
                     days: int = COOLDOWN_DAYS) -> str | None:
    """Il report gia' prodotto per la stessa rottura, se e' recente."""
    if not HEALTH_DB.exists():
        return None
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(HEALTH_DB))
    try:
        row = conn.execute(
            "SELECT report FROM diagnoses WHERE source = ? AND fields = ? "
            "AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
            (source, _fields_key(fields), cutoff),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return row[0] if row else None


def save_diagnosis(source: str, fields: list[str], report: str) -> None:
    try:
        init_health_db()
        conn = sqlite3.connect(str(HEALTH_DB))
        try:
            with conn:
                conn.execute(
                    "INSERT INTO diagnoses (source, fields, created_at, report) "
                    "VALUES (?, ?, ?, ?)",
                    (source, _fields_key(fields),
                     datetime.now().isoformat(timespec="seconds"), report),
                )
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.warning("Diagnosi non salvata (%s).", e)


# ── La diagnosi ────────────────────────────────────────────────────
def probe_url_for(source: str) -> str:
    """L'URL dichiarato dal collector. Import locale: main.py importa mezzo
    progetto e non serve tirarselo dietro quando non si diagnostica."""
    try:
        from main import COLLECTORS
        cls = COLLECTORS.get(source)
        return getattr(cls, "probe_url", "") if cls else ""
    except Exception as e:  # noqa: BLE001 — un import rotto non deve fermare nulla
        log.warning("probe_url di %s non recuperabile (%s).", source, e)
        return ""


def diagnose(source: str, fields: list[str], force: bool = False) -> dict | None:
    """Il dizionario della diagnosi, o None se non e' stato possibile farla."""
    if not fields:
        return None

    if not force:
        cached = recent_diagnosis(source, fields)
        if cached:
            log.info("Diagnosi di %s gia' fatta negli ultimi %d giorni.",
                     source, COOLDOWN_DAYS)
            try:
                return json.loads(cached)
            except ValueError:
                return None

    key = api_key()
    if not key:
        log.error("GEMINI_API_KEY assente: nessuna diagnosi (l'allarme resta).")
        return None

    url = probe_url_for(source)
    page, note = fetch_probe(url)
    code = collector_excerpt(source, fields)

    user = (
        f"SOURCE: {source}\n"
        f"URL: {url or '(sconosciuto)'}\n"
        f"BROKEN FIELDS (0% filled, but the code assigns them): "
        f"{', '.join(fields)}\n\n"
        f"--- PARSING CODE ---\n{code}\n\n"
        f"--- CURRENT RESPONSE ({note}) ---\n{page or '(non disponibile)'}\n"
    )
    text = call_gemini(SYSTEM_INSTRUCTION, user, key,
                       thinking_budget=THINKING_BUDGET, timeout=180)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError as e:
        log.error("Diagnosi illeggibile (%s).", e)
        return None

    data["_source"] = source
    data["_fields"] = fields
    data["_url"] = url
    data["_probe"] = note
    save_diagnosis(source, fields, json.dumps(data, ensure_ascii=False))
    return data


def as_markdown(data: dict) -> str:
    """La diagnosi come pezzo di logs/health_report.md."""
    if not data:
        return ""
    lines = [f"### {data.get('_source', '?')}", ""]
    if data.get("summary"):
        lines += [data["summary"], ""]
    lines += [f"Pagina interrogata: `{data.get('_url', '')}` ({data.get('_probe', '')})", ""]
    for f in data.get("findings", []):
        where = f.get("where_now") or ""
        where = f"`{where}`" if where else "— non c'e' piu' nella risposta"
        lines += [
            f"**`{f.get('field', '?')}`** — confidenza {f.get('confidence', '?')}",
            "",
            f"- cambiato: {f.get('what_changed', '')}",
            f"- adesso sta in: {where}",
            f"- esempio: `{f.get('example_value', '')}`",
            "",
        ]
    return "\n".join(lines)


def diagnose_alerts(alerts: list[Alert], force: bool = False) -> str:
    """Diagnostica ogni fonte in allarme. Ritorna il markdown, "" se niente."""
    by_source: dict[str, list[str]] = {}
    for a in alerts:
        if a.field:
            by_source.setdefault(a.source, []).append(a.field)

    chunks = []
    for source, fields in sorted(by_source.items()):
        try:
            data = diagnose(source, sorted(set(fields)), force=force)
        except Exception as e:  # noqa: BLE001 — la diagnosi non ferma la pipeline
            log.error("Diagnosi di %s fallita (%s: %s)", source, type(e).__name__, e)
            continue
        if data:
            chunks.append(as_markdown(data))
    return "\n".join(chunks)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--source", help="la fonte da diagnosticare")
    ap.add_argument("--fields", help="campi separati da virgola (default: quelli in allarme)")
    ap.add_argument("--force", action="store_true", help="ignora il cooldown di %d giorni" % COOLDOWN_DAYS)
    args = ap.parse_args()

    alerts = run_checks()
    if args.source:
        alerts = [a for a in alerts if a.source == args.source]
        if args.fields:
            wanted = [f.strip() for f in args.fields.split(",") if f.strip() in FIELDS]
            alerts = [a for a in alerts if a.field in wanted]
        if not alerts and args.fields:
            alerts = [Alert(args.source, "manuale", f, "richiesto a mano")
                      for f in (args.fields or "").split(",") if f.strip() in FIELDS]

    if not alerts:
        print("Nessuna fonte in allarme: niente da diagnosticare.")
        return

    md = diagnose_alerts(alerts, force=args.force)
    if md:
        print(md)
    else:
        print("Nessuna diagnosi prodotta (chiave assente, API giu' o pagina "
              "non scaricabile). L'allarme resta valido.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(name)-12s │ %(levelname)-5s │ %(message)s",
        datefmt="%H:%M:%S",
    )
    main()
