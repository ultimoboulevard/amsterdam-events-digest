#!/usr/bin/env python3
"""
Collasso degli alias delle venue.

Lo stesso posto arriva con grafie diverse a seconda della fonte: nel JSON
pubblicato del 24/10/2026 c'erano 302 venue distinte, e dentro ci stavano
'TBA', 'TBA - Amsterdam', 'TBA - noord' e 'TBA - Amsterdam Noord' come se
fossero quattro locali. Chi filtra per venue sul sito ne vede un quarto per
volta, e nella newsletter lo stesso posto compare sotto nomi diversi.

Qui si risolve l'identita' prima di scrivere il JSON: il DB grezzo non si
tocca, cosi' la correzione e' reversibile e non si perde l'informazione su
come la fonte aveva chiamato il posto.

Due livelli:

  - la normalizzazione (casefold, accenti, apostrofi tipografici,
    punteggiatura) cattura le varianti di scrittura;
  - ALIAS cattura i casi in cui lo stesso posto ha nomi davvero diversi,
    che nessuna regola automatica puo' indovinare.

Aggiungere una voce ad ALIAS e' una decisione editoriale, non tecnica: due
sale nello stesso edificio possono essere due venue legittime. Vedi
NON_UNITI in fondo per i casi lasciati separati di proposito.
"""
from __future__ import annotations

import re
import unicodedata

# Variante → nome canonico. La chiave viene normalizzata, quindi non serve
# ripetere maiuscole o apostrofi diversi della stessa variante.
ALIAS: dict[str, str] = {
    # Luogo non ancora annunciato: quattro modi di dire la stessa cosa.
    "TBA - Amsterdam": "TBA",
    "TBA - Amsterdam Noord": "TBA",
    "TBA - noord": "TBA",
    "TBA - AMSTERDAM Secret Location": "TBA - Secret Location",
    "TBA - De Ruijterkade 14, Amsterdam": "De Ruijterkade 14",

    # Il terreno dell'ADM a Noord: due fonti, due nomi, stesso posto.
    "ADMnoord - 't Groene Veld": "Het Groene Veld / ADM",
    "Het Groene Veld": "Het Groene Veld / ADM",

    # Differenze di sola grafia fra fonti.
    "50:HERTZ HOUSE & TECHNO Club Rembrandt Square": "50:Hertz House & Techno Cafe Rembrandt Square",
    "TILLATEC": "Tilla Tec",
    "Feijoa": "Bar Feijoa",
    "Fort van Sjakoo": "Het Fort van Sjakoo",
}

# Casi simili che restano separati di proposito, perche' unirli perderebbe
# informazione vera. Documentati qui perche' ricompaiono a ogni revisione:
#
#   'Paradiso' / 'Paradiso (Tolhuistuin)'
#       Sale diverse, quartieri diversi: 221 e 83 eventi. Unirle direbbe
#       al lettore di andare nel posto sbagliato.
#   'Eetcafe ADM Noord' / 'Het Groene Veld / ADM'
#       Stesso terreno, ambienti diversi: il cafe' non e' il campo aperto.
#       Se un giorno decidi che per il lettore e' lo stesso indirizzo,
#       basta spostarlo in ALIAS.
#   'Supperclub Cruise' / 'Supper Cruise'
#       Probabilmente lo stesso, non verificato. Da controllare prima di
#       unirli: unire due posti diversi e' peggio che tenerne due uguali.
NON_UNITI = (
    "Paradiso | Paradiso (Tolhuistuin)",
    "Eetcafe ADM Noord | Het Groene Veld / ADM",
    "Supperclub Cruise | Supper Cruise",
)

_APOSTROFI = str.maketrans({"‘": "'", "’": "'", "ʼ": "'", "`": "'"})


def _chiave(nome: str) -> str:
    """Forma confrontabile: senza accenti, punteggiatura, maiuscole."""
    s = nome.translate(_APOSTROFI)
    s = unicodedata.normalize("NFKD", s.casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


_ALIAS_PER_CHIAVE = {_chiave(k): v for k, v in ALIAS.items()}


def canonicalizza(nome: str) -> str:
    """
    Nome canonico di una venue.

    Senza corrispondenza restituisce il nome originale ripulito dagli spazi:
    meglio una venue in piu' che una sbagliata.
    """
    if not nome:
        return ""
    pulito = " ".join(nome.split())
    return _ALIAS_PER_CHIAVE.get(_chiave(pulito), pulito)


def rapporto(nomi: list[str]) -> list[tuple[str, list[str]]]:
    """
    Quali nomi collassano su quale canonico, per l'elenco dato.

    Serve a vedere l'effetto prima di pubblicare, e a scoprire varianti nuove
    quando una fonte cambia il modo di scrivere un posto.
    """
    gruppi: dict[str, set[str]] = {}
    for n in nomi:
        gruppi.setdefault(canonicalizza(n), set()).add(n)
    return sorted(
        ((c, sorted(v)) for c, v in gruppi.items() if len(v) > 1),
        key=lambda x: x[0].lower(),
    )


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    percorso = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("site/data/events.json")
    dati = json.loads(percorso.read_text(encoding="utf-8"))
    nomi = [e.get("venue", "") for e in dati.get("events", []) if e.get("venue")]
    prima = len(set(nomi))
    dopo = len({canonicalizza(n) for n in nomi})
    print(f"{percorso}: {prima} venue distinte → {dopo} dopo il collasso\n")
    for canonico, varianti in rapporto(nomi):
        print(f"  {canonico}")
        for v in varianti:
            marca = "  " if v == canonico else "← "
            print(f"      {marca}{v}")
