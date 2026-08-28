#!/usr/bin/env python3
"""
Salute degli scraper: vedere le rotture PARZIALI, non solo i crolli a zero.

PERCHE'
Undici collector leggono siti di terzi e le regole di parsing si rompono
quando i siti cambiano — e' gia' successo (8b8817e su tre collector insieme,
817ea74 sul cambio di formato del Melkweg). La rilevazione in main.py era
binaria: allarme solo se una fonte torna ZERO eventi pur avendone in
archivio. Vede il crollo totale e nient'altro.

La prova che non basta: amsterdam_alt restituisce 125 eventi tutti i giorni,
cmd_collect dichiara successo, e `event_type` e' vuoto su tutti e 125 — il
collector prova a leggerlo (righe 113-123) e non ottiene niente da chissa'
quando. I generi vengono derivati dal tipo (riga 161), quindi sono a zero
anche loro.

I DUE CONTROLLI (deterministici, nessun modello: il modello non serve per
accorgersi, serve per capire — vedi diagnose.py)

(a) TEMPORALE — la fonte contro il proprio passato. A ogni collect si
    registra un'impronta (numero di eventi + % di riempimento di ogni campo)
    e la si confronta con la mediana delle ultime ~14. Scatta se il conteggio
    crolla sotto meta' della mediana o se un campo perde piu' di 40 punti.

(b) TRASVERSALE — la fonte contro se' stessa. Il controllo (a) da solo non
    avrebbe MAI visto amsterdam_alt: e' rotto da prima che esista lo storico,
    quindi la mediana avrebbe imparato lo stato rotto come normale. Serve un
    controllo che non dipenda dal passato: un campo a 0% e' sospetto solo se
    il collector prova a riempirlo.

IL DISCRIMINANTE (b): "prova a riempirlo" NON e' un grep
Un grep sul nome del campo produce 10 allarmi su 7 fonti, quasi tutti falsi:
`genres=[]`, `price_min=None`, `date_end=event_end` sono menzioni, non
tentativi. Si guarda invece l'AST del collector e si cerca un'ASSEGNAZIONE
NON BANALE a un nome uguale al campo:

    event_type = type_raw.get("name", "").strip()   ← tentativo (allarme)
    event_type=item.get("type")                     ← tentativo (allarme)
    genres=[]                                       ← letterale, non e' un tentativo
    date_end=event_end                              ← passa avanti una variabile

Misurato su 11 fonti × 9 campi: con questa regola si accende SOLO
amsterdam_alt (event_type, genres, image_url). Le altre dieci fonti, che pure
hanno decine di campi a 0%, restano zitte — ed e' il punto: un allarme che
grida al lupo non lo legge piu' nessuno.

    ./.venv/bin/python health.py --report     # tabella + verdetti
    ./.venv/bin/python health.py --json       # gli stessi verdetti, per script
"""
from __future__ import annotations

import argparse
import ast
import json
import logging
import sqlite3
import statistics
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from config import DATA_DIR
from models import Event

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
# File a parte e gitignorato: data/events.db e' tracciato in un repo PUBBLICO
# e non va fatto ingrassare con dati rigenerabili.
HEALTH_DB = DATA_DIR / "source_health.db"
REPORT_PATH = BASE_DIR / "logs" / "health_report.md"

# I campi che si misurano. `title`, `date`, `source_url` non ci sono apposta:
# senza quelli l'evento non viene nemmeno costruito, quindi un loro crollo si
# vede gia' come crollo del conteggio.
FIELDS = [
    "venue", "event_type", "artists", "genres",
    "description", "image_url", "tickets_url", "price_min", "date_end",
]

# Soglie del controllo temporale.
HISTORY_RUNS = 14        # quante impronte passate formano la mediana
MIN_HISTORY = 3          # sotto questo la baseline e' ancora in semina
COUNT_DROP_RATIO = 0.5   # conteggio sotto meta' della mediana ⇒ allarme
FIELD_DROP_POINTS = 40.0 # un campo che perde piu' di 40 punti ⇒ allarme

