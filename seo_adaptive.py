#!/usr/bin/env python3
"""seo_adaptive.py — adatta title e description a cio' che la gente CERCA davvero.

Il ciclo, in quattro tempi:

  1. MISURA    `search_console.py` scarica ogni giorno (query, pagina, impression,
               click, CTR, posizione) in data/search_console.db. Gia' esistente.
  2. DIAGNOSI  qui: si cercano quattro situazioni azionabili (vedi `diagnose`).
  3. APPLICA   `--apply` promuove al massimo MAX_PER_RUN proposte in
               data/seo_overrides.json, che build_pages._shell() legge a ogni build.
               Poche per volta: se ne cambi trenta insieme non sai piu' QUALE ha mosso
               i numeri, e con un solo segnale globale il passo 4 e' cieco.
  4. VERIFICA  `--verify` dopo VERIFY_DAYS confronta CTR e posizione con la baseline
               salvata al momento dell'applicazione: se sono migliorati l'override
               resta, se sono peggiorati viene RITIRATO da solo. E' questo che rende
               il sistema adattivo invece che semplicemente automatico.

Cosa NON fa, deliberatamente: non inventa testi con un LLM. Propone di includere la
query REALE che gia' porta impression, perche' il guadagno vero sta nel dire quello
che la gente digita — non nel generare prosa nuova, che e' il modo piu' rapido di
scivolare nel keyword stuffing e farsi penalizzare.

Sui tempi: Search Console consolida con ~3 giorni di ritardo e Google impiega giorni a
rivalutare una pagina. Un giro completo e' 2-3 settimane. Non esiste il SEO in tempo reale.

  python3 seo_adaptive.py            # diagnosi + report, non tocca niente
  python3 seo_adaptive.py --apply    # promuove le proposte migliori a override
  python3 seo_adaptive.py --verify   # tiene o ritira, in base ai numeri
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SC_DB = BASE_DIR / "data" / "search_console.db"
OVERRIDES = BASE_DIR / "data" / "seo_overrides.json"
PROPOSALS = BASE_DIR / "data" / "seo_proposals.json"
REPORT = BASE_DIR / "logs" / "seo_adaptive.md"

WINDOW_DAYS = 28
MIN_IMPRESSIONS = 30      # sotto questa soglia i numeri sono rumore, non segnale
NEAR_LO, NEAR_HI = 8.0, 20.0
MAX_PER_RUN = 5
VERIFY_DAYS = 14
TITLE_MAX = 60            # oltre, Google tronca nella SERP

# CTR tipico per posizione: serve a distinguere "ti vedono e non cliccano" da
# "sei semplicemente in fondo". Valori d'ordine di grandezza, non verita' rivelate.
CTR_ATTESO = {1: .25, 2: .15, 3: .11, 4: .08, 5: .06, 6: .05, 7: .04}


@dataclass
class Finding:
    kind: str
    url: str
    query: str
    impressions: int
    clicks: int
    ctr: float
    position: float
    note: str


def _rows(days: int = WINDOW_DAYS) -> list[dict]:
    """Aggregato (pagina, query) sulla finestra: impression, click, CTR, posizione."""
    if not SC_DB.exists():
        return []
    since = (date.today() - timedelta(days=days)).isoformat()
    con = sqlite3.connect(f"file:{SC_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute("""
            SELECT page, query,
                   SUM(clicks) AS clicks, SUM(impressions) AS impressions,
                   AVG(position) AS position
            FROM search_analytics WHERE day >= ?
            GROUP BY page, query
        """, (since,))
        out = []
        for r in cur:
            impr = r["impressions"] or 0
            out.append({"page": r["page"], "query": r["query"],
                        "clicks": r["clicks"] or 0, "impressions": impr,
                        "position": r["position"] or 0.0,
                        "ctr": (r["clicks"] or 0) / impr if impr else 0.0})
        return out
    finally:
        con.close()


def _indexed_urls() -> set[str]:
    if not SC_DB.exists():
        return set()
    con = sqlite3.connect(f"file:{SC_DB}?mode=ro", uri=True)
    try:
        return {u for (u,) in con.execute(
            "SELECT url FROM url_inspection WHERE coverage LIKE '%indexed%' "
            "AND coverage NOT LIKE '%not indexed%'")}
    except sqlite3.Error:
        return set()
    finally:
        con.close()


def diagnose() -> list[Finding]:
    """Le quattro situazioni che vale la pena guardare."""
    rows = _rows()
    found: list[Finding] = []
    pages_con_dati = {r["page"] for r in rows}

    for r in rows:
        if r["impressions"] < MIN_IMPRESSIONS:
            continue
        pos, ctr = r["position"], r["ctr"]
        if NEAR_LO <= pos <= NEAR_HI:
            found.append(Finding(
                "quasi-prima-pagina", r["page"], r["query"], r["impressions"],
                r["clicks"], ctr, pos,
                f"posizione {pos:.1f}: includere «{r['query']}» in title/description"))
        elif pos < NEAR_LO:
            atteso = CTR_ATTESO.get(int(round(pos)), .04)
            if ctr < atteso * .6:
                found.append(Finding(
                    "ti-vedono-non-cliccano", r["page"], r["query"], r["impressions"],
                    r["clicks"], ctr, pos,
                    f"CTR {ctr:.1%} contro ~{atteso:.0%} atteso a pos {pos:.1f}: "
                    f"il title non attira"))

    # indicizzata ma invisibile: e' un problema di CONTENUTO, non di titolo
    for url in sorted(_indexed_urls() - pages_con_dati):
        found.append(Finding("indicizzata-ma-invisibile", url, "", 0, 0, 0.0, 0.0,
                             "in indice ma zero impression: nessuno la cerca cosi'"))

    found.sort(key=lambda f: (-f.impressions, f.url))
    return found


def _suggest_title(current: str, query: str) -> str | None:
    """Title che include la query reale, se ci sta. None se e' gia' dentro o non ci sta."""
    if not current or not query or query.lower() in current.lower():
        return None
    cand = f"{current} — {query}" if len(current) + len(query) + 3 <= TITLE_MAX else None
    return cand


def propose() -> list[dict]:
    props = []
    for f in diagnose():
        if f.kind == "quasi-prima-pagina":
            props.append({"url": f.url, "query": f.query, "kind": f.kind,
                          "impressions": f.impressions, "baseline_ctr": f.ctr,
                          "baseline_pos": f.position, "note": f.note})
    PROPOSALS.write_text(json.dumps(props, indent=2, ensure_ascii=False), encoding="utf-8")
    return props


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def apply_proposals(limit: int = MAX_PER_RUN) -> int:
    """Promuove le prime `limit` proposte a override, salvando la baseline."""
    props = propose()
    ov = _load(OVERRIDES)
    n = 0
    for p in props:
        if p["url"] in ov or n >= limit:
            continue
        ov[p["url"]] = {
            "query": p["query"], "applied_at": datetime.now(timezone.utc).isoformat(),
            "baseline_ctr": p["baseline_ctr"], "baseline_pos": p["baseline_pos"],
            # title/description restano VUOTI: li scrive una persona (o li propone
            # _suggest_title). Un override vuoto e' un no-op in _shell, non un danno.
            "title": "", "description": "",
        }
        n += 1
    if n:
        OVERRIDES.write_text(json.dumps(ov, indent=2, ensure_ascii=False), encoding="utf-8")
    return n


def verify() -> list[str]:
    """Dopo VERIFY_DAYS: tiene cio' che ha migliorato, ritira cio' che ha peggiorato."""
    ov = _load(OVERRIDES)
    if not ov:
        return []
    now, oggi = datetime.now(timezone.utc), _rows()
    per_url: dict[str, dict] = {}
    for r in oggi:
        cur = per_url.setdefault(r["page"], {"impressions": 0, "clicks": 0, "pos": []})
        cur["impressions"] += r["impressions"]; cur["clicks"] += r["clicks"]
        cur["pos"].append(r["position"])

    esiti, cambiato = [], False
    for url, meta in list(ov.items()):
        try:
            applied = datetime.fromisoformat(meta.get("applied_at", ""))
        except ValueError:
            continue
        if (now - applied).days < VERIFY_DAYS:
            continue
        d = per_url.get(url)
        if not d or not d["impressions"]:
            esiti.append(f"{url}: nessun dato dopo {VERIFY_DAYS} giorni — lascio stare")
            continue
        ctr = d["clicks"] / d["impressions"]
        pos = sum(d["pos"]) / len(d["pos"])
        meglio = ctr >= meta.get("baseline_ctr", 0) or pos <= meta.get("baseline_pos", 99)
        if meglio:
            esiti.append(f"{url}: CTR {meta.get('baseline_ctr',0):.1%}→{ctr:.1%}, "
                         f"pos {meta.get('baseline_pos',0):.1f}→{pos:.1f} — TENGO")
        else:
            del ov[url]; cambiato = True
            esiti.append(f"{url}: CTR {meta.get('baseline_ctr',0):.1%}→{ctr:.1%}, "
                         f"pos {meta.get('baseline_pos',0):.1f}→{pos:.1f} — RITIRO")
    if cambiato:
        OVERRIDES.write_text(json.dumps(ov, indent=2, ensure_ascii=False), encoding="utf-8")
    return esiti


def report() -> str:
    found = diagnose()
    out = ["# SEO adattivo — cosa dicono le ricerche", "",
           f"Generato: {datetime.now().isoformat(timespec='seconds')}",
           f"Finestra: ultimi {WINDOW_DAYS} giorni · soglia {MIN_IMPRESSIONS} impression", ""]
    if not _rows():
        out += ["## Nessun dato di ricerca",
                "", "`search_analytics` e' vuota: finche' Google non indicizza e non arrivano",
                "impression non c'e' niente da adattare. Il modulo resta inerte — e' l'ordine",
                "giusto: prima farsi indicizzare, poi ottimizzare.", ""]
        REPORT.write_text("\n".join(out), encoding="utf-8")
        return "\n".join(out)
    per_kind: dict[str, list[Finding]] = {}
    for f in found:
        per_kind.setdefault(f.kind, []).append(f)
    for kind, fs in per_kind.items():
        out += [f"## {kind} ({len(fs)})", ""]
        for f in fs[:15]:
            q = f"«{f.query}» " if f.query else ""
            out.append(f"- {q}su {f.url} — {f.impressions} impr, "
                       f"CTR {f.ctr:.1%}, pos {f.position:.1f} · {f.note}")
        out.append("")
    REPORT.write_text("\n".join(out), encoding="utf-8")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="promuovi le proposte a override")
    ap.add_argument("--verify", action="store_true", help="tieni o ritira, secondo i numeri")
    a = ap.parse_args()
    if a.verify:
        for line in verify() or ["niente da verificare (nessun override maturo)"]:
            print(" ", line)
        return
    if a.apply:
        print(f"✅ {apply_proposals()} override applicati (baseline salvata)")
        return
    print(report())
    print(f"→ report: {REPORT}")


if __name__ == "__main__":
    main()
