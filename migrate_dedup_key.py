#!/usr/bin/env python3
"""
Migrazione delle righe esistenti alla nuova `dedup_key` (titolo + giorno +
sala canonica). Script a parte, non dentro db.py: si lancia a mano, una volta.

PERCHE' ESISTE
`dedup_key` e' la PRIMARY KEY della tabella `events`. Cambiare la formula in
models.py e basta peggiora la situazione invece di risolverla: il collect
successivo calcola le chiavi nuove, non riconosce nessuna delle righe gia'
presenti, le re-inserisce tutte e lascia le vecchie orfane — il doppio dei
doppioni, piu' migliaia di righe fantasma che nessuno aggiornera' piu'.
Formula nuova e migrazione sono la stessa change. Questo file e' la seconda
meta'.

Non e' una precauzione teorica: nel DB il meccanismo era gia' partito da solo.
Quando una fonte correggeva il locale di un evento gia' salvato — Paradiso che
passa da "Tolhuistuin" a "Paradiso (Tolhuistuin)" — la chiave cambiava e
nasceva una riga nuova, mentre la vecchia smetteva di essere aggiornata. Si
riconoscono dall'`updated_at` congelato a fianco di un gemello aggiornato
stamattina, e dallo stesso `source_url`.

CHI SOPRAVVIVE
`dedup._richness()`, la stessa funzione che sceglie a valle: penalizza il
mojibake (una riga nel DB, "POLIÃ‡A"), poi conta artisti, generi, lunghezza del
titolo, e infine preferisce la fonte del locale all'aggregatore. A parita' di
ricchezza vince la riga aggiornata piu' di recente — fra due grafie del locale
quella fresca e' la grafia che la fonte usa ADESSO — e a parita' di tutto la
`dedup_key` vecchia, perche' due esecuzioni devono scegliere la stessa riga.

La riga superstite eredita comunque il `created_at` piu' antico e
l'`updated_at` piu' recente del gruppo: la fusione non deve far sembrare
l'evento piu' giovane di quanto sia.

USO
    ./.venv/bin/python migrate_dedup_key.py                 # dry run, non tocca niente
    ./.venv/bin/python migrate_dedup_key.py --db copia.db   # dry run su un'altra copia
    ./.venv/bin/python migrate_dedup_key.py --apply         # scrive, dopo aver fatto il backup

E' idempotente: sul DB gia' migrato la seconda esecuzione trova 0 fusioni.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from config import DB_PATH
from dedup import _richness
from models import Event

COLUMNS = [
    "dedup_key", "source", "source_id", "source_url", "title", "date",
    "date_end", "venue", "venue_address", "event_type", "artists", "genres",
    "description", "image_url", "price_min", "price_max", "tickets_url",
    "tickets_status", "attending_count", "scraped_at", "created_at", "updated_at",
]


def _new_key(row: dict) -> str:
    """La chiave nuova, calcolata dal MODELLO e non da una copia della formula."""
    return Event.from_row(row).compute_dedup_key()


def _survivor(rows: list[dict]) -> dict:
    """La riga che resta, fra quelle che collassano sulla stessa chiave."""
    return max(
        rows,
        key=lambda r: (
            _richness(Event.from_row(r)),
            r.get("updated_at") or "",
            r.get("dedup_key") or "",
        ),
    )


def plan(db: Path) -> tuple[list[dict], list[list[dict]], int]:
    """Ritorna (righe finali, gruppi fusi, quante chiavi cambiano)."""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM events")]
    conn.close()

    changed = 0
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        k = _new_key(r)
        if k != r["dedup_key"]:
            changed += 1
        r["_new_key"] = k
        groups[k].append(r)

    final, merged = [], []
    for k, grp in groups.items():
        if len(grp) > 1:
            merged.append(grp)
        keep = dict(_survivor(grp))
        keep["dedup_key"] = k
        keep["created_at"] = min((g.get("created_at") or "") for g in grp) or keep.get("created_at")
        keep["updated_at"] = max((g.get("updated_at") or "") for g in grp) or keep.get("updated_at")
        final.append(keep)
    return final, merged, changed


def report(db: Path, final: list[dict], merged: list[list[dict]], changed: int) -> None:
    conn = sqlite3.connect(str(db))
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    absorbed = total - len(final)
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"righe          : {total} -> {len(final)}   (assorbite {absorbed})")
    print(f"chiavi cambiate: {changed} su {total}")
    print(f"gruppi fusi    : {len(merged)}")
    xs = sum(1 for g in merged if len({r['source'] for r in g}) > 1)
    print(f"  cross-source : {xs}")
    print(f"  intra-source : {len(merged) - xs}")
    fut = sum(1 for r in final if (r.get("date") or "")[:10] >= today)
    print(f"righe future dopo la migrazione: {fut}")

    if merged:
        print("\nesempi di fusione (prime 15):")
        for g in sorted(merged, key=lambda x: x[0].get("date") or "")[:15]:
            keep = _survivor(g)
            print(f"  {(g[0].get('date') or '')[:16]}  {(g[0].get('title') or '')[:44]!r}")
            for r in g:
                mark = "KEEP " if r is keep else "  -  "
                print(f"      {mark}{r['source']:14s} {r.get('venue')!r}"
                      f"  updated={r.get('updated_at')}")


def apply(db: Path, final: list[dict]) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    backup = REPO / "backups" / f"{db.name}.bak-{stamp}-premigrate"
    backup.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(db))
    dst = sqlite3.connect(str(backup))
    with dst:
        src.backup(dst)
    dst.close()
    print(f"backup: {backup}")

    conn = sqlite3.connect(str(db))
    try:
        with conn:
            conn.execute("DELETE FROM events")
            conn.executemany(
                f"INSERT INTO events ({','.join(COLUMNS)}) "
                f"VALUES ({','.join('?' * len(COLUMNS))})",
                [tuple(r.get(c) for c in COLUMNS) for r in final],
            )
        print(f"scritte {len(final)} righe in {db}")
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--apply", action="store_true",
                    help="scrive davvero (default: dry run)")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"DB non trovato: {args.db}", file=sys.stderr)
        return 1

    final, merged, changed = plan(args.db)
    print(f"=== {'APPLY' if args.apply else 'DRY RUN'} su {args.db} ===")
    report(args.db, final, merged, changed)

    if args.apply:
        apply(args.db, final)
        final2, merged2, changed2 = plan(args.db)
        print(f"\nricontrollo: {len(merged2)} fusioni residue, "
              f"{changed2} chiavi ancora da cambiare "
              f"({'idempotente' if not merged2 and not changed2 else 'NON IDEMPOTENTE'})")
    else:
        print("\n(dry run: niente scritto. --apply per applicare)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