COLLECTOR_FILES = {
    "melkweg": "melkweg_collector.py",
    "amsterdam_alt": "amsterdam_alt_collector.py",
    "ra": "ra_collector.py",
    "murmur": "murmur_collector.py",
    "paradiso": "paradiso_collector.py",
    "museumkaart": "museumkaart_collector.py",
    "gallery_viewer": "gallery_viewer_collector.py",
    "muziekgebouw": "muziekgebouw_collector.py",
    "concertgebouw": "concertgebouw_collector.py",
    "splendor": "splendor_collector.py",
    "bimhuis": "bimhuis_collector.py",
}

CREATE_HEALTH_TABLES = """
CREATE TABLE IF NOT EXISTS source_runs (
    source     TEXT NOT NULL,
    run_at     TEXT NOT NULL,
    n_events   INTEGER NOT NULL,
    fill_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_source ON source_runs(source, run_at);

-- Quando l'ultima diagnosi col modello e' stata chiesta per una fonte, per
-- non ripeterla ogni mattina finche' la rottura resta aperta.
CREATE TABLE IF NOT EXISTS diagnoses (
    source      TEXT NOT NULL,
    fields      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    report      TEXT
);
CREATE INDEX IF NOT EXISTS idx_diag_source ON diagnoses(source, created_at);
"""


@dataclass
class Alert:
    source: str
    kind: str      # "cross" (fonte contro se stessa) | "temporal" (contro il passato)
    field: str     # "" quando riguarda il conteggio
    detail: str

    def line(self) -> str:
        where = f"{self.source}:{self.field}" if self.field else self.source
        return f"[{self.kind}] {where} — {self.detail}"


# ── Impronta ───────────────────────────────────────────────────────
def _filled(ev: Event, field: str) -> bool:
    v = getattr(ev, field, None)
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, tuple)):
        return bool(v)
    return v is not None


def fill_rates(events: list[Event]) -> dict[str, float]:
    """% di riempimento di ogni campo, 0-100. Lista vuota ⇒ tutti a 0."""
    if not events:
        return {f: 0.0 for f in FIELDS}
    return {
        f: 100.0 * sum(1 for ev in events if _filled(ev, f)) / len(events)
        for f in FIELDS
    }


# ── Storico ────────────────────────────────────────────────────────
def init_health_db(db_path: Path = HEALTH_DB) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            conn.executescript(CREATE_HEALTH_TABLES)
    finally:
        conn.close()


def record_run(source: str, events: list[Event], db_path: Path = HEALTH_DB) -> None:
    """Registra l'impronta di questo collect. Non solleva mai: la salute non
    deve poter far fallire la raccolta."""
    try:
        init_health_db(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            with conn:
                conn.execute(
                    "INSERT INTO source_runs (source, run_at, n_events, fill_json) "
                    "VALUES (?, ?, ?, ?)",
                    (source, datetime.now().isoformat(timespec="seconds"),
                     len(events), json.dumps(fill_rates(events))),
                )
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.warning("Impronta di %s non registrata (%s).", source, e)


def history(source: str, limit: int = HISTORY_RUNS,
            db_path: Path = HEALTH_DB) -> list[tuple[int, dict[str, float]]]:
    """Le ultime impronte, dalla piu' recente. [(n_events, fill), ...]"""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT n_events, fill_json FROM source_runs WHERE source = ? "
            "ORDER BY run_at DESC LIMIT ?",
            (source, limit),
        ).fetchall()
    except sqlite3.Error as e:
        log.warning("Storico di %s non leggibile (%s).", source, e)
        return []
    finally:
        conn.close()

    out = []
    for n, fill_json in rows:
        try:
            out.append((n, json.loads(fill_json)))
        except (TypeError, ValueError):
            continue
    return out


