"""
Forma canonica del locale: la coppia (operatore, sala).

PERCHE' LA SALA E NON L'OPERATORE
Il locale entra in `dedup_key` (models.py), quindi due grafie della stessa
sala sono due righe. Ma appiattire tutto sull'operatore e' l'errore opposto,
e il DB ha il controesempio: il 22/08 "SOBER in the garden" compare a
Tolhuistuin e a Parallel, due sale diverse dello stesso operatore, con due
url RA diverse (2509416 e 2514604). Sono due eventi. Fondendoli sull'operatore
"Paradiso" se ne perde uno.

Quindi la canonica e' la SALA, e l'operatore resta come metadato: serve a
raggruppare per cartellone, non a decidere l'identita'.

IL PATTERN E' UNA REGOLA SOLA, NON 18 CASI SPECIALI
La fonte `paradiso` scrive "Paradiso (Sala)"; RA e amsterdam_alt scrivono la
sala e basta. Da qui le 18 grafie di "Paradiso": Tolhuistuin ha 167 eventi su
3 grafie (Tolhuistuin, Paradiso (Tolhuistuin), Paradiso Noord / Tolhuistuin),
Bitterzoet 65 su 2, Muziekgebouw 33 su 2 — e le due grafie del Muziekgebouw
differiscono per la POSIZIONE di un apostrofo: "aan 't IJ" contro "aan t' IJ".

Su 413 stringhe distinte se ne fondono 15. Sono poche, ma sono quelle che
pesano.

NIENTE MATCH PER SOTTOSTRINGA
Le mappe qui sotto sono a corrispondenza esatta, di proposito. Un
`LIKE '%ITA%'` durante l'analisi ha agganciato "De Nieuwe Anita", e
`'%Foundation%'` ha agganciato "The Dirty art Foundation", che con
"Paradiso (Foundation)" non c'entra niente. La sottostringa qui non e' un
indizio: e' un falso positivo che aspetta.

DUE FORME, E SERVONO ENTRAMBE
`key` e' senza accenti e minuscola, e serve solo a confrontare.
`sala` e' la forma da mostrare e conserva gli accenti: "Chassé Dance Studios"
deve restare "Chassé Dance Studios".

TBA NON E' UN LOCALE
Sono ~100 righe fra "TBA" secco e una cinquantina di "TBA - qualcosa", e
vogliono dire "da annunciare". Normalizzarle insieme e' tecnicamente corretto
e produce un mostro: nel filtro dei locali comparirebbe "TBA" come una sala
con 46 serate in cartellone. Qui restano DISTINTE fra loro (`placeholder=True`
e chiave sulla stringa intera): non fondono niente e si lasciano escludere a
valle da chi mostra. La decisione su come presentarle non e' di questo modulo.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Operatori che scrivono "Operatore (Sala)". Solo questi: la parentesi da sola
# non vuol dire sala — "Basement (Amsterdam)" e' una citta', "Klaas Kompaan
# (Boat)" un descrittore, "pos (possibilities open space)" un acronimo sciolto.
OPERATORS: frozenset[str] = frozenset({"paradiso"})

# Sala (forma di confronto) → operatore che la programma. Serve a dare
# l'operatore anche quando la fonte scrive solo la sala.
SALA_OPERATOR: dict[str, str] = {
    "paradiso": "Paradiso",          # la sala grande, non l'operatore
    "bitterzoet": "Paradiso",
    "chasse dance studios": "Paradiso",
    "cinetol": "Paradiso",
    "de duif": "Paradiso",
    "documentaire paviljoen": "Paradiso",
    "doka": "Paradiso",
    "foundation": "Paradiso",
    "ita bookshop": "Paradiso",
    "internationaal theater": "Paradiso",
    "parallel": "Paradiso",
    "skatecafe": "Paradiso",
    "toekomstmuziek": "Paradiso",
    "tolhuistuin": "Paradiso",
    "vondelpark openluchttheater": "Paradiso",
    "zonnehuis": "Paradiso",
    "murmur": "Paradiso",
}

# Grafie irregolari che la normalizzazione generica non raggiunge, mappate
# sulla forma da mostrare. La chiave e' gia' in forma di confronto (senza
# punteggiatura): "Paradiso Noord / Tolhuistuin" arriva qui come
# "paradiso noord tolhuistuin", la barra e' gia' sparita.
ALIASES: dict[str, str] = {
    "paradiso noord tolhuistuin": "Tolhuistuin",
    "het veronica schip": "Veronica Schip",
}

# "Ruigoord" e "TBA - Ruigoord" NON si fondono: la seconda e' un placeholder.
# "Paradiso (Foundation)" e "The Dirty art Foundation" nemmeno: nomi diversi.

_PLACEHOLDER_RE = re.compile(
    r"^\s*(tba\b|tbc\b)|secret location|ticket holders", re.IGNORECASE
)
_PAREN_RE = re.compile(r"^(?P<op>[^()]+?)\s*\((?P<sala>[^()]+)\)\s*$")


def _strip_accents(s: str) -> str:
    d = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in d if not unicodedata.combining(ch))


def _compare_form(s: str, strip_city: bool = True) -> str:
    """
    Minuscolo, senza accenti, senza punteggiatura, spazi singoli.

    Gli apostrofi diventano spazio prima del resto: e' quello che riconcilia
    "aan 't IJ" con "aan t' IJ" (l'apostrofo cambia posto, i token no) e
    "Art'otel" con "Art''otel", dove la fonte ne ha raddoppiato uno.
    """
    s = _strip_accents(s or "")
    s = re.sub(r"['`‘’ʼ]+", " ", s)
    s = re.sub(r"[^A-Za-z0-9]+", " ", s).lower()
    s = re.sub(r"\s+", " ", s).strip()
    # Suffisso di citta' ridondante: "Oude Kerk, Amsterdam" e "Oude Kerk" sono
    # lo stesso posto, e "Paradiso (Foundation Amsterdam)" e "(Foundation)"
    # la stessa sala. Solo in coda, e solo se resta qualcosa.
    if not strip_city:
        return s
    s2 = re.sub(r"\s+amsterdam$", "", s)
    return s2 or s


def _titlecase_fallback(s: str) -> str:
    return s.strip()


@dataclass(frozen=True)
class Venue:
    """Forma canonica di un locale."""

    operator: str      # chi programma; "" se sconosciuto
    sala: str          # forma da mostrare, accenti conservati
    placeholder: bool  # True per i TBA: non e' un locale vero

    @property
    def key(self) -> str:
        """
        Forma di confronto. E' questa che entra in dedup_key.

        Sui placeholder il suffisso di citta' NON si toglie: "TBA" e
        "TBA - Amsterdam" resterebbero entrambi "tba" e due serate senza
        locale annunciato diventerebbero la stessa. Su un locale vero quel
        suffisso e' ridondante ("Oude Kerk, Amsterdam"); su un "da annunciare"
        e' l'unica cosa che distingue.
        """
        return _compare_form(self.sala, strip_city=not self.placeholder)

    def __str__(self) -> str:  # pragma: no cover - comodita' di debug
        return f"{self.operator} / {self.sala}" if self.operator else self.sala


def canonical(raw: str) -> Venue:
    """
    Stringa grezza del locale → (operatore, sala).

    Le regole, in ordine: placeholder, alias espliciti, "Operatore (Sala)" per
    gli operatori noti, sala nota con operatore, altrimenti la stringa ripulita.
    """
    raw = (raw or "").strip()
    if not raw:
        return Venue("", "", False)

    if _PLACEHOLDER_RE.search(raw):
        # Chiave sulla stringa intera: due "da annunciare" non sono lo stesso posto.
        return Venue("", raw, True)

    cf = _compare_form(raw)

    if cf in ALIASES:
        sala = ALIASES[cf]
        return Venue(SALA_OPERATOR.get(_compare_form(sala), ""), sala, False)

    m = _PAREN_RE.match(raw)
    if m and _compare_form(m.group("op")) in OPERATORS:
        sala = m.group("sala").strip()
        operator = _titlecase_fallback(m.group("op"))
        return Venue(operator, sala, False)

    return Venue(SALA_OPERATOR.get(cf, ""), raw, False)


def venue_key(raw: str) -> str:
    """Scorciatoia: la sola chiave di confronto. Usata da compute_dedup_key()."""
    return canonical(raw).key

def _display_rank(form: str) -> tuple:
    """Chiave d'ordinamento: piu' bassa = forma preferita per il display."""
    citta = 1 if re.search(r",?\s*amsterdam\s*$", form, re.I) else 0
    paren = 1 if _PAREN_RE.match(form) else 0
    minuscole = any(c.islower() for c in form)
    maiuscole = any(c.isupper() for c in form)
    misto = 0 if (minuscole and maiuscole) else 1
    return (citta, paren, misto, len(form), form)


def display_name(forms) -> str:
    """Una sola scrittura leggibile per le tante forme dello stesso locale.

    `canonical()` normalizza la CHIAVE ma restituisce la stringa grezza come sala:
    perfetto per deduplicare gli eventi, insufficiente per un menu a tendina, dove
    "50 HERTZ" e "50:Hertz" restano due voci distinte. Segnalato da un utente il
    2026-08-20 ("some items are repeated"), che di quel locale ne vedeva QUATTRO.

    La scelta e' deterministica, per criteri in ordine:
      1. senza il suffisso della citta': "Eye Filmmuseum" batte "Eye Filmmuseum, Amsterdam";
      2. la sala nuda invece di "Operatore (Sala)", cosi' le tre sale del Paradiso si
         comportano allo stesso modo — sceglierle per frequenza dava Bitterzoet come
         "Paradiso (Bitterzoet)" e Cinetol come "Cinetol", cioe' un'incoerenza visibile;
      3. capitalizzazione mista ("Murmur") invece di TUTTO MAIUSCOLO o tutto minuscolo:
         il maiuscolo e' enfasi di chi pubblica, non il nome del posto;
      4. la piu' corta, poi l'ordine alfabetico: a parita' serve un risultato STABILE,
         altrimenti il filtro cambia nome da un giorno all'altro.
    """
    forms = [f for f in forms if f and f.strip()]
    return min(forms, key=_display_rank) if forms else ""


def display_map(raws) -> dict:
    """`{forma grezza: forma da mostrare}` per un insieme di locali."""
    gruppi: dict[str, set] = {}
    for r in raws:
        if r and r.strip():
            gruppi.setdefault(venue_key(r), set()).add(r)
    return {raw: display_name(forms) for forms in gruppi.values() for raw in forms}
