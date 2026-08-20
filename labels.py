"""
Etichette condivise: fonte → nome leggibile, tipo di evento → emoji.

Fino al 18/08/2026 le stesse due mappe esistevano in due copie divergenti:
site/app.js (12 fonti, completa) e html_builder.py (6 fonti). La copia corta
non produceva un bug visibile solo per fortuna: le 5 fonti mancanti
(bimhuis, concertgebouw, splendor, murmur, museumkaart) sono parole singole,
e il fallback `source.title()` le rendeva comunque bene. La prossima fonte con
un underscore nel nome sarebbe uscita come "Amsterdam_Alt".

Questo modulo e' la mappa unica lato Python: html_builder e build_pages
importano da qui. La copia in site/app.js resta dov'e' (il browser non puo'
importare Python), ma build_pages la rilegge a ogni run e avvisa se le due
divergono — vedi _check_js_labels_in_sync().
"""
from __future__ import annotations

import re

# Deve restare identica a SOURCE_LABELS in site/app.js.
SOURCE_LABELS: dict[str, str] = {
    "melkweg": "Melkweg",
    "amsterdam_alt": "Amsterdam Alternative",
    "ra": "Resident Advisor",
    "paradiso": "Paradiso",
    "murmur": "Murmur",
    "museumkaart": "Museumkaart",
    "gallery_viewer": "Gallery Viewer",
    "concertgebouw": "Concertgebouw",
    "muziekgebouw": "Muziekgebouw",
    "sitp": "Space is the Place",
    "offbeat": "Offbeat Amsterdam",
    "ndsm": "NDSM",
    "splendor": "Splendor",
    "bimhuis": "Bimhuis",
}

# Deve restare identica a TYPE_EMOJI in site/app.js.
TYPE_EMOJI: dict[str, str] = {
    "Concert": "🎸",
    "Club": "🎧",
    "Film": "🎬",
    "Festival": "🎪",
    "Exhibition": "🖼️",   # era "Expositie": ora i tipi si normalizzano in inglese
}


# Le fonti scrivono il tipo ciascuna nella propria lingua e con le proprie sfumature:
# "Clubnacht" accanto a "Club", "Tentoonstelling" accanto a "Expositie" e "Exhibition",
# "Workshop, class" accanto a "Workshop". Un utente (20/08/2026) l'ha vista come una lista
# ridondante da filtrare — ma il danno era peggiore e invisibile: dotClass() in app.js
# confronta con 'Club' in INGLESE, quindi i 37 "Clubnacht" finivano nel pallino rosa
# "Other" invece che nel ciano dei club. La tassonomia bilingue non era solo scomoda:
# coloriva male il calendario.
#
# Le chiavi sono in minuscolo (confronto case-insensitive). Chi non e' qui resta com'e':
# meglio un tipo grezzo che una traduzione inventata.
TYPE_SYNONYMS: dict[str, str] = {
    # olandese → inglese
    "clubnacht": "Club",
    "tentoonstelling": "Exhibition",
    "expositie": "Exhibition",
    "activiteit": "Activity",
    "rondleiding": "Tour",
    "lezing": "Talk",
    # generici della fonte, che non dicono nulla ("evento", "tutto", "altro")
    "evenement": "Misc",
    "alles": "Misc",
    "overige": "Misc",
    # varianti inglesi della stessa cosa
    "workshop, class": "Workshop",
    "exhibition, art": "Exhibition",
    "food, kitchen": "Food",
    "online audiotour": "Tour",
    "dance performance": "Dance",
    # una festa di musica elettronica e' una club night: cosi' prende anche il colore giusto
    "electronic music, party": "Club",
    # categoria di UNA fonte, fuori contesto incomprensibile (segnalata dall'utente:
    # "may be understood differently by others")
    "demonstration, riot": "Misc",
}


def canonical_type(raw: str) -> str:
    """Tipo normalizzato: una sola scrittura per lingua e variante. Ignoto → invariato."""
    key = (raw or "").strip().lower()
    return TYPE_SYNONYMS.get(key, (raw or "").strip())


