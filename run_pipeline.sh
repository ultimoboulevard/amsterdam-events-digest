#!/bin/sh
# Pipeline Amsterdam events su capitan — sostituisce .github/workflows/weekly_digest.yml
#
#   run_pipeline.sh collect   → raccolta + export JSON (ogni giorno)
#   run_pipeline.sh send      → invio digest via email (solo lunedi)
#
# ULIMIT (rimosso il 30/07/2026): db.py apriva una connessione SQLite nuova per
# ogni evento senza chiuderla. Con ~1900 eventi i descrittori finivano e SQLite
# riportava l'errore come "unable to open database file". Su Ubuntu (CI) il
# limite e' 65536 e il bug non si vedeva; su macOS il soft limit e' 256, e
# servivo "ulimit -n 4096" per tamponare. Ora upsert_many() riusa una sola
# connessione e db.py chiude sempre (vedi _connection): il collect completa con
# il limite di default. Se dovesse ricomparire, la causa e' nel db, non qui.

BASE="/Users/francescozaccaria/Newsletter eventi"
PY="$BASE/.venv/bin/python"
cd "$BASE" || exit 1

# Credenziali: file non versionato. EMAIL_* servono al ramo "send",
# GEMINI_API_KEY a enrich_artists.py e a diagnose.py nel ramo "collect".
# Il "." da solo NON basta: senza il nome nella riga export la variabile
# resta nella shell e non arriva al processo python. Una GEMINI_API_KEY
# dimenticata qui farebbe girare lo step a vuoto ogni mattina, in silenzio,
# perche' enrich_artists.py e' fail-soft per progetto.
if [ -f "$BASE/.env" ]; then
    . "$BASE/.env"
    export EMAIL_USER EMAIL_PASS EMAIL_TO GEMINI_API_KEY
fi

case "$1" in
    collect)
        "$PY" main.py collect && "$PY" enrich_artists.py \
            && "$PY" export_json.py && "$PY" build_pages.py \
            && "$PY" indexnow.py
        # I controlli di salute degli scraper (health.py) e la diagnosi
        # (diagnose.py) NON sono un passo a se': girano dentro "main.py
        # collect", subito dopo la raccolta e prima di tutto il resto. Devono
        # misurare l'output dei collector, che esiste solo li' dentro: dal DB
        # si vedrebbe diluito con le righe buone di ieri. Non cambiano il
        # codice d'uscita — con questo `&&`, un allarme su un campo fermerebbe
        # il sito.
        #
        # enrich_artists.py sta QUI e non dentro i generatori: e' l'unico
        # pezzo che parla con Gemini, export_json e build_pages leggono solo
        # la cache. Se salta (chiave assente, API giu'), esce 0 e le pagine
        # si generano identiche a ieri. In regime sono ~17 titoli nuovi al
        # giorno: una chiamata.
        # indexnow.py annuncia solo le 9 pagine che cambiano ogni giorno
        # (today, tonight, weekend...), non tutte e 148: le pagine-data
        # cambiano solo se cambia il cartellone di quel giorno.
        # Non fallisce mai rumorosamente: se Bing non risponde, il sito
        # e' comunque aggiornato e domani si riprova.
        ;;
    send)
        # La newsletter va a un destinatario esterno: se manca la password non
        # si tenta l'invio, si fallisce rumorosamente nel log.
        if [ -z "$EMAIL_PASS" ]; then
            echo "ERRORE: EMAIL_PASS non impostata ($BASE/.env assente o incompleto). Invio saltato."
            exit 1
        fi
        "$PY" main.py send
        ;;
    *)
        echo "uso: $0 {collect|send}"
        exit 2
        ;;
esac
