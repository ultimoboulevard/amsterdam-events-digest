"""
Collassa i doppioni al momento di mostrarli, condiviso fra export_json e
build_pages.

PERCHE' NON NEL DB
Il doppione nasce a monte: `dedup_key` (models.py:49) e' sha256 di
titolo+giorno+LOCALE, e il locale e' proprio il campo su cui le fonti non
vanno d'accordo. Nella finestra delle pagine "Paradiso" compare con 18
stringhe diverse — Paradiso, Paradiso (Tolhuistuin), Paradiso Noord /
Tolhuistuin, Parallel, Paradiso (Parallel)… — e c'e' perfino "Muziekgebouw
aan 't IJ" contro "Muziekgebouw aan t' IJ", che differiscono per la posizione
di un apostrofo. Ogni variante e' una chiave diversa, quindi una riga in piu'.

Correggere compute_dedup_key() sembra la mossa giusta ed e' una trappola: la
chiave e' la PRIMARY KEY della tabella. Cambiandone la formula, il collect
successivo non riconoscerebbe piu' le righe gia' presenti, le re-inserirebbe
con la chiave nuova e lascerebbe le vecchie orfane — il doppio dei doppioni,
piu' 2.000 righe fantasma. Serve una migrazione vera, ed e' una decisione di
Francesco, non un effetto collaterale di questo modulo.

Qui il DB resta intatto: si filtra solo cio' che si mostra, e basta togliere
la chiamata per tornare indietro.

LA REGOLA, DELIBERATAMENTE STRETTA
A parita' di giorno e di ora d'inizio, due casi e due soli:

  1. titoli normalizzati IDENTICI          → una riga sola, vince la piu' ricca
  2. stesse PAROLE e stesso LOCALE         → una riga sola (ordine diverso, o una
                                             parola in piu' tipo "in" o ": ")
  3. un titolo e' PREFISSO stretto di un
     altro, su confine di parola           → la piu' corta e' un moncone, sparisce

Due righe entrambe dettagliate non si fondono MAI, nemmeno se condividono un
prefisso lungo. La prima versione lo faceva, per transitivita', e il dry run
l'ha punita subito: il 19/08 alle 23:00 al John Doe fondeva "RAVELAND: Hard
Groove Night with Mischa Duncan" e "RAVELAND: Hard Techno & Hard Bounce",
tenute insieme da una terza riga-moncone "RAVELAND". Due code diverse sono
due eventi finche' non si dimostra il contrario.

L'ora uguale non e' un dettaglio: e' la garanzia contro il falso positivo.
Tiene separate le repliche vere, che nel DB ci sono —
  Young Boy Dancing Group   18:00 e 20:00 del 22/08
  Seb Lowe                  19:30 e 20:30 del 26/11
  Indiesleaze / Indiesleaze invites: Sharon Coone   22:00 e 23:00 del 22/08
— e che un merge per solo titolo si mangerebbe.

Vince la riga piu' ricca, non la prima: fra "Techno Tuesday" di Melkweg e
"Techno Tuesday Amsterdam, Dexon, CHAFA, Alix" di RA resta la seconda, che
dice chi suona. A parita' di contenuto vince la fonte del locale sull'aggregatore.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict

log = logging.getLogger(__name__)

# Sotto questa lunghezza (titolo normalizzato, piu' corto dei due) il prefisso
# non e' un indizio: "live" sarebbe prefisso di mezzo cartellone.
MIN_PREFIX = 10

# Parole che non distinguono un evento da un altro: due titoli che differiscono
# solo per queste sono lo stesso titolo scritto in due modi. Servono al caso 2.
_PAROLE_VUOTE = {
    "in", "on", "of", "the", "a", "an", "and", "at", "to", "for", "with",
    "de", "het", "een", "van", "en", "x", "vs", "presents", "invites",
}

# Le fonti che pubblicano il proprio cartellone sanno come si chiama casa loro.
# Gli aggregatori no, ma spesso hanno la line-up completa.
VENUE_NATIVE = {
    "melkweg", "paradiso", "bimhuis", "concertgebouw",
    "muziekgebouw", "splendor", "murmur",
}


def normalize_title(title: str) -> str:
    """Minuscolo, senza punteggiatura, spazi singoli."""
    t = re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def _content_words(title: str) -> frozenset:
    """Le parole che portano significato, senza quelle di servizio."""
    return frozenset(w for w in normalize_title(title).split() if w not in _PAROLE_VUOTE)


def _is_stub_of(short: str, long_: str) -> bool:
    """True se `short` e' un moncone di `long_`: prefisso stretto, su parola intera."""
    if not short or len(short) >= len(long_) or len(short) < MIN_PREFIX:
        return False
    if not long_.startswith(short):
        return False
    # Confine di parola: "sober" non deve agganciare "sobering".
    return long_[len(short)] == " "


