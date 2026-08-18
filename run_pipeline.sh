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

# Credenziali SMTP: file non versionato, solo per il ramo "send".
# Deve contenere EMAIL_USER / EMAIL_PASS / EMAIL_TO.
if [ -f "$BASE/.env" ]; then
    . "$BASE/.env"
    export EMAIL_USER EMAIL_PASS EMAIL_TO
fi

case "$1" in
    collect)
        "$PY" main.py collect && "$PY" export_json.py
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