# ── Il discriminante: cosa il collector PROVA a riempire ───────────
def _is_trivial(node: ast.AST) -> bool:
    """Valore che non e' un'estrazione: una costante o un contenitore vuoto."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return not node.elts
    if isinstance(node, ast.Dict):
        return not node.keys
    return False


def extracted_fields(source: str) -> dict[str, int]:
    """{campo: quante volte il collector prova a estrarlo}.

    Conta due forme:
      - un'assegnazione `campo = <espressione non banale>`
      - un keyword `campo=<espressione>` che NON sia una semplice variabile
        (`event_type=item.get("type")` conta, `date_end=event_end` no: la',
        l'estrazione — se c'e' — sta nel nome che porta il valore, e il campo
        e' solo il posto dove finisce).

    Se il file non c'e' o non si parsa, ritorna vuoto: in dubbio si sta
    zitti, perche' il costo di un falso allarme e' che nessuno legge piu' gli
    allarmi veri.
    """
    fname = COLLECTOR_FILES.get(source)
    if not fname:
        return {}
    path = BASE_DIR / "collectors" / fname
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as e:
        log.warning("Collector %s non analizzabile (%s).", source, e)
        return {}

    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        elif isinstance(node, ast.keyword) and node.arg:
            if isinstance(node.value, ast.Name):
                continue
            targets, value = [ast.Name(id=node.arg)], node.value
        else:
            continue

        if _is_trivial(value):
            continue

        flat: list[ast.AST] = []
        for t in targets:
            flat.extend(t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t])
        for t in flat:
            if isinstance(t, ast.Name) and t.id in FIELDS:
                found[t.id] = found.get(t.id, 0) + 1
    return found


# ── I due controlli ────────────────────────────────────────────────
def check_cross(source: str, fills: dict[str, float]) -> list[Alert]:
    """(b) Campi a 0% che il collector prova a riempire."""
    tries = extracted_fields(source)
    alerts = []
    for field, pct in sorted(fills.items()):
        if pct == 0.0 and field in tries:
            alerts.append(Alert(
                source, "cross", field,
                f"0% riempito, ma il collector prova a estrarlo "
                f"({tries[field]} volte nel codice)",
            ))
    return alerts


def check_temporal(source: str, n_events: int, fills: dict[str, float],
                   past: list[tuple[int, dict[str, float]]]) -> list[Alert]:
    """(a) L'impronta di oggi contro la mediana delle precedenti."""
    if len(past) < MIN_HISTORY:
        return []

    alerts = []
    med_n = statistics.median([n for n, _ in past])
    if med_n > 0 and n_events < med_n * COUNT_DROP_RATIO:
        alerts.append(Alert(
            source, "temporal", "",
            f"{n_events} eventi contro una mediana di {med_n:.0f} "
            f"sulle ultime {len(past)} raccolte",
        ))

    for field in FIELDS:
        values = [f.get(field) for _, f in past if f.get(field) is not None]
        if len(values) < MIN_HISTORY:
            continue
        med = statistics.median(values)
        now = fills.get(field, 0.0)
        if med - now > FIELD_DROP_POINTS:
            alerts.append(Alert(
                source, "temporal", field,
                f"riempimento {now:.0f}% contro una mediana di {med:.0f}%",
            ))
    return alerts


def check_source(source: str, events: list[Event] | None = None,
                 fills: dict[str, float] | None = None,
                 n_events: int | None = None) -> list[Alert]:
    """Tutti e due i controlli per una fonte."""
    if events is not None:
        fills = fill_rates(events)
        n_events = len(events)
    if fills is None:
        return []
    past = history(source)
    return (check_cross(source, fills)
            + check_temporal(source, n_events or 0, fills, past))


# ── Stato corrente letto dal DB degli eventi ───────────────────────
def current_state(days: int = 365) -> dict[str, tuple[int, dict[str, float]]]:
    """{fonte: (n, riempimenti)} sugli eventi FUTURI in archivio.

    E' la via che rende `--report` utile anche prima del primo collect: il
    controllo trasversale non ha bisogno di storico, e questo e' esattamente
    il caso di amsterdam_alt, rotto da prima che lo storico esistesse.
    """
    from db import get_upcoming_events  # import qui: db.py apre il file

    by_source: dict[str, list[Event]] = {}
    for ev in get_upcoming_events(days=days):
        by_source.setdefault(ev.source, []).append(ev)
    return {s: (len(evs), fill_rates(evs)) for s, evs in by_source.items()}


def run_checks(state: dict[str, tuple[int, dict[str, float]]] | None = None) -> list[Alert]:
    """I verdetti su tutte le fonti. Deterministico: due esecuzioni di fila
    danno lo stesso risultato."""
    state = current_state() if state is None else state
    alerts: list[Alert] = []
    for source in sorted(state):
        n, fills = state[source]
        alerts.extend(check_source(source, fills=fills, n_events=n))
    return alerts


# ── Report ─────────────────────────────────────────────────────────
def write_report(alerts: list[Alert],
                 state: dict[str, tuple[int, dict[str, float]]] | None = None,
                 diagnoses: str = "") -> Path:
    """Scrive logs/health_report.md.

    In logs/ e NON in output/: mailer.py:264 sceglie il digest da spedire con
    OUTPUT_DIR.glob("digest_*.html") ordinato al contrario, e un file di
    troppo la' dentro e' un rischio che non vale la pena correre.
    """
    state = current_state() if state is None else state
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Salute degli scraper",
        "",
        f"Generato: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "## Verdetti",
        "",
    ]
    if alerts:
        for a in alerts:
            lines.append(f"- **{a.source}** · `{a.field or 'conteggio'}` "
                         f"({a.kind}) — {a.detail}")
    else:
        lines.append("Nessun allarme.")

    lines += ["", "## Riempimento dei campi per fonte", "",
              "| fonte | n | " + " | ".join(FIELDS) + " |",
              "|---|---:| " + " | ".join("---:" for _ in FIELDS) + " |"]
    for source in sorted(state, key=lambda s: -state[s][0]):
        n, fills = state[source]
        lines.append(f"| {source} | {n} | "
                     + " | ".join(f"{fills.get(f, 0):.0f}%" for f in FIELDS) + " |")

    if diagnoses:
        lines += ["", "## Diagnosi", "", diagnoses]

    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return REPORT_PATH