# ── Chi possiede un locale ─────────────────────────────────────────
# Le fonti si dividono in due specie. Alcune coprono UN locale e lo coprono
# tutto (paradiso, melkweg, bimhuis...); altre sono generaliste e rastrellano
# mezza citta' (ra, amsterdam_alt, offbeat, sitp). Quando una generalista
# incontra un locale che ha gia' la sua fonte dedicata, quegli eventi sono
# doppioni e li salta: la fonte dedicata li ha piu' completi e li aggiorna.
#
# Non e' un dettaglio estetico. dedup.py collassa solo a parita' di giorno E
# di ora, e due fonti sullo stesso concerto l'ora la scrivono diversa (porte
# contro inizio musica), quindi il doppione a valle NON viene assorbito: si
# vede sul sito. Va evitato qui.
#
# Le chiavi sono in forma `venue_key()`, che canonicalizza la SALA: cosi'
# "Paradiso (Tolhuistuin)" e "Tolhuistuin" cadono sulla stessa voce.
#
# NDSM di proposito NON sta qui. Il collector `ndsm` legge il cartellone di
# ndsm.nl, ma sulla werf succedono cose che quel cartellone non elenca —
# verificato: WIEL KLANKEN, raccolto da `sitp`, su ndsm.nl non c'e'. Dare a
# `ndsm` la proprieta' dell'intera werf farebbe sparire quegli eventi.
VENUE_OWNER: dict[str, str] = {
    "paradiso": "paradiso",
    "tolhuistuin": "paradiso",
    "bitterzoet": "paradiso",
    "parallel": "paradiso",
    "melkweg": "melkweg",
    "concertgebouw": "concertgebouw",
    "het concertgebouw": "concertgebouw",
    "muziekgebouw": "muziekgebouw",
    "muziekgebouw aan t ij": "muziekgebouw",
    "splendor": "splendor",
    "bimhuis": "bimhuis",
}


def venue_owner(venue: str, exclude: str = "") -> str:
    """La fonte dedicata che copre gia' questo locale, o '' se e' libero.

    `exclude` e' la fonte che sta chiedendo: un collector non e' doppione di
    se stesso.
    """
    from venues import venue_key  # import qui: venues non deve dipendere da labels

    owner = VENUE_OWNER.get(venue_key(venue or ""))
    return "" if not owner or owner == exclude else owner


def owner_in_text(text: str, exclude: str = "") -> str:
    """Come venue_owner(), ma per le fonti che il locale non lo danno.

    Alcune fonti scrivono la sede solo dentro un testo libero. Qui si cerca
    il nome del locale nel testo, su confine di parola.

    venues.py mette in guardia dal match per sottostringa, e ha ragione: un
    `LIKE '%ITA%'` durante l'analisi aveva agganciato "De Nieuwe Anita". La
    differenza e' che qui gli aghi non sono arbitrari, sono gli undici nomi
    qui sopra — tutti lunghi e distintivi — e il confine di parola impedisce
    che "parallel" si prenda "parallelweg".

    Il confine e' sulle sole LETTERE, non `\\b`: le descrizioni incollano
    l'ora al locale senza spazio ("18.30BIMHUIS, Amsterdam"), e fra "0" e "b"
    `\\b` non vede nessun confine.
    """
    low = (text or "").lower()
    for key, owner in VENUE_OWNER.items():
        if owner == exclude:
            continue
        if re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", low):
            return owner
    return ""


def source_label(source: str) -> str:
    """Nome leggibile della fonte. Fallback: il codice con l'iniziale maiuscola."""
    return SOURCE_LABELS.get(source, (source or "").replace("_", " ").title())


def type_emoji(event_type: str) -> str:
    """Emoji del tipo di evento. Fallback: 📌, come in app.js."""
    return TYPE_EMOJI.get(event_type, "📌")


# ── Cosa NON e' un genere ──────────────────────────────────────────
# Il campo `genres` non contiene generi: e' il raccoglitore dove ogni fonte versa le
# proprie parole chiave. Misurato il 20/08/2026 sui 576 valori distinti: il terzo piu'
# frequente e' "dapperbuurt", un QUARTIERE (327 volte), e offbeat da sola produce il 77%
# dei tag. Un'utente l'aveva detto con altre parole: "too many sub-categories... makes it
# really hard to filter".
#
# Qui si toglie dal FILTRO cio' che genere non e'. Il dato resta sull'evento: e' una
# scelta di presentazione, non una cancellazione.
#
# I QUARTIERI si escludono ma si tengono raggruppati: non sono spazzatura, sono
# l'informazione piu' utile che il sito non offre ancora ("cosa c'e' stasera in zona").
# Diventeranno un filtro "Area" a parte — deciso di rimandarlo, non di buttarlo.
GENRE_NEIGHBOURHOODS: frozenset[str] = frozenset({
    "dapperbuurt", "spui", "eastern docklands", "de pijp", "transvaalbuurt",
    "schinkelbuurt", "minervahaven", "westpoort", "westerpark", "hamerkwartier",
    "buiksloterham", "leidseplein", "vondelparkbuurt", "ndsm", "noorderpark",
    "burgwallen", "slotervaart", "rembrandtpark", "jordaan", "ijplein",
    "weesperzijde", "sloterdijk", "oud-zuid", "oud-west", "schinkelterrein",
})