def is_mojibake(text: str) -> bool:
    """
    True se il testo e' UTF-8 letto come latin-1: "POLIÇA" diventa "POLIÃ‡A".

    Il test e' il viaggio di ritorno, non una lista di caratteri sospetti: se
    ri-codificando e decodificando in UTF-8 esce qualcosa di diverso, allora
    era corrotto. Un titolo sano non sopravvive al giro.

    Si provano DUE codifiche. La prima versione tentava solo latin-1 e diceva
    "no" proprio sul caso che l'aveva motivata: in "POLIÃ‡A" il carattere ‡ e'
    U+2021, che in latin-1 non esiste — encode() sollevava eccezione e il
    mojibake passava per testo sano. cp1252, dove 0x87 e' appunto ‡, e' la
    codifica che lo smaschera, ed e' anche la piu' comune.

    Serve perche' il mojibake e' piu' LUNGO dell'originale (un accento diventa
    due caratteri) e senza questo controllo vince il confronto sulla
    lunghezza: "POLIÃ‡A" assorbirebbe "POLIÇA".
    """
    for codec in ("cp1252", "latin-1"):
        try:
            if text.encode(codec).decode("utf-8") != text:
                return True
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return False


def _richness(ev) -> tuple:
    """Quanto dice questa riga. Piu' alto vince."""
    return (
        0 if is_mojibake(ev.title or "") else 1,
        len(ev.artists or []),
        len(ev.genres or []),
        len(ev.title or ""),
        1 if ev.source in VENUE_NATIVE else 0,
    )


def collapse_duplicates(events: list) -> list:
    """
    Ritorna gli eventi senza doppioni, nell'ordine originale.

    Prima si fondono i titoli identici, poi si tolgono i monconi rimasti. Il
    23:30 del 18/08 al Melkweg ha tre righe — "Techno Tuesday", "Techno
    Tuesday: Dexon", "Techno Tuesday Amsterdam, Dexon, CHAFA, Alix" — e ne
    restano due: il moncone sparisce, le due code diverse no. E' voluto: sono
    quasi certamente lo stesso evento, ma "quasi" non basta per cancellarne uno.
    """
    slots: dict[tuple, list[int]] = defaultdict(list)
    for i, ev in enumerate(events):
        if ev.date:
            slots[(ev.date.date(), ev.date.hour, ev.date.minute)].append(i)

    titles = [normalize_title(ev.title) for ev in events]
    keep = [True] * len(events)
    dropped = 0

    for group in slots.values():
        if len(group) == 1:
            continue

        # ── caso 1: titoli identici ──────────────────────────────
        # dedup_key come ultimo criterio: due run diverse devono scegliere la
        # stessa riga, altrimenti le pagine ballano a ogni rigenerazione.
        exact: dict[str, list[int]] = defaultdict(list)
        for i in group:
            if titles[i]:
                exact[titles[i]].append(i)
        survivors = []
        for same in exact.values():
            best = max(same, key=lambda i: (_richness(events[i]), events[i].dedup_key or ""))
            for i in same:
                if i != best:
                    keep[i] = False
                    dropped += 1
            survivors.append(best)

        # ── caso 2: stesse parole, stesso locale ─────────────────
        # Prende cio' che il prefisso non vede: parole invertite o una parola
        # di servizio in piu'. Misurato sul DB: 7 coppie, tutte vere —
        # "Syntax Error x TILT" / "TILT x Syntax Error",
        # "An Encounter in Materials..." / "An Encounter: Materials...".
        # Il vincolo sul LOCALE e' cio' che lo rende sicuro: senza, due serate
        # diverse con le stesse parole in due posti diversi si fonderebbero.
        per_parole: dict[tuple, list[int]] = defaultdict(list)
        senza_parole: list[int] = []          # titoli fatti solo di parole di
        for i in survivors:                   # servizio: passano oltre intatti,
            parole = _content_words(events[i].title)   # non si confrontano fra loro
            if parole:
                per_parole[(parole, normalize_title(events[i].venue or ""))].append(i)
            else:
                senza_parole.append(i)
        survivors = list(senza_parole)
        for gruppo in per_parole.values():
            best = max(gruppo, key=lambda i: (_richness(events[i]), events[i].dedup_key or ""))
            for i in gruppo:
                if i != best:
                    keep[i] = False
                    dropped += 1
            survivors.append(best)

        # ── caso 3: monconi ──────────────────────────────────────
        # Sopravvive solo chi non e' prefisso di nessun altro rimasto. Niente
        # transitivita': se restano due code diverse restano due eventi.
        for i in survivors:
            if any(j != i and _is_stub_of(titles[i], titles[j]) for j in survivors):
                keep[i] = False
                dropped += 1

    if dropped:
        log.info("Dedup: %d righe assorbite su %d (%.1f%%).",
                 dropped, len(events), 100 * dropped / max(len(events), 1))
    return [ev for i, ev in enumerate(events) if keep[i]]
