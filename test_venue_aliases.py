#!/usr/bin/env python3
"""Test del collasso alias venue. Nessuna rete: `python3 test_venue_aliases.py`."""
from venue_aliases import canonicalizza, rapporto


def test_varianti_tba_collassano():
    for v in ("TBA - Amsterdam", "TBA - Amsterdam Noord", "TBA - noord", "tba - AMSTERDAM"):
        assert canonicalizza(v) == "TBA", v


def test_apostrofo_tipografico():
    # La fonte scrive ‘t con l'apostrofo curvo; la tabella ha quello dritto.
    assert canonicalizza("ADMnoord - ‘t Groene Veld") == "Het Groene Veld / ADM"
    assert canonicalizza("Het Groene Veld") == "Het Groene Veld / ADM"


def test_maiuscole_e_spazi():
    assert canonicalizza("TILLATEC") == "Tilla Tec"
    assert canonicalizza("  Tilla   Tec  ") == "Tilla Tec"


def test_posti_distinti_restano_distinti():
    # Sale diverse in quartieri diversi: unirle manderebbe la gente altrove.
    assert canonicalizza("Paradiso") != canonicalizza("Paradiso (Tolhuistuin)")
    assert canonicalizza("Eetcafe ADM Noord") == "Eetcafe ADM Noord"


def test_sconosciuto_resta_se_stesso():
    assert canonicalizza("Un Posto Mai Visto") == "Un Posto Mai Visto"
    assert canonicalizza("") == ""


def test_rapporto_raggruppa():
    g = dict(rapporto(["TBA", "TBA - noord", "Melkweg"]))
    assert g["TBA"] == ["TBA", "TBA - noord"]
    assert "Melkweg" not in g  # senza varianti non compare


if __name__ == "__main__":
    falliti = 0
    for nome, fn in sorted(globals().items()):
        if nome.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok    {nome}")
            except AssertionError as exc:
                falliti += 1
                print(f"  FAIL  {nome}: {exc}")
    print("tutti verdi" if not falliti else f"{falliti} falliti")
    raise SystemExit(1 if falliti else 0)
