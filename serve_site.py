#!/usr/bin/env python3
"""serve_site.py — come `python3 -m http.server`, ma dice anche CHI ha bussato.

Serve `site/` su 127.0.0.1:<porta> dietro il tunnel Cloudflare (LaunchAgent
com.local.events-site). Il modulo standard logga solo
`127.0.0.1 - - [data] "GET /path" 200 -`: dietro un tunnel quel 127.0.0.1 è SEMPRE
il proxy, quindi dal lato server non si distingue un crawler da una persona — ed è
esattamente la domanda che ci siamo posti guardando 369 richieste "curl" senza
riuscire ad attribuirle (19/08/2026).

Qui si aggiungono in coda a ogni riga:
  - l'IP VERO del visitatore, da `CF-Connecting-IP` (Cloudflare lo inoltra sempre);
  - lo `User-Agent`.
Formato invariato nella prima parte, così i conteggi per path/ora già scritti
continuano a funzionare.
"""
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8890
DIRECTORY = "/Users/francescozaccaria/Newsletter eventi/site"


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        ua = self.headers.get("User-Agent", "-")
        ip = (self.headers.get("CF-Connecting-IP")
              or self.headers.get("X-Forwarded-For")
              or self.client_address[0])
        sys.stderr.write(f'{ip} - - [{self.log_date_time_string()}] '
                         f'{fmt % args} "{ua}"\n')
        sys.stderr.flush()


if __name__ == "__main__":
    ThreadingHTTPServer.allow_reuse_address = True
    with ThreadingHTTPServer(("127.0.0.1", PORT),
                             partial(Handler, directory=DIRECTORY)) as httpd:
        httpd.serve_forever()