def print_report(state: dict[str, tuple[int, dict[str, float]]],
                 alerts: list[Alert]) -> None:
    head = f"{'fonte':16}{'n':>6}  " + "  ".join(f"{f[:9]:>9}" for f in FIELDS)
    print(head)
    print("─" * len(head))
    for source in sorted(state, key=lambda s: -state[s][0]):
        n, fills = state[source]
        print(f"{source:16}{n:6}  "
              + "  ".join(f"{fills.get(f, 0):8.0f}%" for f in FIELDS))
    print()
    if alerts:
        print(f"⚠️  {len(alerts)} allarme/i:")
        for a in alerts:
            print("   " + a.line())
    else:
        print("✅ Nessun allarme.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--report", action="store_true",
                    help="tabella dei riempimenti + verdetti (default)")
    ap.add_argument("--json", action="store_true", help="i verdetti in JSON")
    ap.add_argument("--write", action="store_true",
                    help="scrive anche logs/health_report.md")
    args = ap.parse_args()

    state = current_state()
    alerts = run_checks(state)

    if args.json:
        print(json.dumps([asdict(a) for a in alerts], indent=2, ensure_ascii=False))
    else:
        print_report(state, alerts)

    if args.write:
        path = write_report(alerts, state)
        print(f"\nReport: {path}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s │ %(name)-12s │ %(levelname)-5s │ %(message)s",
        datefmt="%H:%M:%S",
    )
    main()