# Duplicano il filtro Type: e' la ridondanza fra i due menu che l'utente ha notato.
GENRE_EVENT_TYPES: frozenset[str] = frozenset({
    "concert", "theater", "theatre", "festivals", "film", "workshop", "performance",
    "comedy", "cabaret", "exhibition", "club night", "club", "party", "kids", "bar",
    "dance performance", "guided tour", "movie night", "lecture", "discussion",
    "documentary", "music documentary", "stand-up", "meeting", "class", "classes",
    "jam session", "opening", "kleinkunst", "muziektheater",
})

# Attivita' e temi: legittimi, ma non sono generi musicali.
GENRE_ACTIVITIES: frozenset[str] = frozenset({
    "yoga", "food", "community", "stories", "storytelling", "games", "books",
    "dinner", "vegan", "meditation", "plants", "repair", "voku", "activism",
    "gender", "lgbtq+", "flinta", "queer", "stadspas", "poetry", "poezie",
    "creatieve industrie", "multidisciplinair", "emancipatie & identiteit",
    "geopolitiek & mensenrechten", "politiek & democratie", "idee & verbeelding",
    "literature / science / politics / art",
})

# Meta-tag che non restringono niente: "music" su un sito di musica.
GENRE_NOISE: frozenset[str] = frozenset({
    "music", "muziek", "live", "live music", "dj", "hits", "vinyl", "dutch", "urban",
})

# Nomi propri di rassegne: utili DENTRO un locale, incomprensibili in un menu globale.
GENRE_PROGRAMMES: frozenset[str] = frozenset({
    "ade", "amsterdam dance event", "cello bienna1e amsterdam", "cello biënnale amsterdam",
    "impro focus", "headliners", "bimhuis & the rest is noise",
    "bimhuis & space is the place", "new dutch jazz", "bimhuis composition assignment 2026", "free lunchtime", "summer dance forever",
    "live after london calling", "live after lowlands", "live after best kept secret",
    "live after down the rabbit hole",
})

GENRE_EXCLUDE = (GENRE_NEIGHBOURHOODS | GENRE_EVENT_TYPES | GENRE_ACTIVITIES
                 | GENRE_NOISE | GENRE_PROGRAMMES)

# Un genere che compare una volta sola non e' un filtro: ci clicchi e trovi un evento.
# Sui dati del 20/08, 213 valori su 576 comparivano UNA volta. La soglia e' scelta sulla
# curva misurata quel giorno, non a occhio:
#     >=3 -> 159 voci, coprono l'88% degli eventi taggati
#     >=5 ->  94 voci, coprono l'80%      <- scelta: -84% di voci, si perde il 20%
#     >=8 ->  58 voci, coprono il 73%
# I tag esclusi restano sull'evento e restano cercabili: sparisce solo la voce dal menu.
GENRE_MIN_COUNT = 5


def is_genre(tag: str) -> bool:
    """False per quartieri, tipi, attivita', meta-tag e nomi di rassegna."""
    return bool(tag) and tag.strip().lower() not in GENRE_EXCLUDE


# ── Danza: un TIPO di evento, non un genere musicale ───────────────
# Nei dati la parola "dance" vale tre cose diverse (misurato 20/08/2026):
#   1. la DISCIPLINA — "Dance (contemporary, urban, etc)" del Paradiso: Locking &
#      Waacking, Popping, Fresh Moves, in cartellone con Internationaal Theater e
#      Chassé Dance Studios. Questi sono spettacoli di danza;
#   2. la MUSICA da ballo — "Dancehall", "Dance pop", "Indie Dance": generi musicali,
#      che stanno in World/Pop/Electronic e NON qui;
#   3. il tag generico "dance" di offbeat, che mescola pole dance, feste e perfino una
#      meditazione: inaffidabile, si ignora.
# Il guaio pratico: gli spettacoli del gruppo 1 arrivano con event_type "Concert",
# quindi la categoria Dance del filtro ne contava 12 invece di ~28. Qui si corregge il
# TIPO leggendo il genere — l'unico segnale affidabile che quelle fonti danno.
DANCE_GENRE_HINTS: frozenset[str] = frozenset({
    "dance (contemporary, urban, etc)", "dance performance", "dans", "ballet",
    "urban dance", "contemporary dance",
})


def type_from_genres(event_type: str, genres) -> str:
    """Tipo corretto: gli spettacoli di danza smettono di essere 'Concert'."""
    for g in (genres or []):
        if (g or "").strip().lower() in DANCE_GENRE_HINTS:
            return "Dance"
    return canonical_type(event_type)
